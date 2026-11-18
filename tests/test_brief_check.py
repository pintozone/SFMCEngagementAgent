import copy
from pathlib import Path

import pytest
import yaml

from sfmc_kit.brief import load_yaml
from sfmc_kit.briefcheck import check_brief, list_templates, template_info
from sfmc_kit.client.mock import MockBackend
from sfmc_kit.sampledata.generator import generate, master_de_specs

ROOT = Path(__file__).resolve().parent.parent
BASE = load_yaml((ROOT / "briefs/ev_launch.yaml").read_text())


@pytest.fixture(scope="module")
def mockdb(tmp_path_factory):
    path = tmp_path_factory.mktemp("mock") / "mock.db"
    be = MockBackend(path)
    specs = master_de_specs()
    for key, rows in generate("small").items():
        be.ensure_de(specs[key])
        be.upsert_rows(key, rows, specs[key].pk_fields)
    be.db.close()
    return path


def run(tmp_path, mockdb, mutate=None):
    raw = copy.deepcopy(BASE)
    if mutate:
        mutate(raw)
    p = tmp_path / "brief.yaml"
    p.write_text(yaml.safe_dump(raw, sort_keys=False))
    return check_brief(p, db_path=mockdb)


def test_good_brief_passes(tmp_path, mockdb):
    res = run(tmp_path, mockdb)
    assert res.ok, res.errors
    assert res.audience_count and res.audience_count > 0


def test_missing_block(tmp_path, mockdb):
    res = run(tmp_path, mockdb, lambda r: r["emails"][0]["blocks"].pop("cta_url"))
    assert any("needs blocks ['cta_url']" in e for e in res.errors)


def test_unknown_sql_field(tmp_path, mockdb):
    res = run(tmp_path, mockdb, lambda r: r["audience"].update(
        sql=r["audience"]["sql"].replace("c.LoyaltyTier,", "c.LoyaltyLevel AS LoyaltyTier,", 1)))
    assert any("no field 'LoyaltyLevel'" in e for e in res.errors)


def test_subject_too_long(tmp_path, mockdb):
    res = run(tmp_path, mockdb, lambda r: r["emails"][1].update(subject="A" * 70))
    assert any("subject is ~70 chars" in e for e in res.errors)


def test_variable_not_set_by_template(tmp_path, mockdb):
    res = run(tmp_path, mockdb, lambda r: r["emails"][1].update(subject="%%=v(@LoyaltyTier)=%% members: Volt X"))
    assert any("uses @LoyaltyTier" in e for e in res.errors)


def test_duplicate_pk_from_sql(tmp_path, mockdb):
    def dedupe_removed(r):
        r["audience"]["sql"] = r["audience"]["sql"].replace("WHERE x.rn = 1", "WHERE x.rn >= 1").replace(
            "AND NOT EXISTS (SELECT 1 FROM [CK_Master_Vehicles] ev\n"
            "                    WHERE ev.SubscriberKey = c.SubscriberKey AND ev.Powertrain = 'EV')", "")
        r["audience"]["sql"] = r["audience"]["sql"].replace("v.ModelYear <= 2021", "v.ModelYear <= 2030").replace(
            "v.Powertrain <> 'EV'", "1 = 1")
    res = run(tmp_path, mockdb, dedupe_removed)
    assert any("duplicate primary keys" in e for e in res.errors)


def test_sql_column_not_in_de(tmp_path, mockdb):
    res = run(tmp_path, mockdb, lambda r: r["audience"].update(
        sql=r["audience"]["sql"].replace("x.Region\n", "x.Region, x.rn\n", 1)))
    assert any("columns ['rn']" in e for e in res.errors)


def test_template_catalogue():
    names = {t.name for t in list_templates()}
    assert {"hero_cta.html", "service_reminder.html", "offer_card.html", "newsletter.html"} <= names
    info = template_info("offer_card.html")
    assert {"offer_value", "offer_code", "terms"} <= set(info.blocks)
    assert "FirstName" in info.variables and info.description
