"""
analyze.py – Run Gemini vision analysis on downloaded XHS images.

Reads:  <work_dir>/metadata.json  (note_type, media_local)
        <work_dir>/media/*.jpg

Writes: <work_dir>/analysis.json

For video notes (note_type == "video"), Gemini is skipped; analysis is
inferred from metadata alone.

Rate limits: Gemini free tier ≈ 10 RPM → sequential with 7 s sleep.
Concurrency cap: 3 in-flight requests max.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

_CONTENT_TYPES = [
    "recipe",
    "workout",
    "snowboarding",
    "long-form-article",
    "video-tutorial",
    "other",
]

_ANALYZE_PROMPT = """\
You are analyzing an image from a Xiaohongshu (Little Red Book) social post.

Return ONLY a valid JSON object (no markdown fences) with exactly these fields:
{
  "ocr_text": "<all readable text extracted from the image, empty string if none>",
  "content_type": "<one of: recipe, workout, snowboarding, long-form-article, video-tutorial, other>",
  "suggested_tags": ["<3-5 relevant tags, mix of Chinese and English is fine>"],
  "summary": "<1-2 sentence description of what this image shows>"
}
"""

_MULTI_PROMPT = """\
You are analyzing a set of images from a single Xiaohongshu (Little Red Book) post.

Return ONLY a valid JSON object (no markdown fences) with exactly these fields:
{
  "ocr_text": "<all readable text extracted from ALL images combined>",
  "content_type": "<one of: recipe, workout, snowboarding, long-form-article, video-tutorial, other>",
  "suggested_tags": ["<3-5 relevant tags, mix of Chinese and English is fine>"],
  "summary": "<1-2 sentence description of what the post is about>"
}
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _build_analysis_from_metadata(metadata: dict) -> dict:
    """Generate analysis.json from metadata alone (no Gemini call)."""
    title = metadata.get("title", "")
    body = metadata.get("body", "")
    tags = metadata.get("tags", [])
    note_type = metadata.get("note_type", "image")
    content_type = "video-tutorial" if note_type == "video" else "other"
    default_tags = ["video", "小红书"] if note_type == "video" else ["小红书"]
    return {
        "ocr_text": {},
        "content_type": content_type,
        "suggested_tags": tags[:5] if tags else default_tags,
        "summary": (title or body or "Note from Xiaohongshu.")[:200],
        "source": "metadata",
    }


async def _analyze_images_gemini(
    image_paths: list[Path],
    model,
) -> dict:
    """
    Send images to Gemini and return parsed analysis dict.
    Batches all images in one request when ≤ 10; otherwise analyzes
    individually and merges (keeping the last non-empty content_type/summary).
    """
    import PIL.Image  # lazy import so non-analyze paths don't need Pillow

    if not image_paths:
        return {
            "ocr_text": {},
            "content_type": "other",
            "suggested_tags": [],
            "summary": "",
            "source": "gemini",
        }

    # Filter out placeholder filenames (MISSING_*)
    valid_paths = [p for p in image_paths if p.exists()]
    if not valid_paths:
        return {
            "ocr_text": {},
            "content_type": "other",
            "suggested_tags": [],
            "summary": "All media files failed to download.",
            "source": "gemini",
        }

    # Strategy: ≤ 6 images → single batch call; otherwise chunked sequential
    batch_size = 6
    ocr_texts: dict[str, str] = {}
    merged: dict = {}

    semaphore = asyncio.Semaphore(3)

    async def _call_batch(paths: list[Path], prompt: str) -> dict:
        imgs = [PIL.Image.open(p) for p in paths]
        parts = [prompt] + imgs

        async with semaphore:
            response = await asyncio.to_thread(model.generate_content, parts)

        raw = _strip_fences(response.text)
        return json.loads(raw)

    chunks = [valid_paths[i : i + batch_size] for i in range(0, len(valid_paths), batch_size)]
    results: list[dict] = []

    for idx, chunk in enumerate(chunks):
        if idx > 0:
            # 7 s gap to stay under 10 RPM
            await asyncio.sleep(7)

        prompt = _MULTI_PROMPT if len(chunk) > 1 else _ANALYZE_PROMPT
        try:
            result = await _call_batch(chunk, prompt)
        except (json.JSONDecodeError, KeyError, Exception) as exc:
            print(f"  [warn] Gemini chunk {idx} failed: {exc}", file=sys.stderr)
            result = {
                "ocr_text": "",
                "content_type": "other",
                "suggested_tags": [],
                "summary": "",
            }
        results.append((chunk, result))

    # Merge results
    all_tags: list[str] = []
    final_content_type = "other"
    final_summary = ""

    for chunk, res in results:
        # ocr_text: per-image keyed by filename
        ocr = res.get("ocr_text", "")
        if isinstance(ocr, str) and ocr:
            for p in chunk:
                ocr_texts[p.name] = ocr
        elif isinstance(ocr, dict):
            ocr_texts.update(ocr)

        ct = res.get("content_type", "")
        if ct in _CONTENT_TYPES:
            final_content_type = ct

        s = res.get("summary", "")
        if s:
            final_summary = s

        tags = res.get("suggested_tags", [])
        all_tags.extend(t for t in tags if isinstance(t, str))

    # Deduplicate tags, preserve order
    seen: set[str] = set()
    deduped_tags: list[str] = []
    for t in all_tags:
        if t not in seen:
            seen.add(t)
            deduped_tags.append(t)

    return {
        "ocr_text": ocr_texts,
        "content_type": final_content_type,
        "suggested_tags": deduped_tags[:8],
        "summary": final_summary,
        "source": "gemini",
    }


def analyze(work_dir: Path) -> dict:
    """
    Synchronous wrapper for the async analysis pipeline.
    Reads metadata.json, writes analysis.json, returns the analysis dict.
    """
    meta_path = work_dir / "metadata.json"
    analysis_path = work_dir / "analysis.json"

    if analysis_path.exists():
        return json.loads(analysis_path.read_text())

    if not meta_path.exists():
        raise FileNotFoundError(f"metadata.json not found in {work_dir}")

    metadata = json.loads(meta_path.read_text())

    # Video notes: skip Gemini
    if metadata.get("note_type") == "video":
        print("  → Video note detected; skipping Gemini vision.", file=sys.stderr)
        result = _build_analysis_from_metadata(metadata)
        analysis_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        print(
            "  [warn] GEMINI_API_KEY not set; generating stub analysis from metadata.",
            file=sys.stderr,
        )
        result = _build_analysis_from_metadata(metadata)
        result["source"] = "metadata-fallback"
        analysis_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    import google.generativeai as genai

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.5-flash")

    media_dir = work_dir / "media"
    local_files = metadata.get("media_local", [])
    image_paths = [
        media_dir / f
        for f in local_files
        if not f.startswith("MISSING_")
    ]

    print(
        f"  → Analyzing {len(image_paths)} image(s) with Gemini …",
        file=sys.stderr,
    )
    result = asyncio.run(_analyze_images_gemini(image_paths, model))
    analysis_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python analyze.py <work_dir>")
        sys.exit(1)

    _work = Path(sys.argv[1])
    try:
        out = analyze(_work)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)
