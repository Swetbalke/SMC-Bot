"""Asynchronously export completed trades to a durable workbook."""

import queue
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from logger_setup import get_logger

logger = get_logger("excel_exporter")
EXCEL_PATH = Path(__file__).parent / "data" / "trade_execution_log.xlsx"
COLUMNS = [
    "Trade ID", "Symbol", "Execution Venue", "Delta Order ID", "Direction",
    "Entry Time", "Exit Time", "Entry Price", "Exit Price", "Quantity",
    "Stop Loss", "Take Profit", "Target RRR", "Realized PnL", "Exit Reason",
]

try:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
except ImportError:
    openpyxl = None


class ExcelExporter:
    """Single background writer so Excel never blocks trade processing."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else EXCEL_PATH
        # Reporting must never consume unbounded memory or block risk handling.
        self._queue: queue.Queue = queue.Queue(maxsize=100)
        self._thread = threading.Thread(target=self._worker, daemon=True, name="excel-exporter")
        self._thread.start()

    def queue_export(self, trade_data: Dict[str, Any]) -> None:
        if openpyxl is None:
            logger.warning("Excel export skipped: install openpyxl to enable workbook output")
            return
        try:
            self._queue.put_nowait(dict(trade_data))
        except queue.Full:
            logger.error("Excel export queue is full; trade %s remains in SQLite and was not exported",
                         trade_data.get("trade_id"))

    def close(self, timeout: float = 5.0) -> None:
        if self._thread.is_alive():
            self._queue.put(None)
            self._thread.join(timeout=timeout)

    def _worker(self) -> None:
        while True:
            trade = self._queue.get()
            try:
                if trade is None:
                    return
                try:
                    self._write_row(trade)
                except Exception:
                    logger.exception("Excel export failed; retrying once")
                    time.sleep(2)
                    try:
                        self._write_row(trade)
                    except Exception:
                        logger.exception("Excel export retry failed for trade %s", trade.get("trade_id"))
            finally:
                self._queue.task_done()

    def _write_row(self, trade: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            workbook = openpyxl.load_workbook(self.path)
            sheet = workbook["Trade Log"] if "Trade Log" in workbook.sheetnames else workbook.active
            if sheet.max_row == 0 or sheet.cell(1, 1).value != COLUMNS[0]:
                sheet.insert_rows(1)
                self._style_header(sheet)
        else:
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.title = "Trade Log"
            self._style_header(sheet)

        trade_id = trade.get("trade_id", "")
        if trade_id and any(sheet.cell(row, 1).value == trade_id for row in range(2, sheet.max_row + 1)):
            workbook.close()
            return

        row = [trade.get(key, "") for key in (
            "trade_id", "symbol", "execution_venue", "delta_order_id", "direction",
            "entry_time", "exit_time", "entry_price", "exit_price", "quantity",
            "sl_price", "tp_price", "target_rrr", "realized_pnl", "exit_reason",
        )]
        sheet.append(row)
        venue_fill = "C8E6C9" if trade.get("execution_venue") == "DELTA_TESTNET" else "FFF9C4"
        for cell in sheet[sheet.max_row]:
            cell.fill = PatternFill(fill_type="solid", fgColor=venue_fill)
        workbook.save(self.path)
        workbook.close()

    @staticmethod
    def _style_header(sheet) -> None:
        sheet.append(COLUMNS)
        fill = PatternFill(fill_type="solid", fgColor="1565C0")
        for cell in sheet[1]:
            cell.fill = fill
            cell.font = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(horizontal="center")
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:O1"


_excel_exporter = ExcelExporter()
