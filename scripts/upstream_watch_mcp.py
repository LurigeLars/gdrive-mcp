from __future__ import annotations

import json
import os
import subprocess
import tomllib
import urllib.request
from pathlib import Path

PACKAGE = "mcp"
UPSTREAM_REPO = "modelcontextprotocol/python-sdk"


def _gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], check=True, text=True, capture_output=True)
    return result.stdout


def _latest_release(token: str) -> str:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{UPSTREAM_REPO}/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "gdrive-mcp-upstream-watch",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)
    return str(payload["tag_name"]).removeprefix("v")


def _locked_version() -> str:
    lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    for item in lock["package"]:
        if item.get("name") == PACKAGE and "version" in item:
            return str(item["version"])
    raise RuntimeError("mcp is missing from uv.lock")


def main() -> None:
    token = os.environ["GH_TOKEN"]
    repo = os.environ["GH_REPO"]
    current = _locked_version()
    latest = _latest_release(token)
    prefix = "upstream-watch: mcp "

    _gh(
        "label", "create", "upstream-watch", "--repo", repo,
        "--color", "5319e7",
        "--description", "Upstream GitHub release differs from the locked dependency",
        "--force",
    )
    issues = json.loads(
        _gh(
            "issue", "list", "--repo", repo, "--state", "open",
            "--label", "upstream-watch", "--limit", "100", "--json", "number,title",
        )
    )
    related = [issue for issue in issues if issue["title"].startswith(prefix)]
    prs = json.loads(
        _gh(
            "pr", "list", "--repo", repo, "--state", "open", "--limit", "100",
            "--json", "title,author",
        )
    )
    has_dependabot_pr = any(
        pr.get("author", {}).get("login") == "dependabot[bot]"
        and PACKAGE in pr["title"].lower()
        for pr in prs
    )

    if current == latest or has_dependabot_pr:
        reason = "lock matches upstream" if current == latest else "Dependabot PR already open"
        for issue in related:
            _gh(
                "issue", "close", str(issue["number"]), "--repo", repo,
                "--comment", f"Closing automatically: {reason}.",
            )
        status = reason
    else:
        title = f"{prefix}{latest}"
        if not any(issue["title"] == title for issue in related):
            _gh(
                "issue", "create", "--repo", repo, "--title", title,
                "--body",
                (
                    f"Latest upstream MCP Python SDK release is `{latest}`, while "
                    f"`uv.lock` resolves `{current}`.\n\n"
                    "Dependabot remains the normal update path. This issue is a backstop "
                    "for upstream releases that have not yet produced a Dependabot PR."
                ),
                "--label", "upstream-watch",
            )
        for issue in related:
            if issue["title"] != title:
                _gh(
                    "issue", "close", str(issue["number"]), "--repo", repo,
                    "--comment", f"Superseded by upstream release {latest}.",
                )
        status = "issue tracked"

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write("## MCP upstream release watch\n\n")
            handle.write(f"- mcp: locked {current}, upstream {latest} — {status}\n")


if __name__ == "__main__":
    main()
