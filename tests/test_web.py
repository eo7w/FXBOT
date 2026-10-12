import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import web


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.temp_dir.name, "state.json")
        self.app = web.create_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        self.env = patch.dict(os.environ, {
            "STATE_FILE": self.state_path,
            "LOOP_SECONDS": "5",
            "MAX_DAILY_TRADES": "30",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp_dir.cleanup()

    def test_status_returns_heartbeat_and_active_trade(self):
        with open(self.state_path, "w", encoding="utf-8") as handle:
            json.dump({
                "last_heartbeat_epoch": time.time(),
                "paused": False,
                "trades_today": 2,
                "active_trade": {"side": "BUY", "entry": 2300.1, "target": 2301.0, "stop": 2299.2, "opened_at": "2026-10-12T09:00:00+00:00"},
            }, handle)
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["symbol"], "XAUUSD")
        self.assertEqual(payload["bot"]["status"], "active")
        self.assertEqual(payload["bot"]["trades_today"], 2)
        self.assertEqual(payload["bot"]["active_trade"]["side"], "BUY")
        self.assertNotIn("market", payload)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_status_reports_missing_state_without_failing(self):
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["bot"]["status"], "missing")
        self.assertNotIn("market", payload)

    def test_dashboard_page_is_served_without_a_password(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        response.close()
        self.assertIn("لوحة متابعة الذهب", page)


if __name__ == "__main__":
    unittest.main()
