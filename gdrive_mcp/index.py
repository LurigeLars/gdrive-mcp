"""Local semantic search index over the text inside the allowed folders.

Embeddings come from the local Ollama (no file content leaves the machine). Vectors live in SQLite next
to the token. Access is still decided by the Guard at query time.

  python -m gdrive_mcp.index build     # full scan, re-embeds only changed files
  python -m gdrive_mcp.index update    # apply Drive changes since the last run
  python -m gdrive_mcp.index stats
  python -m gdrive_mcp.index search "query"
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import tomllib
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from fnmatch import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import extract
from .policy import FOLDER, PolicyError, SHORTCUT
from .tools import _dumps, _ext, _is_text

# Task prefixes per embedding model family; models not listed take the text as it is.
PREFIXES = {
    "nomic-embed-text": ("search_document: ", "search_query: "),
    "embeddinggemma": ("title: none | text: ", "task: search result | query: "),
}


def prefixes(model: str) -> tuple[str, str]:
    return PREFIXES.get(model.split(":")[0], ("", ""))


@dataclass(frozen=True)
class IndexConfig:
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "embeddinggemma"
    chunk_chars: int = 1200
    overlap: int = 200
    max_file_chars: int = 200_000
    batch: int = 32
    exclude_paths: tuple[str, ...] = ()
    exclude_globs: tuple[str, ...] = ()  # fnmatch patterns on the path, e.g. "*/youtube/transcripts/*"
    exclude_ext: frozenset[str] = frozenset({".log", ".pyc", ".zip", ".vtt", ".srt"})
    exclude_mime_prefixes: tuple[str, ...] = ("image/", "video/", "audio/", "application/zip",
                                              "application/x-zip", "application/octet-stream")

    @classmethod
    def load(cls, path: str | Path) -> IndexConfig:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8")).get("index", {})
        kw = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        for k in ("exclude_paths", "exclude_globs", "exclude_mime_prefixes"):
            if k in kw:
                kw[k] = tuple(kw[k])
        if "exclude_ext" in kw:
            kw["exclude_ext"] = frozenset(e.lower() for e in kw["exclude_ext"])
        return cls(**kw)


def chunk(text: str, size: int, overlap: int) -> list[str]:
    """Split into ~size-char pieces, preferring line or word breaks, overlapping by `overlap`."""
    text = text.strip()
    out, i = [], 0
    while i < len(text):
        end = min(len(text), i + size)
        if end < len(text):
            cut = text.rfind("\n", i + size // 2, end)
            if cut <= i:
                cut = text.rfind(" ", i + size // 2, end)
            if cut > i:
                end = cut
        piece = text[i:end].strip()
        if piece:
            out.append(piece)
        if end >= len(text):
            break
        i = max(end - overlap, i + 1)
    return out


class Embedder:
    def __init__(self, cfg: IndexConfig):
        self.cfg = cfg

    def __call__(self, texts: list[str], prefix: str) -> np.ndarray:
        vecs = []
        for i in range(0, len(texts), self.cfg.batch):
            body = json.dumps({"model": self.cfg.model, "input": [prefix + t for t in texts[i:i + self.cfg.batch]]})
            req = urllib.request.Request(f"{self.cfg.ollama_url}/api/embed", body.encode(),
                                         {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as r:
                vecs += json.load(r)["embeddings"]
        arr = np.asarray(vecs, dtype=np.float32).reshape(len(texts), -1)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        return arr / np.where(norms == 0, 1, norms)


@dataclass
class Stats:
    indexed: int = 0
    unchanged: int = 0
    skipped: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)


class Index:
    def __init__(self, db_path: str | Path, tools, cfg: IndexConfig, embed=None):
        self.path = Path(db_path)
        self.tools = tools
        self.cfg = cfg
        self.embed = embed or Embedder(cfg)
        self.prefix_doc, self.prefix_query = prefixes(cfg.model)
        self._matrix: tuple[int, np.ndarray, np.ndarray] | None = None  # (version, rowids, vectors)
        self._loaded_at = -1e9
        self.clock = getattr(tools.guard, "clock", time.monotonic)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY, path TEXT, mime TEXT, modified TEXT);
                CREATE TABLE IF NOT EXISTS chunks(file_id TEXT, pos INTEGER, text TEXT, vec BLOB);
                CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            """)

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def _meta(self, db, key, value=None):
        if value is None:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else None
        db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))

    def _bump(self, db):
        self._meta(db, "version", int(self._meta(db, "version") or 0) + 1)

    # --- what to index -------------------------------------------------------------------------
    def indexable(self, m: dict) -> bool:
        mime = m.get("mimeType", "")
        if mime in (FOLDER, SHORTCUT) or m.get("trashed"):
            return False
        excluded_mime = any(mime.startswith(p) for p in self.cfg.exclude_mime_prefixes)
        if excluded_mime and mime not in extract.DOCUMENT_MIMES and not _is_text({"name": m.get("name", "")}):
            return False
        if _ext(m.get("name", "")) in self.cfg.exclude_ext:
            return False
        path = m["path"]
        if any(fnmatch(path, g) for g in self.cfg.exclude_globs):
            return False
        return not any(path == p or path.startswith(p.rstrip("/") + "/") for p in self.cfg.exclude_paths)

    def _remove(self, db, file_id: str) -> bool:
        gone = db.execute("DELETE FROM files WHERE id=?", (file_id,)).rowcount
        db.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
        return bool(gone)

    def _index_file(self, m: dict, stats: Stats) -> None:
        with self._db() as db:
            row = db.execute("SELECT modified FROM files WHERE id=?", (m["id"],)).fetchone()
        if row and row[0] == m.get("modifiedTime"):
            stats.unchanged += 1
            return
        try:
            text = self.tools.file_text(m)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
            stats.errors.append(f"{m['path']}: {exc}"[:200])
            return
        pieces = chunk((text or "")[: self.cfg.max_file_chars], self.cfg.chunk_chars, self.cfg.overlap)
        vecs = self.embed([f"{m['path']}\n{p}" for p in pieces], self.prefix_doc) if pieces else None
        with self._db() as db:
            self._remove(db, m["id"])
            db.execute("INSERT INTO files VALUES(?,?,?,?)", (m["id"], m["path"], m.get("mimeType"), m.get("modifiedTime")))
            db.executemany("INSERT INTO chunks VALUES(?,?,?,?)",
                           [(m["id"], i, p, vecs[i].tobytes()) for i, p in enumerate(pieces)])
            self._bump(db)
        stats.indexed += 1

    # --- build / update ------------------------------------------------------------------------
    def build(self, log=print) -> Stats:
        stats, seen, token = Stats(), set(), self.tools.api.start_page_token()
        page, n = None, 0
        while True:
            res = self.tools.api.list_files("trashed = false", page_token=page, page_size=1000)
            for f in res.get("files", []):
                n += 1
                m = self.tools.guard.inside(f)
                if not m or not self.indexable(m):
                    stats.skipped += 1
                    continue
                seen.add(m["id"])
                self._index_file(m, stats)
                if (stats.indexed + stats.unchanged) % 100 == 0:
                    log(f"scanned {n}, indexed {stats.indexed}, unchanged {stats.unchanged}, errors {len(stats.errors)}")
            page = res.get("nextPageToken")
            if not page:
                break
        with self._db() as db:
            stale = [r[0] for r in db.execute("SELECT id FROM files") if r[0] not in seen]
            for fid in stale:
                stats.removed += self._remove(db, fid)
            self._meta(db, "page_token", token)
            self._meta(db, "built_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
            self._bump(db)
        return stats

    def update(self) -> Stats:
        stats = Stats()
        with self._db() as db:
            token = self._meta(db, "page_token")
        if not token:
            raise RuntimeError("index not built yet; run: python -m gdrive_mcp.index build")
        while token:
            res = self.tools.api.list_changes(token)
            for ch in res.get("changes", []):
                f = ch.get("file")
                m = None if ch.get("removed") or not f else self.tools.guard.inside(f)
                if m and self.indexable(m):
                    self._index_file(m, stats)
                else:
                    with self._db() as db:
                        if self._remove(db, ch["fileId"]):
                            stats.removed += 1
                            self._bump(db)
            if "newStartPageToken" in res:
                with self._db() as db:
                    self._meta(db, "page_token", res["newStartPageToken"])
                break
            token = res.get("nextPageToken")
        return stats

    # --- search --------------------------------------------------------------------------------
    RELOAD_SECONDS = 60  # while a build runs the version changes per file; don't reload the matrix each search

    def _vectors(self) -> tuple[np.ndarray, np.ndarray]:
        with self._db() as db:
            version = int(self._meta(db, "version") or 0)
            fresh_enough = self._matrix is not None and self.clock() - self._loaded_at < self.RELOAD_SECONDS
            if (self._matrix is None or self._matrix[0] != version) and not fresh_enough:
                self._loaded_at = self.clock()
                rows = db.execute("SELECT rowid, vec FROM chunks").fetchall()
                ids = np.array([r[0] for r in rows], dtype=np.int64)
                mat = (np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1)
                       if rows else np.zeros((0, 0), dtype=np.float32))
                self._matrix = (version, ids, mat)
        return self._matrix[1], self._matrix[2]

    def _check_many(self, file_ids: list[str]) -> dict[str, dict | None]:
        """Boundary-check hits in parallel; each check is a Drive round trip (cached folders aside)."""
        def one(fid: str):
            try:
                return fid, self.tools.guard.check(fid)
            except PolicyError:
                return fid, None

        if len(file_ids) < 2:
            return dict(one(f) for f in file_ids)
        with ThreadPoolExecutor(max_workers=min(8, len(file_ids))) as pool:
            return dict(pool.map(one, file_ids))

    def search(self, query: str, max_results: int = 10, per_file: int = 2, snippet_chars: int = 500) -> list[dict]:
        ids, mat = self._vectors()
        if not len(ids):
            return []
        scores = mat @ self.embed([query], self.prefix_query)[0]
        order = np.argsort(-scores)[: max_results * 30]  # bounded even if a few files dominate
        hits, per = [], {}
        with self._db() as db:
            for idx in order:
                row = db.execute("SELECT file_id, pos, text FROM chunks WHERE rowid=?", (int(ids[idx]),)).fetchone()
                if not row or per.get(row[0], 0) >= per_file:
                    continue
                per[row[0]] = per.get(row[0], 0) + 1
                hits.append((row[0], row[1], row[2], float(scores[idx])))
                if len(hits) >= max_results * 2:  # spare hits in case some files fail the boundary check
                    break
        checked = self._check_many(list(dict.fromkeys(h[0] for h in hits)))
        out = []
        for fid, pos, text, score in hits:
            m = checked.get(fid)
            if m is None:
                continue
            out.append({"id": fid, "path": m["path"], "score": round(score, 3),
                        "chunk": pos, "snippet": text[:snippet_chars]})
            if len(out) >= max_results:
                break
        return out

    def stats(self) -> dict:
        with self._db() as db:
            return {"files": db.execute("SELECT COUNT(*) FROM files").fetchone()[0],
                    "chunks": db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
                    "built_at": self._meta(db, "built_at"), "db_mb": round(self.path.stat().st_size / 2**20, 1)}


def open_index(tools) -> Index:
    from .server import LOCAL, REPO  # noqa: PLC0415 - same defaults as the server
    import os

    config = Path(os.environ.get("GDRIVE_MCP_CONFIG", REPO / "config.toml"))
    db = Path(os.environ.get("GDRIVE_MCP_INDEX", LOCAL / "index.sqlite"))
    return Index(db, tools, IndexConfig.load(config))


def main(argv: list[str]) -> int:
    from .server import tools as load_tools  # noqa: PLC0415

    cmd = argv[0] if argv else "stats"
    idx = open_index(load_tools())
    t0 = time.time()
    if cmd == "build":
        s = idx.build()
    elif cmd == "update":
        s = idx.update()
    elif cmd == "search":
        print(_dumps(idx.search(" ".join(argv[1:]), 5)))
        return 0
    else:
        print(_dumps(idx.stats()))
        return 0
    result = _dumps({"command": cmd, "finished": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     "seconds": round(time.time() - t0), "indexed": s.indexed, "unchanged": s.unchanged,
                     "skipped": s.skipped, "removed": s.removed, "errors": len(s.errors),
                     "first_errors": s.errors[:5], **idx.stats()})
    print(result)
    (idx.path.parent / "index-last.json").write_text(result, encoding="utf-8")  # visible even under pythonw
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
