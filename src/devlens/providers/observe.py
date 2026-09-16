"""Observability providers.

Every method returns a :class:`ProviderResult`, which keeps three things apart
that a list of strings cannot: whether the provider answered, whether it had
anything to say, and what it said. That separation is what stops a connection
error from being filed as evidence.

Each provider gets its own resilience policy, so one failing backend cannot
open the circuit for the others.
"""

from __future__ import annotations

import os
import re
import time
from urllib.parse import urlsplit

import httpx

from devlens.domain import AccessDenied, ProviderError, ProviderResult
from devlens.providers.http import get_json, request_json
from devlens.resilience import CircuitBreaker, RateLimiter, ResiliencePolicy, attach

# --------------------------------------------------------------------------- #
# SQL safety
# --------------------------------------------------------------------------- #

SELECT = re.compile(r"^\s*select\b", re.I)
MUTATION = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|"
    r"execute|call|merge|vacuum|analyze|reindex|cluster|listen|notify|"
    r"lock|begin|commit|rollback|savepoint|do|prepare|deallocate|set|reset)\b"
    # ``SELECT ... INTO OUTFILE`` writes a file without any of the verbs above.
    r"|\binto\s+(outfile|dumpfile|out\s+file)\b"
    r"|\binto\s+[A-Za-z_\"][^\s;]*\s+from\b",
    re.I,
)
FUNCTION = re.compile(r"\b([A-Za-z_][A-Za-z0-9_\.]*)\s*\(")

#: SQL keywords that can be followed by "(" and would otherwise be read as
#: function calls: ``WITHIN GROUP (...)``, ``IN (...)``, ``OVER (...)``.
#: Listing them keeps the function allowlist strict without rejecting ordinary
#: analytical SQL.
KEYWORDS: frozenset[str] = frozenset(
    {
        "all", "and", "any", "as", "between", "by", "case", "cast", "distinct",
        "else", "end", "exists", "from", "group", "having", "in", "is", "join",
        "like", "limit", "not", "offset", "on", "or", "order", "partition",
        "returning", "select", "some", "then", "union", "using", "values",
        "when", "where", "window", "with", "within",
    }
)

#: Functions a read-only analytical query legitimately needs. Anything else is
#: refused, because "it is a SELECT statement" is not the same as "it has no
#: side effects": ``SELECT pg_read_file('/etc/passwd')`` is a SELECT.
SAFE_FUNCTIONS: frozenset[str] = frozenset(
    {
        "abs", "avg", "cast", "ceil", "ceiling", "coalesce", "concat", "count",
        "date", "date_part", "date_trunc", "extract", "floor", "greatest",
        "least", "length", "lower", "ltrim", "max", "min", "mod", "now",
        "nullif", "percentile_cont", "percentile_disc", "position", "power",
        "rank", "dense_rank", "round", "row_number", "rtrim", "split_part",
        "sqrt", "stddev", "substr", "substring", "sum", "to_char", "to_date",
        "to_number", "to_timestamp", "trim", "trunc", "upper", "variance",
        "json_extract", "jsonb_extract_path_text", "array_agg", "string_agg",
        "lag", "lead", "first_value", "last_value", "over", "filter", "case",
    }
)

#: Named so the refusal message can be specific about why.
DANGEROUS_FUNCTIONS: frozenset[str] = frozenset(
    {
        "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
        "lo_import", "lo_export", "dblink", "dblink_exec", "pg_sleep",
        "pg_terminate_backend", "pg_cancel_backend", "xp_cmdshell",
        "openrowset", "opendatasource", "load_file", "sys_exec", "sys_eval",
        "copy_to", "query_to_xml", "pg_logdir_ls",
    }
)


def select_only(sql: str) -> str:
    """Accept a single side-effect-free SELECT, or refuse with a reason.

    This is defence in depth, not the security boundary. The gateway credential
    must also be a read-only role — DevLens states that requirement rather than
    pretending string parsing is sufficient.
    """
    query = (sql or "").strip()
    if not query:
        raise AccessDenied("An empty query cannot be executed.")
    if len(query) > 8000:
        raise AccessDenied("Query exceeds the 8000 character limit.")
    if not SELECT.search(query):
        raise AccessDenied("Only a statement beginning with SELECT is allowed.")
    body = query.rstrip(";")
    if ";" in body:
        raise AccessDenied("Only a single statement is allowed.")
    if MUTATION.search(body):
        raise AccessDenied("Only a read-only SELECT is allowed.")
    for name in FUNCTION.findall(body):
        bare = name.lower().rsplit(".", 1)[-1]
        if bare in KEYWORDS:
            continue
        if bare in DANGEROUS_FUNCTIONS:
            raise AccessDenied(
                f"Function {bare}() can have side effects and is not allowed."
            )
        if bare not in SAFE_FUNCTIONS:
            raise AccessDenied(
                f"Function {bare}() is not on the read-only allowlist."
            )
    return body


# --------------------------------------------------------------------------- #
# Hub
# --------------------------------------------------------------------------- #


def _policy() -> ResiliencePolicy:
    return ResiliencePolicy(RateLimiter(10, 20), CircuitBreaker(5, 30.0), timeout=20.0)


def _client(url_key: str, token_key: str) -> httpx.AsyncClient | None:
    url = os.environ.get(url_key, "").strip()
    if not url:
        return None
    parsed = urlsplit(url)
    local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not local:
        raise ValueError(
            f"{url_key} must use HTTPS; a bearer token would otherwise be sent "
            "in clear text."
        )
    token = os.environ.get(token_key, "").strip()
    client = httpx.AsyncClient(
        base_url=url.rstrip("/") + "/",
        headers={"Authorization": f"Bearer {token}"} if token else {},
        timeout=httpx.Timeout(15.0, connect=5.0),
        follow_redirects=False,
    )
    attach(client, _policy())
    return client


class ObserveHub:
    """Live telemetry, with availability reported separately from content."""

    def __init__(self, loki=None, prometheus=None, tempo=None, sql=None):
        self.loki = loki
        self.prometheus = prometheus
        self.tempo = tempo
        self.sql = sql

    @property
    def configured(self) -> bool:
        return any((self.loki, self.prometheus, self.tempo, self.sql))

    @classmethod
    def from_env(cls) -> ObserveHub:
        return cls(
            loki=_client("DEVLENS_LOKI_URL", "DEVLENS_LOKI_TOKEN"),
            prometheus=_client("DEVLENS_PROM_URL", "DEVLENS_PROM_TOKEN"),
            tempo=_client("DEVLENS_TEMPO_URL", "DEVLENS_TEMPO_TOKEN"),
            sql=_client("DEVLENS_SQL_URL", "DEVLENS_SQL_TOKEN"),
        )

    async def aclose(self) -> None:
        for client in (self.loki, self.prometheus, self.tempo, self.sql):
            if client is not None:
                await client.aclose()

    # -- individual providers --------------------------------------------- #

    async def search_logs(self, query: str, window_seconds: int = 3600) -> ProviderResult:
        end, start = time.time(), time.time() - window_seconds
        if self.loki is None:
            return _absent("Loki", query)
        try:
            data = await get_json(
                self.loki,
                "loki/api/v1/query_range",
                params={
                    "query": query,
                    "limit": 50,
                    "start": int(start * 1e9),
                    "end": int(end * 1e9),
                    "direction": "backward",
                },
            )
        except ProviderError as exc:
            return _failed("Loki", query, exc, start, end)
        rows: list[str] = []
        payload = data.get("data") or data
        for stream in payload.get("result") or []:
            for entry in (stream.get("values") or [])[:10]:
                rows.append(str(entry[-1]))
        return _answered("Loki", query, rows, start, end, limit=50)

    async def query_metrics(
        self, query: str, window_seconds: int = 3600
    ) -> ProviderResult:
        end, start = time.time(), time.time() - window_seconds
        if self.prometheus is None:
            return _absent("Prometheus", query)
        step = max(15, window_seconds // 60)
        try:
            data = await get_json(
                self.prometheus,
                "api/v1/query_range",
                params={"query": query, "start": start, "end": end, "step": step},
            )
        except ProviderError as exc:
            return _failed("Prometheus", query, exc, start, end)
        rows = []
        payload = data.get("data") or data
        for item in (payload.get("result") or [])[:20]:
            metric = item.get("metric") or {}
            values = item.get("values") or []
            if values:
                first, last = values[0][-1], values[-1][-1]
                rows.append(f"{metric} first={first} last={last} points={len(values)}")
            elif item.get("value"):
                rows.append(f"{metric} = {item['value'][-1]}")
        return _answered("Prometheus", query, rows, start, end, limit=20)

    async def search_traces(
        self, query: str, window_seconds: int = 3600
    ) -> ProviderResult:
        end, start = time.time(), time.time() - window_seconds
        if self.tempo is None:
            return _absent("Tempo", query)
        try:
            data = await get_json(
                self.tempo,
                "api/search",
                params={"q": query, "start": int(start), "end": int(end), "limit": 20},
            )
        except ProviderError as exc:
            return _failed("Tempo", query, exc, start, end)
        traces = data.get("traces") or data.get("data") or []
        rows = []
        for item in traces[:20]:
            if isinstance(item, dict):
                name = item.get("rootTraceName") or item.get("name") or ""
                ident = item.get("traceID") or item.get("trace_id") or ""
                duration = item.get("durationMs")
                rows.append(
                    f"{ident} {name}".strip()
                    + (f" {duration}ms" if duration is not None else "")
                )
            else:
                rows.append(str(item))
        return _answered("Tempo", query, rows, start, end, limit=20)

    async def query_sql(self, sql: str) -> ProviderResult:
        if self.sql is None:
            return _absent("SQL gateway", sql)
        try:
            safe = select_only(sql)
        except AccessDenied as exc:
            return ProviderResult(
                provider="SQL gateway", status="DENIED", error=str(exc), query=sql
            )
        try:
            data = await request_json(self.sql, "POST", "", json={"sql": safe})
        except ProviderError as exc:
            return _failed("SQL gateway", safe, exc, None, None)
        rows = [str(row) for row in (data.get("rows") or data.get("result") or [])[:50]]
        return _answered("SQL gateway", safe, rows, None, None, limit=50)

    # -- aggregate --------------------------------------------------------- #

    async def collect(
        self, query: str, window_seconds: int = 3600
    ) -> dict[str, ProviderResult]:
        """Query every provider. Each result carries its own availability.

        A provider that is down yields ``UNAVAILABLE`` and contributes nothing
        to any hypothesis; a provider that answers with nothing yields
        ``EMPTY``. Neither can become supporting evidence.
        """
        text = (query or "").strip()
        return {
            "log": await self.search_logs(text or '{job=~".+"}', window_seconds),
            "metric": await self.query_metrics(text or "up", window_seconds),
            "trace": await self.search_traces(text, window_seconds),
            "sql": (
                await self.query_sql(text)
                if SELECT.search(text)
                else ProviderResult(
                    provider="SQL gateway",
                    status="NOT_CONFIGURED" if self.sql is None else "EMPTY",
                    query=None,
                    error=None,
                )
            ),
        }


# --------------------------------------------------------------------------- #
# Result constructors
# --------------------------------------------------------------------------- #


def _absent(provider: str, query: str | None) -> ProviderResult:
    return ProviderResult(provider=provider, status="NOT_CONFIGURED", query=query or None)


def _failed(
    provider: str,
    query: str | None,
    exc: Exception,
    start: float | None,
    end: float | None,
) -> ProviderResult:
    message = str(exc)
    status = "TIMEOUT" if "time budget" in message or "timed out" in message else "UNAVAILABLE"
    return ProviderResult(
        provider=provider,
        status=status,  # type: ignore[arg-type]
        error=message,
        query=query or None,
        window_start=start,
        window_end=end,
    )


def _answered(
    provider: str,
    query: str | None,
    rows: list[str],
    start: float | None,
    end: float | None,
    *,
    limit: int,
) -> ProviderResult:
    if not rows:
        return ProviderResult(
            provider=provider,
            status="EMPTY",
            query=query or None,
            window_start=start,
            window_end=end,
        )
    return ProviderResult(
        provider=provider,
        status="AVAILABLE",
        rows=rows[:limit],
        query=query or None,
        window_start=start,
        window_end=end,
        truncated=len(rows) >= limit,
    )
