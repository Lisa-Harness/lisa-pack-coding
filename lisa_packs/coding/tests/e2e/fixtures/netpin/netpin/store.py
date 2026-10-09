"""SQLite-backed reservation store.

A reservation pins a MAC to an IPv4 address (and optionally a hostname).
The MAC is the primary key: one reservation per device.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from netpin.macs import normalize_hostname, normalize_ip, normalize_mac

SCHEMA = """
CREATE TABLE IF NOT EXISTS reservations (
    mac        TEXT PRIMARY KEY,
    ip         TEXT NOT NULL,
    hostname   TEXT NOT NULL DEFAULT '',
    note       TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS reservations_ip ON reservations (ip);
"""


class NotFound(LookupError):
    """No reservation exists for the MAC."""


class AlreadyExists(ValueError):
    """A reservation already exists for the MAC."""


@dataclass(frozen=True)
class Reservation:
    mac: str
    ip: str
    hostname: str
    note: str
    created_at: int
    updated_at: int

    def to_json(self) -> dict[str, object]:
        return {
            "mac": self.mac,
            "ip": self.ip,
            "hostname": self.hostname,
            "note": self.note,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _row(row: sqlite3.Row) -> Reservation:
    return Reservation(
        mac=row["mac"],
        ip=row["ip"],
        hostname=row["hostname"],
        note=row["note"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class ReservationStore:
    """Thread-safe CRUD over the ``reservations`` table."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._conn:
            yield self._conn

    # ── reads ───────────────────────────────────────────────────────────

    def list(self) -> list[Reservation]:
        with self._tx() as conn:
            rows = conn.execute("SELECT * FROM reservations ORDER BY ip").fetchall()
        return [_row(r) for r in rows]

    def get(self, mac: str) -> Reservation:
        key = normalize_mac(mac)
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM reservations WHERE mac = ?", (key,)).fetchone()
        if row is None:
            raise NotFound(key)
        return _row(row)

    def by_ip(self, ip: str) -> list[Reservation]:
        key = normalize_ip(ip)
        with self._tx() as conn:
            rows = conn.execute("SELECT * FROM reservations WHERE ip = ?", (key,)).fetchall()
        return [_row(r) for r in rows]

    # ── writes ──────────────────────────────────────────────────────────

    def create(self, mac: str, ip: str, hostname: str = "", note: str = "") -> Reservation:
        key = normalize_mac(mac)
        addr = normalize_ip(ip)
        name = normalize_hostname(hostname)
        stamp = int(time.time())
        try:
            with self._tx() as conn:
                conn.execute(
                    "INSERT INTO reservations (mac, ip, hostname, note, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (key, addr, name, note, stamp, stamp),
                )
        except sqlite3.IntegrityError as exc:
            raise AlreadyExists(key) from exc
        return self.get(key)

    def update(self, mac: str, ip: str, hostname: str = "", note: str = "") -> Reservation:
        key = normalize_mac(mac)
        addr = normalize_ip(ip)
        name = normalize_hostname(hostname)
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE reservations SET ip = ?, hostname = ?, note = ?, updated_at = ?"
                " WHERE mac = ?",
                (addr, name, note, int(time.time()), key),
            )
        if cur.rowcount == 0:
            raise NotFound(key)
        return self.get(key)

    def delete(self, mac: str) -> None:
        key = normalize_mac(mac)
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM reservations WHERE mac = ?", (key,))
        if cur.rowcount == 0:
            raise NotFound(key)
