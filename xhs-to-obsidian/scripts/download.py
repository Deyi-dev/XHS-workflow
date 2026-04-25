"""
download.py – Resolve an XHS URL, fetch note metadata via xiaohongshu-mcp,
and download all media files into a work directory.

Output layout:
  <work_dir>/
    metadata.json      normalised note data
    raw_response.json  verbatim MCP response for debugging
    media/
      image_0.jpg, ...

Error codes (raised as DownloadError):
  INVALID_URL      – cannot parse feed_id / xsec_token from URL
  MCP_UNREACHABLE  – cannot reach local MCP server
  NOT_LOGGED_IN    – MCP reports authentication error
  NOTE_DELETED     – note not found or unavailable
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

MCP_URL = os.getenv("XHS_MCP_URL", "http://localhost:18060/mcp")

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

    # Accepted path patterns:
    #   /explore/{note_id}
    #   /discovery/item/{note_id}
    #   /xhslink/{...}  (shouldn't reach here but guard anyway)
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
# MCP client
# ---------------------------------------------------------------------------

async def _call_mcp_tool(tool_name: str, arguments: dict) -> dict:
    """Call a tool on the local xiaohongshu-mcp server via MCP HTTP transport."""
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError as exc:
        raise RuntimeError(
            "The 'mcp' package is required. Run: uv sync"
        ) from exc

    try:
        async with streamablehttp_client(MCP_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
    except Exception as exc:
        # Unwrap ExceptionGroup (Python 3.11 TaskGroup wraps inner exceptions)
        inner: BaseException = exc
        if isinstance(exc, BaseExceptionGroup):
            inner = exc.exceptions[0]
        err_str = str(inner).lower()
        conn_keywords = ("connect", "refused", "unreachable", "timeout", "network")
        if any(k in err_str for k in conn_keywords):
            raise DownloadError(
                "MCP_UNREACHABLE",
                f"Cannot connect to xiaohongshu-mcp at {MCP_URL}. "
                f"Is the server running? Details: {inner}",
            ) from exc
        raise DownloadError("MCP_UNREACHABLE", str(inner)) from exc

    if result.isError:
        text = result.content[0].text if result.content else "unknown error"
        lower = text.lower()
        if any(k in lower for k in ("login", "auth", "cookie", "未登录", "请登录")):
            raise DownloadError("NOT_LOGGED_IN", f"MCP auth error: {text}")
        if any(k in lower for k in ("not found", "deleted", "404", "不存在")):
            raise DownloadError("NOTE_DELETED", f"Note unavailable: {text}")
        raise DownloadError("NOTE_DELETED", f"MCP tool error: {text}")

    if not result.content:
        raise DownloadError("NOTE_DELETED", "Empty response from MCP")

    raw_text = result.content[0].text
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise DownloadError("NOTE_DELETED", f"Invalid JSON from MCP: {exc}") from exc


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


def _parse_note_data(raw: dict, feed_id: str, source_url: str) -> dict:
    """
    Normalise the raw MCP response into a stable metadata dict.

    xiaohongshu-mcp wraps the scraped data as:
      { "feed_id": "...", "data": { ...note fields... } }
    The inner fields follow xiaohongshu's own camelCase naming from
    __INITIAL_STATE__.
    """
    # Unwrap outer envelope if present
    data = raw.get("data", raw)
    if isinstance(data, str):
        # Sometimes the data field is a JSON string
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            data = raw

    # ---- Basic fields ----
    title = _extract(data, "title", "displayTitle", "noteTitle")
    body = _extract(data, "desc", "description", "content", "noteDesc")

    # ---- User / author ----
    user = data.get("user") or data.get("author") or data.get("userInfo") or {}
    author = _extract(user, "nickname", "nickName", "name")
    author_id = _extract(user, "userId", "user_id", "uid")

    # ---- Tags ----
    raw_tags = data.get("tagList") or data.get("tags") or []
    tags: list[str] = []
    for t in raw_tags:
        if isinstance(t, dict):
            name = _extract(t, "name", "text", "title")
            if name:
                tags.append(str(name))
        elif isinstance(t, str) and t:
            tags.append(t)

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
        # Unix timestamp in seconds or milliseconds
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
            # Guess extension from URL
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
                # Record as missing rather than crashing
                filenames.append(f"MISSING_{fname}")

    return filenames


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def download(url: str, work_dir: Path) -> dict:
    """
    Resolve *url*, fetch note via MCP, download media into *work_dir*.
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

    # 2. Fetch from MCP
    print(f"  → Calling MCP get_feed_detail …", file=sys.stderr)
    raw = await _call_mcp_tool(
        "get_feed_detail",
        {"feed_id": feed_id, "xsec_token": xsec_token},
    )
    raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2))

    # 3. Parse
    metadata = _parse_note_data(raw, feed_id, url)

    # 4. Download media
    if metadata["media_urls"]:
        print(
            f"  → Downloading {len(metadata['media_urls'])} media file(s) …",
            file=sys.stderr,
        )
        media_dir = work_dir / "media"
        local_filenames = await _download_media(metadata["media_urls"], media_dir)
        metadata["media_local"] = local_filenames
    else:
        metadata["media_local"] = []

    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    return metadata


if __name__ == "__main__":
    import sys

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
