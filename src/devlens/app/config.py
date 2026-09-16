"""Configuration.

``extra="forbid"`` everywhere: a typo in ``devlens.toml`` fails at startup
rather than being silently ignored, which is the difference between "my
allowlist is not working" and "my allowlist has a typo on line 14".

Secrets are referenced by environment variable name in TOML and never stored in
the file, and they are held as :class:`~pydantic.SecretStr` so they cannot be
printed into a log or a response by accident.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from devlens.providers.factory import git_strategy


class ServiceEntry(BaseModel):
    """A declared service. Discovery is not implemented; declaration is."""

    model_config = ConfigDict(extra="forbid")
    repository: str | None = None
    language: str | None = None
    framework: str | None = None
    dependencies: list[str] = Field(default_factory=list, max_length=50)
    databases: list[str] = Field(default_factory=list, max_length=20)
    queues: list[str] = Field(default_factory=list, max_length=20)
    owners: list[str] = Field(default_factory=list, max_length=20)


class ProjectSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jira_url: str
    jira_email: str = Field(min_length=1, max_length=320)
    jira_token: SecretStr
    git_provider: Literal["github", "bitbucket_cloud"] = "github"
    git_token: SecretStr
    git_email: str = ""
    repositories: frozenset[str] = Field(min_length=1)
    jira_projects: frozenset[str] = Field(min_length=1)

    @field_validator("jira_token", "git_token")
    @classmethod
    def nonempty_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("Credential is empty")
        return value

    @field_validator("repositories")
    @classmethod
    def valid_repositories(cls, value):
        for item in value:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", item) or any(
                part in {".", ".."} for part in item.split("/")
            ):
                raise ValueError(f"Invalid repository identifier: {item!r}")
        return value

    @field_validator("jira_projects")
    @classmethod
    def valid_projects(cls, value):
        for item in value:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", item):
                raise ValueError(f"Invalid Jira project key: {item!r}")
        return value

    @field_validator("jira_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".atlassian.net")
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError(
                "Jira URL must be an HTTPS Atlassian Cloud site origin, for "
                "example https://your-team.atlassian.net"
            )
        return value.rstrip("/") + "/"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    projects: dict[str, ProjectSettings] = Field(min_length=1, max_length=50)
    services: dict[str, ServiceEntry] = Field(default_factory=dict, max_length=500)
    concurrency: int = Field(default=5, ge=1, le=32)
    max_analyses: int = Field(default=8, ge=1, le=128)
    analysis_timeout: float = Field(default=300, gt=0, le=1800)
    rate_limit_per_second: float = Field(default=20, gt=0, le=1000)
    rate_limit_burst: float = Field(default=40, ge=1, le=2000)
    circuit_failure_threshold: int = Field(default=5, ge=1, le=100)
    circuit_recovery_seconds: float = Field(default=30, gt=0, le=600)
    http_timeout: float = Field(default=15, gt=0, le=120)
    http_connect_timeout: float = Field(default=5, gt=0, le=60)

    @field_validator("projects")
    @classmethod
    def valid_names(cls, value):
        for name in value:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ValueError(f"Invalid project name: {name!r}")
        return value

    @classmethod
    def from_env(cls) -> Settings:
        if path := os.environ.get("DEVLENS_CONFIG"):
            return cls._from_toml(Path(path))
        return cls._from_variables()

    @classmethod
    def _from_toml(cls, path: Path) -> Settings:
        try:
            with path.open("rb") as file:
                data = tomllib.load(file)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"{path} is not valid TOML: {exc}") from None
        for name, project in (data.get("projects") or {}).items():
            if not isinstance(project, dict):
                raise ValueError(f"Project {name!r} must be a table")
            for key in ("jira_token", "git_token"):
                variable = project.pop(key + "_env", None)
                if not variable:
                    raise ValueError(
                        f"Project {name!r} must set {key}_env to the name of an "
                        "environment variable holding the credential"
                    )
                if not os.environ.get(variable):
                    raise ValueError(
                        f"Environment variable {variable} referenced by project "
                        f"{name!r} is not set"
                    )
                project[key] = os.environ[variable]
        return cls(**data)

    @classmethod
    def _from_variables(cls) -> Settings:
        """Build a single-project configuration from environment variables.

        Missing variables are named before validation runs, because a pydantic
        error dump tells an operator that ``jira_url`` failed a pattern, not
        that ``DEVLENS_JIRA_URL`` was never set.
        """
        strategy_name = os.environ.get("DEVLENS_GIT_PROVIDER", "github")
        try:
            token_variable = git_strategy(strategy_name).token_env
        except (KeyError, ValueError):
            raise ValueError(
                f"DEVLENS_GIT_PROVIDER={strategy_name!r} is not supported; use "
                "'github' or 'bitbucket_cloud'."
            ) from None
        required = (
            "DEVLENS_JIRA_URL",
            "DEVLENS_JIRA_EMAIL",
            "DEVLENS_JIRA_TOKEN",
            token_variable,
            "DEVLENS_REPOSITORIES",
            "DEVLENS_JIRA_PROJECTS",
        )
        missing = [name for name in required if not os.environ.get(name, "").strip()]
        if missing:
            raise ValueError(
                "DevLens is not configured. Set "
                + ", ".join(missing)
                + " (or point DEVLENS_CONFIG at a TOML file). The README lists "
                "what each one is for."
            )
        project = {
            key: os.environ.get("DEVLENS_" + key.upper(), "")
            for key in ("jira_url", "jira_email", "jira_token")
        }
        provider = os.environ.get("DEVLENS_GIT_PROVIDER", "github")
        strategy = git_strategy(provider)

        def names(variable: str) -> frozenset[str]:
            raw = os.environ.get(variable, "")
            return frozenset(item.strip() for item in raw.split(",") if item.strip())

        return cls(
            projects={
                "default": ProjectSettings(
                    jira_url=project["jira_url"],
                    jira_email=project["jira_email"],
                    jira_token=SecretStr(project["jira_token"]),
                    git_provider=provider,  # type: ignore[arg-type]
                    git_email=os.environ.get("DEVLENS_BITBUCKET_EMAIL", ""),
                    git_token=SecretStr(os.environ.get(strategy.token_env, "")),
                    repositories=names("DEVLENS_REPOSITORIES"),
                    jira_projects=names("DEVLENS_JIRA_PROJECTS"),
                )
            }
        )
