from __future__ import annotations

import re
import zlib

import numpy as np
import pytest

from gdrive_mcp.index import Index, IndexConfig, chunk, prefixes
from gdrive_mcp.tools import Tools
from test_gdrive_mcp import FakeApi, cfg, f

DIM = 64


def fake_embed(texts, prefix):
    """Bag-of-words hashing: texts sharing words get similar vectors."""
    out = np.zeros((len(texts), DIM), dtype=np.float32)
    for i, t in enumerate(texts):
        for w in re.findall(r"\w+", t.lower()):
            out[i, zlib.crc32(w.encode()) % DIM] += 1
    n = np.linalg.norm(out, axis=1, keepdims=True)
    return out / np.where(n == 0, 1, n)


class IndexApi(FakeApi):
    def __init__(self):
        super().__init__()
        self.items["N1"] = f("N1", "fiskar.md", "text/markdown", ["PROJ"], size="10")
        self.items["N2"] = f("N2", "bilar.md", "application/octet-stream", ["PROJ"], size="10")
        self.items["LOG"] = f("LOG", "run.log", "text/plain", ["PROJ"], size="10")
        self.items["SKIPDIR"] = f("SKIPDIR", "logs", "application/vnd.google-apps.folder", ["TOOLS"])
        self.items["N3"] = f("N3", "hidden.md", "text/markdown", ["SKIPDIR"], size="10")
        self.texts.update({"N1": "laxen simmar i älven och gäddan jagar", "N2": "volvo och saab är bilar med motor",
                           "LOG": "log line", "N3": "laxen i loggen"})
        self.listing = [self.items[k] for k in ("N1", "N2", "LOG", "N3", "PIC", "OUTFILE", "PROJ")]
        self.changes: list[dict] = []

    def start_page_token(self):
        return "t1"

    def list_changes(self, page_token):
        self._rec("list_changes", page_token)
        return {"changes": self.changes, "newStartPageToken": "t2"}


@pytest.fixture
def setup(tmp_path):
    api = IndexApi()
    tools = Tools(api, cfg())
    icfg = IndexConfig(chunk_chars=40, overlap=10, exclude_paths=("ChatGPT/Tools/logs",))
    return api, Index(tmp_path / "idx.sqlite", tools, icfg, embed=fake_embed)


def test_chunk_sizes_and_overlap():
    text = " ".join(f"w{i}" for i in range(200))
    parts = chunk(text, 100, 20)
    assert all(len(p) <= 100 for p in parts)
    assert parts[0].split()[-1] in parts[1]  # overlap keeps context across the cut
    assert parts[-1].endswith("w199")
    assert chunk("   ", 100, 20) == []


def test_build_indexes_only_allowed_text(setup):
    api, idx = setup
    s = idx.build(log=lambda *_: None)
    assert s.indexed == 2  # N1 and N2 (octet-stream .md counts as text)
    ids = {r["id"] for r in idx.search("laxen", 10)} | {r["id"] for r in idx.search("volvo", 10)}
    assert ids == {"N1", "N2"}  # LOG (.log), N3 (excluded path), PIC, OUTFILE and folders are not indexed


def test_exclude_globs_drop_raw_data(tmp_path):
    api = IndexApi()
    api.items["RAW"] = f("RAW", "clip.json", "application/json", ["SKIPDIR"], size="10")
    api.texts["RAW"] = "laxen simmar i transkriptionen"
    api.listing = [api.items["N1"], api.items["RAW"]]
    tools = Tools(api, cfg())
    icfg = IndexConfig(chunk_chars=40, overlap=10, exclude_globs=("ChatGPT/Tools/*/clip.json",))
    idx = Index(tmp_path / "idx.sqlite", tools, icfg, embed=fake_embed)
    s = idx.build(log=lambda *_: None)
    assert s.indexed == 1 and "RAW" not in {r["id"] for r in idx.search("laxen", 5)}


def test_rebuild_skips_unchanged_and_removes_vanished(setup):
    api, idx = setup
    idx.build(log=lambda *_: None)
    api.listing = [api.items["N1"]]
    s = idx.build(log=lambda *_: None)
    assert (s.indexed, s.unchanged, s.removed) == (0, 1, 1)
    assert idx.stats()["files"] == 1


def test_search_ranks_by_meaning_and_rechecks_boundary(setup):
    api, idx = setup
    idx.build(log=lambda *_: None)
    top = idx.search("gäddan i älven", 1)[0]
    assert top["id"] == "N1" and top["path"] == "ChatGPT/Projects/fiskar.md" and "gäddan" in top["snippet"]
    api.items["N1"]["parents"] = ["OTHER"]  # moved out after indexing
    assert "N1" not in {r["id"] for r in idx.search("gäddan i älven", 5)}


def test_update_applies_changes(setup):
    api, idx = setup
    idx.build(log=lambda *_: None)
    api.items["N1"]["modifiedTime"] = "2026-09-18T00:00:00.000Z"
    api.texts["N1"] = "helt ny text om rymden"
    api.changes = [{"fileId": "N1", "file": api.items["N1"]}, {"fileId": "N2", "removed": True}]
    s = idx.update()
    assert (s.indexed, s.removed) == (1, 1)
    assert idx.search("rymden", 1)[0]["id"] == "N1"
    assert "N2" not in {r["id"] for r in idx.search("volvo", 5)}


def test_vectors_are_not_reloaded_on_every_change(tmp_path):
    """While a build runs the version changes per file; reloading the whole matrix each search was slow."""
    now = [1000.0]
    api = IndexApi()
    tools = Tools(api, cfg(), clock=lambda: now[0])
    idx = Index(tmp_path / "idx.sqlite", tools, IndexConfig(chunk_chars=40, overlap=10), embed=fake_embed)
    idx.build(log=lambda *_: None)
    assert idx.search("laxen", 5)
    api.items["N9"] = f("N9", "ny.md", "text/markdown", ["PROJ"], size="10")
    api.texts["N9"] = "helt ny fil om rymden och raketer"
    api.listing = [api.items["N9"]]
    idx.build(log=lambda *_: None)
    assert "N9" not in {r["id"] for r in idx.search("rymden raketer", 5)}  # cached matrix, < RELOAD_SECONDS
    now[0] += Index.RELOAD_SECONDS + 1
    assert "N9" in {r["id"] for r in idx.search("rymden raketer", 5)}


def test_hits_are_boundary_checked_in_parallel(setup):
    api, idx = setup
    idx.build(log=lambda *_: None)
    seen = []
    real = idx.tools.guard.check
    idx.tools.guard.check = lambda fid, meta=None: (seen.append(fid), real(fid, meta))[1]
    out = idx.search("laxen volvo", 5)
    assert {r["id"] for r in out} == {"N1", "N2"}
    assert len(seen) == len(set(seen))  # each file checked once, not once per chunk


def test_update_before_build_fails(setup):
    _, idx = setup
    with pytest.raises(RuntimeError, match="not built"):
        idx.update()


def test_one_bad_file_does_not_stop_build(setup):
    api, idx = setup
    api.items["BAD"] = f("BAD", "bad.pdf", "application/pdf", ["PROJ"], size="4")
    api.texts["BAD"] = b"nope"
    api.listing.append(api.items["BAD"])
    s = idx.build(log=lambda *_: None)
    assert s.indexed == 2 and len(s.errors) == 1 and "bad.pdf" in s.errors[0]


def test_model_prefixes():
    """A wrong task prefix silently degrades ranking, so pin the two models we use."""
    assert prefixes("embeddinggemma:latest")[1].startswith("task: search result")
    assert prefixes("nomic-embed-text") == ("search_document: ", "search_query: ")
    assert prefixes("bge-m3") == ("", "")


def test_exclude_ext_drops_machine_data(tmp_path):
    """Machine data (.json, .bat, ...) outranked documentation, so it is not indexed."""
    api = IndexApi()
    api.items["CFG"] = f("CFG", "state.json", "application/json", ["PROJ"], size="10")
    api.texts["CFG"] = "laxen simmar i konfigurationen"
    api.listing = [api.items["N1"], api.items["CFG"]]
    icfg = IndexConfig(chunk_chars=40, overlap=10, exclude_ext=frozenset({".json"}))
    idx = Index(tmp_path / "idx.sqlite", tools := Tools(api, cfg()), icfg, embed=fake_embed)
    assert idx.build(log=lambda *_: None).indexed == 1
    assert "CFG" not in {r["id"] for r in idx.search("laxen", 5)} and tools
