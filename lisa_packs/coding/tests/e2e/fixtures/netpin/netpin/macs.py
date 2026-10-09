"""MAC and IPv4 address validation and normalization.

Every MAC that enters the system — from the API, the lease file, or the
reservation store — is normalized to lowercase, colon-separated form so
comparisons are exact string comparisons.
"""

from __future__ import annotations

import ipaddress
import re

_MAC_RE = re.compile(r"^[0-9a-f]{2}([:-]?)[0-9a-f]{2}(\1[0-9a-f]{2}){4}$")
_HOSTNAME_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


class ValidationError(ValueError):
    """Raised when a caller-supplied value is malformed."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


def normalize_mac(value: str) -> str:
    """Return ``value`` as ``aa:bb:cc:dd:ee:ff``; raise ``ValidationError`` if malformed.

    Accepts colon, dash, or bare-hex forms in any case.
    """
    if not isinstance(value, str):
        raise ValidationError("mac", "must be a string")
    raw = value.strip().lower()
    if not _MAC_RE.match(raw):
        raise ValidationError("mac", f"not a MAC address: {value!r}")
    digits = raw.replace(":", "").replace("-", "")
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def normalize_ip(value: str) -> str:
    """Return ``value`` as a canonical dotted-quad IPv4 address."""
    if not isinstance(value, str):
        raise ValidationError("ip", "must be a string")
    try:
        addr = ipaddress.IPv4Address(value.strip())
    except ipaddress.AddressValueError as exc:
        raise ValidationError("ip", f"not an IPv4 address: {value!r}") from exc
    if addr.is_multicast or addr.is_unspecified or addr.is_loopback:
        raise ValidationError("ip", f"not a usable host address: {value!r}")
    return str(addr)


def normalize_hostname(value: str | None) -> str:
    """Return a lowercase DNS label, or ``""`` when no hostname is given."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationError("hostname", "must be a string")
    name = value.strip().lower()
    if name in ("", "*"):
        return ""
    if not _HOSTNAME_RE.match(name):
        raise ValidationError("hostname", f"not a valid DNS label: {value!r}")
    return name


def in_subnet(ip: str, subnet: str | None) -> bool:
    """Return whether ``ip`` lies inside ``subnet`` (always true when no subnet)."""
    if not subnet:
        return True
    return ipaddress.IPv4Address(ip) in ipaddress.IPv4Network(subnet, strict=False)
