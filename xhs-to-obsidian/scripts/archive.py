"""
archive.py – Write a note into the Obsidian vault.

Reads:  <work_dir>/metadata.json
        <work_dir>/analysis.json   (optional; defaults gracefully)
        <work_dir>/media/*

Writes: <OBSIDIAN_VAULT_PATH>/0_inbox/xhs/<YYYY-MM-DD>-<slug>.md
        <OBSIDIAN_VAULT_PATH>/attachments/xhs/<xhs_id>/<filename>

Returns: dict with { "vault_path": "...", "rel_path": "..." }
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


class ArchiveError(Exception):
    pass


# ---------------------------------------------------------------------------
# Slug generation
# ---------------------------------------------------------------------------

def _slugify(text: str, max_len: int = 60) -> str:
    """
    Best-effort ASCII slug from a (possibly Chinese) title.
    Falls back to 'note' if nothing survives.
    """
    # Keep ASCII alphanumeric and spaces
    ascii_only = re.sub(r"[^\x00-\x7F]", "", text)
    # Replace non-alnum runs with hyphens
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    if not slug:
        # All Chinese / no ASCII – use first 8 hex chars of a hash as fallback
        import hashlib
        slug = hashlib.md5(text.encode()).hexdigest()[:8]
    return slug[:max_len]


# ---------------------------------------------------------------------------
# Existing note lookup
# ---------------------------------------------------------------------------

def _find_existing_note(inbox_dir: Path, xhs_id: str) -> Path | None:
    """Return the path of an existing note with this xhs_id, or None."""
    for md_file in inbox_dir.glob("*.md"):
        try:
            if f"xhs_id: {xhs_id}" in md_file.read_text(encoding="utf-8")[:500]:
                return md_file
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Relative path from note to attachment
# ---------------------------------------------------------------------------

def _rel_attachment_path(note_path: Path, attachment_path: Path) -> str:
    """Return the relative POSIX path from note_path to attachment_path."""
    try:
        return attachment_path.relative_to(note_path.parent).as_posix()
    except ValueError:
        # Note is not a parent; compute proper relative path
        rel = os.path.relpath(attachment_path, note_path.parent)
        return Path(rel).as_posix()


# ---------------------------------------------------------------------------
# Main archive function
# ---------------------------------------------------------------------------

def archive(work_dir: Path, priority: str = "medium", status: str = "lite") -> dict:
    """
    Archive the note from *work_dir* into the Obsidian vault.

    Returns a dict with vault_path and summary info for ingest.py.
    """
    vault_root = os.environ.get("OBSIDIAN_VAULT_PATH", "")
    if not vault_root:
        raise ArchiveError("OBSIDIAN_VAULT_PATH environment variable is not set")
    vault = Path(vault_root).expanduser()
    if not vault.is_dir():
        raise ArchiveError(f"Vault directory not found: {vault}")

    # ---- Load metadata ----
    meta_path = work_dir / "metadata.json"
    if not meta_path.exists():
        raise ArchiveError(f"metadata.json not found in {work_dir}")
    metadata = json.loads(meta_path.read_text())

    # ---- Load analysis (optional) ----
    analysis_path = work_dir / "analysis.json"
    if analysis_path.exists():
        analysis = json.loads(analysis_path.read_text())
    else:
        analysis = {
            "ocr_text": {},
            "content_type": "other",
            "suggested_tags": [],
            "summary": metadata.get("title", ""),
        }

    # ---- Load summary (tagline + category) ----
    summary_path = work_dir / "summary.json"
    if summary_path.exists():
        summary_data = json.loads(summary_path.read_text())
    else:
        summary_data = {
            "tagline": metadata.get("title", "") or "（无标题）",
            "category": "其他",
        }
    tagline = summary_data.get("tagline") or metadata.get("title", "")
    category = summary_data.get("category") or "其他"

    xhs_id = metadata["xhs_id"]
    title = metadata.get("title") or "Untitled"

    # ---- Determine destination paths ----
    note_date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    if metadata.get("published_at"):
        try:
            # Handle both ISO strings and plain date strings
            raw_dt = metadata["published_at"]
            if "T" in raw_dt:
                note_date = raw_dt[:10]
            elif len(raw_dt) >= 10:
                note_date = raw_dt[:10]
        except Exception:
            pass

    slug = _slugify(title)
    inbox_dir = vault / "0_inbox" / "xhs"
    inbox_dir.mkdir(parents=True, exist_ok=True)

    note_filename = f"{note_date}-{slug}.md"
    note_path = inbox_dir / note_filename

    # If a note with this xhs_id already exists, update it in place.
    existing = _find_existing_note(inbox_dir, xhs_id)
    if existing:
        note_path = existing

    # ---- Copy media files ----
    attach_dir = vault / "attachments" / "xhs" / xhs_id
    attach_dir.mkdir(parents=True, exist_ok=True)

    media_dir = work_dir / "media"
    media_rel_paths: list[str] = []
    for fname in metadata.get("media_local", []):
        if fname.startswith("MISSING_"):
            continue
        src = media_dir / fname
        dst = attach_dir / fname
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
        if dst.exists() or src.exists():
            rel = _rel_attachment_path(note_path, attach_dir / fname)
            media_rel_paths.append(rel)

    # ---- Merge tags ----
    meta_tags = metadata.get("tags", [])
    analysis_tags = analysis.get("suggested_tags", [])
    seen: set[str] = set()
    merged_tags: list[str] = []
    for t in meta_tags + analysis_tags:
        if t and t not in seen:
            seen.add(t)
            merged_tags.append(t)

    # ---- Build OCR sections for template ----
    ocr_raw = analysis.get("ocr_text", {})
    ocr_sections: list[dict] = []
    if isinstance(ocr_raw, dict):
        for i, (fname, text) in enumerate(ocr_raw.items()):
            if text and text.strip():
                ocr_sections.append({"label": f"图片 {i + 1}（{fname}）", "text": text.strip()})
    elif isinstance(ocr_raw, str) and ocr_raw.strip():
        ocr_sections.append({"label": "全文", "text": ocr_raw.strip()})

    # ---- Render template ----
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    tmpl = env.get_template("note.md.j2")

    note_content = tmpl.render(
        source_url=metadata["source_url"],
        xhs_id=xhs_id,
        author=metadata.get("author", ""),
        author_id=metadata.get("author_id", ""),
        captured_at=metadata.get("captured_at", ""),
        published_at=metadata.get("published_at", ""),
        category=category,
        tagline=tagline,
        tags=merged_tags,
        media=media_rel_paths,
        status=status,
        priority=priority,
        title=title,
        body=metadata.get("body", ""),
        ocr_sections=ocr_sections,
    )

    note_path.write_text(note_content, encoding="utf-8")

    return {
        "vault_path": str(note_path),
        "rel_path": str(note_path.relative_to(vault)),
        "xhs_id": xhs_id,
        "title": title,
        "category": category,
        "tagline": tagline,
        "tags": merged_tags,
        "media_count": len(media_rel_paths),
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python archive.py <work_dir> [--priority medium] [--status lite]")
        sys.exit(1)

    _work = Path(sys.argv[1])
    _priority = "medium"
    _status = "lite"
    args = sys.argv[2:]
    if "--priority" in args:
        _priority = args[args.index("--priority") + 1]
    if "--status" in args:
        _status = args[args.index("--status") + 1]

    try:
        out = archive(_work, priority=_priority, status=_status)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    except ArchiveError as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)
