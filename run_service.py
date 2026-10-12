"""Run the Telegram bot once alongside the Render web dashboard."""
from __future__ import annotations

import fcntl
import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import TextIO

import bot


def _bounded_int(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return default


def build_web_command() -> list[str]:
    port_text = os.getenv("PORT", "8080").strip() or "8080"
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    workers = _bounded_int("WEB_WORKERS", 2, 1, 4)
    threads = _bounded_int("WEB_THREADS", 4, 1, 16)
    return [
        sys.executable, "-m", "gunicorn",
        "--bind", f"0.0.0.0:{port}",
        "--workers", str(workers),
        "--threads", str(threads),
        "--timeout", "60",
        "web:app",
    ]


def acquire_bot_lock() -> TextIO | None:
    """Allow only one supervisor on this host to start the bot loop."""
    lock_path = Path(os.getenv("BOT_LOCK_FILE", "/tmp/fxbot-bot.lock"))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _run_bot() -> None:
    try:
        bot.main()
    except Exception:
        logging.exception("The bot stopped unexpectedly; the dashboard will remain available.")


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        bot_lock = acquire_bot_lock()
    except OSError:
        logging.exception("Could not acquire the single-bot lock.")
        return 1

    if bot_lock is None:
        logging.info("Another supervisor holds the bot lock; skipping duplicate bot startup.")
    else:
        logging.info("Starting the bot once in the service supervisor.")
        threading.Thread(target=_run_bot, name="fxbot-signal-loop", daemon=True).start()

    web_process: subprocess.Popen[bytes] | None = None
    previous_handlers: dict[int, object] = {}

    def stop_web(signum, _frame):
        logging.info("Received signal %s; stopping the web workers.", signum)
        if web_process is not None and web_process.poll() is None:
            web_process.terminate()

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, stop_web)
        web_process = subprocess.Popen(build_web_command())
        return int(web_process.wait() or 0)
    except (OSError, ValueError):
        logging.exception("Could not start the web server.")
        return 1
    finally:
        if web_process is not None and web_process.poll() is None:
            web_process.terminate()
            try:
                web_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                web_process.kill()
                web_process.wait()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        if bot_lock is not None:
            bot_lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
