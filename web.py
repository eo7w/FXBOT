"""Read-only dashboard server for the XAUUSD Telegram signal bot."""
from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, send_from_directory

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"


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


def _quote_cache_paths() -> tuple[Path, Path]:
    cache_file = Path(os.getenv("BIQUOTE_CACHE_FILE", "/tmp/fxbot-biquote-cache.json"))
    lock_file = Path(os.getenv("BIQUOTE_CACHE_LOCK_FILE", "/tmp/fxbot-biquote-cache.lock"))
    if not cache_file.is_absolute():
        cache_file = Path.cwd() / cache_file
    if not lock_file.is_absolute():
        lock_file = Path.cwd() / lock_file
    return cache_file, lock_file


def _read_quote_cache(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _write_quote_cache(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix="fxbot-quote-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"))
        os.replace(temporary_path, path)
    finally:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass


def _retry_after_seconds(headers: Any) -> int:
    try:
        return min(60, max(1, int(headers.get("Retry-After", "1"))))
    except (AttributeError, TypeError, ValueError):
        return 1


def _fetch_market_quote() -> tuple[dict[str, Any] | None, int]:
    base_url = os.getenv("BIQUOTE_BASE_URL", "https://biquote.io/api").rstrip("/")
    symbol = os.getenv("BIQUOTE_SYMBOL", "XAUUSD").strip().upper() or "XAUUSD"
    try:
        response = requests.get(
            f"{base_url}/{symbol}",
            headers={"User-Agent": "FXBOT-Web-Dashboard/1.0"},
            timeout=(3, 5),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.HTTPError as exc:
        return None, _retry_after_seconds(getattr(exc.response, "headers", {}))
    except (requests.RequestException, ValueError, TypeError):
        return None, 1

    if not isinstance(payload, dict):
        return None, 1
    bid, ask = _number(payload.get("bid")), _number(payload.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return None, 1

    midpoint = _number(payload.get("mid"))
    spread = _number(payload.get("spread"))
    return {
        "available": True,
        "symbol": symbol,
        "bid": bid,
        "ask": ask,
        "mid": midpoint if midpoint is not None and midpoint > 0 else (bid + ask) / 2,
        "spread": spread if spread is not None and spread >= 0 else ask - bid,
        "market_state": str(payload.get("marketState", "unknown")).lower(),
        "stale": bool(payload.get("stale", False)),
        "quote_age_seconds": _number(payload.get("quoteAgeSeconds")),
        "timestamp": str(payload.get("timestamp", "")),
    }, 1


def get_market_quote() -> tuple[dict[str, Any] | None, int]:
    """Fetch a public quote, coalescing concurrent viewers across web workers."""
    cache_seconds = _int_env("WEB_QUOTE_CACHE_SECONDS", 1)
    cache_path, lock_path = _quote_cache_paths()
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            now = time.monotonic()
            snapshot = _read_quote_cache(cache_path)
            if snapshot is not None:
                fetched_at = _number(snapshot.get("fetched_at"))
                ttl = _number(snapshot.get("cache_ttl_seconds")) or cache_seconds
                if fetched_at is not None and max(0.0, now - fetched_at) < ttl:
                    cached_quote = snapshot.get("quote")
                    quote = cached_quote if isinstance(cached_quote, dict) else None
                    cached_retry = _number(snapshot.get("retry_after")) or 1
                    retry_after = min(60, max(1, int(cached_retry)))
                    return quote, min(60, retry_after)

            quote, retry_after = _fetch_market_quote()
            cache_ttl = cache_seconds if quote is not None else max(cache_seconds, retry_after)
            _write_quote_cache(cache_path, {
                "fetched_at": time.monotonic(),
                "cache_ttl_seconds": cache_ttl,
                "retry_after": retry_after,
                "quote": quote,
            })
            return quote, retry_after
    except OSError as exc:
        logging.warning("تعذر استخدام ذاكرة سعر السوق المشتركة (%s).", type(exc).__name__)
        return None, 1


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

    @app.get("/api/price")
    def price():
        quote, retry_after = get_market_quote()
        if quote is None:
            response = jsonify({
                "available": False,
                "symbol": os.getenv("BIQUOTE_SYMBOL", "XAUUSD").strip().upper() or "XAUUSD",
                "error": "quote_unavailable",
            })
            response.status_code = 503
            response.headers["Retry-After"] = str(retry_after)
            return response
        return jsonify(quote)

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

        symbol = os.getenv("BIQUOTE_SYMBOL", "XAUUSD").strip().upper() or "XAUUSD"
        try:
            trades_today = max(0, int(state.get("trades_today", 0)))
        except (TypeError, ValueError):
            trades_today = 0
        max_daily_trades = _int_env("MAX_DAILY_TRADES", 30)
        return jsonify({
            "generated_at": _iso_time(now),
            "symbol": symbol,
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
            "disclaimer": "بيانات متابعة مرجعية فقط؛ لا ينفذ الموقع أوامر لدى وسيط ولا يضمن الربح.",
        })

    return app


app = create_app()


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    port = _int_env("PORT", _int_env("WEB_PORT", 8080))
    app.run(host=os.getenv("WEB_HOST", "0.0.0.0"), port=port, debug=False, threaded=True)
