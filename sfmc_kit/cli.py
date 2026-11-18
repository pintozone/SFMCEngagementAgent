"""Command line: python -m sfmc_kit <command> ..."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

import yaml

from .brief import BriefError, load_brief, load_yaml
from .builder import BuildError, Builder, plan, run_analytics, teardown
from .client.base import Backend
from .client.rest import SFMCError
from .auth import AuthError
from .config import DATA_DIR, ConfigError, load_live_config
from .db import Sandbox, mock_db_path
from .modules.assets import preview, render_template
from .modules.data_extensions import DESpec
from .sampledata.generator import generate, master_de_specs


def get_backend(mode: str, verbose: bool) -> Backend:
    if mode == "live":
        from .client.live import LiveBackend
        return LiveBackend(load_live_config(), verbose=verbose)
    from .client.mock import MockBackend
    return MockBackend(mock_db_path(), verbose=verbose)


def table(rows: list[dict], cols: list[str] | None = None, limit: int = 50, maxw: int = 40) -> None:
    if not rows:
        print("  (no rows)")
        return
    cols = cols or list(rows[0].keys())
    rows = rows[:limit]
    w = {c: min(maxw, max(len(c), *(len(str(r.get(c, ""))) for r in rows))) for c in cols}
    print("  " + "  ".join(c.ljust(w[c]) for c in cols))
    print("  " + "  ".join("-" * w[c] for c in cols))
    for r in rows:
        print("  " + "  ".join(str(r.get(c, "") if r.get(c) is not None else "")[:maxw].ljust(w[c]) for c in cols))


def confirm(prompt: str, expected: str, assume_yes: bool) -> None:
    if assume_yes:
        return
    ans = input(f"{prompt}\nType '{expected}' to continue: ").strip()
    if ans != expected:
        raise SystemExit("Aborted.")


def cmd_auth_test(a, be: Backend) -> None:
    print(json.dumps(be.whoami(), indent=2))


def cmd_de(a, be: Backend) -> None:
    if a.de_cmd == "list":
        table(be.list_des(a.like or ""))
    elif a.de_cmd == "describe":
        table(be.describe_de(a.key))
    elif a.de_cmd == "rows":
        table(be.read_rows(a.key, max_rows=a.limit), limit=a.limit)
    elif a.de_cmd == "create":
        spec = DESpec.from_dict(load_yaml(Path(a.spec).read_text()))
        de, created = be.ensure_de(spec)
        print(f"{'Created' if created else 'Exists'}: {de}")
    elif a.de_cmd == "load":
        with open(a.csv, newline="") as f:
            rows = [{k: (v if v != "" else None) for k, v in r.items()} for r in csv.DictReader(f)]
        pk = [f["name"] for f in be.describe_de(a.key) if f["pk"]]
        if not pk:
            raise SystemExit("DE has no primary key; the rowset upsert API needs one")
        print(f"Upserted {be.upsert_rows(a.key, rows, pk)} rows into {a.key}")
    elif a.de_cmd == "delete":
        confirm(f"Delete DE {a.key} in {be.mode} mode?", a.key, a.yes)
        be.delete_de(a.key)
        print("Deleted.")


def cmd_seed(a, be: Backend) -> None:
    data = generate(a.scale, seed=a.seed)
    specs = master_de_specs()
    sb = Sandbox()
    out = DATA_DIR / "sample"
    out.mkdir(parents=True, exist_ok=True)
    for key, rows in data.items():
        spec = specs[key]
        sb.mirror_table(spec, rows)
        with open(out / f"{key}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=[fl.name for fl in spec.fields])
            w.writeheader()
            w.writerows(rows)
        print(f"  generated {len(rows):>6} rows  {key}")
    print(f"  CSVs in {out}; local mirror in {sb.path}")
    if be.mode == "live" and not a.push:
        print("Live mode: add --push to create the master DEs and upload rows to SFMC.")
        return
    if be.mode == "live":
        total = sum(len(r) for r in data.values())
        confirm(f"Create {len(specs)} master DEs and upsert {total} rows into the LIVE tenant?", "PUSH", a.yes)
    for key, rows in data.items():
        spec = specs[key]
        de, created = be.ensure_de(spec)
        n = be.upsert_rows(key, rows, spec.pk_fields)
        print(f"  {'created' if created else 'exists '} {key:<32} upserted {n} rows")


def cmd_build(a, be: Backend) -> None:
    brief = load_brief(a.brief)
    print(f"Campaign: {brief.name} [{brief.code}]  mode={be.mode}")
    for s in plan(brief):
        print(f"  • {s}")
    if a.dry_run:
        print("Dry run only; nothing was created.")
        return
    if a.publish and be.mode == "live":
        confirm("Publishing makes the journey LIVE. Contacts in the entry DE can receive email.", brief.code, a.yes)
    if be.mode == "live":
        confirm(f"Create/update the objects above in the LIVE tenant?", "BUILD", a.yes)
    m = Builder(be, Sandbox()).build(brief, run_audience=not a.skip_audience, publish=a.publish)
    print(f"\nDone. Manifest: {DATA_DIR / 'manifests' / f'{brief.code}.{be.mode}.json'}")
    print(f"Journey {m['journey']['key']} v{m['journey']['version']}: {m['journey']['status']}; "
          f"audience={m['audience_count']}")


def cmd_analytics(a, be: Backend) -> None:
    res = run_analytics(be, Sandbox(), a.code.upper(), refresh=not a.no_refresh)
    print(f"\nKPIs per email activity: {a.code.upper()} ({be.mode})")
    table(res["by_email"], ["ActivityExternalKey", "ActivityName", "Sends", "UniqueOpens", "UniqueClicks", "Bounces", "Unsubs",
                            "FirstSend", "LastSend"])
    print("\nCampaign totals")
    table([res["summary"]])
    print("\nDaily trend")
    table(res["daily"], ["EventDay", "Sends", "Opens", "Clicks"], limit=60)
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=2, default=str))
        print(f"\nWrote {a.json}")


def cmd_preview(a, be: Backend) -> None:
    brief = load_brief(a.brief)
    rows = be.read_rows(brief.audience_de.key, max_rows=1) if be.get_de(brief.audience_de.key) else []
    row = rows[0] if rows else {"FirstName": "Alex", "CurrentModel": "Strada", "DealerName": "Detroit Aurora"}
    out = DATA_DIR / "previews"
    out.mkdir(parents=True, exist_ok=True)
    for e in brief.emails:
        rendered = render_template(e.template, e.blocks, brief.brand)
        html = preview(rendered, row)
        # Subjects are evaluated after the body in SFMC, so body variables are available to them.
        subject = preview(rendered + "<!--SUBJ-->" + e.subject, row).split("<!--SUBJ-->")[-1]
        p = out / f"{brief.code}_{e.ref}.html"
        p.write_text(html)
        print(f"  {p}\n    subject: {subject}")


def cmd_status(a, be: Backend) -> None:
    sb = Sandbox()
    code = a.code.upper()
    objs = sb.objects(code, be.mode)
    table(objs, ["type", "key", "sfmc_id", "updated_at"])
    print("\nLineage")
    table(sb.lineage(code, be.mode), ["parent_type", "parent_key", "relation", "child_type", "child_key"], limit=200)


def cmd_teardown(a, be: Backend) -> None:
    code = a.code.upper()
    confirm(f"Delete every object recorded for {code} in {be.mode} mode?", code, a.yes)
    n = teardown(be, Sandbox(), code)
    print(f"Deleted {n} objects.")


def cmd_schema(a, be: Backend) -> None:
    """Fields of the master DEs (what audience SQL may use), with example values."""
    import sqlite3
    specs = master_de_specs()
    samples: dict[str, dict[str, list]] = {}
    sb_path = Sandbox().path
    db = sqlite3.connect(str(sb_path))
    for key, spec in specs.items():
        try:
            rows = db.execute(f'SELECT * FROM "sample_{spec.name}" LIMIT 400').fetchall()
            cols = [d[0] for d in db.execute(f'SELECT * FROM "sample_{spec.name}" LIMIT 1').description]
        except sqlite3.Error:
            continue
        samples[key] = {c: list(dict.fromkeys(str(r[i]) for r in rows if r[i] is not None))[:4]
                        for i, c in enumerate(cols)}
    db.close()
    keys = list(specs)
    if be.mode == "live":
        keys = [d["key"] for d in be.list_des("CK_Master_")]
    for key in keys:
        print(f"\n[{key}]" + ("  (sendable on SubscriberKey)" if key in specs and specs[key].sendable else ""))
        if be.mode == "live" or key not in specs:
            fields = [{"name": f["name"], "type": f["type"], "pk": f["pk"]} for f in be.describe_de(key)]
        else:
            fields = [{"name": f.name, "type": f.type, "pk": f.pk} for f in specs[key].fields]
        for f in fields:
            ex = ", ".join(samples.get(key, {}).get(f["name"], []))
            print(f"  {f['name']:<22} {f['type']:<12} {'PK' if f['pk'] else '  '}  {ex[:70]}")
    if not samples:
        print("\n(no example values yet: run `python -m sfmc_kit seed`)")


def cmd_templates(a, be: Backend) -> None:
    from .briefcheck import list_templates
    for t in list_templates():
        print(f"\n{t.name}\n  {t.description}")
        print(f"  blocks:     {', '.join(t.blocks)}")
        print(f"  optional:   footnote, cta_alias, hero_alt (defaults applied)")
        print(f"  variables:  {', '.join('@' + v for v in t.variables)}  (use as %%=v(@Var)=%% in subject/blocks)")
        print(f"  reads DE fields: {', '.join(t.attributes)}")


def cmd_brief(a, be: Backend) -> None:
    from .briefcheck import check_brief, format_result
    res = check_brief(a.brief)
    print(format_result(res))
    if not res.ok:
        raise SystemExit(1)


def cmd_cloudpage(a, be: Backend) -> None:
    from .modules import cloudpages as cpm
    if a.cp_cmd == "bookings":
        p = f"CK_{a.code.upper()}_"
        rows = be.read_rows(f"{p}TestDriveBookings", max_rows=100000)
        rows.sort(key=lambda r: str(r.get("CreatedDate")), reverse=True)
        print(f"{len(rows)} booking(s) in {p}TestDriveBookings ({be.mode}); newest {min(a.limit, len(rows))}:")
        table(rows, ["SubscriberKey", "FirstName", "LastName", "Model", "DealerID", "PreferredDate",
                     "PreferredTime", "Status", "CreatedDate"], limit=a.limit)
        return
    brief = load_brief(a.brief)
    if not brief.cloudpage:
        raise BriefError(f"{a.brief} has no cloudpage section")
    p = brief.prefix
    if a.cp_cmd == "stub":
        print(f"Paste this single line as the content of a new CloudPage, then publish it:\n\n  {cpm.page_stub(p)}\n")
        print(f"Then set cloudpage.page_id in {a.brief} to the page's ID and rebuild, so emails link to it.")
        return
    dealers = be.read_rows("CK_Master_Dealers", max_rows=2500) if be.get_de("CK_Master_Dealers") else []
    html = cpm.render_page(brief.cloudpage, brief.code, p, brief.brand, dealers)
    rows = be.read_rows(brief.audience_de.key, max_rows=1) if be.get_de(brief.audience_de.key) else []
    sample = {**(rows[0] if rows else {"FirstName": "Alex", "EmailAddress": "alex@example.com"}),
              "Model": brief.cloudpage.default_model}
    out = DATA_DIR / "previews"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{brief.code}_cloudpage.ampscript.html").write_text(html)
    path = out / f"{brief.code}_cloudpage.html"
    path.write_text(cpm.preview_page(html, sample))
    print(f"  {path}  (static preview, pre-filled for {sample.get('FirstName')})")
    print(f"  {out / f'{brief.code}_cloudpage.ampscript.html'}  (exact page body sent to SFMC)")


def _audit_store(be: Backend):
    from .audit import AuditStore
    mid = "mock"
    if be.mode == "live":
        mid = be.cfg.account_id or str(be.whoami().get("mid"))  # type: ignore[attr-defined]
    return AuditStore(Sandbox().db, mid, be.mode)


def cmd_audit(a, be: Backend) -> None:
    from . import audit as au
    store = _audit_store(be)
    if a.audit_cmd == "sync":
        print(f"Inventorying MID {store.mid} ({be.mode}), read-only…")
        counts = au.sync(be, store.db, store.mid)
        print("Synced: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        return
    if not store.synced():
        raise SFMCError(f"No inventory for MID {store.mid} ({be.mode}) yet: run `audit sync` first")
    s = store.synced()
    if a.audit_cmd == "impact":
        ref, _, field = a.object.partition(".") if a.object.count(".") == 1 and not store.resolve(a.object) else (a.object, "", "")
        obj = store.resolve(ref)
        if not obj:
            raise SFMCError(f"'{ref}' not found in the inventory (synced {s['synced_at']})")
        print(f"{obj['type']}: {obj['name']} (key {obj['key']})   inventory synced {s['synced_at']}")
        if obj["type"] == "de":
            flds = store.fields(obj["key"])
            print(f"  fields: {', '.join(f['name'] + ('*' if f['pk'] else '') for f in flds)}  (* = primary key)")
        if field:
            print(f"\nWhere field '{field}' is used (text match, so treat as a starting point):")
            table(store.field_usage(obj, field), ["match", "type", "name"], limit=200)
        imp = store.impact(obj["type"], obj["key"])
        def fmt(rows):
            seen, out = {(obj["type"], obj["key"])}, []
            for r in rows:  # first (shallowest) path to each object only
                if (r["t"], r["k"]) in seen:
                    continue
                seen.add((r["t"], r["k"]))
                out.append({"depth": r["d"], "object": f"{r['t']}: {store.name_of(r['t'], r['k'])}",
                            "relation": r["rel"], "via": f"{r['via_t']}: {store.name_of(r['via_t'], r['via_k'])}"})
            return out
        print("\nDepends on this (affected if it changes):")
        table(fmt(imp["upstream"]), ["depth", "object", "relation", "via"], limit=200)
        print("\nThis depends on:")
        table(fmt(imp["downstream"]), ["depth", "object", "relation", "via"], limit=200)
    elif a.audit_cmd == "health":
        f = store.health(stale_days=a.stale_days)
        if a.severity:
            f = [x for x in f if x["severity"] in a.severity.split(",")]
        print(f"{len(f)} finding(s) for MID {store.mid} ({be.mode}), inventory synced {s['synced_at']}")
        table(f, ["severity", "check", "object", "detail"], limit=a.limit)
    elif a.audit_cmd == "search":
        hits = store.search(a.text, regex=a.regex)
        print(f"{len(hits)} line(s) match in queries, emails, blocks and pages")
        table(hits, ["type", "name", "line", "text"], limit=a.limit)
    elif a.audit_cmd == "report":
        from .audit_report import render
        out = DATA_DIR / "reports"
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"audit_{store.mid}_{be.mode}.html"
        path.write_text(render(store, store.health(stale_days=a.stale_days)))
        print(f"Report: {path}")


def _read_src(arg: str) -> str:
    if arg == "-":
        return sys.stdin.read()
    p = Path(arg)
    if p.exists():
        return p.read_text()
    return arg  # literal SQL / code


def _store_if_synced(be: Backend):
    st = _audit_store(be)
    if st.synced():
        return st
    print(f"(no inventory for {be.mode} yet: DE/field checks skipped. Run `audit sync` to enable them.)")
    return None


def cmd_sql(a, be: Backend) -> None:
    from . import devtools as dt
    sql = _read_src(a.sql)
    if a.sql_cmd == "lint":
        res = dt.lint_sql(sql, _store_if_synced(be), a.target)
        if be.mode == "live" and not a.no_validate:
            v = be.validate_sql(sql)  # type: ignore[attr-defined]
            for e in v["errors"]:
                res.add("error", "SFMC validation", e)
            for w in v["warnings"]:
                res.add("info", "SFMC validation", w)
        table(res.rows(), ["severity", "rule", "line", "message"], limit=200, maxw=140)
        print("OK" if res.ok else "FAILED")
        if not res.ok:
            raise SystemExit(1)
    elif a.sql_cmd == "explain":
        import json as _j
        ex = dt.explain_sql(sql, _store_if_synced(be))
        print("Sources:"); table(ex["sources"], ["alias", "source", "kind"])
        print("Joins:"); table(ex["joins"], ["type", "source", "on"])
        print(f"Filters: {ex['filters'] or '(none)'}")
        print(f"Grouping: {ex['grouping'] or '(none)'}   Dedupe with ROW_NUMBER(): {ex['dedupe']}   Subqueries: {ex['subqueries']}")
        print("Output columns:"); table(ex["outputs"], ["name", "expr"])
    elif a.sql_cmd == "run":
        res = dt.lint_sql(sql)
        if not res.ok:
            table(res.rows(), ["severity", "rule", "line", "message"])
            raise SystemExit("Fix the lint errors first.")
        if be.mode == "mock":
            import sqlite3
            from .client.sqlxlate import translate
            db = sqlite3.connect(f"file:{mock_db_path()}?mode=ro", uri=True)
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute(translate(sql)).fetchmany(a.top)]
            print(f"{len(rows)} row(s) (mock tenant, first {a.top})")
            table(rows, limit=a.top)
            return
        v = be.validate_sql(sql)  # type: ignore[attr-defined]
        if not v["valid"]:
            raise SFMCError("SFMC validation failed: " + "; ".join(v["errors"]))
        rows = _sql_run_live(a, be, sql, dt)
        print(f"{len(rows)} row(s) (live, first {a.top})")
        table(rows, limit=a.top)


def _sql_run_live(a, be, sql: str, dt) -> list[dict]:
    from .modules.automation import AutomationSpec, QuerySpec
    from .modules.data_extensions import FieldSpec
    store = _store_if_synced(be)
    cols = dt.output_columns(sql)
    aliases = dt.table_aliases(sql)
    fields = []
    for c in cols:
        ftype, length = "Text", 4000
        if store and c["field"] and c["alias_src"]:
            src = store.resolve(aliases.get(c["alias_src"].lower(), ""))
            sf = next((f for f in store.fields(src["key"]) if f["name"].lower() == c["field"].lower()), None) if src else None
            if sf and sf["type"] in ("Number", "Date", "Boolean", "Decimal"):
                ftype, length = sf["type"], None
        fields.append(FieldSpec(c["name"], ftype, length) if ftype == "Text" else FieldSpec(c["name"], ftype))
    name = dt.scratch_name(sql)
    if any(re.fullmatch(r"(\w+\.)?\*", c["expr"].strip()) for c in cols):
        raise SFMCError("list the columns explicitly for `sql run` (SELECT * can't be typed into a scratch DE)")
    collist = ", ".join(f"[{c['name']}]" for c in cols)
    wrapped = sql if re.search(r"\bSELECT\s+(DISTINCT\s+)?TOP\b", sql, re.I) else \
        f"SELECT TOP {a.top} {collist} FROM (\n{sql}\n) AS _q"   # SFMC rejects SELECT * when JOINs are present
    confirm(f"Create scratch DE/query/automation {name} in the LIVE tenant, run it, read up to {a.top} rows, "
            f"then delete all three?", "RUN", a.yes)
    spec = DESpec(name, name, fields, folder="CampaignKit/Scratch", description="sfmc_kit sql run (temporary)")
    created = []
    try:
        be.ensure_de(spec); created.append(("de", name))
        q = be.upsert_query(QuerySpec(name, name, wrapped, name, name)); created.append(("query", q["id"]))
        au = be.upsert_automation(AutomationSpec(name, name, [[name]]), {name: q["id"]}); created.append(("automation", au["id"]))
        status = be.run_automation(au["id"], timeout=a.timeout)
        print(f"  automation finished: {status}")
        return be.read_rows(name, max_rows=a.top)
    finally:
        if a.keep:
            print(f"  kept scratch objects {name} (--keep)")
        else:
            for kind, ident in reversed(created):
                try:
                    {"automation": be.delete_automation, "query": be.delete_query, "de": be.delete_de}[kind](ident)
                except Exception as e:  # report but keep cleaning
                    print(f"  ! could not delete {kind} {ident}: {e}")
            print(f"  cleaned up scratch objects {name}")


def cmd_amp(a, be: Backend) -> None:
    from . import devtools as dt
    if a.amp_cmd == "lint":
        store = _store_if_synced(be)
        p = Path(a.source)
        if p.exists():
            code, label = p.read_text(), str(p)
        else:
            obj = store.resolve(a.source) if store else None
            if not obj or not obj.get("body"):
                raise SFMCError(f"{a.source} is neither a file nor an email/block/page in the inventory")
            code, label = obj["body"], f"{obj['type']}: {obj['name']}"
        res = dt.lint_ampscript(code, store, a.de)
        print(f"Linting {label}")
        table(res.rows(), ["severity", "rule", "line", "message"], limit=200, maxw=140)
        print("OK" if res.ok else "FAILED")
        if not res.ok:
            raise SystemExit(1)
    elif a.amp_cmd == "render":
        out = DATA_DIR / "previews"
        out.mkdir(parents=True, exist_ok=True)
        if be.mode == "live":
            r = be.render_email(a.email, a.de, a.key, a.row)  # type: ignore[attr-defined]
            html = r["html"]
            who = r["record"].get("SubscriberKey") or r["record"].get("subscriberkey") or f"row {r['row']}"
            note = {True: "verified", False: "NOT verified: the rendered email shows a different address", None: "unverified"}[r["verified"]]
        else:
            from .modules.assets import preview
            body = next((o for o in be.db.execute("SELECT payload FROM mock_objects WHERE type='asset' AND key=?", (a.email,))), None)  # type: ignore[attr-defined]
            if not body:
                raise SFMCError(f"email {a.email} not found in the mock tenant")
            import json as _j
            rows = be.read_rows(a.de, max_rows=100000)
            rec = next((x for x in rows if a.key and str(x.get("SubscriberKey")) == a.key), rows[(a.row or 2) - 2] if rows else {})
            html, who, note = preview(_j.loads(body[0])["html"], rec), rec.get("SubscriberKey"), "local emulator"
        path = out / f"render_{a.email}_{who}.html"
        path.write_text(html)
        import html as _h
        text = _h.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<style.*?</style>", "", html, flags=re.S))))
        print(f"Rendered {a.email} for {who} ({note}) → {path}")
        print("  " + text.strip()[:600])


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--mode", choices=["mock", "live"], default=argparse.SUPPRESS)
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("-y", "--yes", action="store_true", default=argparse.SUPPRESS,
                        help="skip confirmation prompts")
    ap = argparse.ArgumentParser(prog="sfmc_kit", description="SFMC Campaign Kit", parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)
    _add = sub.add_parser
    sub.add_parser = lambda *a, **k: _add(*a, parents=[common], **k)  # type: ignore[method-assign]

    sub.add_parser("auth-test", help="verify credentials / show tenant info")

    de = sub.add_parser("de", help="data extension operations")
    ds = de.add_subparsers(dest="de_cmd", required=True)
    x = ds.add_parser("list"); x.add_argument("--like")
    x = ds.add_parser("describe"); x.add_argument("key")
    x = ds.add_parser("rows"); x.add_argument("key"); x.add_argument("--limit", type=int, default=25)
    x = ds.add_parser("create"); x.add_argument("spec", help="YAML DE spec")
    x = ds.add_parser("load"); x.add_argument("key"); x.add_argument("csv")
    x = ds.add_parser("delete"); x.add_argument("key")

    x = sub.add_parser("seed", help="generate automotive sample data (and push to DEs)")
    x.add_argument("--scale", choices=["tiny", "small", "full"], default="small")
    x.add_argument("--seed", type=int, default=2026)
    x.add_argument("--push", action="store_true", help="live mode: upload to SFMC")

    x = sub.add_parser("build", help="build every component of a campaign brief")
    x.add_argument("brief")
    x.add_argument("--dry-run", action="store_true")
    x.add_argument("--publish", action="store_true", help="publish (activate) the journey")
    x.add_argument("--skip-audience", action="store_true", help="don't run the audience automation now")

    x = sub.add_parser("analytics", help="campaign KPIs from Data Views")
    x.add_argument("code"); x.add_argument("--no-refresh", action="store_true"); x.add_argument("--json")

    x = sub.add_parser("preview", help="render brief emails to local HTML with a sample contact")
    x.add_argument("brief")

    x = sub.add_parser("status", help="objects + lineage recorded for a campaign")
    x.add_argument("code")

    x = sub.add_parser("teardown", help="delete every object recorded for a campaign")
    x.add_argument("code")

    sub.add_parser("schema", help="master DE fields + example values (for writing audience SQL)")
    sub.add_parser("templates", help="email templates and the blocks each one needs")
    x = sub.add_parser("brief", help="brief tools")
    bs = x.add_subparsers(dest="brief_cmd", required=True)
    y = bs.add_parser("check", help="validate a brief and dry-run its audience SQL in the mock tenant")
    y.add_argument("brief")

    x = sub.add_parser("cloudpage", help="test-drive booking CloudPage tools")
    cs = x.add_subparsers(dest="cp_cmd", required=True)
    y = cs.add_parser("preview", help="render the booking page locally"); y.add_argument("brief")
    y = cs.add_parser("stub", help="print the one line to paste into the CloudPage"); y.add_argument("brief")
    y = cs.add_parser("bookings", help="list submitted bookings"); y.add_argument("code")
    y.add_argument("--limit", type=int, default=50)

    x = sub.add_parser("audit", help="tenant inventory, dependencies, impact and health (read-only)")
    au = x.add_subparsers(dest="audit_cmd", required=True)
    au.add_parser("sync", help="inventory the business unit into the sandbox DB")
    y = au.add_parser("impact", help="what depends on an object (DE, DE.Field, query, email, block…)")
    y.add_argument("object")
    y = au.add_parser("health", help="failing automations, broken references, risky SQL, stale objects")
    y.add_argument("--stale-days", type=int, default=365); y.add_argument("--severity")
    y.add_argument("--limit", type=int, default=100)
    y = au.add_parser("search", help="find text in queries, emails, blocks and pages")
    y.add_argument("text"); y.add_argument("--regex", action="store_true"); y.add_argument("--limit", type=int, default=100)
    y = au.add_parser("report", help="self-contained HTML audit report")
    y.add_argument("--stale-days", type=int, default=365)

    x = sub.add_parser("sql", help="SFMC SQL: lint, explain, run safely")
    sq = x.add_subparsers(dest="sql_cmd", required=True)
    y = sq.add_parser("lint", help="SFMC rules + (live) SFMC's own validator"); y.add_argument("sql")
    y.add_argument("--target", help="target DE to check columns/PK against"); y.add_argument("--no-validate", action="store_true")
    y = sq.add_parser("explain", help="sources, joins, filters, outputs"); y.add_argument("sql")
    y = sq.add_parser("run", help="run a SELECT and show rows (live: temporary scratch DE, cleaned up)")
    y.add_argument("sql"); y.add_argument("--top", type=int, default=50); y.add_argument("--keep", action="store_true")
    y.add_argument("--timeout", type=int, default=900)
    x = sub.add_parser("amp", help="AMPscript / SSJS: lint, render")
    am = x.add_subparsers(dest="amp_cmd", required=True)
    y = am.add_parser("lint", help="lint a file or an email/block/page from the inventory"); y.add_argument("source")
    y.add_argument("--de", help="sendable DE whose fields AttributeValue() should match")
    y = am.add_parser("render", help="render an email for one DE row (live: SFMC server-side preview)")
    y.add_argument("email", help="email customer key"); y.add_argument("--de", required=True)
    y.add_argument("--key", help="SubscriberKey"); y.add_argument("--row", type=int)

    a = ap.parse_args(argv)
    a.mode = getattr(a, "mode", os.environ.get("SFMC_KIT_MODE", "mock"))
    a.verbose = getattr(a, "verbose", False)
    a.yes = getattr(a, "yes", False)
    try:
        be = get_backend(a.mode, a.verbose)
        {"auth-test": cmd_auth_test, "de": cmd_de, "seed": cmd_seed, "build": cmd_build,
         "analytics": cmd_analytics, "preview": cmd_preview, "status": cmd_status,
         "teardown": cmd_teardown, "schema": cmd_schema, "templates": cmd_templates,
         "brief": cmd_brief, "cloudpage": cmd_cloudpage, "audit": cmd_audit, "sql": cmd_sql,
         "amp": cmd_amp}[a.cmd](a, be)
    except (ConfigError, AuthError, BriefError, BuildError, SFMCError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 0
