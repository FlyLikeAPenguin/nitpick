from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

log = logging.getLogger(__name__)


class DatadogSource:
    """Datadog Logs API search, parameterised by site domain.

    Each instance represents a single region/site. The caller is responsible
    for iterating over regions if multiple are configured.
    """

    def __init__(self, region: str, api_key: str, app_key: str, site: str = ""):
        self._region = region
        self._api_key = api_key
        self._app_key = app_key
        # Default site domain based on region for backward compatibility
        if not site:
            site = "datadoghq.eu" if region == "eu" else "datadoghq.com"
        self._api_url = f"https://api.{site}/api/v2/logs/events/search"

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key and self._app_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "DD-API-KEY": self._api_key,
            "DD-APPLICATION-KEY": self._app_key,
        }

    def fetch(
        self,
        query: str,
        lookback_seconds: int = 3600,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Search Datadog logs. Returns raw log dicts."""
        if not self.is_configured:
            log.warning("No DD credentials for region %s, skipping", self._region)
            return []

        to_time = datetime.now(timezone.utc)
        from_time = to_time - timedelta(seconds=lookback_seconds)

        logs: list[dict[str, Any]] = []
        cursor: str | None = None

        while len(logs) < limit:
            body: dict[str, Any] = {
                "filter": {
                    "from": from_time.isoformat(),
                    "to": to_time.isoformat(),
                    "query": query,
                },
                "page": {"limit": min(1000, limit - len(logs))},
                "sort": "timestamp",
            }
            if cursor:
                body["page"]["cursor"] = cursor

            try:
                resp = requests.post(
                    self._api_url, headers=self._headers(), json=body, timeout=30
                )
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("x-ratelimit-reset", "5"))
                    log.warning("DD rate limited (%s), waiting %ds", self._region, retry_after)
                    time.sleep(retry_after)
                    continue
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException as e:
                log.error("DD API request failed (%s): %s", self._region, e)
                break

            if "data" in data:
                logs.extend(data["data"])

            page_meta = data.get("meta", {}).get("page", {})
            cursor = page_meta.get("after")
            if not cursor:
                break

        log.info("Fetched %d logs from DD %s for query: %s", len(logs), self._region, query[:80])
        return logs[:limit]
