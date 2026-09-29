import asyncio
import unittest

from mcp.shared.memory import create_connected_server_and_client_session

from tapgame_mcp.server import create_owner_server


class FakeService:
    def query_owner_game_metrics(
        self, start_date=None, end_date=None, developer_id=None, app_id=None
    ):
        return {"found": True, "rows": []}

    def get_owner_game_daily_report(
        self, report_date=None, developer_id=None, app_id=None
    ):
        return {"found": True, "report": {}}

    def list_owner_games(self):
        return {"count": 2, "games": []}

    def query_owner_portfolio_metrics(
        self, start_date=None, end_date=None, developer_id=None
    ):
        return {"schema_version": 1, "rows": []}

    def get_owner_collection_status(self):
        return {"game_count": 2, "latest_collection": None}


class McpServerTests(unittest.TestCase):
    def test_server_only_exposes_owner_tools(self):
        async def scenario():
            server = create_owner_server(FakeService())
            async with create_connected_server_and_client_session(
                server, raise_exceptions=True
            ) as client:
                listed = await client.list_tools()
                self.assertEqual(
                    {
                        "query_owner_game_metrics",
                        "get_owner_game_daily_report",
                        "list_owner_games",
                        "query_owner_portfolio_metrics",
                        "get_owner_collection_status",
                    },
                    {tool.name for tool in listed.tools},
                )
                for tool in listed.tools:
                    self.assertTrue(tool.annotations.readOnlyHint)
                    self.assertFalse(tool.annotations.destructiveHint)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
