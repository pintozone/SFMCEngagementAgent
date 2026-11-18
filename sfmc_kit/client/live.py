"""LiveBackend: talks to a real SFMC tenant through REST + SOAP."""
from __future__ import annotations

import time
from typing import Any

import requests

from ..auth import TokenManager
from ..config import SFMCConfig
from ..modules.assets import EmailSpec
from ..modules.automation import UPDATE_TYPES, AutomationSpec, QuerySpec, automation_payload
from ..modules.data_extensions import DESpec
from .base import Backend
from .rest import RestClient, SFMCError
from .soap import SoapClient, complex_filter, simple_filter, x

ROW_BATCH = 500

AUTOMATION_STATUS = {-1: "Error", 0: "BuildError", 1: "Building", 2: "Ready", 3: "Running", 4: "Paused",
                     5: "Stopped", 6: "Scheduled", 7: "AwaitingTrigger", 8: "InactiveTrigger"}


class LiveBackend(Backend):
    mode = "live"

    def __init__(self, cfg: SFMCConfig, verbose: bool = False):
        self.cfg = cfg
        session = requests.Session()
        self.tm = TokenManager(cfg, session)
        self.rest = RestClient(self.tm, session, verbose)
        self.soap = SoapClient(self.tm, session, verbose)
        self._folder_cache: dict[tuple[str, str], int] = {}

    def whoami(self) -> dict:
        self.tm.token(force=True)
        info: dict[str, Any] = {"mode": "live", "rest": self.tm.rest_instance_url, "soap": self.tm.soap_instance_url,
                                "scopes": sorted(self.tm.scope.split())}
        try:
            u = self.rest.get(f"{self.cfg.auth_base}/v2/userinfo")
            org = u.get("organization", {})
            info.update({"mid": org.get("member_id"), "enterprise_id": org.get("enterprise_id"),
                         "user": u.get("user", {}).get("name"), "stack": org.get("stack_key")})
        except SFMCError as e:
            info["userinfo_error"] = str(e)[:200]
        return info

    # ------------------------------------------------------------------ folders
    def ensure_folder(self, content_type: str, path: str) -> int | None:
        """content_type: dataextension | asset | queryactivity | automations. Path like 'CampaignKit/EVLAUNCH'."""
        ck = (content_type, path)
        if ck in self._folder_cache:
            return self._folder_cache[ck]
        if content_type == "asset":
            fid = self._ensure_asset_folder(path)
        else:
            fid = self._ensure_data_folder(content_type, path)
        self._folder_cache[ck] = fid
        return fid

    def _ensure_data_folder(self, content_type: str, path: str) -> int:
        roots = self.soap.retrieve("DataFolder", ["ID", "Name", "ParentFolder.ID"],
                                   complex_filter(simple_filter("ContentType", "equals", content_type), "AND",
                                                  simple_filter("ParentFolder.ID", "equals", 0)))
        if not roots:
            raise SFMCError(f"No root folder for content type {content_type}")
        parent = int(roots[0]["ID"])
        for name in [p for p in path.split("/") if p]:
            found = self.soap.retrieve("DataFolder", ["ID", "Name"], complex_filter(
                complex_filter(simple_filter("ContentType", "equals", content_type), "AND",
                               simple_filter("ParentFolder.ID", "equals", parent)), "AND",
                simple_filter("Name", "equals", name)))
            if found:
                parent = int(found[0]["ID"])
                continue
            res = self.soap.create(
                '<Objects xsi:type="DataFolder">'
                f"<CustomerKey>{x(f'{content_type}_{parent}_{name}')}</CustomerKey><Name>{x(name)}</Name>"
                f"<Description>Created by SFMC Campaign Kit</Description><ContentType>{content_type}</ContentType>"
                "<IsActive>true</IsActive><IsEditable>true</IsEditable><AllowChildren>true</AllowChildren>"
                f"<ParentFolder><ID>{parent}</ID></ParentFolder></Objects>")
            parent = int(res[0]["NewID"])
        return parent

    def _ensure_asset_folder(self, path: str) -> int:
        root = self.rest.get("/asset/v1/content/categories", params={"$filter": "parentId eq 0"})
        parent = int(root["items"][0]["id"])
        for name in [p for p in path.split("/") if p]:
            kids = self.rest.get("/asset/v1/content/categories",
                                 params={"$filter": f"parentId eq {parent}", "$pagesize": 500})
            match = [c for c in kids.get("items", []) if c["name"] == name]
            if match:
                parent = int(match[0]["id"])
            else:
                parent = int(self.rest.post("/asset/v1/content/categories", {"name": name, "parentId": parent})["id"])
        return parent

    # ------------------------------------------------------------------ data extensions
    DE_PROPS = ["ObjectID", "CustomerKey", "Name", "CategoryID", "IsSendable", "Description", "CreatedDate"]

    def get_de(self, key: str) -> dict | None:
        res = self.soap.retrieve("DataExtension", self.DE_PROPS, simple_filter("CustomerKey", "equals", key))
        if not res:
            return None
        r = res[0]
        return {"key": r["CustomerKey"], "name": r["Name"], "object_id": r["ObjectID"],
                "folder_id": r.get("CategoryID"), "sendable": r.get("IsSendable") == "true"}

    def create_de(self, spec: DESpec) -> dict:
        folder = self.ensure_folder("dataextension", spec.folder)
        self.soap.create(spec.soap_xml(folder))
        de = self.get_de(spec.key)
        if not de:
            raise SFMCError(f"DE {spec.key} created but not retrievable")
        return de

    def delete_de(self, key: str) -> None:
        self.soap.delete(f'<Objects xsi:type="DataExtension"><CustomerKey>{x(key)}</CustomerKey></Objects>')

    def list_des(self, name_like: str = "") -> list[dict]:
        flt = simple_filter("Name", "like", f"%{name_like}%") if name_like else ""
        res = self.soap.retrieve("DataExtension", self.DE_PROPS, flt)
        return [{"key": r["CustomerKey"], "name": r["Name"], "sendable": r.get("IsSendable"),
                 "folder_id": r.get("CategoryID"), "created": r.get("CreatedDate")} for r in res]

    def describe_de(self, key: str) -> list[dict]:
        res = self.soap.retrieve(
            "DataExtensionField",
            ["Name", "FieldType", "MaxLength", "IsPrimaryKey", "IsRequired", "Ordinal", "Scale", "DefaultValue"],
            simple_filter("DataExtension.CustomerKey", "equals", key))
        out = [{"name": r["Name"], "type": r["FieldType"], "length": r.get("MaxLength"),
                "pk": r.get("IsPrimaryKey") == "true", "required": r.get("IsRequired") == "true",
                "ordinal": int(r.get("Ordinal") or 0)} for r in res]
        return sorted(out, key=lambda f: f["ordinal"])

    def upsert_rows(self, key: str, rows: list[dict], pk: list[str]) -> int:
        n = 0
        for i in range(0, len(rows), ROW_BATCH):
            batch = []
            for r in rows[i:i + ROW_BATCH]:
                keys = {k: _fmt(r[k]) for k in pk}
                values = {k: _fmt(v) for k, v in r.items() if k not in pk and v is not None}
                batch.append({"keys": keys, "values": values})
            self.rest.post(f"/hub/v1/dataevents/key:{key}/rowset", batch)
            n += len(batch)
        return n

    def read_rows(self, key: str, max_rows: int = 2500) -> list[dict]:
        out: list[dict] = []
        page = 1
        page_size = min(2500, max_rows)
        fields = {f["name"].lower(): f["name"] for f in self.describe_de(key)}
        while len(out) < max_rows:
            res = self.rest.get(f"/data/v1/customobjectdata/key/{key}/rowset",
                                params={"$page": page, "$pageSize": page_size})
            items = res.get("items", [])
            for it in items:
                merged = {**it.get("keys", {}), **it.get("values", {})}
                out.append({fields.get(k.lower(), k): v for k, v in merged.items()})
            if len(items) < page_size or not res.get("links", {}).get("next"):
                break
            page += 1
        return out[:max_rows]

    # ------------------------------------------------------------------ content builder
    def _find_asset(self, key: str) -> dict | None:
        res = self.rest.get("/asset/v1/content/assets", params={"$filter": f"customerKey eq '{key}'"})
        items = res.get("items", []) if res else []
        return items[0] if items else None

    def upsert_email(self, spec: EmailSpec) -> dict:
        folder = self.ensure_folder("asset", spec.folder)
        payload = spec.asset_payload(folder)
        existing = self._find_asset(spec.key)
        if existing:
            res = self.rest.patch(f"/asset/v1/content/assets/{existing['id']}", payload)
            created = False
        else:
            res = self.rest.post("/asset/v1/content/assets", payload)
            created = True
        legacy = (res.get("legacyData") or {}).get("legacyId")
        if not legacy:  # legacy id can lag a moment after creation
            time.sleep(2)
            legacy = ((self._find_asset(spec.key) or {}).get("legacyData") or {}).get("legacyId")
        return {"id": res["id"], "key": spec.key, "legacy_id": legacy, "created": created}

    def delete_asset(self, asset_id: Any) -> None:
        self.rest.delete(f"/asset/v1/content/assets/{asset_id}", allow_404=True)

    # ------------------------------------------------------------------ automation studio
    def _query_folder(self) -> int | None:
        res = self.rest.get("/automation/v1/folders", params={"$filter": "categorytype eq queryactivity"})
        items = res.get("items", []) if res else []
        roots = [f for f in items if not f.get("parentId")] or items
        return roots[0]["categoryId"] if roots else None

    def _find_by_key(self, path: str, key: str, id_field: str) -> dict | None:
        for flt in (f"key eq '{key}'", f"name eq '{key}'"):
            try:
                res = self.rest.get(path, params={"$filter": flt})
            except SFMCError:
                continue
            for it in (res or {}).get("items", []):
                if it.get("key") == key or it.get("name") == key:
                    return it
        return None

    def upsert_query(self, spec: QuerySpec) -> dict:
        target = self.get_de(spec.target_key)
        if not target:
            raise SFMCError(f"Query {spec.key}: target DE {spec.target_key} does not exist")
        payload = {"name": spec.name, "key": spec.key, "description": spec.description, "queryText": spec.sql,
                   "targetName": target["name"], "targetKey": target["key"], "targetId": target["object_id"],
                   "targetUpdateTypeId": UPDATE_TYPES[spec.update_type], "categoryId": self._query_folder()}
        existing = self._find_by_key("/automation/v1/queries", spec.key, "queryDefinitionId")
        if existing:
            qid = existing["queryDefinitionId"]
            self.rest.patch(f"/automation/v1/queries/{qid}", {**payload, "queryDefinitionId": qid})
            return {"id": qid, "key": spec.key, "created": False}
        res = self.rest.post("/automation/v1/queries", payload)
        return {"id": res["queryDefinitionId"], "key": spec.key, "created": True}

    def delete_query(self, query_id: str) -> None:
        self.rest.delete(f"/automation/v1/queries/{query_id}", allow_404=True)

    def upsert_automation(self, spec: AutomationSpec, query_ids: dict[str, str]) -> dict:
        payload = automation_payload(spec, query_ids)
        existing = self._find_by_key("/automation/v1/automations", spec.key, "id")
        if existing:
            aid = existing["id"]
            self.rest.patch(f"/automation/v1/automations/{aid}", {**payload, "id": aid})
            return {"id": aid, "key": spec.key, "created": False}
        res = self.rest.post("/automation/v1/automations", payload)
        return {"id": res["id"], "key": spec.key, "created": True}

    def run_automation(self, automation_id: str, wait: bool = True, timeout: int = 900) -> str:
        before = self.rest.get(f"/automation/v1/automations/{automation_id}")
        last_before = before.get("lastRunTime")
        self.rest.post(f"/automation/v1/automations/{automation_id}/actions/runallonce", {})
        if not wait:
            return "Started"
        deadline = time.time() + timeout
        seen_running = False
        while time.time() < deadline:
            time.sleep(10)
            a = self.rest.get(f"/automation/v1/automations/{automation_id}")
            status = AUTOMATION_STATUS.get(a.get("status"), str(a.get("status")))
            if status == "Running":
                seen_running = True
                continue
            if seen_running or a.get("lastRunTime") != last_before:
                last_status = a.get("lastRunStatus") or status
                if str(last_status).lower() in ("error", "builderror"):
                    raise SFMCError(f"Automation {automation_id} finished with {last_status}")
                return str(last_status)
        raise SFMCError(f"Automation {automation_id} did not finish within {timeout}s")

    def delete_automation(self, automation_id: str) -> None:
        self.rest.delete(f"/automation/v1/automations/{automation_id}", allow_404=True)

    # ------------------------------------------------------------------ journey builder
    def send_defaults(self, send_classification_key: str) -> dict[str, str]:
        try:
            res = self.soap.retrieve("SendClassification",
                                     ["ObjectID", "CustomerKey", "Name", "SenderProfile.ObjectID",
                                      "DeliveryProfile.ObjectID"],
                                     simple_filter("CustomerKey", "equals", send_classification_key))
        except SFMCError as e:
            print(f"  ! could not read send classification '{send_classification_key}': {str(e)[:200]}")
            return {}
        if not res:
            print(f"  ! send classification '{send_classification_key}' not found; Journey Builder defaults apply")
            return {}
        r = res[0]
        return {"sendClassificationId": r.get("ObjectID"),
                "senderProfileId": (r.get("SenderProfile") or {}).get("ObjectID"),
                "deliveryProfileId": (r.get("DeliveryProfile") or {}).get("ObjectID")}

    def _get_or_none(self, path: str) -> dict | None:
        """Journey Builder answers a lookup by unknown key with 400 (not 404)."""
        try:
            return self.rest.get(path, allow_404=True)
        except SFMCError as e:
            if e.status == 400:
                return None
            raise

    def upsert_event_definition(self, payload: dict) -> dict:
        key = payload["eventDefinitionKey"]
        existing = self._get_or_none(f"/interaction/v1/eventDefinitions/key:{key}")
        if existing and existing.get("id"):
            res = self.rest.put(f"/interaction/v1/eventDefinitions/{existing['id']}", {**payload, "id": existing["id"]})
            return {"id": existing["id"], "key": key, "created": False}
        res = self.rest.post("/interaction/v1/eventDefinitions", payload)
        return {"id": res["id"], "key": key, "created": True}

    def delete_event_definition(self, event_def_id: str) -> None:
        self.rest.delete(f"/interaction/v1/eventDefinitions/{event_def_id}", allow_404=True)

    def upsert_journey(self, payload: dict) -> dict:
        body = {k: v for k, v in payload.items() if not k.startswith("_")}
        existing = self._get_or_none(f"/interaction/v1/interactions/key:{payload['key']}")
        if existing and existing.get("id"):
            if existing.get("status") != "Draft":
                raise SFMCError(f"Journey {payload['key']} v{existing.get('version')} is {existing.get('status')}; "
                                "create a new version in the UI or tear down before rebuilding")
            body.update({"id": existing["id"], "version": existing["version"]})
            if existing.get("modifiedDate"):  # optimistic-concurrency token required by Journey Builder
                body["modifiedDate"] = existing["modifiedDate"]
            res = self.rest.put("/interaction/v1/interactions", body)
            created = False
        else:
            res = self.rest.post("/interaction/v1/interactions", body)
            created = True
        return {"id": res["id"], "key": res["key"], "version": res.get("version", 1),
                "status": res.get("status"), "created": created}

    def publish_journey(self, journey_id: str, version: int) -> str:
        res = self.rest.post(f"/interaction/v1/interactions/publishAsync/{journey_id}",
                             params={"versionNumber": version})
        status_id = res.get("statusId")
        for _ in range(60):
            time.sleep(5)
            st = self.rest.get(f"/interaction/v1/interactions/publishStatus/{status_id}")
            status = st.get("status")
            if status not in ("PublishInProcess", "Pending", None):
                if st.get("errors"):
                    raise SFMCError(f"Journey publish failed: {st['errors']}")
                return status
        raise SFMCError("Journey publish timed out")

    def delete_journey(self, journey_id: str) -> None:
        self.rest.delete(f"/interaction/v1/interactions/{journey_id}", allow_404=True)


def _fmt(v: Any) -> Any:
    if isinstance(v, bool):
        return "true" if v else "false"
    return v
