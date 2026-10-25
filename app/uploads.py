"""Inbound material: files the user hands to Jarvis.

The read side of the network was one half of "the material gap"; this is the
other. Uploaded files are stored, never executed, and their text is extracted
once so the agent can read it later with a read-only capability.

Extraction is best-effort by design: a format we cannot read must say so, not
return mojibake. PDF and Word support is optional at import time so a missing
dependency degrades to an honest message instead of breaking uploads.
"""

from __future__ import annotations

import io
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import settings
from typing import Any

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".json", ".csv", ".log", ".html", ".htm", ".xml", ".yaml", ".yml"}
ALLOWED_EXTENSIONS = TEXT_EXTENSIONS | {".pdf", ".docx"}


class UploadRefused(RuntimeError):
    kind = "refused"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _safe_name(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", value or "").strip("-.")
    return (cleaned or fallback)[:80]


def _strip_html(raw: str) -> str:
    from .webread import _TextExtractor

    parser = _TextExtractor()
    parser.feed(raw)
    return parser.text()


def extract_text(filename: str, data: bytes) -> tuple[str, str]:
    """Return (text, note). `note` explains a partial or missing extraction."""
    suffix = Path(filename).suffix.lower()

    if suffix in {".txt", ".md", ".markdown", ".log", ".yaml", ".yml", ".xml", ".csv"}:
        return data.decode("utf-8", "replace")[:MAX_TEXT_CHARS], ""
    if suffix == ".json":
        try:
            parsed = json.loads(data.decode("utf-8", "replace"))
            return json.dumps(parsed, ensure_ascii=False, indent=2)[:MAX_TEXT_CHARS], ""
        except json.JSONDecodeError:
            return data.decode("utf-8", "replace")[:MAX_TEXT_CHARS], ""
    if suffix in {".html", ".htm"}:
        return _strip_html(data.decode("utf-8", "replace"))[:MAX_TEXT_CHARS], ""
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return "", "服务端没有装 PDF 解析库，只能保存文件，读不了内容。"
        try:
            reader = PdfReader(io.BytesIO(data))
            pages = [page.extract_text() or "" for page in reader.pages]
        except Exception as exc:  # noqa: BLE001 - corrupted files must not crash
            return "", f"PDF 解析失败：{type(exc).__name__}"
        text = "\n\n".join(pages).strip()
        note = "" if text else "这份 PDF 里没有可提取的文字（可能是扫描件）。"
        return text[:MAX_TEXT_CHARS], note
    if suffix == ".docx":
        try:
            import docx  # type: ignore
        except ImportError:
            return "", "服务端没有装 Word 解析库，只能保存文件，读不了内容。"
        try:
            document = docx.Document(io.BytesIO(data))
            parts = [paragraph.text for paragraph in document.paragraphs]
        except Exception as exc:  # noqa: BLE001
            return "", f"Word 解析失败：{type(exc).__name__}"
        return "\n".join(parts)[:MAX_TEXT_CHARS], ""
    return "", f"不认识这个格式（{suffix or '无扩展名'}），只能保存文件。"


class UploadStore:
    def __init__(self, database: Any, root: Path) -> None:
        self.database = database
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        filename: str,
        data: bytes,
        *,
        content_type: str = "",
        event_id: str | None = None,
    ) -> dict[str, Any]:
        name = (filename or "").strip()
        if not name:
            raise UploadRefused("没有文件名。")
        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            allowed = "、".join(sorted(ALLOWED_EXTENSIONS))
            raise UploadRefused(f"不支持 {suffix or '这个格式'}。可以传：{allowed}")
        if not data:
            raise UploadRefused("文件是空的。")
        if len(data) > MAX_UPLOAD_BYTES:
            raise UploadRefused(
                f"文件太大（{len(data) // 1024}KB），上限 {MAX_UPLOAD_BYTES // 1024 // 1024}MB。"
            )

        upload_id = uuid.uuid4().hex[:12]
        safe = _safe_name(Path(name).stem, "upload")
        stored = f"{upload_id}-{safe}{suffix}"
        path = self.root / stored
        path.write_bytes(data)

        text, note = extract_text(name, data)
        text_name = f"{upload_id}.txt"
        (self.root / text_name).write_text(text, encoding="utf-8")

        created = datetime.now(timezone.utc).isoformat()
        self.database.execute(
            "INSERT INTO uploads (id, filename, stored_name, content_type, bytes, chars, "
            "note, event_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (upload_id, name, stored, content_type, len(data), len(text), note, event_id, created),
        )
        return {
            "id": upload_id,
            "filename": name,
            "bytes": len(data),
            "chars": len(text),
            "note": note,
            "created_at": created,
            "path": f"/files/uploads/{stored}",
            "url": f"{settings.public_base_url}/files/uploads/{stored}",
        }

    def list(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            "SELECT * FROM uploads ORDER BY created_at DESC LIMIT ?",
            (max(1, min(50, limit)),),
        )
        return [self._row(row) for row in rows]

    def get(self, upload_id: str) -> dict[str, Any] | None:
        row = self.database.fetch_one("SELECT * FROM uploads WHERE id = ?", (upload_id,))
        return self._row(row) if row else None

    def read_text(self, upload_id: str) -> dict[str, Any]:
        row = self.database.fetch_one("SELECT * FROM uploads WHERE id = ?", (upload_id,))
        if not row:
            raise UploadRefused(f"没有编号为 {upload_id} 的文件。")
        path = self.root / f"{row['id']}.txt"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if not text:
            reason = row["note"] or "这个文件里没有可读的文字。"
            raise UploadRefused(f"{row['filename']}：{reason}")
        return {
            "id": row["id"],
            "filename": row["filename"],
            "chars": len(text),
            "text": text,
        }

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "filename": row["filename"],
            "bytes": row["bytes"],
            "chars": row["chars"],
            "note": row["note"] or "",
            "created_at": row["created_at"],
            "url": f"{settings.public_base_url}/files/uploads/{row['stored_name']}",
        }
