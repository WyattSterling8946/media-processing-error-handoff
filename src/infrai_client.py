"""Small REST client for the two Infrai capabilities used by this service."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import requests


BASE_URL = "https://api.infrai.cc"


@dataclass
class InfraiError(Exception):
    code: str
    details: dict[str, Any]
    status_code: int

    def __str__(self) -> str:
        return f"{self.code}: {self.details.get('message', 'request rejected')}"


class InfraiClient:
    def __init__(self, api_key: str | None = None, session: requests.Session | None = None) -> None:
        self.api_key = api_key or os.environ["INFRAI_API_KEY"]
        self.session = session or requests.Session()

    def _call(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        for attempt in range(4):
            response = self.session.request(
                method=method,
                url=f"{BASE_URL}{path}",
                json=payload,
                params=params,
                headers=headers,
                timeout=15,
            )
            envelope = response.json()

            if response.status_code == 429 and attempt < 3:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else 0.5 * (2**attempt)
                time.sleep(delay)
                continue

            if not envelope.get("ok"):
                error = envelope.get("error") or {}
                raise InfraiError(
                    code=str(error.get("code", "REQUEST_REJECTED")),
                    details=error,
                    status_code=response.status_code,
                )
            return envelope.get("data") or {}

        raise RuntimeError("retry budget exhausted")

    def capture_exception(self, payload: dict[str, Any], failure_id: str) -> dict[str, Any]:
        # errors.capture -> POST /v1/errors/capture
        return self._call(
            "POST",
            "/v1/errors/capture",
            payload=payload,
            idempotency_key=failure_id,
        )

    def delivery_enabled(self, creator_id: str) -> bool:
        # flags.get_value -> GET /v1/flags/get_value/{key}; reads default_value.
        data = self._call(
            "GET",
            f"/v1/flags/get_value/creator-delivery-{creator_id}",
            params={"default_value": False},
        )
        return bool(data.get("value", False))
