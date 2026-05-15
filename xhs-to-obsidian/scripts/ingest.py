"""
ingest.py – Orchestrate the XHS → Obsidian flow for one note.

Archive happens first and is content-independent (pure file placement).
Classification is a separate, later step: the agent reads the archived note,
picks a section from _index.md, and we record it there. No user confirmation.

  Phase 1 – archive + hand off:
      uv run scripts/ingest.py <xhs_url>

  Resolves, downloads to a temp dir, archives into the vault, then prints:

      {
        "status": "archived",
        "xhs_id": "...",
        "title": "...",
        "author": "...",
        "body": "...(truncated to 2000 chars)",
        "tags": [...],
        "note_type": "image",
        "rel_path": "0_inbox/xhs/2025-01-01-slug.md",
        "vault_path": "/abs/.../2025-01-01-slug.md",
        "stem": "2025-01-01-slug",
        "sections": ["🏂 Snowboard / Skiing", "🤖 Dev/AI/Tech", ...],
        "work_dir": "/tmp/xhs-<id>"
      }

  The agent then Reads vault_path for full content, picks one section.

  Phase 2 – record classification in _index.md:
      uv run scripts/ingest.py <xhs_url> --section "🤖 Dev/AI/Tech" \
          --tagline "一句话总结"

  (download is cache-hit, archive is idempotent.) Prints:

      {"status": "committed", "rel_path": "...", "section": "...", ...}

Error output (any phase):
      {
        "status": "error",
        "step": "download" | "archive" | "index",
        "error_code": "LOGIN_REQUIRED" | "RATE_LIMITED" | ...,
        "message": "...",
        "work_dir": "/tmp/xhs-<id>" | null
      }
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import click
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

_SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from download import DownloadError, download
from archive import ArchiveError, archive
import index as index_mod

_BODY_PREVIEW_CHARS = 2000


def _work_dir_for(xhs_id: str) -> Path:
    return Path(tempfile.gettempdir()) / f"xhs-{xhs_id}"


def _error(step: str, code: str, message: str, work_dir: Path | None = None) -> dict:
    return {
        "status": "error",
        "step": step,
        "error_code": code,
        "message": message,
        "work_dir": str(work_dir) if work_dir else None,
    }


@click.command()
@click.argument("xhs_url")
@click.option("--section", default="", help="Target _index.md section. Triggers phase 2.")
@click.option("--tagline", default="", help="One-line index summary (falls back to title).")
@click.option("--download-only", is_flag=True, default=False, help="Stop after download.")
def main(xhs_url, section, tagline, download_only):
    """Phase 1 (no --section) archives; phase 2 (with --section) records the section."""
    result = _run(
        xhs_url, section=section, tagline=tagline, download_only=download_only
    )
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == "error":
        sys.exit(1)


def _run(
    xhs_url: str,
    *,
    section: str = "",
    tagline: str = "",
    download_only: bool = False,
) -> dict:
    record_section = bool(section)

    # ---- Step 0: resolve URL ----
    print("[ingest] Step 0: resolving URL …", file=sys.stderr)
    try:
        feed_id, _ = asyncio.run(_resolve_only(xhs_url))
    except DownloadError as exc:
        return _error("download", exc.code, str(exc))
    except Exception as exc:
        return _error("download", "INVALID_URL", str(exc))

    work_dir = _work_dir_for(feed_id)
    print(f"[ingest] work_dir = {work_dir}", file=sys.stderr)

    # ---- Step 1: download to temp dir ----
    print("[ingest] Step 1: downloading …", file=sys.stderr)
    try:
        metadata = asyncio.run(download(xhs_url, work_dir))
    except DownloadError as exc:
        return _error("download", exc.code, str(exc), work_dir)
    except Exception as exc:
        return _error("download", "UNKNOWN", str(exc), work_dir)

    if download_only:
        return {
            "status": "downloaded",
            "xhs_id": metadata["xhs_id"],
            "title": metadata.get("title", ""),
            "work_dir": str(work_dir),
            "error": None,
        }

    # ---- Step 2: archive (pure file placement; idempotent on xhs_id) ----
    print("[ingest] Step 2: archiving to vault …", file=sys.stderr)
    try:
        archive_result = archive(work_dir)
    except ArchiveError as exc:
        return _error("archive", "ARCHIVE_FAILED", str(exc), work_dir)
    except Exception as exc:
        return _error("archive", "UNKNOWN", str(exc), work_dir)

    vault = Path(os.environ.get("OBSIDIAN_VAULT_PATH", "")).expanduser()

    # ---- Phase 1: hand off the archived note + section list ----
    if not record_section:
        try:
            sections = index_mod.list_sections(vault)
        except Exception as exc:
            return _error("index", "INDEX_READ_FAILED", str(exc), work_dir)
        return {
            "status": "archived",
            "xhs_id": metadata["xhs_id"],
            "title": metadata.get("title", ""),
            "author": metadata.get("author", ""),
            "body": (metadata.get("body") or "")[:_BODY_PREVIEW_CHARS],
            "tags": metadata.get("tags", []),
            "note_type": metadata.get("note_type", "image"),
            "rel_path": archive_result["rel_path"],
            "vault_path": archive_result["vault_path"],
            "stem": archive_result["stem"],
            "sections": sections,
            "work_dir": str(work_dir),
            "error": None,
        }

    # ---- Phase 2: record the chosen section in _index.md ----
    print(f"[ingest] Step 3: recording '{section}' in _index.md …", file=sys.stderr)
    tagline = tagline.strip() or metadata.get("title", "") or archive_result["stem"]
    try:
        index_mod.add_entry(vault, section, archive_result["stem"], tagline)
    except Exception as exc:
        return _error("index", "INDEX_WRITE_FAILED", str(exc), work_dir)

    return {
        "status": "committed",
        "xhs_id": metadata["xhs_id"],
        "title": metadata.get("title", ""),
        "author": metadata.get("author", ""),
        "note_type": metadata.get("note_type", "image"),
        "section": section,
        "tagline": tagline,
        "tags": metadata.get("tags", []),
        "rel_path": archive_result["rel_path"],
        "vault_path": archive_result["vault_path"],
        "stem": archive_result["stem"],
        "media_count": archive_result["media_count"],
        "work_dir": str(work_dir),
        "error": None,
    }


async def _resolve_only(url: str):
    from download import resolve_xhs_url as _r
    return await _r(url)


if __name__ == "__main__":
    main()
