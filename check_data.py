"""
check_data.py  --  STEP 1: Delta India ka PUBLIC data check

Ye script sirf price/candle DATA padhti hai.
  - Koi login nahi, koi API key nahi, koi order nahi.
  - Sirf aapke PC se Delta India tak internet check karti hai.

Kaise chalana hai (Windows):
  1. Is file ko apne TradingBot folder mein rakho (jahan paper_engine.py hai).
  2. Us folder mein CMD kholo (folder ke address bar mein 'cmd' likh ke Enter).
  3. Ye command chalao:      python check_data.py
  4. Result 'data_check_report.txt' mein bhi save hota hai -- wo file mujhe bhej do.
"""
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

try:
    import requests
except ImportError:
    print("'requests' install nahi hai. Ye chalao:  pip install requests")
    sys.exit(1)

ENVS = [
    ("PRODUCTION (asli market price)", "https://api.india.delta.exchange"),
    ("TESTNET / DEMO", "https://cdn-ind.testnet.deltaex.org"),
]
SYMBOLS = ["BTCUSD", "BTCUSDT"]          # BTCUSD = Delta India, BTCUSDT = aapka purana code
HEADERS = {"User-Agent": "paper-sim-datacheck/1.0", "Accept": "application/json"}
IST = timezone(timedelta(hours=5, minutes=30))
REPORT = "data_check_report.txt"
_lines = []


def out(text=""):
    print(text)
    _lines.append(text)


def get(base, path, params=None):
    """(json, error, ms, headers) return karta hai. Error ho to json=None."""
    t0 = time.time()
    try:
        r = requests.get(base + path, params=params, headers=HEADERS, timeout=12)
    except requests.exceptions.RequestException as e:
        return None, f"{type(e).__name__}: {str(e)[:110]}", 0, {}
    ms = int((time.time() - t0) * 1000)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:140].strip()}", ms, dict(r.headers)
    try:
        return r.json(), None, ms, dict(r.headers)
    except ValueError:
        return None, "Response JSON nahi hai", ms, dict(r.headers)


def clock_skew(headers):
    """Server clock minus PC clock (seconds). Server ke 'Date' header se."""
    try:
        srv = parsedate_to_datetime(headers.get("Date") or headers.get("date")).timestamp()
        return srv - time.time()
    except Exception:
        return None


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def check_products(base):
    data, err, ms, hdr = get(base, "/v2/products", {"contract_types": "perpetual_futures"})
    if err:
        out(f"    Products list : FAIL  ({err})")
        return None
    syms = sorted({p.get("symbol", "") for p in data.get("result", []) if "BTC" in p.get("symbol", "")})
    out(f"    Products list : OK ({ms} ms)  BTC perpetual symbols: {', '.join(syms[:8]) or 'koi nahi mila'}")
    return syms


def check_ticker(base, symbol):
    data, err, ms, hdr = get(base, f"/v2/tickers/{symbol}")
    if err:
        out(f"    Live price    : FAIL  ({err})")
        return None
    res = data.get("result") or {}
    price = next((fnum(res.get(k)) for k in ("mark_price", "close", "spot_price") if fnum(res.get(k))), None)
    if price is None:
        out(f"    Live price    : FAIL  (response mein price nahi mila; keys: {list(res)[:8]})")
        return None
    out(f"    Live price    : OK ({ms} ms)  {symbol} = {price:,.2f}")
    return price


def check_candles(base, symbol, minutes=30):
    end = int(time.time())
    params = {"symbol": symbol, "resolution": "1m", "start": end - minutes * 60, "end": end}
    data, err, ms, hdr = get(base, "/v2/history/candles", params)
    if err:
        out(f"    1m candles    : FAIL  ({err})")
        return False
    rows = data.get("result") or []
    if not rows:
        out("    1m candles    : FAIL  (khali list aayi)")
        return False
    keys = sorted(rows[0].keys())
    tkey = "time" if "time" in rows[0] else ("t" if "t" in rows[0] else None)
    if tkey is None:
        out(f"    1m candles    : FAIL  (time ka column nahi mila; keys: {keys})")
        return False
    rows = sorted(rows, key=lambda r: r[tkey])
    times = [int(r[tkey]) for r in rows]
    skew = clock_skew(hdr)
    server_now = time.time() + (skew or 0.0)
    age = server_now - times[-1]
    missing = sum(1 for a, b in zip(times, times[1:]) if b - a > 60)
    bad = 0
    for r in rows:
        o, h, l, c = (fnum(r.get(k)) for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c) or h < l or h < max(o, c) or l > min(o, c):
            bad += 1
    last = datetime.fromtimestamp(times[-1], IST).strftime("%H:%M:%S")
    first = datetime.fromtimestamp(times[0], IST).strftime("%H:%M:%S")
    if age < 60:
        kind = "CURRENT (chal rahi) candle shamil hai"
    elif age < 150:
        kind = "last CLOSED candle (chal rahi candle abhi shamil nahi)"
    else:
        kind = "PURANA data (STALE)"
    out(f"    1m candles    : OK ({ms} ms)  {len(rows)} candles, {first} -> {last} IST")
    out(f"                    columns: {', '.join(keys)}")
    out(f"                    last candle ki umar: {age:.0f} sec  => {kind}")
    out(f"                    gaps (missing minutes): {missing} | kharab OHLC rows: {bad}")
    if skew is not None:
        flag = "  <-- PC clock galat hai! Windows time sync karo" if abs(skew) > 5 else ""
        out(f"                    PC clock vs server: {skew:+.1f} sec{flag}")
    return age < 150 and bad == 0


def main():
    out("=" * 64)
    out(" DELTA INDIA DATA CHECK   " + datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"))
    out(" (sirf public data -- koi key/login/order nahi)")
    out("=" * 64)
    results, prices = {}, {}
    for name, base in ENVS:
        out(f"\n[{name}]  {base}")
        check_products(base)
        for sym in SYMBOLS:
            out(f"  Symbol: {sym}")
            price = check_ticker(base, sym)
            ok = check_candles(base, sym)
            results[(name, sym)] = bool(price) and ok
            if price:
                prices[(name, sym)] = price
    out("\n" + "=" * 64)
    out(" NATIJA")
    out("=" * 64)
    good = [(n, s) for (n, s), ok in results.items() if ok]
    if not good:
        out(" KOI source poora kaam nahi kar raha.")
        out(" -> Internet/VPN/firewall check karo, ya upar ke FAIL lines mujhe bhejo.")
    for n, s in good:
        out(f" OK  : {n}  symbol={s}")
    for (n, s), ok in results.items():
        if not ok:
            out(f" FAIL: {n}  symbol={s}")
    p_real = prices.get((ENVS[0][0], "BTCUSD"))
    p_demo = prices.get((ENVS[1][0], "BTCUSD"))
    if p_real and p_demo:
        diff = (p_demo - p_real) / p_real * 100
        out(f"\n Asli vs Demo price: {p_real:,.2f} vs {p_demo:,.2f}  (fark {diff:+.2f}%)")
        if abs(diff) > 0.5:
            out(" -> Demo ka price asli market se alag hai. Algo ke liye ASLI (production) data behtar hai.")
    out("\n Is poore output ko ya 'data_check_report.txt' file ko mujhe bhej do.")
    try:
        with open(REPORT, "w", encoding="utf-8") as f:
            f.write("\n".join(_lines))
        print(f"\n(report save hui: {REPORT})")
    except OSError as e:
        print(f"report save nahi hui: {e}")


if __name__ == "__main__":
    main()
