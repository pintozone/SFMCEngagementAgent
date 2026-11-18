"""Command line: python -m sfmc_kit <command> ..."""
from __future__ import annotations

import argparse
import csv
import json
import os
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


def table(rows: list[dict], cols: list[str] | None = None, limit: int = 50) -> None:
    if not rows:
        print("  (no rows)")
        return
    cols = cols or list(rows[0].keys())
    rows = rows[:limit]
    w = {c: min(40, max(len(c), *(len(str(r.get(c, ""))) for r in rows))) for c in cols}
    print("  " + "  ".join(c.ljust(w[c]) for c in cols))
    print("  " + "  ".join("-" * w[c] for c in cols))
    for r in rows:
        print("  " + "  ".join(str(r.get(c, "") if r.get(c) is not None else "")[:40].ljust(w[c]) for c in cols))


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
        html = preview(render_template(e.template, e.blocks, brief.brand), row)
        p = out / f"{brief.code}_{e.ref}.html"
        p.write_text(html)
        print(f"  {p}  ({e.subject})")


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

    a = ap.parse_args(argv)
    a.mode = getattr(a, "mode", os.environ.get("SFMC_KIT_MODE", "mock"))
    a.verbose = getattr(a, "verbose", False)
    a.yes = getattr(a, "yes", False)
    try:
        be = get_backend(a.mode, a.verbose)
        {"auth-test": cmd_auth_test, "de": cmd_de, "seed": cmd_seed, "build": cmd_build,
         "analytics": cmd_analytics, "preview": cmd_preview, "status": cmd_status,
         "teardown": cmd_teardown}[a.cmd](a, be)
    except (ConfigError, AuthError, BriefError, BuildError, SFMCError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 0
