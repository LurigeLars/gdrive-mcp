# gdrive-mcp

[![CI](https://github.com/LurigeLars/gdrive-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/LurigeLars/gdrive-mcp/actions/workflows/ci.yml)
[![CodeQL](https://github.com/LurigeLars/gdrive-mcp/actions/workflows/codeql.yml/badge.svg)](https://github.com/LurigeLars/gdrive-mcp/actions/workflows/codeql.yml)
[![Static analysis](https://github.com/LurigeLars/gdrive-mcp/actions/workflows/static-analysis.yml/badge.svg)](https://github.com/LurigeLars/gdrive-mcp/actions/workflows/static-analysis.yml)
![Python](https://img.shields.io/badge/python-3.12-blue)

A self-hosted Model Context Protocol (MCP) server for Google Drive, Docs and Sheets with
a **server-enforced folder boundary**.

It lets local or remote MCP clients search, read and perform reviewed writes in selected
Google Drive roots without giving the model unrestricted access to everything the Google
account can see.

## Why this project exists

Google OAuth permissions answer one question:

> What can this Google account access?

For an AI agent, that is often too broad. This project adds a second, independent
question enforced by the MCP server:

> Is this file or folder inside one of the Drive roots this agent is allowed to use?

Every Drive target is resolved through that policy before the operation is allowed.
That gives the deployment a least-privilege boundary even when the Google account itself
can see more of Drive.

The project is useful when you want an agent to work with a known subset of Drive —
project folders, shared working areas or controlled Sheets/Docs — without exposing the
rest of the account.

## What it can do

| Area | Capabilities |
|---|---|
| Drive | search, recent files, list, metadata, read, create, rename, move, copy, trash/restore and sharing |
| Docs | indexed reads, revision-aware edits, markdown append and table insertion |
| Sheets | reads, USER_ENTERED writes/appends and a bounded batchUpdate surface |
| Comments | read, create, reply and resolve |
| Extraction | bounded PDF, DOCX, PPTX, XLSX and common image extraction |
| Search | optional local semantic index |
| Audit | write-operation log without storing file contents |

The same policy layer applies regardless of whether the client is local stdio, local
HTTP or a remote client behind the optional gateway.

## Security model

The design uses several independent boundaries rather than relying on one credential:

1. **Google account boundary** — the configured account can only see what Google allows.
2. **Drive-root boundary** — every requested target must resolve under an allowlisted
   root in `config.toml`.
3. **Write policy** — creation, moves/copies, sharing and Docs/Sheets request types are
   separately constrained.
4. **Untrusted content boundary** — file contents are data returned to the model, never
   instructions to the MCP server.
5. **Local-secret boundary** — OAuth/config/audit/index state remains local and ignored
   by Git.
6. **Remote edge boundary** — cloud clients reach only the reviewed Cloudflare Access
   gateway; the MCP runtime stays on loopback.

Shortcuts are checked at both the shortcut and target. Moves and copies must remain
inside allowed roots. Permanent deletion is not exposed.

On Windows, OAuth authorized-user material is stored with DPAPI CurrentUser instead of
a plaintext runtime token.

## Architecture

```text
Google Drive / Docs / Sheets APIs
              ^
              |
       Google OAuth identity
              ^
              |
        gdrive-mcp policy
       /       |        \
      /        |         \
 Drive-root  write     audit/index
 checks      rules      local state
      ^
      |
 local stdio / loopback HTTP
      ^
      |
 MCP client
```

Optional remote access:

```text
ChatGPT / remote MCP client
      |
      v
Cloudflare Access
      |
      v
reviewed gateway
      |
      v
loopback gdrive-mcp
```

## What this project is not

- It is **not** a generic unrestricted Google Drive proxy.
- It is **not** a fork of Google's Drive integrations or another Drive MCP server.
- It is **not** the built-in ChatGPT Google Drive connector.
- It does not bypass Google permissions.
- It does not expose permanent deletion.
- It does not make a file writable merely because it is readable.

Client implementations and policy are intentionally local-first so the operator can
choose exactly which folders and write surfaces are available.

## Quick start

Requirements:

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- a Google Cloud OAuth Desktop client
- Drive, Docs and Sheets APIs enabled
- one or more Drive folder IDs to use as allowed roots

The next section contains the concrete setup steps. Real OAuth material, root IDs,
machine paths and gateway values must stay in ignored local configuration.

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
- OAuth token: `gdrive-mcp/token.dpapi` on Windows (DPAPI CurrentUser); `gdrive-mcp/token.json` on other platforms
- audit log: the current user's local `gdrive-mcp/audit.jsonl`
- semantic index: the current user's local `gdrive-mcp/index.sqlite`

This prevents inherited process environment variables from redirecting sensitive reads or writes to arbitrary filesystem paths. Never commit the token or other credential material.

## Connecting a client

The server speaks stdio for local clients and streamable HTTP on loopback for local agents that
prefer it. Cloud chats reach it only through the Cloudflare gateway described below.

| Client | Connection |
|---|---|
| Claude Desktop, Cursor, VS Code | local stdio from a checkout |
| Claude Code, Codex | local stdio, or loopback HTTP on `127.0.0.1:8766` |
| ChatGPT and other cloud chats | Cloudflare Access to the public gateway |

Replace every path below with your own; nothing here should be copied literally.

**Claude Desktop** — Settings > Developer > Edit Config, merge, then restart Desktop fully:

```json
{
  "mcpServers": {
    "gdrive": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/gdrive-mcp", "--frozen",
               "python", "-m", "gdrive_mcp.server"]
    }
  }
}
```

On Windows give `command` the absolute path to `uv.exe`; Claude Desktop does not resolve it from
`PATH`.

**Claude Code and Codex:**

```bash
claude mcp add gdrive -- uv run --directory /path/to/gdrive-mcp --frozen python -m gdrive_mcp.server
codex mcp add gdrive -- uv run --directory /path/to/gdrive-mcp --frozen python -m gdrive_mcp.server
```

To use the loopback HTTP server instead, start it with `--http 8766` and point the client at
`http://127.0.0.1:8766/mcp`.

Every client reads the same `config.toml` and the same OAuth token, so they share one identity and
one set of Drive roots. Running several clients at once is supported; they each start their own
process against the same local state, which means the semantic index is opened more than once.

**Verify** the connection by listing the tools: 27 is the full surface. `drive_recent` is the
cheapest call that proves both OAuth and the Drive roots are working.

## OAuth token

Generate the authorized-user token with your own OAuth Desktop client and keep both the token and client-secret
JSON outside the repository. The exact auth bootstrap is deployment-specific; no credentials are included here.

On **Windows**, DriveMCP does not read the OAuth token from plaintext at runtime. The token is stored at
`%USERPROFILE%\\AppData\\Local\\gdrive-mcp\\token.dpapi`, encrypted with Windows DPAPI CurrentUser. Existing
installations that still have `%USERPROFILE%\\AppData\\Local\\gdrive-mcp\\token.json` must migrate it once:

```powershell
uv run python scripts/migrate_token_dpapi.py
```

The migration writes the protected blob, decrypts it again to verify an exact JSON round trip, and only then
removes the legacy plaintext file. DriveMCP refuses to start on Windows while only the legacy plaintext token is
present. OAuth token refreshes are written back through the same protected store.

On macOS/Linux, the current implementation retains the existing local `token.json` behavior; use local filesystem
permissions and keep the file out of source control. Platform-native keychain support there is a separate hardening
task rather than an implied property of the Windows DPAPI implementation.

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

The gateway requires Cloudflare Access on every MCP request, applies a tool allowlist and request limits, requires an
explicit email allowlist, rejects browser-origin requests, strips credentials before forwarding, and forwards only to the loopback MCP server. The plain `/mcp` route is canonical;
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

The `.gitignore` intentionally excludes configuration, OAuth material (including DPAPI blobs), `.env` files, audit logs and local
benchmark case files. Before making a fork or deployment public, still run a secret/PII scan over the full Git
history; `.gitignore` only protects future commits.
