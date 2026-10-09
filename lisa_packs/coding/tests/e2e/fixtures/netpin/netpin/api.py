"""HTTP API over the reservation service and the lease file.

Routes (all JSON; every route except ``GET /health`` needs
``Authorization: Bearer <token>``):

    GET    /health
    GET    /leases
    GET    /leases/<mac>
    GET    /reservations
    POST   /reservations             body: {"mac", "ip", "hostname"?, "note"?}
    GET    /reservations/<mac>
    PUT    /reservations/<mac>       body: {"ip", "hostname"?, "note"?}
    DELETE /reservations/<mac>

Errors are ``{"error": <code>, "detail": <text>}`` with the status codes in
``_ERROR_STATUS``.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from netpin.leases import LeaseFile
from netpin.macs import ValidationError, normalize_mac
from netpin.service import ReservationRequest, ReservationService
from netpin.store import AlreadyExists, NotFound

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 16 * 1024

_ERROR_STATUS: dict[str, HTTPStatus] = {
    "bad_request": HTTPStatus.BAD_REQUEST,
    "unauthorized": HTTPStatus.UNAUTHORIZED,
    "not_found": HTTPStatus.NOT_FOUND,
    "method_not_allowed": HTTPStatus.METHOD_NOT_ALLOWED,
    "already_reserved": HTTPStatus.CONFLICT,
    "invalid": HTTPStatus.UNPROCESSABLE_ENTITY,
    "too_large": HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
}

_ROUTES: list[tuple[str, re.Pattern[str], str]] = [
    ("GET", re.compile(r"^/health$"), "health"),
    ("GET", re.compile(r"^/leases$"), "list_leases"),
    ("GET", re.compile(r"^/leases/(?P<mac>[^/]+)$"), "get_lease"),
    ("GET", re.compile(r"^/reservations$"), "list_reservations"),
    ("POST", re.compile(r"^/reservations$"), "create_reservation"),
    ("GET", re.compile(r"^/reservations/(?P<mac>[^/]+)$"), "get_reservation"),
    ("PUT", re.compile(r"^/reservations/(?P<mac>[^/]+)$"), "update_reservation"),
    ("DELETE", re.compile(r"^/reservations/(?P<mac>[^/]+)$"), "delete_reservation"),
]


class ApiError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

    @property
    def status(self) -> HTTPStatus:
        return _ERROR_STATUS.get(self.code, HTTPStatus.INTERNAL_SERVER_ERROR)

    def body(self) -> dict[str, Any]:
        return {"error": self.code, "detail": self.detail}


class App:
    """Route handlers; independent of the HTTP server so tests can call them."""

    def __init__(self, service: ReservationService, leases: LeaseFile, token: str) -> None:
        self.service = service
        self.leases = leases
        self.token = token

    # ── plumbing ────────────────────────────────────────────────────────

    def authorize(self, header: str | None) -> None:
        scheme, _, value = (header or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(value.strip(), self.token):
            raise ApiError("unauthorized", "missing or invalid bearer token")

    def dispatch(
        self, method: str, path: str, auth: str | None, body: bytes
    ) -> tuple[HTTPStatus, Any]:
        allowed = False
        for route_method, pattern, name in _ROUTES:
            match = pattern.match(path)
            if match is None:
                continue
            allowed = True
            if route_method != method:
                continue
            try:
                if name != "health":
                    self.authorize(auth)
                payload = _decode(body) if method in ("POST", "PUT") else None
                return getattr(self, name)(payload, **match.groupdict())
            except ApiError as exc:
                return exc.status, exc.body()
            except ValidationError as exc:
                return ApiError("invalid", str(exc)).status, ApiError("invalid", str(exc)).body()
            except NotFound as exc:
                return HTTPStatus.NOT_FOUND, ApiError("not_found", str(exc)).body()
            except AlreadyExists as exc:
                err = ApiError("already_reserved", f"{exc} already has a reservation")
                return err.status, err.body()
        if allowed:
            err = ApiError("method_not_allowed", f"{method} {path}")
        else:
            err = ApiError("not_found", path)
        return err.status, err.body()

    # ── handlers ────────────────────────────────────────────────────────

    def health(self, _payload: Any) -> tuple[HTTPStatus, Any]:
        return HTTPStatus.OK, {"status": "ok"}

    def list_leases(self, _payload: Any) -> tuple[HTTPStatus, Any]:
        return HTTPStatus.OK, [lease.to_json() for lease in self.leases.all()]

    def get_lease(self, _payload: Any, mac: str) -> tuple[HTTPStatus, Any]:
        found = self.leases.by_mac(mac)
        if not found:
            raise ApiError("not_found", f"no lease for {normalize_mac(mac)}")
        return HTTPStatus.OK, [lease.to_json() for lease in found]

    def list_reservations(self, _payload: Any) -> tuple[HTTPStatus, Any]:
        return HTTPStatus.OK, [r.to_json() for r in self.service.list()]

    def get_reservation(self, _payload: Any, mac: str) -> tuple[HTTPStatus, Any]:
        return HTTPStatus.OK, self.service.get(mac).to_json()

    def create_reservation(self, payload: Any, **_: str) -> tuple[HTTPStatus, Any]:
        req = _request(payload, mac=None)
        return HTTPStatus.CREATED, self.service.create(req).to_json()

    def update_reservation(self, payload: Any, mac: str) -> tuple[HTTPStatus, Any]:
        req = _request(payload, mac=mac)
        return HTTPStatus.OK, self.service.update(req).to_json()

    def delete_reservation(self, _payload: Any, mac: str) -> tuple[HTTPStatus, Any]:
        self.service.delete(mac)
        return HTTPStatus.NO_CONTENT, None


def _decode(body: bytes) -> dict[str, Any]:
    if len(body) > MAX_BODY_BYTES:
        raise ApiError("too_large", f"body exceeds {MAX_BODY_BYTES} bytes")
    try:
        data = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise ApiError("bad_request", f"invalid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ApiError("bad_request", "body must be a JSON object")
    return data


def _request(payload: dict[str, Any], *, mac: str | None) -> ReservationRequest:
    body_mac = payload.get("mac")
    if mac is None:
        if not body_mac:
            raise ValidationError("mac", "is required")
        mac = str(body_mac)
    elif body_mac and normalize_mac(str(body_mac)) != normalize_mac(mac):
        raise ValidationError("mac", "body MAC does not match the URL")
    ip = payload.get("ip")
    note = payload.get("note", "")
    if not isinstance(note, str):
        raise ValidationError("note", "must be a string")
    return ReservationRequest(
        mac=normalize_mac(mac),
        ip=None if ip is None else str(ip),
        hostname=payload.get("hostname"),
        note=note,
    )


class _Handler(BaseHTTPRequestHandler):
    app: App

    def _serve(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        status, payload = self.app.dispatch(
            self.command, self.path.split("?", 1)[0], self.headers.get("Authorization"), body
        )
        data = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if data:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = _serve  # noqa: N815

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("%s " + fmt, self.address_string(), *args)


def make_server(app: App, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """Bind a server for ``app``; ``port=0`` picks a free port."""
    handler = type("Handler", (_Handler,), {"app": app})
    return ThreadingHTTPServer((host, port), handler)


def serve_in_thread(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread
