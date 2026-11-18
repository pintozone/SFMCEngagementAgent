"""Proofing for emails with visibility conditions (VCs).

* A tiny condition language for brief `sections:` (compiled to AMPscript IF blocks with markers).
* Extraction of fields/literals from arbitrary AMPscript IF conditions (best effort).
* Candidate values per field, coverage generation (smart = pairwise + every block shown/hidden, full, per_block).
* A mini AMPscript evaluator for the mock tenant, and marker-based block detection for verification.
"""
from __future__ import annotations

import html as htmlmod
import itertools
import random
import re
from dataclasses import dataclass, field
from typing import Any

EMPTY = "(empty)"
MARK_RE = re.compile(r"<!--blk:([\w-]+)-->")

# ============================================================================================ condition DSL
# expr := or ; or := and (OR and)* ; and := unary (AND unary)* ; unary := NOT unary | '(' expr ')' | test
# test := F (==|!=|>|>=|<|<=) value | F [not] in [a, b] | F is [not] empty


class ConditionError(ValueError):
    pass


_TOK = re.compile(r"""\s*(?:(?P<str>"[^"]*"|'[^']*')|(?P<op>==|!=|>=|<=|>|<|\(|\)|\[|\]|,)|(?P<word>[^\s()\[\],=!<>"']+))""")


def _tokens(s: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    s = s.strip()
    while pos < len(s):
        m = _TOK.match(s, pos)
        if not m or m.end() == pos:
            raise ConditionError(f"can't read condition near: {s[pos:pos + 20]!r}")
        pos = m.end()
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
    return out


class _P:
    def __init__(self, text: str):
        self.text = text
        self.t = _tokens(text)
        self.i = 0

    def peek(self, k: int = 0) -> tuple[str, str] | None:
        return self.t[self.i + k] if self.i + k < len(self.t) else None

    def kw(self, word: str, k: int = 0) -> bool:
        p = self.peek(k)
        return bool(p and p[0] == "word" and p[1].lower() == word)

    def take(self) -> tuple[str, str]:
        p = self.peek()
        if p is None:
            raise ConditionError(f"condition ends early: {self.text!r}")
        self.i += 1
        return p

    def expect(self, val: str) -> None:
        p = self.take()
        if p[1] != val:
            raise ConditionError(f"expected {val!r} but found {p[1]!r} in {self.text!r}")

    def parse(self) -> tuple:
        e = self.or_()
        if self.peek() is not None:
            raise ConditionError(f"unexpected {self.peek()[1]!r} in {self.text!r}")
        return e

    def or_(self) -> tuple:
        items = [self.and_()]
        while self.kw("or"):
            self.take(); items.append(self.and_())
        return items[0] if len(items) == 1 else ("or", items)

    def and_(self) -> tuple:
        items = [self.unary()]
        while self.kw("and"):
            self.take(); items.append(self.unary())
        return items[0] if len(items) == 1 else ("and", items)

    def unary(self) -> tuple:
        if self.kw("not") and not (self.peek(1) and self.peek(1)[1] in ("in",)):
            self.take(); return ("not", self.unary())
        if self.peek() and self.peek()[1] == "(":
            self.take(); e = self.or_(); self.expect(")"); return e
        return self.test()

    def value(self) -> str:
        p = self.take()
        if p[0] == "str":
            return p[1][1:-1]
        if p[0] != "word":
            raise ConditionError(f"expected a value, found {p[1]!r} in {self.text!r}")
        words = [p[1]]
        while self.peek() and self.peek()[0] == "word" and self.peek()[1].lower() not in ("and", "or"):
            words.append(self.take()[1])
        return " ".join(words)

    def test(self) -> tuple:
        f = self.take()
        if f[0] != "word" or not re.fullmatch(r"[A-Za-z_]\w*", f[1]):
            raise ConditionError(f"expected a field name, found {f[1]!r} in {self.text!r}")
        name = f[1]
        if self.kw("is"):
            self.take()
            neg = False
            if self.kw("not"):
                self.take(); neg = True
            if not self.kw("empty"):
                raise ConditionError(f"expected 'empty' after 'is' in {self.text!r}")
            self.take()
            return ("empty", name, neg)
        neg = False
        if self.kw("not") and self.kw("in", 1):
            self.take(); neg = True
        if self.kw("in"):
            self.take(); self.expect("[")
            vals = []
            while True:
                vals.append(self.value())
                p = self.take()
                if p[1] == "]":
                    break
                if p[1] != ",":
                    raise ConditionError(f"expected , or ] in list in {self.text!r}")
            return ("in", name, vals, neg)
        op = self.take()
        if op[1] not in ("==", "!=", ">", ">=", "<", "<="):
            raise ConditionError(f"unknown operator {op[1]!r} in {self.text!r}")
        return ("cmp", name, op[1], self.value())


def parse_condition(text: str) -> tuple:
    return _P(str(text)).parse()


def condition_fields(ast: tuple) -> list[str]:
    if ast[0] in ("or", "and"):
        return list(dict.fromkeys(f for a in ast[1] for f in condition_fields(a)))
    if ast[0] == "not":
        return condition_fields(ast[1])
    return [ast[1]]


def condition_literals(ast: tuple) -> dict[str, list[tuple[str, str]]]:
    """field -> [(op, literal)] used in the condition."""
    out: dict[str, list[tuple[str, str]]] = {}
    def walk(a: tuple) -> None:
        if a[0] in ("or", "and"):
            for x in a[1]:
                walk(x)
        elif a[0] == "not":
            walk(a[1])
        elif a[0] == "cmp":
            out.setdefault(a[1], []).append((a[2], a[3]))
        elif a[0] == "in":
            for v in a[2]:
                out.setdefault(a[1], []).append(("==", v))
        elif a[0] == "empty":
            out.setdefault(a[1], []).append(("empty", ""))
    walk(ast)
    return out


def _is_bool_lit(v: str) -> bool:
    return str(v).lower() in ("true", "false")


def _norm(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    s = str(v).strip()
    return {"1": "1", "0": "0"}.get(s, s).lower()


def _bool_eq(row_val: Any, lit: str) -> bool:
    rv = _norm(row_val)
    rv = {"1": "true", "0": "false", "y": "true", "n": "false", "yes": "true", "no": "false"}.get(rv, rv)
    return rv == lit.lower()


def _num(v: Any) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def evaluate(ast: tuple, row: dict) -> bool:
    lower = {k.lower(): v for k, v in row.items()}
    def val(f: str) -> Any:
        v = lower.get(f.lower())
        return None if v in (EMPTY,) else v
    k = ast[0]
    if k == "or":
        return any(evaluate(a, row) for a in ast[1])
    if k == "and":
        return all(evaluate(a, row) for a in ast[1])
    if k == "not":
        return not evaluate(ast[1], row)
    if k == "empty":
        e = _norm(val(ast[1])) == ""
        return not e if ast[2] else e
    if k == "in":
        rv = val(ast[1])
        hit = any((_bool_eq(rv, x) if _is_bool_lit(x) else _norm(rv) == _norm(x)) for x in ast[2])
        return not hit if ast[3] else hit
    _, f, op, lit = ast
    rv = val(f)
    if op in ("==", "!="):
        eq = _bool_eq(rv, lit) if _is_bool_lit(lit) else (
            _num(rv) == _num(lit) if _num(rv) is not None and _num(lit) is not None else _norm(rv) == _norm(lit))
        return eq if op == "==" else not eq
    a, b = _num(rv), _num(lit)
    if a is None or b is None:
        return False
    return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]


def _amp_str(v: str) -> str:
    return '"' + str(v).replace('"', "'") + '"'


def to_ampscript(ast: tuple) -> str:
    k = ast[0]
    if k in ("or", "and"):
        return "(" + f" {k.upper()} ".join(to_ampscript(a) for a in ast[1]) + ")"
    if k == "not":
        return f"NOT ({to_ampscript(ast[1])})"
    if k == "empty":
        return f"NOT Empty(@{ast[1]})" if ast[2] else f"Empty(@{ast[1]})"
    if k == "in":
        parts = [_amp_eq(ast[1], v, "!=" if ast[3] else "==") for v in ast[2]]
        return "(" + (" AND " if ast[3] else " OR ").join(parts) + ")"
    _, f, op, lit = ast
    if op in ("==", "!="):
        return _amp_eq(f, lit, op)
    return f"@{f} {op} {lit if _num(lit) is not None else _amp_str(lit)}"


def _amp_eq(f: str, lit: str, op: str) -> str:
    if _is_bool_lit(lit):
        t = lit.lower() == "true"
        pos = f'(Lowercase(@{f}) == "true" OR @{f} == "1")'
        neg = f'(Lowercase(@{f}) == "false" OR @{f} == "0" OR Empty(@{f}))'
        return (pos if t else neg) if op == "==" else (neg if t else pos)
    if _num(lit) is not None:
        return f"@{f} {op} {lit}"
    return f"@{f} {op} {_amp_str(lit)}"


# ============================================================================================ sections
@dataclass
class Section:
    id: str
    when: str | None          # None or "otherwise" = always / group fallback
    group: str | None = None
    title: str = ""
    body: str = ""
    cta_text: str = ""
    cta_url: str = ""
    image: str = ""
    html: str = ""
    ast: tuple | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Section":
        known = set(cls.__dataclass_fields__) - {"ast"}
        unknown = set(d) - known
        if unknown:
            raise ConditionError(f"section {d.get('id')}: unknown keys {sorted(unknown)}")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", str(d.get("id", ""))):
            raise ConditionError(f"section id {d.get('id')!r} must be lowercase letters/digits/_")
        s = cls(**{k: (str(v) if v is not None and k != "when" else v) for k, v in d.items()})
        if s.when is not None and str(s.when).strip().lower() not in ("otherwise", "always"):
            s.ast = parse_condition(str(s.when))
        return s

    @property
    def is_fallback(self) -> bool:
        return self.ast is None and str(self.when or "").lower() == "otherwise"


def validate_sections(sections: list[Section], fields: dict[str, str]) -> None:
    ids = [s.id for s in sections]
    if len(ids) != len(set(ids)):
        raise ConditionError("section ids must be unique")
    lower = {f.lower(): f for f in fields}
    groups: dict[str, list[Section]] = {}
    for s in sections:
        if s.ast:
            for f in condition_fields(s.ast):
                if f.lower() not in lower:
                    raise ConditionError(f"section {s.id}: field {f!r} isn't in the audience DE")
        if s.group:
            groups.setdefault(s.group, []).append(s)
        elif s.is_fallback:
            raise ConditionError(f"section {s.id}: 'otherwise' only makes sense inside a group")
    for g, ss in groups.items():
        fb = [s for s in ss if s.is_fallback]
        if len(fb) > 1 or (fb and ss[-1] is not fb[0]):
            raise ConditionError(f"group {g}: one 'otherwise' section at most, and it must be last")


def visible_blocks(sections: list[Section], row: dict) -> list[str]:
    out, done_groups = [], set()
    for s in sections:
        if s.group:
            if s.group in done_groups:
                continue
            if s.is_fallback or (s.ast is not None and evaluate(s.ast, row)) or (s.ast is None and not s.is_fallback):
                out.append(s.id); done_groups.add(s.group)
        elif s.ast is None or evaluate(s.ast, row):
            out.append(s.id)
    return out


def _section_card(s: Section, brand: dict) -> str:
    if s.html:
        return s.html
    accent = htmlmod.escape(brand.get("accent_color", "#00A884"))
    img = (f'<img src="{htmlmod.escape(s.image)}" width="536" alt="" style="display:block;width:100%;height:auto;'
           f'border:0;border-radius:6px;margin:0 0 12px;">') if s.image else ""
    cta = (f'<a href="{htmlmod.escape(s.cta_url)}" alias="{htmlmod.escape(s.id)}" style="display:inline-block;margin-top:10px;'
           f'color:{accent};font-weight:bold;text-decoration:none;">{s.cta_text} →</a>') if s.cta_text and s.cta_url else ""
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid #e5e7eb;">'
            f'<tr><td style="padding:20px 0;">{img}<h2 style="margin:0 0 6px;color:#111827;font-size:19px;">{s.title}</h2>'
            f'<p style="margin:0;color:#4b5563;font-size:15px;line-height:1.55;">{s.body}</p>{cta}</td></tr></table>')


def compile_sections(sections: list[Section], fields: dict[str, str], brand: dict) -> str:
    """AMPscript + HTML for the {{ sections }} block. Every audience field is SET as @<Field>."""
    names = [f for f in fields if re.fullmatch(r"[A-Za-z_]\w*", f)]
    head = "%%[\n" + "\n".join(f'SET @{f} = AttributeValue("{f}")' for f in names) + "\n]%%\n"
    parts, i = [head], 0
    while i < len(sections):
        s = sections[i]
        if s.group:
            grp = [x for x in sections[i:] if x.group == s.group]
            i += len(grp)
            for j, g in enumerate(grp):
                if g.is_fallback:
                    kw = "ELSE"
                else:
                    cond = to_ampscript(g.ast) if g.ast else "1 == 1"
                    kw = ("IF " if j == 0 else "ELSEIF ") + cond + " THEN"
                parts.append(f"%%[ {kw} ]%%<!--blk:{g.id}-->{_section_card(g, brand)}<!--/blk:{g.id}-->\n")
            parts.append("%%[ ENDIF ]%%\n")
            continue
        i += 1
        body = f"<!--blk:{s.id}-->{_section_card(s, brand)}<!--/blk:{s.id}-->"
        if s.ast is None:
            parts.append(body + "\n")
        else:
            parts.append(f"%%[ IF {to_ampscript(s.ast)} THEN ]%%{body}%%[ ENDIF ]%%\n")
    return "".join(parts)


def rendered_blocks(html: str) -> list[str]:
    return list(dict.fromkeys(MARK_RE.findall(html or "")))


# ============================================================================================ extraction (any AMPscript)
@dataclass
class Analysis:
    fields: dict[str, list[tuple[str, str]]]          # field -> [(op, literal)]
    conditions: list[str]                              # human-readable
    sections: list[Section] = field(default_factory=list)
    source: str = "sections"


_AMP_CMP = re.compile(
    r"(@\w+|AttributeValue\(\s*[\"']([^\"']+)[\"']\s*\)|\[\w+\])\s*(==|!=|>=|<=|>|<)\s*(\"[^\"]*\"|'[^']*'|-?\d+(?:\.\d+)?|true|false)", re.I)


def extract_from_ampscript(code: str) -> Analysis:
    var_field: dict[str, str] = {}
    for m in re.finditer(r"SET\s+@(\w+)\s*=\s*(?:AttributeValue\(\s*[\"']([^\"']+)[\"']\s*\)|\[(\w+)\])", code, re.I):
        var_field[m.group(1).lower()] = m.group(2) or m.group(3)
    for m in re.finditer(r"SET\s+@(\w+)\s*=\s*([A-Za-z_]\w*)\s*(?:\n|\]%%|$)", code, re.I):   # SET @x = FieldName
        if m.group(2).lower() not in ("true", "false"):
            var_field.setdefault(m.group(1).lower(), m.group(2))
    fields: dict[str, list[tuple[str, str]]] = {}
    conds: list[str] = []

    def fname(tok: str, attr: str | None) -> str | None:
        if attr:
            return attr
        if tok.startswith("["):
            return tok.strip("[]")
        return var_field.get(tok[1:].lower())

    for m in re.finditer(r"\b(?:IF|ELSEIF)\b(.+?)\bTHEN\b", code, re.I | re.S):
        cond = " ".join(m.group(1).split())
        conds.append(cond)
        for c in _AMP_CMP.finditer(cond):
            f = fname(c.group(1), c.group(2))
            if f:
                lit = c.group(4).strip("\"'")
                fields.setdefault(f, []).append((c.group(3), lit))
        for c in re.finditer(r"(NOT\s+)?Empty\(\s*(@\w+|AttributeValue\(\s*[\"']([^\"']+)[\"']\s*\))\s*\)", cond, re.I):
            f = fname(c.group(2), c.group(3))
            if f:
                fields.setdefault(f, []).append(("empty", ""))
        for c in re.finditer(r"IndexOf\(\s*(@\w+)\s*,\s*[\"']([^\"']+)[\"']\s*\)", cond, re.I):
            f = fname(c.group(1), None)
            if f:
                fields.setdefault(f, []).append(("contains", c.group(2)))
    return Analysis(fields=fields, conditions=list(dict.fromkeys(conds)), source="ampscript")


def analysis_from_sections(sections: list[Section]) -> Analysis:
    fields: dict[str, list[tuple[str, str]]] = {}
    conds = []
    for s in sections:
        if s.ast:
            for f, lits in condition_literals(s.ast).items():
                fields.setdefault(f, []).extend(lits)
            conds.append(f"{s.id}: {s.when}" + (f"  [group {s.group}]" if s.group else ""))
        elif s.is_fallback:
            conds.append(f"{s.id}: otherwise  [group {s.group}]")
    return Analysis(fields=fields, conditions=conds, sections=sections, source="sections")


# ============================================================================================ candidate values
@dataclass
class Candidate:
    field: str
    value: str
    source: str        # condition | other | empty | data
    selected: bool


def candidates(an: Analysis, data_rows: list[dict], field_types: dict[str, str], max_data: int = 10) -> list[Candidate]:
    out: list[Candidate] = []
    lower_types = {k.lower(): v for k, v in field_types.items()}
    for f, lits in an.fields.items():
        ftype = (lower_types.get(f.lower()) or "Text")
        col = next((k for k in (data_rows[0] if data_rows else {}) if k.lower() == f.lower()), None)
        data_vals = []
        if col:
            counts: dict[str, int] = {}
            for r in data_rows:
                v = r.get(col)
                if v not in (None, ""):
                    counts[str(v)] = counts.get(str(v), 0) + 1
            data_vals = [v for v, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:max_data]
        seen: set[str] = set()

        def add(v: str, src: str, sel: bool) -> None:
            key = v.lower()
            if key in seen:
                return
            seen.add(key)
            out.append(Candidate(f, v, src, sel))

        if ftype == "Boolean" or any(_is_bool_lit(l) for _, l in lits):
            add("true", "condition", True); add("false", "condition", True)
            continue
        nums = sorted({_num(l) for op, l in lits if op in (">", ">=", "<", "<=") and _num(l) is not None})
        for n in nums:   # boundary values around each threshold
            for v in (n - 1, n, n + 1):
                add(str(int(v)) if float(v).is_integer() else str(v), "condition", True)
        for op, l in lits:
            if op in ("==", "!=", "contains") and l != "":
                add(l, "condition", True)
        lit_set = {l.lower() for _, l in lits}
        other = next((v for v in data_vals if v.lower() not in lit_set), None)
        if not nums:
            add(other or f"Other {f}", "other", True)
        add(EMPTY, "empty", any(op == "empty" for op, _ in lits))
        for v in data_vals:
            add(v, "data", False)
    return out


# ============================================================================================ coverage
def _expected(sections: list[Section], row: dict) -> list[str]:
    return visible_blocks(sections, row) if sections else []


def generate_rows(values: dict[str, list[str]], sections: list[Section], strategy: str = "smart",
                  max_rows: int = 200, seed: int = 11) -> tuple[list[dict], list[str]]:
    """Return (rows, notes). values: field -> selected values (EMPTY allowed)."""
    notes: list[str] = []
    fields = [f for f in values if values[f]]
    if not fields:
        return [], ["no fields with selected values"]
    rng = random.Random(seed)
    all_combos_n = 1
    for f in fields:
        all_combos_n *= len(values[f])

    def mk(combo: dict) -> dict:
        return {f: combo[f] for f in fields}

    if strategy == "full":
        if all_combos_n > max_rows:
            raise ValueError(f"full permutations = {all_combos_n} rows, above MaxRows {max_rows}; "
                             "use smart coverage or raise MaxRows")
        rows = [dict(zip(fields, c)) for c in itertools.product(*(values[f] for f in fields))]
        return rows, [f"full permutations: {len(rows)} rows"]

    rows: list[dict] = []
    block_ids = [s.id for s in sections]
    shown: set[str] = set()
    hidden: set[str] = set()

    def note_blocks(r: dict) -> None:
        vis = set(_expected(sections, r))
        shown.update(vis); hidden.update(set(block_ids) - vis)

    if strategy == "smart":
        if len(fields) == 1:
            rows = [{fields[0]: v} for v in values[fields[0]]]
        else:
            uncovered = {((a, va), (b, vb)) for a, b in itertools.combinations(fields, 2)
                         for va in values[a] for vb in values[b]}
            while uncovered:
                best, best_gain = None, -1
                for _ in range(40):
                    (a, va), (b, vb) = rng.choice(sorted(uncovered))
                    row = {a: va, b: vb}
                    for f in rng.sample(fields, len(fields)):
                        if f in row:
                            continue
                        scores = []
                        for v in values[f]:
                            gain = sum(1 for g, gv in row.items()
                                       if (((g, gv), (f, v)) in uncovered or ((f, v), (g, gv)) in uncovered))
                            scores.append((gain, rng.random(), v))
                        row[f] = max(scores)[2]
                    gain = sum(1 for x, y in itertools.combinations(fields, 2)
                               if ((x, row[x]), (y, row[y])) in uncovered)
                    if gain > best_gain:
                        best, best_gain = row, gain
                assert best is not None
                rows.append(mk(best))
                uncovered -= {((x, best[x]), (y, best[y])) for x, y in itertools.combinations(fields, 2)}
        notes.append(f"pairwise: {len(rows)} rows cover every pair of selected values")
    for r in rows:
        note_blocks(r)

    # every block shown and hidden (smart and per_block)
    need = [(b, True) for b in block_ids if b not in shown] + [(b, False) for b in block_ids if b not in hidden]
    space = itertools.product(*(values[f] for f in fields)) if all_combos_n <= 50000 else None
    pool = [dict(zip(fields, c)) for c in space] if space is not None else \
        [{f: rng.choice(values[f]) for f in fields} for _ in range(20000)]
    added_for_blocks = 0
    for b, want in need:
        if (want and b in shown) or (not want and b in hidden):
            continue
        hit = next((r for r in pool if (b in _expected(sections, r)) == want), None)
        if hit:
            rows.append(mk(hit)); note_blocks(hit); added_for_blocks += 1
        else:
            notes.append(f"block {b} can't be {'shown' if want else 'hidden'} with the selected values")
    if added_for_blocks:
        notes.append(f"+{added_for_blocks} rows so every block is seen shown and hidden")
    # de-duplicate, keep order
    uniq, seen = [], set()
    for r in rows:
        k = tuple(r[f] for f in fields)
        if k not in seen:
            seen.add(k); uniq.append(r)
    if len(uniq) > max_rows:
        notes.append(f"trimmed from {len(uniq)} to MaxRows {max_rows}: some pairs may be missing")
        uniq = uniq[:max_rows]
    notes.append(f"{len(uniq)} rows vs {all_combos_n} full permutations")
    return uniq, notes


def coverage_report(rows: list[dict], values: dict[str, list[str]], sections: list[Section]) -> dict:
    fields = [f for f in values if values[f]]
    pairs = {((a, va), (b, vb)) for a, b in itertools.combinations(fields, 2) for va in values[a] for vb in values[b]}
    got = {((a, r[a]), (b, r[b])) for r in rows for a, b in itertools.combinations(fields, 2)}
    shown = {b for r in rows for b in _expected(sections, r)}
    ids = [s.id for s in sections]
    hidden = {b for r in rows for b in ids if b not in _expected(sections, r)}
    return {"pairs_total": len(pairs), "pairs_covered": len(pairs & got),
            "blocks_shown": sorted(shown), "blocks_hidden": sorted(hidden),
            "blocks_never_shown": [b for b in ids if b not in shown],
            "blocks_never_hidden": [b for b in ids if b not in hidden]}


# ============================================================================================ mock AMPscript evaluator
_SEG = re.compile(r"%%\[(.*?)\]%%", re.S)


def _amp_expr_eval(expr: str, vars_: dict[str, Any]) -> bool:
    """Evaluate AMPscript conditions of the shape the kit generates (and common hand-written ones)."""
    s = expr.strip()
    toks = re.findall(r'"[^"]*"|\'[^\']*\'|==|!=|>=|<=|>|<|\(|\)|,|@\w+|[A-Za-z_]\w*|-?\d+(?:\.\d+)?', s)
    pos = 0

    def peek() -> str | None:
        return toks[pos] if pos < len(toks) else None

    def take() -> str:
        nonlocal pos
        pos += 1
        return toks[pos - 1]

    def atom() -> Any:
        t = take()
        if t == "(":
            v = orx(); take(); return v
        if t.upper() == "NOT":
            return not truthy(atom_cmp())
        if t.startswith("@"):
            return vars_.get(t[1:].lower(), "")
        if t[0] in "\"'":
            return t[1:-1]
        if re.fullmatch(r"-?\d+(?:\.\d+)?", t):
            return float(t)
        fn = t.lower()
        take()  # (
        args = []
        while peek() != ")":
            args.append(orx())
            if peek() == ",":
                take()
        take()
        if fn == "empty":
            return str(args[0] if args else "").strip() == ""
        if fn == "lowercase":
            return str(args[0]).lower()
        if fn == "indexof":
            i = str(args[0]).lower().find(str(args[1]).lower())
            return float(i + 1)
        if fn == "attributevalue":
            return vars_.get("__row__", {}).get(str(args[0]).lower(), "")
        raise ValueError(f"mock evaluator: unsupported function {fn}()")

    def truthy(v: Any) -> bool:
        return v if isinstance(v, bool) else str(v).lower() == "true"

    def atom_cmp() -> Any:
        left = atom()
        if peek() in ("==", "!=", ">", ">=", "<", "<="):
            op = take()
            right = atom()
            ln, rn = _num(left), _num(right)
            if ln is not None and rn is not None and not (isinstance(left, str) and isinstance(right, str) and op in ("==", "!=")):
                l2, r2 = ln, rn
            else:
                l2, r2 = str(left).lower(), str(right).lower()
            if op in (">", ">=", "<", "<=") and (ln is None or rn is None):
                return False
            return {"==": l2 == r2, "!=": l2 != r2, ">": l2 > r2, ">=": l2 >= r2, "<": l2 < r2, "<=": l2 <= r2}[op]
        return left

    def andx() -> Any:
        v = truthy(atom_cmp())
        while peek() and peek().upper() == "AND":
            take(); r = truthy(atom_cmp()); v = v and r
        return v

    def orx() -> Any:
        v = andx()
        while peek() and peek().upper() == "OR":
            take(); r = andx(); v = v or r
        return v

    return bool(orx())


def mock_render(html: str, row: dict) -> str:
    """Render IF/ELSEIF/ELSE/ENDIF + SET @x = AttributeValue() across %%[ ]%% blocks; output visible HTML."""
    lower = {k.lower(): ("" if v is None else ("true" if v is True else "false" if v is False else str(v)))
             for k, v in row.items()}
    vars_: dict[str, Any] = {"__row__": lower}
    out: list[str] = []
    stack: list[list[bool]] = []   # [parent_active, branch_taken, current_active]

    def active() -> bool:
        return all(f[2] for f in stack)

    pos = 0
    for m in _SEG.finditer(html):
        if active():
            out.append(html[pos:m.start()])
        pos = m.end()
        code = m.group(1)
        for stmt in re.finditer(r"\b(SET\s+@(\w+)\s*=\s*AttributeValue\(\s*\"([^\"]+)\"\s*\)|ELSEIF\b(.+?)\bTHEN|IF\b(.+?)\bTHEN|ELSE\b|ENDIF\b)", code, re.S | re.I):
            t = stmt.group(0).upper()
            if t.startswith("SET"):
                if active():
                    vars_[stmt.group(2).lower()] = lower.get(stmt.group(3).lower(), "")
            elif t.startswith("ELSEIF"):
                fr = stack[-1]
                parent = all(f[2] for f in stack[:-1])
                if fr[1]:
                    fr[2] = False
                else:
                    ok = parent and _amp_expr_eval(stmt.group(4), vars_)
                    fr[2] = ok; fr[1] = fr[1] or ok
            elif t.startswith("IF"):
                parent = active()
                ok = parent and _amp_expr_eval(stmt.group(5), vars_)
                stack.append([parent, ok, ok])
            elif t.startswith("ELSE"):
                fr = stack[-1]
                fr[2] = not fr[1] and all(f[2] for f in stack[:-1]); fr[1] = True
            elif t.startswith("ENDIF"):
                stack.pop()
    if active():
        out.append(html[pos:])
    return "".join(out)
