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

# Resolve sibling scripts regardless of CWD
_SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from download import DownloadError, download, resolve_xhs_url
from analyze import analyze
from summarize import summarize
from archive import ArchiveError, archive
from build_index import build_index


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
@click.option("--download-only", is_flag=True, default=False, help="Stop after download; skip summarize and archive.")
@click.option("--skip-summarize", is_flag=True, default=True, help="Skip Gemini summarize; archive with fallback category.")
@click.option("--priority", default="medium", show_default=True, help="Frontmatter priority field.")
@click.option("--status", "note_status", default="lite", show_default=True, help="Frontmatter status field.")
def main(xhs_url: str, commit: bool, download_only: bool, skip_summarize: bool, priority: str, note_status: str):
    """
    Download, analyse, and optionally archive one Xiaohongshu note.

    Prints a JSON summary to stdout for agent consumption.
    All progress messages go to stderr.
    """
    result = _run(xhs_url, commit=commit, download_only=download_only, skip_summarize=skip_summarize, priority=priority, note_status=note_status)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == "error":
        sys.exit(1)


def _run(xhs_url: str, *, commit: bool, download_only: bool = False, skip_summarize: bool = False, priority: str, note_status: str) -> dict:
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

    if download_only:
        return {
            "status": "downloaded",
            "xhs_id": metadata["xhs_id"],
            "title": metadata.get("title", ""),
            "work_dir": str(work_dir),
            "error": None,
        }

    # ------------------------------------------------------------------ #
    # Step 2 – Analyze (DISABLED — fall back to metadata-only)
    # ------------------------------------------------------------------ #
    print("[ingest] Step 2: analyze SKIPPED", file=sys.stderr)
    analysis = {
        "ocr_text": {},
        "content_type": "other",
        "suggested_tags": metadata.get("tags", []),
        "summary": metadata.get("title", ""),
        "source": "skipped",
    }
    (work_dir / "analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2)
    )

    # ------------------------------------------------------------------ #
    # Step 3 – Archive immediately (only on --commit), using whatever
    # summary is cached; if none exists yet, archive.py falls back to
    # title/其他 so the note lands in Obsidian right away.
    # ------------------------------------------------------------------ #
    archive_result = None
    if commit:
        print("[ingest] Step 3: archiving to vault (pre-summarize) …", file=sys.stderr)
        try:
            archive_result = archive(work_dir, priority=priority, status=note_status)
        except ArchiveError as exc:
            return _error("archive", "ARCHIVE_FAILED", str(exc), work_dir)
        except Exception as exc:
            return _error("archive", "UNKNOWN", str(exc), work_dir)

    # ------------------------------------------------------------------ #
    # Step 4 – Summarize (text-only LLM: tagline + category)
    # ------------------------------------------------------------------ #
    if skip_summarize:
        print("[ingest] Step 4: summarize SKIPPED", file=sys.stderr)
        summary = {
            "tagline": metadata.get("title", "") or "（无标题）",
            "category": "其他",
            "source": "skipped",
        }
    else:
        print("[ingest] Step 4: summarizing …", file=sys.stderr)
        try:
            summary = summarize(work_dir)
        except Exception as exc:
            print(f"  [warn] summarize raised: {exc}", file=sys.stderr)
            summary = {
                "tagline": metadata.get("title", "") or "（无标题）",
                "category": "其他",
                "source": "error-fallback",
            }

    # ------------------------------------------------------------------ #
    # Step 5 – Re-archive if summarize produced a real result, to update
    # the vault note in place (xhs_id deduplication in archive.py).
    # ------------------------------------------------------------------ #
    if commit and summary.get("source") not in ("fallback", "error-fallback", "skipped"):
        print("[ingest] Step 5: re-archiving with Gemini summary …", file=sys.stderr)
        try:
            archive_result = archive(work_dir, priority=priority, status=note_status)
        except Exception as exc:
            print(f"  [warn] Re-archive failed: {exc}", file=sys.stderr)

    # ------------------------------------------------------------------ #
    # Build result
    # ------------------------------------------------------------------ #
    result: dict = {
        "status": "committed" if commit else "preview",
        "xhs_id": metadata["xhs_id"],
        "title": metadata.get("title", ""),
        "author": metadata.get("author", ""),
        "note_type": metadata.get("note_type", "image"),
        "content_type": analysis.get("content_type", "other"),
        "tags": metadata.get("tags", []),
        "suggested_tags": analysis.get("suggested_tags", []),
        "summary": analysis.get("summary", ""),
        "tagline": summary.get("tagline", ""),
        "category": summary.get("category", "其他"),
        "work_dir": str(work_dir),
        "vault_path": archive_result["vault_path"] if archive_result else None,
        "rel_path": archive_result["rel_path"] if archive_result else None,
        "media_count": archive_result["media_count"] if archive_result else None,
        "error": None,
    }

    if not commit:
        return result

    # ------------------------------------------------------------------ #
    # Step 6 – Rebuild index.md
    # ------------------------------------------------------------------ #
    print("[ingest] Step 6: rebuilding index …", file=sys.stderr)
    try:
        vault_root = Path(os.environ["OBSIDIAN_VAULT_PATH"]).expanduser()
        index_path = build_index(vault_root)
        result["index_path"] = str(index_path)
    except Exception as exc:
        print(f"  [warn] Index rebuild failed: {exc}", file=sys.stderr)
        result["index_path"] = None

    return result


async def _resolve_only(url: str):
    """Thin async wrapper so we can use asyncio.run() from sync code."""
    from download import resolve_xhs_url as _r
    return await _r(url)


if __name__ == "__main__":
    main()
