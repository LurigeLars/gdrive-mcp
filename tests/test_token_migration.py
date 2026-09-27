from __future__ import annotations

import json

import pytest

from gdrive_mcp.token_store import TokenStoreError
from scripts import migrate_token_dpapi


def _legacy_token() -> dict:
    return {
        "token": "migration-test-access",
        "refresh_token": "migration-test-refresh",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "migration-test-client",
        "client_secret": "migration-test-client-value",
        "scopes": ["https://www.googleapis.com/auth/drive"],
    }


def test_migration_removes_legacy_only_after_verified_round_trip(tmp_path):
    legacy = tmp_path / "token.json"
    protected = tmp_path / "token.dpapi"
    token = _legacy_token()
    legacy.write_text(json.dumps(token), encoding="utf-8")

    migrate_token_dpapi.migrate(legacy, protected)

    assert protected.exists()
    assert not legacy.exists()


def test_migration_verification_failure_preserves_legacy(monkeypatch, tmp_path):
    legacy = tmp_path / "token.json"
    protected = tmp_path / "token.dpapi"
    token = _legacy_token()
    legacy.write_text(json.dumps(token), encoding="utf-8")

    monkeypatch.setattr(
        migrate_token_dpapi,
        "load_token_info",
        lambda path: {**token, "refresh_token": "different"},
    )

    with pytest.raises(TokenStoreError, match="verification failed"):
        migrate_token_dpapi.migrate(legacy, protected)

    assert legacy.exists()
    assert not protected.exists()


def test_migration_refuses_to_overwrite_existing_protected_token(tmp_path):
    legacy = tmp_path / "token.json"
    protected = tmp_path / "token.dpapi"
    legacy.write_text(json.dumps(_legacy_token()), encoding="utf-8")
    protected.write_bytes(b"existing-protected-data")

    with pytest.raises(TokenStoreError, match="refusing to overwrite"):
        migrate_token_dpapi.migrate(legacy, protected)

    assert legacy.exists()
    assert protected.read_bytes() == b"existing-protected-data"
