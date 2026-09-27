from __future__ import annotations

import json

import pytest

from gdrive_mcp import token_store


def _token() -> dict:
    return {
        "token": "test-access-value",
        "refresh_token": "test-refresh-value",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "test-client-id",
        "client_secret": "test-client-value",
        "scopes": ["https://www.googleapis.com/auth/drive"],
    }


def test_token_store_round_trip(tmp_path):
    path = tmp_path / ("token.dpapi" if token_store.is_windows() else "token.json")
    value = _token()

    token_store.save_token_info(path, value)

    assert token_store.load_token_info(path) == value
    if token_store.is_windows():
        raw = path.read_bytes()
        assert b"test-access-value" not in raw
        assert b"test-refresh-value" not in raw
        assert b"test-client-value" not in raw


def test_windows_storage_path_uses_protection_before_disk(monkeypatch, tmp_path):
    path = tmp_path / "token.dpapi"
    value = _token()

    monkeypatch.setattr(token_store, "is_windows", lambda: True)
    monkeypatch.setattr(token_store, "_protect_windows", lambda raw: b"dpapi-test:" + raw[::-1])
    monkeypatch.setattr(
        token_store,
        "_unprotect_windows",
        lambda raw: raw.removeprefix(b"dpapi-test:")[::-1],
    )

    token_store.save_token_info(path, value)

    raw = path.read_bytes()
    assert raw.startswith(b"dpapi-test:")
    assert b"test-refresh-value" not in raw
    assert token_store.load_token_info(path) == value


def test_invalid_token_is_rejected(monkeypatch, tmp_path):
    # This test targets the JSON decoder. Windows DPAPI behavior is exercised by
    # test_token_store_round_trip, so force the plaintext decoder path here.
    monkeypatch.setattr(token_store, "is_windows", lambda: False)
    path = tmp_path / "token.json"
    path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

    with pytest.raises(token_store.TokenStoreError, match="invalid"):
        token_store.load_token_info(path)
