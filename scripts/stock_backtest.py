"""
株版「仮想敵」バックテスト v3(日中複数回売買・静観ロジック対応)。
方針(ユーザー承認済み、2026-09-13合意):
- 銘柄プールは stock_universe.py + extra_candidates(2).py + theme_candidates.py + growth_candidates.py
- 同時保有は1ポジションのみ(FXと同じ規律)。ただし1日1トレードには限定せず、
  ポジション決済後は条件が整い次第、同日中に何度でも再エントリーする。
- 対象銘柄は日中で入れ替え可(静観中に他の銘柄が明確に強くなればそちらに乗り換える)。
- 30分ごとに(idle時)候補を再ランキングする:
  - トレンド: その銘柄自身の分足EMA9/EMA20が、当日の値動き方向と一致すること
  - 出来高: 直近5営業日の同時刻帯平均に対する相対出来高(目安1.3倍以上を優先)
  - 値動き: 始値からの騰落率(大きい順)
  - トレンド不一致 or 出来高・値動きで基準を満たす銘柄が無ければ「静観」(その理由を記録し、
    次のスキャンタイミングまで新規エントリーしない)
- エントリー: 直近30分のレンジを30分以内にブレイクした方向に、次の足の始値で成行
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
MAX_LEVERAGE = 3.3  # 1%リスクルールは撤廃済み(2026-09-13)。sizingは常にこの上限いっぱいを狙う。
FIRST_SCAN_TIME = datetime.time(10, 0)
SCAN_STEP = datetime.timedelta(minutes=30)      # re-rank candidates this often while idle
ROLL_RANGE = datetime.timedelta(minutes=30)     # rolling lookback used as the breakout reference range
BREAKOUT_WINDOW = datetime.timedelta(minutes=30)  # how long to wait for a breakout from a given scan point
LAST_ENTRY_TIME = datetime.time(14, 30)
FORCE_CLOSE_TIME = datetime.time(15, 25)
START_DATE = datetime.date(2026, 9, 1)
FINAL_DATE = datetime.date(2026, 9, 30)
_now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
END_DATE = min((_now_jst - datetime.timedelta(days=1)).date(), FINAL_DATE)
MAX_CANDIDATES_TRIED = 8
VOL_SURGE = 1.3
MAX_TRADES_PER_DAY = 1  # see rationale at the break below -- data-driven, 2026-09-13


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
    """1%リスクルールは撤廃(2026-09-13、ユーザー指示)。楽天証券の信用取引を前提に、
    SL幅に関係なく毎回レバレッジ上限(3.3倍)いっぱいで建てる。これによりSL幅が狭い
    トレードは小さい損失、SL幅が広いトレードは資産の数%規模の損失もあり得る
    (リスクは資産%で一定ではなく、レバレッジ%を一定にする設計に変更)。"""
    if entry_price <= 0:
        return 0
    max_shares_leverage = equity * MAX_LEVERAGE / entry_price
    leverage_lots = int(max_shares_leverage // 100) * 100
    if leverage_lots < 100:
        return 0
    return leverage_lots


def rank_candidates_at(data, d, scan_ts):
    """Trend-qualified candidates as of scan_ts that clear the volume-surge gate
    (rel_vol >= VOL_SURGE is a hard requirement, not a soft preference -- a candidate
    with a bigger price move but no real volume surge is excluded outright), then
    ranked by the volume-surge magnitude itself, biggest first. Returns list of dicts
    (possibly empty -> standby). Previously this sorted by |price move| with volume
    only as a soft tie-break, which didn't actually prioritize "increasing volume"
    stocks despite being described that way -- fixed 2026-09-13 per user feedback."""
    out = []
    for ticker, df in data.items():
        day_df = df[df["date"] == d]
        if len(day_df) < 3:
            continue
        up_to = day_df[day_df["ts_jst"] <= scan_ts]
        if len(up_to) < 3:
            continue
        day_open = day_df.iloc[0]["open"]
        last_row = up_to.iloc[-1]
        pct = (last_row["close"] - day_open) / day_open
        trend_dir = "up" if last_row["ema9"] > last_row["ema20"] else ("down" if last_row["ema9"] < last_row["ema20"] else None)
        want_dir = "up" if pct > 0 else "down"
        if trend_dir != want_dir or abs(pct) < 1e-6:
            continue
        rel_vol = relative_volume(df, d, last_row["ts_jst"])
        if rel_vol is None or rel_vol < VOL_SURGE:
            continue  # hard gate: must actually be a volume surge, not just a big mover
        range_df = up_to[up_to["ts_jst"] > scan_ts - ROLL_RANGE]
        if len(range_df) < 2:
            continue
        out.append({
            "ticker": ticker, "pct": pct, "trend_dir": trend_dir, "rel_vol": rel_vol,
            "range_high": range_df["high"].max(), "range_low": range_df["low"].min(),
            "day_open": day_open, "scan_ts": last_row["ts_jst"],
        })
    out.sort(key=lambda c: -c["rel_vol"])
    return out


def try_enter(data, d, cand, scan_ts, equity, name_map):
    """Look for a rolling-range breakout for this candidate within BREAKOUT_WINDOW of scan_ts."""
    ticker = cand["ticker"]
    direction = "LONG" if cand["pct"] > 0 else "SHORT"
    day_df = data[ticker]
    day_df = day_df[day_df["date"] == d].reset_index(drop=True)
    after = day_df[day_df["ts_jst"] > scan_ts].reset_index(drop=True)
    window_end = scan_ts + BREAKOUT_WINDOW

    entry_row, break_row = None, None
    for i in range(len(after) - 1):
        row = after.iloc[i]
        if row["ts_jst"] > window_end or row["ts_jst"].time() >= LAST_ENTRY_TIME:
            break
        if direction == "LONG" and row["close"] > cand["range_high"]:
            entry_row, break_row = after.iloc[i + 1], row
            break
        if direction == "SHORT" and row["close"] < cand["range_low"]:
            entry_row, break_row = after.iloc[i + 1], row
            break
    if entry_row is None:
        return None, None

    entry_price = entry_row["open"]
    pre_break = day_df[(day_df["ts_jst"] >= scan_ts) & (day_df["ts_jst"] <= break_row["ts_jst"])]
    sl_price = pre_break["low"].min() if direction == "LONG" else pre_break["high"].max()
    if direction == "LONG" and entry_price <= sl_price:
        return None, None
    if direction == "SHORT" and sl_price <= entry_price:
        return None, None

    shares = size_position(equity, entry_price, sl_price)
    if shares < 100:
        return None, None

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

    trade = {
        "date": d.isoformat(), "ticker": ticker, "name": name_map.get(ticker, ticker),
        "dir": direction, "entry_time": entry_row["ts_jst"].strftime("%H:%M"),
        "entry": round(float(entry_price), 1), "sl": round(float(sl_price), 1), "tp": round(float(tp_price), 1),
        "shares": int(shares), "exit_time": exit_ts.strftime("%H:%M"), "exit_price": round(float(exit_price), 1),
        "exit_reason": exit_reason, "pnl": round(float(pnl), 1),
        "reason": {
            "decision_time": cand["scan_ts"].strftime("%H:%M"),
            "day_open": round(float(cand["day_open"]), 1),
            "pct_move_at_decision": round(float(cand["pct"]) * 100, 2),
            "range_high": round(float(cand["range_high"]), 1), "range_low": round(float(cand["range_low"]), 1),
            "breakout_time": break_row["ts_jst"].strftime("%H:%M"),
            "breakout_close": round(float(break_row["close"]), 1),
            "rel_volume": round(float(cand["rel_vol"]), 2) if cand["rel_vol"] else None,
        },
    }
    return trade, exit_ts


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
        day_trade_count = 0
        prior_exit = None  # (ticker, exit_reason, exit_ts) of the previous trade today, for standby narration
        scan_ts = None
        any_scan_had_data = False

        # establish the first scan timestamp: the first bar at/after FIRST_SCAN_TIME
        any_ticker_df = None
        for df in data.values():
            day_df = df[df["date"] == d]
            if len(day_df):
                any_ticker_df = day_df
                break
        if any_ticker_df is None:
            d += datetime.timedelta(days=1)
            continue
        first_bars = any_ticker_df[any_ticker_df["ts_jst"].dt.time >= FIRST_SCAN_TIME]
        if not len(first_bars):
            d += datetime.timedelta(days=1)
            continue
        scan_ts = first_bars.iloc[0]["ts_jst"]

        standby_notes = []
        while scan_ts.time() < LAST_ENTRY_TIME:
            candidates = rank_candidates_at(data, d, scan_ts)
            if not candidates:
                standby_notes.append(f"{scan_ts.strftime('%H:%M')}時点: トレンド一致銘柄なし、静観継続")
                scan_ts = scan_ts + SCAN_STEP
                continue

            any_scan_had_data = True
            top3 = [{"ticker": c["ticker"], "name": name_map.get(c["ticker"], c["ticker"]),
                     "pct": round(float(c["pct"]) * 100, 2),
                     "relvol": round(float(c["rel_vol"]), 2) if c["rel_vol"] else None} for c in candidates[:3]]

            found = None
            tried_tickers = []
            for cand in candidates[:MAX_CANDIDATES_TRIED]:
                tried_tickers.append(cand["ticker"])
                trade, exit_ts = try_enter(data, d, cand, scan_ts, equity, name_map)
                if trade is not None:
                    found, found_exit_ts = trade, exit_ts
                    break

            if found is None:
                standby_notes.append(
                    f"{scan_ts.strftime('%H:%M')}時点: 候補{','.join(name_map.get(t,t) for t in tried_tickers[:3])}"
                    f"を検討したがブレイク不成立/資金管理条件未達、静観継続"
                )
                scan_ts = scan_ts + SCAN_STEP
                continue

            # build the standby/gap narration for this trade
            if prior_exit is not None:
                gap_reason = (f"前トレード({prior_exit[0]})が{prior_exit[1]}で決済({prior_exit[2]})後、"
                              f"{'; '.join(standby_notes) if standby_notes else '直後に新条件成立'}。"
                              f"{found['ticker']}が新たにトレンド一致+出来高+ブレイク条件を満たしたため再エントリー。")
            else:
                gap_reason = (f"当日始動(9:00始値基準)。{'; '.join(standby_notes) if standby_notes else '10:00の初回スキャンで条件成立'}。")
            found["reason"]["gap_reason"] = gap_reason
            found["reason"]["top3"] = top3
            standby_notes = []

            equity += found["pnl"]
            found["equity_after"] = round(float(equity), 1)
            trades.append(found)
            day_trade_count += 1
            prior_exit = (found["ticker"], found["exit_reason"], found["exit_time"])
            scan_ts = found_exit_ts

            if day_trade_count >= MAX_TRADES_PER_DAY:
                # Backtest evidence (2026-09-13, v4 vs v3): same-day re-entries averaged a
                # loss (-57/trade over 21 trades) while each day's first qualifying trade
                # averaged a solid win (+606/trade over 9 trades). Chasing more trades per
                # day was diluting -- not growing -- total profit, so re-entries are capped.
                break

        if day_trade_count == 0:
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
