"""SQL Query Activity + Automation specs."""
from __future__ import annotations

from dataclasses import dataclass, field

UPDATE_TYPES = {"Overwrite": 0, "Update": 1, "Append": 2}


@dataclass
class QuerySpec:
    key: str
    name: str
    sql: str
    target_key: str
    target_name: str
    update_type: str = "Overwrite"
    description: str = ""

    def __post_init__(self) -> None:
        if self.update_type not in UPDATE_TYPES:
            raise ValueError(f"Query {self.name}: update_type must be one of {list(UPDATE_TYPES)}")


@dataclass
class AutomationSpec:
    key: str
    name: str
    steps: list[list[str]]          # each step = list of QuerySpec keys run in parallel
    description: str = ""
    schedule: dict | None = None    # informational; schedules are applied in the UI or via a later phase


def automation_payload(spec: AutomationSpec, query_ids: dict[str, str]) -> dict:
    steps = []
    for i, step in enumerate(spec.steps, start=1):
        acts = []
        for j, qkey in enumerate(step, start=1):
            acts.append({"name": qkey, "objectTypeId": 300, "activityObjectId": query_ids[qkey], "displayOrder": j})
        steps.append({"name": f"Step {i}", "step": i, "activities": acts})
    return {"name": spec.name, "key": spec.key, "description": spec.description, "steps": steps}
