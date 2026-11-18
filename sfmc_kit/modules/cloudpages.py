"""CloudPages: a styled test-drive booking page that writes submissions to a bookings DE.

How the pieces fit:
* The page body (HTML + AMPscript) is a Content Builder **HTML block** `CK_<CODE>_CP_TestDriveBooking`,
  created/updated over the API.
* A CloudPage holds one line, ``%%=ContentBlockByKey("CK_<CODE>_CP_TestDriveBooking")=%%``. CloudPages
  publishing isn't available on the public API, so the page is created and published once in the UI; after
  that the kit updates the block and the live page follows without re-publishing.
* Emails link to the page with ``CloudPagesURL(page_id, ...)``: encrypted, personalized parameters pre-fill
  the form. Until ``cloudpage.page_id`` is set in the brief, emails fall back to ``cloudpage.fallback_url``.
"""
from __future__ import annotations

import html as htmlmod
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from ..config import PROJECT_ROOT
from .data_extensions import DESpec, FieldSpec

CLOUDPAGE_TEMPLATE_DIR = PROJECT_ROOT / "templates" / "cloudpages"
CLOUDPAGE_TOKEN = "{{cloudpage_url}}"
HTML_BLOCK_ASSET_TYPE = {"name": "htmlblock", "id": 197}


@dataclass
class CloudPageBrief:
    type: str = "test_drive_booking"
    page_id: int | None = None
    models: list[str] = field(default_factory=lambda: ["Volt X"])
    default_model: str | None = None
    time_slots: list[str] = field(default_factory=lambda: ["Morning (9am-12pm)", "Afternoon (12-4pm)",
                                                           "Evening (4-7pm)"])
    headline: str = "Book your test drive"
    intro: str = "Pick a model, a dealer and a time that suits you. We'll call to confirm."
    button_text: str = "Request my test drive"
    consent_text: str = ("I agree that Aurora Motors and my chosen dealer may contact me by phone or email "
                         "about this test drive.")
    expect: list[str] = field(default_factory=lambda: [
        "A fully charged car ready when you arrive", "About 30 minutes behind the wheel",
        "No obligation, no pressure", "Bring a valid driver's license"])
    footer_text: str = "Aurora Motors is a fictional brand used for demonstration."
    fallback_url: str = "https://www.example.com/aurora/test-drive"
    max_days_ahead: int = 60
    booking_rate: float = 0.35   # mock only: share of clickers who submit the form

    def __post_init__(self) -> None:
        if self.type != "test_drive_booking":
            raise ValueError("cloudpage.type must be test_drive_booking")
        if not self.models:
            raise ValueError("cloudpage.models needs at least one model")
        self.default_model = self.default_model or self.models[0]
        if self.default_model not in self.models:
            raise ValueError("cloudpage.default_model must be one of cloudpage.models")
        for v in [*self.models, *self.time_slots]:
            if "|" in v or '"' in v:
                raise ValueError(f"cloudpage option {v!r} may not contain | or \"")
        if self.page_id is not None:
            self.page_id = int(self.page_id)

    @classmethod
    def from_dict(cls, d: dict | None) -> "CloudPageBrief | None":
        if not d:
            return None
        known = set(cls.__dataclass_fields__)
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"cloudpage: unknown keys {sorted(unknown)}")
        return cls(**d)


def bookings_de(prefix: str, code: str) -> DESpec:
    F = FieldSpec
    return DESpec(
        key=f"{prefix}TestDriveBookings", name=f"{prefix}TestDriveBookings", folder=f"CampaignKit/{code}",
        sendable=True, description="Test-drive requests submitted on the campaign CloudPage",
        fields=[
            F("SubscriberKey", "Text", 50, pk=True), F("Model", "Text", 50, pk=True),
            F("BookingID", "Text", 36), F("FirstName", "Text", 50), F("LastName", "Text", 50),
            F("EmailAddress", "EmailAddress", required=True), F("Phone", "Text", 30),
            F("DealerID", "Text", 10), F("PreferredDate", "Date"), F("PreferredTime", "Text", 30),
            F("Comments", "Text", 500), F("ConsentToContact", "Boolean"), F("Status", "Text", 20),
            F("Source", "Text", 20), F("Campaign", "Text", 20), F("CreatedDate", "Date"),
        ])


def block_key(prefix: str) -> str:
    return f"{prefix}CP_TestDriveBooking"


def page_stub(prefix: str) -> str:
    """The single line to paste into the CloudPage."""
    return f'%%=ContentBlockByKey("{block_key(prefix)}")=%%'


def _e(s: str) -> str:
    return htmlmod.escape(str(s), quote=True)


def render_page(cp: CloudPageBrief, code: str, prefix: str, brand: dict, dealers: list[dict],
                dealers_de: str = "CK_Master_Dealers") -> str:
    src = (CLOUDPAGE_TEMPLATE_DIR / "test_drive_booking.html").read_text()
    models = "\n".join(
        f'            <label class="model"><input type="radio" name="Model" value="{_e(m)}" '
        f'%%=IIF(@model == "{m}", "checked", "")=%%><span>{_e(m)}</span></label>' for m in cp.models)
    dealer_opts = "\n".join(
        f'            <option value="{_e(d["DealerID"])}" %%=IIF(@dealer == "{d["DealerID"]}", "selected", "")=%%>'
        f'{_e(d.get("DealerName", d["DealerID"]))} · {_e(d.get("City", ""))}, {_e(d.get("State", ""))}</option>'
        for d in sorted(dealers, key=lambda d: (str(d.get("State")), str(d.get("City")), str(d.get("DealerName")))))
    slots = "\n".join(
        f'            <option value="{_e(t)}" %%=IIF(@time == "{t}", "selected", "")=%%>{_e(t)}</option>'
        for t in cp.time_slots)
    expect = "\n".join(f"      <li>{_e(x)}</li>" for x in cp.expect)
    values = {
        "campaign_code": code, "bookings_de": f"{prefix}TestDriveBookings", "dealers_de": dealers_de,
        "models_pipe": "|".join(cp.models), "default_model": cp.default_model,
        "slots_pipe": "|".join(cp.time_slots), "max_days_ahead": str(cp.max_days_ahead),
        "headline": _e(cp.headline), "intro": _e(cp.intro), "button_text": _e(cp.button_text),
        "consent_text": _e(cp.consent_text), "footer_text": _e(cp.footer_text),
        "model_options": models, "dealer_options": dealer_opts, "slot_options": slots, "expect_items": expect,
        **{f"brand_{k}": _e(v) for k, v in brand.items()},
    }
    import re

    def sub(m: re.Match) -> str:
        k = m.group(1)
        if k not in values:
            raise KeyError(f"CloudPage template needs '{k}'")
        return values[k]

    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", sub, src)


def cta_link(cp: CloudPageBrief, code: str) -> str:
    """Value substituted for {{cloudpage_url}} in email blocks."""
    if not cp.page_id:
        return cp.fallback_url
    # Single-quoted AMPscript strings: the link sits inside href="...".
    return ("%%=RedirectTo(CloudPagesURL(" + str(cp.page_id) +
            ", 'SubscriberKey', _subscriberkey, 'FirstName', AttributeValue('FirstName'), "
            "'LastName', AttributeValue('LastName'), 'EmailAddress', AttributeValue('EmailAddress'), "
            "'DealerID', AttributeValue('DealerID'), 'Model', '" + cp.default_model + "', "
            "'Campaign', '" + code + "'))=%%")


def resolve_blocks(blocks: dict[str, str], cp: CloudPageBrief | None, code: str) -> dict[str, str]:
    if not cp:
        return blocks
    link = cta_link(cp, code)
    return {k: v.replace(CLOUDPAGE_TOKEN, link) for k, v in blocks.items()}


def preview_page(page_html: str, sample: dict) -> str:
    """Static preview of the form (GET state, pre-filled from `sample`): AMPscript logic is stripped."""
    import re
    vals = {"firstH": sample.get("FirstName", ""), "lastH": sample.get("LastName", ""),
            "emailH": sample.get("EmailAddress", ""), "phoneH": "", "commentsH": "", "date": "",
            "errors": "", "model": sample.get("Model", "")}
    out = re.sub(r"%%\[\s*IF @done == \"true\" THEN\s*\]%%.*?%%\[\s*ELSE\s*\]%%", "", page_html, flags=re.S)
    out = re.sub(r"%%\[\s*IF NOT Empty\(@errors\) THEN\s*\]%%.*?%%\[\s*ENDIF\s*\]%%", "", out, flags=re.S)
    out = re.sub(r"%%\[.*?\]%%", "", out, flags=re.S)
    out = re.sub(r'%%=IIF\(@model == "([^"]+)", "checked", ""\)=%%',
                 lambda m: "checked" if m.group(1) == (sample.get("Model") or vals["model"]) else "", out)
    out = re.sub(r'%%=IIF\(@dealer == "([^"]+)", "selected", ""\)=%%',
                 lambda m: "selected" if m.group(1) == sample.get("DealerID") else "", out)
    out = re.sub(r"%%=v\(@(\w+)\)=%%", lambda m: _e(vals.get(m.group(1), "")), out)
    out = re.sub(r"%%=.*?=%%", "", out, flags=re.S)
    return out


def simulate_bookings(clickers: list[dict], cp: CloudPageBrief, code: str, dealers: list[dict],
                      seed: int = 7) -> list[dict]:
    """Mock tenant only: a share of email clickers submit the form."""
    rng = random.Random(f"{seed}:{code}")
    dealer_ids = [d["DealerID"] for d in dealers] or ["D001"]
    rows = []
    for c in clickers:
        if rng.random() >= cp.booking_rate:
            continue
        clicked_at = datetime.fromisoformat(str(c["ClickDate"]))
        rows.append({
            "SubscriberKey": c["SubscriberKey"], "Model": rng.choice(cp.models), "BookingID": str(uuid.uuid4()),
            "FirstName": c.get("FirstName"), "LastName": c.get("LastName"), "EmailAddress": c["EmailAddress"],
            "Phone": f"555-{rng.randint(100, 999)}-{rng.randint(1000, 9999)}",
            "DealerID": c.get("DealerID") if c.get("DealerID") in dealer_ids else rng.choice(dealer_ids),
            "PreferredDate": (clicked_at + timedelta(days=rng.randint(2, 21))).strftime("%Y-%m-%d"),
            "PreferredTime": rng.choice(cp.time_slots), "Comments": None, "ConsentToContact": True,
            "Status": "Requested", "Source": "CloudPage", "Campaign": code,
            "CreatedDate": (clicked_at + timedelta(minutes=rng.randint(2, 40))).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return rows
