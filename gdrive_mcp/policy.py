"""Access boundary and write rules. No network: the Google client is passed in."""
from __future__ import annotations

import json
import re
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"

MAX_BODY_BYTES = 256 * 1024
MAX_CELLS = 5000
MAX_DOC_REQUESTS = 200
MAX_SHEET_REQUESTS = 100
MAX_DELETE_DIMENSION = 50
MAX_TABLE_ROWS, MAX_TABLE_COLUMNS = 100, 20
SHARE_ROLES = {"reader", "commenter", "writer"}

DOC_REQUESTS = frozenset({
    "insertText", "deleteContentRange", "replaceAllText", "updateTextStyle", "updateParagraphStyle",
    "createParagraphBullets", "deleteParagraphBullets", "insertTable", "insertTableRow",
    "insertTableColumn", "deleteTableRow", "deleteTableColumn", "insertPageBreak",
})
SHEET_REQUESTS = frozenset({
    "addSheet", "updateSheetProperties", "insertDimension", "deleteDimension", "appendDimension",
    "updateDimensionProperties", "repeatCell", "updateCells", "sortRange", "autoResizeDimensions",
    "mergeCells", "unmergeCells", "updateBorders", "deleteSheet", "duplicateSheet",
})


class PolicyError(Exception):
    """A request refused by policy. The message is shown to the agent."""


@dataclass(frozen=True)
class SheetRule:
    file_id: str
    sheet: str | None            # None = every tab of the file
    columns: int | None          # exact width of appended rows
    allowed: dict[int, frozenset[str]]
    allow_formulas: bool | None = None  # true: always allowed here; false: never; unset: the call decides


@dataclass(frozen=True)
class Config:
    account: str
    roots: dict[str, str]        # folder id -> name
    deny: frozenset[str]
    share_allowlist: frozenset[str]
    create_in_root: bool
    sheet_rules: tuple[SheetRule, ...]

    @classmethod
    def load(cls, path: str | Path) -> Config:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        pol = raw.get("policy", {})
        rules = tuple(
            SheetRule(
                file_id=r["file_id"],
                sheet=r.get("sheet"),
                columns=r.get("columns"),
                allowed={int(k): frozenset(v) for k, v in r.get("allowed", {}).items()},
                allow_formulas=r.get("allow_formulas"),
            )
            for r in raw.get("write_rules", [])
        )
        roots = {r["id"]: r["name"] for r in raw["roots"]}
        if not roots:
            raise ValueError("config needs at least one root")
        return cls(
            account=raw["account"].lower(),
            roots=roots,
            deny=frozenset(pol.get("deny", [])),
            share_allowlist=frozenset(e.lower() for e in pol.get("share_allowlist", [])),
            create_in_root=pol.get("create_in_root", False),
            sheet_rules=rules,
        )


class Guard:
    """Decides whether a Drive item lies inside an allowed root.

    The item's own metadata is always fresh; ancestor folders are cached for FOLDER_TTL seconds.
    """

    FOLDER_TTL = 600  # ponytail: a folder moved out of a root stays allowed this long; lower it if folders move often
    MAX_DEPTH = 64

    def __init__(self, api, config: Config, clock=time.monotonic):
        self.api = api
        self.cfg = config
        self.clock = clock
        self._folders: dict[str, tuple[float, list[str], str]] = {}

    def check(self, file_id: str, meta: dict | None = None) -> dict:
        """Metadata of an allowed item (with `path`), or PolicyError."""
        meta = meta if meta is not None else self.api.get_file(file_id)
        if meta is None:
            raise PolicyError(f"{file_id}: not found or not accessible")
        path = self._path(meta)
        if meta.get("mimeType") == SHORTCUT:
            target = (meta.get("shortcutDetails") or {}).get("targetId")
            if not target:
                raise PolicyError(f"{file_id}: shortcut without target")
            self.check(target)  # the target must be inside a root too
        return {**meta, "path": "/".join(path)}

    def inside(self, meta: dict) -> dict | None:
        try:
            return self.check(meta["id"], meta)
        except PolicyError:
            return None

    def check_folder(self, folder_id: str, *, for_new_item: bool) -> dict:
        meta = self.check(folder_id)
        if meta.get("mimeType") != FOLDER:
            raise PolicyError(f"{folder_id}: not a folder")
        if for_new_item and folder_id in self.cfg.roots and not self.cfg.create_in_root:
            raise PolicyError(f"{meta['path']}: do not put files directly in a root folder; use a subfolder")
        return meta

    def _path(self, meta: dict) -> list[str]:
        fid = meta["id"]
        if fid in self.cfg.deny:
            raise PolicyError(f"{fid}: denied by policy")
        if fid in self.cfg.roots:
            return [self.cfg.roots[fid]]
        parents = meta.get("parents") or []
        if not parents:
            raise PolicyError(f"{fid}: outside the allowed folders")
        # Drive items have one parent today; require every listed parent to be inside anyway.
        paths = [self._folder_path(p, 0) for p in parents]
        return paths[0] + [meta.get("name", fid)]

    def _folder_path(self, folder_id: str, depth: int) -> list[str]:
        if folder_id in self.cfg.deny:
            raise PolicyError(f"{folder_id}: denied by policy")
        if folder_id in self.cfg.roots:
            return [self.cfg.roots[folder_id]]
        if depth > self.MAX_DEPTH:
            raise PolicyError(f"{folder_id}: folder chain too deep")
        cached = self._folders.get(folder_id)
        if cached and self.clock() - cached[0] < self.FOLDER_TTL:
            _, parents, name = cached
        else:
            meta = self.api.get_file(folder_id)
            if meta is None:  # an ancestor we cannot see is never inside a shared root
                raise PolicyError(f"{folder_id}: outside the allowed folders")
            parents, name = meta.get("parents") or [], meta.get("name", folder_id)
            self._folders[folder_id] = (self.clock(), parents, name)
        if not parents:
            raise PolicyError(f"{folder_id}: outside the allowed folders")
        return self._folder_path(parents[0], depth + 1) + [name]


def check_share(cfg: Config, email: str, role: str | None = None) -> str:
    email = email.strip().lower()
    if email not in cfg.share_allowlist:
        raise PolicyError(f"sharing is only allowed with: {', '.join(sorted(cfg.share_allowlist))}")
    if role is not None and role not in SHARE_ROLES:
        raise PolicyError(f"role must be one of {sorted(SHARE_ROLES)}")
    return email


def check_body_size(obj) -> None:
    if len(json.dumps(obj, ensure_ascii=False).encode()) > MAX_BODY_BYTES:
        raise PolicyError(f"request larger than {MAX_BODY_BYTES // 1024} KB; split it")


def check_requests(requests: list, allowed: frozenset[str], max_n: int) -> None:
    if not isinstance(requests, list) or not requests:
        raise PolicyError("requests must be a non-empty list")
    if len(requests) > max_n:
        raise PolicyError(f"at most {max_n} requests per call")
    for i, req in enumerate(requests):
        if not isinstance(req, dict) or len(req) != 1:
            raise PolicyError(f"request {i}: must be an object with exactly one request type")
        kind = next(iter(req))
        if kind not in allowed:
            raise PolicyError(f"request {i}: '{kind}' is not allowed; allowed: {', '.join(sorted(allowed))}")
    check_body_size(requests)


def check_sheet_requests(requests: list, confirm: bool) -> None:
    check_requests(requests, SHEET_REQUESTS, MAX_SHEET_REQUESTS)
    for i, req in enumerate(requests):
        if "deleteSheet" in req and not confirm:
            raise PolicyError(f"request {i}: deleteSheet removes a whole tab and needs confirm=true")
        if "deleteDimension" in req:
            rng = req["deleteDimension"].get("range", {})
            count = int(rng.get("endIndex", 0)) - int(rng.get("startIndex", 0))
            if not confirm:
                raise PolicyError(f"request {i}: deleteDimension needs confirm=true")
            if not 0 < count <= MAX_DELETE_DIMENSION:
                raise PolicyError(f"request {i}: deleteDimension may remove 1-{MAX_DELETE_DIMENSION} rows/columns")


def check_table(rows) -> int:
    """A rectangular, bounded grid of strings; returns the cell count."""
    if not isinstance(rows, list) or not rows or not all(isinstance(r, list) for r in rows):
        raise PolicyError("rows must be a non-empty list of lists")
    width = len(rows[0])
    if not 0 < width <= MAX_TABLE_COLUMNS:
        raise PolicyError(f"1-{MAX_TABLE_COLUMNS} columns per table")
    if len(rows) > MAX_TABLE_ROWS:
        raise PolicyError(f"at most {MAX_TABLE_ROWS} rows per table")
    if any(len(r) != width for r in rows):
        raise PolicyError("every row must have the same number of cells")
    check_body_size("".join(str(c) for r in rows for c in r))
    return len(rows) * width


_CELLS = re.compile(r"^\$?([A-Za-z]{1,3})\$?\d*(:\$?[A-Za-z]{0,3}\$?\d*)?$")


def split_a1(a1: str) -> tuple[str | None, int]:
    """(sheet name or None, 0-based start column) of an A1 range."""
    sheet, bang, cells = a1.rpartition("!")
    m = _CELLS.match(cells)
    if not bang and not m:  # a bare tab name such as "Backlog" or "'My tab'"
        sheet, cells, m = cells, "", None
    if sheet.startswith("'") and sheet.endswith("'") and len(sheet) > 1:
        sheet = sheet[1:-1].replace("''", "'")
    col = 0
    if m:
        for ch in m.group(1).upper():
            col = col * 26 + ord(ch) - 64
        col -= 1
    return (sheet or None), col


def check_values(cfg: Config, file_id: str, a1: str, values, *, full_rows: bool,
                 formulas: bool = False) -> None:
    if not isinstance(values, list) or not values or not all(isinstance(r, list) for r in values):
        raise PolicyError("values must be a non-empty list of rows (lists)")
    if sum(len(r) for r in values) > MAX_CELLS:
        raise PolicyError(f"at most {MAX_CELLS} cells per call")
    check_body_size(values)
    sheet, start = split_a1(a1)
    file_rules = [r for r in cfg.sheet_rules if r.file_id == file_id]
    if file_rules and sheet is None:
        raise PolicyError("this spreadsheet has write rules; include the tab name in the range, e.g. 'Backlog!A1'")
    rules = [r for r in file_rules if r.sheet in (None, sheet)]
    # A formula is written when the caller asks for one, or the sheet always allows them.
    # A rule set to false forbids them whatever the call says.
    vetoed = any(r.allow_formulas is False for r in rules)
    formulas_ok = not vetoed and (formulas or any(r.allow_formulas for r in rules))
    for row in values:
        for cell in row:
            if cell is not None and not isinstance(cell, (str, int, float, bool)):
                raise PolicyError(f"unsupported cell value type: {type(cell).__name__}")
            if isinstance(cell, str) and cell.startswith(("=", "+", "@")) and not formulas_ok:
                why = "this sheet does not allow formulas" if formulas else "pass formulas=true to write one"
                raise PolicyError(f"formula-like cell refused: {cell[:40]!r} ({why})")
    for rule in rules:
        for row in values:
            if full_rows and rule.columns is not None and len(row) != rule.columns:
                raise PolicyError(f"{rule.sheet} rows need exactly {rule.columns} columns, got {len(row)}")
            for j, cell in enumerate(row):
                allowed = rule.allowed.get(start + j)
                # None leaves a cell unchanged on update; an appended row must fill ruled columns.
                if allowed is not None and (cell is not None or full_rows) and str(cell) not in allowed:
                    raise PolicyError(f"{rule.sheet} column {start + j + 1}: {cell!r} not in {sorted(allowed)}")
