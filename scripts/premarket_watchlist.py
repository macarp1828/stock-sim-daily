"""
寄り付き前(8:45 JST頃)の「注目リスト」を作る。

重要な制約: Yahoo!ファイナンス等の無料データには寄り付き前の気配値(板情報)は
含まれない(TSEの通常取引は9:00開始、それ以前のデータは提供されない)。
そのため、ここでの「予測」は前日終値までの情報(前日の値動き・出来高)だけを
根拠にした参考リストであり、実際の売買判断(9:00-10:00の値動きを見てから
確定する stock_backtest.py 側のロジック)とは別物である。この区別を必ず
レポートに明記すること。
"""
import json, os, datetime
import pandas as pd
from stock_universe import UNIVERSE as UNIVERSE_A
from extra_candidates import EXTRA as UNIVERSE_B
from extra_candidates2 import EXTRA2 as UNIVERSE_C
from theme_candidates import THEME as UNIVERSE_D
from growth_candidates import GROWTH as UNIVERSE_E

UNIVERSE = UNIVERSE_A + UNIVERSE_B + UNIVERSE_C + UNIVERSE_D + UNIVERSE_E
NAME_MAP = dict(UNIVERSE)

BASE_DIR = "/Users/nakamurashunsuke/scripts/stock-sim-daily"
RAW_DIR = os.path.join(BASE_DIR, "stock_raw")


def load_ticker(ticker):
    fn = os.path.join(RAW_DIR, f"{ticker}.json")
    if not os.path.exists(fn):
        return None
    with open(fn) as f:
        data = json.load(f)
    result = data.get("chart", {}).get("result")
    if not result:
        return None
    res = result[0]
    ts = res.get("timestamp")
    if not ts:
        return None
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({
        "ts_utc": pd.to_datetime(ts, unit="s", utc=True),
        "open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"],
        "volume": q.get("volume"),
    })
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    df["volume"] = df["volume"].fillna(0)
    df["ts_jst"] = df["ts_utc"].dt.tz_convert("Asia/Tokyo")
    df["date"] = df["ts_jst"].dt.date
    return df


def main():
    rows = []
    for ticker, name in UNIVERSE:
        df = load_ticker(ticker)
        if df is None or df.empty:
            continue
        days = sorted(df["date"].unique())
        if len(days) < 6:
            continue
        last_day, prev_day = days[-1], days[-2]
        prior5 = days[-7:-1] if len(days) >= 7 else days[:-1]

        last_df = df[df["date"] == last_day]
        prev_df = df[df["date"] == prev_day]
        if last_df.empty or prev_df.empty:
            continue
        last_close = last_df.iloc[-1]["close"]
        prev_close = prev_df.iloc[-1]["close"]
        pct = (last_close - prev_close) / prev_close

        last_vol = last_df["volume"].sum()
        baseline_vols = [df[df["date"] == d]["volume"].sum() for d in prior5]
        baseline_vols = [v for v in baseline_vols if v > 0]
        rel_vol = (last_vol / (sum(baseline_vols) / len(baseline_vols))) if baseline_vols else None

        rows.append({
            "ticker": ticker, "name": name, "last_session_date": last_day.isoformat(),
            "prev_close": round(float(prev_close), 1), "last_close": round(float(last_close), 1),
            "pct_move": round(float(pct) * 100, 2),
            "rel_volume": round(float(rel_vol), 2) if rel_vol else None,
        })

    if not rows:
        print("NO_DATA")
        return

    rows.sort(key=lambda r: (0 if (r["rel_volume"] and r["rel_volume"] >= 1.3) else 1, -abs(r["pct_move"])))
    top = rows[:10]

    out_path = os.path.join(BASE_DIR, "premarket_watchlist.json")
    watch_date = max(r["last_session_date"] for r in rows)
    payload = {"as_of_session": watch_date, "generated_note": "前日終値・前日出来高ベースの参考リスト(気配値ではない)", "watchlist": top}
    with open(out_path, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"watchlist based on session {watch_date}:")
    for r in top:
        print(f"  {r['ticker']} {r['name']}: 前日{r['pct_move']:+.2f}%, 出来高倍率{r['rel_volume']}")


if __name__ == "__main__":
    main()
