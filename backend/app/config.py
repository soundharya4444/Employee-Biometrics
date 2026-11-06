"""Application configuration. All secrets come from environment variables / .env (never committed)."""
from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"  # development | production
    secret_key: str = "change-me"
    base_url: str = "http://localhost:8000"  # public URL of THIS backend API (used for the Google OAuth callback)
    frontend_url: str = "http://localhost:5173"  # public URL of the separate frontend (QR code target, post-login redirect)
    cookie_samesite: str = "lax"  # "lax" if frontend+backend share a registrable domain; "none" for cross-site (needs HTTPS)
    cookie_domain: str = ""  # e.g. ".gwcdata.ai" so app.gwcdata.ai and api.gwcdata.ai share the session cookie
    database_url: str = f"sqlite:///{(BASE_DIR / 'storage' / 'dev.db').as_posix()}"
    reports_dir: Path = BASE_DIR / "storage" / "reports"
    timezone: str = "Asia/Kolkata"

    # Identity
    allowed_email_domain: str = "gwcdata.ai"
    google_client_id: str = ""
    google_client_secret: str = ""
    dev_auth_enabled: bool = False  # local development only; refused when app_env=production
    # POC sign-in: e-mail + employee ID checked against the HR master list (no Google). NOT strong authentication.
    poc_auth_enabled: bool = False

    # Office-only access (no QR code: employees are given the page URL). Both rules apply to every API call.
    desktop_only: bool = True  # refuse phones and tablets
    office_ip_ranges: str = ""  # office PUBLIC IP/CIDRs, comma separated (e.g. 203.0.113.10,198.51.100.0/28). Empty = any network
    session_max_age_seconds: int = 12 * 3600
    admin_reauth_minutes: int = 60  # admins must have authenticated this recently
    cookie_secure: bool = False
    bootstrap_admin_emails: str = ""  # comma separated; seeded as SUPER_ADMIN at startup
    require_master_list: bool = True  # registration only for employees imported by HR

    # Employees may punch from any network (office, home, mobile data). The client IP is still recorded for audit and
    # used for rate limiting; X-Forwarded-For is honoured ONLY from these proxies (CIDRs, comma separated).
    trusted_proxies: str = ""

    # Attendance policy
    # Shifts and their late/early tolerances live in the shift_master table (HR edits the Shift Master sheet).
    min_seconds_between_punches: int = 30
    stale_session_hours: int = 18
    working_days: str = "0,1,2,3,4"  # Monday=0
    attendance_policy_file: str = ""  # optional HR-authored policy text (UTF-8); shown by the assistant verbatim

    # Proactive monitoring (Python scheduler; the LLM is only invoked when there are new findings)
    monitor_enabled: bool = False
    monitor_interval_minutes: int = 30
    assistant_rate_limit: int = 20  # chat messages per minute per client

    # Rate limiting
    punch_rate_limit: int = 10  # per minute per client
    auth_rate_limit: int = 20

    # Domo (nothing is assumed: endpoint path must be taken from your Domo docs)
    domo_instance_url: str = ""  # e.g. https://yourcompany.domo.com
    domo_developer_token: str = ""
    domo_auth_header: str = "X-DOMO-Developer-Token"
    domo_llm_endpoint_path: str = ""  # e.g. path of the Domo AI text-generation endpoint - verify in Domo docs
    domo_llm_model: str = ""
    domo_llm_input_field: str = "input"
    domo_llm_model_field: str = "model"
    domo_llm_response_field: str = "output"  # dot path into the JSON response
    domo_llm_timeout_seconds: int = 60
    agent_max_steps: int = 8

    @field_validator("allowed_email_domain")
    @classmethod
    def _dom(cls, v: str) -> str:
        return v.strip().lower().lstrip("@")

    # ---- derived helpers ----
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @staticmethod
    def _csv(v: str) -> list[str]:
        return [x.strip() for x in v.split(",") if x.strip()]

    @property
    def trusted_proxy_list(self):
        return [ipaddress.ip_network(x, strict=False) for x in self._csv(self.trusted_proxies)]

    @property
    def office_network_list(self):
        return [ipaddress.ip_network(x, strict=False) for x in self._csv(self.office_ip_ranges)]

    @property
    def bootstrap_admins(self) -> list[str]:
        return [x.lower() for x in self._csv(self.bootstrap_admin_emails)]

    @property
    def frontend_origins(self) -> list[str]:
        """The single origin (from FRONTEND_URL) allowed to call this API with credentials (CORS) and to submit state-changing requests (CSRF)."""
        from urllib.parse import urlparse
        out = []
        for u in [self.frontend_url]:
            p = urlparse(u.strip())
            if p.scheme and p.netloc:
                o = f"{p.scheme}://{p.netloc}"
                if o not in out:
                    out.append(o)
        return out

    @property
    def working_day_set(self) -> set[int]:
        return {int(x) for x in self._csv(self.working_days)}

    def validate_for_startup(self) -> None:
        if self.is_production:
            problems = []
            if self.secret_key in ("", "change-me") or len(self.secret_key) < 32:
                problems.append("SECRET_KEY must be a random value of at least 32 characters")
            if self.dev_auth_enabled:
                problems.append("DEV_AUTH_ENABLED must be false in production")
            if not self.cookie_secure:
                problems.append("COOKIE_SECURE must be true in production (HTTPS)")
            if not self.poc_auth_enabled and not (self.google_client_id and self.google_client_secret):
                problems.append("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are required in production")
            if self.database_url.startswith("sqlite"):
                problems.append("Use PostgreSQL in production")
            if self.office_ip_ranges and not self.trusted_proxies:
                problems.append("OFFICE_IP_RANGES needs TRUSTED_PROXIES so the real client IP is visible behind the host's proxy")
            if self.cookie_samesite.lower() not in ("lax", "none"):
                problems.append("COOKIE_SAMESITE must be lax or none")
            if self.cookie_samesite.lower() == "none" and not self.cookie_secure:
                problems.append("COOKIE_SAMESITE=none requires COOKIE_SECURE=true")
            if not self.frontend_url.startswith("https://"):
                problems.append("FRONTEND_URL must be https:// in production")
            if problems:
                raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()
