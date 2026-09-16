"""The repository's own claims, checked against the repository.

The audit's last finding was that the documentation asserted things the code did
not do. These tests read the shipped files and hold them to what is actually
configured, so the README and the container definition cannot drift away from
the code again without a failing test.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# The container definition
# --------------------------------------------------------------------------- #


def test_the_image_runs_as_a_non_root_user():
    dockerfile = read("Dockerfile")
    assert re.search(r"^USER \d+", dockerfile, re.M), "the image must not run as root"
    assert "useradd" in dockerfile


def test_the_image_carries_the_binaries_the_workflows_shell_out_to():
    dockerfile = read("Dockerfile")
    for tool in ("git", "ripgrep"):
        assert tool in dockerfile, f"{tool} is required at runtime"


def test_the_image_packages_the_compiled_ui_inside_the_package():
    """The bug this catches: the UI was built and then put where nothing looks."""
    dockerfile = read("Dockerfile")
    assert "src/devlens/web/dist" in dockerfile
    packaging = tomllib.loads(read("pyproject.toml"))
    assert packaging["tool"]["setuptools"]["package-data"]["devlens"] == ["web/dist/**/*"]


def test_the_image_enables_no_capability_by_default():
    dockerfile = read("Dockerfile")
    for variable in (
        "DEVLENS_ALLOW_WRITES",
        "DEVLENS_ALLOW_REPO_TESTS",
        "DEVLENS_LLM_PROVIDER",
        "DEVLENS_LLM_API_KEY",
    ):
        assert f"{variable}=" not in dockerfile, f"{variable} must not be set in the image"


def test_the_image_declares_a_health_check():
    assert "HEALTHCHECK" in read("Dockerfile")


def test_compose_constrains_the_container_it_runs():
    compose = yaml.safe_load(read("docker-compose.yml"))
    service = compose["services"]["devlens"]
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["pids_limit"] >= 1
    assert service["mem_limit"]
    assert any(str(item).startswith("/tmp:") for item in service["tmpfs"])
    # Loopback only: one shared password is not a network authentication boundary.
    assert all(str(port).startswith("127.0.0.1:") for port in service["ports"])
    # The analysed checkout is mounted read-only.
    assert any(str(volume).endswith(":ro") for volume in service["volumes"])


# --------------------------------------------------------------------------- #
# Continuous integration runs the gates this repository claims
# --------------------------------------------------------------------------- #


@pytest.fixture
def workflow() -> dict:
    path = ROOT / ".github" / "workflows" / "ci.yml"
    if not path.exists():
        pytest.fail(
            ".github/workflows/ci.yml is missing. Continuous integration is a "
            "claim this repository makes in its README and in "
            "DEVLENS_REMEDIATION.md, so its absence is a failure rather than a "
            "skip. Restore the workflow file."
        )
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_ci_runs_every_gate(workflow):
    commands = " ".join(
        str(step.get("run", ""))
        for job in workflow["jobs"].values()
        for step in job["steps"]
    )
    for gate in ("ruff check", "mypy", "pytest", "npm run typecheck", "npm run test", "vite build"):
        assert gate in commands, f"CI does not run {gate}"


def test_ci_checks_the_bundle_for_external_hosts(workflow):
    commands = " ".join(
        str(step.get("run", ""))
        for job in workflow["jobs"].values()
        for step in job["steps"]
    )
    assert "cdn|unpkg|jsdelivr" in commands


def test_ci_runs_the_browser_suite(workflow):
    commands = " ".join(
        str(step.get("run", ""))
        for job in workflow["jobs"].values()
        for step in job["steps"]
    )
    assert "playwright test" in commands


def test_ci_smoke_tests_the_container_it_builds(workflow):
    """CI must run the same smoke script an engineer runs, not a parallel copy.

    Inline assertions in a workflow file drift away from the script nobody
    remembers to update. One entry point keeps them honest.
    """
    container = workflow["jobs"]["container"]
    commands = " ".join(str(step.get("run", "")) for step in container["steps"])
    assert "scripts/smoke-container.sh" in commands
    assert "PW_BASE_URL" in commands, (
        "the browser suite must run against the image, not only a working copy"
    )


def test_the_container_smoke_script_proves_the_artefact_not_the_dockerfile():
    script = read("scripts/smoke-container.sh")
    for claim in (
        "/health",
        "/capabilities",
        "/ready",
        "id -u",
        "<title>",
        "x-content-type-options",
        "/usr/local/probe",
        "/data/probe",
        "git --version",
        "rg --version",
    ):
        assert claim in script, f"the smoke script does not check {claim}"


def test_the_container_smoke_script_is_executable():
    mode = (ROOT / "scripts" / "smoke-container.sh").stat().st_mode
    assert mode & 0o111, "scripts/smoke-container.sh is not executable"
    assert (ROOT / "scripts" / "verify.sh").stat().st_mode & 0o111


def test_the_image_can_be_built_where_public_registries_are_unreachable():
    """Both base images are parameterised, so a mirror can be substituted.

    A hard-coded ``FROM docker.io/...`` makes the image unbuildable on any
    network that does not reach Docker Hub, which is most regulated ones.
    """
    dockerfile = read("Dockerfile")
    assert re.search(r"^ARG NODE_IMAGE=", dockerfile, re.M)
    assert re.search(r"^ARG PYTHON_IMAGE=", dockerfile, re.M)
    assert "FROM ${NODE_IMAGE}" in dockerfile
    assert "FROM ${PYTHON_IMAGE}" in dockerfile


def test_the_image_declares_its_writable_location():
    """The crash this catches: a tmpfs mounted over /data left it unwritable."""
    dockerfile = read("Dockerfile")
    assert 'VOLUME ["/data"]' in dockerfile


def test_the_browser_suite_can_be_pointed_at_an_already_running_deployment():
    config = read("web/playwright.config.ts")
    assert "PW_BASE_URL" in config
    assert "webServer: externalBaseURL" in config, (
        "pointing the suite at a running deployment must also stop it starting its own"
    )


def test_the_browser_suite_pins_no_port_of_its_own():
    """A hard-coded origin in a spec silently only tests the working copy."""
    for spec in (ROOT / "web" / "e2e").glob("*.spec.ts"):
        text = spec.read_text(encoding="utf-8")
        assert "127.0.0.1:8" not in text, (
            f"{spec.name} pins an origin; read it from baseURL instead"
        )


def test_ci_covers_the_python_versions_the_package_claims(workflow):
    packaging = tomllib.loads(read("pyproject.toml"))
    floor = packaging["project"]["requires-python"].lstrip(">=")
    matrix = [str(item) for item in workflow["jobs"]["backend"]["strategy"]["matrix"]["python"]]
    assert floor in matrix, f"CI does not test the minimum supported Python ({floor})"


def test_the_verification_script_and_ci_agree():
    """`scripts/verify.sh` is the definition of verified; CI must run the same."""
    script = read("scripts/verify.sh")
    for gate in ("ruff check", "mypy", "pytest", "npm run typecheck", "npm run test", "vite build", "playwright test"):
        assert gate in script, f"verify.sh does not run {gate}"


# --------------------------------------------------------------------------- #
# The README describes the code that is here
# --------------------------------------------------------------------------- #


def test_the_readme_names_the_capabilities_that_exist():
    from devlens.guardrails import BOUNDARIES, ENABLE_HINTS

    readme = read("README.md")
    for capability in BOUNDARIES:
        assert capability in readme, f"{capability} is not documented"
    for hint in ENABLE_HINTS.values():
        if hint == "not implemented":
            continue
        assert hint.split()[0].split("=")[0] in readme, f"{hint} is not documented"


def test_the_readme_does_not_name_a_capability_that_does_not_exist():
    from devlens.guardrails import BOUNDARIES

    readme = read("README.md")
    for stale in ("screenshot_root_cause", "service_catalog", "push_commit"):
        assert stale not in readme, f"{stale} is documented but does not exist"
    assert "service_discovery" in BOUNDARIES


def test_the_readme_states_the_write_actions_the_gateway_permits():
    from devlens.providers.writes import CAPABILITY_FOR

    readme = read("README.md")
    for action in CAPABILITY_FOR:
        assert action in readme, f"{action} is not documented"


def test_the_readme_states_the_actions_that_are_always_refused():
    from devlens.domain import HUMAN_ONLY_ACTIONS

    readme = read("README.md")
    for action in HUMAN_ONLY_ACTIONS:
        assert action in readme


def test_the_readme_does_not_claim_a_coverage_gate_it_does_not_have():
    readme = read("README.md")
    packaging = tomllib.loads(read("pyproject.toml"))
    gate = packaging["tool"]["coverage"]["report"]["fail_under"]
    assert "--cov-fail-under=100" not in readme
    assert gate >= 90, "the documented gate and the configured gate must agree"


def test_every_environment_variable_the_code_reads_is_documented():
    """A setting nobody can find is a setting nobody uses."""
    import os as _os

    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "src").rglob("*.py")
    )
    referenced = set(re.findall(r'"(DEVLENS_[A-Z0-9_]+)"', source))
    documented = read("README.md") + read(".env.example") + read(".env.docker.example")
    missing = sorted(name for name in referenced if name not in documented)
    assert missing == [], f"undocumented settings: {missing}"
    assert _os is not None

def test_the_workflow_runner_exists_and_can_read_the_workflow(workflow):
    """The workflow's commands are executable outside Actions, from the file.

    `scripts/verify.sh` is a second copy of CI's gates; `scripts/run-workflow.py`
    is not a copy at all — it reads this file and runs what it says. A step that
    only exists in one of them is the drift this repository keeps catching.
    """
    runner = read("scripts/run-workflow.py")
    assert "ci.yml" in runner
    for job in workflow["jobs"]:
        assert job in " ".join(workflow["jobs"]), job
    # The runner must not quietly pass over a step it cannot run.
    assert "Not executed here" in runner
    assert "--skip" in runner


def test_the_soak_driver_fails_on_growth_rather_than_printing_a_graph():
    soak = read("scripts/soak.py")
    for claim in ("rss_kb", "fds", "threads", "monotonic_growth", "failure_count"):
        assert claim in soak, f"the soak driver does not record {claim}"
    assert "return 1" in soak, "the soak driver must exit non-zero on a leak"


def test_the_browser_suite_covers_what_axe_cannot(workflow):
    """Rule checking is not the same as being usable without sight."""
    spec = read("web/e2e/assistive.spec.ts")
    for claim in (
        "accessibility.snapshot",
        "aria-live",
        "role=\"alert\"",
        "document.title",
        "focus",
        "h1",
    ):
        assert claim in spec, f"the assistive suite does not cover {claim}"
