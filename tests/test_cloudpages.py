import re
from pathlib import Path

import pytest

from sfmc_kit.brief import BriefError, load_brief, parse_brief, load_yaml
from sfmc_kit.builder import Builder, run_analytics, teardown
from sfmc_kit.client.mock import MockBackend
from sfmc_kit.db import Sandbox
from sfmc_kit.modules import cloudpages as cpm
from sfmc_kit.sampledata.generator import generate, master_de_specs

ROOT = Path(__file__).resolve().parent.parent
DEALERS = [{"DealerID": "D001", "DealerName": "Detroit Aurora", "City": "Detroit", "State": "MI"},
           {"DealerID": "D002", "DealerName": "A <b>& Co", "City": "Chicago", "State": "IL"}]


def test_page_renders_with_upsert_and_escaping():
    b = load_brief(ROOT / "briefs/ev_launch.yaml")
    html = cpm.render_page(b.cloudpage, b.code, b.prefix, b.brand, DEALERS)
    assert "{{" not in html
    assert 'UpsertData("CK_EVLAUNCH_TestDriveBookings", 2, "SubscriberKey", @sk, "Model", @model' in html
    spec_fields = {f.name for f in cpm.bookings_de(b.prefix, b.code).fields}
    written = set(re.findall(r'"(\w+)", ', html[html.index("UpsertData("):html.index("SET @done")]))
    assert written - {"CK_EVLAUNCH_TestDriveBookings"} <= spec_fields, written - spec_fields
    assert "A &lt;b&gt;&amp; Co" in html                       # dealer names are HTML-escaped
    assert 'IndexOf("|Volt X|Pulse|"' in html                 # model whitelist
    assert html.count("%%[") == html.count("]%%")


def test_cta_link_fallback_then_cloudpagesurl():
    cp = cpm.CloudPageBrief(models=["Volt X"])
    assert cpm.resolve_blocks({"cta_url": "{{cloudpage_url}}"}, cp, "X")["cta_url"] == cp.fallback_url
    cp.page_id = 4321
    link = cpm.resolve_blocks({"cta_url": "{{cloudpage_url}}"}, cp, "EVX")["cta_url"]
    assert link.startswith("%%=RedirectTo(CloudPagesURL(4321,") and '"' not in link and "'Campaign', 'EVX'" in link


def test_brief_requires_cloudpage_section_for_token():
    raw = load_yaml((ROOT / "briefs/ev_launch.yaml").read_text())
    raw.pop("cloudpage")
    with pytest.raises(BriefError, match="no cloudpage section"):
        parse_brief(raw)
    raw2 = load_yaml((ROOT / "briefs/ev_launch.yaml").read_text())
    raw2["cloudpage"]["default_model"] = "Ridge"
    with pytest.raises(BriefError):
        parse_brief(raw2)


def test_preview_is_static_and_prefilled():
    b = load_brief(ROOT / "briefs/ev_launch.yaml")
    html = cpm.render_page(b.cloudpage, b.code, b.prefix, b.brand, DEALERS)
    pv = cpm.preview_page(html, {"FirstName": "Ana", "DealerID": "D002", "Model": "Pulse"})
    assert "%%" not in pv and 'value="Ana"' in pv
    assert re.search(r'value="D002"\s+selected', pv) and re.search(r'value="Pulse"\s+checked', pv)


def test_mock_build_creates_page_and_bookings(tmp_path):
    be = MockBackend(tmp_path / "mock.db")
    sb = Sandbox(tmp_path / "sandbox.db")
    specs = master_de_specs()
    for key, rows in generate("small").items():
        be.ensure_de(specs[key]); be.upsert_rows(key, rows, specs[key].pk_fields)
    brief = load_brief(ROOT / "briefs/ev_launch.yaml")
    m = Builder(be, sb, log=lambda s: None).build(brief, publish=True)
    types = {(o["type"], o["key"]) for o in m["objects"]}
    assert ("block", "CK_EVLAUNCH_CP_TestDriveBooking") in types
    assert ("de", "CK_EVLAUNCH_TestDriveBookings") in types
    res = run_analytics(be, sb, "EVLAUNCH")
    assert 0 < res["summary"]["Bookings"] <= res["summary"]["UniqueClicks"]
    assert all(r["Campaign"] == "EVLAUNCH" and r["Model"] in ("Volt X", "Pulse") for r in res["bookings"])
    teardown(be, sb, "EVLAUNCH", log=lambda s: None)
    assert be.get_de("CK_EVLAUNCH_TestDriveBookings") is None
    assert be.db.execute("SELECT COUNT(*) FROM mock_objects WHERE type='block'").fetchone()[0] == 0
