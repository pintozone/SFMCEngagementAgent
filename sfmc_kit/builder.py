"""Orchestrator: campaign brief -> every SFMC component, in dependency order."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from .brief import Brief
from .client.base import Backend
from .config import DATA_DIR
from .db import Sandbox
from .modules import analytics as an
from .modules.assets import EmailSpec, render_template
from .modules.automation import AutomationSpec, QuerySpec
from .modules.journey import EntrySpec, JourneyBuilder, event_definition_payload, journey_payload
from .sampledata.generator import MASTER_PREFIX, master_de_specs


class BuildError(RuntimeError):
    pass


def plan(brief: Brief) -> list[str]:
    p = brief.prefix
    steps = [f"Check master DEs exist ({MASTER_PREFIX}*), seeded by `seed`"]
    steps += [f"DE  {d.key}" for d in [brief.audience_de, *brief.extra_des]]
    steps += [f"SQL {p}Q_BuildAudience -> {brief.audience_de.key} ({brief.audience_update_type})",
              f"AUT {p}AUTO_Audience  (runs the audience query once)"]
    steps += [f"EML {p}EM_{e.ref}  '{e.subject}'  [{e.template}]" for e in brief.emails]
    steps += [f"EVT {p}EV_Entry  ({brief.entry_type} on {brief.audience_de.key})",
              f"JRN {brief.journey_name}  ({_count_steps(brief.steps)} activities, draft)",
              f"ANL {p}Analytics_ByEmail / _Daily DEs + queries + {p}AUTO_AnalyticsRefresh"]
    return steps


def _count_steps(steps: list[dict]) -> int:
    n = 0
    for s in steps:
        n += 1
        if "decision" in s:
            n += _count_steps(s["decision"].get("yes", []) or []) + _count_steps(s["decision"].get("no", []) or [])
    return n


class Builder:
    def __init__(self, backend: Backend, sandbox: Sandbox, log: Callable[[str], None] = print):
        self.be = backend
        self.sb = sandbox
        self.log = log

    def _rec(self, brief: Brief, type_: str, key: str, name: str, sfmc_id: Any, created: bool | None = None,
             **extra: Any) -> None:
        self.sb.record(brief.code, self.be.mode, type_, key, name, sfmc_id, extra)
        tag = "" if created is None else (" (created)" if created else " (exists/updated)")
        self.log(f"  ✓ {type_:<10} {key}{tag}")

    def _link(self, brief: Brief, parent: tuple[str, str], child: tuple[str, str], rel: str) -> None:
        self.sb.link(brief.code, self.be.mode, parent, child, rel)

    def build(self, brief: Brief, run_audience: bool = True, publish: bool = False) -> dict:
        mode, p = self.be.mode, brief.prefix
        run_id = self.sb.start_run(brief.code, mode, str(brief.source or ""))
        try:
            manifest = self._build(brief, run_audience, publish)
            self.sb.finish_run(run_id, "ok")
            return manifest
        except Exception as e:
            self.sb.finish_run(run_id, "error", str(e)[:2000])
            raise

    def _build(self, brief: Brief, run_audience: bool, publish: bool) -> dict:
        p = brief.prefix
        camp = ("campaign", brief.code)
        self.log(f"[1/7] Preflight: master data")
        missing = [k for k in master_de_specs() if k in brief.audience_sql and not self.be.get_de(k)]
        if missing:
            raise BuildError(f"Master DEs missing in {self.be.mode} tenant: {missing}. Run `seed` first.")

        self.log("[2/7] Data Extensions")
        des: dict[str, dict] = {}
        for spec in [brief.audience_de, *brief.extra_des]:
            de, created = self.be.ensure_de(spec)
            des[spec.key] = de
            self._rec(brief, "de", spec.key, spec.name, de["object_id"], created, sendable=spec.sendable)
            self._link(brief, camp, ("de", spec.key), "owns")

        self.log("[3/7] Audience query + automation")
        q = QuerySpec(key=f"{p}Q_BuildAudience", name=f"{p}Q_BuildAudience", sql=brief.audience_sql,
                      target_key=brief.audience_de.key, target_name=brief.audience_de.name,
                      update_type=brief.audience_update_type, description=f"Audience for {brief.name}")
        qres = self.be.upsert_query(q)
        self._rec(brief, "query", q.key, q.name, qres["id"], qres["created"], target=q.target_key)
        self._link(brief, ("query", q.key), ("de", q.target_key), "writes")
        for m in master_de_specs():
            if m in brief.audience_sql:
                self._link(brief, ("de", m), ("query", q.key), "reads")
        auto = AutomationSpec(key=f"{p}AUTO_Audience", name=f"{p}AUTO_Audience", steps=[[q.key]],
                              description=f"Refresh audience for {brief.name}")
        ares = self.be.upsert_automation(auto, {q.key: qres["id"]})
        self._rec(brief, "automation", auto.key, auto.name, ares["id"], ares["created"])
        self._link(brief, ("automation", auto.key), ("query", q.key), "runs")
        audience_count = None
        if run_audience:
            status = self.be.run_automation(ares["id"])
            rows = self.be.read_rows(brief.audience_de.key, max_rows=100000)
            audience_count = len(rows)
            self.log(f"  → automation {status}; audience = {audience_count} contacts")
            if audience_count == 0:
                self.log("  ! audience is empty; check the audience SQL")

        self.log("[4/7] Email assets (Content Builder)")
        email_meta: dict[str, dict] = {}
        for e in brief.emails:
            html = render_template(e.template, e.blocks, brief.brand)
            spec = EmailSpec(key=f"{p}EM_{e.ref}", name=f"{p}EM_{e.ref}", subject=e.subject, preheader=e.preheader,
                             html=html, folder=f"CampaignKit/{brief.code}")
            res = self.be.upsert_email(spec)
            if not res.get("legacy_id"):
                raise BuildError(f"Email {spec.key} has no legacy id yet; re-run the build in a minute")
            email_meta[e.ref] = {"legacy_id": res["legacy_id"], "subject": e.subject, "preheader": e.preheader,
                                 "name": e.name, "key": spec.key, "asset_id": res["id"]}
            self._rec(brief, "email", spec.key, e.name, res["id"], res["created"], legacy_id=res["legacy_id"],
                      subject=e.subject)
            self._link(brief, camp, ("email", spec.key), "owns")

        self.log("[5/7] Journey entry event")
        entry = EntrySpec(type=brief.entry_type, de_key=brief.audience_de.key, event_key=f"{p}EV_Entry",
                          name=f"{p}EV_Entry", entry_mode=brief.entry_mode)
        ev = self.be.upsert_event_definition(
            event_definition_payload(entry, des[brief.audience_de.key]["object_id"]))
        self._rec(brief, "eventdef", entry.event_key, entry.name, ev["id"], ev["created"], type=brief.entry_type)
        self._link(brief, ("de", brief.audience_de.key), ("eventdef", entry.event_key), "feeds")

        self.log("[6/7] Journey (emails mounted on EMAILV2 activities)")
        jb = JourneyBuilder(email_meta, self.be.send_defaults(brief.send_classification))
        first = jb.build(brief.steps)
        payload = journey_payload(f"{p}JRN", brief.journey_name, brief.objective, entry, ev["id"],
                                  jb.activities, first)
        jres = self.be.upsert_journey(payload)
        self._rec(brief, "journey", payload["key"], brief.journey_name, jres["id"], jres["created"],
                  version=jres["version"], status=jres.get("status"), activities=len(jb.activities))
        self._link(brief, ("eventdef", entry.event_key), ("journey", payload["key"]), "triggers")
        for ref, act_key in jb.email_activity_key.items():
            self._link(brief, ("journey", payload["key"]), ("email", email_meta[ref]["key"]), f"sends@{act_key}")
        out_dir = DATA_DIR / "manifests"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{brief.code}.{self.be.mode}.journey.json").write_text(json.dumps(payload, indent=2))

        published = None
        if publish:
            published = self.be.publish_journey(jres["id"], jres["version"])
            self.log(f"  → journey publish: {published}")
            self.sb.record(brief.code, self.be.mode, "journey", payload["key"], brief.journey_name, jres["id"],
                           {"version": jres["version"], "status": published})

        self.log("[7/7] Analytics objects (Data Views -> report DEs)")
        ades, aqueries, aauto = an.analytics_objects(p, brief.journey_name)
        for spec in ades:
            de, created = self.be.ensure_de(spec)
            self._rec(brief, "de", spec.key, spec.name, de["object_id"], created)
        qids = {}
        for aq in aqueries:
            r = self.be.upsert_query(aq)
            qids[aq.key] = r["id"]
            self._rec(brief, "query", aq.key, aq.name, r["id"], r["created"], target=aq.target_key)
            self._link(brief, ("query", aq.key), ("de", aq.target_key), "writes")
        r = self.be.upsert_automation(aauto, qids)
        self._rec(brief, "automation", aauto.key, aauto.name, r["id"], r["created"])

        manifest = {
            "campaign": brief.name, "code": brief.code, "mode": self.be.mode,
            "built_at": datetime.now().isoformat(timespec="seconds"), "audience_count": audience_count,
            "journey": {"id": jres["id"], "key": payload["key"], "version": jres["version"],
                        "status": published or jres.get("status")},
            "objects": self.sb.objects(brief.code, self.be.mode),
            "lineage": self.sb.lineage(brief.code, self.be.mode),
        }
        (out_dir / f"{brief.code}.{self.be.mode}.json").write_text(json.dumps(manifest, indent=2, default=str))
        return manifest


def run_analytics(backend: Backend, sandbox: Sandbox, code: str, refresh: bool = True) -> dict:
    p = f"CK_{code}_"
    objs = {(o["type"], o["key"]): o for o in sandbox.objects(code, backend.mode)}
    auto = objs.get(("automation", f"{p}AUTO_AnalyticsRefresh"))
    if not auto:
        raise BuildError(f"No analytics automation recorded for {code} in {backend.mode} mode; run build first")
    if refresh:
        backend.run_automation(auto["sfmc_id"])
    by_email = backend.read_rows(f"{p}Analytics_ByEmail")
    daily = backend.read_rows(f"{p}Analytics_Daily", max_rows=1000)
    daily.sort(key=lambda r: str(r.get("EventDay")))
    by_email.sort(key=lambda r: (str(r.get("FirstSend")), str(r.get("ActivityExternalKey"))))
    return {"by_email": by_email, "daily": daily, "summary": an.summarize(by_email)}


def teardown(backend: Backend, sandbox: Sandbox, code: str, log: Callable[[str], None] = print) -> int:
    objs = sandbox.objects(code, backend.mode)
    order = ["journey", "eventdef", "automation", "query", "email", "de"]
    n = 0
    for t in order:
        for o in [o for o in objs if o["type"] == t]:
            try:
                {"journey": backend.delete_journey, "eventdef": backend.delete_event_definition,
                 "automation": backend.delete_automation, "query": backend.delete_query,
                 "email": backend.delete_asset}.get(t, lambda _id: backend.delete_de(o["key"]))(o["sfmc_id"])
                log(f"  ✗ deleted {t:<10} {o['key']}")
                n += 1
            except Exception as e:  # keep going; report what failed
                log(f"  ! could not delete {t} {o['key']}: {str(e)[:200]}")
    sandbox.forget(code, backend.mode)
    return n
