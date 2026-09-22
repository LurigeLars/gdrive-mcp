from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from gdrive_mcp import extract
from gdrive_mcp import policy as P
from gdrive_mcp.policy import DOC, FOLDER, SHEET, SHORTCUT, PolicyError
from gdrive_mcp.tools import ImageResult, Tools, markdown_requests, u16

ROOT = Path(__file__).resolve().parents[1]
ME = "svc@example.com"
OWNER = "owner@example.com"


def jpeg(w, h, mode="RGB", fmt="JPEG") -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new(mode, (w, h)).save(buf, fmt)
    return buf.getvalue()


def f(fid, name, mime, parents, owner=OWNER, **extra):
    return {"id": fid, "name": name, "mimeType": mime, "parents": parents,
            "owners": [{"emailAddress": owner}], "modifiedTime": "2026-09-17T10:00:00.000Z", **extra}


class FakeApi:
    """In-memory Drive: CHAT and CLAUDE are shared roots; OTHER lives in the service account's own drive."""

    def __init__(self):
        items = [
            f("CHAT", "ChatGPT", FOLDER, ["JROOT"]),
            f("CLAUDE", "Claudecode", FOLDER, ["JROOT"]),
            f("PROJ", "Projects", FOLDER, ["CHAT"]),
            f("TOOLS", "Tools", FOLDER, ["CHAT"]),
            f("EVID", "evidence", FOLDER, ["TOOLS"]),
            f("PIC", "frame.jpg", "image/jpeg", ["EVID"]),
            f("D1", "Handoff", DOC, ["PROJ"]),
            f("TRACKER", "Work Tracker", SHEET, ["PROJ"]),
            f("T1", "AGENTS.md", "text/markdown", ["PROJ"], size="40"),
            f("MINE", "note.md", "text/markdown", ["PROJ"], owner=ME, size="3"),
            f("SC_IN", "link", SHORTCUT, ["PROJ"], shortcutDetails={"targetId": "D1"}),
            f("SC_OUT", "bad link", SHORTCUT, ["PROJ"], shortcutDetails={"targetId": "OUTFILE"}),
            f("OTHER", "My own folder", FOLDER, ["SVCROOT"], owner=ME),
            f("OUTFILE", "secret.md", "text/markdown", ["OTHER"], owner=ME),
            f("ORPHAN", "orphan.md", "text/markdown", []),
        ]
        self.items = {i["id"]: i for i in items}
        self.calls: list[tuple] = []
        self.listing: list[dict] = []
        self.texts = {"T1": "# Rules\n" + "x" * 30, "MINE": "abc", "OUTFILE": "top secret", "PIC": jpeg(40, 20)}
        self.values: list[list] = [["ITEM_063", "2026-09-17"]]
        self.formulas: list[list] = []
        self.extra_permissions: list[dict] = []
        self.comments: list[dict] = []

    def _rec(self, *call):
        self.calls.append(call)

    def get_file(self, file_id, fields=None):
        self._rec("get_file", file_id)
        return dict(self.items[file_id]) if file_id in self.items else None

    def list_files(self, q, page_token=None, order_by=None, page_size=100):
        self._rec("list_files", q, order_by)
        return {"files": [dict(i) for i in self.listing]}

    def export(self, file_id, mime):
        self._rec("export", file_id, mime)
        return b"# Handoff\nbody"

    def download(self, file_id):
        self._rec("download", file_id)
        data = self.texts[file_id]
        return data if isinstance(data, bytes) else data.encode()

    def create(self, metadata, media=None, media_mime=None):
        self._rec("create", metadata, media, media_mime)
        new = f("NEW", metadata["name"], metadata["mimeType"], metadata["parents"], owner=ME)
        self.items["NEW"] = new
        return dict(new)

    def update_media(self, file_id, media, mime):
        self._rec("update_media", file_id, media, mime)
        return {**self.items[file_id], "modifiedTime": "2026-09-17T11:00:00.000Z"}

    def patch(self, file_id, body, **params):
        self._rec("patch", file_id, body, params)
        item = self.items[file_id]
        if "addParents" in params:
            item["parents"] = [params["addParents"]]
        item.update(body)
        return dict(item)

    def copy(self, file_id, body):
        self._rec("copy", file_id, body)
        new = {**self.items[file_id], "id": "COPY", "parents": body["parents"], "owners": [{"emailAddress": ME}]}
        self.items["COPY"] = new
        return dict(new)

    def add_permission(self, file_id, body):
        self._rec("add_permission", file_id, body)
        return {"id": "p1", "emailAddress": body["emailAddress"], "role": body["role"]}

    def list_permissions(self, file_id):
        inherited = [{"inherited": True}]
        return [{"id": "p0", "emailAddress": OWNER, "role": "owner"},
                {"id": "p1", "emailAddress": ME, "role": "writer"},
                {"id": "p2", "emailAddress": OWNER, "role": "writer", "permissionDetails": inherited},
                *self.extra_permissions]

    def delete_permission(self, file_id, permission_id):
        self._rec("delete_permission", file_id, permission_id)

    def docs_get(self, doc_id):
        return {"title": "Handoff", "revisionId": "rev1", "body": {"content": [
            {"startIndex": 0, "endIndex": 1, "sectionBreak": {}},
            {"startIndex": 1, "endIndex": 10, "paragraph": {
                "elements": [{"textRun": {"content": "Handoff\n"}}],
                "paragraphStyle": {"namedStyleType": "HEADING_1"}}},
        ]}}

    def docs_batch_update(self, doc_id, requests, revision_id):
        self._rec("docs_batch_update", doc_id, requests, revision_id)
        return {"writeControl": {"requiredRevisionId": "rev2"}}

    def list_comments(self, file_id, pages=5):
        self._rec("list_comments", file_id)
        return self.comments

    def create_comment(self, file_id, body):
        self._rec("create_comment", file_id, body)
        return {"id": "c9", **body}

    def create_reply(self, file_id, comment_id, body):
        self._rec("create_reply", file_id, comment_id, body)
        return {"id": "r9", **body}

    def sheets_get(self, sheet_id):
        return {"properties": {"title": "TRACKER"}, "sheets": [{"properties": {
            "title": "Backlog", "sheetId": 7, "gridProperties": {"rowCount": 70, "columnCount": 13}}}]}

    def values_get(self, sheet_id, a1, render="FORMATTED_VALUE"):
        self._rec("values_get", sheet_id, a1, render)
        return self.formulas if render == "FORMULA" and self.formulas else self.values

    def values_update(self, sheet_id, a1, values):
        self._rec("values_update", sheet_id, a1, values)
        return {"updatedRange": a1, "updatedCells": sum(len(r) for r in values)}

    def values_append(self, sheet_id, a1, values):
        self._rec("values_append", sheet_id, a1, values)
        return {"updates": {"updatedRange": "Backlog!A65:M65"}}

    def sheets_batch_update(self, sheet_id, requests):
        self._rec("sheets_batch_update", sheet_id, requests)
        return {"replies": [{} for _ in requests]}


def cfg(**over) -> P.Config:
    base = dict(
        account=ME,
        roots={"CHAT": "ChatGPT", "CLAUDE": "Claudecode"},
        deny=frozenset(),
        share_allowlist=frozenset({OWNER, ME}),
        create_in_root=False,
        sheet_rules=(P.SheetRule("TRACKER", "Backlog", 13, {3: frozenset({"OTHER"}), 5: frozenset({"PARKED"})}),),
    )
    base.update(over)
    return P.Config(**base)


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def tools(api, tmp_path):
    return Tools(api, cfg(), audit_path=tmp_path / "audit.jsonl")


def row(status="PARKED", category="OTHER", n=13):
    r = ["x"] * n
    if n > 5:
        r[3], r[5] = category, status
    return r


# --- boundary ------------------------------------------------------------------------------------
def test_inside_file_has_path(tools):
    assert tools.guard.check("D1")["path"] == "ChatGPT/Projects/Handoff"


@pytest.mark.parametrize("fid", ["OUTFILE", "OTHER", "ORPHAN", "MISSING", "JROOT"])
def test_outside_items_refused(tools, fid):
    with pytest.raises(PolicyError):
        tools.guard.check(fid)


def test_shortcut_target_must_be_inside(tools, api):
    assert tools.guard.check("SC_IN")["path"].endswith("link")
    with pytest.raises(PolicyError):
        tools.guard.check("SC_OUT")
    assert "Handoff" in tools.drive_read("SC_IN")


def test_deny_list_blocks_subtree(api):
    t = Tools(api, cfg(deny=frozenset({"EVID"})))
    with pytest.raises(PolicyError, match="denied"):
        t.guard.check("PIC")
    assert t.guard.check("D1")


def test_folder_chain_is_cached_until_ttl(api):
    now = [0.0]
    t = Tools(api, cfg(), clock=lambda: now[0])
    t.guard.check("D1")
    t.guard.check("D1")
    assert sum(1 for c in api.calls if c == ("get_file", "PROJ")) == 1
    now[0] = P.Guard.FOLDER_TTL + 1
    t.guard.check("D1")
    assert sum(1 for c in api.calls if c == ("get_file", "PROJ")) == 2


def test_file_itself_is_always_fresh(tools, api):
    tools.guard.check("T1")
    api.items["T1"]["parents"] = ["OTHER"]  # moved out by someone else
    with pytest.raises(PolicyError):
        tools.guard.check("T1")


def test_search_and_recent_filter_outside_hits(tools, api):
    api.listing = [api.items["OUTFILE"], api.items["D1"], api.items["ORPHAN"], api.items["T1"]]
    found = json.loads(tools.drive_search("x"))["files"]
    assert [x["id"] for x in found] == ["D1", "T1"]
    assert [x["id"] for x in json.loads(tools.drive_recent(1))["files"]] == ["D1"]


def test_search_query_is_escaped(tools, api):
    tools.drive_search("it's")
    q = [c for c in api.calls if c[0] == "list_files"][0][1]
    assert "it\\'s" in q and "trashed = false" in q


def test_search_in_outside_folder_refused(tools):
    with pytest.raises(PolicyError):
        tools.drive_search("x", folder_id="OTHER")


# --- reading -------------------------------------------------------------------------------------
def test_read_text_is_fenced_and_sliced(tools):
    out = tools.drive_read("T1", offset=0, max_chars=5)
    assert "[Untrusted file content" in out
    assert 'nextOffset="5"' in out and "# Rul" in out


def test_read_google_doc_exports_markdown(tools, api):
    tools.drive_read("D1")
    assert ("export", "D1", "text/markdown") in api.calls


def test_read_sheet_refers_to_sheets_tools(tools, api):
    with pytest.raises(PolicyError, match="sheets_read"):
        tools.drive_read("TRACKER")
    assert not [c for c in api.calls if c[0] == "download"]


def test_read_image_returns_image(tools):
    out = tools.drive_read("PIC")
    assert isinstance(out, ImageResult) and out.format == "jpeg" and "untrusted" in out.caption


def test_read_oversized_image_refused(tools, api):
    api.items["PIC"]["size"] = str(50 * 1024 * 1024)
    with pytest.raises(PolicyError, match="larger"):
        tools.drive_read("PIC")


def test_read_office_file_extracts_text(tools, api):
    import docx

    d = docx.Document()
    d.add_paragraph("Hej från Word")
    buf = io.BytesIO()
    d.save(buf)
    api.items["W1"] = f("W1", "a.docx", extract.DOCX, ["PROJ"], size=str(len(buf.getvalue())))
    api.texts["W1"] = buf.getvalue()
    assert "Hej från Word" in tools.drive_read("W1")


def test_corrupt_office_file_gives_policy_error(tools, api):
    api.items["BAD"] = f("BAD", "bad.pdf", extract.PDF, ["PROJ"], size="4")
    api.texts["BAD"] = b"nope"
    with pytest.raises(PolicyError, match="could not extract"):
        tools.drive_read("BAD")


# --- creating and changing files -----------------------------------------------------------------
def test_create_not_directly_in_root(tools):
    with pytest.raises(PolicyError, match="subfolder"):
        tools.drive_create("CHAT", "x", "doc")


def test_create_outside_refused(tools):
    with pytest.raises(PolicyError):
        tools.drive_create("OTHER", "x", "doc")


def test_create_doc_with_markdown(tools, api):
    out = json.loads(tools.drive_create("PROJ", "Plan", "doc", "# Hi"))
    assert out["path"] == "ChatGPT/Projects/Plan"
    call = [c for c in api.calls if c[0] == "create"][0]
    assert call[1]["mimeType"] == DOC and call[3] == "text/markdown"


def test_create_folder_without_content(tools, api):
    tools.drive_create("PROJ", "New folder", "folder")
    assert [c for c in api.calls if c[0] == "create"][0][1]["mimeType"] == FOLDER
    with pytest.raises(PolicyError, match="created empty"):
        tools.drive_create("PROJ", "x", "folder", "content")


def test_create_text_needs_text_extension(tools):
    with pytest.raises(PolicyError):
        tools.drive_create("PROJ", "tool.exe", "text", "x")


def test_update_text_requires_same_modified_time(tools, api):
    with pytest.raises(PolicyError, match="changed"):
        tools.drive_update_text("T1", "new", "2020-01-01T00:00:00.000Z")
    out = json.loads(tools.drive_update_text("T1", "new", "2026-09-17T10:00:00.000Z"))
    assert out["modifiedTime"] == "2026-09-17T11:00:00.000Z"


def test_update_text_refuses_google_files(tools):
    with pytest.raises(PolicyError, match="plain text"):
        tools.drive_update_text("D1", "x", "2026-09-17T10:00:00.000Z")


def test_move_only_within_roots(tools, api):
    with pytest.raises(PolicyError):
        tools.drive_move("D1", "OTHER")
    with pytest.raises(PolicyError, match="root"):
        tools.drive_move("CHAT", "PROJ")
    with pytest.raises(PolicyError):
        tools.drive_move("OUTFILE", "PROJ")  # pulling outside files in is refused too
    tools.drive_move("D1", "TOOLS")
    patch = [c for c in api.calls if c[0] == "patch"][0]
    assert patch[3] == {"addParents": "TOOLS", "removeParents": "PROJ"}


def test_copy_only_within_roots(tools):
    with pytest.raises(PolicyError):
        tools.drive_copy("D1", "OTHER")
    with pytest.raises(PolicyError, match="only files"):
        tools.drive_copy("PROJ", "TOOLS")
    assert json.loads(tools.drive_copy("D1", "TOOLS"))["path"] == "ChatGPT/Tools/Handoff"


def test_trash_only_own_files(tools, api):
    with pytest.raises(PolicyError, match="only the owner"):
        tools.drive_trash("D1")
    assert json.loads(tools.drive_trash("MINE"))["trashed"] is True
    with pytest.raises(PolicyError, match="root"):
        tools.drive_trash("CHAT")


def test_rename_root_refused(tools):
    with pytest.raises(PolicyError):
        tools.drive_rename("CLAUDE", "x")


# --- sharing -------------------------------------------------------------------------------------
def test_share_only_to_allowlist(tools, api):
    with pytest.raises(PolicyError, match="only allowed"):
        tools.drive_share("D1", "stranger@example.com", "reader")
    with pytest.raises(PolicyError, match="role"):
        tools.drive_share("D1", OWNER, "owner")
    tools.drive_share("D1", OWNER.upper(), "writer")
    perm = [c for c in api.calls if c[0] == "add_permission"][0][2]
    assert perm == {"type": "user", "role": "writer", "emailAddress": OWNER}


def test_share_outside_file_refused(tools):
    with pytest.raises(PolicyError):
        tools.drive_share("OUTFILE", OWNER)


def test_unshare_rules(tools, api):
    with pytest.raises(PolicyError, match="own account"):
        tools.drive_unshare("D1", ME)
    out = json.loads(tools.drive_unshare("D1", OWNER))
    assert out["removed"] == 0  # owner and inherited permissions are never touched
    assert not [c for c in api.calls if c[0] == "delete_permission"]
    api.extra_permissions = [{"id": "p3", "emailAddress": OWNER, "role": "reader",
                              "permissionDetails": [{"inherited": False}]}]
    assert json.loads(tools.drive_unshare("D1", OWNER))["removed"] == 1
    assert ("delete_permission", "D1", "p3") in api.calls


# --- Docs ----------------------------------------------------------------------------------------
def test_docs_get_shows_indices(tools):
    out = tools.docs_get("D1")
    assert "[1-10] H1 Handoff" in out and 'revisionId="rev1"' in out


def test_docs_edit_allowlist_and_revision(tools, api):
    with pytest.raises(PolicyError, match="not allowed"):
        tools.docs_edit("D1", [{"createNamedRange": {}}], "rev1")
    with pytest.raises(PolicyError, match="exactly one"):
        tools.docs_edit("D1", [{"insertText": {}, "deleteContentRange": {}}], "rev1")
    with pytest.raises(PolicyError, match="required_revision_id"):
        tools.docs_edit("D1", [{"insertText": {}}], "")
    with pytest.raises(PolicyError, match="not a Google Doc"):
        tools.docs_edit("TRACKER", [{"insertText": {}}], "rev1")
    out = json.loads(tools.docs_edit("D1", [{"insertText": {"location": {"index": 1}, "text": "a"}}], "rev1"))
    assert out["revisionId"] == "rev2"
    assert [c for c in api.calls if c[0] == "docs_batch_update"][0][3] == "rev1"


def test_markdown_requests_indices():
    reqs = markdown_requests(["# Title", "- **bold** item", "plain 😀"], end_index=10)
    insert = reqs[0]["insertText"]
    assert insert["location"]["index"] == 9
    assert insert["text"] == "\nTitle\nbold item\nplain 😀"
    styles = [r["updateParagraphStyle"] for r in reqs if "updateParagraphStyle" in r]
    assert [s["range"]["startIndex"] for s in styles] == [10, 16, 26]
    assert styles[0]["paragraphStyle"]["namedStyleType"] == "HEADING_1"
    bullets = [r["createParagraphBullets"]["range"] for r in reqs if "createParagraphBullets" in r]
    assert bullets == [{"startIndex": 16, "endIndex": 25}]
    bold = [r["updateTextStyle"]["range"] for r in reqs if "updateTextStyle" in r]
    assert bold == [{"startIndex": 16, "endIndex": 20}]
    assert styles[2]["range"]["endIndex"] == 26 + u16("plain 😀")  # emoji counts as 2 units


def test_markdown_requests_empty_doc_has_no_leading_newline():
    reqs = markdown_requests(["hello"], end_index=2)
    assert reqs[0]["insertText"] == {"location": {"index": 1}, "text": "hello"}


def test_append_markdown_uses_current_revision(tools, api):
    tools.docs_append_markdown("D1", "- a")
    call = [c for c in api.calls if c[0] == "docs_batch_update"][0]
    assert call[3] == "rev1"
    assert all(next(iter(r)) in P.DOC_REQUESTS for r in call[2])


# --- Sheets --------------------------------------------------------------------------------------
def test_backlog_append_rules(tools, api):
    with pytest.raises(PolicyError, match="13 columns"):
        tools.sheets_append("TRACKER", "Backlog!A:M", [row(n=12)])
    with pytest.raises(PolicyError, match="column 6"):
        tools.sheets_append("TRACKER", "Backlog!A:M", [row(status="PLANNED")])
    with pytest.raises(PolicyError, match="tab name"):
        tools.sheets_append("TRACKER", "A:M", [row()])
    out = json.loads(tools.sheets_append("TRACKER", "Backlog!A:M", [row()]))
    assert out["updatedRange"] == "Backlog!A65:M65" and out["readback"]


def test_backlog_update_checks_ruled_columns_by_position(tools):
    with pytest.raises(PolicyError, match="column 6"):
        tools.sheets_write("TRACKER", "Backlog!F64", [["PLANNED"]])
    tools.sheets_write("TRACKER", "Backlog!F64", [["PARKED"]])
    tools.sheets_write("TRACKER", "Backlog!G64", [["anything"]])
    tools.sheets_write("TRACKER", "Backlog!D64:F64", [["OTHER", "x", None]])  # None leaves F unchanged


def test_formulas_refused_by_default(tools):
    with pytest.raises(PolicyError, match="formula"):
        tools.sheets_write("TRACKER", "State!A1", [["=IMPORTXML(1)"]])


def test_formulas_allowed_by_rule(api):
    rule = P.SheetRule("TRACKER", "State", None, {}, allow_formulas=True)
    Tools(api, cfg(sheet_rules=(rule,))).sheets_write("TRACKER", "State!A1", [["=1+1"]])


def test_cell_limits_and_types(tools):
    with pytest.raises(PolicyError, match="cells"):
        tools.sheets_write("TRACKER", "State!A1", [["x"] * (P.MAX_CELLS + 1)])
    with pytest.raises(PolicyError, match="type"):
        tools.sheets_write("TRACKER", "State!A1", [[{"a": 1}]])
    with pytest.raises(PolicyError, match="list of rows"):
        tools.sheets_write("TRACKER", "State!A1", ["x"])


def test_sheets_edit_rules(tools):
    delete = {"deleteDimension": {"range": {"sheetId": 7, "dimension": "ROWS", "startIndex": 1, "endIndex": 3}}}
    with pytest.raises(PolicyError, match="confirm"):
        tools.sheets_edit("TRACKER", [delete])
    big = {"deleteDimension": {"range": {"startIndex": 0, "endIndex": P.MAX_DELETE_DIMENSION + 1}}}
    with pytest.raises(PolicyError, match="1-"):
        tools.sheets_edit("TRACKER", [big], confirm=True)
    with pytest.raises(PolicyError, match="deleteSheet.*confirm"):
        tools.sheets_edit("TRACKER", [{"deleteSheet": {"sheetId": 7}}])
    assert json.loads(tools.sheets_edit("TRACKER", [{"deleteSheet": {"sheetId": 7}}], confirm=True))["applied"] == 1
    with pytest.raises(PolicyError, match="not allowed"):
        tools.sheets_edit("TRACKER", [{"addChart": {}}], confirm=True)
    assert json.loads(tools.sheets_edit("TRACKER", [delete], confirm=True))["applied"] == 1


def test_sheets_read_is_tsv_and_fenced(tools, api):
    out = tools.sheets_read("TRACKER", "Backlog!A64:B64", max_cell_chars=4)
    assert "ITEM\t2026" in out and "[Untrusted" in out


@pytest.mark.parametrize("a1,expected", [
    ("Backlog!A64:M64", ("Backlog", 0)),
    ("'My tab'!C3", ("My tab", 2)),
    ("'It''s'!AA1", ("It's", 26)),
    ("Backlog", ("Backlog", 0)),
    ("B2:C3", (None, 1)),
    ("Backlog!F:F", ("Backlog", 5)),
])
def test_split_a1(a1, expected):
    assert P.split_a1(a1) == expected


# --- misc ----------------------------------------------------------------------------------------
def test_body_size_limit(tools):
    with pytest.raises(PolicyError, match="KB"):
        tools.drive_create("PROJ", "big.md", "text", "x" * (P.MAX_BODY_BYTES + 1))


def test_writes_are_audited(tools, tmp_path):
    tools.drive_rename("T1", "RULES.md")
    rec = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert rec["tool"] == "drive_rename" and rec["file_id"] == "T1"


def test_api_retries_quota_and_dropped_connections(monkeypatch):
    from gdrive_mcp.api import GoogleApi

    class Resp:
        def __init__(self, status, payload=b'{"ok": true}'):
            self.status_code, self.content, self.headers = status, payload, {}
            self.text = payload.decode()

        def json(self):
            if self.status_code >= 400:
                return {"error": {"message": f"HTTP {self.status_code}"}}
            return {"ok": True}

    class Session:
        def __init__(self, script):
            self.script, self.calls = list(script), 0

        def request(self, *a, **kw):
            self.calls += 1
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    monkeypatch.setattr("gdrive_mcp.api.time.sleep", lambda *_: None)
    s = Session([Resp(429), ConnectionError("dropped"), Resp(200)])
    assert GoogleApi(s)._call("GET", "http://x")["ok"] is True
    assert s.calls == 3

    s = Session([Resp(429), Resp(429), Resp(429)])
    with pytest.raises(Exception, match="429"):
        GoogleApi(s)._call("GET", "http://x")


def test_example_config_loads():
    c = P.Config.load(ROOT / "config.example.toml")
    assert set(c.roots.values()) == {"ChatGPT", "Claudecode"}
    assert "owner@example.com" in c.share_allowlist
    backlog = c.sheet_rules[0]
    assert backlog.columns == 3 and "TODO" in backlog.allowed[1]


class TableApi(FakeApi):
    """docs_get returns the table only after it has been inserted, like the real API."""

    def __init__(self):
        super().__init__()
        self.inserted = False

    def docs_get(self, doc_id):
        doc = super().docs_get(doc_id)
        if not self.inserted:
            return doc
        cells = [[{"content": [{"startIndex": 12 + (r * 2 + c) * 2}]} for c in range(2)] for r in range(2)]
        doc["body"]["content"].append({"startIndex": 10, "endIndex": 30, "table": {
            "rows": 2, "columns": 2, "tableRows": [{"tableCells": row} for row in cells]}})
        doc["revisionId"] = "rev2"
        return doc

    def docs_batch_update(self, doc_id, requests, revision_id):
        if any("insertTable" in r for r in requests):
            self.inserted = True
        return super().docs_batch_update(doc_id, requests, revision_id)


def test_insert_table_fills_cells_from_the_end(tmp_path):
    api = TableApi()
    t = Tools(api, cfg())
    out = t.docs_insert_table("D1", [["A", "B"], ["C", ""]])
    calls = [c for c in api.calls if c[0] == "docs_batch_update"]
    assert calls[0][2] == [{"insertTable": {"rows": 2, "columns": 2, "location": {"index": 9}}}]
    fills = calls[1][2]
    idx = [r["insertText"]["location"]["index"] for r in fills]
    assert idx == sorted(idx, reverse=True)  # later cells first, so earlier inserts do not shift them
    assert [r["insertText"]["text"] for r in fills] == ["C", "B", "A"]  # the empty cell is skipped
    assert calls[1][3] == "rev2"  # the revision from after the insert, not the stale one
    assert json.loads(out)["rows"] == 2


def test_insert_table_rejects_a_ragged_grid(tools):
    with pytest.raises(PolicyError, match="same number of cells"):
        tools.docs_insert_table("D1", [["A", "B"], ["C"]])
    with pytest.raises(PolicyError, match="non-empty list of lists"):
        tools.docs_insert_table("D1", [])


def test_comments_list_open_threads_as_untrusted_content(tools, api):
    api.comments = [
        {"id": "c1", "author": {"displayName": "Owner"}, "createdTime": "2026-09-18T07:00:00Z",
         "content": "Check this", "quotedFileContent": {"value": "the number"},
         "replies": [{"author": {"displayName": "ChatGPT"}, "content": "Fixed", "action": "resolve"}]},
        {"id": "c2", "author": {"displayName": "Owner"}, "content": "old", "resolved": True},
    ]
    out = tools.drive_comments("D1")
    assert "[c1] Owner" in out and 'on "the number"' in out and "-> ChatGPT (resolve): Fixed" in out
    assert "old" not in out and 'threads="1"' in out  # resolved threads are hidden by default
    assert "Untrusted file content" in out
    assert "old" in tools.drive_comments("D1", include_resolved=True)


def test_comment_replies_and_resolves(tools, api):
    json.loads(tools.drive_comment("D1", "A note"))
    assert [c for c in api.calls if c[0] == "create_comment"][0][2] == {"content": "A note"}
    assert json.loads(tools.drive_comment("D1", "Done", reply_to="c1", resolve=True))["resolved"] is True
    assert [c for c in api.calls if c[0] == "create_reply"][0][3] == {"content": "Done", "action": "resolve"}
    with pytest.raises(PolicyError, match="resolve needs reply_to"):
        tools.drive_comment("D1", "Done", resolve=True)
    with pytest.raises(PolicyError, match="empty"):
        tools.drive_comment("D1", "   ")


def test_comments_stay_inside_the_roots(tools):
    with pytest.raises(PolicyError):
        tools.drive_comments("OUTFILE")
    with pytest.raises(PolicyError):
        tools.drive_comment("OUTFILE", "hello")


def test_sheets_read_can_show_formulas(tools, api):
    api.formulas = [["=SUM(B:B)", "17"]]
    assert "=SUM(B:B)" in tools.sheets_read("TRACKER", "Backlog!A1:B1", formulas=True)
    assert [c for c in api.calls if c[0] == "values_get"][-1][3] == "FORMULA"
    assert "=SUM(B:B)" not in tools.sheets_read("TRACKER", "Backlog!A1:B1")  # computed values by default


def test_a_formula_needs_the_caller_to_ask_for_it():
    """A stray '=...' string must not silently become a formula, but the agent can request one."""
    open_sheet = cfg(sheet_rules=(P.SheetRule("TRACKER", "Backlog", None, {}),))
    formula = [["=A1+1"]]
    with pytest.raises(PolicyError, match="pass formulas=true"):
        Tools(FakeApi(), open_sheet).sheets_write("TRACKER", "Backlog!A1", formula)
    assert json.loads(Tools(FakeApi(), open_sheet).sheets_write("TRACKER", "Backlog!A1", formula, formulas=True))["path"]
    assert json.loads(Tools(FakeApi(), open_sheet).sheets_append("TRACKER", "Backlog!A:A", formula, formulas=True))["path"]
    locked = cfg(sheet_rules=(P.SheetRule("TRACKER", "Backlog", None, {}, False),))
    with pytest.raises(PolicyError, match="does not allow formulas"):
        Tools(FakeApi(), locked).sheets_write("TRACKER", "Backlog!A1", formula, formulas=True)
