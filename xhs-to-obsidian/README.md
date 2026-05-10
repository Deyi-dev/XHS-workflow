# xhs-to-obsidian

Archive Xiaohongshu notes to your Obsidian vault with one command.

**Architecture:** XHS URL → Direct page scrape + cookies → Obsidian markdown

---

## Prerequisites

### 1. Python ≥ 3.11 + uv

```bash
curl -Lsf https://astral.sh/uv/install.sh | sh
```

### 2. XHS Login Binary

Download from **[xpzouying/xiaohongshu-mcp releases](https://github.com/xpzouying/xiaohongshu-mcp/releases)** → `xiaohongshu-login-*`:

```bash
# Choose your platform:
# - macOS arm64:   xiaohongshu-login-darwin-arm64
# - macOS x86:     xiaohongshu-login-darwin-amd64
# - Linux x86:     xiaohongshu-login-linux-amd64
# - Windows:       xiaohongshu-login-windows-amd64.exe

mkdir -p bin
mv xiaohongshu-login-darwin-arm64 bin/
chmod +x bin/xiaohongshu-login-*

# Run once to scan QR code and save cookies
./bin/xiaohongshu-login-darwin-arm64
```

Cookies are stored in `bin/cookies.json`. Re-run the login binary when cookies expire.

---

## Installation

```bash
git clone <this-repo>
cd xhs-to-obsidian
uv sync
```

---

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `OBSIDIAN_VAULT_PATH` | **yes** | — | Absolute path to Obsidian vault root |
| `GEMINI_API_KEY` | recommended | — | Google Gemini API key for image OCR and classification. Falls back to metadata-only if absent. |
| `XHS_COOKIES_PATH` | no | `~/.xhs-mcp/bin/cookies.json` | XHS session cookies. Refresh via `~/.xhs-mcp/bin/xiaohongshu-login-darwin-arm64`. No long-running MCP server needed. |

Set them in your shell or a `.env` file:

```bash
export OBSIDIAN_VAULT_PATH="/Users/you/Documents/MyVault"
export GEMINI_API_KEY="AIza..."
```

---

## Usage

### Full pipeline (recommended)

```bash
# Preview – downloads + analyses, does NOT write to vault
uv run scripts/ingest.py "http://xhslink.com/o/6NpcvjCnxOk"

# Commit – same as above but also writes to vault
uv run scripts/ingest.py "http://xhslink.com/o/6NpcvjCnxOk" --commit
```

Output is a JSON object that agents (or you) can inspect before committing.

### Run individual steps

```bash
# Step 1: download only
uv run scripts/download.py "https://www.xiaohongshu.com/explore/<id>?xsec_token=..." /tmp/xhs-mywork

# Step 2: analyse images
uv run scripts/analyze.py /tmp/xhs-mywork

# Step 3: write to vault
uv run scripts/archive.py /tmp/xhs-mywork
```

---

## Vault layout

```
<OBSIDIAN_VAULT_PATH>/
├── 0_inbox/xhs/
│   └── 2025-01-15-my-note-title.md   ← new note
└── attachments/xhs/<xhs_id>/
    ├── image_0.jpg
    └── image_1.jpg
```

Frontmatter fields written to every note:

```yaml
source_url:       # original XHS link
source_platform:  xiaohongshu
xhs_id:           # note ID
author:           # display name
author_id:        # user ID
captured_at:      # ISO timestamp of archiving
published_at:     # ISO timestamp from the platform
content_type:     # recipe | workout | snowboarding | long-form-article | video-tutorial | other
tags:             # merged from platform tags + Gemini suggestions
media:            # relative paths to attachment files
status:           # lite | full
priority:         # low | medium | high (default: medium)
```

---

## Cross-agent integration

Clone this directory into any agent's skills folder:

```bash
# Claude Code
cp -r xhs-to-obsidian ~/.claude/skills/

# OpenClaw / other agents: point them at SKILL.md
```

The skill uses only `uv run` subprocess calls and standard env vars.
No agent-specific APIs or SDKs are used inside the scripts.

---

## Troubleshooting

| Error code | Cause | Fix |
|-----------|-------|-----|
| `LOGIN_REQUIRED` | Cookies missing or expired | Run `~/.xhs-mcp/bin/xiaohongshu-login-darwin-arm64` to refresh |
| `RATE_LIMITED` | XHS triggered "访问频繁" (error_code 300013) | Wait an hour or change IP. Lower `--concurrency` for `batch_ingest.py` |
| `INVALID_URL` | Short link expired or malformed | Get a fresh share link from the app |
| `NOTE_DELETED` | Note removed from platform | Nothing to do |
| `SCRAPE_FAILED` | Page loaded but `__INITIAL_STATE__` is missing/unparseable | XHS may have changed its page format |
| `ARCHIVE_FAILED` | Vault path wrong | Check `OBSIDIAN_VAULT_PATH` |
| Gemini 429 | Rate limit hit | Reduce image count or wait 60 s |
