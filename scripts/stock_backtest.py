"""
株版「仮想敵」バックテスト v2。
方針(ユーザー承認済み):
- 銘柄プールは stock_universe.py + extra_candidates(2).py + theme_candidates.py(自動運転/先進技術テーマ)
- 毎朝9:00-10:00の値動き(その時点までの情報のみ)でプール内をランキング
- v2で追加: (a) その銘柄自身の分足EMA(9/20)によるトレンドフィルター(ブレイク方向とEMAトレンドが
  一致する銘柄のみ対象、FXの15分足フィルターと同じ発想) (b) 直近5営業日の同時刻帯平均に対する
  出来高倍率(相対出来高)を算出し、出来高急増(目安1.3倍以上)を優先
- 9:00-10:00のレンジを60分以内にブレイクした方向にエントリー(次の足の始値で約定)
- SL=ブレイク直前の押し目/戻り安値高値、TP=SLまでの距離×2(FXと同じRR2)
- 同日中に手仕舞い(信用デイトレのため)、未達なら大引け前の最終足で強制決済
- 元手100,000円、1トレード最大損失=資産の1%(単元株の関係で0.5〜1.7%程度に変動)、レバレッジ上限3.3倍
- 100株単位(単元株)。1%リスクとレバレッジ上限の両方を満たす形で単元株を確保できない銘柄はスキップ
"""
import json, os, datetime
import pandas as pd
import numpy as np
from stock_universe import UNIVERSE as UNIVERSE_A
from extra_candidates import EXTRA as UNIVERSE_B
from extra_candidates2 import EXTRA2 as UNIVERSE_C
from theme_candidates import THEME as UNIVERSE_D
from growth_candidates import GROWTH as UNIVERSE_E

UNIVERSE = UNIVERSE_A + UNIVERSE_B + UNIVERSE_C + UNIVERSE_D + UNIVERSE_E

BASE_DIR = "/Users/nakamurashunsuke/Desktop/Claude code/株シミュレーション"
RAW_DIR = os.path.join(BASE_DIR, "stock_raw")
OUT_DIR = BASE_DIR

START_EQUITY = 100_000.0
RISK_PCT = 0.01
MAX_LEVERAGE = 3.3
DECISION_TIME = datetime.time(10, 0)
BREAKOUT_WINDOW = datetime.timedelta(minutes=60)  # only take a breakout that confirms soon after
                                                    # the range forms -- otherwise "range width" as
                                                    # the SL/TP basis is stale relative to where price
                                                    # actually is, producing unreachable TP targets
LAST_ENTRY_TIME = datetime.time(14, 30)
FORCE_CLOSE_TIME = datetime.time(15, 25)
START_DATE = datetime.date(2026, 9, 1)
FINAL_DATE = datetime.date(2026, 9, 30)  # comparison period hard stop
# END_DATE = "yesterday in JST" each time this runs, capped at FINAL_DATE, so a fresh run
# always picks up the most recently completed trading day without manual editing.
_now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
END_DATE = min((_now_jst - datetime.timedelta(days=1)).date(), FINAL_DATE)
MAX_CANDIDATES_TRIED = 8  # try up to this many top movers per day before giving up


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
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    return df


def relative_volume(df, d, decision_ts, lookback_days=5):
    """cumulative volume from day-open to decision time, vs the average of the same
    cumulative-by-time-of-day volume over the preceding `lookback_days` trading days."""
    day_df = df[df["date"] == d]
    up_to = day_df[day_df["ts_jst"] <= decision_ts]
    today_vol = up_to["volume"].sum()
    if today_vol <= 0:
        return None
    prior_days = sorted(dd for dd in df["date"].unique() if dd < d)[-lookback_days:]
    if not prior_days:
        return None
    baseline_vols = []
    for pd_ in prior_days:
        pdf = df[df["date"] == pd_]
        cutoff_time = decision_ts.time()
        pv = pdf[pdf["ts_jst"].dt.time <= cutoff_time]["volume"].sum()
        if pv > 0:
            baseline_vols.append(pv)
    if not baseline_vols:
        return None
    baseline = sum(baseline_vols) / len(baseline_vols)
    if baseline <= 0:
        return None
    return today_vol / baseline


def size_position(equity, entry_price, sl_price):
    """100-share (単元株) lots make strict flooring to the 1%-risk ideal size unworkable for
    most mid/high-priced stocks on a 100,000-yen account (the ideal size is routinely <1 lot).
    Leverage is a hard broker/margin limit so it is always floored down to a whole lot, never
    rounded up past it. Risk is a soft target: round to the nearest lot, but only if the ideal
    (uncapped) size is at least half a lot -- otherwise even 1 lot would blow risk far past 1%,
    so skip the trade rather than force it."""
    dist = abs(entry_price - sl_price)
    if dist <= 0:
        return 0
    risk_jpy = equity * RISK_PCT
    raw_shares = risk_jpy / dist
    max_shares_leverage = equity * MAX_LEVERAGE / entry_price
    leverage_lots = int(max_shares_leverage // 100) * 100
    if leverage_lots < 100:
        return 0
    if raw_shares < 50:
        return 0
    risk_lots = max(int(round(raw_shares / 100)) * 100, 100)
    return min(risk_lots, leverage_lots)


def main():
    data = {}
    for ticker, name in UNIVERSE:
        df = load_ticker(ticker)
        if df is not None and len(df):
            data[ticker] = df
    print(f"loaded {len(data)}/{len(UNIVERSE)} tickers", flush=True)

    name_map = dict(UNIVERSE)
    equity = START_EQUITY
    trades = []
    skipped_days = []
    d = START_DATE
    while d <= END_DATE:
        candidates = []
        for ticker, df in data.items():
            day_df = df[df["date"] == d]
            if len(day_df) < 3:
                continue
            up_to_decision = day_df[day_df["ts_jst"].dt.time <= DECISION_TIME]
            if len(up_to_decision) < 3:
                continue
            day_open = day_df.iloc[0]["open"]
            last_row = up_to_decision.iloc[-1]
            last_close = last_row["close"]
            pct = (last_close - day_open) / day_open
            rng_high = up_to_decision["high"].max()
            rng_low = up_to_decision["low"].min()
            trend_dir = "up" if last_row["ema9"] > last_row["ema20"] else ("down" if last_row["ema9"] < last_row["ema20"] else None)
            want_dir = "up" if pct > 0 else "down"
            if trend_dir != want_dir:
                continue  # skip: today's early move disagrees with this stock's own EMA9/20 trend
            rel_vol = relative_volume(df, d, last_row["ts_jst"])
            candidates.append((ticker, pct, rng_high, rng_low, day_open, last_row["ts_jst"], rel_vol))

        if not candidates:
            skipped_days.append(d.isoformat())
            d += datetime.timedelta(days=1)
            continue

        VOL_SURGE = 1.3
        candidates.sort(key=lambda c: (0 if (c[6] is not None and c[6] >= VOL_SURGE) else 1, -abs(c[1])))
        top3_for_log = [(t, name_map.get(t, t), round(float(p) * 100, 2), round(float(rv), 2) if rv else None)
                         for t, p, *_, rv in candidates[:3]]

        trade_made = False
        for cand_i in range(min(MAX_CANDIDATES_TRIED, len(candidates))):
            sel_ticker, sel_pct, rng_high, rng_low, day_open, decision_ts, sel_relvol = candidates[cand_i]
            direction = "LONG" if sel_pct > 0 else "SHORT"

            day_df = data[sel_ticker]
            day_df = day_df[day_df["date"] == d].reset_index(drop=True)
            after = day_df[day_df["ts_jst"] > decision_ts].reset_index(drop=True)

            window_end = decision_ts + BREAKOUT_WINDOW
            entry_row, break_row = None, None
            for i in range(len(after) - 1):
                row = after.iloc[i]
                if row["ts_jst"] > window_end or row["ts_jst"].time() >= LAST_ENTRY_TIME:
                    break
                if direction == "LONG" and row["close"] > rng_high:
                    entry_row, break_row = after.iloc[i + 1], row
                    break
                if direction == "SHORT" and row["close"] < rng_low:
                    entry_row, break_row = after.iloc[i + 1], row
                    break
            if entry_row is None:
                continue

            entry_price = entry_row["open"]
            # SL from the swing right before the breakout (decision_ts -> break_row), not the
            # original 9:00-10:00 range -- if the breakout confirms well after the range formed,
            # the range's own low/high can already be far away and produce an unreachable 2R target.
            pre_break = day_df[(day_df["ts_jst"] >= decision_ts) & (day_df["ts_jst"] <= break_row["ts_jst"])]
            if direction == "LONG":
                sl_price = pre_break["low"].min()
            else:
                sl_price = pre_break["high"].max()
            if direction == "LONG" and entry_price <= sl_price:
                continue
            if direction == "SHORT" and sl_price <= entry_price:
                continue

            shares = size_position(equity, entry_price, sl_price)
            if shares < 100:
                continue  # can't size this one within risk%/leverage rules even at 1 lot -- try next mover

            dist = abs(entry_price - sl_price)
            tp_price = entry_price + 2 * dist if direction == "LONG" else entry_price - 2 * dist

            entry_idx = day_df[day_df["ts_jst"] == entry_row["ts_jst"]].index[0]
            exit_price, exit_reason, exit_ts = None, None, None
            for j in range(entry_idx, len(day_df)):
                row = day_df.iloc[j]
                if direction == "LONG":
                    if row["low"] <= sl_price:
                        exit_price, exit_reason = sl_price, "SL"
                    elif row["high"] >= tp_price:
                        exit_price, exit_reason = tp_price, "TP"
                else:
                    if row["high"] >= sl_price:
                        exit_price, exit_reason = sl_price, "SL"
                    elif row["low"] <= tp_price:
                        exit_price, exit_reason = tp_price, "TP"
                if exit_price is not None:
                    exit_ts = row["ts_jst"]
                    break
                if row["ts_jst"].time() >= FORCE_CLOSE_TIME:
                    exit_price, exit_reason, exit_ts = row["close"], "大引け前手仕舞い", row["ts_jst"]
                    break
            if exit_price is None:
                last = day_df.iloc[-1]
                exit_price, exit_reason, exit_ts = last["close"], "大引け前手仕舞い", last["ts_jst"]

            pnl = ((exit_price - entry_price) if direction == "LONG" else (entry_price - exit_price)) * shares
            equity += pnl

            trades.append({
                "date": d.isoformat(), "ticker": sel_ticker, "name": name_map.get(sel_ticker, sel_ticker),
                "dir": direction, "entry_time": entry_row["ts_jst"].strftime("%H:%M"),
                "entry": round(float(entry_price), 1), "sl": round(float(sl_price), 1), "tp": round(float(tp_price), 1),
                "shares": int(shares), "exit_time": exit_ts.strftime("%H:%M"), "exit_price": round(float(exit_price), 1),
                "exit_reason": exit_reason, "pnl": round(float(pnl), 1), "equity_after": round(float(equity), 1),
                "reason": {
                    "decision_time": decision_ts.strftime("%H:%M"),
                    "day_open": round(float(day_open), 1),
                    "pct_move_at_decision": round(float(sel_pct) * 100, 2),
                    "range_high": round(float(rng_high), 1), "range_low": round(float(rng_low), 1),
                    "breakout_time": break_row["ts_jst"].strftime("%H:%M"),
                    "breakout_close": round(float(break_row["close"]), 1),
                    "rank_among_movers": cand_i + 1,
                    "rel_volume": round(float(sel_relvol), 2) if sel_relvol else None,
                    "top3": top3_for_log,
                },
            })
            trade_made = True
            break

        if not trade_made:
            skipped_days.append(d.isoformat())
        d += datetime.timedelta(days=1)

    trades_df = pd.DataFrame(trades)
    trades_df.to_pickle(os.path.join(OUT_DIR, "stock_trades.pkl"))
    print(f"total trades: {len(trades_df)}")
    if len(trades_df):
        print(trades_df[["date", "ticker", "name", "dir", "entry", "sl", "tp", "shares",
                          "exit_price", "exit_reason", "pnl", "equity_after"]].to_string())
    print("no-trade days:", skipped_days)
    print("final equity:", equity, "cum pnl:", equity - START_EQUITY)


if __name__ == "__main__":
    main()
