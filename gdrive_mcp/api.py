"""Thin Google Drive/Docs/Sheets REST client. Every network call of the server goes through here."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from urllib.parse import quote

DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
DOCS = "https://docs.googleapis.com/v1/documents"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/drive"]
FILE_FIELDS = ("id,name,mimeType,parents,owners(emailAddress),modifiedTime,size,trashed,"
               "shortcutDetails(targetId,targetMimeType),webViewLink")
COMMENT_FIELDS = ("id,author(displayName),createdTime,resolved,content,quotedFileContent(value),"
                  "replies(id,author(displayName),createdTime,content,action)")
TIMEOUT = 60
RETRIES = 3
BACKOFF = 2.0  # seconds, doubled per attempt
RETRY_STATUS = {429, 500, 502, 503, 504}  # per-minute quota and transient server errors


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Google API error {status}: {message}")
        self.status = status


class GoogleApi:
    def __init__(self, session):
        self.s = session

    @classmethod
    def from_token(cls, token_path: str | Path) -> GoogleApi:
        from google.auth.transport.requests import AuthorizedSession, Request
        from google.oauth2.credentials import Credentials

        path = Path(token_path)
        creds = Credentials.from_authorized_user_file(str(path), SCOPES)
        if not creds.valid:
            creds.refresh(Request())
            path.write_text(creds.to_json(), encoding="utf-8")
        return cls(AuthorizedSession(creds))

    def _call(self, method: str, url: str, *, raw: bool = False, ok404: bool = False, **kw):
        for attempt in range(RETRIES):
            try:
                r = self.s.request(method, url, timeout=TIMEOUT, **kw)
            except OSError:  # dropped connection; Google closes idle sockets
                if attempt == RETRIES - 1:
                    raise
                time.sleep(BACKOFF * 2**attempt)
                continue
            if r.status_code in RETRY_STATUS and attempt < RETRIES - 1:
                time.sleep(float(r.headers.get("retry-after") or BACKOFF * 2**attempt))
                continue
            break
        if ok404 and r.status_code == 404:
            return None
        if r.status_code >= 400:
            try:
                msg = r.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                msg = r.text[:300]
            raise ApiError(r.status_code, msg)
        if raw:
            return r.content
        return r.json() if r.content else {}

    # --- Drive ---------------------------------------------------------------------------------
    def get_file(self, file_id: str, fields: str = FILE_FIELDS) -> dict | None:
        return self._call("GET", f"{DRIVE}/files/{quote(file_id)}", ok404=True, params={"fields": fields})

    def list_files(self, q: str, *, page_token: str | None = None, order_by: str | None = None,
                   page_size: int = 100) -> dict:  # Drive allows up to 1000 per page
        params = {"q": q, "pageSize": page_size, "fields": f"nextPageToken,files({FILE_FIELDS})"}
        if page_token:
            params["pageToken"] = page_token
        if order_by:
            params["orderBy"] = order_by
        return self._call("GET", f"{DRIVE}/files", params=params)

    def start_page_token(self) -> str:
        return self._call("GET", f"{DRIVE}/changes/startPageToken")["startPageToken"]

    def list_changes(self, page_token: str) -> dict:
        return self._call("GET", f"{DRIVE}/changes", params={
            "pageToken": page_token, "pageSize": 1000, "includeRemoved": "true",
            "fields": f"nextPageToken,newStartPageToken,changes(fileId,removed,file({FILE_FIELDS}))"})

    def export(self, file_id: str, mime: str) -> bytes:
        return self._call("GET", f"{DRIVE}/files/{quote(file_id)}/export", raw=True, params={"mimeType": mime})

    def download(self, file_id: str) -> bytes:
        return self._call("GET", f"{DRIVE}/files/{quote(file_id)}", raw=True, params={"alt": "media"})

    def create(self, metadata: dict, media: bytes | None = None, media_mime: str | None = None) -> dict:
        params = {"fields": FILE_FIELDS}
        if media is None:
            return self._call("POST", f"{DRIVE}/files", params=params, json=metadata)
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps(metadata)}\r\n--{boundary}\r\nContent-Type: {media_mime}\r\n\r\n").encode()
        body += media + f"\r\n--{boundary}--".encode()
        return self._call("POST", f"{UPLOAD}/files", params={**params, "uploadType": "multipart"}, data=body,
                          headers={"Content-Type": f"multipart/related; boundary={boundary}"})

    def update_media(self, file_id: str, media: bytes, mime: str) -> dict:
        return self._call("PATCH", f"{UPLOAD}/files/{quote(file_id)}", data=media,
                          params={"uploadType": "media", "fields": FILE_FIELDS}, headers={"Content-Type": mime})

    def patch(self, file_id: str, body: dict, **params) -> dict:
        return self._call("PATCH", f"{DRIVE}/files/{quote(file_id)}", json=body,
                          params={"fields": FILE_FIELDS, **params})

    def copy(self, file_id: str, body: dict) -> dict:
        return self._call("POST", f"{DRIVE}/files/{quote(file_id)}/copy", json=body, params={"fields": FILE_FIELDS})

    def add_permission(self, file_id: str, body: dict) -> dict:
        return self._call("POST", f"{DRIVE}/files/{quote(file_id)}/permissions", json=body,
                          params={"sendNotificationEmail": "false", "fields": "id,emailAddress,role"})

    def list_permissions(self, file_id: str) -> list[dict]:
        res = self._call("GET", f"{DRIVE}/files/{quote(file_id)}/permissions",
                         params={"fields": "permissions(id,type,emailAddress,role,permissionDetails(inherited))"})
        return res.get("permissions", [])

    def delete_permission(self, file_id: str, permission_id: str) -> None:
        self._call("DELETE", f"{DRIVE}/files/{quote(file_id)}/permissions/{quote(permission_id)}")

    def list_comments(self, file_id: str, pages: int = 5) -> list[dict]:
        out, token = [], None
        for _ in range(pages):
            params = {"fields": f"nextPageToken,comments({COMMENT_FIELDS})", "pageSize": 100}
            if token:
                params["pageToken"] = token
            res = self._call("GET", f"{DRIVE}/files/{quote(file_id)}/comments", params=params)
            out += res.get("comments", [])
            token = res.get("nextPageToken")
            if not token:
                break
        return out

    def create_comment(self, file_id: str, body: dict) -> dict:
        return self._call("POST", f"{DRIVE}/files/{quote(file_id)}/comments", json=body,
                          params={"fields": COMMENT_FIELDS})

    def create_reply(self, file_id: str, comment_id: str, body: dict) -> dict:
        return self._call("POST", f"{DRIVE}/files/{quote(file_id)}/comments/{quote(comment_id)}/replies",
                          json=body, params={"fields": "id,action,content,author(displayName)"})

    # --- Docs ----------------------------------------------------------------------------------
    def docs_get(self, doc_id: str) -> dict:
        return self._call("GET", f"{DOCS}/{quote(doc_id)}")

    def docs_batch_update(self, doc_id: str, requests: list, revision_id: str) -> dict:
        body = {"requests": requests, "writeControl": {"requiredRevisionId": revision_id}}
        return self._call("POST", f"{DOCS}/{quote(doc_id)}:batchUpdate", json=body)

    # --- Sheets --------------------------------------------------------------------------------
    def sheets_get(self, sheet_id: str) -> dict:
        fields = "properties.title,sheets.properties,sheets.protectedRanges(range,description)"
        return self._call("GET", f"{SHEETS}/{quote(sheet_id)}", params={"fields": fields})

    def values_get(self, sheet_id: str, a1: str, render: str = "FORMATTED_VALUE") -> list[list]:
        res = self._call("GET", f"{SHEETS}/{quote(sheet_id)}/values/{quote(a1, safe='')}",
                         params={"valueRenderOption": render})
        return res.get("values", [])

    def values_update(self, sheet_id: str, a1: str, values: list) -> dict:
        return self._call("PUT", f"{SHEETS}/{quote(sheet_id)}/values/{quote(a1, safe='')}",
                          params={"valueInputOption": "USER_ENTERED"}, json={"values": values})

    def values_append(self, sheet_id: str, a1: str, values: list) -> dict:
        return self._call("POST", f"{SHEETS}/{quote(sheet_id)}/values/{quote(a1, safe='')}:append",
                          params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
                          json={"values": values})

    def sheets_batch_update(self, sheet_id: str, requests: list) -> dict:
        return self._call("POST", f"{SHEETS}/{quote(sheet_id)}:batchUpdate", json={"requests": requests})
