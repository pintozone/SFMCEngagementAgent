import sqlite3
from pathlib import Path

from sfmc_kit import devtools as dt
from sfmc_kit.audit import AuditStore, Inventory, build_edges


def store(tmp_path):
    inv = Inventory(objects=[
        {"type": "de", "key": "CUST", "id": "1", "name": "Customers", "modified": None, "meta": {}, "body": None},
        {"type": "de", "key": "VEH", "id": "2", "name": "Vehicles", "modified": None, "meta": {}, "body": None},
        {"type": "de", "key": "AUD", "id": "3", "name": "Audience", "modified": None, "meta": {}, "body": None},
    ], fields=[
        {"de_key": "CUST", "name": "SubscriberKey", "type": "Text", "length": "50", "pk": True, "required": True, "ordinal": 0},
        {"de_key": "CUST", "name": "EmailAddress", "type": "EmailAddress", "length": "254", "pk": False, "required": True, "ordinal": 1},
        {"de_key": "CUST", "name": "Notes", "type": "Text", "length": "4000", "pk": False, "required": False, "ordinal": 2},
        {"de_key": "VEH", "name": "SubscriberKey", "type": "Text", "length": "50", "pk": False, "required": False, "ordinal": 0},
        {"de_key": "VEH", "name": "Model", "type": "Text", "length": "50", "pk": False, "required": False, "ordinal": 1},
        {"de_key": "AUD", "name": "SubscriberKey", "type": "Text", "length": "50", "pk": True, "required": True, "ordinal": 0},
        {"de_key": "AUD", "name": "EmailAddress", "type": "EmailAddress", "length": "254", "pk": False, "required": True, "ordinal": 1},
        {"de_key": "AUD", "name": "Notes", "type": "Text", "length": "100", "pk": False, "required": False, "ordinal": 2},
    ])
    st = AuditStore(sqlite3.connect(tmp_path / "s.db"), "1", "live")
    st.save(inv, build_edges(inv))
    return st


def rules(res):
    return {f.rule for f in res.findings}


def test_sql_lint_rules(tmp_path):
    st = store(tmp_path)
    bad = """SELECT c.SubscriberKey, c.EmailAddress, c.Notes, v.Colour, UPPER(v.Model)
             FROM [Customers] c JOIN [Vehicles] v ON v.SubscriberKey = c.SubscriberKey
             JOIN [Nope] n ON 1=1 ORDER BY c.SubscriberKey"""
    r = rules(dt.lint_sql(bad, st, "Audience"))
    assert {"ORDER BY without TOP", "unnamed column", "unknown field", "unknown DE", "column not in target",
            "possible duplicate keys", "may truncate"} <= r
    good = """SELECT x.SubscriberKey, x.EmailAddress FROM (
                SELECT c.SubscriberKey, c.EmailAddress, ROW_NUMBER() OVER (PARTITION BY c.SubscriberKey ORDER BY c.SubscriberKey) AS rn
                FROM [Customers] c JOIN [Vehicles] v ON v.SubscriberKey = c.SubscriberKey) x WHERE x.rn = 1"""
    res = dt.lint_sql(good, st, "Audience")
    assert res.ok, res.rows()
    assert "SELECT * with JOIN" in rules(dt.lint_sql("SELECT * FROM [Customers] c JOIN [Vehicles] v ON 1=1", st))
    assert "not a SELECT" in rules(dt.lint_sql("DELETE FROM [Customers]; SELECT 1 AS x", st))
    assert "data view without date filter" in rules(dt.lint_sql("SELECT s.SubscriberKey FROM _Sent s"))


def test_output_columns_and_explain():
    cols = dt.output_columns("SELECT TOP 5 a.X, b.[Y] AS Why, COUNT(*) AS N, 'lit' Z FROM [T] a")
    assert [c["name"] for c in cols] == ["X", "Why", "N", "Z"]
    ex = dt.explain_sql("SELECT a.X FROM [T] a LEFT JOIN _Open o ON o.SubscriberKey = a.K WHERE a.Y = 1 GROUP BY a.X")
    assert {s["source"] for s in ex["sources"]} == {"T", "_Open"} and ex["joins"][0]["type"] == "LEFT"
    assert ex["filters"].startswith("a.Y = 1") and ex["grouping"].strip() == "a.X"


def test_ampscript_lint(tmp_path):
    st = store(tmp_path)
    code = """%%[ SET @a = Lookup("Customers", "Notes", "SubscriberKey", @sk)
               IF @a = "x" THEN SET @b = Repeat("*", 3) ENDIF
               UpsertData("Customers", 1, "SubscriberKey", @sk, "Colour", "red")
               SET @c = “smart” ]%%
               <a href="https://x.example/?id=%%SubscriberKey%%">x</a> %%=v(@missing)=%% %%=Lookup("Nope","a","b",1)=%%
               <script runat="server">var d = DataExtension.Init("CUST"); let x = 1; d.Rows.Add({a:1});</script>"""
    r = rules(dt.lint_ampscript(code, st))
    assert {"comparison", "unknown function", "unknown field", "smart quotes", "unknown DE", "Platform.Load",
            "ES5 only", "no try/catch", "variable not set here"} <= r
    assert "unbalanced inline" not in r   # ?id=%%SubscriberKey%% is personalization, not a closer
    ok = """%%[ VAR @n SET @n = AttributeValue("FirstName") IF Empty(@n) THEN SET @n = "there" ELSEIF @n == "x" THEN SET @n = "y" ENDIF ]%%
            Hi %%=v(@n)=%% <a href="https://e.example/?u=%%=v(@n)=%%&k=%%_subscriberkey%%">go</a>"""
    assert dt.lint_ampscript(ok, st).ok


def test_kit_templates_lint_clean():
    root = Path(__file__).resolve().parent.parent / "templates"
    for f in [*root.glob("emails/*.html"), *root.glob("cloudpages/*.html")]:
        res = dt.lint_ampscript(f.read_text())
        assert res.ok, (f.name, res.rows())
