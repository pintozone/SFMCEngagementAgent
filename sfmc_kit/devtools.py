"""SQL / AMPscript / SSJS developer tools: lint, explain, safe run, live validation and email rendering."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from .audit import DATA_VIEWS, AuditStore, sql_sources

# ------------------------------------------------------------------------------------------------ helpers


@dataclass
class Finding:
    severity: str   # error | warn | info
    rule: str
    message: str
    line: int | None = None


@dataclass
class LintResult:
    findings: list[Finding] = field(default_factory=list)

    def add(self, sev: str, rule: str, msg: str, line: int | None = None) -> None:
        self.findings.append(Finding(sev, rule, msg, line))

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    def rows(self) -> list[dict]:
        order = {"error": 0, "warn": 1, "info": 2}
        return [f.__dict__ for f in sorted(self.findings, key=lambda f: (order[f.severity], f.line or 0))]


def strip_sql_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*|/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), sql, flags=re.S)


def _blank_strings(sql: str) -> str:
    """Replace string literal contents with spaces (keeps positions) so keyword regexes don't match inside them."""
    return re.sub(r"'(?:[^']|'')*'", lambda m: "'" + " " * (len(m.group(0)) - 2) + "'", sql)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _split_top(s: str, sep: str = ",") -> list[str]:
    parts, depth, cur, q = [], 0, [], False
    for ch in s:
        if ch == "'" :
            q = not q
        if not q:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == sep and depth == 0:
                parts.append("".join(cur)); cur = []; continue
        cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _outer_select_list(sql: str) -> str | None:
    """Text between the outermost SELECT and its FROM (depth 0)."""
    s = _blank_strings(strip_sql_comments(sql))
    m = re.search(r"\bSELECT\b", s, re.I)
    if not m:
        return None
    depth, i = 0, m.end()
    while i < len(s):
        ch = s[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and re.match(r"\bFROM\b", s[i:i + 5], re.I) and (i == 0 or not s[i - 1].isalnum()):
            return sql[m.end():i]
        i += 1
    return sql[m.end():]


def output_columns(sql: str) -> list[dict]:
    """Best-effort parse of the outer SELECT list → [{name, expr, alias_src, field}]."""
    sel = _outer_select_list(sql)
    if sel is None:
        return []
    sel = re.sub(r"^\s*(DISTINCT\s+)?(TOP\s+\(?\d+\)?\s+(PERCENT\s+)?)?", "", sel, flags=re.I)
    cols = []
    for item in _split_top(sel):
        m = re.match(r"^(.*?)\s+(?:AS\s+)?(\[[^\]]+\]|[A-Za-z_]\w*)\s*$", item, re.S | re.I)
        expr, name = (m.group(1), m.group(2).strip("[]")) if m and not re.match(r"^[\w.\[\]]+$", item) else (item, None)
        expr = expr.strip()
        src = re.match(r"^(?:(\w+)\.)?\[?(\w+)\]?$", expr)
        if name is None:
            name = src.group(2) if src else None
        cols.append({"name": name, "expr": expr, "alias_src": src.group(1) if src else None,
                     "field": src.group(2) if src else None})
    return cols


def table_aliases(sql: str) -> dict[str, str]:
    """alias → source name for FROM/JOIN items."""
    s = strip_sql_comments(sql)
    out: dict[str, str] = {}
    for m in re.finditer(r"\b(?:FROM|JOIN)\s+((?:ENT\.)?(?:\[[^\]]+\]|[A-Za-z_]\w*))(?:\s+(?:AS\s+)?(?!ON\b|WHERE\b|INNER\b|LEFT\b|RIGHT\b|FULL\b|CROSS\b|JOIN\b|GROUP\b|ORDER\b|WITH\b)([A-Za-z_]\w*))?", s, re.I):
        src = m.group(1).replace("ENT.", "").strip("[]")
        out[(m.group(2) or src).lower()] = src
    return out


# ------------------------------------------------------------------------------------------------ SQL lint
UNSUPPORTED = [
    (r"^\s*(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|CREATE|ALTER|EXEC(UTE)?)\b", "not a SELECT",
     "Query activities only run a single SELECT; the target DE and update type replace INSERT/UPDATE/DELETE"),
    (r"\bDECLARE\s+@|\bSET\s+@\w+\s*=", "variables", "T-SQL variables (DECLARE/SET @x) aren't supported"),
    (r"#\w+", "temp tables", "temp tables (#tmp) aren't supported; stage data in a DE with a separate query"),
    (r"\bINTO\s+[#\[\w]", "SELECT INTO", "SELECT … INTO isn't supported; the query's target DE receives the rows"),
    (r";\s*\S", "multiple statements", "only one statement per query activity"),
    (r"\bWITH\s+\w+\s+AS\s*\(", "CTE", "common table expressions are supported in many tenants but fail in some; "
                                       "prefer a derived table (subquery) if validation complains"),
]


def lint_sql(sql: str, store: AuditStore | None = None, target_key: str | None = None) -> LintResult:
    r = LintResult()
    clean = _blank_strings(strip_sql_comments(sql))
    if not re.search(r"\bSELECT\b", clean, re.I):
        r.add("error", "not a SELECT", "no SELECT found")
        return r
    for pat, rule, msg in UNSUPPORTED:
        m = re.search(pat, clean, re.I | re.M)
        if m:
            r.add("info" if rule == "CTE" else "error", rule, msg, _line_of(clean, m.start()))
    m = re.search(r"\bSELECT\s+(?:DISTINCT\s+)?(?:TOP\s+\(?\d+\)?\s+)?(?:\w+\.)?\*", clean, re.I)
    if m:
        if re.search(r"\bJOIN\b", clean, re.I):
            r.add("error", "SELECT * with JOIN", "SFMC rejects SELECT * in queries that use JOINs; list the columns",
                  _line_of(clean, m.start()))
        else:
            r.add("warn", "SELECT *", "list columns explicitly; SELECT * breaks when a source DE gains or renames fields",
                  _line_of(clean, m.start()))
    # ORDER BY at depth 0 (not inside OVER()) without TOP
    depth = 0
    for i, ch in enumerate(clean):
        depth += ch == "("
        depth -= ch == ")"
        if depth == 0 and re.match(r"ORDER\s+BY\b", clean[i:i + 12], re.I) and (i == 0 or not clean[i - 1].isalnum()):
            if not re.search(r"\bSELECT\s+(DISTINCT\s+)?TOP\b", clean, re.I):
                r.add("error", "ORDER BY without TOP",
                      "ORDER BY is only allowed with TOP in query activities (row order in a DE isn't kept anyway)",
                      _line_of(clean, i))
            break
    cols = output_columns(sql)
    names = [c["name"] for c in cols]
    for c in cols:
        if not c["name"] and not re.fullmatch(r"(\w+\.)?\*", c["expr"].strip()):
            r.add("error", "unnamed column", f"expression `{c['expr'][:50]}` needs an alias (AS FieldName)")
    dup = sorted({n for n in names if n and [x.lower() for x in names if x].count(n.lower()) > 1})
    if dup:
        r.add("error", "duplicate column", f"columns {dup} appear more than once in the SELECT list")
    dvs = [s for s in sql_sources(sql) if s.lower() in DATA_VIEWS]
    event_views = {"_sent", "_open", "_click", "_bounce", "_unsubscribe", "_complaint", "_ftaf",
                   "_smsmessagetracking", "_businessunitunsubscribes"}
    for dv in [d for d in dvs if d.lower() in event_views]:
        if not re.search(r"\bWHERE\b[\s\S]*\b(EventDate|CreatedDate|DeliveredTime|DateUnsubscribed|DateJoined)\b", clean, re.I):
            r.add("warn", "data view without date filter",
                  f"{dv} has ~6 months of history; filter on a date (e.g. EventDate > DATEADD(DAY, -30, GETDATE())) "
                  "to keep the query fast and under the 30-minute limit")
    for m in re.finditer(r"DATEADD\(\s*(DAY|DD|D|MONTH|MM|M|YEAR|YY|YYYY)\s*,\s*-(\d+)", clean, re.I):
        unit = m.group(1).upper()
        days = int(m.group(2)) * (365 if unit.startswith("Y") else 30 if unit.startswith("M") else 1)
        if dvs and days > 180:
            r.add("warn", "data view retention", f"looks back ~{days} days; system data views keep about 6 months",
                  _line_of(clean, m.start()))
    if re.search(r"\bGETDATE\(\)", clean, re.I):
        r.add("info", "server time", "GETDATE() is SFMC server time (US Central Standard Time, UTC-6, no daylight saving)")

    if store:
        aliases = table_aliases(sql)
        de_names = {o["name"].lower(): o for o in store.objects("de")}
        for alias, src in aliases.items():
            if src.lower() in DATA_VIEWS:
                continue
            de = de_names.get(src.lower())
            if not de:
                r.add("error", "unknown DE", f"'{src}' isn't a DE in this BU (shared DEs need the ENT. prefix)")
                continue
            known = {f["name"].lower() for f in store.fields(de["key"])}
            for fm in set(re.findall(rf"\b{re.escape(alias)}\.\[?(\w+)\]?", clean, re.I)):
                if known and fm.lower() not in known:
                    r.add("error", "unknown field", f"{alias}.{fm}: {src} has no field '{fm}'")
        if target_key:
            tde = store.resolve(target_key)
            if not tde or tde["type"] != "de":
                r.add("error", "unknown target", f"target DE '{target_key}' not found")
            else:
                tf = {f["name"].lower(): f for f in store.fields(tde["key"])}
                for n in names:
                    if n and n.lower() not in tf:
                        r.add("error", "column not in target", f"'{n}' isn't a field of {tde['name']}")
                star = any(re.fullmatch(r"(\w+\.)?\*", c["expr"].strip()) for c in cols)
                for f in ([] if star else tf.values()):
                    if (f["required"] or f["pk"]) and f["name"].lower() not in {n.lower() for n in names if n}:
                        r.add("error", "required field missing", f"{tde['name']}.{f['name']} is required/PK but not selected")
                pks = [f["name"] for f in tf.values() if f["pk"]]
                if pks and re.search(r"\bJOIN\b", clean, re.I) and not re.search(r"ROW_NUMBER\s*\(|\bGROUP\s+BY\b|\bDISTINCT\b", clean, re.I):
                    r.add("warn", "possible duplicate keys",
                          f"joins can return several rows per {pks}; dedupe with ROW_NUMBER() OVER (PARTITION BY …) "
                          "or the run fails with a primary key violation")
                for c in cols:
                    if c["field"] and c["alias_src"] and c["name"] and c["name"].lower() in tf:
                        src = aliases.get(c["alias_src"].lower())
                        sde = de_names.get((src or "").lower())
                        if not sde:
                            continue
                        sf = {f["name"].lower(): f for f in store.fields(sde["key"])}.get(c["field"].lower())
                        tfld = tf[c["name"].lower()]
                        try:
                            if sf and sf["length"] and tfld["length"] and int(sf["length"]) > int(tfld["length"]):
                                r.add("warn", "may truncate", f"{c['expr']} is {sf['type']}({sf['length']}) but "
                                      f"{tde['name']}.{tfld['name']} is {tfld['type']}({tfld['length']})")
                        except ValueError:
                            pass
    return r


def explain_sql(sql: str, store: AuditStore | None = None) -> dict:
    clean = strip_sql_comments(sql)
    aliases = table_aliases(sql)
    joins = [{"type": (m.group(1) or "INNER").upper().strip(), "source": m.group(2).replace("ENT.", "").strip("[]"),
              "on": " ".join(m.group(3).split())}
             for m in re.finditer(r"\b(LEFT\s+OUTER|LEFT|RIGHT|FULL|INNER|CROSS)?\s*JOIN\s+((?:ENT\.)?(?:\[[^\]]+\]|\w+))\s+(?:AS\s+)?\w*\s*ON\s+(.+?)(?=\b(?:LEFT|RIGHT|FULL|INNER|CROSS|JOIN|WHERE|GROUP|ORDER|HAVING)\b|\)\s*\w*\s*$|$)",
                                  clean, re.I | re.S)]
    where = re.search(r"\bWHERE\b(.+?)(?=\bGROUP\s+BY\b|\bORDER\s+BY\b|\bHAVING\b|\)\s*\w+\s*WHERE\b|$)", clean, re.I | re.S)
    sources = []
    for alias, src in aliases.items():
        kind = "data view" if src.lower() in DATA_VIEWS else "DE"
        if store and kind == "DE" and not any(o["name"].lower() == src.lower() for o in store.objects("de")):
            kind = "unknown"
        sources.append({"alias": alias, "source": src, "kind": kind})
    return {
        "sources": sources, "joins": joins,
        "filters": " ".join(where.group(1).split()) if where else None,
        "outputs": output_columns(sql),
        "dedupe": bool(re.search(r"ROW_NUMBER\s*\(", clean, re.I)),
        "grouping": " ".join(m.group(1).split()) if (m := re.search(r"\bGROUP\s+BY\b(.+?)(?=\bHAVING\b|\bORDER\b|\)|$)", clean, re.I | re.S)) else None,
        "subqueries": len(re.findall(r"\(\s*SELECT\b", clean, re.I)),
    }


def scratch_name(sql: str) -> str:
    return "CK_Scratch_" + hashlib.sha1(sql.encode()).hexdigest()[:8]


# ------------------------------------------------------------------------------------------------ AMPscript lint
AMP_FUNCTIONS = {f.lower() for f in """
Add Divide Mod Multiply Random Subtract Concat Char Format FormatCurrency FormatNumber IndexOf Length Lowercase
ProperCase RegExMatch Replace ReplaceList StringToDate StringToHex Substring Trim Uppercase BuildRowSetFromString
BuildRowSetFromXML Field Row RowCount Lookup LookupOrderedRows LookupOrderedRowsCS LookupRows LookupRowsCS
InsertData InsertDE UpdateData UpdateDE UpsertData UpsertDE DeleteData DeleteDE ClaimRow ClaimRowValue
DataExtensionRowCount DateAdd DateDiff DateParse DatePart FormatDate Now SystemDateToLocalDate LocalDateToSystemDate
Empty IIf IsEmailAddress IsNull IsNullDefault IsPhoneNumber Base64Decode Base64Encode Decrypt
EncryptSymmetric DecryptSymmetric GUID MD5 SHA1 SHA256 SHA512 AttributeValue Domain V Output OutputLine
TreatAsContent TreatAsContentArea ContentArea ContentAreaByName ContentBlockById ContentBlockByKey ContentBlockByName
ContentImageById ContentImageByKey BeginImpressionRegion EndImpressionRegion GetPortfolioItem Image ImageById
CloudPagesURL MicrositeURL RedirectTo RequestParameter QueryParameter HTTPGet HTTPPost HTTPPost2 HTTPGetWrap
URLEncode WrapLongURL IsCHTMLBrowser HTTPRequestHeader GetSendTime RaiseError AttachFile BarcodeURL
CreateObject SetObjectProperty AddObjectArrayItem InvokeCreate InvokeUpdate InvokeDelete InvokeRetrieve
InvokePerform InvokeExecute InvokeConfigure InvokeExtract CreateSalesforceObject UpdateSingleSalesforceObject
RetrieveSalesforceObjects RetrieveSalesforceJobSources LongSFID ExecuteFilter ExecuteFilterOrderedRows Redirect
ParseJSON BuildOptionList ConvertCSS ConvertTZ ConvertToSystemTime ContentBlockByID FormatString
EndSmsConversation SetSmsConversationNextKeyword CreateSmsConversation UpsertContact UpdateSingleSalesforceObject
TransformXML TreatAsContent IsEmailAddress Unicode GetJWT GetJWTByKeyName AuthenticatedEmployeeID
AuthenticatedEmployeeNotificationAddress AuthenticatedEmployeeUserName AuthenticatedEnterpriseID
AuthenticatedMemberID AuthenticatedMemberName IsDecimal""".split()}
AMP_KEYWORDS = {"if", "elseif", "else", "endif", "then", "for", "to", "downto", "do", "next", "set", "var", "and", "or",
                "not", "output", "outputline", "v"}


def _call_args(code: str, paren: int) -> list[str]:
    """Top-level arguments of the call whose '(' is at index `paren`."""
    depth, q, i = 0, None, paren
    while i < len(code):
        ch = code[i]
        if q:
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return _split_top_q(code[paren + 1:i])
        i += 1
    return []


def _split_top_q(s: str) -> list[str]:
    parts, depth, cur, q = [], 0, [], None
    for ch in s:
        if q:
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip()); cur = []; continue
        cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def _str_lit(a: str) -> str | None:
    m = re.fullmatch(r"\s*([\"'])(.*)\1\s*", a, re.S)
    return m.group(2) if m else None


def _amp_regions(code: str) -> list[tuple[int, str, str]]:
    """(offset, kind, text) for %%[ ]%% blocks, %%= =%% inline expressions and <script language=ampscript>."""
    out = [(m.start(1), "block", m.group(1)) for m in re.finditer(r"%%\[(.*?)\]%%", code, re.S)]
    out += [(m.start(1), "inline", m.group(1)) for m in re.finditer(r"%%=(.*?)=%%(?![A-Za-z_\[=])", code, re.S)]
    out += [(m.start(1), "block", m.group(1)) for m in
            re.finditer(r"<script[^>]*language=[\"']?ampscript[\"']?[^>]*>(.*?)</script>", code, re.S | re.I)]
    return out


def lint_ampscript(code: str, store: AuditStore | None = None, de_key: str | None = None) -> LintResult:
    r = LintResult()
    opens, closes = len(re.findall(r"%%\[", code)), len(re.findall(r"\]%%", code))
    if opens != closes:
        r.add("error", "unbalanced block", f"{opens} '%%[' vs {closes} ']%%'")
    n_open = len(re.findall(r"%%=", code))
    n_close = len(re.findall(r"=%%(?![A-Za-z_\[=])", code))   # "?x=%%SubscriberKey%%" is a URL param + personalization
    if n_open != n_close:
        r.add("error", "unbalanced inline", f"{n_open} '%%=' vs {n_close} '=%%'")
    regions = _amp_regions(code)
    set_vars: set[str] = set()
    all_text = "\n".join(t for _, _, t in regions)
    for m in re.finditer(r"\b(?:SET|VAR)\s+((?:@\w+\s*,?\s*)+)", all_text, re.I):
        set_vars |= {v.lower() for v in re.findall(r"@(\w+)", m.group(1))}
    set_vars |= {v.lower() for v in re.findall(r"\bFOR\s+@(\w+)", all_text, re.I)}
    ifs = endifs = fors = nexts = 0
    for off, kind, text in regions:
        line = _line_of(code, off)
        body = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
        if re.search(r"[“”‘’]", body):
            r.add("error", "smart quotes", "curly quotes inside AMPscript; use straight quotes \" or '", line)
        nostr = re.sub(r"\"[^\"]*\"|'[^']*'", '""', body)
        if nostr.count('"') % 2:
            r.add("error", "unclosed string", "a string literal isn't closed", line)
        ifs += len(re.findall(r"\bIF\b", nostr, re.I))  # \b excludes ELSEIF / ENDIF / IIF
        endifs += len(re.findall(r"\bENDIF\b", nostr, re.I))
        fors += len(re.findall(r"\bFOR\s+@", nostr, re.I))
        nexts += len(re.findall(r"\bNEXT\b", nostr, re.I))
        for m in re.finditer(r"\b(?:IF|ELSEIF)\b(.*?)\bTHEN\b", nostr, re.I | re.S):
            if re.search(r"(?<![=!<>])=(?!=)", m.group(1)):
                r.add("error", "comparison", "use == to compare in IF conditions (single = is assignment)", line)
        for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", nostr):
            fn = m.group(1)
            if fn.lower() not in AMP_FUNCTIONS and fn.lower() not in AMP_KEYWORDS:
                r.add("error", "unknown function", f"{fn}() isn't an AMPscript function", line)
        for v in set(re.findall(r"@(\w+)", nostr)):
            if v.lower() not in set_vars:
                where = "an included content block or the template" if re.search(r"ContentBlockBy|ContentArea", code, re.I) \
                    else "a template or block this code is placed in"
                r.add("info", "variable not set here", f"@{v} is used but not SET in this code (it may come from {where})", line)
    if ifs != endifs:
        r.add("error", "IF/ENDIF", f"{ifs} IF vs {endifs} ENDIF")
    if fors != nexts:
        r.add("error", "FOR/NEXT", f"{fors} FOR vs {nexts} NEXT")

    # SSJS
    for m in re.finditer(r"<script[^>]*runat=[\"']?server[\"']?[^>]*>(.*?)</script>", code, re.S | re.I):
        if re.search(r"language=[\"']?ampscript", m.group(0), re.I):
            continue
        js, line = m.group(1), _line_of(code, m.start())
        if re.search(r"\b(DataExtension|Subscriber|Email|TriggeredSend|List|Folder|Send)\.(Init|Retrieve|Add)", js) and \
                "Platform.Load" not in js:
            r.add("error", "Platform.Load", 'Core library objects need Platform.Load("Core", "1.1.1") first', line)
        if re.search(r"\.Rows\.(Add|Update|Remove)|Platform\.Function\.(Insert|Upsert|Update|Delete)Data|HTTP\.Post", js) and \
                not re.search(r"\btry\s*\{", js):
            r.add("warn", "no try/catch", "writes/HTTP calls without try/catch fail the whole page or email", line)
        if re.search(r"\b(let|const)\s|=>|`", js):
            r.add("error", "ES5 only", "SSJS is ECMAScript 3/5: no let/const, arrow functions or template literals", line)

    # references
    if store:
        de_names = {o["name"].lower(): o for o in store.objects("de")}
        for m in re.finditer(r"\b(Lookup|LookupRows|LookupOrderedRows|LookupRowsCS|LookupOrderedRowsCS|InsertData|InsertDE|"
                             r"UpsertData|UpsertDE|UpdateData|UpdateDE|DeleteData|DeleteDE)\s*\(", code, re.I):
            args = _call_args(code, m.end() - 1)
            if not args:
                continue
            name = _str_lit(args[0])
            if name is None or "{{" in name:   # variable DE name, or a build-time template placeholder
                continue
            de = de_names.get(name.lower())
            line = _line_of(code, m.start())
            fn = m.group(1).lower()
            if not de:
                r.add("error", "unknown DE", f"{m.group(1)}(\"{name}\"): no such DE in this BU", line)
                continue
            known = {f["name"].lower() for f in store.fields(de["key"])}
            if fn == "lookup":
                pos = [1] + list(range(2, len(args), 2))
            elif fn in ("lookuporderedrows", "lookuporderedrowscs"):
                pos = list(range(3, len(args), 2))
                order = _str_lit(args[2]) if len(args) > 2 else None
                if order and order.split()[0].lower() not in known:
                    r.add("warn", "unknown field", f"{name}: order-by field '{order.split()[0]}' doesn't exist", line)
            elif fn in ("upsertdata", "upsertde", "updatedata", "updatede"):
                pos = list(range(2, len(args), 2))
            else:
                pos = list(range(1, len(args), 2))
            for i in pos:
                f = _str_lit(args[i]) if i < len(args) else None
                if f and known and f.lower() not in known:
                    r.add("error", "unknown field", f"{m.group(1)}: {name} has no field '{f}'", line)
        if de_key:
            de = store.resolve(de_key)
            known = {f["name"].lower() for f in store.fields(de["key"])} if de else set()
            for f in set(re.findall(r"AttributeValue\(\s*[\"']([^\"']+)[\"']", code, re.I)):
                if known and f.lower() not in known:
                    r.add("warn", "attribute not in DE", f"AttributeValue(\"{f}\") isn't a field of {de['name']}")
    return r


def run_select_live(be: Any, sql: str, store: AuditStore | None = None, top: int = 50, timeout: int = 900,
                    keep: bool = False, log: Any = print) -> list[dict]:
    """Run a SELECT in the live tenant through a temporary DE + query + automation, then clean up.
    The caller is responsible for asking the user first (this writes to the tenant temporarily)."""
    from .client.rest import SFMCError
    from .modules.automation import AutomationSpec, QuerySpec
    from .modules.data_extensions import DESpec, FieldSpec
    cols = output_columns(sql)
    if any(re.fullmatch(r"(\w+\.)?\*", c["expr"].strip()) for c in cols):
        raise SFMCError("list the columns explicitly (SELECT * can't be typed into a scratch DE)")
    aliases = table_aliases(sql)
    fields = []
    for c in cols:
        ftype = "Text"
        if store and c["field"] and c["alias_src"]:
            src = store.resolve(aliases.get(c["alias_src"].lower(), ""))
            sf = next((f for f in store.fields(src["key"]) if f["name"].lower() == c["field"].lower()), None) if src else None
            if sf and sf["type"] in ("Number", "Date", "Boolean", "Decimal"):
                ftype = sf["type"]
        fields.append(FieldSpec(c["name"], "Text", 4000) if ftype == "Text" else FieldSpec(c["name"], ftype))
    name = scratch_name(sql)
    collist = ", ".join(f"[{c['name']}]" for c in cols)
    wrapped = sql if re.search(r"\bSELECT\s+(DISTINCT\s+)?TOP\b", sql, re.I) else \
        f"SELECT TOP {top} {collist} FROM (\n{sql}\n) AS _q"   # SFMC rejects SELECT * when JOINs are present
    spec = DESpec(name, name, fields, folder="CampaignKit/Scratch", description="sfmc_kit scratch query (temporary)")
    created: list[tuple[str, str]] = []
    try:
        be.ensure_de(spec); created.append(("de", name))
        q = be.upsert_query(QuerySpec(name, name, wrapped, name, name)); created.append(("query", q["id"]))
        au = be.upsert_automation(AutomationSpec(name, name, [[name]]), {name: q["id"]}); created.append(("automation", au["id"]))
        status = be.run_automation(au["id"], timeout=timeout)
        log(f"  automation finished: {status}")
        return be.read_rows(name, max_rows=top)
    finally:
        if keep:
            log(f"  kept scratch objects {name} (--keep)")
        else:
            for kind, ident in reversed(created):
                try:
                    {"automation": be.delete_automation, "query": be.delete_query, "de": be.delete_de}[kind](ident)
                except Exception as e:  # report but keep cleaning
                    log(f"  ! could not delete {kind} {ident}: {e}")
            log(f"  cleaned up scratch objects {name}")
