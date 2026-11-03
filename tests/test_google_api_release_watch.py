import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


def load_watch():
    path = Path(__file__).resolve().parents[1] / "scripts/google_api_release_watch.py"
    spec = importlib.util.spec_from_file_location("google_api_release_watch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_atom_feed_with_updated_timestamps():
    watch = load_watch()
    feed = b"""<feed xmlns="http://www.w3.org/2005/Atom">
      <entry><id>abc-2</id><title>New release</title>
        <updated>2026-11-04T09:00:00-07:00</updated></entry>
      <entry><id>abc-1</id><title>Old release</title>
        <updated>2026-10-12T09:00:00-07:00</updated></entry>
    </feed>"""
    events = watch.parse_feed(feed)
    assert len(events) == 2
    assert events[0][1] == "abc-2"
    assert events[0][0] == datetime(2026, 11, 4, 16, tzinfo=timezone.utc)


def test_feed_rejects_unknown_or_empty():
    watch = load_watch()
    for feed in [b"<html></html>", b"<feed xmlns=\"http://www.w3.org/2005/Atom\"></feed>"]:
        try:
            watch.parse_feed(feed)
        except ValueError:
            pass
        else:
            raise AssertionError("Expected feed error")


def test_no_prebaseline_issue_created():
    watch = load_watch()
    past = [(datetime(2026, 10, 12, tzinfo=timezone.utc), "old", "Old release")]
    with patch.object(watch, "gh", return_value="[]") as gh:
        watch.reconcile("Drive", past, "LurigeLars/gdrive-mcp")
        assert gh.call_count == 1


def test_repeated_release_is_idempotent():
    watch = load_watch()
    events = [(datetime(2026, 11, 4, tzinfo=timezone.utc), "new", "New API feature")]
    marker = watch.issue_token(events)
    existing = [{"number": 101, "title": "Upstream watch: Google Docs API release notes",
                 "state": "OPEN", "body": f"<!-- {watch.MARKER}: {marker} -->"}]
    with patch.object(watch, "gh", return_value=__import__("json").dumps(existing)) as gh:
        watch.reconcile("Docs", events, "LurigeLars/gdrive-mcp")
        assert gh.call_count == 1


def test_new_release_updates_review_issue():
    watch = load_watch()
    events = [(datetime(2026, 11, 4, tzinfo=timezone.utc), "new", "New API feature")]
    existing = [{"number": 101, "title": "Upstream watch: Google Sheets API release notes",
                 "state": "CLOSED", "body": "Old digest"}]
    calls = []
    def fake_gh(*args):
        calls.append(args)
        if args[:2] == ("issue", "list"):
            return __import__("json").dumps(existing)
        return ""
    with patch.object(watch, "gh", side_effect=fake_gh):
        watch.reconcile("Sheets", events, "LurigeLars/gdrive-mcp")
    assert ("issue", "reopen", "101", "--repo", "LurigeLars/gdrive-mcp") in calls
    assert any(args[:2] == ("issue", "edit") for args in calls)

