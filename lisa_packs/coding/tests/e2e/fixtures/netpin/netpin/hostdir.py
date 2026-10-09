"""Publishes reservations to dnsmasq as ``--dhcp-hostsdir`` files.

Each reservation becomes ``<dir>/<mac-with-dashes>.conf`` containing one
``dhcp-host=`` line. dnsmasq re-reads the directory on change (inotify), so
a file written here takes effect without a restart. Writes are atomic
(temp file + rename) so dnsmasq never reads a half-written file.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from netpin.store import Reservation

logger = logging.getLogger(__name__)


def host_file_name(mac: str) -> str:
    return mac.replace(":", "-") + ".conf"


def dhcp_host_line(reservation: Reservation) -> str:
    fields = [reservation.mac, reservation.ip]
    if reservation.hostname:
        fields.append(reservation.hostname)
    return "dhcp-host=" + ",".join(fields) + "\n"


class HostDir:
    """Owns the dhcp-hostsdir directory."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)

    def file_for(self, mac: str) -> Path:
        return self.path / host_file_name(mac)

    def publish(self, reservation: Reservation) -> Path:
        target = self.file_for(reservation.mac)
        fd, tmp = tempfile.mkstemp(dir=self.path, prefix=".tmp-", suffix=".conf")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(dhcp_host_line(reservation))
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        logger.info("published %s -> %s", reservation.mac, target.name)
        return target

    def withdraw(self, mac: str) -> None:
        self.file_for(mac).unlink(missing_ok=True)

    def published(self) -> dict[str, str]:
        """Map of file name → contents for every published host file."""
        return {
            entry.name: entry.read_text(encoding="utf-8")
            for entry in sorted(self.path.glob("*.conf"))
        }
