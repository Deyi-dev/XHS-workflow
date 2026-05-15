"""
batch_ingest.py – Process pending notes from xhs-queue.md using threads.

Usage:
    uv run scripts/batch_ingest.py [--concurrency 2] [--filter DSA]

Behaviour:
- Single attempt per URL (no in-script retry; download.py also no longer retries).
- Aborts the whole batch immediately on RATE_LIMITED so we don't dig the hole
  deeper while XHS is throttling us.
- Aborts the whole batch on LOGIN_REQUIRED – cookies need refreshing first.
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Allow importing sibling scripts
_SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPTS_DIR))

QUEUE_PATH = Path(__file__).parent.parent.parent / "xhs-queue.md"

ABORT_CODES = {"RATE_LIMITED", "LOGIN_REQUIRED"}


def load_queue(filter_prefix: str) -> list[tuple[int, str]]:
    """Return list of (line_index, url) for lines matching filter_prefix."""
    entries = []
    for i, line in enumerate(QUEUE_PATH.read_text().splitlines()):
        if line[:3] == filter_prefix:
            parts = line.split(" ", 1)
            if len(parts) == 2:
                entries.append((i, parts[1].strip()))
    return entries


def update_queue(updates: dict[int, str]) -> None:
    lines = QUEUE_PATH.read_text().splitlines()
    for idx, new_status in updates.items():
        if idx < len(lines):
            lines[idx] = new_status + lines[idx][3:]
    QUEUE_PATH.write_text("\n".join(lines) + "\n")


def process_one(url: str, section: str) -> str:
    """Run ingest._run() in this thread. Returns 'committed' or 'error:<code>'."""
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")

    from ingest import _run
    result = _run(url, section=section)
    status = result.get("status", "error")
    if status != "committed":
        code = result.get("error_code", status)
        return f"error:{code}"
    return "committed"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--filter", default="---", dest="filter_prefix", metavar="STATUS")
    parser.add_argument(
        "--section",
        default="📦 Other",
        help="Index section for all batched notes (no per-note classification in batch mode).",
    )
    args = parser.parse_args()

    entries = load_queue(args.filter_prefix)
    total = len(entries)
    if total == 0:
        print(f"No entries with status '{args.filter_prefix}' found.")
        return

    print(f"Processing {total} notes (concurrency={args.concurrency})", flush=True)
    done = errors = 0
    start = time.time()
    abort_reason: str | None = None

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(process_one, url, args.section): idx
            for idx, url in entries
        }
        pending_updates: dict[int, str] = {}

        for i, future in enumerate(as_completed(futures), 1):
            idx = futures[future]
            try:
                result = future.result()
            except Exception as e:
                result = f"error:{e}"

            if result == "committed":
                pending_updates[idx] = "D-A"
                done += 1
            else:
                errors += 1
                print(f"  SKIP [{result}]", flush=True)
                code = result.split(":", 1)[1] if result.startswith("error:") else ""
                if code in ABORT_CODES and abort_reason is None:
                    abort_reason = code

            # Flush queue update every 20 completions
            if i % 20 == 0 or i == total or abort_reason:
                update_queue(pending_updates)
                pending_updates = {}
                elapsed = time.time() - start
                rate = i / elapsed * 60 if elapsed > 0 else 0
                eta = (total - i) / rate if rate > 0 else 0
                print(
                    f"[{i}/{total}] done={done} errors={errors} "
                    f"~{rate:.0f}/min ETA {eta:.0f}min",
                    flush=True,
                )

            if abort_reason:
                # Cancel any not-yet-started futures and stop early.
                for f in futures:
                    f.cancel()
                break

    if abort_reason:
        print(f"\nAborted: {abort_reason}.", flush=True)
        if abort_reason == "RATE_LIMITED":
            print(
                "  XHS triggered 访问频繁. Wait an hour or change IP, then retry.",
                flush=True,
            )
        elif abort_reason == "LOGIN_REQUIRED":
            print(
                "  Run ~/.xhs-mcp/bin/xiaohongshu-login-darwin-arm64 to refresh cookies, then retry.",
                flush=True,
            )
        sys.exit(2)


if __name__ == "__main__":
    main()
