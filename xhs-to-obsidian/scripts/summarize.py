"""
summarize.py – Generate a one-sentence tagline + category for a note.

Reads:  <work_dir>/metadata.json   (title, body, tags)
Writes: <work_dir>/summary.json    {"tagline": "...", "category": "..."}

Pure text Gemini call – no images. Cheap and fast.
Falls back to {tagline: title, category: "其他"} if GEMINI_API_KEY is unset
or the call fails.
"""

from __future__ import annotations

import json
import os
import re
import sys
import warnings
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

CATEGORIES = [
    "体能健康",
    "学习思考",
    "工具AI",
    "生活娱乐",
    "户外旅行",
    "其他",
]

_PROMPT_TEMPLATE = """\
你在为一篇小红书笔记写归档索引。给定标题、正文和原始标签，请返回一个 JSON 对象：

{{
  "tagline": "<一句话总结，最多 30 个汉字，描述这篇笔记在讲什么；不要复述标题，要提炼信息>",
  "category": "<必须是这几个之一: {categories}>"
}}

标题: {title}

原始标签: {tags}

正文:
{body}

只返回 JSON，不要 markdown 代码块。
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _fallback(metadata: dict) -> dict:
    return {
        "tagline": (metadata.get("title") or "").strip()[:60] or "（无标题）",
        "category": "其他",
        "source": "fallback",
    }


def summarize(work_dir: Path) -> dict:
    """Generate {tagline, category} for the note at work_dir."""
    summary_path = work_dir / "summary.json"
    if summary_path.exists():
        return json.loads(summary_path.read_text())

    meta_path = work_dir / "metadata.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"metadata.json not found in {work_dir}")
    metadata = json.loads(meta_path.read_text())

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        result = _fallback(metadata)
        summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    title = (metadata.get("title") or "").strip()
    body = (metadata.get("body") or "").strip()
    tags = metadata.get("tags") or []
    # Cap body length – avoid sending megabyte-long copy-paste pages.
    body = body[:4000]

    prompt = _PROMPT_TEMPLATE.format(
        title=title or "（无标题）",
        tags=", ".join(tags) if tags else "（无）",
        body=body or "（无正文）",
        categories=" / ".join(CATEGORIES),
    )

    try:
        # google-generativeai is deprecated but still works; suppress its
        # FutureWarning so the JSON output stays clean.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=FutureWarning)
            import google.generativeai as genai

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.5-flash")

        import time, re as _re
        last_exc = None
        for attempt in range(4):
            try:
                resp = model.generate_content(prompt)
                break
            except Exception as exc:
                last_exc = exc
                # Extract retry_delay from 429 error if present
                delay_match = _re.search(r"retry[_ ]in\s+([\d.]+)s", str(exc), _re.I)
                wait = float(delay_match.group(1)) + 2 if delay_match else (30 * (attempt + 1))
                if "429" in str(exc) and attempt < 3:
                    print(f"  [warn] Rate limited, retrying in {wait:.0f}s (attempt {attempt+1}/4)…", file=sys.stderr)
                    time.sleep(wait)
                else:
                    raise
        else:
            raise last_exc

        text = _strip_fences(resp.text or "")
        parsed = json.loads(text)
    except Exception as exc:
        print(f"  [warn] Summarize failed, using fallback: {exc}", file=sys.stderr)
        result = _fallback(metadata)
        summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    tagline = (parsed.get("tagline") or "").strip()
    category = (parsed.get("category") or "").strip()
    if category not in CATEGORIES:
        category = "其他"
    if not tagline:
        tagline = title or "（无标题）"

    result = {
        "tagline": tagline,
        "category": category,
        "source": "gemini-2.5-flash",
    }
    summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python summarize.py <work_dir>")
        sys.exit(1)
    out = summarize(Path(sys.argv[1]))
    print(json.dumps(out, ensure_ascii=False, indent=2))
