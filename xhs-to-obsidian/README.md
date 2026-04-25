# xhs-to-obsidian

Archive Xiaohongshu notes to your Obsidian vault with one command.

**Pipeline:** XHS URL → xiaohongshu-mcp → Gemini 2.5 Flash OCR/classification → Obsidian markdown

---

## Prerequisites

### 1. xiaohongshu-mcp (local service)

Clone and start the MCP server from **[xpzouying/xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp)**:

```bash
# Docker (recommended)
docker compose up -d

# Or run the binary directly
./xiaohongshu-mcp-darwin-arm64        # macOS ARM
./xiaohongshu-mcp-linux-amd64         # Linux x86
```

The server listens on `http://localhost:18060/mcp` by default.

**Login:** Open the MCP browser UI and scan the QR code with your Xiaohongshu app once, then cookies are stored automatically.

### 2. Python ≥ 3.11 + uv

```bash
curl -Lsf https://astral.sh/uv/install.sh | sh
```

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
| `XHS_MCP_URL` | no | `http://localhost:18060/mcp` | Override MCP server URL |

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
| `MCP_UNREACHABLE` | xiaohongshu-mcp not running | `docker compose up -d` |
| `NOT_LOGGED_IN` | Cookies expired | Re-scan QR code in MCP browser |
| `INVALID_URL` | Short link expired or malformed | Get a fresh share link from the app |
| `NOTE_DELETED` | Note removed from platform | Nothing to do |
| `ARCHIVE_FAILED` | Vault path wrong | Check `OBSIDIAN_VAULT_PATH` |
| Gemini 429 | Rate limit hit | Reduce image count or wait 60 s |
