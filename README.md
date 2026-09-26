# gdrive-mcp

A self-hosted MCP server for Google Drive, Docs and Sheets with a configurable folder boundary.
It supports local stdio clients and streamable HTTP behind an optional gateway.

The server is intentionally independent of Google's built-in Drive connector. Access is restricted twice:
Google only exposes files the configured account can access, and `gdrive_mcp/policy.py` independently requires
every file to resolve under an allowlisted Drive root.

## Features

- **Drive:** search, recent files, list, metadata, read, create, rename, move, copy, trash/restore and sharing.
- **Docs:** indexed reads, revision-aware edits, markdown append and table insertion.
- **Sheets:** reads, writes, appends and a bounded `batchUpdate` surface.
- **Comments:** read, create, reply and resolve.
- **File extraction:** PDF, docx, pptx, xlsx and common image formats.
- **Semantic search:** optional local Ollama-backed index.
- **Audit log:** write operations are recorded without dumping file contents.
- **MCP annotations:** tools declare read-only/destructive/open-world hints; enforcement remains server-side.

## Security model

1. **Google permission boundary.** Use a dedicated Google account and share only the Drive roots it needs.
2. **Server boundary.** Every target is checked against `roots` in `config.toml`; shortcuts are checked at both
   the shortcut and target.
3. **Write controls.** Moves/copies stay inside allowed roots, creation can be forbidden directly in a root,
   shares are limited to an explicit allowlist, permanent deletion is not exposed, and Docs/Sheets request
   types are allowlisted.
4. **Untrusted content.** File contents are returned as data and are explicitly marked untrusted.
5. **Local secrets.** OAuth material, runtime configuration, gateway credentials and audit logs are gitignored.

This is a powerful integration: the Google OAuth scope and the configured Drive shares determine the maximum
Google-side access. Use a dedicated account and the smallest set of shared folders that satisfies your use case.

## Setup

Requirements: Python 3.12, `uv`, a Google Cloud OAuth Desktop client, and the Drive/Docs/Sheets APIs enabled.

```bash
uv sync --dev
cp config.example.toml config.toml
```

Edit `config.toml` with your own Google account, Drive root IDs and optional policy rules. `config.toml` is
ignored by git and must not be committed.

Run locally:

```bash
uv run python -m gdrive_mcp.server
```

Run the streamable HTTP server on loopback:

```bash
uv run python -m gdrive_mcp.server --http 8766
```

Runtime filesystem locations are intentionally fixed rather than environment-overridable:

- policy/config: `<repo>/config.toml`
- OAuth token: the current user's local `gdrive-mcp/token.json`
- audit log: the current user's local `gdrive-mcp/audit.jsonl`
- semantic index: the current user's local `gdrive-mcp/index.sqlite`

This prevents inherited process environment variables from redirecting sensitive reads or writes to arbitrary filesystem paths. Never commit the token or other credential material.

## OAuth token

The server expects a local OAuth token JSON at the configured/default token path. Generate it with your own
OAuth Desktop client and keep both the token and client-secret JSON outside the repository. The exact auth
bootstrap is deployment-specific; no credentials are included in this repository.

## Configuration

`config.example.toml` contains placeholders only. Important sections:

- `[[roots]]` — allowed Drive root folder IDs and display names.
- `[policy].deny` — IDs that remain blocked even when they are below a root.
- `[policy].share_allowlist` — addresses accepted by share/unshare tools.
- `[[write_rules]]` — optional per-spreadsheet constraints.
- `[index]` — local semantic-index configuration and exclusions.

## Optional Cloudflare gateway

`compose.public.yaml` runs the Node gateway used by the shared Cloudflare Tunnel. Runtime Access settings are loaded from the local gitignored file:

- `public/gateway.env`

Start from the sanitized template:

```bash
cp public/gateway.env.example public/gateway.env
```

Replace the placeholders with your own deployment values; never commit the real file.

The gateway requires Cloudflare Access on every MCP request, applies a tool allowlist and request limits, strips
credential/origin headers, and forwards only to the loopback MCP server. The plain `/mcp` route is canonical;
a configured legacy secret-path alias is routing-only and never bypasses Access. Configure your own public
hostname and Access policy in Cloudflare, and route the shared tunnel to `http://drive-gateway:8080`; no account or tunnel credentials are stored in the repository.

```bash
docker compose -f compose.public.yaml up -d
```

The gateway is optional; local stdio use does not require Cloudflare or Docker.

## Validation

Unit tests use an in-memory fake Google API and require no credentials:

```bash
uv run pytest
```

For a live smoke test, provide a writable subfolder ID from your own configured roots:

```bash
uv run python scripts/live_smoke.py --folder-id YOUR_TEST_FOLDER_ID
```

Optional flags let the script exercise sharing or a known outside-root negative test without hardcoding any
private IDs in source. Run `--help` for details.

The semantic-search benchmark is data-driven. Put benchmark cases in an untracked local JSON file and run:

```bash
uv run python scripts/measure_search.py path/to/benchmark_cases.local.json report.md
```

Each case is an object with `question`, `keywords`, and `expected_path`.

## Files that must remain local

The `.gitignore` intentionally excludes configuration, OAuth material, `.env` files, audit logs and local
benchmark case files. Before making a fork or deployment public, still run a secret/PII scan over the full Git
history; `.gitignore` only protects future commits.
