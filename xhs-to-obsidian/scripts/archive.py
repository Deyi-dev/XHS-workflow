"""
archive.py – Write a note into the Obsidian vault.

Reads:  <work_dir>/metadata.json
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
    ascii_only = re.sub(r"[^\x00-\x7F]", "", text)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    if not slug:
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
        rel = os.path.relpath(attachment_path, note_path.parent)
        return Path(rel).as_posix()


# ---------------------------------------------------------------------------
# Main archive function
# ---------------------------------------------------------------------------

def archive(work_dir: Path) -> dict:
    """
    Archive the note from *work_dir* into the Obsidian vault.

    Pure file placement: copy media into attachments/, render metadata.json
    through the template into 0_inbox/xhs/. No classification — that lives in
    _index.md and is applied separately by index.py after the agent decides.

    Returns a dict with vault_path, stem, and summary info for ingest.py.
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

    xhs_id = metadata["xhs_id"]
    title = metadata.get("title") or "Untitled"

    # ---- Determine destination paths ----
    note_date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    if metadata.get("published_at"):
        try:
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
        tags=metadata.get("tags", []),
        media=media_rel_paths,
        title=title,
        body=metadata.get("body", ""),
    )

    note_path.write_text(note_content, encoding="utf-8")

    return {
        "vault_path": str(note_path),
        "rel_path": str(note_path.relative_to(vault)),
        "stem": note_path.stem,
        "xhs_id": xhs_id,
        "title": title,
        "tags": metadata.get("tags", []),
        "media_count": len(media_rel_paths),
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python archive.py <work_dir>")
        sys.exit(1)

    _work = Path(sys.argv[1])

    try:
        out = archive(_work)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    except ArchiveError as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)
