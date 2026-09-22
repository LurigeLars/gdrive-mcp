"""Live smoke test against the configured Drive.

Run manually with a writable subfolder that is already inside one configured root:
    uv run python scripts/live_smoke.py --folder-id YOUR_TEST_FOLDER_ID

Optional flags:
    --share-email EMAIL   exercise share/unshare (must be in config share_allowlist)
    --outside-id ID       verify that a known outside-root item is refused

The script contains no deployment-specific Drive IDs or email addresses.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

sys.path.insert(0, ".")
from gdrive_mcp.server import tools as load_tools  # noqa: E402
from gdrive_mcp.tools import ImageResult  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder-id", required=True, help="Writable subfolder inside a configured root")
    parser.add_argument("--share-email", help="Optional allowlisted email for share/unshare validation")
    parser.add_argument("--outside-id", help="Optional known item outside all configured roots")
    return parser.parse_args()


args = parse_args()
t = load_tools()
failures = 0
root_id = next(iter(t.guard.cfg.roots))
root_name = t.guard.cfg.roots[root_id]


def short(out) -> str:
    if isinstance(out, ImageResult):
        return f"<image {out.format} {len(out.data)} bytes>"
    s = str(out).replace("\n", " ")
    return s[:160] + ("…" if len(s) > 160 else "")


def step(name, fn, expect_error=False):
    global failures
    try:
        out = fn()
    except Exception as exc:  # noqa: BLE001 - smoke test reports every failure
        ok = expect_error
        print(f"{'PASS' if ok else 'FAIL'}  {name}  -> {type(exc).__name__}: {str(exc)[:160]}")
        failures += not ok
        return None
    ok = not expect_error
    print(f"{'PASS' if ok else 'FAIL'}  {name}  -> {short(out)}")
    failures += not ok
    return out


def first_of(mime: str) -> str | None:
    res = t.api.list_files(f"mimeType = '{mime}' and trashed = false", page_token=None)
    return next((f["id"] for f in res.get("files", []) if t.guard.inside(f)), None)


# Fail early if the requested write folder is not a writable subfolder under a configured root.
step("validate test folder", lambda: t.guard.check_folder(args.folder_id, for_new_item=True))

print("== read ==")
step(f"list root {root_name}", lambda: t.drive_list(root_id))
step("recent", lambda: t.drive_recent(3))
hits = step("search markdown", lambda: json.loads(t.drive_search(".md", max_results=5)))
md = next((h["id"] for h in (hits or {}).get("files", []) if h["name"].endswith(".md")), None)
if md:
    step("read a .md", lambda: t.drive_read(md, max_chars=200))
else:
    print("SKIP  read a .md  (no markdown hit inside the roots)")

doc_id = first_of("application/vnd.google-apps.document")
if doc_id:
    step("read Google Doc as markdown", lambda: t.drive_read(doc_id, max_chars=200))
    step("docs_get", lambda: t.docs_get(doc_id))
else:
    print("SKIP  Google Doc reads  (no document inside the roots)")

sheet_id = first_of("application/vnd.google-apps.spreadsheet")
if sheet_id:
    step("sheets_get", lambda: t.sheets_get(sheet_id))
else:
    print("SKIP  sheets_get  (no spreadsheet inside the roots)")

for label, mime in [
    ("pdf", "application/pdf"),
    ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("slides", "application/vnd.google-apps.presentation"),
    ("jpeg", "image/jpeg"),
    ("png", "image/png"),
]:
    fid = first_of(mime)
    if fid:
        step(f"read {label}", lambda fid=fid: t.drive_read(fid, max_chars=200))
    else:
        print(f"SKIP  read {label}  (no such file inside the roots)")

print("== boundary ==")
if args.outside_id:
    step("known outside-root item refused", lambda: t.drive_get(args.outside_id), expect_error=True)
else:
    print("SKIP  outside-root negative test  (--outside-id not supplied)")
if not t.guard.cfg.create_in_root:
    step("create directly in root refused", lambda: t.drive_create(root_id, "x", "doc"), expect_error=True)
else:
    print("SKIP  root-create refusal  (create_in_root=true)")
if doc_id:
    step("share to unlisted address refused", lambda: t.drive_share(doc_id, "not-allowed@example.invalid"), expect_error=True)

print("== write cycle ==")
stamp = time.strftime("%Y%m%d-%H%M%S")
created: list[str] = []


def new(kind, name, content=None):
    out = step(f"create {kind}", lambda: json.loads(t.drive_create(args.folder_id, name, kind, content)))
    if out:
        created.append(out["id"])
        return out["id"]
    return None


doc = new("doc", f"gdrive-mcp live test {stamp}", "# Test\n- item **bold**")
if doc:
    step("docs_append_markdown", lambda: t.docs_append_markdown(doc, "## Added\n- two\nplain 😀 **bold**"))
    got = step("docs_get after append", lambda: t.docs_get(doc))
    rev = got.split('revisionId="')[1].split('"')[0] if got else ""
    step("docs_edit replaceAllText", lambda: t.docs_edit(doc, [{"replaceAllText": {
        "containsText": {"text": "two", "matchCase": True}, "replaceText": "TWO"}}], rev))
    step("docs_edit with stale revision refused", lambda: t.docs_edit(doc, [{"insertText": {
        "location": {"index": 1}, "text": "x"}}], rev), expect_error=True)
    text = step("doc reads back edited", lambda: t.drive_read(doc))
    if text and not all(s in text for s in ("TWO", "Added", "😀")):
        print("FAIL  doc content check")
        failures += 1

sheet = new("sheet", f"gdrive-mcp live sheet {stamp}", "a,b\n1,2")
if sheet:
    tab = json.loads(t.sheets_get(sheet))["tabs"][0]
    title, sid = tab["title"], tab["sheetId"]
    step("sheets_write", lambda: t.sheets_write(sheet, f"'{title}'!A3", [["x", 3]]))
    step("sheets_append", lambda: t.sheets_append(sheet, f"'{title}'!A:B", [["y", 4]]))
    step("sheets_edit insert row", lambda: t.sheets_edit(sheet, [{"insertDimension": {
        "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 1, "endIndex": 2}}}]))
    step("sheets_edit delete row", lambda: t.sheets_edit(sheet, [{"deleteDimension": {
        "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 1, "endIndex": 2}}}], confirm=True))
    step("formula refused", lambda: t.sheets_write(sheet, f"'{title}'!C1", [["=1+1"]]), expect_error=True)
    step("sheets_read", lambda: t.sheets_read(sheet, f"'{title}'!A1:B5"))

folder = new("folder", f"gdrive-mcp live folder {stamp}")
txt = new("text", f"gdrive-mcp-live-{stamp}.md", "first")
if txt and folder:
    body = t.drive_read(txt)
    mtime = body.split('modifiedTime="')[1].split('"')[0]
    step("update_text with wrong time refused", lambda: t.drive_update_text(txt, "x", "2000-01-01T00:00:00.000Z"),
         expect_error=True)
    step("update_text", lambda: t.drive_update_text(txt, "second", mtime))
    step("text reads back", lambda: t.drive_read(txt))
    step("rename", lambda: t.drive_rename(txt, f"gdrive-mcp-live-{stamp}-renamed.md"))
    copy = step("copy", lambda: json.loads(t.drive_copy(txt, args.folder_id, f"gdrive-mcp-live-{stamp}-copy.md")))
    if copy:
        created.append(copy["id"])
        step("move copy into test folder", lambda: t.drive_move(copy["id"], folder))
    if args.share_email:
        step("share allowlisted address", lambda: t.drive_share(txt, args.share_email, "reader"))
        step("unshare allowlisted address", lambda: t.drive_unshare(txt, args.share_email))
    else:
        print("SKIP  share/unshare  (--share-email not supplied)")

print("== cleanup ==")
for fid in reversed(created):
    step(f"trash {fid[:8]}", lambda fid=fid: t.drive_trash(fid))

print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
sys.exit(1 if failures else 0)
