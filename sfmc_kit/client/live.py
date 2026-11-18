"""LiveBackend: talks to a real SFMC tenant through REST + SOAP."""
from __future__ import annotations

import re
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

    def upsert_block(self, key: str, name: str, html: str, folder: str) -> dict:
        """Content Builder HTML block (used as a CloudPage body via ContentBlockByKey)."""
        from ..modules.cloudpages import HTML_BLOCK_ASSET_TYPE
        payload = {"name": name, "customerKey": key, "assetType": HTML_BLOCK_ASSET_TYPE, "content": html,
                   "category": {"id": self.ensure_folder("asset", folder)}}
        existing = self._find_asset(key)
        if existing:
            res = self.rest.patch(f"/asset/v1/content/assets/{existing['id']}", payload)
            return {"id": res["id"], "key": key, "created": False}
        res = self.rest.post("/asset/v1/content/assets", payload)
        return {"id": res["id"], "key": key, "created": True}

    # ------------------------------------------------------------------ developer tools
    def validate_sql(self, sql: str) -> dict:
        """SFMC's own syntax check for query activity SQL (doesn't run anything)."""
        r = self.rest.post("/automation/v1/queries/actions/validate/", {"Text": sql})
        clean = lambda xs: [re.sub(r"\s+", " ", (x.get("message") or "").replace("Just a heads-up...", "")).strip()
                            for x in xs or []]
        return {"valid": bool(r.get("queryValid")), "errors": clean(r.get("errors")),
                "warnings": clean(r.get("warnings"))}

    def _preview(self, legacy: Any, de_id: str, n: int) -> str | None:
        try:
            r = self.rest.post(f"/guide/v1/emails/{legacy}/dataExtension/{de_id}/row/{n}/preview?kind=html,text", {})
        except SFMCError:
            return None
        views = {v.get("contentType", ""): v.get("content", "") for v in (r.get("message") or {}).get("views", [])}
        return next((c for t, c in views.items() if "html" in t.lower()), "")

    @staticmethod
    def _identity_cols(rows: list[dict]) -> list[str]:
        """Columns whose non-empty values are unique across rows (used to recognise which row a preview shows)."""
        if not rows:
            return []
        out = []
        pref = ["FirstName", "EmailAddress", "SubscriberKey"]
        cols = sorted(rows[0].keys(), key=lambda c: (pref.index(c) if c in pref else 9, c))
        for c in cols:
            vals = [str(r.get(c) or "").strip() for r in rows]
            if all(len(v) >= 4 for v in vals) and len({v.lower() for v in vals}) == len(vals):
                out.append(c)
        return out

    @staticmethod
    def _identify(html: str, rows: list[dict], cols: list[str]) -> int | None:
        import html as _h
        text = _h.unescape(html or "").lower()
        for c in cols:
            hits = [i for i, r in enumerate(rows) if str(r.get(c)).strip().lower() in text]
            if len(hits) == 1:
                return hits[0]
        return None

    def preview_rows(self, email_key: str, de_key: str, rows: list[dict] | None = None) -> dict[int, str]:
        """Render every row of a DE server-side and return {row position in `rows`: html}.
        SFMC addresses preview rows by an internal row number that doesn't follow the REST order,
        so each render is matched back to its row by a uniquely-valued column it displays."""
        legacy, de_id = self._email_and_de(email_key, de_key)
        rows = self.read_rows(de_key, max_rows=2500) if rows is None else rows
        cols = self._identity_cols(rows)
        found: dict[int, str] = {}
        misses = 0
        for n in range(1, len(rows) + 6):
            html = self._preview(legacy, de_id, n)
            if html is None:
                misses += 1
                if misses > 5 and n > len(rows):
                    break
                continue
            i = self._identify(html, rows, cols)
            if i is not None and i not in found:
                found[i] = html
            if len(found) == len(rows):
                break
        return found

    def _email_and_de(self, email_key: str, de_key: str) -> tuple[Any, str]:
        asset = self._find_asset(email_key)
        if not asset:
            raise SFMCError(f"email {email_key} not found in Content Builder")
        de = self.get_de(de_key)
        if not de:
            raise SFMCError(f"DE {de_key} not found")
        return (asset.get("legacyData") or {}).get("legacyId"), de["object_id"]

    def render_email(self, email_key: str, de_key: str, subscriber_key: str | None = None,
                     row: int | None = None, rows: list[dict] | None = None) -> dict:
        """Server-side preview of an email for one DE row (AMPscript fully evaluated by SFMC).
        With subscriber_key, nearby preview rows are tried and the right one is recognised from its content."""
        asset = self._find_asset(email_key)
        legacy, de_id = self._email_and_de(email_key, de_key)
        rows = self.read_rows(de_key, max_rows=2500) if rows is None else rows
        subject = ((asset.get("views") or {}).get("subjectline") or {}).get("content")
        if subscriber_key:
            keycol = next((k for k in (rows[0] if rows else {}) if k.lower() == "subscriberkey"), None)
            idx = next((i for i, r in enumerate(rows) if keycol and str(r[keycol]) == subscriber_key), None)
            if idx is None:
                raise SFMCError(f"SubscriberKey {subscriber_key} not in {de_key} (first 2,500 rows checked)")
            cols = self._identity_cols(rows)
            tried = []
            for n in [idx + 2, idx + 1, idx + 3, idx, idx + 4] + list(range(1, len(rows) + 6)):
                if n < 1 or n in tried:
                    continue
                tried.append(n)
                html = self._preview(legacy, de_id, n)
                if html is None:
                    continue
                got = self._identify(html, rows, cols)
                if got == idx or (not cols and n == idx + 2):
                    return {"html": html, "row": n, "record": rows[idx], "verified": bool(cols),
                            "subject_template": subject}
                if len(tried) > len(rows) + 8:
                    break
            raise SFMCError(f"couldn't find a preview of {subscriber_key}: no column with unique values appears "
                            "in the rendered email (use --row N instead)")
        n = row or 2
        html = self._preview(legacy, de_id, n) or ""
        got = self._identify(html, rows, self._identity_cols(rows))
        return {"html": html, "row": n, "record": rows[got] if got is not None else {}, "verified": got is not None,
                "subject_template": subject}

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

    def automation_runs(self, automation_key: str, limit: int = 20) -> list[dict]:
        """Run history from SOAP AutomationInstance (REST history endpoints aren't available on all stacks)."""
        res = self.soap.retrieve("AutomationInstance",
                                 ["Name", "CustomerKey", "Status", "StatusMessage", "StartTime", "CompletedTime",
                                  "StatusLastUpdate", "IsActive"],
                                 simple_filter("CustomerKey", "equals", automation_key))
        out = [{"status": r.get("StatusMessage") or r.get("Status"), "message": r.get("StatusMessage"),
                "started": r.get("StartTime"), "completed": r.get("CompletedTime"),
                "active": r.get("IsActive") == "true", "activity": None}
               for r in res if r.get("StartTime") or r.get("StatusMessage")]   # skip "No rows were found" placeholders
        out.sort(key=lambda x: str(x["started"] or ""), reverse=True)
        return out[:limit]

    # ------------------------------------------------------------------ automation schedules (SOAP Schedule)
    def _schedule_call(self, automation_id: str, action: str, inner: str = "<Schedule/>") -> str:
        from .soap import NS
        body = (f'<ScheduleRequestMsg xmlns="{NS}"><Action>{action}</Action>{inner}<Interactions>'
                f'<Interaction xsi:type="Automation"><ObjectID>{x(automation_id)}</ObjectID></Interaction>'
                f'</Interactions></ScheduleRequestMsg>')
        root = self.soap._call("Schedule", body)
        codes = [e.text for e in root.iter() if e.tag.endswith("StatusCode")]
        msgs = [e.text for e in root.iter() if e.tag.endswith("StatusMessage")]
        if codes and codes[0] != "OK":
            raise SFMCError(f"Schedule {action} failed: {msgs[0] if msgs else codes[0]}")
        return msgs[0] if msgs else "OK"

    def schedule_automation(self, automation_id: str, recurrence_xml: str) -> dict:
        """Replace any existing schedule (SFMC keeps the old one otherwise), then wait for the new one to show."""
        cur = self.automation_schedule(automation_id)
        if cur.get("scheduleStatus") in ("active", "paused"):
            self.unschedule(automation_id)
        self._schedule_call(automation_id, "start", recurrence_xml)
        return self._wait_schedule(automation_id, lambda s: s.get("scheduleStatus") == "active" and s.get("id") != cur.get("id"))

    def pause_schedule(self, automation_id: str) -> dict:
        self._schedule_call(automation_id, "pause")
        return self._wait_schedule(automation_id, lambda s: s.get("scheduleStatus") == "paused")

    def unschedule(self, automation_id: str) -> dict:
        self.rest.patch(f"/automation/v1/automations/{automation_id}", {"startSource": {"typeId": 1}})
        return self._wait_schedule(automation_id, lambda s: s.get("scheduleStatus") in ("none", None))

    def _wait_schedule(self, automation_id: str, done: Any, tries: int = 10) -> dict:
        s: dict = {}
        for _ in range(tries):   # reads lag writes by a few seconds
            s = self.automation_schedule(automation_id)
            if done(s):
                return s
            time.sleep(2)
        return s

    def automation_schedule(self, automation_id: str) -> dict:
        a = self.rest.get(f"/automation/v1/automations/{automation_id}")
        return {"status": a.get("status"), **(a.get("schedule") or {})}

    def delete_automation(self, automation_id: str) -> None:
        """REST DELETE isn't available on every stack (404), so delete over SOAP and confirm it's gone."""
        exists = lambda: self.rest.get(f"/automation/v1/automations/{automation_id}", allow_404=True) is not None  # noqa: E731
        if not exists():
            return
        try:
            self.soap.delete(f'<Objects xsi:type="Automation"><ObjectID>{x(automation_id)}</ObjectID></Objects>')
        except SFMCError:
            self.rest.delete(f"/automation/v1/automations/{automation_id}", allow_404=True)
        if exists():
            raise SFMCError(f"automation {automation_id} could not be deleted")

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
