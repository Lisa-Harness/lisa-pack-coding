"""Read-only view over dnsmasq's DHCP lease file.

dnsmasq writes one lease per line::

    <expiry-epoch> <mac> <ip> <hostname|*> <client-id|*>

An expiry of ``0`` means the lease never expires. dnsmasq rewrites the file
in place, so every read re-parses it; nothing is cached.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from netpin.macs import ValidationError, in_subnet, normalize_hostname, normalize_ip, normalize_mac

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Lease:
    """One parsed lease line."""

    expires: int
    mac: str
    ip: str
    hostname: str
    client_id: str

    def to_json(self) -> dict[str, object]:
        return {
            "expires": self.expires,
            "mac": self.mac,
            "ip": self.ip,
            "hostname": self.hostname,
            "client_id": self.client_id,
        }


def parse_line(line: str) -> Lease | None:
    """Parse one lease line; return ``None`` for blank or malformed lines."""
    parts = line.split()
    if len(parts) < 4:
        return None
    try:
        expires = int(parts[0])
        mac = normalize_mac(parts[1])
        ip = normalize_ip(parts[2])
        hostname = normalize_hostname(parts[3])
    except (ValueError, ValidationError):
        logger.debug("skipping malformed lease line: %r", line)
        return None
    client_id = parts[4] if len(parts) > 4 and parts[4] != "*" else ""
    return Lease(expires=expires, mac=mac, ip=ip, hostname=hostname, client_id=client_id)


class LeaseFile:
    """Parses the lease file at ``path`` on every call."""

    def __init__(self, path: Path, *, subnet: str | None = None) -> None:
        self.path = Path(path)
        self.subnet = subnet

    def _lines(self) -> list[str]:
        try:
            return self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            logger.warning("lease file %s does not exist; treating as empty", self.path)
            return []

    def all(self) -> list[Lease]:
        """Every well-formed lease in the file, filtered to ``subnet`` when set."""
        out: list[Lease] = []
        for line in self._lines():
            lease = parse_line(line)
            if lease is None:
                continue
            if not in_subnet(lease.ip, self.subnet):
                continue
            out.append(lease)
        return out

    def by_mac(self, mac: str) -> list[Lease]:
        """Every lease for ``mac`` (normalized), expired or not."""
        wanted = normalize_mac(mac)
        return [lease for lease in self.all() if lease.mac == wanted]

    def by_ip(self, ip: str) -> list[Lease]:
        """Every lease for ``ip``, expired or not."""
        wanted = normalize_ip(ip)
        return [lease for lease in self.all() if lease.ip == wanted]


def now() -> int:
    """Current epoch seconds (a seam tests can patch)."""
    return int(time.time())
