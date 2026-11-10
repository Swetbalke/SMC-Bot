from unittest.mock import Mock, patch
import pandas as pd

from backtest_lab.public_client import DeltaPublicHistoryClient
from backtest_lab.metrics import summarize


def test_public_client_uses_no_auth_headers():
    response = Mock(status_code=200)
    response.json.return_value = {"success": True, "result": []}
    response.raise_for_status.return_value = None
    with patch("backtest_lab.public_client.requests.get", return_value=response) as get:
        DeltaPublicHistoryClient().fetch("BTCUSD", "1m", 0, 1)
    headers = get.call_args.kwargs["headers"]
    assert "api-key" not in headers and "signature" not in headers


def test_metrics_reports_drawdown_and_sharpe_formula():
    result = summarize([], [1000, 1100, 900], gaps=1)
    assert result["max_drawdown_pct"] < 0
    assert "sqrt" in result["sharpe_formula"]


def test_backtest_lab_does_not_import_paper_engine():
    import backtest_lab.engine as engine
    assert "paper_engine" not in engine.__dict__
