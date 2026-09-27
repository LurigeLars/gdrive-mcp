"""OS-protected persistence for the Google OAuth authorized-user token.

On Windows the token is encrypted with DPAPI CurrentUser before it reaches disk.
On non-Windows platforms this module preserves the existing local-file behavior.
"""
from __future__ import annotations

import ctypes
import json
import os
import tempfile
from pathlib import Path
from typing import Any

_DPAPI_UI_FORBIDDEN = 0x1
_MAX_TOKEN_BYTES = 1024 * 1024


class TokenStoreError(RuntimeError):
    """Safe token-store failure without credential material or backend details."""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def is_windows() -> bool:
    return os.name == "nt"


def _blob(data: bytes) -> tuple[_DataBlob, Any]:
    buffer = (ctypes.c_ubyte * max(1, len(data)))()
    if data:
        ctypes.memmove(buffer, data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _dpapi_function(name: str):
    if not is_windows():
        raise TokenStoreError("Windows DPAPI is unavailable on this platform")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    function = getattr(crypt32, name)
    if name == "CryptProtectData":
        function.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_wchar_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(_DataBlob),
        ]
    elif name == "CryptUnprotectData":
        function.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(_DataBlob),
        ]
    else:
        raise TokenStoreError("Unsupported Windows DPAPI operation")
    function.restype = ctypes.c_int
    return function


def _local_free(pointer) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree(pointer)


def _protect_windows(data: bytes) -> bytes:
    source, source_buffer = _blob(data)
    destination = _DataBlob()
    protect = _dpapi_function("CryptProtectData")
    _ = source_buffer
    ok = protect(
        ctypes.byref(source),
        "gdrive-mcp Google OAuth token",
        None,
        None,
        None,
        _DPAPI_UI_FORBIDDEN,
        ctypes.byref(destination),
    )
    if not ok:
        raise TokenStoreError("Could not protect the Google OAuth token with Windows DPAPI")
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        _local_free(destination.pbData)


def _unprotect_windows(data: bytes) -> bytes:
    source, source_buffer = _blob(data)
    destination = _DataBlob()
    unprotect = _dpapi_function("CryptUnprotectData")
    _ = source_buffer
    ok = unprotect(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        _DPAPI_UI_FORBIDDEN,
        ctypes.byref(destination),
    )
    if not ok:
        raise TokenStoreError("Could not unlock the Google OAuth token with Windows DPAPI")
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        _local_free(destination.pbData)


def _decode_token_json(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > _MAX_TOKEN_BYTES:
        raise TokenStoreError("Google OAuth token data is missing or invalid")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TokenStoreError("Google OAuth token data is invalid") from error
    if not isinstance(value, dict):
        raise TokenStoreError("Google OAuth token data is invalid")
    return value


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary_path, 0o600)
        except OSError:
            pass
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def load_token_info(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        data = target.read_bytes()
    except OSError as error:
        raise TokenStoreError("Could not read the Google OAuth token store") from error
    if len(data) > _MAX_TOKEN_BYTES:
        raise TokenStoreError("Google OAuth token data is missing or invalid")
    if is_windows():
        data = _unprotect_windows(data)
    return _decode_token_json(data)


def save_token_info(path: str | Path, token: dict[str, Any]) -> None:
    try:
        raw = json.dumps(token, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise TokenStoreError("Google OAuth token data is invalid") from error
    if not raw or len(raw) > _MAX_TOKEN_BYTES:
        raise TokenStoreError("Google OAuth token data is missing or invalid")
    data = _protect_windows(raw) if is_windows() else raw
    try:
        _atomic_write(Path(path), data)
    except OSError as error:
        raise TokenStoreError("Could not write the Google OAuth token store") from error
