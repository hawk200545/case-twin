"""Safe, format-aware extraction of clinical-note attachments."""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from pypdf import PdfReader

MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
MAX_EXTRACTED_CHARS = 60_000


class DocumentExtractionError(ValueError):
    """A user-safe attachment extraction failure."""


def _clean(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]*\n[ \t]*", "\n\n", text)
    return text.strip()[:MAX_EXTRACTED_CHARS]


def _pdf_text(raw: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:
                raise DocumentExtractionError("The PDF is password-protected and cannot be read.") from exc
        pages: list[str] = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text(extraction_mode="layout") or "")
            except TypeError:  # Compatible with older pypdf releases.
                pages.append(page.extract_text() or "")
        text = _clean("\n\n".join(pages))
    except DocumentExtractionError:
        raise
    except Exception as exc:
        raise DocumentExtractionError("The PDF could not be parsed.") from exc
    if len(text) < 20:
        raise DocumentExtractionError(
            "No selectable text was found in this PDF. Upload a text-searchable PDF or paste the clinical note."
        )
    return text


def _docx_text(raw: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            document = ElementTree.fromstring(archive.read("word/document.xml"))
        namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraphs = ["".join(node.text or "" for node in paragraph.findall(".//w:t", namespace))
                      for paragraph in document.findall(".//w:p", namespace)]
    except Exception as exc:
        raise DocumentExtractionError("The DOCX file could not be parsed.") from exc
    text = _clean("\n".join(paragraphs))
    if not text:
        raise DocumentExtractionError("No readable text was found in this DOCX file.")
    return text


def extract_document_text(filename: str | None, content_type: str | None, raw: bytes) -> str:
    """Return human-readable attachment text; never decode binary formats as UTF-8."""
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise DocumentExtractionError("The document exceeds the 25 MB extraction limit.")
    suffix = Path(filename or "").suffix.lower()
    media_type = (content_type or "").lower().split(";", 1)[0]
    is_pdf = suffix == ".pdf" or media_type == "application/pdf" or raw.startswith(b"%PDF-")
    if is_pdf:
        return _pdf_text(raw)
    if suffix == ".docx" or media_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return _docx_text(raw)
    if suffix == ".json" or media_type == "application/json":
        try:
            return _clean(json.dumps(json.loads(raw.decode("utf-8")), ensure_ascii=False, indent=2))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DocumentExtractionError("The JSON attachment is not valid UTF-8 JSON.") from exc
    if suffix in {"", ".txt", ".md", ".csv"} or media_type.startswith("text/"):
        try:
            text = _clean(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise DocumentExtractionError("The text attachment must be UTF-8 encoded.") from exc
        if not text:
            raise DocumentExtractionError("The text attachment is empty.")
        return text
    raise DocumentExtractionError("Supported clinical-note formats are PDF, DOCX, TXT, CSV, Markdown, and JSON.")
