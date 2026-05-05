import os
import sys
import unittest
from unittest.mock import patch

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(root, "strategy_builder"))
sys.path.insert(0, root)

from fastapi.testclient import TestClient

from backend.main import app
from backend.routers import ict as ict_router


class ICTApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_start_requires_authentication(self):
        with patch.object(ict_router, "is_authenticated", return_value=False):
            response = self.client.post(
                "/api/ict/start",
                json={"symbols": ["005930"], "config_id": "default_ict_v1"},
            )

        self.assertEqual(response.status_code, 401)

    def test_start_rejects_prod_mode(self):
        with (
            patch.object(ict_router, "is_authenticated", return_value=True),
            patch.object(ict_router, "get_current_mode", return_value="prod"),
        ):
            response = self.client.post(
                "/api/ict/start",
                json={"symbols": ["005930"], "config_id": "default_ict_v1"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("vps paper mode only", response.json()["detail"])

    def test_stop_with_cancel_requires_authentication(self):
        with patch.object(ict_router, "is_authenticated", return_value=False):
            response = self.client.post(
                "/api/ict/stop",
                json={"cancel_pending": True},
            )

        self.assertEqual(response.status_code, 401)

    def test_status_includes_realtime_and_risk_guard(self):
        response = self.client.get("/api/ict/status")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("realtime", body)
        self.assertIn("loss_limit_reached", body["daily_risk"])


if __name__ == "__main__":
    unittest.main()
