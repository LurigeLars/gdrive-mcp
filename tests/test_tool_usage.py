import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from gdrive_mcp.tool_usage import KNOWN_TOOLS, UsageStore, observe_call


class UsageTest(TestCase):
    def test_counts_and_redaction(self):
        with TemporaryDirectory() as root, patch.dict("os.environ", {"PYTEST_CURRENT_TEST": ""}):
            store = UsageStore(Path(root) / "usage.db", frozenset({"quote", "other"}))
            for ms in (10, 100, 200, 800, 900):
                store.record("quote", ms, "OK")
            store.record("quote", 50, "PRIVATE_TOKEN")
            store.record("private_account_identifier", 10, "OK")
            r = store.report()
            self.assertEqual(6, r["recorded_calls"])
            self.assertEqual(1, r["recorded_errors"])
            self.assertEqual(["other"], r["unobserved_tools"])
            self.assertEqual({"UNCLASSIFIED_ERROR": 1}, r["tools"][0]["error_codes"])
            self.assertEqual(900, r["tools"][0]["p95_upper_ms"])
            self.assertNotIn(b"PRIVATE_TOKEN", store.path.read_bytes())
            self.assertNotIn(b"private_account_identifier", store.path.read_bytes())
            self.assertEqual(6, UsageStore(store.path, store.tools).report()["recorded_calls"])

    def test_observer_preserves_failure_and_result(self):
        with TemporaryDirectory() as root, patch.dict("os.environ", {"PYTEST_CURRENT_TEST": ""}):
            store = UsageStore(Path(root) / "usage.db", frozenset({"quote", "other"}))
            async def good(_):
                return {"success": True, "secret": "PRIVATE_RESPONSE"}

            async def bad(_):
                raise TimeoutError("PRIVATE_EXCEPTION")

            self.assertEqual("PRIVATE_RESPONSE",
                             asyncio.run(observe_call(store, "quote", None, good))["secret"])
            with self.assertRaises(TimeoutError):
                asyncio.run(observe_call(store, "quote", None, bad))
            self.assertEqual({"TIMEOUT": 1}, store.report()["tools"][0]["error_codes"])
            self.assertNotIn(b"PRIVATE_RESPONSE", store.path.read_bytes())
            self.assertNotIn(b"PRIVATE_EXCEPTION", store.path.read_bytes())

    def test_inventory(self):
        self.assertTrue(KNOWN_TOOLS)
        self.assertNotIn("tool_usage", KNOWN_TOOLS)


    def test_mcp_catalog_and_failure(self):
        import anyio
        from mcp.client import Client
        from gdrive_mcp import server, tool_usage

        async def exercise(store):
            async with Client(server.mcp) as client:
                names = {t.name for t in (await client.list_tools()).tools}
                self.assertEqual(KNOWN_TOOLS | {"tool_usage"}, names)
                # Missing required argument: rejected before Google access.
                await client.call_tool("drive_read", {})
                report = await client.call_tool("tool_usage", {})
                self.assertFalse(report.is_error)
            self.assertEqual(1, store.report()["recorded_calls"])
            self.assertEqual(1, store.report()["recorded_errors"])

        with TemporaryDirectory() as root, patch.dict("os.environ", {"PYTEST_CURRENT_TEST": ""}):
            store = UsageStore(Path(root) / "usage.db", KNOWN_TOOLS)
            with patch.object(tool_usage, "_store", store):
                anyio.run(exercise, store)
