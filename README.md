# gdrive-mcp

## Repository status

This is an original MCP server, **not a fork of Google's Drive integrations or another Drive MCP project**. It is designed around a self-hosted, folder-bounded Google Drive/Docs/Sheets deployment.

Project-specific design includes:

- A server-enforced allowlisted Drive-root boundary in addition to Google's own account permissions.
- Drive, Docs, Sheets, comments, file extraction, and bounded write operations behind the same policy layer.
- Optional local semantic search and an audit log that avoids storing file contents.
- Local stdio/HTTP operation plus an optional Cloudflare Access gateway with explicit tool and request controls.
- Windows OAuth token material protected at rest with DPAPI CurrentUser, plus local-only runtime configuration, deterministic runtime paths, tests, and security-focused CI.

The project is intentionally independent of Google's built-in ChatGPT Drive connector.

A self-hosted MCP server for Google Drive, Docs and Sheets with a configurable folder boundary. It supports local stdio clients and Streamable HTTP behind an optional gateway.

Access is restricted twice: Google only exposes files the configured account can access, and `gdrive_mcp/policy.py` independently requires every file to resolve under an allowlisted Drive root.

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
5. **Local secrets.** On Windows, the Google OAuth authorized-user token is stored as a DPAPI CurrentUser blob; runtime configuration, gateway settings and audit logs remain local and gitignored.

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

**Verify** the connection by listing the tools: 26 is the full surface. `drive_recent` is the
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

The `.gitignore` intentionally excludes configuration, OAuth material (including DPAPI blobs), `.env` files, audit logs and local
benchmark case files. Before making a fork or deployment public, still run a secret/PII scan over the full Git
history; `.gitignore` only protects future commits.
