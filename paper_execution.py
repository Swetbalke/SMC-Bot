"""Local paper-execution boundary for protective stop lifecycle.

This adapter confirms simulated state only; it does not submit exchange orders.
"""
from math import isfinite

from strategies.stop_manager import validate_stop


class PaperExecutionAdapter:
    def confirm_entry_stop(self, direction, entry_price, stop_price, minimum_distance=0.01):
        error = validate_stop(direction, entry_price, stop_price, minimum_distance)
        if error:
            return {"accepted": False, "error": error}
        return {"accepted": True, "stop_price": round(float(stop_price), 2),
                "confirmation": "simulated"}

    @staticmethod
    def confirm_stop_update(position, proposed_stop):
        """Reject an update that would loosen the trade's last confirmed stop."""
        current = float(position["sl_price"])
        proposed = float(proposed_stop)
        if not isfinite(current) or not isfinite(proposed) or proposed <= 0:
            return {"accepted": False, "error": "Stop update must be finite and greater than zero"}
        if position["direction"] not in {"LONG", "SHORT"}:
            return {"accepted": False, "error": "Unknown position direction"}
        if position["direction"] == "LONG" and proposed < current:
            return {"accepted": False, "error": "BUY stop cannot move downward"}
        if position["direction"] == "SHORT" and proposed > current:
            return {"accepted": False, "error": "SELL stop cannot move upward"}
        return {"accepted": True, "stop_price": proposed, "confirmation": "simulated"}
