import sqlite3
from pathlib import Path

from sfmc_kit.audit import AuditStore, Inventory, build_edges, code_refs, sql_sources, sync
from sfmc_kit.brief import load_brief
from sfmc_kit.builder import Builder
from sfmc_kit.client.mock import MockBackend
from sfmc_kit.db import Sandbox
from sfmc_kit.sampledata.generator import generate, master_de_specs

ROOT = Path(__file__).resolve().parent.parent


def test_sql_sources():
    sql = """SELECT a.x FROM [My DE] a INNER JOIN ENT.[Shared DE] s ON 1=1
             LEFT JOIN _Sent snt ON 1=1 JOIN Plain p ON 1=1 -- FROM [Commented]
             WHERE EXISTS (SELECT 1 FROM [Inner DE] i)"""
    assert sql_sources(sql) == ["My DE", "Shared DE", "_Sent", "Plain", "Inner DE"]


def test_code_refs():
    body = '''%%[ SET @r = LookupRows("Dealers","Region",@x) UpsertData("Bookings",1,"K",@k) ]%%
              %%=ContentBlockByKey("hdr")=%% %%=RedirectTo(CloudPagesURL(123,'a',1))=%%
              <script runat="server">var de = DataExtension.Init("LogKey"); Platform.Function.InsertData("Log",["a"],[1]);</script>'''
    refs = set(code_refs(body))
    assert {("reads", "de_name", "Dealers"), ("writes", "de_name", "Bookings"), ("includes", "block_key", "hdr"),
            ("links", "cloudpage_id", "123"), ("uses", "de_key", "LogKey"), ("writes", "de_name", "Log")} <= refs


def test_edges_flag_missing_and_data_views():
    inv = Inventory(objects=[
        {"type": "de", "key": "A", "id": "1", "name": "A", "modified": None, "meta": {}, "body": None},
        {"type": "query", "key": "q", "id": "q1", "name": "q", "modified": None,
         "meta": {"target_key": "A"}, "body": "SELECT * FROM [Nope] JOIN _Open o ON 1=1"},
        {"type": "email", "key": "e", "id": "9", "name": "e", "modified": None, "meta": {},
         "body": '%%=ContentBlockByKey("gone")=%% %%=Lookup("A","x","y",1)=%%'},
    ], fields=[])
    edges = {(e["src_key"], e["rel"], e["dst_type"], e["dst_key"]) for e in build_edges(inv)}
    assert ("q", "writes", "de", "A") in edges and ("q", "reads", "missing_de", "Nope") in edges
    assert ("q", "reads", "dataview", "_open") in edges
    assert ("e", "includes", "missing_block", "gone") in edges and ("e", "reads", "de", "A") in edges


def test_mock_sync_impact_and_health(tmp_path):
    be = MockBackend(tmp_path / "mock.db")
    sb = Sandbox(tmp_path / "sb.db")
    specs = master_de_specs()
    for key, rows in generate("tiny").items():
        be.ensure_de(specs[key]); be.upsert_rows(key, rows, specs[key].pk_fields)
    Builder(be, sb, log=lambda s: None).build(load_brief(ROOT / "briefs/ev_launch.yaml"))
    db = sqlite3.connect(tmp_path / "inv.db")
    counts = sync(be, db, "mock", log=lambda s: None)
    assert counts["journey"] == 1 and counts["edges"] > 10
    st = AuditStore(db, "mock", "mock")
    up = st.impact("de", "CK_Master_Customers")["upstream"]
    reached = {(r["t"], r["k"]) for r in up}
    assert ("journey", "CK_EVLAUNCH_JRN") in reached and ("de", "CK_EVLAUNCH_Audience") in reached
    page = st.impact("block", "CK_EVLAUNCH_CP_TestDriveBooking")["downstream"]
    assert {("de", "CK_EVLAUNCH_TestDriveBookings"), ("de", "CK_Master_Dealers")} <= {(r["t"], r["k"]) for r in page}
    usage = st.field_usage(st.resolve("CK_Master_Customers"), "LoyaltyTier")
    assert any(u["key"] == "CK_EVLAUNCH_Q_BuildAudience" for u in usage)
    findings = st.health()
    assert not [f for f in findings if f["severity"] == "error"], findings
    assert any(f["check"] == "unreferenced DE" and f["object"] == "CK_Master_Purchases" for f in findings)
    assert st.search("UpsertData") and st.search(r"CloudPagesURL\(", regex=True) == []
