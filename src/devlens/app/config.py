import os
import re
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr, field_validator


class ProjectSettings(BaseModel):
    jira_url: str
    jira_email: str = Field(min_length=1)
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
            raise ValueError("Empty credential")
        return value

    @field_validator("repositories")
    @classmethod
    def valid_repositories(cls, value):
        if any(
            not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", item)
            or any(p in {".", ".."} for p in item.split("/"))
            for item in value
        ):
            raise ValueError("Invalid repository identifier")
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
            raise ValueError("Jira URL must be an HTTPS Atlassian Cloud site origin")
        return value.rstrip("/") + "/"


class Settings(BaseModel):
    projects: dict[str, ProjectSettings] = Field(min_length=1)
    concurrency: int = Field(default=5, ge=1, le=32)
    max_analyses: int = Field(default=8, ge=1, le=128)
    analysis_timeout: float = Field(default=120, gt=0, le=900)

    @field_validator("projects")
    @classmethod
    def valid_names(cls, value):
        if any(not re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in value):
            raise ValueError("Invalid project name")
        return value

    @classmethod
    def from_env(cls):
        if path := os.environ.get("DEVLENS_CONFIG"):
            with Path(path).open("rb") as file:
                data = tomllib.load(file)
            for project in data.get("projects", {}).values():
                for key in ("jira_token", "git_token"):
                    variable = project.pop(key + "_env", None)
                    if not variable or not os.environ.get(variable):
                        raise ValueError(
                            "Project credential environment variable is missing"
                        )
                    project[key] = os.environ[variable]
            return cls(**data)
        project = {
            key: os.environ.get("DEVLENS_" + key.upper(), "")
            for key in ("jira_url", "jira_email", "jira_token")
        }
        provider = os.environ.get("DEVLENS_GIT_PROVIDER", "github")
        project.update(
            git_provider=provider,
            git_email=os.environ.get("DEVLENS_BITBUCKET_EMAIL", ""),
            git_token=os.environ.get(
                "DEVLENS_GITHUB_TOKEN"
                if provider == "github"
                else "DEVLENS_BITBUCKET_TOKEN",
                "",
            ),
        )
        for key in ("repositories", "jira_projects"):
            project[key] = frozenset(
                item.strip()
                for item in os.environ.get("DEVLENS_" + key.upper(), "").split(",")
                if item.strip()
            )
        return cls(projects={"default": ProjectSettings(**project)})
