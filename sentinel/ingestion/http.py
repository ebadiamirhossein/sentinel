"""Shared async HTTP access: per-call timeout, max-2 retries with backoff.

specs/DATA_SOURCES.md §3 and CLAUDE.md both require this shape, so every REST
client goes through :class:`HttpFetcher` rather than hand-rolling its own policy.
Client errors (4xx) are not retried — only timeouts, transport errors and 5xx.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from sentinel.core.logging import get_logger
from sentinel.ingestion.errors import SourceUnavailable

log = get_logger(__name__)


class HttpFetcher:
    """Thin retry/timeout wrapper around one shared :class:`httpx.AsyncClient`."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
    ) -> None:
        self._client = client
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._backoff = backoff_seconds

    async def get_text(
        self,
        url: str,
        *,
        source: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        response = await self._get(url, source=source, params=params, headers=headers)
        return response.text

    async def get_json(
        self,
        url: str,
        *,
        source: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        response = await self._get(url, source=source, params=params, headers=headers)
        try:
            return response.json()
        except ValueError as exc:
            raise SourceUnavailable(source, f"invalid JSON: {exc}") from exc

    async def post_form(
        self,
        url: str,
        *,
        source: str,
        data: dict[str, str],
        auth: tuple[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """POST an ``application/x-www-form-urlencoded`` body and return the JSON.

        Added for M10b's OAuth token exchange, which is the first thing in this
        system that has to *write* to a vendor endpoint rather than read from one.

        Two properties matter and both are deliberate:

        * **Any 2xx is success.** Saxo answers the token exchange with ``201
          Created``, and journal/M10b_SPIKE.md records a bug where checking
          ``!= 200`` sent a perfectly successful exchange down the error branch —
          the credential was issued and thrown away, and the log said it had failed.
        * **The response body never reaches a log line.** It contains the tokens.
          ``SourceUnavailable`` is raised with the status code only.

        Retries follow the same policy as :meth:`get_json`: transport failures and
        5xx are retried, 4xx is not. A 400 on a refresh means the token is spent and
        hammering it cannot help.
        """
        response = await self._request(
            "POST", url, source=source, data=data, auth=auth, headers=headers
        )
        try:
            return response.json()
        except ValueError as exc:
            raise SourceUnavailable(source, f"invalid JSON: {exc}") from exc

    async def _get(
        self,
        url: str,
        *,
        source: str,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        return await self._request("GET", url, source=source, params=params, headers=headers)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        source: str,
        params: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        auth: tuple[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        attempts = self._max_retries + 1
        last_reason = "no attempt made"
        last_status: int | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    auth=auth or httpx.USE_CLIENT_DEFAULT,
                    headers=headers,
                    timeout=self._timeout,
                )
            except httpx.HTTPError as exc:
                last_reason = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code < 400:
                    return response
                last_reason = f"HTTP {response.status_code}"
                last_status = response.status_code
                if response.status_code < 500:
                    # A 4xx will not fix itself; fail fast instead of hammering.
                    break

            log.warning(
                "http.retry",
                source=source,
                url=url,
                attempt=attempt,
                max_attempts=attempts,
                reason=last_reason,
            )
            if attempt < attempts:
                await asyncio.sleep(self._backoff * attempt)

        raise SourceUnavailable(source, last_reason, status_code=last_status)
