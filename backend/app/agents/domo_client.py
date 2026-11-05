"""Domo LLM provider adapter.

NOTHING about the Domo API is assumed here. The endpoint path, auth header, request field names and
response field are all configuration (see .env.example). If they are not set, `is_configured` is False and
the agent reports exactly what is missing instead of silently using another model.

Verify against YOUR Domo instance's AI/LLM documentation which endpoint is enabled for your account
(and whether it is the text-generation service, an AI agent service, or a Domo-hosted model).
"""
from __future__ import annotations

import httpx

from app.config import Settings


class DomoError(Exception):
    pass


class DomoClient:
    provider_name = "domo"

    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.s = settings
        self._http = http

    def missing_config(self) -> list[str]:
        s = self.s
        missing = []
        if not s.domo_instance_url:
            missing.append("DOMO_INSTANCE_URL")
        if not s.domo_developer_token:
            missing.append("DOMO_DEVELOPER_TOKEN (or whichever credential your Domo AI service requires)")
        if not s.domo_llm_endpoint_path:
            missing.append("DOMO_LLM_ENDPOINT_PATH (from your Domo AI API documentation)")
        return missing

    @property
    def is_configured(self) -> bool:
        return not self.missing_config()

    def generate(self, prompt: str) -> str:
        if not self.is_configured:
            raise DomoError("Domo LLM is not configured: " + ", ".join(self.missing_config()))
        s = self.s
        url = s.domo_instance_url.rstrip("/") + "/" + s.domo_llm_endpoint_path.lstrip("/")
        body: dict = {s.domo_llm_input_field: prompt}
        if s.domo_llm_model:
            body[s.domo_llm_model_field] = s.domo_llm_model
        headers = {s.domo_auth_header: s.domo_developer_token, "Content-Type": "application/json"}
        try:
            client = self._http or httpx.Client(timeout=s.domo_llm_timeout_seconds)
            resp = client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise DomoError(f"Domo request failed: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise DomoError(f"Domo returned HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise DomoError("Domo returned non-JSON response") from exc
        cur = data
        for part in s.domo_llm_response_field.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            else:
                keys = list(data) if isinstance(data, dict) else type(data).__name__
                raise DomoError(f"Response field '{s.domo_llm_response_field}' not found; top-level keys: {keys}")
        if not isinstance(cur, str):
            raise DomoError("Domo response field is not text")
        return cur
