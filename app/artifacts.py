"""Artifacts: things the agent produces that the user can actually open.

A reply is not a deliverable. This module is the difference: it writes a real
file, records it, and hands back a link that works from the watch or a browser.
"""

from __future__ import annotations

import re
import shutil
import uuid
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .config import settings
from typing import Any

from fastapi.staticfiles import StaticFiles

MAX_SLIDES = 30
MAX_TITLE_CHARS = 120
MAX_BULLET_CHARS = 200
MAX_BULLETS_PER_SLIDE = 8
# Files a job may hand over: things a person can open, not build debris.
ADOPT_SUFFIXES = {
    ".html": "html", ".htm": "html", ".md": "text", ".txt": "text",
    ".json": "text", ".csv": "text", ".py": "text", ".js": "text",
    ".css": "text", ".pdf": "pdf", ".pptx": "pptx", ".docx": "text",
}
MAX_ADOPT_BYTES = 5 * 1024 * 1024

SHORT_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


class ArtifactError(RuntimeError):
    kind = "invalid"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class ArtifactUnavailable(ArtifactError):
    kind = "unavailable"


class ArtifactStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: dict[str, Any]):
        response = await super().get_response(path, scope)
        response.headers["Content-Security-Policy"] = "sandbox"
        return response


def _safe_name(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", value or "").strip("-.")
    return (cleaned or fallback)[:60]


def _escape(value: str) -> str:
    """reportlab parses a mini-HTML, so user text must be escaped."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class ArtifactStore:
    def __init__(self, database: Any, root: Path) -> None:
        self.database = database
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._ensure_job_column()

    def _ensure_job_column(self) -> None:
        """Keep the migration next to the code that needs it.

        Older databases predate job-produced artifacts, and a table written by an
        earlier version has no job_id column; adding it here means no separate
        migration script to forget.
        """
        try:
            self.database.execute("ALTER TABLE artifacts ADD COLUMN job_id TEXT")
        except Exception:  # noqa: BLE001 - exists already, or table missing on first run
            pass

    # ---- creation ---------------------------------------------------
    def create_pptx(
        self,
        title: str,
        slides: list[dict[str, Any]],
        *,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        title = (title or "").strip()[:MAX_TITLE_CHARS]
        if not title:
            raise ArtifactError("缺少标题。")
        if not slides:
            raise ArtifactError("没有可放进 PPT 的内容。")
        if len(slides) > MAX_SLIDES:
            raise ArtifactError(f"页数超过上限（{MAX_SLIDES} 页）。")
        try:
            from pptx import Presentation
            from pptx.util import Pt
        except ImportError as exc:  # pragma: no cover - install-time issue
            raise ArtifactUnavailable("服务端没有安装 pptx 生成库。") from exc

        presentation = Presentation()
        cover = presentation.slides.add_slide(presentation.slide_layouts[0])
        cover.shapes.title.text = title
        subtitle = (
            str(slides[0].get("subtitle") or "").strip()
            if isinstance(slides[0], dict) else ""
        )
        if subtitle and len(cover.placeholders) > 1:
            cover.placeholders[1].text = subtitle[:MAX_BULLET_CHARS]

        for raw in slides:
            if not isinstance(raw, dict):
                continue
            slide_title = str(raw.get("title") or "").strip()[:MAX_TITLE_CHARS]
            bullets = [
                str(item).strip()[:MAX_BULLET_CHARS]
                for item in (raw.get("bullets") or [])[:MAX_BULLETS_PER_SLIDE]
                if str(item).strip()
            ]
            if not slide_title and not bullets:
                continue
            slide = presentation.slides.add_slide(presentation.slide_layouts[1])
            slide.shapes.title.text = slide_title or "（未命名）"
            body = slide.placeholders[1]
            frame = body.text_frame
            frame.text = bullets[0] if bullets else ""
            for line in bullets[1:]:
                paragraph = frame.add_paragraph()
                paragraph.text = line
                paragraph.font.size = Pt(18)

        artifact_id = uuid.uuid4().hex[:12]
        stored = f"{artifact_id}-{_safe_name(title, 'deck')}.pptx"
        path = self.root / stored
        try:
            presentation.save(str(path))
        except Exception as exc:  # noqa: BLE001 - disk problems must surface
            raise ArtifactUnavailable(f"写入文件失败：{type(exc).__name__}") from exc

        return self._record(
            artifact_id,
            kind="pptx",
            title=title,
            stored=stored,
            path=path,
            event_id=event_id,
        )

    def create_html(
        self,
        title: str,
        html: str,
        *,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        title = (title or "").strip()[:MAX_TITLE_CHARS]
        body = html or ""
        if not title:
            raise ArtifactError("缺少标题。")
        if not body.strip():
            raise ArtifactError("页面内容为空。")
        if len(body.encode("utf-8")) > 500_000:
            raise ArtifactError("页面太大（超过 500KB）。")
        artifact_id = uuid.uuid4().hex[:12]
        stored = f"{artifact_id}-{_safe_name(title, 'page')}.html"
        path = self.root / stored
        try:
            path.write_text(body, encoding="utf-8")
        except OSError as exc:
            raise ArtifactUnavailable(f"写入文件失败：{type(exc).__name__}") from exc
        return self._record(
            artifact_id,
            kind="html",
            title=title,
            stored=stored,
            path=path,
            event_id=event_id,
        )

    def create_pdf(
        self,
        title: str,
        sections: list[dict[str, Any]],
        *,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        title = (title or "").strip()[:MAX_TITLE_CHARS]
        if not title:
            raise ArtifactError("缺少标题。")
        if not sections:
            raise ArtifactError("没有可放进 PDF 的内容。")
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
            from reportlab.lib.units import mm
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.cidfonts import UnicodeCIDFont
            from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
        except ImportError as exc:  # pragma: no cover - install-time issue
            raise ArtifactUnavailable("服务端没有安装 PDF 生成库。") from exc

        # The CID font ships with reportlab, so Chinese works without font files.
        font_name = "STSong-Light"
        try:
            pdfmetrics.registerFont(UnicodeCIDFont(font_name))
        except Exception as exc:  # noqa: BLE001 - font problems must be visible
            raise ArtifactUnavailable(f"中文字体注册失败：{type(exc).__name__}") from exc

        artifact_id = uuid.uuid4().hex[:12]
        stored = f"{artifact_id}-{_safe_name(title, 'report')}.pdf"
        path = self.root / stored

        styles = getSampleStyleSheet()
        body = ParagraphStyle(
            "JarvisBody", parent=styles["Normal"], fontName=font_name,
            fontSize=10.5, leading=16, spaceAfter=4,
        )
        heading = ParagraphStyle(
            "JarvisHeading", parent=body, fontSize=14, spaceBefore=10, spaceAfter=6,
        )
        title_style = ParagraphStyle(
            "JarvisTitle", parent=body, fontSize=20, spaceAfter=14,
        )
        bullet = ParagraphStyle(
            "JarvisBullet", parent=body, leftIndent=12, bulletIndent=2,
        )

        story: list[Any] = [Paragraph(_escape(title), title_style), Spacer(1, 6)]
        for raw in sections:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("heading") or "").strip()[:MAX_TITLE_CHARS]
            if name:
                story.append(Paragraph(_escape(name), heading))
            for text in (raw.get("paragraphs") or [])[:40]:
                value = str(text).strip()
                if value:
                    story.append(Paragraph(_escape(value), body))
            for text in (raw.get("bullets") or [])[:40]:
                value = str(text).strip()
                if value:
                    story.append(Paragraph(_escape(value), bullet, bulletText="•"))
            story.append(Spacer(1, 6))

        try:
            document = SimpleDocTemplate(
                str(path), pagesize=A4,
                leftMargin=18 * mm, rightMargin=18 * mm,
                topMargin=16 * mm, bottomMargin=16 * mm,
                title=title,
            )
            document.build(story)
        except Exception as exc:  # noqa: BLE001 - surface the real cause
            raise ArtifactUnavailable(f"生成 PDF 失败：{type(exc).__name__}") from exc

        return self._record(
            artifact_id, kind="pdf", title=title, stored=stored, path=path,
            event_id=event_id,
        )

    def _record(
        self,
        artifact_id: str,
        *,
        kind: str,
        title: str,
        stored: str,
        path: Path,
        event_id: str | None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        size = path.stat().st_size
        created = datetime.now(timezone.utc).isoformat()
        for _ in range(10):
            code = "".join(secrets.choice(SHORT_CODE_ALPHABET) for _ in range(4))
            try:
                self.database.execute(
                    "INSERT INTO artifacts (id, kind, title, stored_name, bytes, event_id, created_at, short_code, job_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (artifact_id, kind, title, stored, size, event_id, created, code, job_id),
                )
                break
            except Exception as exc:
                if "UNIQUE" not in str(exc).upper():
                    raise
        else:
            raise RuntimeError("unable to allocate artifact short code")
        return {
            "id": artifact_id,
            "kind": kind,
            "title": title,
            "bytes": size,
            "created_at": created,
            "path": f"/files/{stored}",
            "url": f"{settings.public_base_url}/files/{stored}",
            "code": code,
        }

    # ---- reading ----------------------------------------------------
    def adopt(
        self,
        source: Path,
        *,
        title: str | None = None,
        event_id: str | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Take a file the job produced and make it a delivered artifact.

        The gap this closes: a job writes into its workspace, which the user
        cannot reach. Jobs make things; artifacts are how things get handed over.
        """
        path = Path(source)
        if not path.is_file():
            raise ArtifactError(f"文件不存在：{source}")
        suffix = path.suffix.lower()
        if suffix not in ADOPT_SUFFIXES:
            raise ArtifactError(f"这种格式不当作交付物：{suffix or '(无扩展名)'}")
        size = path.stat().st_size
        if size > MAX_ADOPT_BYTES:
            raise ArtifactError(f"文件太大（{size // 1024}KB），上限 {MAX_ADOPT_BYTES // 1024 // 1024}MB。")
        if size == 0:
            raise ArtifactError("文件是空的。")

        artifact_id = uuid.uuid4().hex[:12]
        stored = f"{artifact_id}-{_safe_name(path.stem, 'file')}{suffix}"
        try:
            shutil.copyfile(path, self.root / stored)
        except OSError as exc:
            raise ArtifactUnavailable(f"复制文件失败：{type(exc).__name__}") from exc
        return self._record(
            artifact_id,
            kind=ADOPT_SUFFIXES[suffix],
            title=(title or path.name)[:MAX_TITLE_CHARS],
            stored=stored,
            path=self.root / stored,
            event_id=event_id,
            job_id=job_id,
        )

    def for_job(self, job_id: str) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            "SELECT id, kind, title, stored_name, bytes, created_at, short_code "
            "FROM artifacts WHERE job_id = ? ORDER BY created_at",
            (job_id,),
        )
        return [
            {
                "id": row["id"],
                "kind": row["kind"],
                "title": row["title"],
                "bytes": row["bytes"],
                "created_at": row["created_at"],
                "path": f"/files/{row['stored_name']}",
                "url": f"{settings.public_base_url}/files/{row['stored_name']}",
                "code": row["short_code"],
            }
            for row in rows
        ]

    def list(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            "SELECT id, kind, title, stored_name, bytes, created_at, short_code FROM artifacts "
            "ORDER BY created_at DESC LIMIT ?",
            (max(1, min(50, limit)),),
        )
        return [
            {
                "id": row["id"],
                "kind": row["kind"],
                "title": row["title"],
                "bytes": row["bytes"],
                "created_at": row["created_at"],
                "path": f"/files/{row['stored_name']}",
                "url": f"{settings.public_base_url}/files/{row['stored_name']}",
                "code": row.get("short_code"),
            }
            for row in rows
        ]

    def get(self, artifact_id: str) -> dict[str, Any] | None:
        for item in self.list(50):
            if item["id"] == artifact_id:
                return item
        return None

    def get_by_code(self, code: str) -> dict[str, Any] | None:
        row = self.database.fetch_one(
            "SELECT id, kind, title, stored_name, bytes, created_at, short_code FROM artifacts WHERE short_code = ?",
            (code.upper(),),
        )
        if not row:
            return None
        return {
            "id": row["id"], "kind": row["kind"], "title": row["title"],
            "bytes": row["bytes"], "created_at": row["created_at"],
            "path": f"/files/{row['stored_name']}",
            "url": f"{settings.public_base_url}/files/{row['stored_name']}",
            "code": row.get("short_code"),
        }
