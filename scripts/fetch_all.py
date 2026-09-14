"""Refresh 2-minute intraday data for the whole candidate universe.
Always re-fetches (Yahoo's range=1mo window rolls forward daily, so cached
files go stale) -- run this before stock_backtest.py on every scheduled day."""
import requests, time, os
from stock_universe import UNIVERSE as UNIVERSE_A
from extra_candidates import EXTRA as UNIVERSE_B
from extra_candidates2 import EXTRA2 as UNIVERSE_C
from theme_candidates import THEME as UNIVERSE_D
from growth_candidates import GROWTH as UNIVERSE_E

UNIVERSE = UNIVERSE_A + UNIVERSE_B + UNIVERSE_C + UNIVERSE_D + UNIVERSE_E

BASE_DIR = "/Users/nakamurashunsuke/scripts/stock-sim-daily"
RAW_DIR = os.path.join(BASE_DIR, "stock_raw")
HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch(ticker, max_retries=5):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=2m&range=1mo"
    backoff = 1.0
    for attempt in range(max_retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200 and r.json().get("chart", {}).get("error") is None:
                return r.text
        except Exception:
            pass
        time.sleep(backoff)
        backoff = min(backoff * 1.7, 15)
    return None


def main():
    os.makedirs(RAW_DIR, exist_ok=True)
    ok, failed = 0, []
    for ticker, name in UNIVERSE:
        text = fetch(ticker)
        if text is not None:
            with open(os.path.join(RAW_DIR, f"{ticker}.json"), "w") as f:
                f.write(text)
            ok += 1
        else:
            failed.append(ticker)
        time.sleep(0.35)
    print(f"refreshed {ok}/{len(UNIVERSE)} tickers", flush=True)
    if failed:
        print("failed:", failed, flush=True)


if __name__ == "__main__":
    main()
