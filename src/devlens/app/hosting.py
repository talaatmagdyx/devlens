"""Validated operator-owned tenancy configuration; never supplied by HTTP callers."""
import os
import re
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TenantConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    projects: frozenset[str] = Field(min_length=1)


class IdentityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant: str
    role: Literal["viewer", "operator"] = "viewer"
    token_env: str


class HostingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    public_origin: str
    data_dir: Path
    tenants: dict[str, TenantConfig] = Field(min_length=1, max_length=100)
    identities: dict[str, IdentityConfig] = Field(min_length=1, max_length=1000)
    session_seconds: int = Field(default=3600, ge=60, le=86400)
    requests_per_minute: int = Field(default=120, ge=1, le=10000)
    max_inflight_per_tenant: int = Field(default=16, ge=1, le=128)
    max_jobs_per_tenant: int = Field(default=8, ge=1, le=128)

    @field_validator("public_origin")
    @classmethod
    def origin(cls, value):
        url = urlsplit(value)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment:
            raise ValueError("public_origin must be an HTTPS origin")
        return value.rstrip("/")

    @field_validator("data_dir")
    @classmethod
    def absolute_directory(cls, value):
        if not value.is_absolute():
            raise ValueError("data_dir must be absolute")
        return value

    @model_validator(mode="after")
    def isolation(self):
        names = [*self.tenants, *self.identities]
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name) for name in names):
            raise ValueError("Invalid tenant or identity name")
        projects = [project for tenant in self.tenants.values() for project in tenant.projects]
        if len(projects) != len(set(projects)):
            raise ValueError("A project must belong to exactly one tenant")
        if any(identity.tenant not in self.tenants for identity in self.identities.values()):
            raise ValueError("Identity refers to an unknown tenant")
        return self

    @classmethod
    def from_env(cls):
        path = os.environ.get("DEVLENS_SAAS_CONFIG")
        if not path:
            raise ValueError("DEVLENS_SAAS_CONFIG is required")
        with Path(path).open("rb") as file:
            return cls.model_validate(tomllib.load(file))
