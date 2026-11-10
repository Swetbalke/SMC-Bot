"""
data_client.py  --  STEP 2 (naya version): Delta India LIVE public data

Kya badla (purane version ki galtiyan theek ki):
  1. Symbol: 'BTCUSDT' Delta India par chalta hi nahi. Ab 'BTCUSDT' likha ho tab bhi
     apne aap 'BTCUSD' use hota hai (verified: aapke PC par BTCUSD OK, BTCUSDT FAIL).
  2. NAKLI DATA KABHI NAHI. Pehle API fail hone par 2024 ka synthetic CSV ($44k) aa jata tha.
     Ab fail hone par: thodi der (max_stale_sec) purana asli data, uske baad None
     (engine entries/SL-TP ruk jayenge, koi fake price nahi).
  3. Price taaza: candles ~8 sec, ticker ~3 sec cache (pehle 55 sec).
  4. 5m/15m/1h candles 1-minute asli candles se local banti hain (algo aur chart ka data same).
  5. Koi login/API key/order nahi. Sirf public data.

Purane naam wahi hain (get_candles, get_current_price, is_using_fallback, get_last_error),
isliye paper_engine.py aur dashboard.py ko abhi badalna nahi padta.

Demo (testnet) par chalane ke liye:  set DELTA_ENV=testnet   (CMD mein, chalane se pehle)
Self-check:  python data_client.py
"""
import os
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import requests

from logger_setup import get_logger

logger = get_logger("data_client")

ENVS = {
    "testnet": "https://cdn-ind.testnet.deltaex.org",    # Delta India demo
}
# Delta India ka symbol BTCUSD hai. Purane naam apne aap badal jate hain.
SYMBOL_ALIAS = {
    "BTCUSDT": "BTCUSD", "BTC/USDT": "BTCUSD", "BTC/USD": "BTCUSD", "BTCUSD": "BTCUSD",
    "ETHUSDT": "ETHUSD", "ETH/USDT": "ETHUSD", "ETH/USD": "ETHUSD", "ETHUSD": "ETHUSD",
}
TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}
REQUIRED_COLS = ["time", "open", "high", "low", "close", "volume"]
HEADERS = {"User-Agent": "paper-sim/1.0", "Accept": "application/json"}
LEGACY_RENAME = {"t": "time", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}


class DeltaPublicClient:
    def __init__(self, fallback_csv: Optional[str] = None, env: Optional[str] = None,
                 max_stale_sec: int = 120):
        # fallback_csv sirf purane code ke saath compatible rehne ke liye hai. Ab use NAHI hota.
        # This project is Demo/Testnet-only.  A missing, malformed, or
        # production setting must not select a production endpoint.
        env = (env or os.environ.get("DELTA_ENV", "testnet")).strip().lower()
        self.env = "testnet"
        self.base_url = ENVS[self.env]
        self.max_stale_sec = max_stale_sec
        self.candle_ttl = 8.0
        self.price_ttl = 3.0
        self._lock = threading.RLock()
        self._cache = None            # {"sym", "df" (1m), "ts", "n"}
        self._price_cache = {}        # sym -> (price, ts)
        self._last_error = "" if env == "testnet" else "Non-testnet DELTA_ENV ignored; using testnet"
        self.last_candle_forming = False   # kya df ki aakhri row abhi chal rahi candle hai?
        self.data_age_sec: Optional[float] = None
        self.last_symbol = ""

    # ------------------------------------------------------------------ #
    # Public interface (purane naam)
    # ------------------------------------------------------------------ #
    def get_candles(self, symbol: str = "BTCUSD", resolution: str = "1m",
                    limit: int = 200) -> Optional[pd.DataFrame]:
        """
        Last `limit` candles. Columns: time (UTC), open, high, low, close, volume.
        Aakhri row chal rahi candle ho sakti hai (self.last_candle_forming dekho).
        Data na mile to None (kabhi nakli data nahi).
        """
        mult = TF_MINUTES.get(resolution)
        if mult is None:
            self._last_error = f"Unsupported timeframe: {resolution}"
            return None
        sym = self._sym(symbol)
        need = limit * mult + mult
        with self._lock:
            c = self._cache
            usable = c and c["sym"] == sym and c["n"] >= need
            if usable and (time.time() - c["ts"]) < self.candle_ttl:
                df1 = c["df"]
            else:
                fetch_n = max(need, 200)
                new = self._fetch_1m(sym, fetch_n)
                if new is not None:
                    self._cache = {"sym": sym, "df": new, "ts": time.time(), "n": fetch_n}
                    df1 = new
                elif usable and (time.time() - c["ts"]) <= self.max_stale_sec:
                    logger.warning(f"Delta fetch fail ({self._last_error}). Purana ASLI data use ho raha hai.")
                    df1 = c["df"]
                else:
                    logger.warning(f"Data unavailable: {self._last_error}")
                    self.data_age_sec = None
                    return None
            self._update_freshness(df1, sym)
        if mult == 1:
            return df1.tail(limit).reset_index(drop=True)
        return self._resample(df1, mult).tail(limit).reset_index(drop=True)

    def get_current_price(self, symbol: str = "BTCUSD") -> Optional[float]:
        """Taaza live price. Na mile to None."""
        sym = self._sym(symbol)
        with self._lock:
            pc = self._price_cache.get(sym)
            if pc and (time.time() - pc[1]) < self.price_ttl:
                return pc[0]
            data, err = self._get(f"/v2/tickers/{sym}")
            if not err:
                res = (data or {}).get("result") or {}
                for k in ("mark_price", "close", "spot_price"):
                    try:
                        p = float(res.get(k))
                        if p > 0:
                            self._price_cache[sym] = (p, time.time())
                            return p
                    except (TypeError, ValueError):
                        continue
                err = f"ticker mein price nahi mila (keys: {list(res)[:6]})"
            self._last_error = err
            # ticker fail: taaza ASLI candle ka close (sirf agar bahut purana na ho)
            c = self._cache
            if c and c["sym"] == sym and (time.time() - c["ts"]) <= self.max_stale_sec:
                return float(c["df"]["close"].iloc[-1])
            return None

    def is_using_fallback(self) -> bool:
        return False   # ab koi nakli/CSV fallback hai hi nahi

    def get_last_error(self) -> str:
        return self._last_error

    def is_stale(self, max_age_sec: float = 150.0) -> bool:
        """True = data nahi hai ya aakhri candle bahut purani hai."""
        return self.data_age_sec is None or self.data_age_sec > max_age_sec

    def is_price_fresh(self, symbol: str, max_age_sec: float = 10.0) -> bool:
        """Only a recent ticker response qualifies as an executable market price."""
        sym = self._sym(symbol)
        with self._lock:
            cached = self._price_cache.get(sym)
            return bool(cached and time.time() - cached[1] <= max_age_sec)

    def get_status(self) -> dict:
        return {"env": self.env, "base_url": self.base_url, "symbol": self.last_symbol,
                "data_age_sec": self.data_age_sec, "last_candle_forming": self.last_candle_forming,
                "last_error": self._last_error}

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #
    @staticmethod
    def _sym(symbol: str) -> str:
        s = (symbol or "BTCUSD").strip().upper()
        return SYMBOL_ALIAS.get(s, s)

    def _get(self, path: str, params: Optional[dict] = None):
        try:
            r = requests.get(self.base_url + path, params=params, headers=HEADERS, timeout=10)
        except requests.exceptions.RequestException as e:
            self._last_error = f"{type(e).__name__}: {str(e)[:100]}"
            return None, self._last_error
        if r.status_code != 200:
            self._last_error = f"HTTP {r.status_code}: {r.text[:100].strip()}"
            return None, self._last_error
        try:
            return r.json(), None
        except ValueError:
            self._last_error = "Response JSON nahi hai"
            return None, self._last_error

    def _fetch_1m(self, sym: str, n: int) -> Optional[pd.DataFrame]:
        end = int(time.time())
        params = {"symbol": sym, "resolution": "1m", "start": end - (n + 5) * 60, "end": end}
        data, err = self._get("/v2/history/candles", params)
        if err:
            return None
        rows = (data or {}).get("result") or []
        if not rows:
            self._last_error = f"Delta ne {sym} ki candles nahi di (symbol galat ho sakta hai)"
            return None
        df = pd.DataFrame(rows).rename(columns=LEGACY_RENAME)
        if "volume" not in df.columns:
            df["volume"] = 0.0
        missing = [c for c in REQUIRED_COLS if c not in df.columns]
        if missing:
            self._last_error = f"Response mein columns nahi mile: {missing}"
            return None
        df = df[REQUIRED_COLS].copy()
        df["time"] = pd.to_datetime(pd.to_numeric(df["time"], errors="coerce"), unit="s", utc=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["time", "open", "high", "low", "close"])
        df = df.drop_duplicates("time").sort_values("time").reset_index(drop=True)
        if df.empty:
            self._last_error = "Valid candles nahi mili"
            return None
        self._last_error = ""
        return df

    def _update_freshness(self, df1: pd.DataFrame, sym: str):
        self.last_symbol = sym
        last_open = df1["time"].iloc[-1].timestamp()
        self.data_age_sec = max(0.0, time.time() - last_open)
        self.last_candle_forming = self.data_age_sec < 60

    @staticmethod
    def _resample(df1: pd.DataFrame, mult: int) -> pd.DataFrame:
        g = df1.set_index("time").resample(f"{mult}min", label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        return g.dropna(subset=["close"]).reset_index()


# ---------------------------------------------------------------------- #
# Purane code ke saath compatible (paper_engine.py ise import karta hai).
# YE SIRF TEST DATA HAI -- live algo kabhi isse use nahi karta.
# ---------------------------------------------------------------------- #
def generate_sample_csv(filepath: str = "data/btc_1m_sample.csv", rows: int = 500):
    path = Path(filepath)
    path.parent.mkdir(exist_ok=True)
    if path.exists():
        return
    rng = np.random.default_rng(42)
    closes = 44000.0 * np.exp(np.cumsum(rng.normal(0, 0.0015, rows)))
    opens = np.r_[closes[0], closes[:-1]]
    vp = np.abs(rng.normal(0, 0.002, rows))
    df = pd.DataFrame({
        "time": pd.date_range("2024-01-15 09:00:00", periods=rows, freq="1min").strftime("%Y-%m-%d %H:%M:%S"),
        "open": opens.round(2), "high": (closes * (1 + vp)).round(2),
        "low": (closes * (1 - vp)).round(2), "close": closes.round(2),
        "volume": rng.uniform(0.5, 5.0, rows).round(3)})
    df.to_csv(path, index=False)
    print(f"[data_client] SYNTHETIC test data likha: {filepath} (live trading mein use NAHI hota)")


if __name__ == "__main__":
    print("=== data_client self-check (Delta India, public data) ===")
    cl = DeltaPublicClient()
    print(f"Environment : {cl.env}  ({cl.base_url})")
    price = cl.get_current_price("BTCUSDT")      # purana naam -> BTCUSD
    print(f"Live price  : {price}")
    df = cl.get_candles("BTCUSDT", "1m", 5)
    if df is None:
        print(f"FAIL: candles nahi mili -> {cl.get_last_error()}")
    else:
        print(df.tail(3).to_string())
        st = cl.get_status()
        print(f"Symbol used : {st['symbol']}")
        print(f"Last candle : {st['data_age_sec']:.0f} sec purani | chal rahi candle: {st['last_candle_forming']}")
        df5 = cl.get_candles("BTCUSD", "5m", 3)
        print("5m candles  :", "OK" if df5 is not None and len(df5) else "FAIL")
        print("NAKLI DATA  : nahi (is version mein fallback hai hi nahi)")
    print("=== done ===")
