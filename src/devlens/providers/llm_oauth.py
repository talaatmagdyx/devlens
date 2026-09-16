import base64
import json
import os
import time
from pathlib import Path

import httpx

from devlens.domain import ProviderError

CLAUDE_BETA = "oauth-2025-04-20"
CODEX_BACKEND = "https://chatgpt.com/backend-api/codex"


def opted_in() -> bool:
    return os.environ.get("DEVLENS_LLM_OAUTH", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def env_token(*keys: str) -> str:
    for key in keys:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def claude_api_key() -> str:
    return env_token("DEVLENS_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")


def claude_oauth_token() -> str:
    return env_token("DEVLENS_CLAUDE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")


def codex_api_key() -> str:
    return env_token("DEVLENS_CODEX_API_KEY", "DEVLENS_LLM_API_KEY")


def codex_oauth_token() -> str:
    return env_token("DEVLENS_CODEX_OAUTH_TOKEN", "CODEX_ACCESS_TOKEN")


def claude_ready() -> bool:
    return bool(claude_api_key() or resolve_claude_oauth())


def claude_oauth_ready() -> bool:
    return bool(resolve_claude_oauth())


def codex_ready() -> bool:
    return bool(codex_api_key() or resolve_codex_oauth())


def codex_oauth_ready() -> bool:
    return bool(resolve_codex_oauth())


def auth_methods() -> dict[str, str | None]:
    return {
        "openai": "api_key" if env_token("DEVLENS_LLM_API_KEY") else None,
        "claude": "api_key" if claude_api_key() else ("oauth" if claude_oauth_ready() else None),
        "codex": "api_key" if env_token("DEVLENS_CODEX_API_KEY", "DEVLENS_LLM_API_KEY") else (
            "oauth" if codex_oauth_ready() else None
        ),
    }


def resolve_claude_oauth() -> str:
    token = claude_oauth_token()
    if token:
        return token
    for path in claude_paths():
        token = token_from_claude_file(path)
        if token:
            return token
    return ""


def resolve_codex_oauth() -> dict | None:
    token = codex_oauth_token()
    if token:
        return {
            "token": token,
            "account_id": env_token("DEVLENS_CODEX_ACCOUNT_ID") or jwt_account_id(token),
        }
    for path in codex_paths():
        creds = parse_codex(read_json(path) or {})
        if creds and creds["mode"] == "oauth" and creds["token"]:
            return creds
    return None


def resolve_codex() -> dict | None:
    key = env_token("DEVLENS_CODEX_API_KEY") or env_token("DEVLENS_LLM_API_KEY")
    if key:
        return {"mode": "api_key", "token": key, "account_id": ""}
    oauth = resolve_codex_oauth()
    if oauth:
        return {"mode": "oauth", **oauth}
    for path in codex_paths():
        creds = parse_codex(read_json(path) or {})
        if creds and creds["token"]:
            return creds
    return None


def claude_paths() -> list[Path]:
    explicit = os.environ.get("DEVLENS_CLAUDE_CREDENTIALS", "").strip()
    if explicit:
        return [Path(explicit)]
    if not opted_in():
        return []
    profile = os.environ.get("ANTHROPIC_PROFILE", "default").strip() or "default"
    roots = []
    config_dir = os.environ.get("ANTHROPIC_CONFIG_DIR", "").strip()
    if config_dir:
        roots.append(Path(config_dir))
    roots.append(Path.home() / ".config" / "anthropic")
    paths = [root / "credentials" / f"{profile}.json" for root in roots]
    paths.append(Path.home() / ".claude" / ".credentials.json")
    return paths


def codex_paths() -> list[Path]:
    explicit = os.environ.get("DEVLENS_CODEX_AUTH", "").strip()
    if explicit:
        return [Path(explicit)]
    if not opted_in():
        return []
    home = os.environ.get("CODEX_HOME", "").strip()
    root = Path(home) if home else Path.home() / ".codex"
    return [root / "auth.json"]


def token_from_claude_file(path: Path) -> str:
    data = read_json(path)
    if data is None:
        return ""
    creds = parse_claude(data)
    token = creds.get("access_token") or ""
    expires = creds.get("expires_at")
    can_refresh = bool(creds.get("refresh_token") and creds.get("client_id"))
    if token and (expires is None or time.time() < expires or not can_refresh):
        return token
    if not can_refresh:
        return token
    refreshed = refresh_claude(
        creds,
        base_url=os.environ.get("DEVLENS_ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        path=path,
        original=data,
    )
    return refreshed or token


def parse_claude(data: dict) -> dict:
    source = data.get("claudeAiOauth") or data.get("claude_ai_oauth")
    if not isinstance(source, dict):
        source = data
    token = _first(source, "access_token", "accessToken")
    refresh = _first(source, "refresh_token", "refreshToken")
    client_id = _first(source, "client_id", "clientId") or env_token(
        "DEVLENS_CLAUDE_OAUTH_CLIENT_ID"
    )
    return {
        "access_token": token,
        "refresh_token": refresh,
        "client_id": client_id,
        "expires_at": coerce_expires(
            source.get("expires_at", source.get("expiresAt"))
        ),
    }


def parse_codex(data: dict) -> dict | None:
    if not data:
        return None
    mode = str(data.get("auth_mode") or data.get("authMode") or "").lower()
    raw_tokens = data.get("tokens")
    tokens: dict = raw_tokens if isinstance(raw_tokens, dict) else {}
    access = _first(tokens, "access_token", "accessToken") or _first(
        data, "access_token", "accessToken"
    )
    account = (
        _first(tokens, "account_id", "accountId")
        or _first(data, "account_id", "accountId")
        or env_token("DEVLENS_CODEX_ACCOUNT_ID")
        or jwt_account_id(access)
    )
    api_key = _first(data, "OPENAI_API_KEY", "api_key", "apiKey")
    if mode == "apikey" or (api_key and not access):
        return {"mode": "api_key", "token": api_key, "account_id": ""}
    if access:
        return {"mode": "oauth", "token": access, "account_id": account}
    return None


def jwt_account_id(token: str) -> str:
    parts = token.split(".")
    if len(parts) < 2:
        return ""
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, UnicodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    auth = data.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        return str(auth.get("chatgpt_account_id") or "")
    return str(data.get("chatgpt_account_id") or "")


def coerce_expires(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return None
    if stamp > 1_000_000_000_000:
        stamp /= 1000.0
    return stamp


def refresh_claude(
    creds: dict,
    *,
    base_url: str,
    path: Path | None = None,
    original: dict | None = None,
    post=None,
) -> str | None:
    poster = post or post_json
    payload = poster(
        f"{base_url.rstrip('/')}/v1/oauth/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": creds["refresh_token"],
            "client_id": creds["client_id"],
        },
        {
            "content-type": "application/json",
            "anthropic-beta": CLAUDE_BETA,
        },
    )
    if not payload:
        return None
    access = str(payload.get("access_token") or "")
    if not access:
        return None
    refresh = str(payload.get("refresh_token") or creds["refresh_token"])
    try:
        expires_in = int(payload.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    expires_at = int(time.time()) + expires_in
    if path is not None:
        write_json(path, apply_claude_refresh(original or {}, access, refresh, expires_at))
    return access


def apply_claude_refresh(data: dict, access: str, refresh: str, expires_at: int) -> dict:
    updated = dict(data)
    nested = updated.get("claudeAiOauth")
    if isinstance(nested, dict):
        nested = dict(nested)
        nested["accessToken"] = access
        nested["refreshToken"] = refresh
        # Keep whichever unit the file already used, so a refreshed file is
        # still readable by whatever wrote it.
        previous = coerce_expires(nested.get("expiresAt"))
        in_milliseconds = previous is not None and previous != float(
            nested.get("expiresAt") or 0
        )
        nested["expiresAt"] = expires_at * 1000 if in_milliseconds else expires_at
        updated["claudeAiOauth"] = nested
        return updated
    updated["access_token"] = access
    updated["refresh_token"] = refresh
    updated["expires_at"] = expires_at
    return updated


def post_json(url: str, body: dict, headers: dict) -> dict | None:
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(url, json=body, headers=headers)
    except httpx.RequestError:
        return None
    if response.status_code != 200:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def sse_text(body: str) -> str:
    if not body.strip():
        raise ProviderError("Language model returned an empty completion.")
    stripped = body.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if isinstance(data, dict):
            text = response_text(data)
            if text:
                return text
            raise ProviderError("Language model returned a malformed completion.")
    chunks: list[str] = []
    fallback = ""
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        delta = event.get("delta")
        if isinstance(delta, str):
            chunks.append(delta)
        text = response_text(event)
        if text:
            fallback = text
        inner = event.get("response")
        if isinstance(inner, dict):
            text = response_text(inner)
            if text:
                fallback = text
    text = "".join(chunks).strip() or fallback.strip()
    if not text:
        raise ProviderError("Language model returned an empty completion.")
    return text


def response_text(data: dict) -> str:
    for key in ("output_text", "text"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    output = data.get("output")
    if not isinstance(output, list):
        return ""
    parts = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
        elif isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts).strip()


def _first(data: dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
