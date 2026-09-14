import json
import httpx

from devlens.domain import ProviderError


async def get_bytes(
    client: httpx.AsyncClient, path: str, *, max_bytes: int = 8_000_000, **kwargs
) -> bytes:
    try:
        async with client.stream("GET", path, **kwargs) as response:
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > max_bytes:
                    raise ProviderError("Provider response exceeds the supported size.")
            return bytes(content)
    except httpx.HTTPStatusError as exc:
        raise ProviderError(
            f"Provider request failed (HTTP {exc.response.status_code})."
        ) from None
    except httpx.RequestError:
        raise ProviderError("Provider could not be reached.") from None


async def get_json(client: httpx.AsyncClient, path: str, **kwargs) -> dict:
    try:
        data = json.loads(await get_bytes(client, path, **kwargs))
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (ValueError, UnicodeError):
        raise ProviderError("Provider returned invalid JSON.") from None
