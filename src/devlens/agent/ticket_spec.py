import re

from devlens.agent.context_builder import terms
from devlens.domain import TicketSpec

AC_LINE = re.compile(
    r"^\s*(?:ac\s*\d+[:.)\s-]+|acceptance criteria\s*[:.-]\s*|\*\s+|\-\s+|\d+[.)]\s+)",
    re.I,
)
EXPECTED = re.compile(
    r"(?:expected(?: behavior)?|should)\s*[:.-]\s*(.+)", re.I
)
OBSERVED = re.compile(
    r"(?:observed(?: behavior)?|actual|currently)\s*[:.-]\s*(.+)", re.I
)


def _named(value) -> str | None:
    if isinstance(value, dict):
        return value.get("displayName") or value.get("name")
    return None


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def build_ticket_spec(
    *,
    key: str,
    summary: str,
    description: str,
    comments: list[str],
    attachments,
    labels: list[str],
    components: list[str],
    custom_fields: dict[str, str],
) -> TicketSpec:
    body = description or ""
    acceptance = []
    for field_name, field_value in custom_fields.items():
        if "accept" in field_name.lower():
            acceptance.extend(_lines(field_value))
    if not acceptance:
        capturing = False
        for line in _lines(body):
            if re.search(r"acceptance criteria", line, re.I):
                capturing = True
                remainder = re.sub(r"^.*acceptance criteria\s*[:.-]?\s*", "", line, flags=re.I)
                if remainder:
                    acceptance.append(remainder)
                continue
            if capturing:
                if re.search(r"^(expected|observed|problem|notes)\b", line, re.I):
                    capturing = False
                else:
                    acceptance.append(re.sub(AC_LINE, "", line).strip() or line)
                    continue
            elif AC_LINE.match(line) and re.search(r"\bac\b", line, re.I):
                acceptance.append(re.sub(AC_LINE, "", line).strip() or line)
    expected = None
    observed = None
    for line in _lines(body) + comments:
        if expected is None and (match := EXPECTED.search(line)):
            expected = match.group(1).strip()
        if observed is None and (match := OBSERVED.search(line)):
            observed = match.group(1).strip()
    missing = []
    if not acceptance:
        missing.append("Acceptance criteria are not stated.")
    if not expected:
        missing.append("Expected behavior is not stated.")
    if not observed:
        missing.append("Observed behavior is not stated.")
    if not attachments:
        missing.append("No attachments were provided.")
    observations: list[str] = []
    for attachment in attachments:
        observations.extend(
            f"{attachment.filename}: {item}" for item in attachment.observations
        )
        if attachment.extracted_text:
            observations.append(
                f"{attachment.filename}: extracted {len(attachment.extracted_text)} characters of text."
            )
        if attachment.injection_suspected:
            # Surfaced from the flag, not only from the observation list: the
            # flag is what the rest of DevLens keys off, so the specification
            # must state it even when the observation is missing.
            observations.append(
                f"{attachment.filename}: contains instruction-like text. It is "
                "treated as data and is never passed to a model as an instruction."
            )
    observations = list(dict.fromkeys(observations))
    probable = sorted(terms(summary + " " + body) | {item.lower() for item in components + labels})
    return TicketSpec(
        key=key,
        problem=summary,
        expected_behavior=expected,
        observed_behavior=observed,
        acceptance_criteria=acceptance,
        important_comments=comments[-5:],
        attachment_observations=observations,
        probable_areas=probable[:20],
        missing_information=missing,
    )


def display_name(value) -> str | None:
    return _named(value)
