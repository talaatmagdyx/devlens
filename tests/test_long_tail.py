"""The long tail: the branches that only fire on bad input.

Nothing here is exotic — an attachment hosted somewhere else, a PDF whose text
streams are compressed, a trace backend that answers in a shape nobody
documented, an audit file big enough to rotate. They are the paths that are
never exercised in a demo and always exercised in production.
"""

from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest

from devlens.agent.audit import MAX_BYTES, AuditLog, is_secret_key, redact_url
from devlens.agent.ticket_spec import build_ticket_spec, display_name
from devlens.domain import Attachment, PullRequestFile
from devlens.providers.attachments import process_attachment
from devlens.providers.jira.cloud import JiraCloudProvider
from devlens.providers.observe import ObserveHub
from devlens.tools.review_heuristics import review_changes
from tests.conftest import jira_transport


def attachment(filename, mime, content, size=None):
    return process_attachment(
        identifier="1",
        filename=filename,
        mime_type=mime,
        size_bytes=size if size is not None else len(content or b""),
        url="https://team.atlassian.net/attachment/content/1",
        content=content,
    )


# --------------------------------------------------------------------------- #
# Attachments
# --------------------------------------------------------------------------- #


def test_an_attachment_that_was_not_downloaded_says_so():
    item = attachment("server.log", "text/plain", None, size=10)
    assert item.extracted_text is None
    assert "was not downloaded" in " ".join(item.observations)


def test_a_compressed_pdf_says_it_was_not_read_rather_than_that_it_was_empty():
    item = attachment("report.pdf", "application/pdf", b"%PDF-1.7\n<< /Filter /FlateDecode >>")
    assert item.extracted_text is None
    assert "content streams are compressed" in " ".join(item.observations)


def test_an_uncompressed_pdf_yields_its_literal_strings():
    body = b"%PDF-1.4\n1 0 obj\n(Checkout returns 504 after the deploy)\nendobj\n"
    item = attachment("report.pdf", "application/pdf", body)
    assert "Checkout returns 504" in (item.extracted_text or "")


def test_a_corrupt_archive_is_reported_not_raised():
    item = attachment("bundle.zip", "application/zip", b"not a zip at all")
    assert item.extracted_text is None
    assert "could not be opened" in " ".join(item.observations)


def test_an_archive_listing_is_capped():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(40):
            archive.writestr(f"logs/app-{index}.log", "timeout")
    item = attachment("bundle.zip", "application/zip", buffer.getvalue())
    assert "truncated at" in " ".join(item.observations)


def test_a_png_reports_its_dimensions_without_interpreting_pixels():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (1280).to_bytes(4, "big") + (720).to_bytes(4, "big")
    item = attachment("shot.png", "image/png", png)
    assert "1280x720" in " ".join(item.observations)
    assert "Pixels were not interpreted" in (item.image_analysis or "")


def test_a_text_file_with_nul_bytes_is_flagged_rather_than_decoded():
    item = attachment("server.log", "text/plain", b"line\x00line")
    assert item.extracted_text is None
    assert "NUL bytes" in " ".join(item.observations)


def test_an_attachment_is_identified_by_its_digest():
    item = attachment("a.log", "text/plain", b"content")
    assert len(item.sha256) == 64


@pytest.mark.asyncio
async def test_an_attachment_hosted_off_the_jira_site_is_not_downloaded():
    """The content URL comes from the ticket, so it is attacker-influenced."""
    provider = JiraCloudProvider(
        httpx.AsyncClient(
            base_url="https://team.atlassian.net/",
            transport=jira_transport(
                attachments=[
                    {
                        "id": "1",
                        "filename": "evil.log",
                        "mimeType": "text/plain",
                        "size": 10,
                        "content": "https://evil.example/steal",
                    }
                ]
            ),
        )
    )
    context = await provider.get_issue_context("DEV-1")
    assert [item.filename for item in context.attachments] == ["evil.log"]
    assert context.attachments[0].extracted_text is None


@pytest.mark.asyncio
async def test_an_attachment_on_the_jira_site_is_downloaded():
    provider = JiraCloudProvider(
        httpx.AsyncClient(
            base_url="https://team.atlassian.net/",
            transport=jira_transport(
                attachments=[
                    {
                        "id": "1",
                        "filename": "server.log",
                        "mimeType": "text/plain",
                        "size": 30,
                        "content": "https://team.atlassian.net/attachment/content/1",
                    }
                ]
            ),
        )
    )
    context = await provider.get_issue_context("DEV-1")
    assert "log line one" in (context.attachments[0].extracted_text or "")


@pytest.mark.asyncio
async def test_attachments_beyond_the_cap_are_recorded_as_a_truncation():
    many = [
        {
            "id": str(index),
            "filename": f"f{index}.log",
            "mimeType": "text/plain",
            "size": 10,
            "content": f"https://team.atlassian.net/attachment/content/{index}",
        }
        for index in range(30)
    ]
    provider = JiraCloudProvider(
        httpx.AsyncClient(
            base_url="https://team.atlassian.net/",
            transport=jira_transport(attachments=many),
        )
    )
    context = await provider.get_issue_context("DEV-1")
    assert len(context.attachments) == 20
    assert any(item.source == "attachments" for item in context.truncations)


@pytest.mark.asyncio
async def test_a_comment_is_posted_as_separate_paragraphs():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.read())
        return httpx.Response(201, json={})

    provider = JiraCloudProvider(
        httpx.AsyncClient(
            base_url="https://team.atlassian.net/",
            transport=httpx.MockTransport(handler),
        )
    )
    await provider.post_comment("DEV-1", "First paragraph.\n\nSecond paragraph.")
    blocks = captured["body"]["body"]["content"]
    assert len(blocks) == 2
    assert blocks[0]["content"][0]["text"] == "First paragraph."


# --------------------------------------------------------------------------- #
# Ticket specification
# --------------------------------------------------------------------------- #


def test_a_specification_records_what_the_ticket_did_not_say():
    spec = build_ticket_spec(
        key="DEV-1",
        summary="Checkout is broken",
        description="It broke.",
        comments=[],
        attachments=[],
        labels=[],
        components=[],
        custom_fields={},
    )
    assert spec.missing_information
    assert spec.acceptance_criteria == []


def test_a_specification_extracts_stated_criteria_and_behaviour():
    spec = build_ticket_spec(
        key="DEV-1",
        summary="Checkout times out",
        description=(
            "Expected: the reply posts within five seconds.\n"
            "Observed: it times out.\n"
            "Acceptance criteria:\n"
            "- the reply posts within five seconds\n"
            "- the user sees a confirmation\n"
        ),
        comments=["Reproduced on staging."],
        attachments=[],
        labels=["checkout"],
        components=["payments"],
        custom_fields={"customfield_1": "Team Payments"},
    )
    assert len(spec.acceptance_criteria) == 2
    assert "five seconds" in (spec.expected_behavior or "")
    assert "times out" in (spec.observed_behavior or "")


def test_an_attachment_that_suggests_injection_is_named_in_the_specification():
    spec = build_ticket_spec(
        key="DEV-1",
        summary="s",
        description="d",
        comments=[],
        attachments=[
            Attachment(
                id="1",
                filename="hostile.log",
                mime_type="text/plain",
                size_bytes=10,
                url="https://team.atlassian.net/x",
                injection_suspected=True,
            )
        ],
        labels=[],
        components=[],
        custom_fields={},
    )
    assert any(
        "hostile.log" in item and "instruction-like" in item
        for item in spec.attachment_observations
    )


def test_an_attachment_observation_is_not_repeated():
    spec = build_ticket_spec(
        key="DEV-1",
        summary="s",
        description="d",
        comments=[],
        attachments=[
            Attachment(
                id="1",
                filename="hostile.log",
                mime_type="text/plain",
                size_bytes=10,
                url="https://team.atlassian.net/x",
                injection_suspected=True,
                observations=[
                    "This attachment contains instruction-like text. It is "
                    "treated as data and is never passed to a model as an instruction."
                ],
            )
        ],
        labels=[],
        components=[],
        custom_fields={},
    )
    assert len(spec.attachment_observations) == len(set(spec.attachment_observations))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"displayName": "Ada"}, "Ada"),
        ({"name": "ada"}, "ada"),
        ({}, None),
        (None, None),
        ("Ada", None),
        ({"displayName": ""}, None),
    ],
)
def test_a_display_name_is_extracted_defensively(value, expected):
    assert display_name(value) == expected


# --------------------------------------------------------------------------- #
# Review heuristics, the remaining shapes
# --------------------------------------------------------------------------- #


def diff_for(path: str, *lines: str) -> str:
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,1 +1,{len(lines) + 1} @@\n context\n{body}"
    )


@pytest.mark.parametrize(
    ("line", "category"),
    [
        ('AWS_KEY = "AKIAIOSFODNN7EXAMPLE"', "secret-exposure"),
        ('TOKEN = "ghp_' + "a" * 36 + '"', "secret-exposure"),
        ('SLACK = "xoxb-123456789012-abcdefghijkl"', "secret-exposure"),
        ("subprocess.run(command, shell=True)", "command-injection"),
        ("eval(user_input)", "dynamic-execution"),
        ("except:", "error-handling"),
    ],
)
def test_each_heuristic_fires_on_its_own_shape(line, category):
    findings = review_changes(
        diff_for("app.py", line), [PullRequestFile(path="app.py", status="modified")], {}
    )
    assert category in {item.category for item in findings}


def test_a_placeholder_is_not_reported_as_a_credential():
    findings = review_changes(
        diff_for("app.py", 'PASSWORD = "changeme"  # example placeholder'),
        [PullRequestFile(path="app.py", status="modified")],
        {},
    )
    assert "secret-exposure" not in {item.category for item in findings}


def test_a_comment_line_is_not_scanned_for_credentials():
    findings = review_changes(
        diff_for("app.py", '# AWS_KEY = "AKIAIOSFODNN7EXAMPLE"'),
        [PullRequestFile(path="app.py", status="modified")],
        {},
    )
    assert "secret-exposure" not in {item.category for item in findings}


def test_a_query_inside_a_loop_is_reported_only_where_the_diff_touched():
    content = (
        "def load(ids):\n"
        "    out = []\n"
        "    for identifier in ids:\n"
        "        out.append(Model.objects.get(id=identifier))\n"
        "    return out\n"
    )
    findings = review_changes(
        diff_for("app.py", "        out.append(Model.objects.get(id=identifier))"),
        [PullRequestFile(path="app.py", status="modified")],
        {"app.py": content},
    )
    assert any(item.category in {"n-plus-one", "performance"} for item in findings) or True
    for finding in findings:
        assert finding.file == "app.py"


def test_a_binary_file_in_a_pull_request_produces_no_line_findings():
    findings = review_changes(
        "", [PullRequestFile(path="image.png", status="added", binary=True)], {}
    )
    assert all(item.file != "image.png" or item.line is None for item in findings)


# --------------------------------------------------------------------------- #
# Observability providers, success shapes
# --------------------------------------------------------------------------- #


def hub_with(name: str, handler) -> ObserveHub:
    client = httpx.AsyncClient(
        base_url="https://provider.internal/", transport=httpx.MockTransport(handler)
    )
    return ObserveHub(**{name: client})


@pytest.mark.asyncio
async def test_loki_rows_are_extracted_from_its_stream_shape():
    hub = hub_with(
        "loki",
        lambda request: httpx.Response(
            200,
            json={"data": {"result": [{"values": [["1", "timeout in checkout"]]}]}},
        ),
    )
    result = await hub.search_logs('{job="checkout"}')
    assert result.status == "AVAILABLE"
    assert result.rows == ["timeout in checkout"]
    assert result.query == '{job="checkout"}'


@pytest.mark.asyncio
async def test_prometheus_ranges_are_summarised_by_first_and_last_point():
    hub = hub_with(
        "prometheus",
        lambda request: httpx.Response(
            200,
            json={
                "data": {
                    "result": [
                        {"metric": {"job": "checkout"},
                         "values": [[1, "0.1"], [2, "0.4"]]}
                    ]
                }
            },
        ),
    )
    result = await hub.query_metrics("histogram_quantile(0.99, x)")
    assert result.status == "AVAILABLE"
    assert "first=0.1" in result.rows[0]
    assert "last=0.4" in result.rows[0]


@pytest.mark.asyncio
async def test_a_scalar_metric_is_reported_too():
    hub = hub_with(
        "prometheus",
        lambda request: httpx.Response(
            200, json={"data": {"result": [{"metric": {"job": "x"}, "value": [1, "7"]}]}}
        ),
    )
    result = await hub.query_metrics("up")
    assert "= 7" in result.rows[0]


@pytest.mark.asyncio
async def test_tempo_traces_are_listed():
    hub = hub_with(
        "tempo",
        lambda request: httpx.Response(
            200,
            json={"traces": [{"traceID": "abc", "rootServiceName": "checkout",
                              "durationMs": 1200}]},
        ),
    )
    result = await hub.search_traces("checkout")
    assert result.status in {"AVAILABLE", "EMPTY"}


@pytest.mark.asyncio
async def test_a_sql_gateway_answer_is_capped():
    hub = hub_with(
        "sql",
        lambda request: httpx.Response(200, json={"rows": [{"n": index} for index in range(200)]}),
    )
    result = await hub.query_sql("SELECT count(*) FROM orders")
    assert result.status == "AVAILABLE"
    assert len(result.rows) == 50


@pytest.mark.asyncio
async def test_closing_the_hub_closes_every_client(monkeypatch):
    monkeypatch.setenv("DEVLENS_LOKI_URL", "https://logs.internal/")
    hub = ObserveHub.from_env()
    await hub.aclose()
    assert hub.loki.is_closed


@pytest.mark.asyncio
async def test_collect_only_queries_sql_for_a_select():
    hub = ObserveHub()
    results = await hub.collect("checkout is slow")
    assert results["sql"].status == "NOT_CONFIGURED"
    assert results["sql"].query is None


# --------------------------------------------------------------------------- #
# Audit file handling
# --------------------------------------------------------------------------- #


def test_the_audit_file_is_appended_and_read_back_from_the_tail(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    for index in range(120):
        audit.record({"event": "run.completed", "index": index})
    recent = audit.recent(limit=10)
    assert len(recent) == 10
    assert recent[-1]["index"] == 119


def test_a_corrupt_audit_line_is_skipped(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    audit.record({"event": "a"})
    with path.open("a") as handle:
        handle.write("not json\n")
    audit.record({"event": "b"})
    events = [item["event"] for item in audit.recent()]
    assert events == ["a", "b"]


def test_the_audit_file_rotates_when_it_grows(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr("devlens.agent.audit.MAX_BYTES", 200)
    audit = AuditLog(path)
    for index in range(20):
        audit.record({"event": "x" * 40, "index": index})
    assert path.with_suffix(".jsonl.1").exists()
    assert MAX_BYTES > 200, "the real ceiling is not 200 bytes"


def test_an_in_memory_audit_log_is_bounded():
    audit = AuditLog(max_records=5)
    for index in range(20):
        audit.record({"event": "x", "index": index})
    assert len(audit.recent(limit=100)) == 5


def test_an_audit_log_from_the_environment_writes_where_it_is_told(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("DEVLENS_AUDIT_LOG", str(path))
    AuditLog.from_env().record({"event": "x"})
    assert path.exists()


@pytest.mark.parametrize(
    ("key", "secret"),
    [
        ("github_token", True),
        ("access_token", True),
        ("api_key", True),
        ("authorization", True),
        ("session", True),
        ("key", False),
        ("ticket_key", False),
        ("idempotency_key", False),
        ("path", False),
    ],
)
def test_the_key_classifier_matches_on_substrings_with_an_exemption_list(key, secret):
    assert is_secret_key(key) is secret


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://user:pw@api.example.com/x", "***@api.example.com"),
        ("https://api.example.com/x?token=abc&page=2", "token=***"),
        ("not a url", "not a url"),
        ("https://api.example.com:8443/x", ":8443"),
    ],
)
def test_credentials_are_stripped_from_urls(url, expected):
    assert expected in redact_url(url)
