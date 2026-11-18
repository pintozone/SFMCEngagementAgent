"""Proofing workflow: setup (config DEs + form page), answers, generate (proofing DE), verify (render + compare)."""
from __future__ import annotations

import html as htmlmod
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .brief import load_brief
from .client.base import Backend
from .client.rest import SFMCError
from .config import DATA_DIR, PROJECT_ROOT
from .modules import proofing as pf
from .modules.cloudpages import _e
from .modules.data_extensions import DESpec, FieldSpec

VALUES_DE = "CK_Proof_Values"
SETTINGS_DE = "CK_Proof_Settings"
FORM_BLOCK = "CK_PROOF_FormBlock"
FOLDER = "CampaignKit/Proofing"


def values_de_spec() -> DESpec:
    F = FieldSpec
    return DESpec(VALUES_DE, VALUES_DE, folder=FOLDER, description="Proofing: candidate values per email field", fields=[
        F("EmailKey", "Text", 200, pk=True), F("Field", "Text", 100, pk=True), F("Value", "Text", 200, pk=True),
        F("Selected", "Boolean"), F("Source", "Text", 20), F("FieldIdx", "Number"), F("Sort", "Number")])


def settings_de_spec() -> DESpec:
    F = FieldSpec
    return DESpec(SETTINGS_DE, SETTINGS_DE, folder=FOLDER, description="Proofing: per-email settings and status", fields=[
        F("EmailKey", "Text", 200, pk=True), F("Label", "Text", 200), F("Listed", "Text", 1), F("Fields", "Text", 1000),
        F("Blocks", "Text", 4000), F("Inboxes", "Text", 4000), F("Strategy", "Text", 20), F("MaxRows", "Number"),
        F("Status", "Text", 20), F("SubmittedAt", "Date"), F("GeneratedAt", "Date"), F("Rows", "Number"),
        F("ProofDE", "Text", 200)])


@dataclass
class Target:
    email_key: str
    label: str
    analysis: pf.Analysis
    field_types: dict[str, str]
    audience_key: str | None
    proof_de: str
    html: str | None = None              # email HTML for mock rendering
    audience_fields: list[FieldSpec] = field(default_factory=list)

    @property
    def sections(self) -> list[pf.Section]:
        return self.analysis.sections


def resolve_target(be: Backend, ref: str, email_ref: str | None = None, de: str | None = None) -> Target:
    if ref.endswith((".yaml", ".yml")) and Path(ref).exists():
        brief = load_brief(ref)
        emails = [e for e in brief.emails if e.ref == email_ref] if email_ref else \
            ([e for e in brief.emails if e.sections] or brief.emails)
        if not emails:
            raise SFMCError(f"email ref {email_ref!r} not in {ref}")
        e = emails[0]
        an = pf.analysis_from_sections(e.sections) if e.sections else None
        key = f"{brief.prefix}EM_{e.ref}"
        if an is None:   # brief email without sections: parse its AMPscript
            from .modules.assets import render_template
            an = pf.extract_from_ampscript(render_template(e.template, e.blocks, brief.brand))
        proof = f"{brief.prefix}Proofing" if len(brief.emails) == 1 else f"{brief.prefix}Proofing_{e.ref}"
        html = _email_html(be, key)
        return Target(key, f"{brief.name}: {e.name}", an, {f.name: f.type for f in brief.audience_de.fields},
                      brief.audience_de.key, proof, html, list(brief.audience_de.fields))
    # an email by Content Builder customer key
    html = _email_html(be, ref)
    if html is None:
        raise SFMCError(f"email {ref} not found ({be.mode})")
    an = pf.extract_from_ampscript(html)
    ftypes: dict[str, str] = {}
    afields: list[FieldSpec] = []
    if de:
        for f in be.describe_de(de):
            ftypes[f["name"]] = f["type"]
            afields.append(FieldSpec(f["name"], f["type"] if f["type"] in
                                     ("Text", "Number", "Date", "Boolean", "EmailAddress", "Phone", "Decimal", "Locale") else "Text",
                                     int(f["length"]) if f.get("length") and f["type"] in ("Text", "EmailAddress", "Phone") else None,
                                     pk=False, required=False))
    safe = re.sub(r"[^A-Za-z0-9_]", "_", ref)[:80]
    return Target(ref, ref, an, ftypes, de, f"CK_PROOF_{safe}", html, afields)


def _email_html(be: Backend, key: str) -> str | None:
    if be.mode == "live":
        a = be._find_asset(key)  # type: ignore[attr-defined]
        if not a:
            return None
        return ((a.get("views") or {}).get("html") or {}).get("content")
    r = be.db.execute("SELECT payload FROM mock_objects WHERE type='asset' AND key=?", (key,)).fetchone()  # type: ignore[attr-defined]
    return json.loads(r[0])["html"] if r else None


# ---------------------------------------------------------------------------------------------- setup
def _render_form(brand: dict) -> str:
    src = (PROJECT_ROOT / "templates" / "cloudpages" / "proofing_form.html").read_text()
    brand = {"name": "AURORA MOTORS", "primary_color": "#0B3D91", "accent_color": "#00A884", **(brand or {})}
    values = {"values_de": VALUES_DE, "settings_de": SETTINGS_DE,
              **{f"brand_{k}": _e(v) for k, v in brand.items()}}
    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", lambda m: values[m.group(1)], src)


def setup(be: Backend, t: Target, brand: dict, log: Callable[[str], None] = print) -> dict:
    for spec in (values_de_spec(), settings_de_spec()):
        _, created = be.ensure_de(spec)
        if created:
            log(f"  ✓ created DE {spec.key}")
    res = be.upsert_block(FORM_BLOCK, FORM_BLOCK, _render_form(brand), FOLDER)
    log(f"  ✓ form page body {FORM_BLOCK} ({'created' if res['created'] else 'updated'})")
    data = be.read_rows(t.audience_key, max_rows=2500) if t.audience_key and be.get_de(t.audience_key) else []
    cands = pf.candidates(t.analysis, data, t.field_types)
    fields = list(dict.fromkeys(c.field for c in cands))
    existing = {(r["Field"].lower(), str(r["Value"]).lower()) for r in read_values(be, t.email_key)}
    rows = []
    for c in cands:
        if (c.field.lower(), c.value.lower()) in existing:
            continue
        fidx = fields.index(c.field) + 1
        rows.append({"EmailKey": t.email_key, "Field": c.field, "Value": c.value, "Selected": c.selected,
                     "Source": c.source, "FieldIdx": fidx, "Sort": fidx * 1000 + len(rows) + 1})
    if rows:
        be.upsert_rows(VALUES_DE, rows, ["EmailKey", "Field", "Value"])
    s = read_settings(be, t.email_key) or {}
    blocks_txt = "\n".join(t.analysis.conditions) or "(no conditions found)"
    be.upsert_rows(SETTINGS_DE, [{
        "EmailKey": t.email_key, "Label": t.label, "Listed": "Y", "Fields": "|".join(fields),
        "Blocks": blocks_txt[:4000], "Strategy": s.get("Strategy") or "smart", "MaxRows": s.get("MaxRows") or 100,
        "Status": s.get("Status") or "Draft", "ProofDE": t.proof_de}], ["EmailKey"])
    return {"fields": fields, "candidates": len(cands), "new_rows": len(rows), "conditions": t.analysis.conditions}


def read_values(be: Backend, email_key: str) -> list[dict]:
    if not be.get_de(VALUES_DE):
        return []
    rows = [r for r in be.read_rows(VALUES_DE, max_rows=100000) if str(r.get("EmailKey")) == email_key]
    return sorted(rows, key=lambda r: (int(float(r.get("FieldIdx") or 0)), int(float(r.get("Sort") or 0))))


def read_settings(be: Backend, email_key: str) -> dict | None:
    if not be.get_de(SETTINGS_DE):
        return None
    return next((r for r in be.read_rows(SETTINGS_DE, max_rows=10000) if str(r.get("EmailKey")) == email_key), None)


def _true(v: Any) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes", "y")


def answer(be: Backend, email_key: str, inboxes: list[str], strategy: str | None = None, max_rows: int | None = None,
           select: dict[str, list[str]] | None = None) -> None:
    """CLI equivalent of submitting the form (also used by tests and the mock tenant)."""
    vals = read_values(be, email_key)
    if not vals:
        raise SFMCError(f"{email_key} has no proofing setup; run proof setup first")
    if select:
        lower_sel = {f.lower(): {v.lower() for v in vs} for f, vs in select.items()}
        fields = list(dict.fromkeys(r["Field"] for r in vals))
        upd = []
        for f, wanted in lower_sel.items():
            fname = next((x for x in fields if x.lower() == f), None)
            if not fname:
                raise SFMCError(f"field {f} isn't part of {email_key}'s conditions ({', '.join(fields)})")
            have = {str(r["Value"]).lower() for r in vals if r["Field"] == fname}
            for r in vals:
                if r["Field"] == fname:
                    upd.append({**_key(r), "Selected": str(r["Value"]).lower() in wanted})
            fidx = fields.index(fname) + 1
            for i, v in enumerate(sorted(wanted - have)):
                orig = next(x for x in select[next(k for k in select if k.lower() == f)] if x.lower() == v)
                upd.append({"EmailKey": email_key, "Field": fname, "Value": orig, "Selected": True, "Source": "added",
                            "FieldIdx": fidx, "Sort": fidx * 1000 + 500 + i})
        be.upsert_rows(VALUES_DE, upd, ["EmailKey", "Field", "Value"])
    s = read_settings(be, email_key) or {}
    be.upsert_rows(SETTINGS_DE, [{"EmailKey": email_key, "Inboxes": ",".join(inboxes),
                                  "Strategy": strategy or s.get("Strategy") or "smart",
                                  "MaxRows": max_rows or s.get("MaxRows") or 100, "Status": "Submitted",
                                  "SubmittedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}], ["EmailKey"])


def _key(r: dict) -> dict:
    return {"EmailKey": r["EmailKey"], "Field": r["Field"], "Value": r["Value"]}


# ---------------------------------------------------------------------------------------------- generate
def selections(be: Backend, email_key: str) -> tuple[dict, dict[str, list[str]]]:
    s = read_settings(be, email_key)
    if not s:
        raise SFMCError(f"{email_key} has no proofing setup; run proof setup first")
    values: dict[str, list[str]] = {}
    for r in read_values(be, email_key):
        if _true(r.get("Selected")):
            values.setdefault(r["Field"], []).append(str(r["Value"]))
    return s, values


def generate(be: Backend, t: Target, force: bool = False, log: Callable[[str], None] = print) -> dict:
    s, values = selections(be, t.email_key)
    if str(s.get("Status")) not in ("Submitted", "Generated") and not force:
        raise SFMCError(f"{t.email_key}: selections not submitted yet (status {s.get('Status')}). "
                        "Submit the proofing form first, or use --force to use the defaults.")
    inboxes = [x for x in str(s.get("Inboxes") or "").split(",") if x.strip()]
    if not inboxes:
        raise SFMCError("no proof inboxes saved; add them in the form")
    strategy = str(s.get("Strategy") or "smart")
    max_rows = int(float(s.get("MaxRows") or 100))
    combos, notes = pf.generate_rows(values, t.sections, strategy, max_rows)
    cov = pf.coverage_report(combos, values, t.sections)

    base = _base_row(be, t)
    fields = list(t.audience_fields) or [FieldSpec("SubscriberKey", "Text", 50), FieldSpec("EmailAddress", "EmailAddress"),
                                         FieldSpec("FirstName", "Text", 50)]
    names = {f.name.lower() for f in fields}
    for vf in values:
        if vf.lower() not in names:
            fields.append(FieldSpec(vf, "Text", 200))
            names.add(vf.lower())
    for need in ("SubscriberKey", "EmailAddress"):
        if need.lower() not in names:
            fields.insert(0, FieldSpec(need, "EmailAddress" if need == "EmailAddress" else "Text", 254 if need == "EmailAddress" else 50))
    out_fields = []
    for f in fields:
        nf = FieldSpec(f.name, f.type, f.length, pk=f.name.lower() == "subscriberkey",
                       required=f.name.lower() in ("subscriberkey", "emailaddress"), precision=f.precision, scale=f.scale)
        out_fields.append(nf)
    out_fields += [FieldSpec("ProofCase", "Text", 1000), FieldSpec("ExpectedBlocks", "Text", 1000)]
    spec = DESpec(t.proof_de, t.proof_de, out_fields, sendable=True, folder=FOLDER,
                  description=f"Proofing rows for {t.email_key} ({strategy})")
    if be.get_de(t.proof_de):
        be.delete_de(t.proof_de)
    be.create_de(spec)
    ftypes = {f.name.lower(): f.type for f in out_fields}
    rows = []
    for i, combo in enumerate(combos, start=1):
        r = {k: v for k, v in base.items() if k.lower() in ftypes}
        for f, v in combo.items():
            col = next(x.name for x in out_fields if x.name.lower() == f.lower())
            r[col] = _typed(None if v == pf.EMPTY else v, ftypes[f.lower()])
        r["SubscriberKey"] = f"PROOF-{_short(t.email_key)}-{i:03d}"
        r["EmailAddress"] = inboxes[(i - 1) % len(inboxes)].strip()
        if "firstname" in ftypes and "firstname" not in {f.lower() for f in combo}:
            r[next(x.name for x in out_fields if x.name.lower() == "firstname")] = f"Proof {i:03d}"
        r["ProofCase"] = " · ".join(f"{f}={v}" for f, v in combo.items())[:1000]
        r["ExpectedBlocks"] = (", ".join(pf.visible_blocks(t.sections, combo)) if t.sections else "n/a")[:1000]
        rows.append(r)
    be.upsert_rows(t.proof_de, rows, ["SubscriberKey"])
    be.upsert_rows(SETTINGS_DE, [{"EmailKey": t.email_key, "Status": "Generated", "Rows": len(rows),
                                  "GeneratedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "ProofDE": t.proof_de}],
                   ["EmailKey"])
    return {"rows": rows, "notes": notes, "coverage": cov, "strategy": strategy, "inboxes": inboxes,
            "values": values, "proof_de": t.proof_de}


def _short(key: str) -> str:
    m = re.match(r"CK_([A-Z0-9]+)_EM_(\w+)", key)
    return (m.group(1) if m else re.sub(r"[^A-Za-z0-9]", "", key))[:20]


def _typed(v: Any, ftype: str) -> Any:
    if v is None:
        return None
    if ftype == "Boolean":
        return str(v).lower() in ("true", "1", "yes")
    return v


def _base_row(be: Backend, t: Target) -> dict:
    """Plausible values for non-VC fields: the first audience row, else type-based defaults."""
    if t.audience_key and be.get_de(t.audience_key):
        rows = be.read_rows(t.audience_key, max_rows=1)
        if rows:
            return {k: v for k, v in rows[0].items() if v not in (None, "")}
    defaults = {"Text": "Sample", "Number": 1, "Decimal": 1.0, "Date": datetime.now().strftime("%Y-%m-%d"),
                "Boolean": False, "Phone": "555-0100", "Locale": "en-US"}
    return {f.name: defaults.get(f.type) for f in t.audience_fields if f.type in defaults}


# ---------------------------------------------------------------------------------------------- verify
def verify(be: Backend, t: Target, log: Callable[[str], None] = print) -> dict:
    if not be.get_de(t.proof_de):
        raise SFMCError(f"{t.proof_de} doesn't exist; run proof generate first")
    raw = be.read_rows(t.proof_de, max_rows=1000)
    rendered = be.preview_rows(t.email_key, t.proof_de, raw) if be.mode == "live" else {}  # type: ignore[attr-defined]
    results = []
    for pos, r in sorted(enumerate(raw), key=lambda x: str(x[1].get("SubscriberKey"))):
        sk = str(r.get("SubscriberKey"))
        try:
            if be.mode == "live":
                if pos not in rendered:
                    raise SFMCError("no server-side preview could be matched to this row")
                html = rendered[pos]
            else:
                html = pf.mock_render(t.html or "", r)
            actual = pf.rendered_blocks(html)
            err = None
        except Exception as e:  # render failed: record and continue
            actual, html, err = [], "", str(e)[:300]
        expected = [b.strip() for b in str(r.get("ExpectedBlocks") or "").split(",") if b.strip() and b.strip() != "n/a"]
        has_expect = str(r.get("ExpectedBlocks") or "") != "n/a"
        status = "error" if err else ("ok" if (not has_expect or set(actual) == set(expected)) else "mismatch")
        text = re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<style.*?</style>", "", html, flags=re.S))))
        results.append({"SubscriberKey": sk, "ProofCase": r.get("ProofCase"), "expected": expected, "actual": actual,
                        "status": status, "error": err, "snippet": text.strip()[:400]})
        log(f"  {status:<8} {sk}  {r.get('ProofCase')}")
    out = DATA_DIR / "reports"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"proof_{_short(t.email_key)}_{be.mode}.html"
    path.write_text(_report(t, results, be.mode))
    summary = {s: sum(1 for x in results if x["status"] == s) for s in ("ok", "mismatch", "error")}
    return {"results": results, "summary": summary, "report": str(path)}


def _report(t: Target, results: list[dict], mode: str) -> str:
    rows = []
    for r in results:
        color = {"ok": "#15803d", "mismatch": "#b91c1c", "error": "#b45309"}[r["status"]]
        missing = sorted(set(r["expected"]) - set(r["actual"]))
        extra = sorted(set(r["actual"]) - set(r["expected"])) if r["expected"] or r["status"] != "ok" else []
        diff = (f"<div><b>missing:</b> {', '.join(missing)}</div>" if missing else "") + \
               (f"<div><b>unexpected:</b> {', '.join(extra)}</div>" if extra else "") + \
               (f"<div><b>error:</b> {htmlmod.escape(r['error'])}</div>" if r["error"] else "")
        rows.append(f"<tr><td><span style='color:{color};font-weight:700'>{r['status'].upper()}</span></td>"
                    f"<td><code>{htmlmod.escape(r['SubscriberKey'])}</code></td><td>{htmlmod.escape(str(r['ProofCase']))}</td>"
                    f"<td>{htmlmod.escape(', '.join(r['actual']))}{diff}</td>"
                    f"<td class='snip'>{htmlmod.escape(r['snippet'][:220])}</td></tr>")
    ok = sum(1 for r in results if r["status"] == "ok")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Proofing Report</title><style>
:root{{--bg:#f6f7f9;--card:#fff;--ink:#111827;--muted:#6b7280;--line:#e5e7eb}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1115;--card:#171a21;--ink:#e5e7eb;--muted:#9ca3af;--line:#2a2f3a}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif}}
main{{max-width:1200px;margin:0 auto;padding:24px 16px}}h1{{font-size:22px;margin:0}}.muted{{color:var(--muted)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow-x:auto;margin-top:16px}}
table{{border-collapse:collapse;width:100%}}th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{font-size:12px;color:var(--muted)}}.snip{{color:var(--muted);font-size:12px;max-width:360px}}pre{{white-space:pre-wrap;font-size:12px}}
</style></head><body><main>
<h1>Proofing report: {htmlmod.escape(t.label)}</h1>
<p class="muted">{htmlmod.escape(t.email_key)} · proofing DE {htmlmod.escape(t.proof_de)} · {mode} · {datetime.now():%Y-%m-%d %H:%M} ·
<b>{ok}/{len(results)}</b> rows render the expected blocks</p>
<div class="card" style="padding:12px 14px"><b>Visibility conditions</b><pre>{htmlmod.escape(chr(10).join(t.analysis.conditions))}</pre></div>
<div class="card"><table><thead><tr><th>Result</th><th>Row</th><th>Case</th><th>Blocks rendered</th><th>Text</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div></main></body></html>"""
