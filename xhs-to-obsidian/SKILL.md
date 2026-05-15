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

There is **no user-confirmation step**. Once triggered, archive end-to-end and
report the result. You (the agent) are the classifier.

---

## Step-by-step procedure

### 1. Archive (download → vault), then hand off

```bash
uv run scripts/ingest.py <xhs_url>
```

This downloads to a temp dir and **archives the note into the vault
immediately** (pure file placement — no classification yet). It prints a
JSON object — parse it.

**On error** (`status == "error"`):
- `step: "download"` + `error_code: "LOGIN_REQUIRED"` → cookies missing/expired. Tell the user to run `bin/xiaohongshu-login-darwin-arm64` (from `xhs-to-obsidian/`) then retry
- `step: "download"` + `error_code: "RATE_LIMITED"` → XHS "访问频繁" (300013). Wait an hour or change IP
- `step: "download"` + `error_code: "INVALID_URL"` → link expired; ask for a fresh one
- `step: "download"` + `error_code: "NOTE_DELETED"` → note removed from platform
- `step: "download"` + `error_code: "SCRAPE_FAILED"` → XHS page format changed
- `step: "archive"` + `error_code: "ARCHIVE_FAILED"` → vault write failed; check `OBSIDIAN_VAULT_PATH`
- `step: "index"` + `error_code: "INDEX_READ_FAILED"` → `_index.md` missing; check `OBSIDIAN_VAULT_PATH`

On success `status == "archived"`, the JSON contains the archived
`vault_path` / `rel_path` / `stem`, a `body` preview, `tags`, and
`sections` — the section names currently in `_index.md`.

### 2. Classify from the real archived note

Read the archived markdown at `vault_path` for full content (the JSON
`body` is truncated). Choose **exactly one** value from the `sections`
array — use the string verbatim, including the emoji. Do not invent a new
section; if nothing fits, pick the catch-all (`📦 Other`).

Write a concise one-line Chinese tagline (≤ 30 汉字) capturing what the
note is about — do not just copy the title.

### 3. Record the classification in _index.md

```bash
uv run scripts/ingest.py <xhs_url> --section "<chosen section>" --tagline "<tagline>"
```

Download is a cache hit and archive is idempotent; this call's real work
is inserting `- [[stem]] — tagline` as the newest entry under
`## <section>` in `_index.md` and bumping the heading / 目录 / total
counts. Re-running with a different section moves the entry and rebalances
counts automatically.

Parse the returned JSON. On `status == "committed"` report:

> 已归档到 `{{ rel_path }}`，归类为 **{{ section }}**。

---

## Division of responsibilities

| Layer | Responsibility |
|-------|----------------|
| **Scripts** | Deterministic work: HTTP, file I/O, template render, index insert + count bookkeeping |
| **Agent** | Read the archived note, classify into one existing section, write the tagline, call phase 2 |

The agent must **not** write markdown files or edit `_index.md` directly;
always go through `ingest.py`. Note frontmatter holds only factual metadata
(source, author, timestamps, tags, media) — classification lives solely in
`_index.md`.

---

## Environment requirements

| Variable | Purpose |
|----------|---------|
| `OBSIDIAN_VAULT_PATH` | Absolute path to Obsidian vault root (must contain `0_inbox/xhs/_index.md`) |
| `XHS_COOKIES_PATH` | XHS session cookies path (default: `bin/cookies.json` relative to project) |

**Login**: when you get `LOGIN_REQUIRED`:

1. Download the login binary from [xpzouying/xiaohongshu-mcp releases](https://github.com/xpzouying/xiaohongshu-mcp/releases) for your platform.
2. Place it in `bin/` and `chmod +x bin/xiaohongshu-login-*`
3. Run it and scan the QR code: `./bin/xiaohongshu-login-darwin-arm64`
4. Cookies are written to `bin/cookies.json`. No long-running server needed.

---

## Portability note

This skill uses only standard shell execution (`uv run`), file I/O, and
environment variables. It works in any agent that can run a subprocess and
read its stdout.
