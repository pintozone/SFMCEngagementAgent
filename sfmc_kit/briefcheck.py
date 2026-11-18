"""`brief check`: catch brief mistakes before anything is built.

Checks schema (via load_brief), template blocks, copy rules, AMPscript/personalization fields,
link aliases, and runs the audience SQL read-only against the mock tenant.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .brief import Brief, BriefError, load_brief
from .builder import plan
from .client.rest import SFMCError
from .client.sqlxlate import translate
from .config import TEMPLATE_DIR
from .db import mock_db_path
from .modules.assets import render_template
from .modules.cloudpages import render_page, resolve_blocks
from .sampledata.generator import master_de_specs

SUBJECT_MAX = 60
PREHEADER_MAX = 100
# Blocks the brief parser fills by default, and system personalization strings SFMC provides.
DEFAULT_BLOCKS = {"preheader_hidden", "footnote", "cta_alias", "hero_alt"}
SYSTEM_STRINGS = {"emailaddr", "member_busname", "member_addr", "member_city", "member_state",
                  "member_postalcode", "member_country", "profile_center_url", "unsub_center_url",
                  "view_email_url", "subscriberkey", "_subscriberkey", "jobid", "_messagecontext"}
AMP_PLACEHOLDER = "X" * 10  # rough rendered width of a personalized value, for length rules


@dataclass
class TemplateInfo:
    name: str
    description: str
    blocks: list[str]             # {{ tokens }} the brief must supply (excludes brand_* and defaults)
    brand_keys: list[str]         # brand_* tokens the brief's campaign.brand must supply
    attributes: list[str]         # AttributeValue("X") fields the template reads (fallbacks exist)
    variables: list[str]          # @vars the template SETs, usable in subject/blocks


def template_info(name: str) -> TemplateInfo:
    path = TEMPLATE_DIR / name
    src = path.read_text()
    tokens = list(dict.fromkeys(re.findall(r"\{\{\s*([\w.]+)\s*\}\}", src)))
    m = re.search(r"<!--\s*template:(.*?)-->", src, re.S)
    desc = " ".join(m.group(1).split()) if m else ""
    return TemplateInfo(
        name=name, description=desc,
        blocks=[t for t in tokens if not t.startswith("brand_") and t not in DEFAULT_BLOCKS],
        brand_keys=[t[len("brand_"):] for t in tokens if t.startswith("brand_")],
        attributes=list(dict.fromkeys(re.findall(r'AttributeValue\(\s*"([^"]+)"\s*\)', src))),
        variables=list(dict.fromkeys(re.findall(r"SET\s+@(\w+)", src, re.I))),
    )


def list_templates() -> list[TemplateInfo]:
    return [template_info(p.name) for p in sorted(TEMPLATE_DIR.glob("*.html"))]


@dataclass
class CheckResult:
    brief: Brief | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    audience_count: int | None = None
    sample_rows: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _visible_len(s: str) -> int:
    s = re.sub(r"%%=.*?=%%", AMP_PLACEHOLDER, s)
    s = re.sub(r"%%\w+%%", AMP_PLACEHOLDER, s)
    return len(s)


def check_brief(path: str | Path, db_path: Path | None = None) -> CheckResult:
    res = CheckResult()
    try:
        brief = load_brief(path)
    except (BriefError, KeyError, TypeError, ValueError) as e:
        res.errors.append(f"brief: {e}")
        return res
    except FileNotFoundError:
        res.errors.append(f"brief: file not found: {path}")
        return res
    res.brief = brief
    de_fields = {f.name.lower(): f.name for f in brief.audience_de.fields}

    for e in brief.emails:
        ctx = f"email '{e.ref}'"
        if not (TEMPLATE_DIR / e.template).exists():
            names = ", ".join(t.name for t in list_templates())
            res.errors.append(f"{ctx}: template {e.template} not found (available: {names})")
            continue
        info = template_info(e.template)
        missing = [b for b in info.blocks if b not in e.blocks]
        if missing:
            res.errors.append(f"{ctx}: template {e.template} needs blocks {missing}")
        unknown = [b for b in e.blocks if b not in info.blocks and b not in DEFAULT_BLOCKS]
        if unknown:
            res.warnings.append(f"{ctx}: blocks {unknown} are not used by {e.template}")
        miss_brand = [k for k in info.brand_keys if k not in brief.brand]
        if miss_brand:
            res.errors.append(f"campaign.brand is missing {miss_brand} (used by {e.template})")
        for attr in info.attributes:
            if attr.lower() not in de_fields:
                res.warnings.append(f"{ctx}: {e.template} reads field '{attr}', which the audience DE "
                                    "doesn't have (the template's fallback text will be used)")

        if _visible_len(e.subject) > SUBJECT_MAX:
            res.errors.append(f"{ctx}: subject is ~{_visible_len(e.subject)} chars; keep it ≤ {SUBJECT_MAX}")
        if _visible_len(e.preheader) > PREHEADER_MAX:
            res.errors.append(f"{ctx}: preheader is ~{_visible_len(e.preheader)} chars; keep it ≤ {PREHEADER_MAX}")
        if not e.preheader:
            res.warnings.append(f"{ctx}: no preheader")

        copy = " ".join([e.subject, e.preheader, *e.blocks.values()])
        for var in re.findall(r"%%=\s*v\(\s*@(\w+)\s*\)\s*=%%", copy, re.I):
            if var.lower() not in {v.lower() for v in info.variables}:
                res.errors.append(f"{ctx}: uses @{var}, but {e.template} only sets "
                                  f"{['@' + v for v in info.variables]}")
        for s in re.findall(r"%%(\w+)%%", copy):
            if s.lower() not in de_fields and s.lower() not in SYSTEM_STRINGS:
                res.errors.append(f"{ctx}: personalization %%{s}%% is not a field of the audience DE")

        if not missing and not miss_brand:
            blocks = resolve_blocks(e.blocks, brief.cloudpage, brief.code)
            try:
                html = render_template(e.template, blocks, brief.brand)
            except KeyError as err:
                res.errors.append(f"{ctx}: {err}")
                continue
            for tag in re.findall(r"<a\s[^>]*>", html):
                href = re.search(r'href="([^"]*)"', tag)
                if href and href.group(1).startswith("http") and "alias=" not in tag:
                    res.warnings.append(f"{ctx}: link {href.group(1)[:60]} has no alias (tracking reads poorly)")
            urls = [u for u in re.findall(r'href="(http[^"]+)"', html)]
            cta = blocks.get("cta_url", "")
            if cta.startswith("http") and cta not in urls and "cta_url" in info.blocks:
                res.warnings.append(f"{ctx}: cta_url does not appear in the rendered email")

    if brief.cloudpage:
        cp = brief.cloudpage
        try:
            render_page(cp, brief.code, brief.prefix, brief.brand,
                        [{"DealerID": "D001", "DealerName": "Sample", "City": "Detroit", "State": "MI"}])
        except KeyError as err:
            res.errors.append(f"cloudpage: {err}")
        users = [e.ref for e in brief.emails if any("{{cloudpage_url}}" in v for v in e.blocks.values())]
        if not users:
            res.warnings.append("cloudpage is configured but no email links to it (use cta_url: \"{{cloudpage_url}}\")")
        if not cp.page_id:
            res.warnings.append(f"cloudpage.page_id not set yet: emails {users} will use {cp.fallback_url} until "
                                "the CloudPage is published and its ID is added to the brief")
        if "DealerID" not in {f.name for f in brief.audience_de.fields}:
            res.warnings.append("audience DE has no DealerID: the booking form can't preselect the customer's dealer")

    _check_audience_sql(brief, res, db_path or mock_db_path())
    return res


def _check_audience_sql(brief: Brief, res: CheckResult, db_path: Path) -> None:
    sql = brief.audience_sql
    if re.search(r"'[^']*'\s*\+|\+\s*'[^']*'", sql):
        res.warnings.append("audience SQL: '+' string concatenation does not run in the mock tenant")
    masters = master_de_specs()
    used = [k for k in masters if k in sql]
    if not used:
        res.warnings.append("audience SQL reads no CK_Master_* DE; make sure every DE it reads exists in SFMC")
    for key in used:
        known = {f.name.lower() for f in masters[key].fields}
        for alias in re.findall(rf"\[{re.escape(key)}\]\s+(?:AS\s+)?(\w+)", sql, re.I):
            for col in set(re.findall(rf"\b{alias}\.(\w+)", sql)):
                if col.lower() not in known:
                    res.errors.append(f"audience SQL: {alias}.{col}: {key} has no field '{col}' "
                                      "(run `python -m sfmc_kit schema`)")
    if not db_path.exists():
        res.warnings.append("audience SQL not executed: no mock tenant yet (run `python -m sfmc_kit seed`)")
        return
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        have = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [k for k in used if k not in have]
        if missing:
            res.warnings.append(f"audience SQL not executed: mock tenant lacks {missing} (run `seed`)")
            return
        try:
            cur = db.execute(translate(sql))
        except sqlite3.Error as e:
            res.errors.append(f"audience SQL failed in the mock tenant: {e}")
            return
        cols = [d[0] for d in cur.description]
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        db.close()

    de = brief.audience_de
    de_names = {f.name.lower(): f for f in de.fields}
    extra = [c for c in cols if c.lower() not in de_names]
    if extra:
        res.errors.append(f"audience SQL returns columns {extra} that the audience DE does not define")
    dup_cols = [c for c in cols if [x.lower() for x in cols].count(c.lower()) > 1]
    if dup_cols:
        res.errors.append(f"audience SQL returns duplicate column names {sorted(set(dup_cols))}")
    lower_cols = {c.lower() for c in cols}
    for f in de.fields:
        if f.required and f.name.lower() not in lower_cols and f.default is None:
            res.errors.append(f"audience DE field '{f.name}' is required but the SQL doesn't return it")
    res.audience_count = len(rows)
    res.sample_rows = rows[:3]
    if rows and de.pk_fields:
        pk = [next(c for c in cols if c.lower() == p.lower()) for p in de.pk_fields if p.lower() in lower_cols]
        if len(pk) == len(de.pk_fields):
            keys = [tuple(r[c] for c in pk) for r in rows]
            dups = len(keys) - len(set(keys))
            if dups:
                res.errors.append(f"audience SQL returns {dups} duplicate primary keys {de.pk_fields}; "
                                  "SFMC would fail the query (dedupe with ROW_NUMBER())")
    email_col = next((c for c in cols if c.lower() == "emailaddress"), None)
    if email_col:
        nulls = sum(1 for r in rows if not r[email_col])
        if nulls:
            res.errors.append(f"{nulls} audience rows have no EmailAddress")
    if res.audience_count == 0:
        res.warnings.append("audience is empty in the mock tenant; check the filters")
    if "emailoptin" not in sql.lower():
        res.warnings.append("audience SQL does not filter on EmailOptIn; commercial sends need consent")


def format_result(res: CheckResult) -> str:
    out = []
    if res.brief:
        b = res.brief
        out.append(f"Brief: {b.name} [{b.code}]  emails={len(b.emails)}  journey='{b.journey_name}'")
        out += [f"  • {s}" for s in plan(b)]
    if res.audience_count is not None:
        out.append(f"Audience (mock tenant): {res.audience_count} contacts")
        for r in res.sample_rows:
            out.append("  sample: " + ", ".join(f"{k}={v}" for k, v in list(r.items())[:6]))
    for w in res.warnings:
        out.append(f"WARN  {w}")
    for e in res.errors:
        out.append(f"ERROR {e}")
    out.append("OK: brief is ready to build" if res.ok else f"FAILED: {len(res.errors)} error(s)")
    return "\n".join(out)
