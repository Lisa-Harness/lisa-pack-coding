"""Reservation workflows: validate, persist, then publish to dnsmasq.

The service is the only writer of both the store and the host directory, so
the two never drift: a reservation is published only after it is stored,
and withdrawn only after it is deleted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from netpin.hostdir import HostDir
from netpin.leases import LeaseFile
from netpin.macs import ValidationError, in_subnet, normalize_ip, normalize_mac
from netpin.store import Reservation, ReservationStore

logger = logging.getLogger(__name__)


@dataclass
class ReservationRequest:
    """A create/update request after JSON decoding, before validation."""

    mac: str
    ip: str | None = None
    hostname: str | None = None
    note: str = ""


class ReservationService:
    def __init__(
        self,
        store: ReservationStore,
        hostdir: HostDir,
        leases: LeaseFile,
        *,
        subnet: str | None = None,
    ) -> None:
        self.store = store
        self.hostdir = hostdir
        self.leases = leases
        self.subnet = subnet

    def _check_subnet(self, ip: str) -> str:
        addr = normalize_ip(ip)
        if not in_subnet(addr, self.subnet):
            raise ValidationError("ip", f"{addr} is outside the managed subnet {self.subnet}")
        return addr

    def list(self) -> list[Reservation]:
        return self.store.list()

    def get(self, mac: str) -> Reservation:
        return self.store.get(mac)

    def create(self, req: ReservationRequest) -> Reservation:
        if req.ip is None:
            raise ValidationError("ip", "is required")
        addr = self._check_subnet(req.ip)
        reservation = self.store.create(req.mac, addr, req.hostname or "", req.note)
        self.hostdir.publish(reservation)
        logger.info("reserved %s -> %s", reservation.mac, reservation.ip)
        return reservation

    def update(self, req: ReservationRequest) -> Reservation:
        if req.ip is None:
            raise ValidationError("ip", "is required")
        addr = self._check_subnet(req.ip)
        reservation = self.store.update(req.mac, addr, req.hostname or "", req.note)
        self.hostdir.publish(reservation)
        logger.info("updated %s -> %s", reservation.mac, reservation.ip)
        return reservation

    def delete(self, mac: str) -> None:
        self.store.delete(mac)
        self.hostdir.withdraw(normalize_mac(mac))
        logger.info("released %s", mac)
