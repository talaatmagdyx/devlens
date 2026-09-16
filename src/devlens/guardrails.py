"""Deny-by-default capability gate.

Every optional backend — language models, vision, live telemetry, remote
writes, executing a repository's own tests — is a named capability that is off
unless it is explicitly configured. Capability state is computed once from the
environment at startup and never from a provider response, so nothing DevLens
reads from the outside world can widen its own permissions.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from devlens.domain import AccessDenied, ProviderError

#: Every capability, with the reason it is refused and what turns it on.
BOUNDARIES: dict[str, str] = {
    "llm": "No language model is configured. DevLens stays deterministic.",
    "screenshot_analysis": (
        "Screenshot analysis is disabled. It requires a vision-capable model, "
        "and even when enabled it produces observations, never a root cause."
    ),
    "observability_provider": (
        "No observability provider is configured. Logs, metrics, traces and "
        "SELECT queries are unavailable."
    ),
    "repository_code_execution": (
        "Executing a repository's own test suite is disabled. Enable it only "
        "for repositories you trust to run code on this machine."
    ),
    "jira_writeback": "Jira write-back is disabled.",
    "git_writeback": "Git remote write-back is disabled.",
    "service_discovery": (
        "Automatic service discovery is not implemented. The service catalog is "
        "built from the repository allowlist and declared services only."
    ),
}

ENABLE_HINTS: dict[str, str] = {
    "llm": "DEVLENS_LLM_PROVIDER",
    "screenshot_analysis": "DEVLENS_LLM_PROVIDER + DEVLENS_VISION_MODEL",
    "observability_provider": "DEVLENS_LOKI_URL / DEVLENS_PROM_URL / DEVLENS_TEMPO_URL / DEVLENS_SQL_URL",
    "repository_code_execution": "DEVLENS_ALLOW_REPO_TESTS=1",
    "jira_writeback": "DEVLENS_ALLOW_WRITES=1",
    "git_writeback": "DEVLENS_ALLOW_WRITES=1",
    "service_discovery": "not implemented",
}

TRUTHY = frozenset({"1", "true", "yes", "on"})


def enabled_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in TRUTHY


def forbid(capability: str) -> None:
    raise AccessDenied(BOUNDARIES.get(capability, f"{capability} is disabled."))


class CapabilityGuard:
    """The single place where optional backends are gated."""

    def __init__(
        self,
        enabled: Iterable[str] | None = None,
        llm: Any = None,
        observe: Any = None,
    ) -> None:
        self.enabled = frozenset(enabled or ())
        self.denied = frozenset(BOUNDARIES) - self.enabled
        self.llm = llm
        self.observe = observe

    @classmethod
    def from_env(cls) -> CapabilityGuard:
        from devlens.providers.llm import LlmProvider
        from devlens.providers.observe import ObserveHub

        llm = LlmProvider.from_env()
        observe = ObserveHub.from_env()
        enabled: set[str] = set()
        if llm is not None:
            enabled.add("llm")
            if llm.vision_capable:
                enabled.add("screenshot_analysis")
        if observe.configured:
            enabled.add("observability_provider")
        if enabled_flag("DEVLENS_ALLOW_WRITES"):
            enabled.update({"git_writeback", "jira_writeback"})
        if enabled_flag("DEVLENS_ALLOW_REPO_TESTS"):
            enabled.add("repository_code_execution")
        return cls(enabled, llm=llm, observe=observe)

    # -- queries ---------------------------------------------------------- #

    def has(self, capability: str) -> bool:
        return capability in self.enabled

    def require(self, capability: str) -> None:
        """Raise unless the capability is enabled. The only enforcement point."""
        if capability not in self.enabled:
            forbid(capability)

    def inventory(self) -> dict[str, list[str] | dict[str, str]]:
        return {
            "enabled": sorted(self.enabled),
            "denied": sorted(self.denied),
            "reasons": {name: BOUNDARIES[name] for name in sorted(self.denied)},
            "enable_with": {name: ENABLE_HINTS[name] for name in sorted(self.denied)},
        }

    def limitations(self) -> list[str]:
        """Limitations that apply to every report, stated from actual state."""
        items: list[str] = []
        if "llm" in self.enabled:
            items.append(
                "A language model summarised retrieved evidence. It cannot add "
                "facts, and confidence stays Low/Medium/High/Very High."
            )
        else:
            items.append("No language model is used; all output is deterministic.")
        if "screenshot_analysis" in self.enabled:
            items.append(
                "Screenshot analysis adds observations only and can never confirm "
                "a root cause."
            )
        else:
            items.append("Screenshots are described by metadata only.")
        if "observability_provider" in self.enabled:
            items.append(
                "Live telemetry is configured. Provider availability is reported "
                "per source; an unreachable provider is recorded as UNKNOWN, "
                "never as supporting evidence."
            )
        else:
            items.append("Observability providers are disabled; nothing is verified at runtime.")
        if {"git_writeback", "jira_writeback"} & self.enabled:
            items.append(
                "Remote writes are possible but require an operator to approve a "
                "specific proposal. Merge and deploy remain human-only."
            )
        else:
            items.append("Jira and git remotes are read-only.")
        if "repository_code_execution" in self.enabled:
            from devlens.tools.shell import isolation_runtime, running_as_root

            runtime = isolation_runtime()
            if runtime:
                items.append(
                    f"Repository test execution is enabled and confined by {runtime}."
                )
            elif running_as_root() and enabled_flag("DEVLENS_ALLOW_ROOT_REPO_TESTS"):
                items.append(
                    "Repository test execution is enabled as root with no container "
                    "runtime, because DEVLENS_ALLOW_ROOT_REPO_TESTS is set. Process "
                    "limits do not constrain a root user, so this host is not "
                    "protected from the repository's own test suite."
                )
            else:
                items.append(
                    "Repository test execution is enabled with process limits only; "
                    "no container runtime is available to isolate it."
                )
        else:
            items.append("Repository code is read but never executed.")
        items.append("Service discovery is not implemented; the catalog is declared.")
        return items

    # -- gated backends --------------------------------------------------- #

    async def complete(self, prompt: str = "") -> str:
        self.require("llm")
        if self.llm is None:  # pragma: no cover - guarded by require()
            raise ProviderError("Language model client is not attached.")
        return await self.llm.complete(prompt)

    async def analyze_screenshot(self, attachment: Any) -> list[str]:
        """Describe an image. Returns observations; never a root cause."""
        self.require("screenshot_analysis")
        if self.llm is None:  # pragma: no cover - guarded by require()
            raise ProviderError("Vision model client is not attached.")
        content, mime = image_bytes(attachment)
        return await self.llm.observe_image(content, mime)

    async def collect_runtime(self, query: str, window_seconds: int = 3600) -> Any:
        """Query every configured observability provider, honestly."""
        self.require("observability_provider")
        if self.observe is None:  # pragma: no cover - guarded by require()
            raise ProviderError("Observability hub is not attached.")
        return await self.observe.collect(query, window_seconds=window_seconds)


def image_bytes(attachment: Any) -> tuple[bytes, str]:
    if attachment is None:
        raise ProviderError("Screenshot bytes are required.")
    if isinstance(attachment, (bytes, bytearray)):
        return bytes(attachment), "image/png"
    content = getattr(attachment, "content", None)
    mime = getattr(attachment, "mime_type", None) or "image/png"
    if content is None and isinstance(attachment, dict):
        content = attachment.get("content")
        mime = attachment.get("mime_type") or mime
    if not content:
        raise ProviderError("Screenshot bytes are required.")
    return content, mime
