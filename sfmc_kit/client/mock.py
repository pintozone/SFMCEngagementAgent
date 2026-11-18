"""MockBackend: an offline SFMC tenant on SQLite.

* DEs are real SQLite tables (named exactly like the DE) so query activities can run.
* Data Views (_Sent, _Open, _Click, _Bounce, _Unsubscribe, _Job, _Journey, _JourneyActivity)
  are tables with SFMC column names, filled when a journey is "published" (simulated sends).
"""
from __future__ import annotations

import json
import math
import random
import re
import sqlite3
import uuid
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..modules.assets import EmailSpec
from ..modules.automation import AutomationSpec, QuerySpec
from ..modules.data_extensions import DESpec
from .base import Backend
from .rest import SFMCError
from .sqlxlate import translate

DATA_VIEWS = {
    "_Journey": "VersionID TEXT, JourneyID TEXT, JourneyName TEXT, VersionNumber INTEGER, CreatedDate TEXT, "
                "LastPublishedDate TEXT, ModifiedDate TEXT, JourneyStatus TEXT",
    "_JourneyActivity": "VersionID TEXT, ActivityID TEXT, ActivityName TEXT, ActivityExternalKey TEXT, "
                        "JourneyActivityObjectID TEXT, ActivityType TEXT",
    "_Job": "JobID INTEGER, EmailID INTEGER, EmailName TEXT, EmailSubject TEXT, TriggererSendDefinitionObjectID TEXT, "
            "DeliveredTime TEXT, CreatedDate TEXT, JobStatus TEXT",
    "_Sent": "AccountID INTEGER, JobID INTEGER, ListID INTEGER, BatchID INTEGER, SubscriberID INTEGER, "
             "SubscriberKey TEXT, EventDate TEXT, Domain TEXT, TriggererSendDefinitionObjectID TEXT, "
             "TriggeredSendCustomerKey TEXT",
    "_Open": "AccountID INTEGER, JobID INTEGER, ListID INTEGER, BatchID INTEGER, SubscriberID INTEGER, "
             "SubscriberKey TEXT, EventDate TEXT, Domain TEXT, IsUnique INTEGER, TriggererSendDefinitionObjectID TEXT",
    "_Click": "AccountID INTEGER, JobID INTEGER, ListID INTEGER, BatchID INTEGER, SubscriberID INTEGER, "
              "SubscriberKey TEXT, EventDate TEXT, Domain TEXT, URL TEXT, LinkName TEXT, LinkContent TEXT, "
              "IsUnique INTEGER, TriggererSendDefinitionObjectID TEXT",
    "_Bounce": "AccountID INTEGER, JobID INTEGER, ListID INTEGER, BatchID INTEGER, SubscriberID INTEGER, "
               "SubscriberKey TEXT, EventDate TEXT, Domain TEXT, BounceCategory TEXT, BounceType TEXT, "
               "SMTPCode INTEGER, TriggererSendDefinitionObjectID TEXT",
    "_Unsubscribe": "AccountID INTEGER, JobID INTEGER, ListID INTEGER, BatchID INTEGER, SubscriberID INTEGER, "
                    "SubscriberKey TEXT, EventDate TEXT, Domain TEXT, IsUnique INTEGER",
}

MOCK_MID = 100000001


def _ts(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


class MockBackend(Backend):
    mode = "mock"

    def __init__(self, db_path: str | Path, verbose: bool = False, seed: int = 42):
        self.db = sqlite3.connect(str(db_path))
        self.db.row_factory = sqlite3.Row
        self.verbose = verbose
        self.seed = seed
        self.db.execute("CREATE TABLE IF NOT EXISTS mock_objects (type TEXT, key TEXT, id TEXT, name TEXT, "
                        "payload TEXT, created_at TEXT, PRIMARY KEY(type, key))")
        for name, cols in DATA_VIEWS.items():
            self.db.execute(f'CREATE TABLE IF NOT EXISTS "{name}" ({cols})')
        self.db.commit()

    # ------------------------------------------------------------------ object store helpers
    def _get(self, type_: str, key: str) -> dict | None:
        r = self.db.execute("SELECT * FROM mock_objects WHERE type=? AND key=?", (type_, key)).fetchone()
        return {**json.loads(r["payload"]), "id": r["id"], "key": r["key"], "name": r["name"]} if r else None

    def _get_by_id(self, type_: str, id_: Any) -> dict | None:
        r = self.db.execute("SELECT * FROM mock_objects WHERE type=? AND id=?", (type_, str(id_))).fetchone()
        return {**json.loads(r["payload"]), "id": r["id"], "key": r["key"], "name": r["name"]} if r else None

    def _put(self, type_: str, key: str, name: str, payload: dict, id_: str | None = None) -> tuple[str, bool]:
        existing = self._get(type_, key)
        oid = existing["id"] if existing else (id_ or str(uuid.uuid4()))
        self.db.execute("INSERT OR REPLACE INTO mock_objects VALUES (?,?,?,?,?,?)",
                        (type_, key, oid, name, json.dumps(payload, default=str), _ts(datetime.now())))
        self.db.commit()
        return oid, existing is None

    def _del(self, type_: str, id_: Any) -> None:
        self.db.execute("DELETE FROM mock_objects WHERE type=? AND id=?", (type_, str(id_)))
        self.db.commit()

    def whoami(self) -> dict:
        n = self.db.execute("SELECT type, COUNT(*) c FROM mock_objects GROUP BY type").fetchall()
        return {"mode": "mock", "mid": MOCK_MID, "objects": {r["type"]: r["c"] for r in n}}

    # ------------------------------------------------------------------ folders
    def ensure_folder(self, content_type: str, path: str) -> int | None:
        key = f"{content_type}:{path}"
        f = self._get("folder", key)
        if f:
            return int(f["id"])
        nid = 1000 + self.db.execute("SELECT COUNT(*) FROM mock_objects WHERE type='folder'").fetchone()[0]
        self._put("folder", key, path, {"content_type": content_type}, str(nid))
        return nid

    # ------------------------------------------------------------------ data extensions
    def get_de(self, key: str) -> dict | None:
        d = self._get("de", key)
        if not d:
            return None
        return {"key": d["key"], "name": d["name"], "object_id": d["id"], "folder_id": d.get("folder_id"),
                "sendable": d.get("sendable", False)}

    def create_de(self, spec: DESpec) -> dict:
        if self.db.execute("SELECT 1 FROM mock_objects WHERE type='de' AND name=?", (spec.name,)).fetchone():
            raise SFMCError(f"A DE named {spec.name} already exists")
        cols = []
        for f in spec.fields:
            c = f'"{f.name}" {f.sqlite_type}'
            if f.required:
                c += " NOT NULL"
            if f.default is not None:
                c += f" DEFAULT {json.dumps(f.default) if isinstance(f.default, str) else f.default}"
            cols.append(c)
        if spec.pk_fields:
            cols.append("PRIMARY KEY (" + ", ".join(f'"{p}"' for p in spec.pk_fields) + ")")
        self.db.execute(f'CREATE TABLE "{spec.name}" ({", ".join(cols)})')
        folder = self.ensure_folder("dataextension", spec.folder)
        self._put("de", spec.key, spec.name, {
            "folder_id": folder, "sendable": spec.sendable, "sendable_field": spec.sendable_field,
            "fields": [f.__dict__ for f in spec.fields], "description": spec.description})
        return self.get_de(spec.key)  # type: ignore[return-value]

    def delete_de(self, key: str) -> None:
        d = self._get("de", key)
        if not d:
            return
        self.db.execute(f'DROP TABLE IF EXISTS "{d["name"]}"')
        self._del("de", d["id"])

    def list_des(self, name_like: str = "") -> list[dict]:
        rows = self.db.execute("SELECT key, name, payload, created_at FROM mock_objects WHERE type='de' "
                               "AND name LIKE ? ORDER BY name", (f"%{name_like}%",)).fetchall()
        out = []
        for r in rows:
            p = json.loads(r["payload"])
            n = self.db.execute(f'SELECT COUNT(*) FROM "{r["name"]}"').fetchone()[0]
            out.append({"key": r["key"], "name": r["name"], "sendable": p.get("sendable"),
                        "folder_id": p.get("folder_id"), "created": r["created_at"], "rows": n})
        return out

    def describe_de(self, key: str) -> list[dict]:
        d = self._get("de", key)
        if not d:
            raise SFMCError(f"DE {key} not found")
        return [{"name": f["name"], "type": f["type"], "length": f.get("length"), "pk": f["pk"],
                 "required": f["required"], "ordinal": i} for i, f in enumerate(d["fields"])]

    def _fields(self, key: str) -> tuple[str, list[dict]]:
        d = self._get("de", key)
        if not d:
            raise SFMCError(f"DE {key} not found (HTTP 404)")
        return d["name"], d["fields"]

    def upsert_rows(self, key: str, rows: list[dict], pk: list[str]) -> int:
        name, fields = self._fields(key)
        types = {f["name"].lower(): (f["name"], f["type"]) for f in fields}
        n = 0
        for r in rows:
            cols, vals = [], []
            for k, v in r.items():
                if k.lower() not in types:
                    raise SFMCError(f"DE {key}: unknown column {k}")
                real, t = types[k.lower()]
                if t == "Boolean" and v is not None:
                    v = 1 if str(v).lower() in ("1", "true", "yes", "y") else 0
                cols.append(f'"{real}"')
                vals.append(v)
            self.db.execute(f'INSERT OR REPLACE INTO "{name}" ({",".join(cols)}) VALUES ({",".join("?" * len(vals))})',
                            vals)
            n += 1
        self.db.commit()
        return n

    def read_rows(self, key: str, max_rows: int = 2500) -> list[dict]:
        name, _ = self._fields(key)
        return [dict(r) for r in self.db.execute(f'SELECT * FROM "{name}" LIMIT ?', (max_rows,)).fetchall()]

    # ------------------------------------------------------------------ content
    def upsert_email(self, spec: EmailSpec) -> dict:
        existing = self._get("asset", spec.key)
        legacy = existing["legacy_id"] if existing else \
            5000 + self.db.execute("SELECT COUNT(*) FROM mock_objects WHERE type='asset'").fetchone()[0]
        aid = existing["id"] if existing else str(90000 + legacy)
        folder = self.ensure_folder("asset", spec.folder)
        self._put("asset", spec.key, spec.name, {"legacy_id": legacy, "subject": spec.subject,
                                                 "preheader": spec.preheader, "html": spec.html,
                                                 "folder_id": folder}, aid)
        return {"id": aid, "key": spec.key, "legacy_id": legacy, "created": existing is None}

    def delete_asset(self, asset_id: Any) -> None:
        self._del("asset", asset_id)

    # ------------------------------------------------------------------ automation
    def upsert_query(self, spec: QuerySpec) -> dict:
        if not self.get_de(spec.target_key):
            raise SFMCError(f"Query {spec.key}: target DE {spec.target_key} does not exist")
        qid, created = self._put("query", spec.key, spec.name, {"sql": spec.sql, "target_key": spec.target_key,
                                                                "update_type": spec.update_type})
        return {"id": qid, "key": spec.key, "created": created}

    def delete_query(self, query_id: str) -> None:
        self._del("query", query_id)

    def upsert_automation(self, spec: AutomationSpec, query_ids: dict[str, str]) -> dict:
        steps = [[query_ids[k] for k in step] for step in spec.steps]
        aid, created = self._put("automation", spec.key, spec.name, {"steps": steps, "status": "Ready"})
        return {"id": aid, "key": spec.key, "created": created}

    def run_automation(self, automation_id: str, wait: bool = True, timeout: int = 900) -> str:
        a = self._get_by_id("automation", automation_id)
        if not a:
            raise SFMCError(f"Automation {automation_id} not found")
        for step in a["steps"]:
            for qid in step:
                q = self._get_by_id("query", qid)
                if not q:
                    raise SFMCError(f"Query {qid} not found")
                self._run_query(q)
        return "Complete"

    def _run_query(self, q: dict) -> int:
        name, fields = self._fields(q["target_key"])
        sql = translate(q["sql"])
        try:
            cur = self.db.execute(sql)
        except sqlite3.Error as e:
            raise SFMCError(f"Query {q['key']} failed: {e}\n--- translated SQL ---\n{sql}") from e
        out_cols = [d[0] for d in cur.description]
        target = {f["name"].lower(): f["name"] for f in fields}
        unknown = [c for c in out_cols if c.lower() not in target]
        if unknown:
            raise SFMCError(f"Query {q['key']}: columns {unknown} do not exist in target DE {name}")
        rows = cur.fetchall()
        if q["update_type"] == "Overwrite":
            self.db.execute(f'DELETE FROM "{name}"')
        verb = "INSERT OR REPLACE" if q["update_type"] == "Update" else "INSERT"
        cols = ",".join(f'"{target[c.lower()]}"' for c in out_cols)
        try:
            self.db.executemany(f'{verb} INTO "{name}" ({cols}) VALUES ({",".join("?" * len(out_cols))})',
                                [tuple(r) for r in rows])
        except sqlite3.IntegrityError as e:
            self.db.rollback()
            raise SFMCError(f"Query {q['key']}: primary key / null violation writing {name}: {e}") from e
        self.db.commit()
        if self.verbose:
            print(f"  [mock] query {q['key']} -> {len(rows)} rows into {name}")
        return len(rows)

    def delete_automation(self, automation_id: str) -> None:
        self._del("automation", automation_id)

    # ------------------------------------------------------------------ journeys
    def send_defaults(self, send_classification_key: str) -> dict[str, str]:
        return {"sendClassificationId": f"mock-sc-{send_classification_key}",
                "senderProfileId": "mock-sender", "deliveryProfileId": "mock-delivery"}

    def upsert_event_definition(self, payload: dict) -> dict:
        de_id = payload["dataExtensionId"]
        de = self._get_by_id("de", de_id)
        if not de:
            raise SFMCError(f"Event definition: DE {de_id} not found")
        eid, created = self._put("eventdef", payload["eventDefinitionKey"], payload["name"],
                                 {**payload, "de_key": de["key"]})
        return {"id": eid, "key": payload["eventDefinitionKey"], "created": created}

    def delete_event_definition(self, event_def_id: str) -> None:
        self._del("eventdef", event_def_id)

    def upsert_journey(self, payload: dict) -> dict:
        existing = self._get("journey", payload["key"])
        if existing and existing.get("status") != "Draft":
            raise SFMCError(f"Journey {payload['key']} is {existing['status']}; tear down before rebuilding")
        self._validate_journey(payload)
        jid, created = self._put("journey", payload["key"], payload["name"],
                                 {"payload": payload, "status": "Draft", "version": 1})
        return {"id": jid, "key": payload["key"], "version": 1, "status": "Draft", "created": created}

    @staticmethod
    def _validate_journey(p: dict) -> None:
        keys = {a["key"] for a in p["activities"]}
        for a in p["activities"]:
            for o in a["outcomes"]:
                if o.get("next") and o["next"] not in keys:
                    raise SFMCError(f"Journey {p['key']}: {a['key']} points to missing activity {o['next']}")
            if a["type"] == "EMAILV2" and not a["configurationArguments"]["triggeredSend"].get("emailId"):
                raise SFMCError(f"Journey {p['key']}: {a['key']} has no emailId")

    def publish_journey(self, journey_id: str, version: int) -> str:
        j = self._get_by_id("journey", journey_id)
        if not j:
            raise SFMCError(f"Journey {journey_id} not found")
        self._simulate(j)
        j_payload = {k: v for k, v in j.items() if k not in ("id", "key", "name")}
        j_payload["status"] = "Published"
        self._put("journey", j["key"], j["name"], j_payload)
        return "Published"

    def delete_journey(self, journey_id: str) -> None:
        j = self._get_by_id("journey", journey_id)
        if j:
            self._clear_tracking(j["name"])
        self._del("journey", journey_id)

    # ------------------------------------------------------------------ send simulation
    def _clear_tracking(self, journey_name: str) -> None:
        vids = [r[0] for r in self.db.execute('SELECT VersionID FROM "_Journey" WHERE JourneyName=?', (journey_name,))]
        for vid in vids:
            tsds = [r[0] for r in self.db.execute(
                'SELECT JourneyActivityObjectID FROM "_JourneyActivity" WHERE VersionID=?', (vid,))]
            for t in tsds:
                jobs = [r[0] for r in self.db.execute(
                    'SELECT JobID FROM "_Job" WHERE TriggererSendDefinitionObjectID=?', (t,))]
                for tbl in ("_Sent", "_Open", "_Click", "_Bounce"):
                    self.db.execute(f'DELETE FROM "{tbl}" WHERE TriggererSendDefinitionObjectID=?', (t,))
                for job in jobs:
                    self.db.execute('DELETE FROM "_Unsubscribe" WHERE JobID=?', (job,))
                self.db.execute('DELETE FROM "_Job" WHERE TriggererSendDefinitionObjectID=?', (t,))
            self.db.execute('DELETE FROM "_JourneyActivity" WHERE VersionID=?', (vid,))
        self.db.execute('DELETE FROM "_Journey" WHERE JourneyName=?', (journey_name,))

    def _simulate(self, j: dict) -> None:
        p = j["payload"]
        acts = {a["key"]: a for a in p["activities"]}
        meta = p.get("_activityMeta", {})
        trig = p["triggers"][0]
        ev = self._get("eventdef", trig["eventDefinitionKey"])
        if not ev:
            raise SFMCError("Journey entry event definition not found")
        contacts = self.read_rows(ev["de_key"], max_rows=10_000_000)
        rng = random.Random(zlib.crc32(f"{self.seed}:{p['key']}".encode()))

        def duration(key: str | None) -> float:
            if not key:
                return 0.0
            a = acts[key]
            own = 0.0
            if a["type"] == "WAIT":
                c = a["configurationArguments"]
                own = c["waitDuration"] * {"MINUTES": 1 / 1440, "HOURS": 1 / 24, "DAYS": 1, "WEEKS": 7}[c["waitUnit"]]
            elif a["type"] == "EMAILV2":
                own = 0.5
            return own + max([duration(o.get("next")) for o in a["outcomes"]] or [0])

        now = datetime.now().replace(microsecond=0)
        start = now - timedelta(days=math.ceil(duration(p.get("_firstActivityKey"))) + 2)

        self._clear_tracking(p["name"])
        version_id, journey_id = str(uuid.uuid4()), j["id"]
        self.db.execute('INSERT INTO "_Journey" VALUES (?,?,?,?,?,?,?,?)',
                        (version_id, journey_id, p["name"], 1, _ts(start - timedelta(days=1)), _ts(start), _ts(now),
                         "Running"))
        job_base = (self.db.execute('SELECT IFNULL(MAX(JobID), 700000) FROM "_Job"').fetchone()[0]) + 1
        email_info: dict[str, dict] = {}
        for i, (k, a) in enumerate(acts.items()):
            if a["type"] != "EMAILV2":
                continue
            tsd = str(uuid.uuid4())
            ts = a["configurationArguments"]["triggeredSend"]
            asset = self.db.execute("SELECT payload FROM mock_objects WHERE type='asset' AND payload LIKE ?",
                                    (f'%"legacy_id": {ts["emailId"]},%',)).fetchone()
            html = json.loads(asset[0])["html"] if asset else ""
            links = []
            for tag in re.findall(r"<a\s[^>]*>", html):
                h, al = re.search(r'href="(http[^"]+)"', tag), re.search(r'alias="([^"]*)"', tag)
                if h:
                    links.append((h.group(1), al.group(1) if al else h.group(1)))
            links = links or [("https://www.example.com/aurora", "Home")]
            job = job_base + i
            email_info[k] = {"tsd": tsd, "job": job, "links": links, "idx": len(email_info),
                             "ref": meta.get(k, {}).get("emailRef")}
            self.db.execute('INSERT INTO "_JourneyActivity" VALUES (?,?,?,?,?,?)',
                            (version_id, str(uuid.uuid4()), a["name"], k, tsd, "EMAILV2"))
            self.db.execute('INSERT INTO "_Job" VALUES (?,?,?,?,?,?,?,?)',
                            (job, ts["emailId"], ts["name"], ts["emailSubject"], tsd, _ts(start), _ts(start),
                             "Complete"))

        sent, opens, clicks, bounces, unsubs = [], [], [], [], []
        for c in contacts:
            ck = {k.lower(): v for k, v in c.items()}
            skey = str(ck.get("subscriberkey"))
            sid = zlib.crc32(skey.encode()) % 90_000_000 + 10_000_000
            domain = str(ck.get("emailaddress", "x@example.com")).split("@")[-1]
            base = 0.30 + {"Platinum": 0.18, "Gold": 0.10, "Silver": 0.04}.get(str(ck.get("loyaltytier")), 0.0)
            base += rng.uniform(-0.08, 0.08)
            click_aff = 0.24 + (0.08 if str(ck.get("modelinterest", "")).lower().find("volt") >= 0 else 0.0)
            t = start + timedelta(hours=rng.uniform(6, 30))
            state: dict[str, dict[str, bool]] = {}
            node = p.get("_firstActivityKey")
            unsubscribed = False
            hops = 0
            while node and hops < 100:
                hops += 1
                a = acts[node]
                nxt = a["outcomes"][0].get("next") if a["outcomes"] else None
                if a["type"] == "WAIT":
                    cfg = a["configurationArguments"]
                    t += timedelta(**{cfg["waitUnit"].lower(): cfg["waitDuration"]})
                elif a["type"] == "EMAILV2":
                    if unsubscribed:
                        break
                    e = email_info[node]
                    batch = int(t.strftime("%j"))
                    common = (MOCK_MID, e["job"], 1, batch, sid, skey)
                    sent.append((*common, _ts(t), domain, e["tsd"], f"{p['key']}-{node}"))
                    st = state.setdefault(e["ref"] or node, {"opened": False, "clicked": False})
                    if rng.random() < 0.015:
                        hard = rng.random() < 0.4
                        bounces.append((*common, _ts(t + timedelta(minutes=2)), domain,
                                        "Hard bounce" if hard else "Soft bounce",
                                        "Immediate" if hard else "Delayed", 550 if hard else 421, e["tsd"]))
                    else:
                        p_open = max(0.05, base * (0.85 ** e["idx"]))
                        if rng.random() < p_open:
                            st["opened"] = True
                            ot = t + timedelta(hours=rng.expovariate(1 / 6))
                            opens.append((*common, _ts(ot), domain, 1, e["tsd"]))
                            for _ in range(rng.choice([0, 0, 1, 2])):
                                opens.append((*common, _ts(ot + timedelta(hours=rng.uniform(1, 48))), domain, 0,
                                              e["tsd"]))
                            if rng.random() < click_aff:
                                st["clicked"] = True
                                url, alias = rng.choice(e["links"])
                                ct = ot + timedelta(minutes=rng.uniform(1, 30))
                                clicks.append((*common, _ts(ct), domain, url, alias or url, alias or url, 1, e["tsd"]))
                                if rng.random() < 0.3:
                                    url2, alias2 = rng.choice(e["links"])
                                    clicks.append((*common, _ts(ct + timedelta(minutes=3)), domain, url2,
                                                   alias2 or url2, alias2 or url2, 0, e["tsd"]))
                            if rng.random() < 0.004:
                                unsubscribed = True
                                unsubs.append((*common, _ts(ot + timedelta(minutes=5)), domain, 1))
                    t += timedelta(minutes=rng.uniform(1, 20))
                elif a["type"] == "ENGAGEMENTDECISION":
                    m = meta.get(node, {})
                    st = state.get(m.get("emailRef"), {})
                    yes = bool(st.get("opened" if m.get("on", "opened") == "opened" else "clicked"))
                    outs = {o["metaData"].get("label"): o.get("next") for o in a["outcomes"]}
                    nxt = outs.get("Yes" if yes else "No")
                node = nxt

        self.db.executemany('INSERT INTO "_Sent" VALUES (?,?,?,?,?,?,?,?,?,?)', sent)
        self.db.executemany('INSERT INTO "_Open" VALUES (?,?,?,?,?,?,?,?,?,?)', opens)
        self.db.executemany('INSERT INTO "_Click" VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', clicks)
        self.db.executemany('INSERT INTO "_Bounce" VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', bounces)
        self.db.executemany('INSERT INTO "_Unsubscribe" VALUES (?,?,?,?,?,?,?,?,?)', unsubs)
        self.db.commit()
        if self.verbose:
            print(f"  [mock] simulated {len(contacts)} entries: {len(sent)} sends, {len(opens)} opens, "
                  f"{len(clicks)} clicks, {len(bounces)} bounces, {len(unsubs)} unsubs")
