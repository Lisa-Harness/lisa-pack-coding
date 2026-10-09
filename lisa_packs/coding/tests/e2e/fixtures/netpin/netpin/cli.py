"""Process wiring: build the app from paths and run the HTTP server."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from netpin.api import App, make_server
from netpin.hostdir import HostDir
from netpin.leases import LeaseFile
from netpin.service import ReservationService
from netpin.store import ReservationStore


def build_app(
    *,
    db_path: Path | str,
    lease_path: Path | str,
    hostdir_path: Path | str,
    token: str,
    subnet: str | None = None,
) -> App:
    """Wire store, lease file, host dir, and service into an ``App``."""
    leases = LeaseFile(Path(lease_path), subnet=subnet)
    service = ReservationService(
        ReservationStore(db_path), HostDir(hostdir_path), leases, subnet=subnet
    )
    return App(service, leases, token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="netpin")
    parser.add_argument("--db", default="netpin.db")
    parser.add_argument("--leases", default="/var/lib/misc/dnsmasq.leases")
    parser.add_argument("--hostdir", default="/etc/dnsmasq.hosts.d")
    parser.add_argument("--subnet", default=None, help="only manage this IPv4 network")
    parser.add_argument("--listen", default="127.0.0.1:8067")
    args = parser.parse_args(argv)
    token = os.environ.get("NETPIN_TOKEN", "")
    if not token:
        parser.error("NETPIN_TOKEN must be set")
    logging.basicConfig(level=logging.INFO)
    host, _, port = args.listen.rpartition(":")
    app = build_app(
        db_path=args.db,
        lease_path=args.leases,
        hostdir_path=args.hostdir,
        token=token,
        subnet=args.subnet,
    )
    server = make_server(app, host or "127.0.0.1", int(port))
    logging.getLogger(__name__).info("listening on %s", args.listen)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
