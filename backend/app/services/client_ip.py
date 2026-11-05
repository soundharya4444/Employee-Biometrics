"""Client IP resolution (for audit records and rate limiting only - there is no network-based access rule).

X-Forwarded-For is only honoured when the direct TCP peer is a configured trusted proxy, and the chain is
walked right-to-left so client-supplied (spoofed) left-most entries are ignored.
"""
from __future__ import annotations

import ipaddress

from fastapi import Request

from app.config import Settings


def _parse(ip: str | None):
    try:
        return ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return None


def resolve_client_ip(peer: str | None, xff: str | None, trusted) -> tuple[str | None, bool]:
    peer_ip = _parse(peer)
    if peer_ip is None:
        return None, False
    if not any(peer_ip in n for n in trusted) or not xff:
        return str(peer_ip), False
    for part in reversed([p.strip() for p in xff.split(",") if p.strip()]):
        ip = _parse(part)
        if ip is None:
            return None, True  # malformed chain -> unknown
        if not any(ip in n for n in trusted):
            return str(ip), True
    return str(peer_ip), True


def get_client_ip(request: Request, settings: Settings) -> str | None:
    peer = request.client.host if request.client else None
    return resolve_client_ip(peer, request.headers.get("x-forwarded-for"), settings.trusted_proxy_list)[0]
