"""Measure semantic search against keyword search using a local, untracked benchmark case file.

Usage:
    uv run python scripts/measure_search.py benchmark_cases.local.json [report.md]

The JSON file must contain a list of objects with:
    question: natural-language query
    keywords: keyword query for drive_search
    expected_path: path fragment expected in the correct hit

Keeping cases outside source control prevents private corpus names, paths and questions from leaking into a
public repository. Tokens are estimated as characters / 4 of what the agent would receive.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time

sys.path.insert(0, ".")
from gdrive_mcp.index import open_index  # noqa: E402
from gdrive_mcp.server import tools as load_tools  # noqa: E402


def tok(s: str) -> int:
    return len(s) // 4


def load_cases(path: str) -> list[tuple[str, str, str]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("benchmark case file must contain a non-empty JSON list")
    cases = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"case {i}: expected an object")
        try:
            question = str(item["question"])
            keywords = str(item["keywords"])
            expected = str(item["expected_path"])
        except KeyError as exc:
            raise ValueError(f"case {i}: missing {exc.args[0]}") from None
        cases.append((question, keywords, expected))
    return cases


def main(cases_path: str, report: str | None) -> None:
    cases = load_cases(cases_path)
    t = load_tools()
    idx = open_index(t)
    rows, sums = [], {"sem": 0, "sem_read": 0, "kw": 0, "sem_hit": 0, "kw_hit": 0}
    for question, keywords, expected in cases:
        t0 = time.time()
        sem = idx.search(question, 8)
        sem_ms = int((time.time() - t0) * 1000)
        sem_text = json.dumps({"results": sem}, ensure_ascii=False)
        sem_rank = next((i + 1 for i, r in enumerate(sem) if expected in r["path"]), None)

        kw = json.loads(t.drive_search(keywords, max_results=20))["files"]
        kw_text = json.dumps({"files": kw}, ensure_ascii=False)
        kw_rank = next((i + 1 for i, r in enumerate(kw) if expected in r["path"]), None)

        def read_cost(hits):
            if not hits:
                return 0
            try:
                out = t.drive_read(hits[0]["id"], max_chars=20000)
                return tok(out if isinstance(out, str) else "x" * 6000)
            except Exception:  # noqa: BLE001 - e.g. Sheets: count nothing
                return 0

        kw_total = tok(kw_text) + read_cost(kw)
        sem_only = tok(sem_text)
        sem_read = sem_only + read_cost(sem)
        sums["sem"] += sem_only
        sums["sem_read"] += sem_read
        sums["kw"] += kw_total
        sums["sem_hit"] += bool(sem_rank and sem_rank <= 5)
        sums["kw_hit"] += bool(kw_rank and kw_rank <= 5)
        rows.append((question, sem_rank, kw_rank, sem_only, sem_read, kw_total, sem_ms))
        print(f"{question[:50]:50} sem#{sem_rank} kw#{kw_rank} sem={sem_only} sem+read={sem_read} kw+read={kw_total} {sem_ms}ms")

    n = len(cases)
    lines = [
        "# Semantic search vs keyword search",
        "",
        f"Date: {time.strftime('%Y-%m-%d %H:%M')} · Index: {json.dumps(idx.stats(), ensure_ascii=False)}",
        "",
        "Tokens ≈ characters/4 of returned content.",
        "- **Semantic:** search result only, 8 passages.",
        "- **Semantic + read:** plus reading the top hit (≤20,000 characters).",
        "- **Keyword + read:** `drive_search`, 20 hits, plus reading the top hit.",
        "- **Rank:** position of the expected path (– = not found).",
        "",
        "| Question | Semantic rank | Keyword rank | Semantic tokens | Semantic+read | Keyword+read | Semantic time |",
        "|---|---|---|---|---|---|---|",
    ]
    for q, sr, kr, s, sr_t, k, ms in rows:
        lines.append(f"| {q} | {sr or '–'} | {kr or '–'} | {s} | {sr_t} | {k} | {ms} ms |")
    lines += [
        "",
        f"**Top-5 hits:** semantic {sums['sem_hit']}/{n}, keyword {sums['kw_hit']}/{n}.",
        "",
        f"**Total tokens:** semantic {sums['sem']}, semantic+read {sums['sem_read']}, keyword+read {sums['kw']}.",
    ]
    text = "\n".join(lines) + "\n"
    if report:
        Path(report).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: measure_search.py benchmark_cases.local.json [report.md]")
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
