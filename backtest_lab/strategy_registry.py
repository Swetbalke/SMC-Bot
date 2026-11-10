STRATEGIES = ("SMC BB Breakout", "EMA Crossover + 100-Point Trail")


def validate_request(request):
    if request.get("strategy") not in STRATEGIES:
        raise ValueError("Unsupported strategy")
    if request.get("timeframe") not in {"1m", "3m", "5m", "15m", "30m", "1h", "4h"}:
        raise ValueError("Unsupported timeframe")
    costs = request.get("costs") or {}
    if not 0 <= float(costs.get("fee_rate", 0)) <= .02 or not 0 <= float(costs.get("slippage_bps", 0)) <= 500:
        raise ValueError("Invalid fee or slippage")
