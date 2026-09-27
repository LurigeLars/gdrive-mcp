"""Migrate the legacy Windows Google OAuth token.json into a DPAPI-protected store."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from gdrive_mcp.paths import LEGACY_TOKEN, TOKEN
from gdrive_mcp.token_store import TokenStoreError, is_windows, load_token_info, save_token_info


def migrate(legacy_path: Path, protected_path: Path) -> None:
    if protected_path.exists():
        raise TokenStoreError("Protected token already exists; refusing to overwrite it")
    if not legacy_path.exists():
        raise TokenStoreError("Legacy plaintext token does not exist")

    try:
        source = json.loads(legacy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TokenStoreError("Legacy plaintext token is invalid") from error
    if not isinstance(source, dict):
        raise TokenStoreError("Legacy plaintext token is invalid")

    try:
        save_token_info(protected_path, source)
        verified = load_token_info(protected_path)
        if verified != source:
            raise TokenStoreError("Protected token verification failed")
    except Exception:
        try:
            protected_path.unlink(missing_ok=True)
        except OSError:
            # Preserve the original migration failure. Startup still refuses the
            # legacy plaintext token, so a partial protected blob cannot enable use.
            pass
        raise

    try:
        legacy_path.unlink()
    except OSError as error:
        raise TokenStoreError(
            "Protected token verified, but the legacy plaintext file could not be removed"
        ) from error


def main() -> int:
    if not is_windows() or LEGACY_TOKEN is None:
        print("DPAPI token migration is only required on Windows.", file=sys.stderr)
        return 2

    try:
        migrate(LEGACY_TOKEN, TOKEN)
    except TokenStoreError as error:
        print(f"Migration failed safely: {error}", file=sys.stderr)
        return 1

    print(f"Protected token verified: {TOKEN}")
    print("Legacy plaintext token removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
