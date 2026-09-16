"""HTTP helpers shared by every provider.

Responses are streamed with a byte ceiling so a hostile or simply enormous
response cannot exhaust memory, upstream errors are sanitised into
:class:`ProviderError` so status codes and bodies never leak into a report, and
rate-limit responses are surfaced as a distinct, actionable message rather than
a generic failure.
"""

from __future__ import annotations

import json
import re

import httpx

from devlens.domain import ProviderError

LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="next"', re.I)


async def _read_bytes(
    client: httpx.AsyncClient,
    path: str,
    *,
    max_bytes: int,
    method: str = "GET",
    capture: dict | None = None,
    **kwargs,
) -> bytes:
    try:
        async with client.stream(method, path, **kwargs) as response:
            if capture is not None:
                capture["headers"] = dict(response.headers)
                capture["status"] = response.status_code
            if response.status_code in {403, 429}:
                await response.aread()
                retry = response.headers.get("retry-after")
                remaining = response.headers.get("x-ratelimit-remaining")
                if response.status_code == 429 or remaining == "0":
                    hint = f" Retry after {retry}s." if retry else ""
                    raise ProviderError(f"Provider rate limit reached.{hint}")
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > max_bytes:
                    raise ProviderError(
                        f"Provider response exceeds the {max_bytes // 1000} kB limit."
                    )
            return bytes(content)
    except httpx.HTTPStatusError as exc:
        raise ProviderError(
            f"Provider request failed (HTTP {exc.response.status_code})."
        ) from None
    except httpx.TooManyRedirects:
        raise ProviderError("Provider returned too many redirects.") from None
    except httpx.RequestError:
        raise ProviderError("Provider could not be reached.") from None


async def request_bytes(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    max_bytes: int = 8_000_000,
    capture: dict | None = None,
    **kwargs,
) -> bytes:
    policy = getattr(client, "resilience", None)
    if policy is None:
        return await _read_bytes(
            client, path, max_bytes=max_bytes, method=method, capture=capture, **kwargs
        )
    return await policy.call(
        lambda: _read_bytes(
            client, path, max_bytes=max_bytes, method=method, capture=capture, **kwargs
        )
    )


async def get_bytes(
    client: httpx.AsyncClient, path: str, *, max_bytes: int = 8_000_000, **kwargs
) -> bytes:
    return await request_bytes(client, "GET", path, max_bytes=max_bytes, **kwargs)


async def get_json(client: httpx.AsyncClient, path: str, **kwargs) -> dict:
    data = await get_json_value(client, path, **kwargs)
    if not isinstance(data, dict):
        raise ProviderError("Provider returned invalid JSON.")
    return data


async def get_json_list(client: httpx.AsyncClient, path: str, **kwargs) -> list:
    data = await get_json_value(client, path, **kwargs)
    if not isinstance(data, list):
        raise ProviderError("Provider returned invalid JSON.")
    return data


async def get_json_value(client: httpx.AsyncClient, path: str, **kwargs):
    return await request_json_value(client, "GET", path, **kwargs)


async def request_json_value(client: httpx.AsyncClient, method: str, path: str, **kwargs):
    raw = await request_bytes(client, method, path, **kwargs)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise ProviderError("Provider returned invalid JSON.") from None


async def request_json(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> dict:
    data = await request_json_value(client, method, path, **kwargs)
    if data == {} and method != "GET":
        return {}
    if not isinstance(data, dict):
        raise ProviderError("Provider returned invalid JSON.")
    return data


async def paginate_link(
    client: httpx.AsyncClient,
    path: str,
    *,
    max_pages: int = 10,
    params: dict | None = None,
) -> tuple[list, bool]:
    """Follow ``Link: rel="next"`` up to ``max_pages``.

    Returns the accumulated items and whether more pages remain. Callers record
    that flag as a :class:`~devlens.domain.Truncation` rather than discarding it:
    a review of the first 100 files of a 400-file pull request must say so.
    """
    items: list = []
    url: str = path
    query = params
    base = client.base_url
    for _ in range(max_pages):
        capture: dict = {}
        raw = await request_bytes(
            client, "GET", url, params=query, capture=capture, max_bytes=8_000_000
        )
        query = None
        try:
            page = json.loads(raw) if raw else []
        except (ValueError, UnicodeError):
            raise ProviderError("Provider returned invalid JSON.") from None
        if not isinstance(page, list):
            raise ProviderError("Provider returned invalid JSON.")
        items.extend(page)
        link = (capture.get("headers") or {}).get("link", "")
        match = LINK_NEXT.search(link)
        if not match:
            return items, False
        candidate = httpx.URL(match.group(1))
        if (candidate.scheme, candidate.host) != (base.scheme, base.host):
            raise ProviderError("Provider returned an off-host pagination link.")
        url = str(candidate)
    return items, True


async def paginate_values(
    client: httpx.AsyncClient,
    path: str,
    *,
    max_pages: int = 10,
    params: dict | None = None,
) -> tuple[list, bool]:
    """Follow Atlassian-style ``next`` URLs, validating each hop stays on host."""
    items: list = []
    url: str = path
    query = params
    base = client.base_url
    seen: set[str] = set()
    for _ in range(max_pages):
        data = await get_json(client, url, params=query)
        query = None
        values = data.get("values")
        if not isinstance(values, list):
            raise ProviderError("Provider returned a malformed page.")
        items.extend(values)
        nxt = data.get("next")
        if not nxt:
            return items, False
        candidate = httpx.URL(str(nxt))
        if (
            candidate.username
            or candidate.password
            or candidate.fragment
            or (candidate.scheme, candidate.host, candidate.port)
            != (base.scheme, base.host, base.port)
        ):
            raise ProviderError("Provider returned an unsafe pagination link.")
        key = str(candidate)
        if key in seen:
            raise ProviderError("Provider returned cyclic pagination.")
        seen.add(key)
        url = key
    return items, True
