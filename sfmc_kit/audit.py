"""Tenant audit: inventory a business unit, extract dependencies, answer impact questions, flag problems.

Everything lives in the sandbox DB (inv_* tables) so questions are answered offline after one sync.
Edges read "src depends on / acts on dst": (query)-[reads]->(de), (query)-[writes]->(de),
(automation)-[runs]->(query), (journey)-[enters_from]->(eventdef)-[on]->(de), (journey)-[sends]->(email),
(email|block|page)-[reads|writes|includes|links]->(...).
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .client.base import Backend
from .client.rest import SFMCError

SCHEMA = """
CREATE TABLE IF NOT EXISTS inv_sync (mid TEXT, mode TEXT, synced_at TEXT, counts TEXT, PRIMARY KEY (mid, mode));
CREATE TABLE IF NOT EXISTS inv_objects (
  mid TEXT, mode TEXT, type TEXT, key TEXT, id TEXT, name TEXT, modified TEXT, meta TEXT, body TEXT,
  PRIMARY KEY (mid, mode, type, key));
CREATE TABLE IF NOT EXISTS inv_fields (
  mid TEXT, mode TEXT, de_key TEXT, name TEXT, type TEXT, length TEXT, pk INTEGER, required INTEGER, ordinal INTEGER);
CREATE TABLE IF NOT EXISTS inv_edges (
  mid TEXT, mode TEXT, src_type TEXT, src_key TEXT, dst_type TEXT, dst_key TEXT, rel TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS inv_edges_src ON inv_edges (mid, mode, src_type, src_key);
CREATE INDEX IF NOT EXISTS inv_edges_dst ON inv_edges (mid, mode, dst_type, dst_key);
"""

# Content Builder asset types that carry code we scan.
CODE_ASSET_TYPES = {207: "email", 208: "email", 209: "email", 195: "block", 196: "block", 197: "block",
                    220: "block", 205: "webpage", 247: "page"}
AUTOMATION_ACTIVITY_TYPES = {300: "query", 42: "email_send", 43: "import", 53: "file_transfer",
                             73: "data_extract", 423: "script", 467: "wait", 425: "filter", 952: "journey_entry",
                             1101: "refresh_group", 1000: "verification"}
DATA_VIEWS = {"_sent", "_open", "_click", "_bounce", "_unsubscribe", "_job", "_journey", "_journeyactivity",
              "_subscribers", "_listsubscribers", "_complaint", "_businessunitunsubscribes", "_ftaf",
              "_surveyresponse", "_socialnetworkimpressions", "_socialnetworktracking", "_smsmessagetracking",
              "_smssubscriptionlog", "_undeliverablesms", "_mobileaddress", "_mobilesubscription",
              "_pushaddress", "_pushtag", "_enterpriseattribute", "_automationinstance",
              "_automationactivityinstance"}

AMP_READ = r"(?:Lookup|LookupRows|LookupOrderedRows|LookupRowsCS|LookupOrderedRowsCS|DataExtensionRowCount)"
AMP_WRITE = r"(?:InsertData|InsertDE|UpsertData|UpsertDE|UpdateData|UpdateDE|DeleteData|DeleteDE|ClaimRow)"


@dataclass
class Inventory:
    objects: list[dict]   # {type, key, id, name, modified, meta, body}
    fields: list[dict]    # {de_key, name, type, length, pk, required, ordinal}


# ---------------------------------------------------------------------------------------------- collection
def collect(be: Backend, log: Callable[[str], None] = print) -> Inventory:
    if be.mode == "live":
        return _collect_live(be, log)
    return _collect_mock(be)


def _pages(be: Any, path: str, size: int = 500, extra: dict | None = None) -> list[dict]:
    out, page = [], 1
    while True:
        r = be.rest.get(path, params={"$page": page, "$pageSize": size, **(extra or {})})
        items = (r or {}).get("items", [])
        out += items
        if len(items) < size or len(out) >= int((r or {}).get("count", 0) or 0):
            return out
        page += 1


def _collect_live(be: Any, log: Callable[[str], None]) -> Inventory:
    objs: list[dict] = []
    fields: list[dict] = []
    log("  data extensions…")
    des = be.soap.retrieve("DataExtension", ["ObjectID", "CustomerKey", "Name", "CategoryID", "IsSendable",
                                             "CreatedDate", "ModifiedDate", "Description"])
    for d in des:
        objs.append({"type": "de", "key": d["CustomerKey"], "id": d.get("ObjectID"), "name": d["Name"],
                     "modified": d.get("ModifiedDate") or d.get("CreatedDate"),
                     "meta": {"sendable": d.get("IsSendable") == "true", "folder_id": d.get("CategoryID"),
                              "description": d.get("Description")}, "body": None})
    log("  fields…")
    for f in be.soap.retrieve("DataExtensionField", ["Name", "FieldType", "MaxLength", "IsPrimaryKey", "IsRequired",
                                                     "Ordinal", "DataExtension.CustomerKey"]):
        fields.append({"de_key": (f.get("DataExtension") or {}).get("CustomerKey"), "name": f["Name"],
                       "type": f.get("FieldType"), "length": f.get("MaxLength"),
                       "pk": f.get("IsPrimaryKey") == "true", "required": f.get("IsRequired") == "true",
                       "ordinal": int(f.get("Ordinal") or 0)})
    log("  queries…")
    for q in _pages(be, "/automation/v1/queries"):
        objs.append({"type": "query", "key": q.get("key") or q["queryDefinitionId"], "id": q["queryDefinitionId"],
                     "name": q.get("name"), "modified": q.get("modifiedDate") or q.get("createdDate"),
                     "meta": {"target_key": q.get("targetKey"), "target_name": q.get("targetName"),
                              "update_type": q.get("targetUpdateTypeName"), "status": q.get("status")},
                     "body": q.get("queryText")})
    log("  automations…")
    for a in _pages(be, "/automation/v1/automations"):
        try:
            d = be.rest.get(f"/automation/v1/automations/{a['id']}")
        except SFMCError:
            d = a
        steps = [{"step": s.get("step"), "activities": [
            {"name": x.get("name"), "type": AUTOMATION_ACTIVITY_TYPES.get(x.get("objectTypeId"), str(x.get("objectTypeId"))),
             "object_id": x.get("activityObjectId")} for x in s.get("activities", [])]} for s in d.get("steps", [])]
        objs.append({"type": "automation", "key": a.get("key") or a["id"], "id": a["id"], "name": a.get("name"),
                     "modified": d.get("modifiedDate") or a.get("lastRunTime"),
                     "meta": {"status": a.get("status"), "status_id": a.get("statusId"),
                              "last_run": a.get("lastRunTime"), "last_run_status": d.get("lastRunStatus"),
                              "schedule": a.get("schedule"), "steps": steps}, "body": None})
    log("  journeys…")
    for j in _pages(be, "/interaction/v1/interactions", size=50):
        try:
            d = be.rest.get(f"/interaction/v1/interactions/{j['id']}")
        except SFMCError:
            d = j
        acts = [{"key": a.get("key"), "type": a.get("type"), "name": a.get("name"),
                 "email_id": ((a.get("configurationArguments") or {}).get("triggeredSend") or {}).get("emailId")}
                for a in d.get("activities", [])]
        trig = [t.get("metaData", {}).get("eventDefinitionKey") or t.get("eventDefinitionKey")
                for t in d.get("triggers", [])]
        objs.append({"type": "journey", "key": d.get("key") or j["id"], "id": j["id"], "name": d.get("name"),
                     "modified": d.get("modifiedDate"),
                     "meta": {"status": d.get("status"), "version": d.get("version"), "activities": acts,
                              "event_keys": [t for t in trig if t]}, "body": None})
    log("  entry events…")
    for e in _pages(be, "/interaction/v1/eventDefinitions", size=50):
        objs.append({"type": "eventdef", "key": e.get("eventDefinitionKey") or e["id"], "id": e["id"],
                     "name": e.get("name"), "modified": e.get("modifiedDate"),
                     "meta": {"de_id": e.get("dataExtensionId"), "type": e.get("type")}, "body": None})
    log("  content (emails, blocks, pages)…")
    page = 1
    while True:
        r = be.rest.post("/asset/v1/content/assets/query", {
            "page": {"page": page, "pageSize": 200},
            "query": {"property": "assetType.id", "simpleOperator": "in", "value": sorted(CODE_ASSET_TYPES)},
            "fields": ["id", "name", "customerKey", "assetType", "modifiedDate", "category", "legacyData", "content",
                       "views", "meta"]})
        items = r.get("items", [])
        for a in items:
            t = CODE_ASSET_TYPES.get(a["assetType"]["id"], "block")
            objs.append({"type": t, "key": a.get("customerKey") or str(a["id"]), "id": str(a["id"]),
                         "name": a.get("name"), "modified": a.get("modifiedDate"),
                         "meta": {"asset_type": a["assetType"]["name"], "legacy_id": (a.get("legacyData") or {}).get("legacyId"),
                                  "folder": (a.get("category") or {}).get("name"),
                                  "ref_asset": ((a.get("meta") or {}).get("thumbnailRefAssetId")),
                                  "published": ((a.get("meta") or {}).get("cloudPages") or {}).get("publishDate"),
                                  "subject": (((a.get("views") or {}).get("subjectline") or {}).get("content"))},
                         "body": _asset_body(a)})
        if len(items) < 200:
            break
        page += 1
    return Inventory(objs, fields)


def _asset_body(a: dict) -> str:
    """All code-bearing text of an asset: content, views, slots and nested blocks."""
    parts: list[str] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("content", "design") and isinstance(v, str) and k == "content":
                    parts.append(v)
                elif k != "thumbnail":
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    if isinstance(a.get("content"), str):
        parts.append(a["content"])
    walk(a.get("views") or {})
    return "\n".join(parts)


def _collect_mock(be: Any) -> Inventory:
    objs: list[dict] = []
    fields: list[dict] = []
    rows = be.db.execute("SELECT type, key, id, name, payload, created_at FROM mock_objects").fetchall()
    for r in rows:
        p = json.loads(r["payload"])
        t = r["type"]
        if t == "de":
            objs.append({"type": "de", "key": r["key"], "id": r["id"], "name": r["name"], "modified": r["created_at"],
                         "meta": {"sendable": p.get("sendable")}, "body": None})
            for i, f in enumerate(p["fields"]):
                fields.append({"de_key": r["key"], "name": f["name"], "type": f["type"], "length": f.get("length"),
                               "pk": f["pk"], "required": f["required"], "ordinal": i})
        elif t == "query":
            tgt = be._get("de", p["target_key"])
            objs.append({"type": "query", "key": r["key"], "id": r["id"], "name": r["name"], "modified": r["created_at"],
                         "meta": {"target_key": p["target_key"], "target_name": tgt["name"] if tgt else None,
                                  "update_type": p["update_type"]}, "body": p["sql"]})
        elif t == "automation":
            steps = [{"step": i + 1, "activities": [{"name": q, "type": "query", "object_id": q} for q in s]}
                     for i, s in enumerate(p["steps"])]
            objs.append({"type": "automation", "key": r["key"], "id": r["id"], "name": r["name"],
                         "modified": r["created_at"], "meta": {"status": p.get("status", "Ready"),
                                                               "last_run_status": "Complete", "steps": steps},
                         "body": None})
        elif t == "journey":
            pl = p["payload"]
            acts = [{"key": a["key"], "type": a["type"], "name": a["name"],
                     "email_id": ((a.get("configurationArguments") or {}).get("triggeredSend") or {}).get("emailId")}
                    for a in pl["activities"]]
            objs.append({"type": "journey", "key": r["key"], "id": r["id"], "name": r["name"],
                         "modified": r["created_at"], "meta": {"status": p.get("status"), "version": 1,
                                                               "activities": acts,
                                                               "event_keys": [t["eventDefinitionKey"] for t in pl["triggers"]]},
                         "body": None})
        elif t == "eventdef":
            objs.append({"type": "eventdef", "key": r["key"], "id": r["id"], "name": r["name"],
                         "modified": r["created_at"], "meta": {"de_id": p["dataExtensionId"], "type": p["type"]},
                         "body": None})
        elif t in ("asset", "block"):
            objs.append({"type": "email" if t == "asset" else "block", "key": r["key"], "id": r["id"],
                         "name": r["name"], "modified": r["created_at"],
                         "meta": {"legacy_id": p.get("legacy_id"), "subject": p.get("subject")},
                         "body": p.get("html")})
    return Inventory(objs, fields)


# ---------------------------------------------------------------------------------------------- edges
def _norm(name: str) -> str:
    n = name.strip().strip("[]").strip()
    if n.upper().startswith("ENT."):
        n = n[4:].strip("[]")
    return n.lower()


SQL_SOURCE = re.compile(r"\b(?:FROM|JOIN)\s+((?:ENT\.)?(?:\[[^\]]+\]|[A-Za-z_][\w]*))", re.I)


def sql_sources(sql: str) -> list[str]:
    sql = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql or "", flags=re.S)
    out = []
    for m in SQL_SOURCE.finditer(sql):
        raw = m.group(1)
        if raw.startswith("(") or raw.upper() in ("SELECT",):
            continue
        out.append(raw.strip("[]").replace("ENT.", "").strip("[]"))
    return list(dict.fromkeys(out))


def code_refs(body: str) -> list[tuple[str, str, str]]:
    """(rel, target_kind, target) references found in AMPscript / SSJS / HTML."""
    if not body:
        return []
    refs: list[tuple[str, str, str]] = []
    for m in re.finditer(rf"\b{AMP_READ}\s*\(\s*['\"]([^'\"]+)['\"]", body, re.I):
        refs.append(("reads", "de_name", m.group(1)))
    for m in re.finditer(rf"\b{AMP_WRITE}\s*\(\s*['\"]([^'\"]+)['\"]", body, re.I):
        refs.append(("writes", "de_name", m.group(1)))
    for m in re.finditer(r"Platform\.Function\.(Lookup\w*|InsertData|UpsertData|UpdateData|DeleteData)\s*\(\s*['\"]([^'\"]+)['\"]", body):
        refs.append(("writes" if re.match(r"(Insert|Upsert|Update|Delete)", m.group(1)) else "reads", "de_name", m.group(2)))
    for m in re.finditer(r"DataExtension\.Init\(\s*['\"]([^'\"]+)['\"]", body):
        refs.append(("uses", "de_key", m.group(1)))
    for m in re.finditer(r"ContentBlockByKey\(\s*['\"]([^'\"]+)['\"]", body, re.I):
        refs.append(("includes", "block_key", m.group(1)))
    for m in re.finditer(r"ContentBlockById\(\s*['\"]?(\d+)['\"]?", body, re.I):
        refs.append(("includes", "asset_id", m.group(1)))
    for m in re.finditer(r"CloudPagesURL\(\s*(\d+)", body, re.I):
        refs.append(("links", "cloudpage_id", m.group(1)))
    return list(dict.fromkeys(refs))


def build_edges(inv: Inventory) -> list[dict]:
    by = lambda t: [o for o in inv.objects if o["type"] == t]  # noqa: E731
    de_by_name = {o["name"].lower(): o for o in by("de")}
    de_by_key = {o["key"].lower(): o for o in by("de")}
    de_by_id = {str(o["id"]).lower(): o for o in by("de") if o["id"]}
    q_by_id = {str(o["id"]): o for o in by("query")}
    content = [o for o in inv.objects if o["type"] in ("email", "block", "webpage", "page")]
    email_by_legacy = {str(o["meta"].get("legacy_id")): o for o in content if o["meta"].get("legacy_id")}
    by_asset_id = {str(o["id"]): o for o in content}
    by_block_key = {o["key"].lower(): o for o in content}
    ev_by_key = {o["key"]: o for o in by("eventdef")}
    edges: list[dict] = []

    def e(src: dict, dst_type: str, dst_key: str, rel: str, detail: str = "") -> None:
        edges.append({"src_type": src["type"], "src_key": src["key"], "dst_type": dst_type, "dst_key": dst_key,
                      "rel": rel, "detail": detail})

    def de_ref(name: str) -> tuple[str, str]:
        n = _norm(name)
        if n in DATA_VIEWS:
            return "dataview", n
        d = de_by_name.get(n) or de_by_key.get(n)
        return ("de", d["key"]) if d else ("missing_de", name)

    for q in by("query"):
        tk = q["meta"].get("target_key")
        if tk:
            d = de_by_key.get(tk.lower())
            e(q, "de" if d else "missing_de", d["key"] if d else tk, "writes", q["meta"].get("update_type") or "")
        for src in sql_sources(q["body"] or ""):
            t, k = de_ref(src)
            e(q, t, k, "reads")
    for a in by("automation"):
        for s in a["meta"].get("steps", []):
            for act in s["activities"]:
                q = q_by_id.get(str(act["object_id"]))
                if q:
                    e(a, "query", q["key"], "runs", f"step {s['step']}")
                else:
                    e(a, "activity", f"{act['type']}:{act['name']}", "runs", f"step {s['step']}")
    for ev in by("eventdef"):
        d = de_by_id.get(str(ev["meta"].get("de_id") or "").lower())
        if d:
            e(ev, "de", d["key"], "on")
    for j in by("journey"):
        for k in j["meta"].get("event_keys", []):
            e(j, "eventdef" if k in ev_by_key else "missing_eventdef", k, "enters_from")
        for act in j["meta"].get("activities", []):
            if act.get("email_id"):
                em = email_by_legacy.get(str(act["email_id"]))
                e(j, "email" if em else "missing_email", em["key"] if em else str(act["email_id"]), "sends", act["key"])
    for c in content:
        if c["type"] == "page" and c["meta"].get("ref_asset"):
            w = by_asset_id.get(str(c["meta"]["ref_asset"]))
            if w:
                e(c, w["type"], w["key"], "renders")
        for rel, kind, target in code_refs(c["body"] or ""):
            if kind == "de_name":
                t, k = de_ref(target)
                e(c, t, k, rel)
            elif kind == "de_key":
                d = de_by_key.get(target.lower())
                e(c, "de" if d else "missing_de", d["key"] if d else target, rel)
            elif kind == "block_key":
                b = by_block_key.get(target.lower())
                e(c, b["type"] if b else "missing_block", b["key"] if b else target, rel)
            elif kind == "asset_id":
                b = by_asset_id.get(target)
                e(c, b["type"] if b else "missing_block", b["key"] if b else target, rel)
            elif kind == "cloudpage_id":
                e(c, "cloudpage", target, rel)
    # de-duplicate
    seen, out = set(), []
    for x in edges:
        k = tuple(x.values())
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


# ---------------------------------------------------------------------------------------------- storage
class AuditStore:
    def __init__(self, db: sqlite3.Connection, mid: str, mode: str):
        self.db, self.mid, self.mode = db, str(mid), mode
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def save(self, inv: Inventory, edges: list[dict]) -> dict:
        w = (self.mid, self.mode)
        for t in ("inv_objects", "inv_fields", "inv_edges"):
            self.db.execute(f"DELETE FROM {t} WHERE mid=? AND mode=?", w)
        self.db.executemany("INSERT OR REPLACE INTO inv_objects VALUES (?,?,?,?,?,?,?,?,?)",
                            [(*w, o["type"], o["key"], str(o["id"]), o["name"], o["modified"],
                              json.dumps(o["meta"], default=str), o["body"]) for o in inv.objects])
        self.db.executemany("INSERT INTO inv_fields VALUES (?,?,?,?,?,?,?,?,?)",
                            [(*w, f["de_key"], f["name"], f["type"], f["length"], int(bool(f["pk"])),
                              int(bool(f["required"])), f["ordinal"]) for f in inv.fields])
        self.db.executemany("INSERT INTO inv_edges VALUES (?,?,?,?,?,?,?,?)",
                            [(*w, x["src_type"], x["src_key"], x["dst_type"], x["dst_key"], x["rel"], x["detail"])
                             for x in edges])
        counts = {t: sum(1 for o in inv.objects if o["type"] == t)
                  for t in sorted({o["type"] for o in inv.objects})}
        counts["fields"], counts["edges"] = len(inv.fields), len(edges)
        self.db.execute("INSERT OR REPLACE INTO inv_sync VALUES (?,?,?,?)",
                        (*w, datetime.now().isoformat(timespec="seconds"), json.dumps(counts)))
        self.db.commit()
        return counts

    def synced(self) -> dict | None:
        r = self.db.execute("SELECT * FROM inv_sync WHERE mid=? AND mode=?", (self.mid, self.mode)).fetchone()
        return {"synced_at": r["synced_at"], "counts": json.loads(r["counts"])} if r else None

    def objects(self, type_: str | None = None) -> list[dict]:
        q = "SELECT * FROM inv_objects WHERE mid=? AND mode=?" + (" AND type=?" if type_ else "")
        args = (self.mid, self.mode, type_) if type_ else (self.mid, self.mode)
        return [{**dict(r), "meta": json.loads(r["meta"] or "{}")} for r in self.db.execute(q, args)]

    def fields(self, de_key: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM inv_fields WHERE mid=? AND mode=? AND lower(de_key)=lower(?) ORDER BY ordinal",
            (self.mid, self.mode, de_key))]

    def resolve(self, ref: str) -> dict | None:
        """Find an object by key, name or id (case-insensitive), preferring DEs."""
        rows = self.db.execute(
            "SELECT * FROM inv_objects WHERE mid=? AND mode=? AND (lower(key)=lower(?) OR lower(name)=lower(?) OR id=?) "
            "ORDER BY CASE type WHEN 'de' THEN 0 ELSE 1 END", (self.mid, self.mode, ref, ref, ref)).fetchall()
        return {**dict(rows[0]), "meta": json.loads(rows[0]["meta"] or "{}")} if rows else None

    def name_of(self, type_: str, key: str) -> str:
        r = self.db.execute("SELECT name FROM inv_objects WHERE mid=? AND mode=? AND type=? AND key=?",
                            (self.mid, self.mode, type_, key)).fetchone()
        return r["name"] if r and r["name"] else key

    # -------------------------------------------------------------------------------- impact
    def impact(self, type_: str, key: str, depth: int = 6) -> dict:
        """Upstream = things that depend on the object (would break if it changes);
        downstream = things the object depends on."""
        up = self.db.execute("""
            WITH RECURSIVE walk(t, k, rel, via_t, via_k, d) AS (
              SELECT src_type, src_key, rel, dst_type, dst_key, 1 FROM inv_edges
               WHERE mid=:m AND mode=:mo AND dst_type=:t AND dst_key=:k
              UNION
              SELECT e.src_type, e.src_key, e.rel, e.dst_type, e.dst_key, w.d + 1 FROM inv_edges e
               JOIN walk w ON e.dst_type = w.t AND e.dst_key = w.k
               WHERE e.mid=:m AND e.mode=:mo AND w.d < :depth
              UNION
              -- data flow: a query/page that reads the object and WRITES another DE affects that DE's users too
              SELECT e.dst_type, e.dst_key, 'feeds', w.t, w.k, w.d + 1 FROM inv_edges e
               JOIN walk w ON e.src_type = w.t AND e.src_key = w.k
               WHERE e.mid=:m AND e.mode=:mo AND e.rel = 'writes' AND w.rel != 'feeds' AND w.d < :depth)
            SELECT t, k, rel, via_t, via_k, MIN(d) AS d FROM walk GROUP BY t, k, rel, via_t, via_k ORDER BY d, t, k""",
                             {"m": self.mid, "mo": self.mode, "t": type_, "k": key, "depth": depth}).fetchall()
        down = self.db.execute("""
            WITH RECURSIVE walk(t, k, rel, via_t, via_k, d) AS (
              SELECT dst_type, dst_key, rel, src_type, src_key, 1 FROM inv_edges
               WHERE mid=:m AND mode=:mo AND src_type=:t AND src_key=:k
              UNION
              SELECT e.dst_type, e.dst_key, e.rel, e.src_type, e.src_key, w.d + 1 FROM inv_edges e
               JOIN walk w ON e.src_type = w.t AND e.src_key = w.k
               WHERE e.mid=:m AND e.mode=:mo AND w.d < :depth)
            SELECT t, k, rel, via_t, via_k, MIN(d) AS d FROM walk GROUP BY t, k, rel, via_t, via_k ORDER BY d, t, k""",
                               {"m": self.mid, "mo": self.mode, "t": type_, "k": key, "depth": depth}).fetchall()
        return {"upstream": [dict(r) for r in up], "downstream": [dict(r) for r in down]}

    def field_usage(self, de: dict, field: str) -> list[dict]:
        """Code objects that mention the field and touch the DE (text match: approximate)."""
        users = {(r["src_type"], r["src_key"]) for r in self.db.execute(
            "SELECT src_type, src_key FROM inv_edges WHERE mid=? AND mode=? AND dst_type='de' AND dst_key=?",
            (self.mid, self.mode, de["key"]))}
        pat = re.compile(rf"(?<![\w@]){re.escape(field)}(?!\w)", re.I)
        out = []
        for o in self.objects():
            if o["body"] and pat.search(o["body"]):
                direct = (o["type"], o["key"]) in users
                out.append({"type": o["type"], "key": o["key"], "name": o["name"],
                            "match": "uses DE + field" if direct else "mentions field only"})
        for q in self.objects("query"):
            if (q["meta"].get("target_key") or "").lower() == de["key"].lower():
                out.append({"type": "query", "key": q["key"], "name": q["name"], "match": "writes DE (target)"})
        return sorted({(x["type"], x["key"]): x for x in out}.values(), key=lambda x: (x["match"], x["type"]))

    def search(self, text: str, regex: bool = False) -> list[dict]:
        pat = re.compile(text if regex else re.escape(text), re.I)
        hits = []
        for o in self.objects():
            for i, line in enumerate((o["body"] or "").splitlines(), start=1):
                if pat.search(line):
                    hits.append({"type": o["type"], "key": o["key"], "name": o["name"], "line": i,
                                 "text": line.strip()[:140]})
        return hits

    # -------------------------------------------------------------------------------- health
    def health(self, stale_days: int = 365) -> list[dict]:
        F: list[dict] = []

        def add(sev: str, kind: str, obj: str, msg: str) -> None:
            F.append({"severity": sev, "check": kind, "object": obj, "detail": msg})

        edges = [dict(r) for r in self.db.execute("SELECT * FROM inv_edges WHERE mid=? AND mode=?",
                                                  (self.mid, self.mode))]
        des = self.objects("de")
        de_fields = {d["key"]: self.fields(d["key"]) for d in des}
        for x in edges:
            if x["dst_type"].startswith("missing_"):
                what = x["dst_type"][len("missing_"):]
                add("error", f"missing {what}", f"{x['src_type']}:{self.name_of(x['src_type'], x['src_key'])}",
                    f"{x['rel']} {what} '{x['dst_key']}', which doesn't exist in this BU"
                    + (" (it may be a shared/ENT DE)" if what == "de" else ""))
        for a in self.objects("automation"):
            st = str(a["meta"].get("status") or "")
            lrs = str(a["meta"].get("last_run_status") or "")
            if st.lower() in ("error", "builderror") or lrs.lower() in ("error", "failed"):
                add("error", "automation failing", a["name"], f"status {st or '?'}, last run {lrs or '?'} "
                    f"at {a['meta'].get('last_run') or '?'}")
            elif st.lower() == "paused" and a["meta"].get("schedule"):
                add("warn", "automation paused", a["name"], "has a schedule but is paused")
        for q in self.objects("query"):
            sql = q["body"] or ""
            tk = q["meta"].get("target_key")
            flds = de_fields.get(tk) if tk else None
            if re.search(r"SELECT\s+(TOP\s+\d+\s+)?\*", sql, re.I):
                add("warn", "SELECT *", q["name"], "selects * into a DE; breaks when source fields change")
            if flds is not None and not any(f["pk"] for f in flds) and (q["meta"].get("update_type") or "").lower() == "update":
                add("error", "update without PK", q["name"], f"update type 'Update' but target {tk} has no primary key")
            for m in re.finditer(r"DATEADD\(\s*(DAY|DD|D|MONTH|MM|M)\s*,\s*-(\d+)", sql, re.I):
                days = int(m.group(2)) * (30 if m.group(1).upper().startswith("M") else 1)
                if days > 180 and re.search(r"\b_(Sent|Open|Click|Bounce|Unsubscribe|Job|Complaint)\b", sql, re.I):
                    add("warn", "data view retention", q["name"],
                        f"looks back ~{days} days; system data views keep about 6 months")
        for d in des:
            if d["name"].startswith("_"):   # SFMC system DEs (mobile/push data) aren't ours to fix
                continue
            flds = de_fields.get(d["key"], [])
            if flds and not any(f["pk"] for f in flds):
                add("info", "DE without primary key", d["name"], "upserts and query 'Update' mode need a PK")
            used = any((x["dst_type"] == "de" and x["dst_key"] == d["key"]) for x in edges)
            if not used:
                add("info", "unreferenced DE", d["name"],
                    "nothing in this BU reads or writes it (imports, API writes and UI sends aren't visible here)")
        for j in self.objects("journey"):
            if j["meta"].get("status") in ("Published", "Running") and not j["meta"].get("activities"):
                add("warn", "empty journey", j["name"], "published with no activities")
        cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
        for o in self.objects():
            if o["type"] in ("query", "automation", "journey") and o["modified"]:
                try:
                    dt = _parse_date(o["modified"])
                except ValueError:
                    continue
                if dt < cutoff:
                    add("info", "stale", f"{o['type']}:{o['name']}", f"not modified since {dt.date()}")
        order = {"error": 0, "warn": 1, "info": 2}
        return sorted(F, key=lambda f: (order[f["severity"]], f["check"], f["object"]))


def _parse_date(s: str) -> datetime:
    s = str(s).strip()
    for fmt in ("%m/%d/%Y %I:%M:%S %p",):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    s = re.sub(r"(\.\d+)?(Z)?$", "", s.replace(" ", "T", 1)) if "T" not in s else s
    dt = datetime.fromisoformat(re.sub(r"\.\d+", "", s).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def sync(be: Backend, db: sqlite3.Connection, mid: str, log: Callable[[str], None] = print) -> dict:
    inv = collect(be, log)
    edges = build_edges(inv)
    return AuditStore(db, mid, be.mode).save(inv, edges)
