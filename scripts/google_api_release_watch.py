"""Read-only upstream release-note watch for Drive, Docs and Sheets APIs.

Only Google-published release notes are monitored. This is not a fork sync.
The latest observed release IDs are stored in a single review issue per API.
"""
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.request import Request, urlopen

START = datetime(2026, 11, 3, tzinfo=timezone.utc)
ATOM = "{http://www.w3.org/2005/Atom}"
SOURCES = {
    "Drive": "https://developers.google.com/feeds/drive-release-notes.xml",
    "Docs": "https://developers.google.com/feeds/docs-release-notes.xml",
    "Sheets": "https://developers.google.com/feeds/sheets-release-notes.xml",
}
PAGE = "https://developers.google.com/workspace/{}/release-notes"
MARKER = "google-api-upstream-release-token"


def parse_date(value):
    if not value:
        raise ValueError("Release-note entry lacks a timestamp")
    try:
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        result = parsedate_to_datetime(value.strip())
    if result.tzinfo is None:
        raise ValueError("Release-note timestamp lacks timezone")
    return result.astimezone(timezone.utc)


def parse_feed(payload):
    root = ET.fromstring(payload)
    if root.tag == ATOM + "feed":
        nodes = root.findall(ATOM + "entry")
        parsed = [
            (parse_date(n.findtext(ATOM + "published") or n.findtext(ATOM + "updated")),
             n.findtext(ATOM + "id") or n.findtext(ATOM + "title") or "",
             n.findtext(ATOM + "title") or "Untitled")
            for n in nodes
        ]
    elif root.tag == "rss":
        nodes = root.findall("./channel/item")
        parsed = [
            (parse_date(n.findtext("pubDate")),
             n.findtext("guid") or n.findtext("link") or n.findtext("title") or "",
             n.findtext("title") or "Untitled")
            for n in nodes
        ]
    else:
        raise ValueError("Unexpected Google release-note feed format")
    if not parsed:
        raise ValueError("Google release-note feed contained no entries")
    return sorted(parsed, reverse=True)


def fetch_feed(url):
    request = Request(url, headers={"User-Agent": "gdrive-mcp-release-watch/1"})
    last_error = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=15) as response:
                data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise ValueError("Google release-note feed exceeds size limit")
            return parse_feed(data)
        except (OSError, ValueError, ET.ParseError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1)
    raise RuntimeError(f"Cannot read Google release-note feed: {last_error}")


def issue_token(items):
    identifiers = "|".join(f"{when.isoformat()}:{ident}" for when, ident, _ in items)
    return hashlib.sha256(identifiers.encode("utf-8")).hexdigest()[:24]


def issue_body(api, items):
    token = issue_token(items)
    lines = [
        f"<!-- {MARKER}: {token} -->",
        f"## Google {api} API: official upstream release notes",
        "",
        f"Source: {PAGE.format(api.lower())}",
        "Review relevance for our REST-based Drive/Docs/Sheets MCP adapter.",
        "Google-hosted Workspace MCP release changes do not necessarily affect this adapter.",
        "Do not automatically update credentials, scopes, policy, or runtime.",
        "",
    ]
    for when, _, title in items[:12]:
        safe_title = re.sub(r"\s+", " ", title)[:180]
        safe_title = re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", safe_title)
        lines.append(f"- **{when.date().isoformat()}**: {safe_title}")
    return "\n".join(lines) + "\n"


def gh(*args):
    result = subprocess.run(["gh", *args], check=True, text=True,
                            capture_output=True, timeout=30)
    return result.stdout.strip()


def reconcile(api, items, repo):
    title = f"Upstream watch: Google {api} API release notes"
    matches = json.loads(gh("issue", "list", "--repo", repo, "--state", "all",
                            "--limit", "500", "--json", "number,title,state,body"))
    existing = next((x for x in matches if x["title"] == title), None)
    new_items = [x for x in items if x[0] >= START]
    if not new_items:
        print(f"{api}: no new API notes since {START.date()}")
        return
    body = issue_body(api, new_items)
    token = issue_token(new_items)
    if existing and f"<!-- {MARKER}: {token} -->" in existing.get("body", ""):
        print(f"{api}: already reported; review issue unchanged")
        return
    with tempfile.TemporaryDirectory() as tmp:
        body_file = Path(tmp) / "issue.md"
        body_file.write_text(body, encoding="utf-8")
        if existing:
            gh("issue", "edit", str(existing["number"]), "--repo", repo,
               "--body-file", str(body_file))
            if existing["state"] == "CLOSED":
                gh("issue", "reopen", str(existing["number"]), "--repo", repo)
        else:
            gh("issue", "create", "--repo", repo, "--title", title,
               "--body-file", str(body_file))
    print(f"{api}: updated upstream review issue")


def main():
    repo = os.environ.get("GH_REPO", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise RuntimeError("GH_REPO must identify the current repository")
    for api, feed in SOURCES.items():
        items = fetch_feed(feed)
        print(f"{api}: latest note {items[0][0].date().isoformat()}")
        reconcile(api, items, repo)


if __name__ == "__main__":
    main()
