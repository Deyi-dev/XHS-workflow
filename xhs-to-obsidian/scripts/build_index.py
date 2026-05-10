"""
build_index.py – Rebuild <vault>/0_inbox/xhs/index.md from scratch.

Scans every <inbox>/*.md note, reads its frontmatter, groups by `category`,
sorts by `published_at` desc within each group, and writes one
`- [[note]] — tagline` line per note.

Full rebuild every run – the index never drifts when notes are renamed,
moved, or deleted.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path

import frontmatter

from summarize import CATEGORIES


def _index_lines_for(notes: list[dict], category: str) -> list[str]:
    rows = [n for n in notes if n.get("category", "其他") == category]
    rows.sort(key=lambda n: str(n.get("published_at") or ""), reverse=True)
    return [
        f"- [[{n['stem']}]] — {n['tagline']}"
        for n in rows
    ]


def build_index(vault_root: Path) -> Path:
    inbox = vault_root / "0_inbox" / "xhs"
    if not inbox.is_dir():
        raise FileNotFoundError(f"Inbox not found: {inbox}")

    notes: list[dict] = []
    for md in inbox.glob("*.md"):
        if md.name == "index.md":
            continue
        try:
            post = frontmatter.load(md)
        except Exception as exc:
            print(f"  [warn] Failed to parse {md.name}: {exc}", file=sys.stderr)
            continue
        meta = post.metadata or {}
        notes.append(
            {
                "stem": md.stem,
                "title": meta.get("title") or md.stem,
                "tagline": (meta.get("tagline") or meta.get("title") or md.stem).strip(),
                "category": (meta.get("category") or "其他").strip() or "其他",
                "published_at": meta.get("published_at") or "",
            }
        )

    seen_categories = {n["category"] for n in notes}
    # Order: known categories first (in CATEGORIES order), then any
    # unexpected ones at the bottom for visibility.
    ordered = [c for c in CATEGORIES if c in seen_categories]
    extras = sorted(seen_categories - set(CATEGORIES))
    ordered.extend(extras)

    lines: list[str] = []
    lines.append("---")
    lines.append("title: 小红书归档索引")
    lines.append("source_platform: xiaohongshu")
    lines.append(f"updated_at: {datetime.now().astimezone().isoformat(timespec='seconds')}")
    lines.append(f"note_count: {len(notes)}")
    lines.append("---")
    lines.append("")
    lines.append("# 小红书归档索引")
    lines.append("")
    lines.append(f"*共 {len(notes)} 条笔记，按内容分类。每次 `ingest --commit` 自动重建。*")
    lines.append("")

    for cat in ordered:
        rows = _index_lines_for(notes, cat)
        if not rows:
            continue
        lines.append(f"## {cat} ({len(rows)})")
        lines.append("")
        lines.extend(rows)
        lines.append("")

    out_path = inbox / "index.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


if __name__ == "__main__":
    vault = os.environ.get("OBSIDIAN_VAULT_PATH", "")
    if not vault:
        print("OBSIDIAN_VAULT_PATH not set", file=sys.stderr)
        sys.exit(1)
    p = build_index(Path(vault).expanduser())
    print(f"Wrote {p}")
