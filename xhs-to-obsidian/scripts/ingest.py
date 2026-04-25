"""
ingest.py – Orchestrate download → analyze → archive for one XHS note.

CLI contract
------------
Preview (no vault write):
    uv run scripts/ingest.py <xhs_url>

Commit (write to vault):
    uv run scripts/ingest.py <xhs_url> --commit

The script always prints a single JSON object to stdout so the calling agent
can parse it programmatically.

Preview output:
    {
      "status": "preview",
      "xhs_id": "...",
      "title": "...",
      "author": "...",
      "content_type": "recipe",
      "suggested_tags": ["..."],
      "summary": "...",
      "note_type": "image",
      "work_dir": "/tmp/xhs-<id>",
      "vault_path": null,
      "error": null
    }

Committed output (--commit):
    {
      "status": "committed",
      "vault_path": "/path/to/vault/0_inbox/xhs/2025-01-01-slug.md",
      "rel_path": "0_inbox/xhs/2025-01-01-slug.md",
      ...same fields...
    }

Error output:
    {
      "status": "error",
      "step": "download" | "analyze" | "archive",
      "error_code": "MCP_UNREACHABLE",
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

# Resolve sibling scripts regardless of CWD
_SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from download import DownloadError, download, resolve_xhs_url
from analyze import analyze
from archive import ArchiveError, archive


def _work_dir_for(xhs_id: str) -> Path:
    """Deterministic temp dir path per note; reused across calls."""
    base = Path(tempfile.gettempdir()) / f"xhs-{xhs_id}"
    return base


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
@click.option("--commit", is_flag=True, default=False, help="Write to Obsidian vault after analysis.")
@click.option("--priority", default="medium", show_default=True, help="Frontmatter priority field.")
@click.option("--status", "note_status", default="lite", show_default=True, help="Frontmatter status field.")
def main(xhs_url: str, commit: bool, priority: str, note_status: str):
    """
    Download, analyse, and optionally archive one Xiaohongshu note.

    Prints a JSON summary to stdout for agent consumption.
    All progress messages go to stderr.
    """
    result = _run(xhs_url, commit=commit, priority=priority, note_status=note_status)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == "error":
        sys.exit(1)


def _run(xhs_url: str, *, commit: bool, priority: str, note_status: str) -> dict:
    # ------------------------------------------------------------------ #
    # Step 0 – Resolve URL to get xhs_id (needed for work dir name)
    # ------------------------------------------------------------------ #
    print("[ingest] Step 0: resolving URL …", file=sys.stderr)
    try:
        feed_id, xsec_token = asyncio.run(
            _resolve_only(xhs_url)
        )
    except DownloadError as exc:
        return _error("download", exc.code, str(exc))
    except Exception as exc:
        return _error("download", "INVALID_URL", str(exc))

    work_dir = _work_dir_for(feed_id)
    print(f"[ingest] work_dir = {work_dir}", file=sys.stderr)

    # ------------------------------------------------------------------ #
    # Step 1 – Download
    # ------------------------------------------------------------------ #
    print("[ingest] Step 1: downloading …", file=sys.stderr)
    try:
        metadata = asyncio.run(download(xhs_url, work_dir))
    except DownloadError as exc:
        return _error("download", exc.code, str(exc), work_dir)
    except Exception as exc:
        return _error("download", "UNKNOWN", str(exc), work_dir)

    # ------------------------------------------------------------------ #
    # Step 2 – Analyze
    # ------------------------------------------------------------------ #
    print("[ingest] Step 2: analyzing …", file=sys.stderr)
    try:
        analysis = analyze(work_dir)
    except Exception as exc:
        return _error("analyze", "ANALYZE_FAILED", str(exc), work_dir)

    # ------------------------------------------------------------------ #
    # Build preview result
    # ------------------------------------------------------------------ #
    result: dict = {
        "status": "preview",
        "xhs_id": metadata["xhs_id"],
        "title": metadata.get("title", ""),
        "author": metadata.get("author", ""),
        "note_type": metadata.get("note_type", "image"),
        "content_type": analysis.get("content_type", "other"),
        "suggested_tags": analysis.get("suggested_tags", []),
        "summary": analysis.get("summary", ""),
        "work_dir": str(work_dir),
        "vault_path": None,
        "rel_path": None,
        "error": None,
    }

    if not commit:
        return result

    # ------------------------------------------------------------------ #
    # Step 3 – Archive (only on --commit)
    # ------------------------------------------------------------------ #
    print("[ingest] Step 3: archiving to vault …", file=sys.stderr)
    try:
        archive_result = archive(work_dir, priority=priority, status=note_status)
    except ArchiveError as exc:
        return _error("archive", "ARCHIVE_FAILED", str(exc), work_dir)
    except Exception as exc:
        return _error("archive", "UNKNOWN", str(exc), work_dir)

    result.update(
        {
            "status": "committed",
            "vault_path": archive_result["vault_path"],
            "rel_path": archive_result["rel_path"],
            "tags": archive_result["tags"],
            "media_count": archive_result["media_count"],
        }
    )
    return result


async def _resolve_only(url: str):
    """Thin async wrapper so we can use asyncio.run() from sync code."""
    from download import resolve_xhs_url as _r
    return await _r(url)


if __name__ == "__main__":
    main()
