"""
download.py – Resolve an XHS URL, fetch note metadata via direct page scrape,
and download all media files into a work directory.

Architecture
------------
We hit XHS's public share page directly with the cookies maintained by the
xiaohongshu-login binary (~/.xhs-mcp/bin/cookies.json). No MCP server is
required at runtime; the login binary just refreshes cookies when they expire.

Output layout:
  <work_dir>/
    metadata.json      normalised note data
    raw_response.json  verbatim parsed __INITIAL_STATE__ note for debugging
    media/
      image_0.jpg, ...

Error codes (raised as DownloadError):
  INVALID_URL     – cannot parse feed_id / xsec_token from URL
  LOGIN_REQUIRED  – XHS redirected to login wall; cookies missing or expired.
                    Fix: re-run ~/.xhs-mcp/bin/xiaohongshu-login-darwin-arm64
  RATE_LIMITED    – XHS error_code=300013 ("访问频繁"). Wait or change IP.
  NOTE_DELETED    – note removed from the platform
  SCRAPE_FAILED   – page loaded but __INITIAL_STATE__ is missing/unparseable
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

XHS_COOKIES_PATH = os.getenv(
    "XHS_COOKIES_PATH",
    str(Path(__file__).parent.parent / "bin" / "cookies.json"),
)

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class DownloadError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------

async def resolve_xhs_url(url: str) -> tuple[str, str]:
    """Return (feed_id, xsec_token) from an XHS URL (short or long)."""
    # Fast path: already a long URL we can parse without a network call
    try:
        return _parse_xhs_url(url)
    except DownloadError:
        pass  # short link – need to follow redirect

    headers = {"User-Agent": _BROWSER_UA}
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, headers=headers, timeout=15
        ) as client:
            # Use GET: xhslink.com returns 404 for HEAD but redirects GET
            resp = await client.get(url)
            final_url = str(resp.url)
    except httpx.ConnectError as exc:
        raise DownloadError("INVALID_URL", f"Cannot reach URL: {exc}") from exc
    except httpx.TimeoutException as exc:
        raise DownloadError("INVALID_URL", f"Timeout resolving URL: {exc}") from exc

    return _parse_xhs_url(final_url)


def _parse_xhs_url(url: str) -> tuple[str, str]:
    """Extract (feed_id, xsec_token) from a resolved xiaohongshu.com URL."""
    parsed = urlparse(url)

    match = re.search(
        r"/(?:explore|discovery/item)/([0-9a-fA-F]{20,})", parsed.path
    )
    if not match:
        raise DownloadError(
            "INVALID_URL",
            f"Cannot extract note ID from URL: {url}",
        )

    feed_id = match.group(1)
    params = parse_qs(parsed.query)
    xsec_token_list = params.get("xsec_token", [])
    if not xsec_token_list:
        raise DownloadError(
            "INVALID_URL",
            f"No xsec_token query parameter in URL: {url}",
        )

    return feed_id, xsec_token_list[0]


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _extract(d: dict, *keys, default=""):
    """Try multiple key names in order, return first non-empty value."""
    for k in keys:
        v = d.get(k)
        if v is not None and v != "":
            return v
    return default


def _parse_note_data(note: dict, feed_id: str, source_url: str) -> dict:
    """
    Normalise the inner note dict (from __INITIAL_STATE__.note.noteDetailMap[id].note)
    into a stable metadata dict.
    """
    data = note

    # ---- Basic fields ----
    title = _extract(data, "title", "displayTitle", "noteTitle")
    body = _extract(data, "desc", "description", "content", "noteDesc")

    # ---- User / author ----
    user = data.get("user") or data.get("author") or data.get("userInfo") or {}
    author = _extract(user, "nickname", "nickName", "name")
    author_id = _extract(user, "userId", "user_id", "uid")

    # ---- Tags ----
    # XHS embeds platform tags inline at the end of `desc` as `#name[话题]#`.
    # The structured tagList field is sometimes absent, so we also parse the
    # hashtags out of the body and strip them so the body stays clean.
    raw_tags = data.get("tagList") or data.get("tags") or []
    tags: list[str] = []
    seen_tags: set[str] = set()
    for t in raw_tags:
        if isinstance(t, dict):
            name = _extract(t, "name", "text", "title")
        elif isinstance(t, str):
            name = t
        else:
            name = ""
        if name and name not in seen_tags:
            seen_tags.add(name)
            tags.append(str(name))

    if isinstance(body, str) and body:
        for name in re.findall(r"#([^#\[\]\n]+?)\[话题\]#", body):
            name = name.strip()
            if name and name not in seen_tags:
                seen_tags.add(name)
                tags.append(name)
        body = re.sub(r"#[^#\[\]\n]+?\[话题\]#", "", body)
        body = re.sub(r"[ \t]+\n", "\n", body).strip()

    # ---- Media (images) ----
    image_list = data.get("imageList") or data.get("images") or []
    media_urls: list[str] = []
    for img in image_list:
        if isinstance(img, dict):
            url = _extract(img, "url", "urlDefault", "urlPre", "traceUrl")
        elif isinstance(img, str):
            url = img
        else:
            continue
        if url:
            media_urls.append(url)

    # ---- Note type ----
    video = data.get("video") or data.get("videoMedia") or {}
    model_type = _extract(data, "type", "modelType", "noteType", default="").lower()
    if video and isinstance(video, dict) and video:
        note_type = "video"
    elif "video" in model_type:
        note_type = "video"
    else:
        note_type = "image"

    # ---- Timestamps ----
    raw_ts = _extract(data, "time", "publishTime", "createTime", "lastUpdateTime")
    if isinstance(raw_ts, (int, float)) and raw_ts > 1e9:
        ts_sec = raw_ts / 1000 if raw_ts > 1e12 else raw_ts
        published_at = datetime.fromtimestamp(ts_sec, tz=timezone.utc).isoformat()
    elif isinstance(raw_ts, str) and raw_ts:
        published_at = raw_ts
    else:
        published_at = ""

    return {
        "xhs_id": feed_id,
        "source_url": source_url,
        "title": title,
        "body": body,
        "author": author,
        "author_id": author_id,
        "tags": tags,
        "media_urls": media_urls,
        "note_type": note_type,
        "published_at": published_at,
        "captured_at": datetime.now(tz=timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Page scrape (primary path)
# ---------------------------------------------------------------------------

def _load_xhs_cookies() -> dict:
    """Load xiaohongshu.com cookies from the login binary's cookies.json."""
    try:
        raw = json.loads(Path(XHS_COOKIES_PATH).read_text())
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"  [warn] Failed to load cookies from {XHS_COOKIES_PATH}: {exc}", file=sys.stderr)
        return {}
    if not isinstance(raw, list):
        return {}
    return {
        c["name"]: c["value"]
        for c in raw
        if isinstance(c, dict)
        and "name" in c
        and "value" in c
        and c.get("domain", "").endswith("xiaohongshu.com")
    }


def _classify_redirect(final_url: str) -> tuple[str, str] | None:
    """
    If the response was redirected to a known error page, return
    (error_code, message). Otherwise return None.
    """
    if "/website-login/error" not in final_url:
        return None
    params = parse_qs(urlparse(final_url).query)
    err = (params.get("error_code", [""])[0] or "").strip()
    msg = (params.get("error_msg", [""])[0] or "").strip() or "（XHS 未提供错误描述）"
    if err == "300013":
        return ("RATE_LIMITED", f"XHS rate limit: {msg}")
    return ("LOGIN_REQUIRED", f"XHS login wall (error_code={err}): {msg}")


async def _fetch_initial_state(feed_id: str, xsec_token: str | None) -> dict:
    """
    Fetch the XHS page and parse window.__INITIAL_STATE__.
    If xsec_token is None, fetches the plain URL (token-expired fallback).
    Raises DownloadError on any failure – no retries, no None returns.
    """
    if xsec_token:
        page_url = (
            f"https://www.xiaohongshu.com/discovery/item/{feed_id}"
            f"?xsec_token={xsec_token}&xsec_source=pc_share"
        )
    else:
        page_url = f"https://www.xiaohongshu.com/discovery/item/{feed_id}"
    cookies = _load_xhs_cookies()
    if not cookies:
        raise DownloadError(
            "LOGIN_REQUIRED",
            f"No XHS cookies found at {XHS_COOKIES_PATH}. "
            "Run ~/.xhs-mcp/bin/xiaohongshu-login-darwin-arm64 to log in.",
        )

    headers = {"User-Agent": _BROWSER_UA}
    try:
        async with httpx.AsyncClient(
            cookies=cookies, headers=headers, follow_redirects=True, timeout=20
        ) as client:
            resp = await client.get(page_url)
    except Exception as exc:
        raise DownloadError("SCRAPE_FAILED", f"HTTP request failed: {exc}") from exc

    classified = _classify_redirect(str(resp.url))
    if classified:
        raise DownloadError(*classified)

    if resp.status_code != 200:
        raise DownloadError(
            "SCRAPE_FAILED",
            f"XHS returned HTTP {resp.status_code} for feed {feed_id}",
        )

    match = re.search(
        r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>",
        resp.text,
        re.DOTALL,
    )
    if not match:
        raise DownloadError(
            "SCRAPE_FAILED",
            f"__INITIAL_STATE__ missing from page for feed {feed_id}",
        )

    raw = match.group(1)
    raw = re.sub(r":\s*undefined\b", ": null", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DownloadError(
            "SCRAPE_FAILED",
            f"Failed to parse __INITIAL_STATE__: {exc}",
        ) from exc


def _note_from_state(state: dict, feed_id: str) -> dict | None:
    """Pull the per-note dict out of state.note.noteDetailMap."""
    note_map = state.get("note", {}).get("noteDetailMap", {})
    entry = note_map.get(feed_id) or next(iter(note_map.values()), None)
    if not isinstance(entry, dict):
        return None
    note = entry.get("note")
    return note if isinstance(note, dict) else None


async def _scrape_note(feed_id: str, xsec_token: str) -> dict:
    """
    Fetch the XHS page and return the inner note dict.
    Falls back to a token-less request when the token has expired
    (noteDetailMap empty on first attempt but present on second).
    Raises DownloadError on any failure.
    """
    state = await _fetch_initial_state(feed_id, xsec_token)
    note = _note_from_state(state, feed_id)
    if not note:
        # xsec_token may have expired – try plain URL which sometimes still
        # returns note data embedded in the 404 page's __INITIAL_STATE__.
        print(f"  [warn] noteDetailMap empty with token, retrying without token …", file=sys.stderr)
        state2 = await _fetch_initial_state(feed_id, None)
        note = _note_from_state(state2, feed_id)
    if not note:
        raise DownloadError(
            "NOTE_DELETED",
            f"noteDetailMap empty for feed {feed_id} – note may be deleted or token expired",
        )
    return note


async def _fetch_video_url(feed_id: str, xsec_token: str) -> str | None:
    """Extract the video master URL from the same page state. None if no video."""
    try:
        state = await _fetch_initial_state(feed_id, xsec_token)
    except DownloadError:
        return None
    note = _note_from_state(state, feed_id) or {}
    stream = note.get("video", {}).get("media", {}).get("stream", {})
    for fmt in ("h264", "h265", "h266", "av1"):
        urls = stream.get(fmt) or []
        if urls and isinstance(urls[0], dict):
            master = urls[0].get("masterUrl")
            if master:
                return master
    return None


async def _download_video(url: str, dest: Path) -> bool:
    """Download a video file. Returns True on success."""
    headers = {
        "User-Agent": _BROWSER_UA,
        "Referer": "https://www.xiaohongshu.com/",
    }
    try:
        async with httpx.AsyncClient(
            headers=headers, follow_redirects=True, timeout=120
        ) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with dest.open("wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=1 << 16):
                        f.write(chunk)
        return True
    except Exception as exc:
        print(f"  [warn] Failed to download video {url}: {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Media download
# ---------------------------------------------------------------------------

async def _download_media(urls: list[str], media_dir: Path) -> list[str]:
    """Download media files; return list of local filenames (not full paths)."""
    media_dir.mkdir(parents=True, exist_ok=True)
    filenames: list[str] = []

    async with httpx.AsyncClient(
        headers={"User-Agent": _BROWSER_UA},
        follow_redirects=True,
        timeout=30,
    ) as client:
        for i, url in enumerate(urls):
            url_path = urlparse(url).path
            suffix = Path(url_path).suffix
            if suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                suffix = ".jpg"
            fname = f"image_{i}{suffix}"
            dest = media_dir / fname

            if dest.exists():
                filenames.append(fname)
                continue

            try:
                resp = await client.get(url)
                resp.raise_for_status()
                dest.write_bytes(resp.content)
                filenames.append(fname)
            except Exception as exc:
                print(f"  [warn] Failed to download {url}: {exc}", file=sys.stderr)
                filenames.append(f"MISSING_{fname}")

    return filenames


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def download(url: str, work_dir: Path) -> dict:
    """
    Resolve *url*, scrape the note page, download media into *work_dir*.
    Returns the normalised metadata dict (also written as metadata.json).
    """
    raw_path = work_dir / "raw_response.json"
    meta_path = work_dir / "metadata.json"

    # Fast-path: already downloaded
    if meta_path.exists():
        return json.loads(meta_path.read_text())

    work_dir.mkdir(parents=True, exist_ok=True)

    # 1. Resolve URL
    print(f"  → Resolving URL: {url}", file=sys.stderr)
    feed_id, xsec_token = await resolve_xhs_url(url)
    print(f"  → feed_id={feed_id}", file=sys.stderr)

    # 2. Scrape the page (single attempt; failures bubble up immediately)
    print(f"  → Scraping XHS page …", file=sys.stderr)
    note = await _scrape_note(feed_id, xsec_token)
    raw_path.write_text(json.dumps(note, ensure_ascii=False, indent=2))

    # 3. Parse
    metadata = _parse_note_data(note, feed_id, url)

    # 4. Download media
    media_dir = work_dir / "media"
    if metadata["media_urls"]:
        print(
            f"  → Downloading {len(metadata['media_urls'])} image(s) …",
            file=sys.stderr,
        )
        local_filenames = await _download_media(metadata["media_urls"], media_dir)
        metadata["media_local"] = local_filenames
    else:
        metadata["media_local"] = []

    # 5. Video — already in the same state we scraped, but _fetch_video_url
    # will refetch. Cheap and isolated; leave as-is for clarity.
    metadata["video_url"] = ""
    metadata["video_local"] = ""
    if metadata["note_type"] == "video":
        print("  → Resolving video URL …", file=sys.stderr)
        video_url = await _fetch_video_url(feed_id, xsec_token)
        if video_url:
            metadata["video_url"] = video_url
            media_dir.mkdir(parents=True, exist_ok=True)
            video_dest = media_dir / "video.mp4"
            print(f"  → Downloading video …", file=sys.stderr)
            if await _download_video(video_url, video_dest):
                metadata["video_local"] = "video.mp4"
                metadata["media_local"].append("video.mp4")
        else:
            print("  [warn] Could not resolve video URL", file=sys.stderr)

    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    return metadata


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python download.py <xhs_url> <work_dir>")
        sys.exit(1)

    _url = sys.argv[1]
    _work = Path(sys.argv[2])

    async def _main():
        try:
            meta = await download(_url, _work)
            print(json.dumps(meta, ensure_ascii=False, indent=2))
        except DownloadError as e:
            print(json.dumps({"error": e.code, "message": str(e)}))
            sys.exit(1)

    asyncio.run(_main())
