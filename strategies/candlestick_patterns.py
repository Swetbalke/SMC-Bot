"""Closed-candle engulfing pattern checks based on real bodies."""


def bullish_engulfing(previous, current) -> bool:
    return bool(
        float(previous["close"]) < float(previous["open"])
        and float(current["close"]) > float(current["open"])
        and float(current["open"]) <= float(previous["close"])
        and float(current["close"]) >= float(previous["open"])
    )


def bearish_engulfing(previous, current) -> bool:
    return bool(
        float(previous["close"]) > float(previous["open"])
        and float(current["close"]) < float(current["open"])
        and float(current["open"]) >= float(previous["close"])
        and float(current["close"]) <= float(previous["open"])
    )

