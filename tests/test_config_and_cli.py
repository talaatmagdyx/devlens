"""Configuration, policy and the command line.

Configuration is the last place a security property should be optional, so the
tests here are mostly about refusal: a credential that is empty, a Jira URL
that is not an Atlassian origin, a repository identifier that traverses, a TOML
file that inlines a token instead of naming an environment variable.

Regression coverage for DL-P2-025 (credentials could be written into the config
file), DL-P3-035 (the CLI printed a stack trace) and DL-P3-037 (no exit code
distinguished a refusal from a crash).
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from devlens.app.config import ProjectSettings, ServiceEntry, Settings
from devlens.cli import build_parser, main
from devlens.domain import AccessDenied
from devlens.policies import Decision, Policy, ReadPolicy


def project(**overrides) -> dict:
    return {
        "jira_url": "https://team.atlassian.net",
        "jira_email": "operator@example.com",
        "jira_token": "jira-token",
        "git_token": "git-token",
        "repositories": frozenset({"org/repo"}),
        "jira_projects": frozenset({"DEV"}),
        **overrides,
    }


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def test_a_valid_single_project_configuration_is_accepted(single_project):
    settings = Settings.from_env()
    assert set(settings.projects) == {"default"}
    assert settings.projects["default"].repositories == frozenset({"org/repo"})


def test_a_credential_never_appears_in_a_repr_or_a_dump(single_project):
    settings = Settings.from_env()
    assert "jira-token" not in repr(settings)
    assert "jira-token" not in json.dumps(settings.model_dump(), default=str)
    assert settings.projects["default"].jira_token.get_secret_value() == "jira-token"


@pytest.mark.parametrize(
    "url",
    [
        "http://team.atlassian.net",
        "https://team.atlassian.net.evil.example",
        "https://user:pw@team.atlassian.net",
        "https://team.atlassian.net/secure/path",
        "https://team.atlassian.net?x=1",
        "https://jira.internal",
    ],
)
def test_a_jira_url_that_is_not_an_atlassian_origin_is_refused(url):
    with pytest.raises(ValidationError, match="Atlassian Cloud site origin"):
        ProjectSettings(**project(jira_url=url))


def test_a_jira_url_is_normalised_to_an_origin_with_a_trailing_slash():
    assert ProjectSettings(**project()).jira_url == "https://team.atlassian.net/"


@pytest.mark.parametrize(
    "repository",
    ["../etc", "org/../etc", "org", "org/repo/extra", "org/re po", ""],
)
def test_an_invalid_repository_identifier_is_refused(repository):
    with pytest.raises(ValidationError):
        ProjectSettings(**project(repositories=frozenset({repository})))


@pytest.mark.parametrize("key", ["dev", "1DEV", "DEV-1", ""])
def test_an_invalid_jira_project_key_is_refused(key):
    with pytest.raises(ValidationError):
        ProjectSettings(**project(jira_projects=frozenset({key})))


@pytest.mark.parametrize("field", ["jira_token", "git_token"])
def test_an_empty_credential_is_refused(field):
    with pytest.raises(ValidationError, match="Credential is empty"):
        ProjectSettings(**project(**{field: "   "}))


def test_at_least_one_repository_and_one_project_are_required():
    with pytest.raises(ValidationError):
        ProjectSettings(**project(repositories=frozenset()))
    with pytest.raises(ValidationError):
        ProjectSettings(**project(jira_projects=frozenset()))


def test_an_unknown_setting_is_refused_rather_than_ignored():
    """Extra keys are a typo, and a silently ignored typo is a wrong policy."""
    with pytest.raises(ValidationError):
        ProjectSettings(**project(jira_urls="https://team.atlassian.net"))
    with pytest.raises(ValidationError):
        ServiceEntry(repositroy="org/repo")


def test_a_toml_config_must_name_an_environment_variable_for_each_credential(
    tmp_path, monkeypatch
):
    """DL-P2-025: a token in a config file is a token in version control."""
    config = tmp_path / "devlens.toml"
    config.write_text(
        """
[projects.default]
jira_url = "https://team.atlassian.net"
jira_email = "operator@example.com"
jira_token = "inline-secret"
git_token = "inline-secret"
repositories = ["org/repo"]
jira_projects = ["DEV"]
"""
    )
    monkeypatch.setenv("DEVLENS_CONFIG", str(config))
    with pytest.raises(ValueError, match="must set jira_token_env"):
        Settings.from_env()


def test_a_toml_config_reads_each_credential_from_the_environment(tmp_path, monkeypatch):
    config = tmp_path / "devlens.toml"
    config.write_text(
        """
[projects.default]
jira_url = "https://team.atlassian.net"
jira_email = "operator@example.com"
jira_token_env = "MY_JIRA_TOKEN"
git_token_env = "MY_GIT_TOKEN"
repositories = ["org/repo"]
jira_projects = ["DEV"]
"""
    )
    monkeypatch.setenv("DEVLENS_CONFIG", str(config))
    monkeypatch.setenv("MY_JIRA_TOKEN", "from-env")
    monkeypatch.setenv("MY_GIT_TOKEN", "from-env")
    settings = Settings.from_env()
    assert settings.projects["default"].jira_token.get_secret_value() == "from-env"


def test_a_missing_credential_variable_is_named_in_the_error(tmp_path, monkeypatch):
    config = tmp_path / "devlens.toml"
    config.write_text(
        """
[projects.default]
jira_url = "https://team.atlassian.net"
jira_email = "operator@example.com"
jira_token_env = "ABSENT_JIRA_TOKEN"
git_token_env = "ABSENT_GIT_TOKEN"
repositories = ["org/repo"]
jira_projects = ["DEV"]
"""
    )
    monkeypatch.setenv("DEVLENS_CONFIG", str(config))
    with pytest.raises(ValueError, match="ABSENT_JIRA_TOKEN"):
        Settings.from_env()


def test_invalid_toml_names_the_file(tmp_path, monkeypatch):
    config = tmp_path / "devlens.toml"
    config.write_text("[projects.default\n")
    monkeypatch.setenv("DEVLENS_CONFIG", str(config))
    with pytest.raises(ValueError, match="is not valid TOML"):
        Settings.from_env()


def test_resilience_settings_are_bounded():
    with pytest.raises(ValidationError):
        Settings(projects={"default": ProjectSettings(**project())}, concurrency=0)
    with pytest.raises(ValidationError):
        Settings(projects={"default": ProjectSettings(**project())}, analysis_timeout=0)
    with pytest.raises(ValidationError):
        Settings(projects={"default": ProjectSettings(**project())}, http_timeout=1000)


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


def test_reads_are_permitted_and_writes_are_not_by_default():
    policy = Policy()
    assert policy.decide("operator", "repository.read").allow is True
    assert policy.decide("operator", "git.create_pr").allow is False
    assert "write-back is disabled" in policy.decide("operator", "git.create_pr").reason.lower()


@pytest.mark.parametrize("action", ["merge_pr", "deploy", "release", "git.merge_pr"])
def test_merge_deploy_and_release_are_refused_in_every_configuration(action):
    policy = Policy(writes_enabled=True, execution_enabled=True, environment="local")
    decision = policy.decide("operator", action, approved=True)
    assert decision.allow is False
    assert "human-only" in decision.reason


def test_a_write_needs_an_operator_the_capability_and_an_approval():
    enabled = Policy(writes_enabled=True)
    assert enabled.decide("agent", "git.create_pr", approved=True).allow is False
    assert enabled.decide("operator", "git.create_pr", approved=False).allow is False
    assert enabled.decide("operator", "git.create_pr", approved=True).allow is True


def test_repository_code_is_never_executed_in_production():
    policy = Policy(execution_enabled=True, environment="production")
    decision = policy.decide("operator", "repository.run_tests")
    assert decision.allow is False
    assert "production" in decision.reason


def test_an_unknown_action_is_denied_by_default():
    decision = Policy().decide("operator", "something.new")
    assert decision.allow is False
    assert "denied by default" in decision.reason


def test_a_decision_can_be_enforced():
    with pytest.raises(AccessDenied, match="nope"):
        Decision(False, "nope").enforce()
    Decision(True, "fine").enforce()


def test_the_read_allowlist_is_case_insensitive_and_closed():
    policy = ReadPolicy(frozenset({"Org/Repo"}), frozenset({"dev"}))
    policy.authorize_repository("org/repo")
    policy.authorize_ticket("DEV-1")
    with pytest.raises(AccessDenied, match="not on this project's allowlist"):
        policy.authorize_repository("org/other")
    with pytest.raises(AccessDenied, match="not on this project's allowlist"):
        policy.authorize_ticket("OPS-1")
    with pytest.raises(AccessDenied, match="ticket key is required"):
        policy.authorize_ticket("")


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def test_every_command_is_reachable_from_the_parser():
    parser = build_parser()
    for command in ("ticket", "analyze", "review", "implement", "ask", "investigate"):
        assert parser.parse_args([command, *_arguments(command)]).command == command


def _arguments(command: str) -> list[str]:
    return {
        "ticket": ["DEV-1", "--repository", "org/repo"],
        "analyze": ["."],
        "review": ["--repository", "org/repo", "--pr", "7"],
        "implement": ["DEV-1", "--repository", "org/repo"],
        "ask": ["why?"],
        "investigate": ["why?"],
    }[command]


def test_no_command_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exit_code:
        main([])
    assert exit_code.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_the_run_tests_flag_says_which_variable_enables_it(capsys):
    """An operator should not have to read the source to find the switch."""
    with pytest.raises(SystemExit):
        main(["review", "--help"])
    assert "DEVLENS_ALLOW_REPO_TESTS" in capsys.readouterr().out


def test_a_refusal_is_a_message_and_an_exit_code_not_a_traceback(capsys, tmp_path):
    """DL-P3-035 and DL-P3-037."""
    code = main(["analyze", str(tmp_path)])
    output = capsys.readouterr()
    assert code == 1
    assert "Traceback" not in output.err
    assert "not a git repository" in (output.err + output.out)


def test_a_local_analysis_runs_without_any_credentials(capsys, git_repo):
    assert main(["analyze", str(git_repo)]) == 0
    assert "Workspace" in capsys.readouterr().out


def test_a_configured_command_without_configuration_explains_itself(capsys):
    code = main(["ticket", "DEV-1", "--repository", "org/repo"])
    output = capsys.readouterr()
    assert code == 1
    assert "Traceback" not in output.err
    combined = output.err + output.out
    assert "DEVLENS_JIRA_URL" in combined
    assert "not configured" in combined
    assert "validation error" not in combined


# --------------------------------------------------------------------------- #
# The CLI, end to end
# --------------------------------------------------------------------------- #


def test_every_platform_command_runs_without_configuration(capsys, git_repo):
    """ask, investigate, design and observe need no credentials at all."""
    for command, arguments in (
        ("ask", ["why is checkout slow?"]),
        ("investigate", ["p99 latency tripled after the deploy"]),
        ("design", ["10k events per second, 2kb payload"]),
        ("observe", ["--path", str(git_repo)]),
    ):
        assert main([command, *arguments]) == 0, command
        printed = capsys.readouterr().out
        assert printed.strip().startswith("{"), f"{command} did not print a report"
        assert '"limitations"' in printed


def test_a_platform_command_reports_its_capability_state(capsys):
    assert main(["ask", "what is a circuit breaker?"]) == 0
    printed = capsys.readouterr().out
    assert "deterministic" in printed


def test_a_configured_platform_command_uses_the_configured_project(
    capsys, monkeypatch, single_project
):
    """With configuration present the CLI runs through the wired agent."""
    import httpx

    from tests.conftest import github_transport, jira_transport

    real_client = httpx.AsyncClient

    def client(*args, **kwargs):
        base = str(kwargs.get("base_url", ""))
        kwargs["transport"] = github_transport() if "github" in base else jira_transport()
        return real_client(*args, **kwargs)

    monkeypatch.setattr("devlens.app.dependencies.httpx.AsyncClient", client)
    assert main(["ask", "why is checkout slow?"]) == 0
    assert '"task"' in capsys.readouterr().out


def test_a_ticket_command_runs_against_a_configured_project(
    capsys, monkeypatch, single_project
):
    import httpx

    from tests.conftest import github_transport, jira_transport

    real_client = httpx.AsyncClient

    def client(*args, **kwargs):
        base = str(kwargs.get("base_url", ""))
        kwargs["transport"] = github_transport() if "github" in base else jira_transport()
        return real_client(*args, **kwargs)

    monkeypatch.setattr("devlens.app.dependencies.httpx.AsyncClient", client)
    assert main(["ticket", "DEV-1", "--repository", "org/repo"]) == 0
    printed = capsys.readouterr().out
    assert '"ticket"' in printed
    assert "jira-token" not in printed, "a credential must never reach stdout"


def test_a_review_command_reports_a_refusal_for_a_repository_off_the_allowlist(
    capsys, monkeypatch, single_project
):
    import httpx

    from tests.conftest import github_transport, jira_transport

    real_client = httpx.AsyncClient

    def client(*args, **kwargs):
        base = str(kwargs.get("base_url", ""))
        kwargs["transport"] = github_transport() if "github" in base else jira_transport()
        return real_client(*args, **kwargs)

    monkeypatch.setattr("devlens.app.dependencies.httpx.AsyncClient", client)
    code = main(["review", "--repository", "org/forbidden", "--pr", "7"])
    output = capsys.readouterr()
    assert code == 1
    assert "policy_denied" in output.err
    assert "Traceback" not in output.err


def test_the_version_flag_reports_the_version(capsys):
    from devlens.domain import DEVLENS_VERSION

    with pytest.raises(SystemExit) as exit_code:
        main(["--version"])
    assert exit_code.value.code == 0
    assert DEVLENS_VERSION in capsys.readouterr().out
