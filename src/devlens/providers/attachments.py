"""Attachment handling.

Attachment content is the least trustworthy input DevLens sees: it is arbitrary
bytes uploaded by anyone with Jira access. Everything here is therefore bounded
and defensive — decompression ratios are checked before extraction, member
names are validated against traversal, and extracted text is scanned for
instruction-like language and flagged so it can never be mistaken for a
directive.

Images are described by metadata only unless the ``screenshot_analysis``
capability is enabled, and even then the result is a set of observations. No
path in DevLens lets an image establish a root cause.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile

from devlens.domain import Attachment

TEXT_TYPES = frozenset(
    {
        "text/plain",
        "text/csv",
        "text/markdown",
        "application/json",
        "application/xml",
        "text/xml",
        "application/x-yaml",
        "text/yaml",
        "text/x-log",
    }
)
IMAGE_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
)
TEXT_SUFFIXES = (".txt", ".csv", ".json", ".md", ".log", ".yml", ".yaml", ".xml")

MAX_EXTRACT = 20_000
MAX_ZIP_MEMBERS = 20
MAX_ZIP_RATIO = 100
MAX_ZIP_TOTAL = 4_000_000

#: Phrasing that tries to talk to a model rather than describe a defect.
INJECTION = re.compile(
    r"(ignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)\s+instructions?"
    r"|disregard\s+(all\s+|your\s+)?(previous|prior|system)"
    r"|you\s+are\s+now\s+"
    r"|system\s*prompt"
    r"|</?(system|assistant|untrusted-data)>"
    r"|reveal\s+(your|the)\s+(prompt|instructions|environment)"
    r"|(print|send|exfiltrate|post)\s+.{0,40}(env(ironment)?|token|api[_ -]?key|secret)"
    r"|approve\s+all\s+"
    r"|rm\s+-rf\s+/)",
    re.I,
)


def scan_for_injection(text: str) -> bool:
    return bool(text) and bool(INJECTION.search(text))


def _safe_text(data: bytes) -> str | None:
    if b"\x00" in data:
        return None
    return data.decode("utf-8", errors="replace")[:MAX_EXTRACT]


def _pdf_text(data: bytes) -> str | None:
    """Best-effort text from an uncompressed PDF stream.

    This recovers literal strings only. Compressed content streams are not
    decoded, and when nothing is recovered the attachment says so rather than
    implying the PDF was empty.
    """
    if not data.startswith(b"%PDF"):
        return None
    chunks = []
    for match in re.finditer(rb"\((?:\\.|[^\\)]){3,}\)", data[:4_000_000]):
        raw = match.group()[1:-1].replace(rb"\(", b"(").replace(rb"\)", b")")
        text = raw.decode("latin-1")
        if any(32 <= ord(char) < 127 or char in "\n\t" for char in text):
            chunks.append(text)
        if sum(len(chunk) for chunk in chunks) > MAX_EXTRACT:
            break
    return "\n".join(chunks)[:MAX_EXTRACT] if chunks else None


def _png_size(data: bytes) -> str | None:
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        if 0 < width < 100_000 and 0 < height < 100_000:
            return f"{width}x{height}"
    return None


def _zip_text(data: bytes) -> tuple[str | None, list[str]]:
    """List and sample a zip, refusing archives that expand explosively."""
    notes: list[str] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, EOFError):
        return None, ["Archive could not be opened."]
    infos = archive.infolist()
    total = sum(info.file_size for info in infos)
    if total > MAX_ZIP_TOTAL or (data and total / max(len(data), 1) > MAX_ZIP_RATIO):
        return None, [
            "Archive expands far beyond its compressed size and was not extracted."
        ]
    parts: list[str] = []
    for index, info in enumerate(infos):
        if index >= MAX_ZIP_MEMBERS:
            notes.append(f"Archive listing truncated at {MAX_ZIP_MEMBERS} members.")
            break
        name = info.filename
        if name.startswith("/") or any(
            part in {"..", ""} for part in name.replace("\\", "/").split("/")[:-1]
        ):
            notes.append(f"Skipped unsafe archive member {name!r}.")
            continue
        parts.append(f"{name} ({info.file_size} bytes)")
        if info.file_size <= 100_000 and not name.endswith("/"):
            try:
                extracted = _safe_text(archive.read(info))
            except (zipfile.BadZipFile, RuntimeError, OSError):
                continue
            if extracted:
                parts.append(extracted[:2000])
    return ("\n".join(parts)[:MAX_EXTRACT] if parts else None), notes


def process_attachment(
    *,
    identifier: str,
    filename: str,
    mime_type: str,
    size_bytes: int,
    url: str,
    content: bytes | None,
) -> Attachment:
    mime = (mime_type or "application/octet-stream").split(";")[0].strip().lower()
    lowered = filename.lower()
    digest = hashlib.sha256(content).hexdigest() if content else None
    text: str | None = None
    observations: list[str] = []
    image_analysis: str | None = None

    if not content:
        observations.append("Attachment content was not downloaded.")
    elif mime in TEXT_TYPES or lowered.endswith(TEXT_SUFFIXES):
        text = _safe_text(content)
        if text is None:
            observations.append("Attachment is declared as text but contains NUL bytes.")
    elif mime == "application/pdf" or lowered.endswith(".pdf"):
        text = _pdf_text(content)
        if text is None:
            observations.append(
                "PDF text could not be extracted; its content streams are "
                "compressed. The attachment was not read."
            )
    elif mime == "application/zip" or lowered.endswith(".zip"):
        text, notes = _zip_text(content)
        observations.extend(notes)
    elif mime in IMAGE_TYPES:
        dimensions = _png_size(content)
        image_analysis = (
            "Image recorded. Pixels were not interpreted; enable "
            "screenshot_analysis for a description."
        )
        observations.append(
            "A screenshot records a symptom and cannot establish a root cause."
        )
        if dimensions:
            observations.append(f"Image dimensions are {dimensions}.")
        observations.append(
            "Next evidence required: logs, metrics and traces for the same window."
        )
    else:
        observations.append(f"No text extractor is registered for MIME type {mime}.")

    suspected = scan_for_injection(text or "")
    if suspected:
        observations.append(
            "This attachment contains instruction-like text. It is treated as "
            "data and is never passed to a model as an instruction."
        )
    return Attachment(
        id=identifier,
        filename=filename,
        mime_type=mime,
        size_bytes=size_bytes,
        url=url,
        sha256=digest,
        extracted_text=text,
        image_analysis=image_analysis,
        observations=observations,
        injection_suspected=suspected,
    )
