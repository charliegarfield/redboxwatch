"""openFEC API client (spec §3.1, §5).

Thin, cached wrapper over https://api.open.fec.gov/v1/. Responses are cached to
disk keyed by (path, params) so reruns during development don't burn the
api.data.gov rate limit. Pass ``use_cache=False`` to force-refresh.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterator

import httpx

from .util import sha256_text

BASE_URL = "https://api.open.fec.gov/v1"


class FECClient:
    def __init__(
        self,
        api_key: str,
        cache_dir: str | Path,
        *,
        user_agent: str = "RedBoxTracker/0.1",
        min_delay_seconds: float = 0.6,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_delay = min_delay_seconds
        self._last_request = 0.0
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": user_agent},
        )

    # ------------------------------------------------------------------
    def _cache_path(self, path: str, params: dict[str, Any]) -> Path:
        # api_key excluded from the cache key so rotating it doesn't bust cache.
        keyable = {k: v for k, v in sorted(params.items()) if k != "api_key"}
        digest = sha256_text(path + "?" + json.dumps(keyable, sort_keys=True))
        return self.cache_dir / f"{digest}.json"

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_delay:
            time.sleep(self.min_delay - elapsed)
        self._last_request = time.monotonic()

    def get(
        self, path: str, params: dict[str, Any] | None = None, *, use_cache: bool = True
    ) -> dict[str, Any]:
        """GET a single page from openFEC, with disk caching and throttling."""
        params = dict(params or {})
        cache_file = self._cache_path(path, params)
        if use_cache and cache_file.exists():
            return json.loads(cache_file.read_text())

        params["api_key"] = self.api_key
        url = f"{BASE_URL}{path}"
        max_attempts = 5
        resp = None
        for attempt in range(max_attempts):
            self._throttle()
            try:
                resp = self._client.get(url, params=params)
            except (httpx.TimeoutException, httpx.TransportError):
                # Transient network failure (read timeout, connection reset).
                # At scale these are routine — back off and retry rather than
                # crash the whole run.
                if attempt == max_attempts - 1:
                    raise
                time.sleep(min(2.0 * (2 ** attempt), 30.0))
                continue
            if resp.status_code != 429:
                break
            # Rate limited — honor Retry-After if present, else exponential backoff.
            retry_after = resp.headers.get("retry-after")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 2.0 * (2 ** attempt)
            if attempt < max_attempts - 1:
                time.sleep(min(delay, 60.0))
        resp.raise_for_status()
        data = resp.json()
        cache_file.write_text(json.dumps(data))
        return data

    # ------------------------------------------------------------------
    def paginate(
        self, path: str, params: dict[str, Any] | None = None, *, use_cache: bool = True
    ) -> Iterator[dict[str, Any]]:
        """Yield every result across page-based pagination endpoints.

        Works for the page/per_page endpoints (candidates, totals). Schedule E
        uses seek pagination and is handled separately in :meth:`schedule_e`.
        """
        params = dict(params or {})
        params.setdefault("per_page", 100)
        page = 1
        while True:
            params["page"] = page
            data = self.get(path, params, use_cache=use_cache)
            results = data.get("results", []) or []
            for row in results:
                yield row
            pagination = data.get("pagination", {}) or {}
            pages = pagination.get("pages") or 0
            if page >= pages or not results:
                break
            page += 1

    # ------------------------------------------------------------------
    # spec §3.1 population A
    def candidates(
        self,
        *,
        election_year: int,
        office: str,
        state: str | None = None,
        extra: dict[str, Any] | None = None,
        use_cache: bool = True,
    ) -> list[dict[str, Any]]:
        """Pull candidates for a cycle/office (optionally one state).

        ``candidate_status`` values: C=statutory candidate, F=future candidate,
        N=not yet a candidate, P=prior candidate. We keep C (active) by default.
        """
        params: dict[str, Any] = {
            "election_year": election_year,
            "office": office,
            "candidate_status": "C",
            "is_active_candidate": True,
            "has_raised_funds": True,   # cheap server-side drop of zero-fund filers
            "sort": "name",
            "per_page": 100,
        }
        if state:
            params["state"] = state
        if extra:
            params.update(extra)
        return list(self.paginate("/candidates/", params, use_cache=use_cache))

    def inactive_ids(
        self, candidate_ids: list[str], *, use_cache: bool = True
    ) -> set[str]:
        """Which of these candidate_ids the FEC marks ``candidate_inactive``.

        The flag marks a candidacy as withdrawn/superseded — most commonly a
        House member now running for Senate whose old H record still shows
        cycle money (committee transfers), e.g. Buddy Carter GA / Andy Barr KY.
        The API's /candidates/ search already excludes these via
        ``is_active_candidate``; this batch lookup brings the same signal to
        the bulk-file (weball) discovery path, which has no such column.
        """
        out: set[str] = set()
        ids = sorted(set(candidate_ids))
        for i in range(0, len(ids), 100):
            batch = ids[i:i + 100]
            for row in self.paginate(
                "/candidates/", {"candidate_id": batch, "per_page": 100},
                use_cache=use_cache,
            ):
                if row.get("candidate_inactive"):
                    out.add(row["candidate_id"])
        return out

    def candidate_totals(
        self, candidate_id: str, *, cycle: int, use_cache: bool = True
    ) -> dict[str, Any] | None:
        """Financial totals for a candidate; ``receipts`` is total money raised.

        NB: this endpoint has no ``election_year`` param — it filters on ``cycle``
        and ``election_full`` (verified against the openFEC swagger). We request
        the full-election consolidated row. Returns None when the candidate has
        no totals row for the cycle (a just-filed candidate who has not reported
        returns an empty ``results`` array, not a row of zeros).
        """
        params = {
            "cycle": cycle,
            "election_full": True,
            "per_page": 1,
            "sort": "-cycle",
        }
        data = self.get(
            f"/candidate/{candidate_id}/totals/", params, use_cache=use_cache
        )
        results = data.get("results", []) or []
        return results[0] if results else None

    # ------------------------------------------------------------------
    # spec §3.6 — Schedule E independent expenditures (used in phase 4).
    # This endpoint uses SEEK pagination, not page-based: carry last_index plus
    # the sort-keyed cursor (last_expenditure_date for the default sort).
    def schedule_e(
        self,
        *,
        candidate_id: str,
        cycle: int,
        support_oppose: str | None = None,
        use_cache: bool = True,
    ) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {
            "candidate_id": candidate_id,
            "cycle": cycle,
            "sort": "-expenditure_date",
            "per_page": 100,
        }
        if support_oppose:
            params["support_oppose_indicator"] = support_oppose
        prev_index = None
        while True:
            data = self.get("/schedules/schedule_e/", params, use_cache=use_cache)
            results = data.get("results", []) or []
            for row in results:
                yield row
            seek = (data.get("pagination", {}) or {}).get("last_indexes") or {}
            last_index = seek.get("last_index")
            # Stop on empty page, missing cursor, or a cursor that did not advance
            # (openFEC can repeat last_index on the final page -> guard against an
            # infinite loop / duplicate rows).
            if not results or not last_index or last_index == prev_index:
                break
            prev_index = last_index
            params = dict(params)
            params["last_index"] = last_index
            # Default sort is by expenditure_date -> carry last_expenditure_date.
            if "last_expenditure_date" in seek:
                params["last_expenditure_date"] = seek["last_expenditure_date"]

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "FECClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
