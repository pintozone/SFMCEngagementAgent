"""Campaign brief loading + validation."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .modules.data_extensions import DESpec


class BriefError(ValueError):
    pass


@dataclass
class EmailBrief:
    ref: str
    name: str
    subject: str
    preheader: str
    template: str
    blocks: dict[str, str]


@dataclass
class Brief:
    name: str
    code: str
    objective: str
    brand: dict[str, str]
    kpis: dict[str, Any]
    send_classification: str
    audience_de: DESpec
    audience_sql: str
    audience_update_type: str
    extra_des: list[DESpec]
    emails: list[EmailBrief]
    journey_name: str
    entry_type: str
    entry_mode: str
    steps: list[dict]
    source: Path | None = None
    raw: dict = field(default_factory=dict)

    @property
    def prefix(self) -> str:
        return f"CK_{self.code}_"


def _req(d: dict, key: str, ctx: str) -> Any:
    if key not in d or d[key] in (None, ""):
        raise BriefError(f"{ctx}: '{key}' is required")
    return d[key]


def load_brief(path: str | Path) -> Brief:
    path = Path(path)
    raw = yaml.safe_load(path.read_text())
    return parse_brief(raw, path)


def parse_brief(raw: dict, source: Path | None = None) -> Brief:
    c = _req(raw, "campaign", "brief")
    code = str(_req(c, "code", "campaign")).upper()
    if not re.fullmatch(r"[A-Z0-9]{3,20}", code):
        raise BriefError("campaign.code must be 3-20 chars A-Z/0-9 (used in every object name)")
    prefix = f"CK_{code}_"

    aud = _req(raw, "audience", "brief")
    de_raw = dict(_req(aud, "de", "audience"))
    de_raw.setdefault("key", f"{prefix}Audience")
    de_raw.setdefault("name", de_raw["key"])
    de_raw.setdefault("sendable", True)
    de_raw.setdefault("folder", f"CampaignKit/{code}")
    audience_de = DESpec.from_dict(de_raw)
    if not audience_de.sendable:
        raise BriefError("audience.de must be sendable (journey entry)")
    field_names = {f.name for f in audience_de.fields}
    if "EmailAddress" not in field_names:
        raise BriefError("audience.de needs an EmailAddress field (journey default email)")

    extra = []
    for d in raw.get("data_extensions", []) or []:
        d = dict(d)
        k = str(d.get("key") or d["name"])
        if not k.startswith("CK_"):
            k = prefix + k
        d["key"] = d["name"] = k
        d.setdefault("folder", f"CampaignKit/{code}")
        extra.append(DESpec.from_dict(d))

    emails = []
    refs = set()
    for e in _req(raw, "emails", "brief"):
        ref = _req(e, "ref", "email")
        if ref in refs:
            raise BriefError(f"duplicate email ref {ref}")
        refs.add(ref)
        e = dict(e)
        e["blocks"] = {"preheader_hidden": e.get("preheader", ""), "footnote": "", "cta_alias": "Primary CTA",
                       "hero_alt": c.get("name", ""), **(e.get("blocks") or {})}
        emails.append(EmailBrief(ref=ref, name=e.get("name", ref), subject=_req(e, "subject", f"email {ref}"),
                                 preheader=e.get("preheader", ""), template=_req(e, "template", f"email {ref}"),
                                 blocks={k: str(v) for k, v in (e.get("blocks") or {}).items()}))

    j = _req(raw, "journey", "brief")
    entry = j.get("entry", {}) or {}
    entry_type = entry.get("type", "data_extension")
    if entry_type not in ("data_extension", "api_event"):
        raise BriefError("journey.entry.type must be data_extension or api_event")
    steps = _normalize_steps(_req(j, "steps", "journey"))
    _check_steps(steps, refs, sent=set())

    return Brief(
        name=_req(c, "name", "campaign"), code=code, objective=c.get("objective", ""),
        brand=c.get("brand", {}) or {}, kpis=c.get("kpis", {}) or {},
        send_classification=(raw.get("send") or {}).get("send_classification", "Default Commercial"),
        audience_de=audience_de, audience_sql=_req(aud, "sql", "audience"),
        audience_update_type=aud.get("update_type", "Overwrite"), extra_des=extra, emails=emails,
        journey_name=j.get("name", f"{prefix}Journey"), entry_type=entry_type,
        entry_mode=entry.get("entry_mode", "SingleEntryAcrossAllVersions"), steps=steps, source=source, raw=raw)


def _normalize_steps(steps: list) -> list:
    """YAML 1.1 reads bare `yes:` / `no:` keys as booleans; map them back to strings."""
    out = []
    for s in steps or []:
        if isinstance(s, dict) and "decision" in s and isinstance(s["decision"], dict):
            d = {({True: "yes", False: "no"}.get(k, k) if isinstance(k, bool) else k): v
                 for k, v in s["decision"].items()}
            d["yes"] = _normalize_steps(d.get("yes") or [])
            d["no"] = _normalize_steps(d.get("no") or [])
            s = {"decision": d}
        out.append(s)
    return out


def _check_steps(steps: list, refs: set[str], sent: set[str]) -> None:
    for i, s in enumerate(steps):
        if not isinstance(s, dict) or len(s) != 1:
            raise BriefError(f"journey step {s!r} must be a single-key mapping (email/wait/decision)")
        if "email" in s:
            if s["email"] not in refs:
                raise BriefError(f"journey step uses unknown email ref '{s['email']}'")
            sent = sent | {s["email"]}
        elif "wait" in s:
            w = s["wait"]
            if not isinstance(w, dict) or len(w) != 1 or next(iter(w)) not in ("minutes", "hours", "days", "weeks"):
                raise BriefError(f"wait step {w!r} must be like {{days: 3}}")
        elif "decision" in s:
            d = s["decision"]
            if d.get("on", "opened") not in ("opened", "clicked"):
                raise BriefError("decision.on must be opened or clicked")
            if d.get("email") not in sent:
                raise BriefError(f"decision on '{d.get('email')}' must come after that email is sent")
            if i != len(steps) - 1:
                raise BriefError("a decision must be the last step in its path (put follow-ups in yes/no)")
            _check_steps(d.get("yes", []) or [], refs, sent)
            _check_steps(d.get("no", []) or [], refs, sent)
        else:
            raise BriefError(f"unknown journey step {s!r}")
