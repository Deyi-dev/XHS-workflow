---
name: xhs-to-obsidian
description: >
  Archive a Xiaohongshu note to the Obsidian vault.
  Trigger when the user shares a xiaohongshu.com or xhslink.com URL
  and expresses intent to save, archive, collect, or store it
  (e.g. "帮我存下来", "归档", "收藏到 Obsidian", "save this note").
---

# XHS → Obsidian Archiving SOP

## Trigger conditions

- User message contains an XHS link (pattern: `xhslink.com` or `xiaohongshu.com/explore/`)
- AND user expresses archiving intent: 存/存下/收藏/归档/保存/save/archive/keep

---

## Step-by-step procedure

### 1. Preview (always run first)

```bash
uv run scripts/ingest.py <xhs_url>
```

This downloads the note and runs Gemini analysis but does **not** write to the vault.
It prints a JSON object to stdout. Parse it.

**On error** (`status == "error"`):
- `step: "download"` + `error_code: "LOGIN_REQUIRED"` → cookies are missing or expired. Tell the user to run `bin/xiaohongshu-login-darwin-arm64` (from project root `xhs-to-obsidian/`) and re-try
- `step: "download"` + `error_code: "RATE_LIMITED"` → XHS triggered "访问频繁" (error_code=300013). Wait an hour or change IP before retrying
- `step: "download"` + `error_code: "INVALID_URL"` → the link may be expired; ask user to share a new one
- `step: "download"` + `error_code: "NOTE_DELETED"` → note has been removed from the platform
- `step: "download"` + `error_code: "SCRAPE_FAILED"` → page loaded but `__INITIAL_STATE__` is missing/unparseable; XHS may have changed its page format
- `step: "analyze"` → analysis failed; proceed to step 2 and note `status: lite`
- `step: "archive"` → vault write failed; check `OBSIDIAN_VAULT_PATH`

### 2. Present a one-line confirmation to the user

Summarise the preview JSON in a single sentence, for example:

> 打算归类为 **recipe**，tags: `#cooking #家常菜 #减脂餐`，共 6 张图。确认归档吗？

Fields to surface: `content_type`, `suggested_tags`, `title`, `note_type`, `media_count` (if committed).

### 3. Wait for explicit user confirmation

Do **not** commit until the user says yes / 确认 / OK / 好的.

The agent may adjust `content_type` or `suggested_tags` based on user feedback before committing.

### 4. Commit (write to vault)

```bash
uv run scripts/ingest.py <xhs_url> --commit
```

Parse the returned JSON. On success:
- `status == "committed"`
- `rel_path` gives the vault-relative path of the new note

Report to user:
> 已归档到 `{{ rel_path }}`，tags: `{{ tags }}`。

---

## Division of responsibilities

| Layer | Responsibility |
|-------|----------------|
| **Scripts** | All deterministic work: HTTP calls, Gemini API, file I/O |
| **Agent** | Read JSON output, present to user, handle confirmation dialogue, optionally tweak frontmatter fields before commit |

The agent must **not** write markdown files directly; always go through `archive.py` or `ingest.py --commit`.

---

## Environment requirements

| Variable | Purpose |
|----------|---------|
| `GEMINI_API_KEY` | Gemini vision analysis (graceful fallback if absent) |
| `OBSIDIAN_VAULT_PATH` | Absolute path to Obsidian vault root |
| `XHS_COOKIES_PATH` | XHS session cookies path (default: `bin/cookies.json` relative to project) |

**Login**: when you get `LOGIN_REQUIRED`:

1. Download the login binary for your platform from [xpzouying/xiaohongshu-mcp releases](https://github.com/xpzouying/xiaohongshu-mcp/releases):
   - macOS arm64: `xiaohongshu-login-darwin-arm64`
   - macOS x86: `xiaohongshu-login-darwin-amd64`
   - Linux x86: `xiaohongshu-login-linux-amd64`
   - Windows: `xiaohongshu-login-windows-amd64.exe`

2. Place it in `bin/` and make it executable: `chmod +x bin/xiaohongshu-login-*`

3. Run and scan the QR code:
   ```bash
   ./bin/xiaohongshu-login-darwin-arm64  # (or your platform)
   ```

4. Cookies are written to `bin/cookies.json`. No long-running server needed.

---

## Portability note

This skill uses only standard shell execution (`uv run`), file I/O, and
environment variables. It works identically in Claude Code, OpenClaw, Cursor,
Codex, or any agent that can run a subprocess and read its stdout.
