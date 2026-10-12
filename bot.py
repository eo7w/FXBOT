"""XAUUSD Telegram signal monitor. Signals only; it never places broker orders."""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

import requests
from technical_analysis import format_analysis

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


@dataclass(frozen=True)
class Config:
    bot_token: str
    chat_id: str
    biquote_base_url: str = "https://biquote.io/api"
    symbol: str = "XAUUSD"
    state_file: str = "state.json"
    loop_seconds: int = 5
    candle_refresh_seconds: int = 20
    max_quote_age_seconds: int = 60
    max_spread: float = 0.50
    min_atr: float = 0.30
    max_daily_trades: int = 30
    cooldown_seconds: int = 60
    max_hold_minutes: int = 20
    price_digits: int = 3

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("BOT_TOKEN", "").strip()
        chat_id = os.getenv("CHAT_ID", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN غير مضبوط. أضف توكن Telegram في متغيرات Render.")
        if not chat_id:
            raise RuntimeError("CHAT_ID غير مضبوط. أضف معرّف المحادثة في متغيرات Render.")
        return cls(
            bot_token=token,
            chat_id=chat_id,
            biquote_base_url=os.getenv("BIQUOTE_BASE_URL", "https://biquote.io/api").rstrip("/"),
            symbol=os.getenv("BIQUOTE_SYMBOL", "XAUUSD").strip().upper(),
            state_file=os.getenv("STATE_FILE", "state.json"),
            loop_seconds=_int_env("LOOP_SECONDS", 5, 2, 60),
            candle_refresh_seconds=_int_env("CANDLE_REFRESH_SECONDS", 20, 5, 300),
            max_quote_age_seconds=_int_env("MAX_QUOTE_AGE_SECONDS", 60, 5, 600),
            max_spread=_float_env("MAX_SPREAD", 0.50, 0.01, 10.0),
            min_atr=_float_env("MIN_ATR", 0.30, 0.01, 100.0),
            max_daily_trades=_int_env("MAX_DAILY_TRADES", 30, 1, 200),
            cooldown_seconds=_int_env("COOLDOWN_SECONDS", 60, 0, 3600),
            max_hold_minutes=_int_env("MAX_HOLD_MINUTES", 20, 1, 240),
            price_digits=_int_env("PRICE_DIGITS", 3, 2, 5),
        )


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        return max(low, min(high, value)) if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Bar:
    time: str
    open: float
    high: float
    low: float
    close: float
    is_open: bool = False


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def atr(bars: list[Bar], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    ranges = []
    for previous, current in zip(bars, bars[1:]):
        ranges.append(max(current.high - current.low,
                          abs(current.high - previous.close),
                          abs(current.low - previous.close)))
    sample = ranges[-period:]
    return sum(sample) / len(sample) if sample else 0.0


def rsi(values: list[float], period: int = 7) -> float:
    if len(values) < period + 1:
        return 50.0
    changes = [b - a for a, b in zip(values, values[1:])][-period:]
    gains = sum(max(change, 0.0) for change in changes) / period
    losses = sum(max(-change, 0.0) for change in changes) / period
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    return 100.0 - (100.0 / (1.0 + gains / losses))


def parse_bars(payload: dict[str, Any]) -> list[Bar]:
    bars: list[Bar] = []
    for item in payload.get("bars", []):
        try:
            bars.append(Bar(
                time=str(item["openTime"]),
                open=float(item["open"]),
                high=float(item["high"]),
                low=float(item["low"]),
                close=float(item["close"]),
                is_open=bool(item.get("isOpen", False)),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted((bar for bar in bars if not bar.is_open), key=lambda bar: bar.time)


def _bullish_setup(m1: list[Bar], m5: list[Bar], min_atr: float) -> tuple[str, str, float] | None:
    """Closed-candle trend + breakout/pullback filter; returns side, reason, M1 ATR."""
    if len(m1) < 40 or len(m5) < 30:
        return None
    closes1 = [bar.close for bar in m1]
    closes5 = [bar.close for bar in m5]
    e5 = ema(closes1, 5)
    e13 = ema(closes1, 13)
    e9_5m = ema(closes5, 9)
    e21_5m = ema(closes5, 21)
    volatility = atr(m1, 14)
    if volatility < min_atr:
        return None
    current = m1[-1]
    previous = m1[-2]
    strength = rsi(closes1, 7)
    body = abs(current.close - current.open)
    previous_high = max(bar.high for bar in m1[-6:-1])
    previous_low = min(bar.low for bar in m1[-6:-1])
    breakout_up = current.close > previous_high and current.close > current.open and body >= 0.20 * volatility
    breakout_down = current.close < previous_low and current.close < current.open and body >= 0.20 * volatility

    # Allow a recent EMA5 retest followed by a close back in the trend direction.
    recent_lows = m1[-4:-1]
    recent_highs = m1[-4:-1]
    pullback_up = (current.close > e5[-1] and current.close > current.open
                   and any(bar.low <= e5[-(len(recent_lows) - i + 1)] + 0.15 * volatility
                           for i, bar in enumerate(recent_lows)))
    pullback_down = (current.close < e5[-1] and current.close < current.open
                     and any(bar.high >= e5[-(len(recent_highs) - i + 1)] - 0.15 * volatility
                             for i, bar in enumerate(recent_highs)))
    # A close reclaiming EMA5 after the previous close was on the other side improves timing.
    reclaim_up = previous.close <= e5[-2] and current.close > e5[-1] and current.close > current.open
    reclaim_down = previous.close >= e5[-2] and current.close < e5[-1] and current.close < current.open

    trend_up = closes5[-1] > e9_5m[-1] > e21_5m[-1]
    trend_down = closes5[-1] < e9_5m[-1] < e21_5m[-1]
    aligned_up = closes1[-1] > e5[-1] > e13[-1]
    aligned_down = closes1[-1] < e5[-1] < e13[-1]

    if trend_up and aligned_up and 45.0 <= strength <= 72.0 and (breakout_up or pullback_up or reclaim_up):
        setup = "اختراق مقاومة قصيرة" if breakout_up else "ارتداد/استعادة متوسط EMA5"
        return "BUY", f"اتجاه 5 دقائق صاعد، والزخم متوافق ({setup})، RSI={strength:.0f}.", volatility
    if trend_down and aligned_down and 28.0 <= strength <= 55.0 and (breakout_down or pullback_down or reclaim_down):
        setup = "كسر دعم قصير" if breakout_down else "ارتداد/استعادة متوسط EMA5"
        return "SELL", f"اتجاه 5 دقائق هابط، والزخم متوافق ({setup})، RSI={strength:.0f}.", volatility
    return None


class BiQuoteClient:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "XAUUSD-Telegram-Signal-Bot/1.0"})

    def _get_json(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        response = self.session.get(url, params=params, timeout=(4, 8))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("BiQuote returned an unexpected response")
        return payload

    def quote(self) -> dict[str, Any]:
        url = f"{self.config.biquote_base_url}/{self.config.symbol}"
        return self._get_json(url, {"allowStale": "false"})

    def candles(self, interval: str, limit: int = 250) -> list[Bar]:
        url = f"{self.config.biquote_base_url}/{self.config.symbol}/ohlc"
        payload = self._get_json(url, {"interval": interval, "limit": str(limit)})
        return parse_bars(payload)


class TelegramClient:
    def __init__(self, config: Config):
        self.config = config
        self.base = f"https://api.telegram.org/bot{config.bot_token}"
        self.offset = 0
        self.session = requests.Session()

    def send(self, text: str) -> bool:
        try:
            response = self.session.post(
                f"{self.base}/sendMessage",
                json={"chat_id": self.config.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=(4, 12),
            )
            response.raise_for_status()
            if not response.json().get("ok", False):
                logging.error("Telegram rejected a message (response omitted).")
                return False
            return True
        except (requests.RequestException, ValueError):
            logging.warning("تعذر إرسال رسالة Telegram؛ سيحاول البوت لاحقًا.")
            return False

    def commands(self) -> list[str]:
        try:
            response = self.session.get(
                f"{self.base}/getUpdates",
                params={"offset": self.offset, "timeout": 0, "allowed_updates": json.dumps(["message"])},
                timeout=(2, 4),
            )
            response.raise_for_status()
            payload = response.json()
            commands: list[str] = []
            for update in payload.get("result", []):
                self.offset = max(self.offset, int(update.get("update_id", 0)) + 1)
                message = update.get("message", {})
                chat = message.get("chat", {})
                if str(chat.get("id", "")) != self.config.chat_id:
                    continue
                text = str(message.get("text", "")).strip().split()
                if text:
                    commands.append(text[0].split("@", 1)[0].lower())
            return commands
        except (requests.RequestException, ValueError, TypeError):
            return []


class StateStore:
    def __init__(self, filename: str):
        self.path = Path(filename)
        self.data: dict[str, Any] = {
            "day": datetime.now(timezone.utc).date().isoformat(),
            "trades_today": 0,
            "last_signal_epoch": 0.0,
            "active_trade": None,
            "paused": False,
        }
        self.load()

    def load(self) -> None:
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                self.data.update(saved)
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError):
            logging.warning("تعذر قراءة ملف الحالة؛ سيبدأ البوت بحالة جديدة.")
        self.roll_day()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix="state-", suffix=".json", dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(self.data, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except OSError:
            logging.exception("تعذر حفظ الحالة محليًا؛ تحقق من STATE_FILE وصلاحيات الكتابة.")

    def roll_day(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if self.data.get("day") != today:
            self.data["day"] = today
            self.data["trades_today"] = 0
            self.save()


class SignalBot:
    def __init__(self, config: Config):
        self.config = config
        self.market = BiQuoteClient(config)
        self.telegram = TelegramClient(config)
        self.state = StateStore(config.state_file)
        self.last_closed_bar = ""
        self.last_candle_refresh = 0.0
        self.m1: list[Bar] = []
        self.m5: list[Bar] = []
        self.m15: list[Bar] = []
        self.last_data_warning = 0.0

    def _price(self, value: float) -> str:
        return f"{value:,.{self.config.price_digits}f}"

    def _process_commands(self) -> None:
        for command in self.telegram.commands():
            if command == "/pause":
                self.state.data["paused"] = True
                self.state.save()
                self.telegram.send("⏸ تم إيقاف <b>الإشارات الجديدة</b>. متابعة الصفقة المفتوحة مستمرة.")
            elif command == "/resume":
                self.state.data["paused"] = False
                self.state.save()
                self.telegram.send("▶️ عادت مراقبة الإشارات الجديدة.")
            elif command == "/status":
                self._send_status()
            elif command == "/analysis":
                self._send_analysis()
            elif command in ("/help", "/start"):
                self.telegram.send("الأوامر: /analysis للتحليل الفني، /status للحالة، /pause لإيقاف الإشارات الجديدة، /resume لاستئنافها. متابعة الصفقة المفتوحة لا تتوقف عند /pause.")

    def _send_status(self) -> None:
        self.state.roll_day()
        active = self.state.data.get("active_trade")
        status = "متوقف مؤقتًا" if self.state.data.get("paused") else "يعمل"
        text = f"🛰 حالة بوت الإشارات: <b>{status}</b>\nإشارات اليوم (UTC): {self.state.data.get('trades_today', 0)}/{self.config.max_daily_trades}"
        if active:
            side = "شراء" if active["side"] == "BUY" else "بيع"
            text += f"\nصفقة متابعة مفتوحة: {side} من {self._price(float(active['entry']))} | هدف {self._price(float(active['target']))} | وقف {self._price(float(active['stop']))}"
        self.telegram.send(text)

    def _send_analysis(self) -> None:
        try:
            quote = self.market.quote()
            self._update_candles(time.monotonic(), force=True)
            setup = _bullish_setup(self.m1, self.m5, self.config.min_atr)
            report = format_analysis(
                self.config.symbol, quote, self.m1, self.m5, self.m15, setup,
                self.config.max_spread, self.config.max_quote_age_seconds, self._price,
            )
            self.telegram.send(report)
        except (requests.RequestException, ValueError, KeyError, TypeError):
            self.telegram.send("تعذر جلب بيانات كافية للتحليل الآن؛ سأحاول مجددًا في الدورة القادمة.")

    def _format_analysis(self, quote: dict[str, Any], compact: bool = False) -> str:
        setup = _bullish_setup(self.m1, self.m5, self.config.min_atr)
        return format_analysis(
            self.config.symbol, quote, self.m1, self.m5, self.m15, setup,
            self.config.max_spread, self.config.max_quote_age_seconds, self._price, compact,
        )

    def _update_candles(self, now: float, force: bool = False) -> None:
        if not force and now - self.last_candle_refresh < self.config.candle_refresh_seconds:
            return
        self.m1 = self.market.candles("1m", 250)
        self.m5 = self.market.candles("5m", 250)
        self.m15 = self.market.candles("15m", 250)
        self.last_candle_refresh = now

    def _quote_is_fresh(self, quote: dict[str, Any]) -> bool:
        try:
            age = float(quote.get("quoteAgeSeconds", math.inf))
            return (quote.get("marketState") == "open" and not quote.get("stale", True)
                    and age <= self.config.max_quote_age_seconds)
        except (TypeError, ValueError):
            return False

    def _monitor_trade(self, quote: dict[str, Any]) -> None:
        trade = self.state.data.get("active_trade")
        if not trade:
            return
        side = trade["side"]
        try:
            exit_price = float(quote["bid"] if side == "BUY" else quote["ask"])
        except (KeyError, TypeError, ValueError):
            return
        stop, target = float(trade["stop"]), float(trade["target"])
        reason = None
        # Conservative priority: if a polling gap leaves both barriers crossed, count the stop first.
        if (side == "BUY" and exit_price <= stop) or (side == "SELL" and exit_price >= stop):
            reason = "وقف الخسارة"
        elif (side == "BUY" and exit_price >= target) or (side == "SELL" and exit_price <= target):
            reason = "الهدف"
        elif time.time() - float(trade["opened_epoch"]) >= self.config.max_hold_minutes * 60:
            reason = "انتهاء مدة المتابعة"
        if not reason:
            return
        entry = float(trade["entry"])
        pnl = exit_price - entry if side == "BUY" else entry - exit_price
        result = "موجبة" if pnl > 0 else "سالبة" if pnl < 0 else "متعادلة"
        title = "🟢 شراء" if side == "BUY" else "🔴 بيع"
        message = (
            f"🏁 <b>انتهت متابعة صفقة {self.config.symbol}</b>\n{title}\n"
            f"السبب: {reason}\nسعر الدخول المرجعي: {self._price(entry)}\n"
            f"سعر الخروج المرصود: {self._price(exit_price)}\n"
            f"النتيجة السعرية التقديرية: <b>{self._price(pnl)}</b> دولار/أونصة ({result})\n"
            "هذه متابعة سعرية افتراضية قبل عمولة/انزلاق الوسيط وليست سجل تنفيذ حقيقي."
        )
        if self.telegram.send(message):
            self.state.data["active_trade"] = None
            self.state.save()

    def _consider_signal(self, quote: dict[str, Any]) -> None:
        if self.state.data.get("active_trade") or self.state.data.get("paused"):
            return
        self.state.roll_day()
        if int(self.state.data.get("trades_today", 0)) >= self.config.max_daily_trades:
            return
        if time.time() - float(self.state.data.get("last_signal_epoch", 0.0)) < self.config.cooldown_seconds:
            return
        if not self.m1 or not self.m5 or not self._quote_is_fresh(quote):
            return
        try:
            bid, ask = float(quote["bid"]), float(quote["ask"])
            spread = float(quote.get("spread", ask - bid))
        except (KeyError, TypeError, ValueError):
            return
        if bid <= 0 or ask < bid or spread > self.config.max_spread:
            return
        latest_time = self.m1[-1].time
        if latest_time == self.last_closed_bar:
            return
        self.last_closed_bar = latest_time
        setup = _bullish_setup(self.m1, self.m5, self.config.min_atr)
        if setup is None:
            return
        side, reason, volatility = setup
        entry = ask if side == "BUY" else bid
        risk_distance = max(volatility * 0.95, spread * 2.5, 0.25)
        reward_distance = max(risk_distance * 1.25, spread * 3.5)
        if side == "BUY":
            stop, target = entry - risk_distance, entry + reward_distance
            label = "🟢 <b>شراء</b>"
        else:
            stop, target = entry + risk_distance, entry - reward_distance
            label = "🔴 <b>بيع</b>"
        rr = reward_distance / risk_distance
        opened = datetime.now(timezone.utc).isoformat(timespec="seconds")
        message = (
            f"📊 <b>إشارة {escape(self.config.symbol)} — مراقبة فقط</b>\n{label}\n"
            f"الدخول المرجعي: <code>{self._price(entry)}</code>\n"
            f"الهدف القريب: <code>{self._price(target)}</code>\n"
            f"وقف الخسارة: <code>{self._price(stop)}</code>\n"
            f"نسبة العائد/المخاطرة النظرية: 1:{rr:.2f}\n"
            f"سبب الإشارة: {escape(reason)}\n"
            f"الإطار: 1m مع تأكيد 5m وسياق 15m | السبريد: {self._price(spread)}\n"
            f"{escape(self._format_analysis(quote, compact=True))}\n"
            f"وقت الإشارة: {opened}\n"
            "لا تُنفَّذ أي صفقة تلقائيًا؛ السعر الفعلي لدى وسيطك قد يختلف."
        )
        if not self.telegram.send(message):
            return
        self.state.data["active_trade"] = {
            "side": side,
            "entry": entry,
            "stop": stop,
            "target": target,
            "opened_epoch": time.time(),
            "opened_at": opened,
            "signal_bar": latest_time,
        }
        self.state.data["trades_today"] = int(self.state.data.get("trades_today", 0)) + 1
        self.state.data["last_signal_epoch"] = time.time()
        self.state.save()

    def run(self) -> None:
        logging.info("Starting signal monitor for %s", self.config.symbol)
        self.telegram.send(
            f"✅ <b>بدأ مراقب {escape(self.config.symbol)}</b>\n"
            f"بيانات: BiQuote | الإطارات: 1m + 5m + 15m\n"
            f"الحد الأقصى للإشارات: {self.config.max_daily_trades}/اليوم (UTC)\n"
            "إشارات ومتابعة فقط؛ لا ينفذ أوامر لدى وسيط. استخدم /analysis للتحليل الفني أو /status و/pause و/resume."
        )
        while True:
            started = time.monotonic()
            try:
                self._process_commands()
                quote = self.market.quote()
                if self._quote_is_fresh(quote):
                    self._monitor_trade(quote)
                self._update_candles(time.monotonic())
                self._consider_signal(quote)
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                now = time.monotonic()
                if now - self.last_data_warning >= 60:
                    logging.warning("تعذر تحديث بيانات السوق (%s). إعادة المحاولة تلقائيًا.", type(exc).__name__)
                    self.last_data_warning = now
            except Exception as exc:
                logging.error("خطأ غير متوقع (%s)، سيواصل البوت المحاولة.", type(exc).__name__)
            elapsed = time.monotonic() - started
            time.sleep(max(0.5, self.config.loop_seconds - elapsed))


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    config = Config.from_env()
    SignalBot(config).run()


if __name__ == "__main__":
    main()
