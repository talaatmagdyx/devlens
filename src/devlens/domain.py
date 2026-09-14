from typing import Literal

from pydantic import BaseModel, Field


class ProviderError(Exception):
    """Sanitized upstream failure safe to expose to callers."""


class AccessDenied(Exception):
    pass


class AnalysisRequest(BaseModel):
    project: str = Field(default="default", pattern=r"^[A-Za-z0-9_-]+$")
    ticket_key: str = Field(pattern=r"^[A-Z][A-Z0-9_]*-[1-9][0-9]*$")
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    ref: str = Field(default="HEAD", min_length=1, max_length=200)


class Ticket(BaseModel):
    key: str
    summary: str
    description: str
    url: str


class RepositoryFile(BaseModel):
    path: str
    size: int


class Evidence(BaseModel):
    path: str
    line: int
    excerpt: str
    url: str
    matched_terms: list[str]


class AnalysisResult(BaseModel):
    project: str = "default"
    provider: str = "github"
    status: Literal["completed", "insufficient_context"]
    method: Literal["lexical"] = "lexical"
    ticket: Ticket
    repository: str
    commit: str
    summary: str
    evidence: list[Evidence]
    limitations: list[str]
