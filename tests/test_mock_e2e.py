from pathlib import Path

from sfmc_kit.brief import load_brief
from sfmc_kit.builder import Builder, run_analytics, teardown
from sfmc_kit.client.mock import MockBackend
from sfmc_kit.db import Sandbox
from sfmc_kit.sampledata.generator import generate, master_de_specs

ROOT = Path(__file__).resolve().parent.parent


def test_full_campaign_in_mock(tmp_path):
    be = MockBackend(tmp_path / "mock.db")
    sb = Sandbox(tmp_path / "sandbox.db")
    specs = master_de_specs()
    for key, rows in generate("small").items():
        be.ensure_de(specs[key])
        be.upsert_rows(key, rows, specs[key].pk_fields)

    brief = load_brief(ROOT / "briefs/ev_launch.yaml")
    m = Builder(be, sb, log=lambda s: None).build(brief, publish=True)
    assert m["audience_count"] > 0
    assert m["journey"]["status"] == "Published"
    types = {o["type"] for o in m["objects"]}
    assert {"de", "query", "automation", "email", "eventdef", "journey"} <= types

    # idempotent rebuild: no duplicates, journey already published -> builder refuses to overwrite it
    res = run_analytics(be, sb, "EVLAUNCH")
    s = res["summary"]
    assert s["Sends"] >= m["audience_count"]
    assert 0 < s["UniqueOpens"] < s["Sends"] and 0 < s["UniqueClicks"] < s["UniqueOpens"]
    assert len(res["by_email"]) == 6 and res["daily"]

    n = teardown(be, sb, "EVLAUNCH", log=lambda s: None)
    assert n == len(m["objects"])
    assert be.get_de("CK_EVLAUNCH_Audience") is None
    assert be.db.execute('SELECT COUNT(*) FROM "_Sent"').fetchone()[0] == 0


def test_rebuild_draft_is_idempotent(tmp_path):
    be = MockBackend(tmp_path / "mock.db")
    sb = Sandbox(tmp_path / "sandbox.db")
    specs = master_de_specs()
    for key, rows in generate("tiny").items():
        be.ensure_de(specs[key]); be.upsert_rows(key, rows, specs[key].pk_fields)
    brief = load_brief(ROOT / "briefs/service_reminder.yaml")
    b = Builder(be, sb, log=lambda s: None)
    m1 = b.build(brief)
    m2 = b.build(brief)
    assert len(m1["objects"]) == len(m2["objects"])
    assert be.db.execute("SELECT COUNT(*) FROM mock_objects WHERE type='journey'").fetchone()[0] == 1
