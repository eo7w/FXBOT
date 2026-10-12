import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_service


class ServiceSupervisorTests(unittest.TestCase):
    def test_gunicorn_workers_serve_web_app_without_starting_bot_workers(self):
        with patch.dict(os.environ, {"PORT": "12345", "WEB_WORKERS": "3", "WEB_THREADS": "2"}):
            command = run_service.build_web_command()
        self.assertIn("--workers", command)
        self.assertEqual(command[command.index("--workers") + 1], "3")
        self.assertIn("web:app", command)
        self.assertNotIn("bot:app", command)

    def test_local_lock_allows_only_one_bot_supervisor_per_host(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "bot.lock"
            with patch.dict(os.environ, {"BOT_LOCK_FILE": str(lock_path)}):
                first = run_service.acquire_bot_lock()
                second = run_service.acquire_bot_lock()
                self.assertIsNotNone(first)
                self.assertIsNone(second)
                first.close()
                third = run_service.acquire_bot_lock()
                self.assertIsNotNone(third)
                third.close()

    def test_web_command_rejects_invalid_port(self):
        with patch.dict(os.environ, {"PORT": "invalid"}):
            with self.assertRaises(ValueError):
                run_service.build_web_command()


if __name__ == "__main__":
    unittest.main()
