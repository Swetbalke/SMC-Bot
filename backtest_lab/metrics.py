import math
import pandas as pd


def summarize(trades, equity, gaps):
    pnl = [float(t["net_pnl"]) for t in trades]
    wins, losses = [x for x in pnl if x > 0], [x for x in pnl if x < 0]
    gross_loss = abs(sum(losses))
    factor = sum(wins) / gross_loss if gross_loss else None
    curve = pd.Series(equity, dtype=float)
    drawdown = ((curve / curve.cummax()) - 1).min() * 100 if len(curve) else 0
    returns = curve.pct_change().dropna()
    sharpe = (returns.mean() / returns.std() * math.sqrt(len(returns))) if len(returns) > 1 and returns.std() else 0
    return {"trades": len(trades), "win_rate": (len(wins) / len(pnl) * 100) if pnl else 0,
            "profit_factor": factor, "expectancy": sum(pnl) / len(pnl) if pnl else 0,
            "max_drawdown_pct": float(drawdown), "total_return": ((curve.iloc[-1] / curve.iloc[0]) - 1) * 100 if len(curve) > 1 else 0,
            "average_r": sum(float(t["r"]) for t in trades) / len(trades) if trades else 0,
            "sharpe": float(sharpe), "sharpe_formula": "mean(period returns) / stdev(period returns) × sqrt(number of returns)",
            "gaps": gaps, "equity": equity}
