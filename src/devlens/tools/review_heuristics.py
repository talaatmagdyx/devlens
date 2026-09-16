"""Deterministic pull-request heuristics.

These are pattern checks, not a semantic review, and each finding says so
through its status: a regex match on an added line is ``SUPPORTED`` because the
line demonstrably exists; a query-shaped call inside a loop is ``HYPOTHESIS``
because whether it is an N+1 depends on data the diff does not contain.

Every finding is anchored to a line the pull request actually changed. A
finding that points at untouched code is noise in a review, so the analysis is
restricted to added lines and, for whole-file checks, to the enclosing regions
of added lines.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from devlens.domain import Finding, PullRequestFile
from devlens.evidence import finding_band

SECRET = re.compile(
    r"(api[_-]?key|secret|password|passwd|token|credential)\s*[:=]\s*"
    r"['\"][^'\"]{8,}['\"]",
    re.I,
)
AWS_KEY = re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")
GITHUB_TOKEN = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")
SLACK_TOKEN = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")
EVAL_CALL = re.compile(r"\b(eval|exec|os\.system|subprocess\.call|popen)\s*\(")
SHELL_TRUE = re.compile(r"subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True")
BARE_EXCEPT = re.compile(r"except\s*:")
BARE_RESCUE = re.compile(r"rescue\s*(=>|$)")
#: Call shapes that reach a database. Deliberately anchored on the call rather
#: than on a bare word: ``where`` alone matched "elsewhere" in a comment, and
#: the most common Django n+1 — ``Model.objects.get(...)`` inside a loop — was
#: not matched at all.
QUERY = re.compile(
    r"(\.objects\.(get|filter|all|first|last|count|exists)\(|"
    r"\.find\(|\bfind_by\w*\(|\.where\(|\.query\(|\.execute\(|"
    r"\bSELECT\s|\bActiveRecord\b|\bsession\.(get|query|execute)\(|"
    r"\.fetchone\(|\.fetchall\(|\.scalar\(|\.aggregate\(|\.all\(\))",
    re.I,
)
LOOP = re.compile(r"^\s*(for\s|while\s)|\.each\s*(do|\{)|\.map\s*(do|\{)")

#: Placeholder values that look like secrets but are not.
PLACEHOLDER = re.compile(
    r"(xxx+|placeholder|example|changeme|your[_-]?(key|token|secret)|dummy|"
    r"<[^>]+>|\$\{[^}]+\}|\.\.\.)",
    re.I,
)


@dataclass
class Hunk:
    path: str
    added: list[tuple[int, str]]

    @property
    def lines(self) -> set[int]:
        return {number for number, _ in self.added}


def parse_added_lines(diff: str) -> dict[str, list[tuple[int, str]]]:
    """Map each file to its added lines, with correct post-image numbering."""
    files: dict[str, list[tuple[int, str]]] = {}
    path: str | None = None
    line_no = 0
    for raw in diff.splitlines():
        if raw.startswith("diff --git"):
            path, line_no = None, 0
            continue
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            path = None if target == "/dev/null" else target[2:] if target.startswith("b/") else target
            if path is not None:
                files.setdefault(path, [])
            continue
        if raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            line_no = int(match.group(1)) if match else 1
            continue
        if path is None or raw.startswith(("---", "index ", "new file", "deleted file",
                                           "similarity index", "rename ", "old mode",
                                           "new mode", "Binary files")):
            continue
        if raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        if raw.startswith("+"):
            files[path].append((line_no, raw[1:]))
            line_no += 1
        elif raw.startswith("-"):
            continue
        else:
            line_no += 1
    return files


def _finding(
    identifier: str,
    severity: str,
    status: str,
    category: str,
    title: str,
    *,
    path: str,
    line: int | None,
    evidence: list[str],
    scenario: str,
    impact: str,
    fix: str,
    test: str | None = None,
    corroborations: int = 1,
) -> Finding:
    return Finding(
        id=identifier,
        severity=severity,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        confidence=finding_band(status, corroborations),
        category=category,
        title=title,
        file=path,
        line=line,
        production_scenario=scenario,
        evidence=evidence,
        impact=impact,
        recommended_fix=fix,
        required_test=test,
        verification=test,
    )


def _secret_findings(path: str, line: int, text: str) -> list[Finding]:
    findings: list[Finding] = []
    stripped = text.strip()
    if not stripped or stripped.startswith(("#", "//", "*")):
        return findings
    hit = (
        AWS_KEY.search(text)
        or GITHUB_TOKEN.search(text)
        or SLACK_TOKEN.search(text)
        or PRIVATE_KEY.search(text)
    )
    generic = SECRET.search(text) and not PLACEHOLDER.search(text)
    if hit or generic:
        findings.append(
            _finding(
                f"SEC-{path}:{line}",
                "P0" if hit else "P1",
                "SUPPORTED" if hit else "HYPOTHESIS",
                "secret-exposure",
                "Possible credential committed in an added line.",
                path=path,
                line=line,
                evidence=[_mask(text)],
                scenario=(
                    "The credential reaches every clone of the repository and "
                    "every build log that prints the file."
                ),
                impact="Anyone with read access to the repository gains the credential.",
                fix="Remove it, rotate the credential, and load it from a secret manager.",
                test="Add a secret-scanning check to CI so this fails before merge.",
                corroborations=2 if hit else 1,
            )
        )
    if SHELL_TRUE.search(text):
        findings.append(
            _finding(
                f"SHELL-{path}:{line}",
                "P1",
                "SUPPORTED",
                "command-injection",
                "Subprocess invoked with shell=True on an added line.",
                path=path,
                line=line,
                evidence=[text.strip()[:200]],
                scenario="Any interpolated value becomes shell syntax.",
                impact="Command injection if any part of the command is caller-controlled.",
                fix="Pass an argument list and drop shell=True.",
                test="Add a test passing a value containing ';' and assert it is not executed.",
            )
        )
    elif EVAL_CALL.search(text):
        findings.append(
            _finding(
                f"EVAL-{path}:{line}",
                "P2",
                "HYPOTHESIS",
                "dynamic-execution",
                "Dynamic execution introduced on an added line.",
                path=path,
                line=line,
                evidence=[text.strip()[:200]],
                scenario="Reachable with attacker-influenced input, this executes it.",
                impact="Remote code execution if the argument is not fully controlled.",
                fix="Replace with an explicit allowlisted operation.",
                test="Add a test asserting untrusted input cannot reach this call.",
            )
        )
    if BARE_EXCEPT.search(text) or BARE_RESCUE.search(text):
        findings.append(
            _finding(
                f"EXC-{path}:{line}",
                "P3",
                "SUPPORTED",
                "error-handling",
                "Bare exception handler added.",
                path=path,
                line=line,
                evidence=[text.strip()[:200]],
                scenario="A failure here is swallowed and never surfaces in logs or metrics.",
                impact="Incidents take longer to diagnose because the error is invisible.",
                fix="Catch specific exceptions and record them.",
                test="Add a test asserting the failure path is logged.",
            )
        )
    return findings


def _nplusone(path: str, content: str, changed: set[int]) -> list[Finding]:
    """Query-shaped call inside a loop, restricted to regions this PR touched."""
    if path.endswith(".py"):
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return []
        lines = content.splitlines()
        findings = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                continue
            start = getattr(node, "lineno", 0)
            end = getattr(node, "end_lineno", start)
            if not changed & set(range(start, end + 1)):
                continue
            for number in range(start, min(end, len(lines)) + 1):
                text = lines[number - 1]
                if number != start and QUERY.search(text):
                    findings.append(
                        _finding(
                            f"NPLUS1-{path}:{start}",
                            "P2",
                            "HYPOTHESIS",
                            "performance",
                            "Query-shaped call inside a loop changed by this pull request.",
                            path=path,
                            line=number,
                            evidence=[text.strip()[:200]],
                            scenario=(
                                "With N rows this issues N queries; latency grows "
                                "linearly with data volume that does not exist yet in test."
                            ),
                            impact="Response time degrades as the table grows.",
                            fix="Batch or prefetch the related records outside the loop.",
                            test="Assert the query count stays constant as the row count grows.",
                        )
                    )
                    break
        return findings
    findings = []
    depth = 0
    loop_start = 0
    for index, line in enumerate(content.splitlines(), 1):
        if LOOP.search(line):
            depth += 1
            loop_start = loop_start or index
        if depth and QUERY.search(line) and index != loop_start and index in changed:
            findings.append(
                _finding(
                    f"NPLUS1-{path}:{index}",
                    "P2",
                    "HYPOTHESIS",
                    "performance",
                    "Query-shaped call inside a loop changed by this pull request.",
                    path=path,
                    line=index,
                    evidence=[line.strip()[:200]],
                    scenario="With N rows this issues N queries.",
                    impact="Response time degrades as the table grows.",
                    fix="Batch or prefetch the related records outside the loop.",
                    test="Assert the query count stays constant as the row count grows.",
                )
            )
        if depth and re.search(r"\bend\b|^\s*\}", line):
            depth = max(0, depth - 1)
            if not depth:
                loop_start = 0
    return findings


def missing_tests(files: list[PullRequestFile]) -> list[Finding]:
    paths = {item.path for item in files}
    findings = []
    for item in files:
        if item.status not in {"added", "renamed"} or item.binary:
            continue
        if not item.path.endswith((".py", ".rb", ".ts", ".js", ".go")):
            continue
        if re.search(r"(^|/)(tests?|spec)/|(^|/)(test_|_test\.|_spec\.|\.test\.|\.spec\.)", item.path):
            continue
        stem = re.sub(r"\.[a-z]+$", "", item.path.split("/")[-1])
        expected = (f"test_{stem}", f"{stem}_test", f"{stem}_spec", f"{stem}.test", f"{stem}.spec")
        if not any(token in path for path in paths for token in expected):
            findings.append(
                _finding(
                    f"TEST-{item.path}",
                    "P3",
                    "SUPPORTED",
                    "test-coverage",
                    "New source file has no matching test in this pull request.",
                    path=item.path,
                    line=None,
                    evidence=[item.path],
                    scenario="A regression in this file will not be caught by CI.",
                    impact="The behaviour is unverified and free to drift.",
                    fix="Add a regression test alongside the new file.",
                    test=f"Add a test covering {item.path}.",
                )
            )
    return findings


def review_changes(
    diff: str,
    files: list[PullRequestFile],
    contents: dict[str, str] | None = None,
) -> list[Finding]:
    """All heuristics, anchored to the lines this pull request actually changed."""
    added = parse_added_lines(diff)
    findings: list[Finding] = []
    for path, lines in added.items():
        for line, text in lines:
            findings.extend(_secret_findings(path, line, text))
    for path, content in (contents or {}).items():
        changed = {number for number, _ in added.get(path, [])}
        if changed:
            findings.extend(_nplusone(path, content, changed))
    findings.extend(missing_tests(files))
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    findings.sort(key=lambda item: (order[item.severity], item.file or "", item.line or 0))
    return findings


def _mask(text: str) -> str:
    """Never echo a suspected credential back into a report verbatim."""
    masked = re.sub(r"(['\"])([^'\"]{8,})(['\"])", r"\1***\3", text)
    masked = AWS_KEY.sub("AKIA***", masked)
    masked = GITHUB_TOKEN.sub("ghp_***", masked)
    masked = SLACK_TOKEN.sub("xox*-***", masked)
    return masked.strip()[:200]
