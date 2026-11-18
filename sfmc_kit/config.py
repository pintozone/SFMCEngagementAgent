"""Configuration: environment variables (optionally from a git-ignored .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("SFMC_KIT_DATA_DIR", PROJECT_ROOT / "data"))
TEMPLATE_DIR = PROJECT_ROOT / "templates" / "emails"

# Prefix applied to every object the kit creates in SFMC.
OBJECT_PREFIX = "CK_"


def _load_dotenv() -> None:
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class SFMCConfig:
    subdomain: str
    client_id: str
    client_secret: str
    account_id: str | None

    @property
    def auth_base(self) -> str:
        return f"https://{self.subdomain}.auth.marketingcloudapis.com"

    @property
    def rest_base(self) -> str:
        return f"https://{self.subdomain}.rest.marketingcloudapis.com"

    @property
    def soap_url(self) -> str:
        return f"https://{self.subdomain}.soap.marketingcloudapis.com/Service.asmx"


class ConfigError(RuntimeError):
    pass


def load_live_config() -> SFMCConfig:
    _load_dotenv()
    missing = [k for k in ("SFMC_SUBDOMAIN", "SFMC_CLIENT_ID", "SFMC_CLIENT_SECRET") if not os.environ.get(k)]
    if missing:
        raise ConfigError(
            "Missing SFMC credentials: " + ", ".join(missing) +
            ". Set them as environment variables (cloud environment settings) or in a git-ignored .env file."
        )
    return SFMCConfig(
        subdomain=os.environ["SFMC_SUBDOMAIN"].strip(),
        client_id=os.environ["SFMC_CLIENT_ID"].strip(),
        client_secret=os.environ["SFMC_CLIENT_SECRET"].strip(),
        account_id=(os.environ.get("SFMC_ACCOUNT_ID") or "").strip() or None,
    )


def sandbox_db_path() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return Path(os.environ.get("SFMC_KIT_DB", DATA_DIR / "sandbox.db"))
