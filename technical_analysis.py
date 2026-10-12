"""Rule-based candlestick and market-structure analysis for closed OHLC bars.

This recognizes common, heuristic patterns. It is not a trained chart-vision model
and it does not claim to recognize every possible candle formation.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from html import escape
from typing import Any, Callable


@dataclass(frozen=True)
class Pivot:
    index: int
    time: str
    price: float
    kind: str  # "high" or "low"


@dataclass(frozen=True)
class CandlePattern:
    name: str
    direction: str


def find_swings(bars: list[Any], left: int = 2, right: int = 2) -> list[Pivot]:
    """Find confirmed local swing points; pivots need `right` closed bars to confirm."""
    if left < 1 or right < 1 or len(bars) < left + right + 1:
        return []
    pivots: list[Pivot] = []
    for index in range(left, len(bars) - right):
        bar = bars[index]
        before = bars[index - left:index]
        after = bars[index + 1:index + right + 1]
        if bar.high > max(item.high for item in before) and bar.high >= max(item.high for item in after):
            pivots.append(Pivot(index, str(bar.time), float(bar.high), "high"))
        if bar.low < min(item.low for item in before) and bar.low <= min(item.low for item in after):
            pivots.append(Pivot(index, str(bar.time), float(bar.low), "low"))
    return sorted(pivots, key=lambda pivot: (pivot.index, pivot.kind))


def _rsi(closes: list[float], period: int = 7) -> float:
    if len(closes) < period + 1:
        return 50.0
    changes = [after - before for before, after in zip(closes, closes[1:])][-period:]
    gains = sum(max(change, 0.0) for change in changes) / period
    losses = sum(max(-change, 0.0) for change in changes) / period
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + gains / losses)


def _atr(bars: list[Any], period: int = 14) -> float:
    ranges = [
        max(bar.high - bar.low, abs(bar.high - previous.close), abs(bar.low - previous.close))
        for previous, bar in zip(bars, bars[1:])
    ]
    sample = ranges[-period:]
    return sum(sample) / len(sample) if sample else 0.0


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def candlestick_patterns(bars: list[Any]) -> list[CandlePattern]:
    """Detect several common single-, two-, and three-candle formations."""
    if not bars:
        return []
    current = bars[-1]
    previous = bars[-2] if len(bars) >= 2 else None
    two_back = bars[-3] if len(bars) >= 3 else None
    body = abs(current.close - current.open)
    candle_range = max(current.high - current.low, 1e-12)
    upper_wick = current.high - max(current.open, current.close)
    lower_wick = min(current.open, current.close) - current.low
    patterns: list[CandlePattern] = []

    if body / candle_range <= 0.10:
        patterns.append(CandlePattern("دوجي (تردد)", "محايد"))
    if body > 0 and lower_wick >= 2.0 * body and upper_wick <= max(0.60 * body, 0.12 * candle_range):
        patterns.append(CandlePattern("ذيل سفلي طويل (مطرقة محتملة)", "احتمال ارتداد صاعد؛ يحتاج تأكيدًا"))
    if body > 0 and upper_wick >= 2.0 * body and lower_wick <= max(0.60 * body, 0.12 * candle_range):
        patterns.append(CandlePattern("ذيل علوي طويل (شهاب محتمل)", "احتمال ضغط هابط؛ يحتاج تأكيدًا"))
    if body / candle_range >= 0.85:
        direction = "صاعد" if current.close > current.open else "هابط" if current.close < current.open else "محايد"
        patterns.append(CandlePattern("شمعة ممتلئة قوية", direction))

    if previous is not None:
        previous_body = abs(previous.close - previous.open)
        if current.high <= previous.high and current.low >= previous.low:
            patterns.append(CandlePattern("شمعة داخلية", "انتظار اختراق النطاق"))
        if (current.close > current.open and previous.close < previous.open
                and current.open <= previous.close and current.close >= previous.open):
            patterns.append(CandlePattern("ابتلاع شرائي", "صاعد محتمل"))
        elif (current.close < current.open and previous.close > previous.open
                and current.open >= previous.close and current.close <= previous.open):
            patterns.append(CandlePattern("ابتلاع بيعي", "هابط محتمل"))
        elif previous_body > body and min(current.open, current.close) >= min(previous.open, previous.close) \
                and max(current.open, current.close) <= max(previous.open, previous.close):
            patterns.append(CandlePattern("هارامي محتمل", "تباطؤ؛ الاتجاه غير محسوم"))
        if len(bars) >= 14:
            tolerance = max(_atr(bars, 14) * 0.08, 0.03)
            if abs(previous.high - current.high) <= tolerance and previous.close > previous.open and current.close < current.open:
                patterns.append(CandlePattern("قمتان متقاربتان (tweezer top)", "ضغط هابط محتمل"))
            if abs(previous.low - current.low) <= tolerance and previous.close < previous.open and current.close > current.open:
                patterns.append(CandlePattern("قاعان متقاربان (tweezer bottom)", "ضغط صاعد محتمل"))

    if two_back is not None and previous is not None:
        first_body = abs(two_back.close - two_back.open)
        middle_body = abs(previous.close - previous.open)
        first_midpoint = (two_back.open + two_back.close) / 2.0
        if (two_back.close < two_back.open and middle_body <= first_body * 0.60
                and current.close > current.open and current.close > first_midpoint):
            patterns.append(CandlePattern("نجم صباحي محتمل", "انعكاس صاعد محتمل"))
        if (two_back.close > two_back.open and middle_body <= first_body * 0.60
                and current.close < current.open and current.close < first_midpoint):
            patterns.append(CandlePattern("نجم مسائي محتمل", "انعكاس هابط محتمل"))
    return patterns


def analyze_timeframe(bars: list[Any]) -> dict[str, Any]:
    """Summarize EMA trend, swing structure, latest confirmed levels and candle shape."""
    result: dict[str, Any] = {
        "trend": "بيانات غير كافية", "structure": "غير مكتمل", "structure_direction": "neutral",
        "highs": [], "lows": [], "breakout": "لا توجد بيانات كافية لتأكيد كسر",
        "patterns": [], "rsi": 50.0, "atr": 0.0, "last_close": None,
    }
    if not bars:
        return result
    closes = [float(bar.close) for bar in bars]
    current = bars[-1]
    result.update({
        "highs": [pivot for pivot in find_swings(bars) if pivot.kind == "high"],
        "lows": [pivot for pivot in find_swings(bars) if pivot.kind == "low"],
        "patterns": candlestick_patterns(bars),
        "rsi": _rsi(closes, 7),
        "atr": _atr(bars, 14),
        "last_close": float(current.close),
    })
    e9, e21 = _ema(closes, 9), _ema(closes, 21)
    if len(closes) >= 21:
        if closes[-1] > e9[-1] > e21[-1]:
            result["trend"] = "صاعد"
        elif closes[-1] < e9[-1] < e21[-1]:
            result["trend"] = "هابط"
        else:
            result["trend"] = "عرضي/غير حاسم"
    highs, lows = result["highs"], result["lows"]
    if len(highs) >= 2 and len(lows) >= 2:
        higher_high = highs[-1].price > highs[-2].price
        higher_low = lows[-1].price > lows[-2].price
        lower_high = highs[-1].price < highs[-2].price
        lower_low = lows[-1].price < lows[-2].price
        if higher_high and higher_low:
            result["structure"], result["structure_direction"] = "قمم وقيعان أعلى (HH/HL)", "bullish"
        elif lower_high and lower_low:
            result["structure"], result["structure_direction"] = "قمم وقيعان أدنى (LH/LL)", "bearish"
        else:
            result["structure"] = "بنية مختلطة/نطاقية"

    previous_close = float(bars[-2].close) if len(bars) >= 2 else float(current.close)
    last_high = highs[-1] if highs else None
    last_low = lows[-1] if lows else None
    if last_high and current.close > last_high.price:
        prefix = "كسر صاعد جديد" if previous_close <= last_high.price else "السعر فوق"
        result["breakout"] = f"{prefix} آخر قمة مؤكدة {last_high.price:.3f}"
        result["breakout_direction"] = "bullish"
    elif last_low and current.close < last_low.price:
        prefix = "كسر هابط جديد" if previous_close >= last_low.price else "السعر تحت"
        result["breakout"] = f"{prefix} آخر قاع مؤكد {last_low.price:.3f}"
        result["breakout_direction"] = "bearish"
    else:
        result["breakout"] = "لا يوجد إغلاق يتجاوز آخر قمة/قاع مؤكد"
        result["breakout_direction"] = "neutral"
    return result


def format_analysis(
    symbol: str,
    quote: dict[str, Any],
    m1: list[Any],
    m5: list[Any],
    m15: list[Any],
    setup: tuple[str, str, float] | None,
    max_spread: float,
    max_quote_age: int,
    price: Callable[[float], str],
    compact: bool = False,
) -> str:
    views = {"1m": analyze_timeframe(m1), "5m": analyze_timeframe(m5), "15m": analyze_timeframe(m15)}
    try:
        bid, ask = float(quote["bid"]), float(quote["ask"])
        spread = float(quote.get("spread", ask - bid))
        age = float(quote.get("quoteAgeSeconds", math.inf))
    except (KeyError, TypeError, ValueError):
        bid = ask = spread = 0.0
        age = math.inf
    fresh = quote.get("marketState") == "open" and not quote.get("stale", True) and age <= max_quote_age
    conflicting_15m = False
    if setup:
        side, reason, volatility = setup
        wanted = "صاعد" if side == "BUY" else "هابط"
        opposite = "هابط" if side == "BUY" else "صاعد"
        conflicting_15m = views["15m"]["trend"] == opposite
        action = "شراء" if side == "BUY" else "بيع"
        if not fresh or spread > max_spread:
            suggestion = "انتظار؛ السعر غير حديث أو السبريد أعلى من الحد المسموح."
        elif conflicting_15m:
            suggestion = f"إشارة {action} قصيرة موجودة، لكن 15m {opposite}؛ الأفضل انتظار توافق الإطار الأكبر."
        else:
            suggestion = f"مراقبة {action} مشروطة؛ ليست أمر تنفيذ ولا ضمانًا."
        entry = ask if side == "BUY" else bid
        risk = max(volatility * 0.95, spread * 2.5, 0.25)
        reward = max(risk * 1.25, spread * 3.5)
        stop = entry - risk if side == "BUY" else entry + risk
        target = entry + reward if side == "BUY" else entry - reward
        levels = f"الدخول المرجعي {price(entry)} | الهدف {price(target)} | الوقف {price(stop)}"
        setup_reason = reason
    else:
        levels = "لا توجد مستويات دخول صالحة الآن."
        setup_reason = "لم يتحقق محفز دخول متوافق على شموع الدقيقة."
        if not fresh or spread > max_spread:
            suggestion = "انتظار؛ السعر غير حديث أو السبريد أعلى من الحد المسموح."
        elif views["5m"]["trend"] == views["15m"]["trend"] == "صاعد":
            suggestion = "ميل صاعد، لكن لا يوجد محفز دخول؛ انتظر إغلاقًا واضحًا فوق قمة مؤكدة أو ارتدادًا مؤكدًا."
        elif views["5m"]["trend"] == views["15m"]["trend"] == "هابط":
            suggestion = "ميل هابط، لكن لا يوجد محفز دخول؛ انتظر إغلاقًا واضحًا تحت قاع مؤكد أو إعادة اختبار فاشلة."
        else:
            suggestion = "انتظار؛ اتجاهات الأطر غير متوافقة أو لا يوجد تأكيد كافٍ."

    def describe(label: str, view: dict[str, Any]) -> str:
        highs, lows = view["highs"], view["lows"]
        high_text = price(highs[-1].price) if highs else "غير متاح"
        low_text = price(lows[-1].price) if lows else "غير متاح"
        patterns = "، ".join(pattern.name for pattern in view["patterns"][:3]) or "لا يوجد نموذج واضح"
        return (f"{label}: اتجاه {view['trend']}؛ البنية {view['structure']}؛ "
                f"آخر قمة {high_text} وآخر قاع {low_text}؛ {view['breakout']}؛ "
                f"RSI7={view['rsi']:.0f} وATR14={view['atr']:.3f}؛ الشمعة: {patterns}.")

    if compact:
        p1 = "، ".join(pattern.name for pattern in views["1m"]["patterns"][:2]) or "لا نموذج بارز"
        context = (f"السياق الفني: 1m {views['1m']['trend']}، 5m {views['5m']['trend']}، "
                   f"15m {views['15m']['trend']}؛ {views['5m']['structure']}؛ "
                   f"{views['5m']['breakout']}؛ شمعة 1m: {p1}.")
        return f"{context}\nالتوصية المشروطة: {suggestion}"

    lines = [
        f"📋 <b>التحليل الفني لـ {escape(symbol)}</b>",
        f"السعر: bid {price(bid)} | ask {price(ask)} | السبريد {price(spread)} | عمر السعر {age:.0f}s",
        describe("1m", views["1m"]),
        describe("5m", views["5m"]),
        describe("15m", views["15m"]),
        "ملاحظة القمم/القيعان: تُعتمد القمة أو القاع بعد ظهور شمعتين مغلقتين على يمينه لتقليل الإشارات الوهمية.",
        f"سبب/حالة الدخول: {escape(setup_reason)}",
        f"المستويات التقديرية: {levels}",
        f"💡 <b>الاقتراح:</b> {escape(suggestion)}",
        "هذا تحليل آلي مبني على قواعد شائعة، وليس نصيحة مالية أو ضمانًا للنتيجة.",
    ]
    return "\n".join(lines)
