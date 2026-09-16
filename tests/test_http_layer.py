"""The HTTP layer every provider shares.

This is the code that decides what an upstream system is allowed to do to
DevLens: how much it can return, how long it gets, what it may say about its
own errors, and where it may send the next request. The tests are written from
the upstream's point of view — each one is a thing a hostile or broken provider
might try.
"""

from __future__ import annotations

import httpx
import pytest

from devlens.domain import ProviderError
from devlens.providers.http import (
    get_bytes,
    get_json,
    get_json_list,
    paginate_link,
    paginate_values,
    request_json,
)
from devlens.resilience import CircuitBreaker, RateLimiter, ResiliencePolicy, attach


def client_for(handler, base: str = "https://api.example.com/") -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base, transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# What an upstream may return
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_rate_limited_response_names_the_retry_delay():
    async with client_for(
        lambda request: httpx.Response(429, headers={"retry-after": "42"}, json={})
    ) as client:
        with pytest.raises(ProviderError, match=r"rate limit reached\. Retry after 42s"):
            await get_json(client, "x")


@pytest.mark.asyncio
async def test_a_rate_limited_response_without_a_delay_still_says_so():
    async with client_for(lambda request: httpx.Response(429, json={})) as client:
        with pytest.raises(ProviderError, match="rate limit reached"):
            await get_json(client, "x")


@pytest.mark.asyncio
async def test_a_403_with_no_remaining_quota_is_reported_as_a_rate_limit():
    """GitHub signals an exhausted quota as a 403, not a 429."""
    async with client_for(
        lambda request: httpx.Response(
            403, headers={"x-ratelimit-remaining": "0"}, json={}
        )
    ) as client:
        with pytest.raises(ProviderError, match="rate limit reached"):
            await get_json(client, "x")


@pytest.mark.asyncio
async def test_an_ordinary_403_is_reported_as_a_failed_request():
    async with client_for(lambda request: httpx.Response(403, json={})) as client:
        with pytest.raises(ProviderError, match=r"HTTP 403"):
            await get_json(client, "x")


@pytest.mark.asyncio
async def test_a_redirect_loop_is_refused():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://api.example.com/loop"})

    client = httpx.AsyncClient(
        base_url="https://api.example.com/",
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        max_redirects=2,
    )
    async with client:
        with pytest.raises(ProviderError, match="too many redirects"):
            await get_json(client, "x")


@pytest.mark.asyncio
async def test_an_unreachable_host_says_so_without_leaking_the_address():
    def handler(request):
        raise httpx.ConnectError("[Errno -2] Name or service not known: secret.internal")

    async with client_for(handler) as client:
        with pytest.raises(ProviderError) as caught:
            await get_json(client, "x")
    assert "secret.internal" not in str(caught.value)
    assert "could not be reached" in str(caught.value)


@pytest.mark.asyncio
async def test_a_response_larger_than_the_ceiling_is_refused_mid_stream():
    def handler(request):
        return httpx.Response(200, content=b"x" * 2_000_000)

    async with client_for(handler) as client:
        with pytest.raises(ProviderError, match="exceeds the 100 kB limit"):
            await get_bytes(client, "x", max_bytes=100_000)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"[]", b'"a string"', b"123", b"null"])
async def test_a_payload_that_is_not_an_object_is_invalid_json(body):
    async with client_for(lambda request: httpx.Response(200, content=body)) as client:
        with pytest.raises(ProviderError, match="invalid JSON"):
            await get_json(client, "x")


@pytest.mark.asyncio
async def test_a_payload_that_is_not_a_list_is_invalid_json():
    async with client_for(lambda request: httpx.Response(200, json={})) as client:
        with pytest.raises(ProviderError, match="invalid JSON"):
            await get_json_list(client, "x")


@pytest.mark.asyncio
async def test_malformed_json_is_reported_as_malformed():
    async with client_for(lambda request: httpx.Response(200, content=b"{oops")) as client:
        with pytest.raises(ProviderError, match="invalid JSON"):
            await get_json(client, "x")


@pytest.mark.asyncio
async def test_an_empty_body_is_an_empty_object():
    async with client_for(lambda request: httpx.Response(200, content=b"")) as client:
        assert await get_json(client, "x") == {}


@pytest.mark.asyncio
async def test_a_write_that_answers_with_nothing_is_accepted():
    """A 204 from a comment endpoint is success, not a malformed response."""
    async with client_for(lambda request: httpx.Response(201, content=b"")) as client:
        assert await request_json(client, "POST", "x", json={"body": "hi"}) == {}


@pytest.mark.asyncio
async def test_a_write_that_answers_with_a_list_is_malformed():
    async with client_for(lambda request: httpx.Response(200, json=[1, 2])) as client:
        with pytest.raises(ProviderError, match="invalid JSON"):
            await request_json(client, "POST", "x", json={})


# --------------------------------------------------------------------------- #
# Where an upstream may send the next request
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_link_pagination_follows_the_host_it_was_given():
    pages = {1: [{"n": 1}], 2: [{"n": 2}], 3: [{"n": 3}]}

    def handler(request):
        page = int(request.url.params.get("page", 1))
        headers = (
            {"Link": f'<https://api.example.com/x?page={page + 1}>; rel="next"'}
            if page < 3
            else {}
        )
        return httpx.Response(200, json=pages[page], headers=headers)

    async with client_for(handler) as client:
        items, more = await paginate_link(client, "x")
    assert [item["n"] for item in items] == [1, 2, 3]
    assert more is False


@pytest.mark.asyncio
async def test_link_pagination_refuses_a_page_that_is_not_a_list():
    async with client_for(lambda request: httpx.Response(200, json={"not": "a list"})) as client:
        with pytest.raises(ProviderError, match="invalid JSON"):
            await paginate_link(client, "x")


@pytest.mark.asyncio
async def test_link_pagination_refuses_a_malformed_page():
    async with client_for(lambda request: httpx.Response(200, content=b"{oops")) as client:
        with pytest.raises(ProviderError, match="invalid JSON"):
            await paginate_link(client, "x")


@pytest.mark.asyncio
async def test_value_pagination_follows_a_same_host_next_link():
    def handler(request):
        page = int(request.url.params.get("page", 1))
        body = {"values": [{"n": page}]}
        if page < 3:
            body["next"] = f"https://api.example.com/x?page={page + 1}"
        return httpx.Response(200, json=body)

    async with client_for(handler) as client:
        items, more = await paginate_values(client, "x")
    assert [item["n"] for item in items] == [1, 2, 3]
    assert more is False


@pytest.mark.asyncio
async def test_value_pagination_refuses_a_cycle():
    def handler(request):
        return httpx.Response(
            200, json={"values": [{"n": 1}], "next": "https://api.example.com/x?page=2"}
        )

    async with client_for(handler) as client:
        with pytest.raises(ProviderError, match="cyclic pagination"):
            await paginate_values(client, "x")


@pytest.mark.asyncio
async def test_value_pagination_refuses_a_malformed_page():
    async with client_for(lambda request: httpx.Response(200, json={"values": "nope"})) as client:
        with pytest.raises(ProviderError, match="malformed page"):
            await paginate_values(client, "x")


@pytest.mark.asyncio
async def test_value_pagination_refuses_a_fragment_in_the_next_link():
    async with client_for(
        lambda request: httpx.Response(
            200, json={"values": [], "next": "https://api.example.com/x#frag"}
        )
    ) as client:
        with pytest.raises(ProviderError, match="unsafe pagination link"):
            await paginate_values(client, "x")


@pytest.mark.asyncio
async def test_value_pagination_stops_at_its_page_ceiling():
    def handler(request):
        page = int(request.url.params.get("page", 1))
        return httpx.Response(
            200,
            json={"values": [{"n": page}], "next": f"https://api.example.com/x?page={page + 1}"},
        )

    async with client_for(handler) as client:
        items, more = await paginate_values(client, "x", max_pages=3)
    assert len(items) == 3
    assert more is True


# --------------------------------------------------------------------------- #
# The resilience policy, attached to a client
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_client_carries_its_policy_into_every_request():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(500, json={})

    client = client_for(handler)
    attach(
        client,
        ResiliencePolicy(RateLimiter(100, 100), CircuitBreaker(2, 30.0), timeout=5.0),
    )
    async with client:
        for _ in range(10):
            with pytest.raises(ProviderError):
                await get_json(client, "x")
    assert calls <= 4, f"the circuit did not stop the retries ({calls} calls)"


@pytest.mark.asyncio
async def test_a_policy_timeout_is_reported_as_a_provider_error():
    import asyncio

    async def handler(request):
        await asyncio.sleep(2)
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(
        base_url="https://api.example.com/", transport=httpx.MockTransport(handler)
    )
    attach(
        client,
        ResiliencePolicy(RateLimiter(100, 100), CircuitBreaker(5, 30.0), timeout=0.05),
    )
    async with client:
        with pytest.raises(ProviderError):
            await get_json(client, "x")


def test_the_captured_headers_are_available_to_the_caller():
    """`capture` is how pagination reads Link without buffering the body twice."""
    captured: dict = {}

    async def run():
        async with client_for(
            lambda request: httpx.Response(200, json={}, headers={"link": "<x>; rel=next"})
        ) as client:
            await get_json(client, "x", capture=captured)

    import asyncio

    asyncio.run(run())
    assert captured["status"] == 200
    assert "link" in captured["headers"]
