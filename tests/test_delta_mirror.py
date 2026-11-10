from unittest.mock import patch

from paper_engine import PaperEngine


def test_delta_failure_never_raises_into_paper_flow():
    engine = PaperEngine()
    engine._delta_client = None
    with patch.object(engine, "_connect_delta_demo"), \
         patch("paper_engine.update_open_delta_mirror") as update:
        engine._mirror_delta_entry(42, "LONG", 0.01, "BTCUSD")
    update.assert_called()
    assert update.call_args.args[1] == "FAILED"


def test_delta_circuit_breaker_skips_without_submitting():
    engine = PaperEngine()
    engine._delta_paused_until = 9999999999
    with patch("paper_engine.update_open_delta_mirror") as update:
        engine._mirror_delta_entry(42, "LONG", 0.01, "BTCUSD")
    assert update.call_args.args[1] == "SKIPPED"


def test_delta_rejection_is_recorded_without_raising():
    engine = PaperEngine()
    with patch("paper_engine.update_open_delta_mirror") as update:
        engine._record_delta_failure(42, "Order rejected: insufficient margin")
    assert update.call_args.args[1] == "REJECTED"
