"""Automation Studio operations: list, show, run history, diagnose, run, schedule, builders."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Callable

from .client.base import Backend
from .client.rest import SFMCError
from . import devtools as dt

ACTIVITY_TYPES = {300: "query", 42: "email send", 43: "import", 53: "file transfer", 73: "data extract",
                  423: "script", 467: "wait", 425: "filter", 952: "journey entry", 1101: "refresh group",
                  1000: "verification", 303: "filter", 733: "journey entry", 84: "report"}
STATUS = {-1: "Error", 0: "BuildError", 1: "Building", 2: "Ready", 3: "Running", 4: "Paused", 5: "Stopped",
          6: "Scheduled", 7: "AwaitingTrigger", 8: "InactiveTrigger"}


# ---------------------------------------------------------------------------------------------- read
def list_automations(be: Backend) -> list[dict]:
    if be.mode == "live":
        from .audit import _pages
        items = _pages(be, "/automation/v1/automations")
        return [{"id": a["id"], "key": a.get("key"), "name": a.get("name"), "status": a.get("status"),
                 "type": a.get("type"), "schedule": (a.get("schedule") or {}).get("scheduleStatus"),
                 "last_run": a.get("lastRunTime")} for a in items]
    out = []
    for r in be.db.execute("SELECT key, id, name, payload FROM mock_objects WHERE type='automation'"):  # type: ignore[attr-defined]
        p = json.loads(r["payload"])
        runs = be.automation_runs(r["id"], 1)  # type: ignore[attr-defined]
        out.append({"id": r["id"], "key": r["key"], "name": r["name"],
                    "status": "Error" if runs and runs[0]["status"] == "Error" else p.get("status", "Ready"),
                    "type": "scheduled", "schedule": p.get("schedule_status", "none"),
                    "last_run": runs[0]["started"] if runs else None})
    return sorted(out, key=lambda a: a["name"])


def find(be: Backend, ref: str) -> dict:
    items = list_automations(be)
    hit = next((a for a in items if ref in (a["id"], a["key"]) or (a["name"] or "").lower() == ref.lower()), None)
    if not hit:
        part = [a for a in items if ref.lower() in (a["name"] or "").lower()]
        if len(part) == 1:
            hit = part[0]
        elif part:
            raise SFMCError(f"'{ref}' matches several automations: {', '.join(a['name'] for a in part)}")
        else:
            raise SFMCError(f"automation '{ref}' not found")
    return detail(be, hit)


def detail(be: Backend, a: dict) -> dict:
    steps: list[dict] = []
    if be.mode == "live":
        d = be.rest.get(f"/automation/v1/automations/{a['id']}")  # type: ignore[attr-defined]
        for s in d.get("steps", []):
            acts = []
            for x in s.get("activities", []):
                act = {"name": x.get("name"), "type": ACTIVITY_TYPES.get(x.get("objectTypeId"), str(x.get("objectTypeId"))),
                       "object_id": x.get("activityObjectId"),
                       "targets": [{"name": t.get("name"), "key": t.get("key"), "rows": t.get("rowCount")}
                                   for t in x.get("targetDataExtensions") or []]}
                if x.get("objectTypeId") == 300 and x.get("activityObjectId"):
                    try:
                        q = be.rest.get(f"/automation/v1/queries/{x['activityObjectId']}")  # type: ignore[attr-defined]
                        act.update({"sql": q.get("queryText"), "target_key": q.get("targetKey"),
                                    "update_type": q.get("targetUpdateTypeName"), "query_key": q.get("key")})
                    except SFMCError:
                        pass
                acts.append(act)
            steps.append({"step": s.get("step"), "activities": acts})
        sched = d.get("schedule") or {}
    else:
        p = json.loads(be.db.execute("SELECT payload FROM mock_objects WHERE type='automation' AND id=?",  # type: ignore[attr-defined]
                                     (a["id"],)).fetchone()[0])
        for i, step in enumerate(p["steps"], start=1):
            acts = []
            for qid in step:
                q = be._get_by_id("query", qid)  # type: ignore[attr-defined]
                tgt = be.get_de(q["target_key"]) if q else None
                rows = be.db.execute(f'SELECT COUNT(*) FROM "{tgt["name"]}"').fetchone()[0] if tgt else None  # type: ignore[attr-defined]
                acts.append({"name": q["name"] if q else qid, "type": "query", "object_id": qid, "sql": q and q["sql"],
                             "target_key": q and q["target_key"], "update_type": q and q["update_type"],
                             "query_key": q and q["key"], "targets": [{"name": tgt["name"], "key": tgt["key"], "rows": rows}] if tgt else []})
            steps.append({"step": i, "activities": acts})
        sched = {"scheduleStatus": p.get("schedule_status", "none")}
    return {**a, "steps": steps, "schedule_detail": sched}


def runs(be: Backend, info: dict, limit: int = 20) -> list[dict]:
    if be.mode == "live":
        return be.automation_runs(info["key"], limit)  # type: ignore[attr-defined]
    return be.automation_runs(info["id"], limit)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------------------------- diagnose
DEEP_SQL = """SELECT ai.ActivityName, ai.ActivityType, ai.ActivityInstanceStatus, ai.ActivityInstanceStatusDetails,
       ai.ActivityInstanceStartTime_UTC, ai.ActivityInstanceEndTime_UTC
FROM _AutomationActivityInstance ai
WHERE ai.AutomationCustomerKey = '{key}' AND ai.ActivityInstanceStartTime_UTC > DATEADD(DAY, -{days}, GETDATE())"""


def diagnose(be: Backend, info: dict, store: Any = None, deep_rows: list[dict] | None = None) -> dict:
    findings: list[dict] = []
    last = (runs(be, info, 5) or [None])[0]

    def add(sev: str, where: str, msg: str, fix: str = "") -> None:
        findings.append({"severity": sev, "where": where, "detail": msg, "fix": fix})

    if last is None:
        add("info", "history", "no run history available for this automation")
    elif str(last["status"]).lower() not in ("complete", "completed"):
        add("error", "last run", f"{last['status']}: {last.get('message') or ''} (started {last['started']})"
            + (f", failing activity {last['activity']}" if last.get("activity") else ""))
    st = str(info.get("status") or "")
    if "paus" in st.lower() or st.lower() == "stopped":
        add("warn", "status", f"automation is {st}", "resume it if it should still run on schedule")
    for s in info["steps"]:
        for act in s["activities"]:
            where = f"step {s['step']} · {act['type']} · {act['name']}"
            if act["type"] != "query" or not act.get("sql"):
                continue
            tgt = act.get("target_key")
            if tgt and not be.get_de(tgt):
                add("error", where, f"target DE {tgt} doesn't exist", "recreate the DE or retarget the query")
            res = dt.lint_sql(act["sql"], store, tgt)
            if be.mode == "live":
                v = be.validate_sql(act["sql"])  # type: ignore[attr-defined]
                for e in v["errors"]:
                    res.add("error", "SFMC validation", e)
            for f in res.findings:
                if f.severity in ("error", "warn"):
                    add(f.severity, where, f"{f.rule}: {f.message}", _fix_for(f.rule))
    for r in deep_rows or []:
        st = str(r.get("ActivityInstanceStatus") or "")
        if st and st.lower() not in ("complete", "completed", "success"):
            add("error", f"activity run · {r.get('ActivityName')}",
                f"{st}: {r.get('ActivityInstanceStatusDetails') or ''} ({r.get('ActivityInstanceStartTime_UTC')})")
    causes = [f for f in findings if f["severity"] == "error"]
    return {"last_run": last, "findings": findings,
            "summary": ("Likely cause: " + causes[0]["detail"]) if causes else "No errors found in the definition."}


def _fix_for(rule: str) -> str:
    return {
        "possible duplicate keys": "dedupe with ROW_NUMBER() OVER (PARTITION BY <PK>) … WHERE rn = 1",
        "may truncate": "widen the target field or LEFT()/SUBSTRING() the value",
        "required field missing": "select the field (or give it a default in the DE)",
        "column not in target": "add the field to the target DE or remove/rename the column",
        "unknown field": "fix the field name (see `audit impact <DE>` for its fields)",
        "unknown DE": "fix the DE name, or prefix shared DEs with ENT.",
        "ORDER BY without TOP": "remove ORDER BY or add TOP",
        "SELECT * with JOIN": "list the columns explicitly",
        "data view without date filter": "filter on EventDate to stay within ~6 months and the 30-minute limit",
    }.get(rule, "")


def deep_sql(info: dict, days: int = 7) -> str:
    return DEEP_SQL.format(key=info["key"].replace("'", "''"), days=int(days))


# ---------------------------------------------------------------------------------------------- schedules
SFMC_TZ_OFFSET = -6   # SFMC server time: US Central Standard Time, no daylight saving
DAY_NAMES = {"MO": "Monday", "TU": "Tuesday", "WE": "Wednesday", "TH": "Thursday", "FR": "Friday",
             "SA": "Saturday", "SU": "Sunday"}


def to_sfmc_time(local: datetime, tz: str | None) -> datetime:
    """Convert a naive local time in `tz` (IANA name) to SFMC server time (naive, UTC-6)."""
    from datetime import timedelta, timezone
    from zoneinfo import ZoneInfo
    if not tz:
        return local
    aware = local.replace(tzinfo=ZoneInfo(tz))
    return aware.astimezone(timezone(timedelta(hours=SFMC_TZ_OFFSET))).replace(tzinfo=None)


def recurrence_xml(every: str, interval: int, start: datetime, until: datetime | None = None,
                   count: int | None = None, days: list[str] | None = None) -> str:
    every = every.lower()
    if every == "daily":
        rec = ('<Recurrence xsi:type="DailyRecurrence"><DailyRecurrencePatternType>Interval</DailyRecurrencePatternType>'
               f"<DayInterval>{interval}</DayInterval></Recurrence><RecurrenceType>Daily</RecurrenceType>")
    elif every == "hourly":
        rec = ('<Recurrence xsi:type="HourlyRecurrence"><HourlyRecurrencePatternType>Interval</HourlyRecurrencePatternType>'
               f"<HourInterval>{interval}</HourInterval></Recurrence><RecurrenceType>Hourly</RecurrenceType>")
    elif every == "weekly":
        days = [d.upper()[:2] for d in (days or [])] or [list(DAY_NAMES)[start.weekday()]]
        bad = [d for d in days if d not in DAY_NAMES]
        if bad:
            raise ValueError(f"unknown day(s) {bad}; use MO,TU,WE,TH,FR,SA,SU")
        flags = "".join(f"<{DAY_NAMES[d]}>true</{DAY_NAMES[d]}>" for d in days)
        rec = ('<Recurrence xsi:type="WeeklyRecurrence"><WeeklyRecurrencePatternType>ByDay</WeeklyRecurrencePatternType>'
               f"<WeekInterval>{interval}</WeekInterval>{flags}</Recurrence><RecurrenceType>Weekly</RecurrenceType>")
    else:
        raise ValueError("every must be hourly, daily or weekly")
    if count:
        rng = f"<RecurrenceRangeType>EndAfter</RecurrenceRangeType><Occurrences>{int(count)}</Occurrences>"
    else:
        from datetime import timedelta
        end = until or (start + timedelta(days=365))
        rng = f"<RecurrenceRangeType>EndOn</RecurrenceRangeType>"
    xml = f"<Schedule>{rec}{rng}<StartDateTime>{start:%Y-%m-%dT%H:%M:%S}</StartDateTime>"
    if not count:
        xml += f"<EndDateTime>{end:%Y-%m-%dT%H:%M:%S}</EndDateTime>"
    return xml + "</Schedule>"


def recurrence_from_ical(sched: dict, now: datetime) -> str:
    """Rebuild a Schedule from the REST schedule (kept while paused), moving the start into the future."""
    from datetime import timedelta
    ical = dict(p.split("=", 1) for p in str(sched.get("icalRecur") or "").split(";") if "=" in p)
    freq = ical.get("FREQ", "DAILY").lower()
    every = {"daily": "daily", "weekly": "weekly", "hourly": "hourly"}.get(freq)
    if not every:
        raise SFMCError(f"can't resume a {freq} schedule from the API; resume it in Automation Studio")
    interval = int(ical.get("INTERVAL", 1))
    start = datetime.fromisoformat(str(sched["startDate"])[:19])
    step = timedelta(hours=interval) if every == "hourly" else timedelta(days=1)
    while start <= now:
        start += step
    until = datetime.strptime(ical["UNTIL"][:15], "%Y%m%dT%H%M%S") if "UNTIL" in ical else \
        (datetime.fromisoformat(str(sched["endDate"])[:19]) if sched.get("endDate") and not ical.get("COUNT") else None)
    count = int(ical["COUNT"]) if "COUNT" in ical else None
    days = ical.get("BYDAY", "").split(",") if ical.get("BYDAY") else None
    if until and until <= start:
        raise SFMCError("the paused schedule has already ended; create a new one with auto schedule")
    return recurrence_xml(every, interval, start, until, count, days)


def sfmc_now() -> datetime:
    from datetime import timedelta, timezone
    return datetime.now(timezone(timedelta(hours=SFMC_TZ_OFFSET))).replace(tzinfo=None)


def set_schedule(be: Backend, info: dict, xml: str) -> dict:
    if be.mode == "live":
        return be.schedule_automation(info["id"], xml)  # type: ignore[attr-defined]
    return _mock_sched(be, info, "active", xml)


def pause(be: Backend, info: dict) -> dict:
    if be.mode == "live":
        return be.pause_schedule(info["id"])  # type: ignore[attr-defined]
    return _mock_sched(be, info, "paused")


def resume(be: Backend, info: dict) -> dict:
    if be.mode == "live":
        cur = be.automation_schedule(info["id"])  # type: ignore[attr-defined]
        if cur.get("scheduleStatus") != "paused":
            raise SFMCError(f"{info['name']} isn't paused (schedule status {cur.get('scheduleStatus')})")
        return be.schedule_automation(info["id"], recurrence_from_ical(cur, sfmc_now()))  # type: ignore[attr-defined]
    return _mock_sched(be, info, "active")


def unschedule(be: Backend, info: dict) -> dict:
    if be.mode == "live":
        return be.unschedule(info["id"])  # type: ignore[attr-defined]
    return _mock_sched(be, info, "none")


def _mock_sched(be: Any, info: dict, status: str, xml: str | None = None) -> dict:
    r = be.db.execute("SELECT key, name, payload FROM mock_objects WHERE type='automation' AND id=?", (info["id"],)).fetchone()
    p = json.loads(r["payload"])
    p["schedule_status"] = status
    if xml:
        p["schedule_xml"] = xml
    p["status"] = {"active": "Scheduled", "paused": "PausedSchedule", "none": "Ready"}[status]
    be._put("automation", r["key"], r["name"], p)
    return {"status": p["status"], "scheduleStatus": status}
