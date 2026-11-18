"""Email asset specs, template rendering and offline preview."""
from __future__ import annotations

import html as htmlmod
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import TEMPLATE_DIR

HTML_EMAIL_ASSET_TYPE = {"name": "htmlemail", "id": 208}


@dataclass
class EmailSpec:
    key: str
    name: str
    subject: str
    preheader: str
    html: str
    folder: str = "CampaignKit"

    def asset_payload(self, category_id: int | None) -> dict[str, Any]:
        p: dict[str, Any] = {
            "name": self.name,
            "customerKey": self.key,
            "assetType": HTML_EMAIL_ASSET_TYPE,
            "views": {
                "html": {"content": self.html},
                "subjectline": {"content": self.subject},
                "preheader": {"content": self.preheader},
            },
            "data": {"email": {"options": {"characterEncoding": "utf-8"}}},
        }
        if category_id:
            p["category"] = {"id": category_id}
        return p


def render_template(template: str, blocks: dict[str, str], brand: dict[str, str]) -> str:
    """Fill {{ placeholder }} tokens (build-time) – AMPscript (%%...%%) is left for SFMC send-time."""
    path = Path(template)
    if not path.is_absolute():
        path = TEMPLATE_DIR / template
    src = path.read_text()
    values = {**{f"brand_{k}": v for k, v in brand.items()}, **blocks}

    def sub(m: re.Match) -> str:
        k = m.group(1).strip()
        if k not in values:
            raise KeyError(f"Template {path.name} needs block '{k}' (not provided in brief)")
        return str(values[k])

    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", sub, src)


def preview(html: str, row: dict[str, Any]) -> str:
    """Very small AMPscript emulator for offline previews (SET/AttributeValue/v/IIF/Empty + %%Field%%)."""
    lower = {k.lower(): v for k, v in row.items()}
    vars_: dict[str, Any] = {}

    def eval_expr(expr: str) -> Any:
        expr = expr.strip()
        m = re.fullmatch(r'AttributeValue\(\s*"([^"]+)"\s*\)', expr, re.I)
        if m:
            return lower.get(m.group(1).lower(), "")
        m = re.fullmatch(r'"([^"]*)"', expr)
        if m:
            return m.group(1)
        m = re.fullmatch(r"@(\w+)", expr)
        if m:
            return vars_.get(m.group(1).lower(), "")
        m = re.fullmatch(r"Empty\((.+)\)", expr, re.I)
        if m:
            return eval_expr(m.group(1)) in ("", None)
        m = re.fullmatch(r"IIF\((.+?),(.+?),(.+)\)", expr, re.I)
        if m:
            return eval_expr(m.group(2)) if eval_expr(m.group(1)) else eval_expr(m.group(3))
        return expr

    def run_block(code: str) -> None:
        # IF Empty(@x) THEN SET @x = "..." ENDIF  (single-level)
        for m in re.finditer(r"IF\s+(.+?)\s+THEN(.+?)ENDIF", code, re.I | re.S):
            if eval_expr(m.group(1)):
                run_block(m.group(2))
        code = re.sub(r"IF\s+.+?\s+THEN.+?ENDIF", "", code, flags=re.I | re.S)
        for m in re.finditer(r"SET\s+@(\w+)\s*=\s*(.+?)(?=\s+SET\s+@|\s*$)", code, re.I | re.S):
            vars_[m.group(1).lower()] = eval_expr(m.group(2))

    def block(m: re.Match) -> str:
        run_block(m.group(1))
        return ""

    out = re.sub(r"%%\[(.*?)\]%%", block, html, flags=re.S)
    out = re.sub(r"%%=\s*v\(\s*@(\w+)\s*\)\s*=%%",
                 lambda m: htmlmod.escape(str(vars_.get(m.group(1).lower(), ""))), out, flags=re.I)
    system = {"member_busname": "Aurora Motors (Sample)", "member_addr": "100 Example Way",
              "member_city": "Detroit", "member_state": "MI", "member_postalcode": "48201",
              "member_country": "US", "profile_center_url": "#profile", "unsub_center_url": "#unsub",
              "view_email_url": "#view", "emailaddr": lower.get("emailaddress", "")}
    out = re.sub(r"%%(\w+)%%", lambda m: htmlmod.escape(str(lower.get(m.group(1).lower(),
                                                                         system.get(m.group(1).lower(), "")))), out)
    return out
