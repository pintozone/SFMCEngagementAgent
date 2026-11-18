import itertools
from pathlib import Path

import pytest

from sfmc_kit import devtools as dt
from sfmc_kit import proofing_service as ps
from sfmc_kit.brief import BriefError, load_brief, load_yaml, parse_brief
from sfmc_kit.builder import Builder
from sfmc_kit.client.mock import MockBackend
from sfmc_kit.db import Sandbox
from sfmc_kit.modules import proofing as pf
from sfmc_kit.sampledata.generator import generate, master_de_specs

ROOT = Path(__file__).resolve().parent.parent


def test_condition_parse_eval_and_ampscript():
    ast = pf.parse_condition('Model in [Volt X, "Pulse"] and (Tier == Gold or Score >= 50) and Notes is not empty')
    row = {"Model": "pulse", "Tier": "Silver", "Score": "50", "Notes": "x"}
    assert pf.evaluate(ast, row) and set(pf.condition_fields(ast)) == {"Model", "Tier", "Score", "Notes"}
    assert not pf.evaluate(ast, {**row, "Notes": ""})
    amp = pf.to_ampscript(ast)
    assert '@Model == "Volt X"' in amp and "@Score >= 50" in amp and "NOT Empty(@Notes)" in amp
    assert pf.evaluate(pf.parse_condition("Due == true"), {"Due": 1})
    assert pf.evaluate(pf.parse_condition("Due == false"), {"Due": "False"})
    with pytest.raises(pf.ConditionError):
        pf.parse_condition("Model === x")


def test_brief_sections_compile_and_validate():
    b = load_brief(ROOT / "briefs/ownernews.yaml")
    e = b.emails[0]
    assert len(e.sections) == 8 and "<!--blk:ev_tips-->" in e.blocks["sections"]
    assert dt.lint_ampscript(e.blocks["sections"]).ok
    raw = load_yaml((ROOT / "briefs/ownernews.yaml").read_text())
    raw["emails"][0]["sections"][0]["when"] = "Colour == Red"
    with pytest.raises(BriefError, match="Colour"):
        parse_brief(raw)


def test_mock_render_matches_expected_for_all_combinations():
    b = load_brief(ROOT / "briefs/ownernews.yaml")
    e = b.emails[0]
    for m, t, r, s in itertools.product(["Volt X", "Ridge", "Strada", ""], ["Platinum", "Gold", "Silver"],
                                        ["South", "West", "Midwest"], [True, False]):
        row = {"Model": m, "LoyaltyTier": t, "Region": r, "ServiceDue": s}
        assert pf.rendered_blocks(pf.mock_render(e.blocks["sections"], row)) == pf.visible_blocks(e.sections, row), row


def test_extract_from_handwritten_ampscript():
    code = '''%%[ SET @m = AttributeValue("Model") SET @t = [Tier]
             IF @m == "Volt X" AND @t != "Gold" THEN ]%%a%%[ ELSEIF Empty(@m) OR AttributeValue("Score") > 50 THEN ]%%b
             %%[ ELSEIF IndexOf(@t, "Plat") > 0 THEN ]%%c%%[ ENDIF ]%%'''
    an = pf.extract_from_ampscript(code)
    assert ("==", "Volt X") in an.fields["Model"] and ("empty", "") in an.fields["Model"]
    assert ("!=", "Gold") in an.fields["Tier"] and ("contains", "Plat") in an.fields["Tier"]
    assert (">", "50") in an.fields["Score"]
    vals = {c.value for c in pf.candidates(an, [], {"Score": "Number"}) if c.field == "Score"}
    assert {"49", "50", "51"} <= vals


def test_coverage_strategies():
    b = load_brief(ROOT / "briefs/ownernews.yaml")
    secs = b.emails[0].sections
    values = {"Model": ["Volt X", "Pulse", "Ridge", "Strada"], "LoyaltyTier": ["Platinum", "Gold", "Silver"],
              "Region": ["South", "West", "Midwest"], "ServiceDue": ["true", "false"]}
    rows, _ = pf.generate_rows(values, secs, "smart", 200)
    cov = pf.coverage_report(rows, values, secs)
    assert cov["pairs_covered"] == cov["pairs_total"] and not cov["blocks_never_shown"] and not cov["blocks_never_hidden"]
    assert len(rows) < 72 / 3
    full, _ = pf.generate_rows(values, secs, "full", 200)
    assert len(full) == 72
    with pytest.raises(ValueError):
        pf.generate_rows(values, secs, "full", 50)
    pb, _ = pf.generate_rows(values, secs, "per_block", 200)
    cpb = pf.coverage_report(pb, values, secs)
    assert not cpb["blocks_never_shown"] and not cpb["blocks_never_hidden"] and len(pb) <= len(rows)
    # a block that can't be shown with the selected values is reported
    _, notes = pf.generate_rows({"Model": ["Strada"], "Region": ["West"]}, secs, "smart", 50)
    assert any("ev_tips can't be shown" in n for n in notes)


def _tenant(tmp_path):
    be = MockBackend(tmp_path / "mock.db")
    specs = master_de_specs()
    for key, rows in generate("tiny").items():
        be.ensure_de(specs[key]); be.upsert_rows(key, rows, specs[key].pk_fields)
    Builder(be, Sandbox(tmp_path / "sb.db"), log=lambda s: None).build(load_brief(ROOT / "briefs/ownernews.yaml"))
    return be


def test_mock_end_to_end_setup_answer_generate_verify(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "DATA_DIR", tmp_path)
    be = _tenant(tmp_path)
    t = ps.resolve_target(be, str(ROOT / "briefs/ownernews.yaml"))
    r = ps.setup(be, t, {}, log=lambda s: None)
    assert r["fields"] == ["Model", "LoyaltyTier", "Region", "ServiceDue"]
    assert ps.read_settings(be, t.email_key)["Status"] == "Draft"
    with pytest.raises(Exception, match="not submitted"):
        ps.generate(be, t, log=lambda s: None)
    ps.answer(be, t.email_key, ["qa1@example.com", "qa2@example.com"], "smart", 100,
              {"Model": ["Volt X", "Ridge", "Strada", "Summit"], "Region": ["South", "West", "Pacific"]})
    g = ps.generate(be, t, log=lambda s: None)
    assert {r["EmailAddress"] for r in g["rows"]} == {"qa1@example.com", "qa2@example.com"}
    assert "Pacific" in g["values"]["Region"] and "Pulse" not in g["values"]["Model"]
    assert ps.read_settings(be, t.email_key)["Status"] == "Generated"
    v = ps.verify(be, t, log=lambda s: None)
    assert v["summary"] == {"ok": len(g["rows"]), "mismatch": 0, "error": 0}
    assert Path(v["report"]).exists()


def test_verify_catches_a_broken_condition(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "DATA_DIR", tmp_path)
    be = _tenant(tmp_path)
    t = ps.resolve_target(be, str(ROOT / "briefs/ownernews.yaml"))
    ps.setup(be, t, {}, log=lambda s: None)
    ps.answer(be, t.email_key, ["qa@example.com"])
    ps.generate(be, t, log=lambda s: None)
    t.html = t.html.replace('@Region == "South"', '@Region == "Sout"')   # the "bug" in the email
    v = ps.verify(be, t, log=lambda s: None)
    assert v["summary"]["mismatch"] > 0
    assert all("south_event" in set(x["expected"]) - set(x["actual"]) for x in v["results"] if x["status"] == "mismatch")


def test_form_page_renders_and_lints():
    html = ps._render_form({"name": "A", "primary_color": "#000", "accent_color": "#0a0"})
    assert "{{" not in html and 'LookupOrderedRows("CK_Proof_Values"' in html
    assert dt.lint_ampscript(html).ok
