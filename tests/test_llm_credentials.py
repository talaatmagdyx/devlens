"""Model credential resolution.

DevLens can use a local Claude Code or Codex session instead of an API key, and
that is exactly the sort of convenience that turns into "it read a file from my
home directory without asking". The rule asserted here is that it never looks at
a credential file unless the operator opted in, and that a credential resolved
from one never reaches a log, a report or a settings response.

Regression coverage for DL-P2-029 (credential files were read unconditionally)
and DL-P3-033 (an expired OAuth token produced an unhelpful 401).
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

from devlens.domain import ProviderError
from devlens.providers.llm_oauth import (
    CLAUDE_BETA,
    apply_claude_refresh,
    auth_methods,
    claude_paths,
    codex_paths,
    coerce_expires,
    jwt_account_id,
    opted_in,
    parse_claude,
    parse_codex,
    read_json,
    refresh_claude,
    resolve_claude_oauth,
    resolve_codex,
    response_text,
    sse_text,
    token_from_claude_file,
    write_json,
)


def jwt(account: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": account}}).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.signature"


# --------------------------------------------------------------------------- #
# Opt-in
# --------------------------------------------------------------------------- #


def test_no_credential_file_is_read_without_an_explicit_opt_in(monkeypatch):
    """DL-P2-029: DevLens does not go looking in a home directory by default."""
    assert opted_in() is False
    assert claude_paths() == []
    assert codex_paths() == []
    assert resolve_claude_oauth() == ""
    assert resolve_codex() is None


def test_opting_in_names_the_paths_that_will_be_read(monkeypatch):
    monkeypatch.setenv("DEVLENS_LLM_OAUTH", "1")
    paths = claude_paths()
    assert paths and all(isinstance(item, Path) for item in paths)
    assert any(str(item).endswith(".credentials.json") for item in paths)
    assert codex_paths()[0].name == "auth.json"


def test_an_explicit_path_is_honoured_without_the_opt_in(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVLENS_CLAUDE_CREDENTIALS", str(tmp_path / "creds.json"))
    monkeypatch.setenv("DEVLENS_CODEX_AUTH", str(tmp_path / "auth.json"))
    assert claude_paths() == [tmp_path / "creds.json"]
    assert codex_paths() == [tmp_path / "auth.json"]


def test_an_environment_token_takes_precedence_over_any_file(monkeypatch):
    monkeypatch.setenv("DEVLENS_CLAUDE_OAUTH_TOKEN", "from-env")
    assert resolve_claude_oauth() == "from-env"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "payload",
    [
        {"claudeAiOauth": {"accessToken": "tok", "refreshToken": "ref", "clientId": "cid"}},
        {"claude_ai_oauth": {"access_token": "tok", "refresh_token": "ref",
                             "client_id": "cid"}},
        {"access_token": "tok", "refresh_token": "ref", "client_id": "cid"},
    ],
)
def test_both_credential_file_shapes_are_understood(payload):
    parsed = parse_claude(payload)
    assert parsed["access_token"] == "tok"
    assert parsed["refresh_token"] == "ref"
    assert parsed["client_id"] == "cid"


def test_a_millisecond_expiry_is_normalised_to_seconds():
    assert coerce_expires(1_800_000_000_000) == 1_800_000_000.0
    assert coerce_expires(1_800_000_000) == 1_800_000_000.0
    assert coerce_expires(None) is None
    assert coerce_expires("nonsense") is None


def test_a_codex_api_key_file_is_distinguished_from_an_oauth_one():
    api = parse_codex({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-file"})
    assert api == {"mode": "api_key", "token": "sk-file", "account_id": ""}

    oauth = parse_codex({"tokens": {"access_token": jwt("acct-1")}})
    assert oauth["mode"] == "oauth"
    assert oauth["account_id"] == "acct-1"

    assert parse_codex({}) is None
    assert parse_codex({"unrelated": True}) is None


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b", "a.!!!.c"])
def test_a_malformed_token_yields_no_account_rather_than_raising(token):
    assert jwt_account_id(token) == ""


def test_a_corrupt_credential_file_is_ignored(tmp_path):
    path = tmp_path / "creds.json"
    path.write_text("{not json")
    assert read_json(path) is None
    assert token_from_claude_file(path) == ""
    assert read_json(tmp_path / "absent.json") is None


def test_a_credential_file_holding_a_list_is_ignored(tmp_path):
    path = tmp_path / "creds.json"
    path.write_text("[]")
    assert read_json(path) is None


# --------------------------------------------------------------------------- #
# Refresh
# --------------------------------------------------------------------------- #


def test_a_live_token_is_used_without_a_refresh(tmp_path, monkeypatch):
    path = tmp_path / "creds.json"
    write_json(
        path,
        {
            "claudeAiOauth": {
                "accessToken": "live",
                "refreshToken": "ref",
                "clientId": "cid",
                "expiresAt": int((time.time() + 3600) * 1000),
            }
        },
    )
    monkeypatch.setenv("DEVLENS_CLAUDE_CREDENTIALS", str(path))
    assert token_from_claude_file(path) == "live"


def test_an_expired_token_is_refreshed_and_written_back(tmp_path):
    """DL-P3-033: the operator should not have to re-authenticate by hand."""
    path = tmp_path / "creds.json"
    original = {
        "claudeAiOauth": {
            "accessToken": "stale",
            "refreshToken": "ref",
            "clientId": "cid",
            "expiresAt": int((time.time() - 10) * 1000),
        }
    }
    write_json(path, original)

    seen = {}

    def post(url, body, headers):
        seen["url"] = url
        seen["body"] = body
        seen["headers"] = headers
        return {"access_token": "fresh", "refresh_token": "ref2", "expires_in": 3600}

    token = refresh_claude(
        parse_claude(original),
        base_url="https://api.anthropic.com",
        path=path,
        original=original,
        post=post,
    )
    assert token == "fresh"
    assert seen["url"].endswith("/v1/oauth/token")
    assert seen["body"]["grant_type"] == "refresh_token"
    assert seen["headers"]["anthropic-beta"] == CLAUDE_BETA
    stored = read_json(path)["claudeAiOauth"]
    assert stored["accessToken"] == "fresh"
    assert stored["refreshToken"] == "ref2"


def test_a_failed_refresh_returns_none_rather_than_raising():
    assert refresh_claude(
        {"refresh_token": "r", "client_id": "c"},
        base_url="https://api.anthropic.com",
        post=lambda *args, **kwargs: None,
    ) is None
    assert refresh_claude(
        {"refresh_token": "r", "client_id": "c"},
        base_url="https://api.anthropic.com",
        post=lambda *args, **kwargs: {"no_access_token": True},
    ) is None


def test_a_refresh_preserves_the_rest_of_the_file():
    updated = apply_claude_refresh(
        {"claudeAiOauth": {"accessToken": "old"}, "other": "keep"},
        "new",
        "ref",
        1_800_000_000,
    )
    assert updated["other"] == "keep"
    assert updated["claudeAiOauth"]["accessToken"] == "new"

    flat = apply_claude_refresh({"access_token": "old"}, "new", "ref", 1_800_000_000)
    assert flat["access_token"] == "new"
    assert flat["expires_at"] == 1_800_000_000


def test_the_write_is_atomic(tmp_path):
    path = tmp_path / "creds.json"
    write_json(path, {"a": 1})
    assert read_json(path) == {"a": 1}
    assert not (tmp_path / "creds.json.tmp").exists()


# --------------------------------------------------------------------------- #
# Reported auth methods
# --------------------------------------------------------------------------- #


def test_auth_methods_report_nothing_when_nothing_is_configured():
    assert auth_methods() == {"openai": None, "claude": None, "codex": None}


def test_auth_methods_distinguish_a_key_from_an_oauth_session(monkeypatch):
    monkeypatch.setenv("DEVLENS_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("DEVLENS_CLAUDE_OAUTH_TOKEN", "oauth-token")
    methods = auth_methods()
    assert methods["openai"] == "api_key"
    assert methods["claude"] == "oauth"
    assert methods["codex"] == "api_key"
    assert "sk-test" not in str(methods)
    assert "oauth-token" not in str(methods)


# --------------------------------------------------------------------------- #
# Streaming responses
# --------------------------------------------------------------------------- #


def test_a_streamed_response_is_reassembled_in_order():
    body = "\n".join(
        [
            'data: {"delta": "The pool "}',
            'data: {"delta": "is saturated."}',
            "data: [DONE]",
        ]
    )
    assert sse_text(body) == "The pool is saturated."


def test_a_non_streamed_json_response_is_read_directly():
    assert sse_text(json.dumps({"output_text": "answer"})) == "answer"


def test_a_stream_that_only_carries_a_final_response_still_yields_text():
    body = 'data: {"response": {"output_text": "final"}}\ndata: [DONE]\n'
    assert sse_text(body) == "final"


def test_malformed_stream_lines_are_skipped_rather_than_fatal():
    body = "\n".join(
        [
            "ignored line",
            "data: not-json",
            'data: "a string, not an object"',
            'data: {"delta": "ok"}',
        ]
    )
    assert sse_text(body) == "ok"


@pytest.mark.parametrize("body", ["", "   ", "data: [DONE]"])
def test_an_empty_completion_is_a_provider_error(body):
    with pytest.raises(ProviderError, match=r"empty|malformed"):
        sse_text(body)


def test_a_malformed_json_completion_is_a_provider_error():
    with pytest.raises(ProviderError, match="malformed"):
        sse_text(json.dumps({"unexpected": True}))


@pytest.mark.parametrize(
    "payload",
    [
        {"output_text": "answer"},
        {"text": "answer"},
        {"output": [{"content": [{"type": "output_text", "text": "answer"}]}]},
        {"output": [{"text": "answer"}]},
    ],
)
def test_every_supported_responses_api_shape_is_understood(payload):
    assert response_text(payload) == "answer"


@pytest.mark.parametrize("payload", [{}, {"output": "not a list"}, {"output": [1, 2]}])
def test_an_unrecognised_shape_yields_no_text_rather_than_raising(payload):
    assert response_text(payload) == ""
