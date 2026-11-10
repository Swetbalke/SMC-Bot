"""Small state holder for one pending EMA crossover setup."""
from dataclasses import dataclass
from typing import Optional


@dataclass
class PendingSetup:
    direction: str
    crossover_time: str
    crossover_index: int
    candles_waited: int = 0


class SignalManager:
    def __init__(self):
        self.pending: Optional[PendingSetup] = None
        self.last_emitted_signal_id: Optional[str] = None

    def start(self, direction: str, crossover_time: str, index: int):
        self.pending = PendingSetup(direction, crossover_time, index)

    def clear(self):
        self.pending = None

    def snapshot(self):
        if self.pending is None:
            return None
        return {
            "direction": self.pending.direction,
            "crossover_time": self.pending.crossover_time,
            "candles_waited": self.pending.candles_waited,
        }

