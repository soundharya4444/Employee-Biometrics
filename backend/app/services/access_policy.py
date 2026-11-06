"""Office-only access policy: the app may be used only from desktop/laptop browsers on the office network.

* Device rule  - phones and tablets are refused (User-Agent + Client Hints). This is a convenience check: a user can
                 fake a User-Agent, so it is NOT a security boundary on its own.
* Network rule - the request must come from one of OFFICE_IP_RANGES (the office's public/egress IP or CIDR). This is the
                 real control, and it needs a correct client IP (see TRUSTED_PROXIES).
"""
from __future__ import annotations

import ipaddress
import re

from fastapi import Request

from app.config import Settings
from app.services.client_ip import get_client_ip

_MOBILE_UA = re.compile(
    r"Android|iPhone|iPad|iPod|Mobile|Tablet|Silk|Kindle|PlayBook|BlackBerry|BB10|webOS|Opera Mini|Opera Mobi|IEMobile|"
    r"SM-T\d|Nexus 7|Nexus 9|Xoom", re.I)


# The rules apply ONLY to the employee attendance features. Sign-in and the whole HR/admin portal (/admin/...) are open
# from any device and any network (permanent decision).
RESTRICTED_PREFIXES = ("/api/attendance", "/api/remarks", "/api/assistant")


def is_restricted_path(path: str) -> bool:
    return path.startswith(RESTRICTED_PREFIXES)


def is_mobile_or_tablet(request: Request) -> bool:
    if request.headers.get("sec-ch-ua-mobile", "").strip() == "?1":
        return True
    return bool(_MOBILE_UA.search(request.headers.get("user-agent", "")))


def check_access(request: Request, settings: Settings) -> tuple[str, str | None] | None:
    """None = allowed, else (reason_code, client_ip)."""
    if settings.desktop_only and is_mobile_or_tablet(request):
        return "DEVICE", None
    nets = settings.office_network_list
    if nets:
        ip = get_client_ip(request, settings)
        try:
            ok = ip is not None and any(ipaddress.ip_address(ip) in n for n in nets)
        except ValueError:
            ok = False
        if not ok:
            return "NETWORK", ip
    return None


MESSAGES = {
    "DEVICE": "Restricted: attendance can only be used from your office computer, not from a mobile phone or tablet.",
    "NETWORK": "Restricted: attendance can only be used from the office network (connect your office computer to the office Wi-Fi).",
}
