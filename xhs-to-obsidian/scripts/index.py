"""
index.py – Read sections from and insert entries into <vault>/0_inbox/xhs/_index.md.

_index.md layout (maintained by hand historically, now by this module):

    ---
    title: 小红书归档索引
    ...
    ---

    # 小红书归档索引

    *共 NNNN 条笔记，按内容人工分类（12 大类 + 其他）。*

    ## 目录

    - 🏂 Snowboard / Skiing: 141
    - 🤖 Dev/AI/Tech: 271
    ...

    ## 🏂 Snowboard / Skiing (141)

    - [[2026-04-05-4]] — tagline
    ...

Public API
----------
list_sections(vault)               -> ["🏂 Snowboard / Skiing", ...]
add_entry(vault, section, stem, tagline)
    Insert `- [[stem]] — tagline` as the first entry under `## <section>`,
    bump the heading count, the 目录 line, and the total. Idempotent: if the
    stem already exists (any section), the old line is removed first and
    counts are rebalanced so re-archiving / re-classifying is safe.
"""

from __future__ import annotations

import re
from pathlib import Path

_HEADING_RE = re.compile(r"^## (.+?) \((\d+)\)\s*$")
_TOC_RE = re.compile(r"^- (.+?): (\d+)\s*$")
_TOTAL_RE = re.compile(r"^\*共 (\d+) 条笔记")


class IndexError_(Exception):
    pass


def _index_path(vault: Path) -> Path:
    p = vault / "0_inbox" / "xhs" / "_index.md"
    if not p.exists():
        raise IndexError_(f"_index.md not found at {p}")
    return p


def list_sections(vault: Path) -> list[str]:
    """Return section names (with emoji), in file order, excluding 目录."""
    lines = _index_path(vault).read_text(encoding="utf-8").splitlines()
    sections: list[str] = []
    for line in lines:
        m = _HEADING_RE.match(line)
        if m and m.group(1).strip() != "目录":
            sections.append(m.group(1).strip())
    return sections


def _find_existing_entry(lines: list[str], stem: str) -> int | None:
    needle = f"[[{stem}]]"
    for i, line in enumerate(lines):
        if line.startswith("- ") and needle in line:
            return i
    return None


def _section_of_line(lines: list[str], line_idx: int) -> str | None:
    """Walk backwards from line_idx to find which ## section it belongs to."""
    for i in range(line_idx, -1, -1):
        m = _HEADING_RE.match(lines[i])
        if m:
            name = m.group(1).strip()
            return None if name == "目录" else name
    return None


def _bump(lines: list[str], section: str, delta: int) -> None:
    """Adjust the heading count, 目录 count, and total by *delta*."""
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m and m.group(1).strip() == section:
            lines[i] = f"## {section} ({int(m.group(2)) + delta})"
            continue
        m = _TOC_RE.match(line)
        if m and m.group(1).strip() == section:
            lines[i] = f"- {section}: {int(m.group(2)) + delta}"
            continue
        m = _TOTAL_RE.match(line)
        if m:
            lines[i] = _TOTAL_RE.sub(
                f"*共 {int(m.group(1)) + delta} 条笔记", line, count=1
            )


def add_entry(vault: Path, section: str, stem: str, tagline: str) -> None:
    """
    Insert `- [[stem]] — tagline` as the newest entry under `## <section>`.
    Idempotent on stem. Raises IndexError_ if the section heading is absent.
    """
    path = _index_path(vault)
    lines = path.read_text(encoding="utf-8").splitlines()
    tagline = (tagline or stem).strip().replace("\n", " ")
    new_line = f"- [[{stem}]] — {tagline}"

    # Locate target section heading.
    heading_idx = None
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m and m.group(1).strip() == section:
            heading_idx = i
            break
    if heading_idx is None:
        raise IndexError_(
            f"Section '{section}' not found in _index.md. "
            f"Available: {', '.join(list_sections(vault))}"
        )

    # Remove a pre-existing entry for this stem (re-archive / re-classify).
    existing = _find_existing_entry(lines, stem)
    if existing is not None:
        old_section = _section_of_line(lines, existing)
        if old_section == section:
            # Same section: replace text in place, no count change.
            lines[existing] = new_line
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return
        del lines[existing]
        if old_section:
            _bump(lines, old_section, -1)
        # heading_idx may have shifted if the removed line was above it.
        if existing < heading_idx:
            heading_idx -= 1

    # Insert as the first entry: heading, then blank line, then entries.
    insert_at = heading_idx + 1
    if insert_at < len(lines) and lines[insert_at].strip() == "":
        insert_at += 1
    lines.insert(insert_at, new_line)
    _bump(lines, section, +1)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    import os
    import sys

    vault = Path(os.environ["OBSIDIAN_VAULT_PATH"]).expanduser()
    if len(sys.argv) == 1 or sys.argv[1] == "list":
        print("\n".join(list_sections(vault)))
    elif sys.argv[1] == "add" and len(sys.argv) >= 5:
        add_entry(vault, sys.argv[2], sys.argv[3], sys.argv[4])
        print(f"Added [[{sys.argv[3]}]] to {sys.argv[2]}")
    else:
        print("Usage: python index.py [list | add <section> <stem> <tagline>]")
        sys.exit(1)
