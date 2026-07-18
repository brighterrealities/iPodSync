"""Persistent sync state (SQLite at /config/state.sqlite).

Maps each source file to the on-iPod track it produced, so subsequent runs copy
only genuinely-new/changed files. On the very first run against an iTunes-built DB
there is no mapping yet; sync.py bootstraps it by matching source files to existing
iPod tracks by identity (see engine.ipod.identity_key).
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS synced (
    src_path   TEXT PRIMARY KEY,
    src_mtime  REAL NOT NULL,
    src_size   INTEGER NOT NULL,
    identity   TEXT NOT NULL,
    ipod_path  TEXT,
    added_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_synced_identity ON synced(identity);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


@dataclass
class SyncedRow:
    src_path: str
    src_mtime: float
    src_size: int
    identity: str
    ipod_path: Optional[str]


class State:
    def __init__(self, path: str):
        self.path = path
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    # --- lookups ------------------------------------------------------------
    def get(self, src_path: str) -> Optional[SyncedRow]:
        r = self.con.execute(
            "SELECT src_path, src_mtime, src_size, identity, ipod_path FROM synced WHERE src_path=?",
            (src_path,),
        ).fetchone()
        return _row(r) if r else None

    def all_paths(self) -> set[str]:
        return {r[0] for r in self.con.execute("SELECT src_path FROM synced")}

    def by_identity(self, identity: str) -> Optional[SyncedRow]:
        r = self.con.execute(
            "SELECT src_path, src_mtime, src_size, identity, ipod_path FROM synced WHERE identity=? LIMIT 1",
            (identity,),
        ).fetchone()
        return _row(r) if r else None

    # --- mutations ----------------------------------------------------------
    def upsert(self, src_path: str, mtime: float, size: int, identity: str, ipod_path: Optional[str]) -> None:
        self.con.execute(
            """INSERT INTO synced(src_path, src_mtime, src_size, identity, ipod_path, added_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(src_path) DO UPDATE SET
                 src_mtime=excluded.src_mtime, src_size=excluded.src_size,
                 identity=excluded.identity, ipod_path=excluded.ipod_path""",
            (src_path, mtime, size, identity, ipod_path, time.time()),
        )

    def delete(self, src_paths: Iterable[str]) -> None:
        self.con.executemany("DELETE FROM synced WHERE src_path=?", ((p,) for p in src_paths))

    def commit(self) -> None:
        self.con.commit()

    # --- meta ---------------------------------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        self.con.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.con.commit()

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        r = self.con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default


def _row(r: sqlite3.Row) -> SyncedRow:
    return SyncedRow(
        src_path=r["src_path"], src_mtime=r["src_mtime"], src_size=r["src_size"],
        identity=r["identity"], ipod_path=r["ipod_path"],
    )
