import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from sfmc_kit.brief import BriefError, load_brief, parse_brief
from sfmc_kit.client.sqlxlate import translate
from sfmc_kit.modules.data_extensions import DESpec, FieldSpec
from sfmc_kit.modules.journey import EntrySpec, JourneyBuilder, journey_payload
from sfmc_kit.sampledata.generator import generate

ROOT = Path(__file__).resolve().parent.parent


def test_sqlxlate():
    s = translate("SELECT TOP 5 [a b].x FROM [My DE] WHERE d < DATEADD(MONTH, -7, GETDATE()) AND ISNULL(y,0)=0")
    assert '"My DE"' in s and "datetime(datetime('now'), '-7 months')" in s and "IFNULL(" in s and s.endswith("LIMIT 5")
    assert "date(s.EventDate)" in translate("SELECT CONVERT(DATE, s.EventDate) FROM _Sent s")
    assert "julianday" in translate("SELECT DATEDIFF(DAY, a.x, GETDATE())")


def test_de_soap_xml_is_wellformed():
    spec = DESpec("K", "K & Co", [FieldSpec("SubscriberKey", pk=True), FieldSpec("Amt", "Decimal"),
                                   FieldSpec("EmailAddress", "EmailAddress")], sendable=True)
    xml = spec.soap_xml(123)
    root = ET.fromstring(xml.replace('xsi:type="DataExtension"', ""))
    assert root.find("CategoryID").text == "123"
    assert root.find("SendableSubscriberField/Name").text == "Subscriber Key"
    assert len(root.findall("Fields/Field")) == 3
    assert "K &amp; Co" in xml


def test_bad_field_type():
    with pytest.raises(ValueError):
        FieldSpec("x", "Integer")


def test_briefs_parse_and_yes_no_keys():
    b = load_brief(ROOT / "briefs/ev_launch.yaml")
    d = b.steps[2]["decision"]
    assert d["yes"] and d["no"], "YAML yes/no keys must survive parsing"
    nested = d["yes"][2]["decision"]
    assert nested["on"] == "clicked", "YAML `on:` key must not become a boolean"
    assert b.audience_de.fields[0].pk is True
    assert b.prefix == "CK_EVLAUNCH_" and b.audience_de.key == "CK_EVLAUNCH_Audience"
    load_brief(ROOT / "briefs/service_reminder.yaml")


def test_brief_rejects_decision_before_email():
    raw = {"campaign": {"name": "n", "code": "ABC"},
           "audience": {"sql": "SELECT 1", "de": {"fields": [{"name": "SubscriberKey", "pk": True},
                                                              {"name": "EmailAddress", "type": "EmailAddress"}]}},
           "emails": [{"ref": "a", "subject": "s", "template": "hero_cta.html"}],
           "journey": {"steps": [{"decision": {"email": "a"}}]}}
    with pytest.raises(BriefError):
        parse_brief(raw)


def test_journey_graph_is_connected():
    b = load_brief(ROOT / "briefs/ev_launch.yaml")
    emails = {e.ref: {"legacy_id": i + 1, "subject": e.subject, "preheader": "", "name": e.name, "key": e.ref}
              for i, e in enumerate(b.emails)}
    jb = JourneyBuilder(emails, {})
    first = jb.build(b.steps)
    p = journey_payload("K", "N", "", EntrySpec("data_extension", "DE", "EV", "EV"), "evid", jb.activities, first)
    keys = {a["key"] for a in p["activities"]}
    assert first in keys and len(keys) == len(p["activities"]) == 12
    seen, stack = set(), [first]
    while stack:
        k = stack.pop()
        seen.add(k)
        a = next(a for a in p["activities"] if a["key"] == k)
        stack += [o["next"] for o in a["outcomes"] if o.get("next")]
    assert seen == keys
    dec = [a for a in p["activities"] if a["type"] == "ENGAGEMENTDECISION"]
    assert all(d["configurationArguments"]["refActivityCustomerKey"].startswith("EMAIL-") for d in dec)


def test_generator_is_deterministic_and_safe():
    a, b = generate("tiny", seed=7), generate("tiny", seed=7)
    assert a == b
    custs = a["CK_Master_Customers"]
    assert all(c["EmailAddress"].split("@")[1] in ("example.com", "example.net", "example.org") for c in custs)
    assert len({c["EmailAddress"] for c in custs}) == len(custs)
    vins = {v["VIN"] for v in a["CK_Master_Vehicles"]}
    assert all(len(v) == 17 for v in vins)


def test_journey_description_is_sanitized():
    p = journey_payload("K", "N", "Owner's update & <stuff> / \"x\"", EntrySpec("data_extension", "DE", "EV", "EV"), "e", [], None)
    assert p["description"] == "Owners update  stuff  x"
