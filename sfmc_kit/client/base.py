"""Backend interface implemented by LiveBackend (real SFMC) and MockBackend (SQLite sandbox)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..modules.assets import EmailSpec
from ..modules.automation import AutomationSpec, QuerySpec
from ..modules.data_extensions import DESpec


class Backend(ABC):
    mode: str = "abstract"

    # --- folders ---
    @abstractmethod
    def ensure_folder(self, content_type: str, path: str) -> int | None: ...

    # --- data extensions ---
    @abstractmethod
    def get_de(self, key: str) -> dict | None: ...
    @abstractmethod
    def create_de(self, spec: DESpec) -> dict: ...
    @abstractmethod
    def delete_de(self, key: str) -> None: ...
    @abstractmethod
    def list_des(self, name_like: str = "") -> list[dict]: ...
    @abstractmethod
    def describe_de(self, key: str) -> list[dict]: ...
    @abstractmethod
    def upsert_rows(self, key: str, rows: list[dict], pk: list[str]) -> int: ...
    @abstractmethod
    def read_rows(self, key: str, max_rows: int = 2500) -> list[dict]: ...

    def ensure_de(self, spec: DESpec) -> tuple[dict, bool]:
        existing = self.get_de(spec.key)
        if existing:
            return existing, False
        return self.create_de(spec), True

    # --- content ---
    @abstractmethod
    def upsert_email(self, spec: EmailSpec) -> dict: ...
    @abstractmethod
    def delete_asset(self, asset_id: Any) -> None: ...

    # --- automation ---
    @abstractmethod
    def upsert_query(self, spec: QuerySpec) -> dict: ...
    @abstractmethod
    def delete_query(self, query_id: str) -> None: ...
    @abstractmethod
    def upsert_automation(self, spec: AutomationSpec, query_ids: dict[str, str]) -> dict: ...
    @abstractmethod
    def run_automation(self, automation_id: str, wait: bool = True, timeout: int = 900) -> str: ...
    @abstractmethod
    def delete_automation(self, automation_id: str) -> None: ...

    # --- journeys ---
    @abstractmethod
    def send_defaults(self, send_classification_key: str) -> dict[str, str]: ...
    @abstractmethod
    def upsert_event_definition(self, payload: dict) -> dict: ...
    @abstractmethod
    def delete_event_definition(self, event_def_id: str) -> None: ...
    @abstractmethod
    def upsert_journey(self, payload: dict) -> dict: ...
    @abstractmethod
    def publish_journey(self, journey_id: str, version: int) -> str: ...
    @abstractmethod
    def delete_journey(self, journey_id: str) -> None: ...

    def whoami(self) -> dict:
        return {"mode": self.mode}
