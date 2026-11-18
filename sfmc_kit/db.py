"""Local sandbox DB (SQLite): build log, asset lineage and a mirror of the sample data.

The *mock SFMC tenant* lives in a separate file (data/mock_sfmc.db) so that the sandbox's
records of what was built in live mode never mix with simulated tenant state.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR, sandbox_db_path
from .modules.data_extensions import DESpec

SCHEMA = """
CREATE TABLE IF NOT EXISTS build_runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, mode TEXT, brief TEXT,
  started_at TEXT, finished_at TEXT, status TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS objects (
  code TEXT, mode TEXT, type TEXT, key TEXT, name TEXT, sfmc_id TEXT, extra TEXT, updated_at TEXT,
  PRIMARY KEY (code, mode, type, key));
CREATE TABLE IF NOT EXISTS lineage (
  code TEXT, mode TEXT, parent_type TEXT, parent_key TEXT, child_type TEXT, child_key TEXT, relation TEXT,
  PRIMARY KEY (code, mode, parent_type, parent_key, child_type, child_key, relation));
"""


class Sandbox:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else sandbox_db_path()
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def start_run(self, code: str, mode: str, brief: str) -> int:
        cur = self.db.execute("INSERT INTO build_runs (code, mode, brief, started_at, status) VALUES (?,?,?,?,?)",
                              (code, mode, brief, datetime.now().isoformat(timespec="seconds"), "running"))
        self.db.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, error: str | None = None) -> None:
        self.db.execute("UPDATE build_runs SET finished_at=?, status=?, error=? WHERE run_id=?",
                        (datetime.now().isoformat(timespec="seconds"), status, error, run_id))
        self.db.commit()

    def record(self, code: str, mode: str, type_: str, key: str, name: str, sfmc_id: str | None,
               extra: dict | None = None) -> None:
        self.db.execute("INSERT OR REPLACE INTO objects VALUES (?,?,?,?,?,?,?,?)",
                        (code, mode, type_, key, name, None if sfmc_id is None else str(sfmc_id),
                         json.dumps(extra or {}, default=str), datetime.now().isoformat(timespec="seconds")))
        self.db.commit()

    def link(self, code: str, mode: str, parent: tuple[str, str], child: tuple[str, str], relation: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO lineage VALUES (?,?,?,?,?,?,?)",
                        (code, mode, parent[0], parent[1], child[0], child[1], relation))
        self.db.commit()

    def objects(self, code: str, mode: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM objects WHERE code=? AND mode=? ORDER BY updated_at", (code, mode))]

    def lineage(self, code: str, mode: str) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM lineage WHERE code=? AND mode=?", (code, mode))]

    def forget(self, code: str, mode: str) -> None:
        self.db.execute("DELETE FROM objects WHERE code=? AND mode=?", (code, mode))
        self.db.execute("DELETE FROM lineage WHERE code=? AND mode=?", (code, mode))
        self.db.commit()

    def mirror_table(self, spec: DESpec, rows: list[dict]) -> None:
        """Keep a local copy of sample data (handy for ad-hoc SQL and for diffing against SFMC)."""
        name = f"sample_{spec.name}"
        self.db.execute(f'DROP TABLE IF EXISTS "{name}"')
        cols = ", ".join(f'"{f.name}" {f.sqlite_type}' for f in spec.fields)
        self.db.execute(f'CREATE TABLE "{name}" ({cols})')
        fields = [f.name for f in spec.fields]
        self.db.executemany(f'INSERT INTO "{name}" VALUES ({",".join("?" * len(fields))})',
                            [tuple(r.get(f) for f in fields) for r in rows])
        self.db.commit()


def mock_db_path() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "mock_sfmc.db"
