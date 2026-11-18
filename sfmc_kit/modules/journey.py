"""Journey Builder JSON construction (entry event + EMAILV2 / WAIT / ENGAGEMENTDECISION activities)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Journey Builder application-extension IDs for entry sources (stable platform constants).
DE_ENTRY_SAE_ID = "97e942ee-6914-4d3d-9e52-37ecb71f79ed"
API_ENTRY_SAE_ID = "7db1f972-f8b7-49b6-91b5-fa218e13953d"

# Engagement split statistic types. Verify on first live build; adjust here if SFMC rejects.
STATS_TYPE = {"opened": 2, "clicked": 3}

WAIT_UNITS = {"minutes": "MINUTES", "hours": "HOURS", "days": "DAYS", "weeks": "WEEKS"}


@dataclass
class EntrySpec:
    type: str          # data_extension | api_event
    de_key: str
    event_key: str
    name: str
    entry_mode: str = "SingleEntryAcrossAllVersions"   # | MultipleEntries | NoReentry


def event_definition_payload(e: EntrySpec, de_object_id: str) -> dict:
    if e.type == "data_extension":
        return {
            "type": "EmailAudience", "name": e.name, "eventDefinitionKey": e.event_key, "mode": "Production",
            "dataExtensionId": de_object_id, "sourceApplicationExtensionId": DE_ENTRY_SAE_ID,
            "iconUrl": "/images/icon_data-extension.svg", "isVisibleInPicker": False, "category": "Audience",
            "arguments": {"dataExtensionId": de_object_id, "criteria": ""},
            "configurationArguments": {"unconfigured": False},
            "metaData": {"scheduleState": "No Schedule"},
        }
    if e.type == "api_event":
        return {
            "type": "APIEvent", "name": e.name, "eventDefinitionKey": e.event_key, "mode": "Production",
            "dataExtensionId": de_object_id, "sourceApplicationExtensionId": API_ENTRY_SAE_ID,
            "iconUrl": "/images/icon_journeyBuilder-event-api-blue.svg", "isVisibleInPicker": False,
            "category": "Event", "schema": {"fields": []},
        }
    raise ValueError(f"Unknown entry type {e.type}")


class JourneyBuilder:
    """Turns brief steps into the activities array. Steps:
         - email: <email_ref>
         - wait: {days: 3}            (or hours/minutes/weeks)
         - decision: {on: opened|clicked, email: <email_ref>, yes: [steps], no: [steps]}
    """

    def __init__(self, emails: dict[str, dict[str, Any]], send_defaults: dict[str, str]):
        self.emails = emails              # ref -> {legacy_id, subject, preheader, name, key}
        self.send_defaults = send_defaults
        self.activities: list[dict] = []
        self.counters: dict[str, int] = {}
        self.email_activity_key: dict[str, str] = {}

    def _key(self, prefix: str) -> str:
        self.counters[prefix] = self.counters.get(prefix, 0) + 1
        return f"{prefix}-{self.counters[prefix]}"

    def build(self, steps: list[dict]) -> str | None:
        """Builds a chain and returns the key of its first activity (None if empty)."""
        first: str | None = None
        prev: dict | None = None
        for step in steps:
            act = self._make(step)
            self.activities.append(act)
            if prev is None:
                first = act["key"]
            else:
                prev["outcomes"][0]["next"] = act["key"]
            if act["type"] == "ENGAGEMENTDECISION":
                break  # branches carry on; nothing may follow a split in the same chain
            prev = act
        return first

    def _make(self, step: dict) -> dict:
        if "email" in step:
            ref = step["email"]
            if ref not in self.emails:
                raise ValueError(f"Journey step references unknown email '{ref}'")
            em = self.emails[ref]
            key = self._key("EMAIL")
            self.email_activity_key[ref] = key
            ts = {
                "emailId": em["legacy_id"], "emailSubject": em["subject"], "preHeader": em["preheader"],
                "name": em["name"], "description": "", "isMultipart": True, "autoAddSubscribers": True,
                "autoUpdateSubscribers": True, "isTrackingClicks": True, "isSendLogging": True,
                "suppressTracking": False, "isSalesforceTracking": False, "isStoppedOnJobError": False,
                "suppressionLists": [], "exclusionFilter": "",
                **{k: v for k, v in self.send_defaults.items() if v},
            }
            return {"key": key, "name": em["name"], "type": "EMAILV2",
                    "outcomes": [{"key": f"{key}-out", "next": None, "arguments": {}, "metaData": {}}],
                    "arguments": {}, "configurationArguments": {"triggeredSend": ts},
                    "metaData": {"isConfigured": True, "emailRef": ref}}
        if "wait" in step:
            (unit, n), = step["wait"].items()
            key = self._key("WAIT")
            return {"key": key, "name": f"Wait {n} {unit}", "type": "WAIT",
                    "outcomes": [{"key": f"{key}-out", "next": None, "arguments": {}, "metaData": {}}],
                    "arguments": {}, "configurationArguments": {"waitDuration": int(n), "waitUnit": WAIT_UNITS[unit]},
                    "metaData": {"isConfigured": True}}
        if "decision" in step:
            d = step["decision"]
            on, ref = d.get("on", "opened"), d["email"]
            if ref not in self.email_activity_key:
                raise ValueError(f"Decision references email '{ref}' that has not been sent earlier in the path")
            key = self._key("ENGAGE")
            act = {"key": key, "name": f"{on.title()} {self.emails[ref]['name']}?", "type": "ENGAGEMENTDECISION",
                   "outcomes": [], "arguments": {},
                   "configurationArguments": {"statsTypeId": STATS_TYPE[on],
                                              "refActivityCustomerKey": self.email_activity_key[ref]},
                   "metaData": {"isConfigured": True, "on": on, "emailRef": ref}}
            # Reserve the slot before building branches so activity order is readable.
            self.activities.append(act)
            yes = self.build(d.get("yes", []))
            no = self.build(d.get("no", []))
            self.activities.remove(act)
            act["outcomes"] = [
                {"key": f"{key}-yes", "next": yes, "arguments": {}, "metaData": {"label": "Yes"}},
                {"key": f"{key}-no", "next": no, "arguments": {}, "metaData": {"label": "No"}},
            ]
            return act
        raise ValueError(f"Unknown journey step: {step}")


def journey_payload(key: str, name: str, description: str, entry: EntrySpec, event_def_id: str,
                    activities: list[dict], first_key: str | None) -> dict:
    trigger = {
        "key": "TRIGGER", "name": entry.name,
        "type": "EmailAudience" if entry.type == "data_extension" else "APIEvent",
        "eventDefinitionKey": entry.event_key, "outcomes": [{"key": "TRIGGER-out", "next": first_key}],
        "arguments": {}, "configurationArguments": {},
        "metaData": {"eventDefinitionId": event_def_id, "eventDefinitionKey": entry.event_key,
                     "chainType": "None", "configurationRequired": False},
    }
    clean = []
    for a in activities:
        a = {**a, "metaData": {k: v for k, v in a["metaData"].items() if k == "isConfigured"},
             "outcomes": [{k: v for k, v in o.items() if not (k == "next" and v is None)} for o in a["outcomes"]]}
        clean.append(a)
    return {
        "key": key, "name": name, "description": description, "workflowApiVersion": 1.0,
        "entryMode": entry.entry_mode,
        "defaults": {"email": [f'{{{{Event.{entry.event_key}."EmailAddress"}}}}']},
        "triggers": [{k: v for k, v in trigger.items() if k != "outcomes"}],
        "activities": clean,
        # kept for the mock simulator / manifest (stripped before live POST)
        "_firstActivityKey": first_key,
        "_activityMeta": {a["key"]: a["metaData"] for a in activities},
    }
