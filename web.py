"""Read-only dashboard server for the XAUUSD Telegram signal bot."""
from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import requests
from flask import Flask, jsonify, send_from_directory

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
_quote_lock = Lock()
_quote_cache: dict[str, Any] = {"fetched_at": 0.0, "quote": None}


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _iso_time(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def get_market_quote() -> dict[str, Any] | None:
    """Fetch and briefly cache public market data; never uses Telegram credentials."""
    now = time.monotonic()
    cache_ttl = _int_env("WEB_QUOTE_CACHE_SECONDS", 5)
    with _quote_lock:
        if now - float(_quote_cache["fetched_at"]) < cache_ttl:
            return _quote_cache["quote"]
        quote: dict[str, Any] | None = None
        try:
            base_url = os.getenv("BIQUOTE_BASE_URL", "https://biquote.io/api").rstrip("/")
            symbol = os.getenv("BIQUOTE_SYMBOL", "XAUUSD").strip().upper() or "XAUUSD"
            response = requests.get(
                f"{base_url}/{symbol}",
                params={"allowStale": "false"},
                headers={"User-Agent": "FXBOT-Web-Dashboard/1.0"},
                timeout=(3, 5),
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                bid, ask = _number(payload.get("bid")), _number(payload.get("ask"))
                if bid is not None and ask is not None and bid > 0 and ask >= bid:
                    quote = {
                        "symbol": symbol,
                        "bid": bid,
                        "ask": ask,
                        "spread": _number(payload.get("spread")) or ask - bid,
                        "market_state": str(payload.get("marketState", "unknown")),
                        "stale": bool(payload.get("stale", False)),
                        "quote_age_seconds": _number(payload.get("quoteAgeSeconds")),
                    }
        except (requests.RequestException, ValueError, TypeError):
            quote = None
        _quote_cache["fetched_at"] = time.monotonic()
        _quote_cache["quote"] = quote
        return quote


def _load_bot_state() -> tuple[dict[str, Any], bool]:
    filename = os.getenv("STATE_FILE", "state.json")
    path = Path(filename)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (data, True) if isinstance(data, dict) else ({}, False)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}, False


def _active_trade(data: dict[str, Any]) -> dict[str, Any] | None:
    trade = data.get("active_trade")
    if not isinstance(trade, dict) or trade.get("side") not in ("BUY", "SELL"):
        return None
    entry, target, stop = (_number(trade.get(key)) for key in ("entry", "target", "stop"))
    if entry is None or target is None or stop is None:
        return None
    return {
        "side": trade["side"],
        "entry": entry,
        "target": target,
        "stop": stop,
        "opened_at": str(trade.get("opened_at", "")),
    }


def create_app() -> Flask:
    app = Flask(__name__)

    @app.after_request
    def add_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def dashboard():
        return send_from_directory(WEB_ROOT, "index.html")

    @app.get("/healthz")
    def health():
        return jsonify(ok=True)

    @app.get("/api/status")
    def status():
        state, state_available = _load_bot_state()
        now = time.time()
        heartbeat = _number(state.get("last_heartbeat_epoch"))
        loop_seconds = _int_env("LOOP_SECONDS", 5)
        stale_after = _int_env("WEB_STATUS_STALE_SECONDS", max(90, loop_seconds * 6))
        heartbeat_age = max(0.0, now - heartbeat) if heartbeat is not None else None
        running = bool(state_available and heartbeat_age is not None and heartbeat_age <= stale_after)
        paused = bool(state.get("paused", False))
        if not state_available:
            bot_status = "missing"
        elif not running:
            bot_status = "stopped"
        elif paused:
            bot_status = "paused"
        else:
            bot_status = "active"

        quote = get_market_quote()
        symbol = os.getenv("BIQUOTE_SYMBOL", "XAUUSD").strip().upper() or "XAUUSD"
        if quote:
            market = {
                "available": True,
                "symbol": quote["symbol"],
                "bid": quote["bid"],
                "ask": quote["ask"],
                "spread": quote["spread"],
                "market_state": quote["market_state"],
                "stale": quote["stale"],
                "quote_age_seconds": quote["quote_age_seconds"],
                "updated_at": _iso_time(time.time()),
            }
        else:
            market = {
                "available": False,
                "symbol": symbol,
                "bid": None,
                "ask": None,
                "spread": None,
                "market_state": "unknown",
                "stale": True,
                "quote_age_seconds": None,
                "updated_at": None,
            }

        try:
            trades_today = max(0, int(state.get("trades_today", 0)))
        except (TypeError, ValueError):
            trades_today = 0
        max_daily_trades = _int_env("MAX_DAILY_TRADES", 30)
        return jsonify({
            "generated_at": _iso_time(now),
            "bot": {
                "status": bot_status,
                "state_available": state_available,
                "running": running,
                "paused": paused,
                "trades_today": trades_today,
                "max_daily_trades": max_daily_trades,
                "heartbeat_age_seconds": round(heartbeat_age, 1) if heartbeat_age is not None else None,
                "last_heartbeat": _iso_time(heartbeat),
                "active_trade": _active_trade(state),
            },
            "market": market,
            "disclaimer": "بيانات متابعة مرجعية فقط؛ لا ينفذ الموقع أوامر لدى وسيط ولا يضمن الربح.",
        })

    return app


app = create_app()


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    port = _int_env("PORT", _int_env("WEB_PORT", 8080))
    app.run(host=os.getenv("WEB_HOST", "0.0.0.0"), port=port, debug=False, threaded=True)
