"""Translate the SFMC (T-SQL subset) used by the kit into SQLite for the mock tenant.

Supported: [bracketed names], GETDATE(), DATEADD(unit, n, expr), DATEDIFF(unit, a, b),
CONVERT(DATE, x), CAST(x AS DATE), ISNULL(), LEN(), TOP n, ROW_NUMBER() OVER (...).
Keep campaign SQL inside this subset so the same text runs in both modes.
"""
from __future__ import annotations

import re

_UNITS = {"day": "days", "dd": "days", "d": "days", "hour": "hours", "hh": "hours", "minute": "minutes",
          "mi": "minutes", "n": "minutes", "month": "months", "mm": "months", "m": "months", "year": "years",
          "yy": "years", "yyyy": "years", "week": "days", "wk": "days", "ww": "days"}
_JULIAN = {"days": 1, "hours": 24, "minutes": 1440}

_ARG = r"(__NOW__|'[^']*'|[\w\".@]+)"


def translate(sql: str) -> str:
    s = sql
    s = re.sub(r"\[([^\]]+)\]", lambda m: '"' + m.group(1) + '"', s)
    s = re.sub(r"GETDATE\(\s*\)", "__NOW__", s, flags=re.I)

    def dateadd(m: re.Match) -> str:
        unit = m.group(1).lower()
        n = int(m.group(2))
        u = _UNITS[unit]
        if unit in ("week", "wk", "ww"):
            n *= 7
        return f"datetime({m.group(3)}, '{n:+d} {u}')"

    s = re.sub(rf"DATEADD\(\s*(\w+)\s*,\s*(-?\d+)\s*,\s*{_ARG}\s*\)", dateadd, s, flags=re.I)

    def datediff(m: re.Match) -> str:
        u = _UNITS[m.group(1).lower()]
        mult = _JULIAN.get(u, 1)
        return f"CAST((julianday({m.group(3)}) - julianday({m.group(2)})) * {mult} AS INTEGER)"

    s = re.sub(rf"DATEDIFF\(\s*(\w+)\s*,\s*{_ARG}\s*,\s*{_ARG}\s*\)", datediff, s, flags=re.I)
    s = re.sub(rf"CONVERT\(\s*DATE\s*,\s*{_ARG}\s*\)", r"date(\1)", s, flags=re.I)
    s = re.sub(rf"CAST\(\s*{_ARG}\s+AS\s+DATE\s*\)", r"date(\1)", s, flags=re.I)
    s = re.sub(r"\bISNULL\(", "IFNULL(", s, flags=re.I)
    s = re.sub(r"\bLEN\(", "length(", s, flags=re.I)
    s = s.replace("__NOW__", "datetime('now')")
    top = re.match(r"\s*SELECT\s+TOP\s+(\d+)\s+", s, flags=re.I)
    if top:
        s = "SELECT " + s[top.end():] + f" LIMIT {top.group(1)}"
    return s
