"""
claudecodebot — NQ/MNQ Futures Trading Bot
Strategy: Window-based analysis 9:30 AM - 4:00 PM EST
Data: yfinance (NQ=F)
"""
from __future__ import annotations

import os
import sys
import time
import json
import re
import requests
import pytz
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ─── CONFIG ─────────────────────────────────────────────────────────────────
POLYGON_API_KEY   = os.getenv("POLYGON_API_KEY", "")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

CONTRACTS         = int(os.getenv("CONTRACTS", "5"))
INITIAL_SL_PTS    = float(os.getenv("INITIAL_SL_PTS", "30"))
REDUCE_SL_AT_PTS  = float(os.getenv("REDUCE_SL_AT_PTS", "15"))
REDUCE_SL_TO_PTS  = float(os.getenv("REDUCE_SL_TO_PTS", "5"))   # +5 in profit
TRAIL_START_PTS   = float(os.getenv("TRAIL_START_PTS", "25"))
TRAIL_BEHIND_PTS  = float(os.getenv("TRAIL_BEHIND_PTS", "10"))
MIN_WINDOW_MOVE   = float(os.getenv("MIN_WINDOW_MOVE", "10"))     # min pts to consider a move
MIN_WICK_PTS      = float(os.getenv("MIN_WICK_PTS", "2"))         # min wick for reversal entry
CONSEC_CANDLES    = int(os.getenv("CONSEC_CANDLES", "3"))
MAX_DAILY_LOSS    = float(os.getenv("MAX_DAILY_LOSS", "2000"))
MAX_DAILY_PROFIT  = float(os.getenv("MAX_DAILY_PROFIT", "1580"))

PAPER_MODE        = os.getenv("PAPER_MODE", "true").lower() == "true"
EST               = pytz.timezone("America/New_York")
TICKER            = "NQ"   # Polygon futures ticker

# ─── TRADING WINDOWS ────────────────────────────────────────────────────────
WINDOWS = {
    "nine_thirty":   ("09:30", "09:37"),
    "ten_am":        ("10:00", "10:07"),
    "ten_thirty":    ("10:30", "10:37"),
    "eleven_am":     ("11:00", "11:07"),
    "eleven_thirty": ("11:30", "11:37"),
    "twelve_pm":     ("12:00", "12:07"),
    "two_pm":        ("13:55", "14:10"),
    "three_pm":      ("15:00", "15:07"),
    "three_thirty":  ("15:30", "15:37"),
}

# ─── DAILY STATE ────────────────────────────────────────────────────────────
daily_state = {
    "date":            None,
    "daily_pnl":       0.0,
    "active_trade":    None,
    "trade_history":   [],
    "window_done":     {},
    "open_bias":       None,   # "bull" or "bear" — premarket direction
    "premarket_range": 0.0,
    "overnight_dir":   None,
    "gap_pts":         0.0,
    "prior_close":     None,
    "prev_day_high":   None,
    "prev_day_low":    None,
    "asia_high":       None,
    "asia_low":        None,
    "london_high":     None,
    "london_low":      None,
    "midnight_open":   None,
    "eight_thirty_open": None,
    "nine_thirty_open":  None,
    "four_hr_6am_open":  None,
    "precandles_dir":  None,   # direction of last 3 candles before 9:30
}

STATE_FILE = Path(os.getenv("STATE_FILE", "/tmp/claudecodebot_state.json"))

# ─── TELEGRAM ───────────────────────────────────────────────────────────────
def send_telegram(msg: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"  Telegram error: {e}")

# ─── YFINANCE DATA ───────────────────────────────────────────────────────────
def get_candles(minutes: int = 1, count: int = 120) -> list[dict]:
    """Get recent 1-min NQ candles via yfinance"""
    try:
        df = yf.Ticker("NQ=F").history(period="1d", interval="1m")
        if df is None or df.empty:
            return []
        df = df.tail(count)
        candles = []
        for idx, row in df.iterrows():
            ts = pd.Timestamp(idx).tz_convert(EST)
            candles.append({
                "time":   ts.strftime("%H:%M"),
                "open":   float(row["Open"]),
                "high":   float(row["High"]),
                "low":    float(row["Low"]),
                "close":  float(row["Close"]),
                "volume": float(row["Volume"]),
            })
        return candles
    except Exception as e:
        print(f"  yfinance error: {e}")
        return []

def get_daily_levels() -> dict:
    """Fetch prior day high/low/close and intraday session levels via yfinance"""
    levels = {}
    try:
        # Daily data for prior close and prev day high/low
        df_daily = yf.Ticker("NQ=F").history(period="5d", interval="1d")
        if df_daily is not None and len(df_daily) >= 2:
            prev = df_daily.iloc[-2]
            levels["prev_day_high"] = float(prev["High"])
            levels["prev_day_low"]  = float(prev["Low"])
            levels["prior_close"]   = float(prev["Close"])
    except Exception as e:
        print(f"  Daily levels error: {e}")

    try:
        # Intraday 1-min for session levels
        df = yf.Ticker("NQ=F").history(period="1d", interval="1m")
        if df is None or df.empty:
            return levels

        candles = []
        for idx, row in df.iterrows():
            ts = pd.Timestamp(idx).tz_convert(EST)
            candles.append({
                "ts": ts, "h": float(row["h"]) if "h" in row else float(row["High"]),
                "l": float(row["l"]) if "l" in row else float(row["Low"]),
                "o": float(row["o"]) if "o" in row else float(row["Open"]),
                "c": float(row["c"]) if "c" in row else float(row["Close"]),
            })

        def sess(sh, sm, eh, em):
            return [c for c in candles
                    if (c["ts"].hour > sh or (c["ts"].hour == sh and c["ts"].minute >= sm))
                    and (c["ts"].hour < eh or (c["ts"].hour == eh and c["ts"].minute <= em))]

        # Midnight open
        mid = [c for c in candles if c["ts"].hour == 0 and c["ts"].minute == 0]
        if mid:
            levels["midnight_open"] = mid[0]["o"]

        # Asia 6PM-2AM
        asia = sess(18, 0, 2, 0)
        if asia:
            levels["asia_high"] = max(c["h"] for c in asia)
            levels["asia_low"]  = min(c["l"] for c in asia)

        # London 3AM-9:29AM
        london = sess(3, 0, 9, 29)
        if london:
            levels["london_high"] = max(c["h"] for c in london)
            levels["london_low"]  = min(c["l"] for c in london)

        # 8:30 open
        et = [c for c in candles if c["ts"].hour == 8 and c["ts"].minute == 30]
        if et:
            levels["eight_thirty_open"] = et[0]["o"]

        # 6AM open
        six = [c for c in candles if c["ts"].hour == 6 and c["ts"].minute == 0]
        if six:
            levels["four_hr_6am_open"] = six[0]["o"]

        # Premarket bias 4AM-9:29AM
        pm = sess(4, 0, 9, 29)
        if len(pm) >= 2:
            levels["premarket_range"] = round(abs(pm[-1]["c"] - pm[0]["o"]), 2)
            levels["open_bias"] = "bull" if pm[-1]["c"] > pm[0]["o"] else "bear"

        # Gap
        if levels.get("prior_close") and candles:
            levels["gap_pts"] = round(candles[0]["o"] - levels["prior_close"], 2)
            levels["overnight_dir"] = "bull" if candles[-1]["c"] > levels["prior_close"] else "bear"

        # Pre-open 3 candles before 9:30
        pre = [c for c in candles if c["ts"].hour == 9 and c["ts"].minute < 30]
        if len(pre) >= 3:
            dirs = ["bull" if c["c"] >= c["o"] else "bear" for c in pre[-3:]]
            levels["precandles_dir"] = "bull" if all(d=="bull" for d in dirs) \
                else "bear" if all(d=="bear" for d in dirs) else "mixed"

    except Exception as e:
        print(f"  Intraday levels error: {e}")

    return levels

# ─── CANDLE ANALYSIS ────────────────────────────────────────────────────────
def candle_dir(c: dict) -> str:
    return "bull" if c["close"] >= c["open"] else "bear"

def wick_top(c: dict) -> float:
    return round(c["high"] - max(c["open"], c["close"]), 2)

def wick_bot(c: dict) -> float:
    return round(min(c["open"], c["close"]) - c["low"], 2)

def body_size(c: dict) -> float:
    return round(abs(c["close"] - c["open"]), 2)

def consecutive_candles(candles: list[dict], n: int = 3) -> str | None:
    """Return direction if last n candles are all same color, else None"""
    if len(candles) < n:
        return None
    last = candles[-n:]
    dirs = [candle_dir(c) for c in last]
    if all(d == "bull" for d in dirs):
        return "bull"
    if all(d == "bear" for d in dirs):
        return "bear"
    return None

def detect_failed_auction(candles: list[dict]) -> str | None:
    """Price makes new high/low then immediately reverses — strong reversal signal"""
    if len(candles) < 3:
        return None
    recent_high = max(c["high"] for c in candles[-10:])
    recent_low  = min(c["low"]  for c in candles[-10:])
    last = candles[-1]
    if last["high"] >= recent_high and last["close"] < last["open"]:
        return "bear"  # failed auction at high
    if last["low"] <= recent_low and last["close"] > last["open"]:
        return "bull"  # failed auction at low
    return None

def window_move(candles: list[dict], lookback: int = 7) -> tuple[float, str]:
    """Measure pts move and direction over last N candles"""
    if not candles:
        return 0.0, "flat"
    window = candles[-lookback:]
    open_  = window[0]["open"]
    close_ = window[-1]["close"]
    high_  = max(c["high"] for c in window)
    low_   = min(c["low"]  for c in window)
    net    = round(close_ - open_, 2)
    total  = round(high_ - low_, 2)
    direction = "bull" if net > 0 else "bear" if net < 0 else "flat"
    return total, direction

def nearest_key_level(price: float, levels: dict) -> tuple[float, str, float]:
    """Find nearest key level to current price"""
    candidates = []
    level_map = {
        "prev_day_high":    levels.get("prev_day_high"),
        "prev_day_low":     levels.get("prev_day_low"),
        "prior_close":      levels.get("prior_close"),
        "asia_high":        levels.get("asia_high"),
        "asia_low":         levels.get("asia_low"),
        "london_high":      levels.get("london_high"),
        "london_low":       levels.get("london_low"),
        "midnight_open":    levels.get("midnight_open"),
        "eight_thirty_open": levels.get("eight_thirty_open"),
        "four_hr_6am_open": levels.get("four_hr_6am_open"),
    }
    for name, val in level_map.items():
        if val:
            candidates.append((abs(price - val), val, name))
    if not candidates:
        # fallback to round numbers
        base = round(price / 25) * 25
        return base, "round_number", abs(price - base)
    candidates.sort()
    dist, val, name = candidates[0]
    return val, name, round(dist, 2)

# ─── SETUP DETECTION ────────────────────────────────────────────────────────
def analyze_window(window_name: str, candles_1m: list[dict]) -> dict | None:
    """
    Apply all strategy rules to determine if a trade setup exists.
    Returns setup dict if trade should be taken, None if skip.
    """
    if not candles_1m or len(candles_1m) < 15:
        return None

    now_est       = datetime.now(EST)
    price         = candles_1m[-1]["close"]
    move_pts, move_dir = window_move(candles_1m, lookback=7)
    consec        = consecutive_candles(candles_1m, CONSEC_CANDLES)
    failed_auc    = detect_failed_auction(candles_1m)
    last          = candles_1m[-1]
    prev          = candles_1m[-2] if len(candles_1m) >= 2 else last

    # Key levels
    lvl_price, lvl_name, lvl_dist = nearest_key_level(
        price, {k: daily_state.get(k) for k in [
            "prev_day_high","prev_day_low","prior_close",
            "asia_high","asia_low","london_high","london_low",
            "midnight_open","eight_thirty_open","four_hr_6am_open"
        ]}
    )

    # 5m and 15m context
    candles_5m  = candles_1m[::5]  # approximate from 1m
    move_5m, dir_5m = window_move(candles_5m, lookback=3)

    setup = {
        "window":      window_name,
        "time":        now_est.strftime("%H:%M"),
        "price":       price,
        "move_pts":    move_pts,
        "move_dir":    move_dir,
        "consec":      consec,
        "failed_auc":  failed_auc,
        "wick_top":    wick_top(last),
        "wick_bot":    wick_bot(last),
        "body":        body_size(last),
        "key_level":   lvl_price,
        "key_name":    lvl_name,
        "key_dist":    lvl_dist,
        "open_bias":   daily_state.get("open_bias"),
        "precandles":  daily_state.get("precandles_dir"),
        "gap_pts":     daily_state.get("gap_pts", 0),
        "overnight":   daily_state.get("overnight_dir"),
        "move_5m":     move_5m,
        "dir_5m":      dir_5m,
        "direction":   None,
        "reason":      [],
        "confidence":  0,
    }

    reasons  = []
    conf     = 0
    direction = None

    # ── RULE 1: Failed auction (strongest signal) ──────────────────────────
    if failed_auc:
        direction = failed_auc
        reasons.append(f"Failed auction at {'high' if failed_auc == 'bear' else 'low'}")
        conf += 3

    # ── RULE 2: Big move with tiny/no wick — continuation ─────────────────
    if move_pts >= MIN_WINDOW_MOVE:
        if move_dir == "bull" and wick_top(last) <= MIN_WICK_PTS:
            direction = "bull"
            reasons.append(f"Strong bull move {move_pts:.1f}pts, no top wick — continuation")
            conf += 3
        elif move_dir == "bear" and wick_bot(last) <= MIN_WICK_PTS:
            direction = "bear"
            reasons.append(f"Strong bear move {move_pts:.1f}pts, no bot wick — continuation")
            conf += 3

    # ── RULE 3: Wick reversal ──────────────────────────────────────────────
    if move_pts >= MIN_WINDOW_MOVE and not direction:
        if move_dir == "bull" and wick_top(last) > body_size(last):
            direction = "bear"
            reasons.append(f"Wick reversal: top wick {wick_top(last):.1f}pts > body {body_size(last):.1f}pts after bull move")
            conf += 2
        elif move_dir == "bear" and wick_bot(last) > body_size(last):
            direction = "bull"
            reasons.append(f"Wick reversal: bot wick {wick_bot(last):.1f}pts > body {body_size(last):.1f}pts after bear move")
            conf += 2

    # ── RULE 4: 3-candle reversal ─────────────────────────────────────────
    if consec:
        reversal_dir = "bear" if consec == "bull" else "bull"
        if not direction:
            direction = reversal_dir
        if direction == reversal_dir:
            reasons.append(f"{CONSEC_CANDLES} consecutive {consec} candles — reversal due")
            conf += 2

    # ── RULE 5: 10am — 4hr candle open context ────────────────────────────
    if window_name == "ten_am":
        four_hr_open = daily_state.get("four_hr_6am_open")
        if four_hr_open:
            four_hr_move = price - four_hr_open
            if abs(four_hr_move) >= MIN_WINDOW_MOVE:
                four_hr_dir = "bull" if four_hr_move > 0 else "bear"
                # If big move then reversal — trade reversal
                if move_dir != four_hr_dir and move_pts >= MIN_WINDOW_MOVE:
                    if not direction:
                        direction = move_dir
                    reasons.append(f"10am: 4hr was {four_hr_dir} {abs(four_hr_move):.0f}pts, reversing {move_dir}")
                    conf += 2

    # ── RULE 6: 10:30am — reversal of 9:30am move ────────────────────────
    if window_name == "ten_thirty":
        nine_thirty_open = daily_state.get("nine_thirty_open")
        if nine_thirty_open:
            morning_move = price - nine_thirty_open
            morning_dir  = "bull" if morning_move > 0 else "bear"
            # Expect reversal of morning direction
            expected_reversal = "bear" if morning_dir == "bull" else "bull"
            if move_dir == expected_reversal:
                if not direction:
                    direction = expected_reversal
                reasons.append(f"10:30: reversing 9:30 {morning_dir} move of {abs(morning_move):.0f}pts")
                conf += 2

    # ── RULE 7: Pre-open 3-candle direction ───────────────────────────────
    precandles = daily_state.get("precandles_dir")
    if precandles and precandles != "mixed" and window_name == "nine_thirty":
        reversal_of_pre = "bear" if precandles == "bull" else "bull"
        if not direction:
            direction = reversal_of_pre
        if direction == reversal_of_pre:
            reasons.append(f"Pre-open 3 candles were {precandles} — fade at open")
            conf += 2

    # ── RULE 8: Gap fill bias ─────────────────────────────────────────────
    gap = daily_state.get("gap_pts", 0)
    if abs(gap) >= 20:
        gap_fill_dir = "bear" if gap > 0 else "bull"
        if not direction:
            direction = gap_fill_dir
        if direction == gap_fill_dir:
            reasons.append(f"Gap {gap:+.0f}pts — gap fill bias {gap_fill_dir}")
            conf += 1

    # ── RULE 9: Key level confluence ─────────────────────────────────────
    if lvl_dist <= 15:
        reasons.append(f"Near key level {lvl_name} ({lvl_price:.2f}) {lvl_dist:.1f}pts away")
        conf += 1

    # ── RULE 10: Premarket bias alignment ────────────────────────────────
    open_bias = daily_state.get("open_bias")
    if open_bias and direction == open_bias:
        reasons.append(f"Aligned with premarket bias ({open_bias})")
        conf += 1
    elif open_bias and direction and direction != open_bias:
        reasons.append(f"Against premarket bias ({open_bias}) — counter-trend")
        conf -= 1

    # ── RULE 11: VIX-style: large move = high volatility, widen expectations
    if move_pts > 80:
        reasons.append(f"High volatility day ({move_pts:.0f}pts range) — bigger moves expected")
        conf += 1

    # ── No direction found ────────────────────────────────────────────────
    if not direction or conf < 2:
        return None

    setup["direction"]  = direction
    setup["reason"]     = reasons
    setup["confidence"] = conf

    return setup

# ─── CLAUDE MARKET READ ──────────────────────────────────────────────────────
def claude_market_read(setup: dict) -> str:
    """Use Claude only to narrate the market context, not to override rules"""
    if not ANTHROPIC_API_KEY:
        return ""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = f"""You are an NQ futures market analyst. Describe in 2 sentences what the market is doing right now based on these facts. Do not suggest a trade direction — just describe the context.

Window: {setup['window']} at {setup['time']} EST
Price: {setup['price']:.2f}
Move this window: {setup['move_pts']:.1f}pts {setup['move_dir']}
Premarket bias: {setup['open_bias']}
Overnight direction: {setup['overnight']}
Gap from prior close: {setup['gap_pts']:+.1f}pts
Nearest key level: {setup['key_name']} at {setup['key_level']:.2f} ({setup['key_dist']:.1f}pts away)
Pre-open 3 candles: {setup['precandles']}"""

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"  Claude read error: {e}")
        return ""

# ─── STOP MANAGEMENT ────────────────────────────────────────────────────────
def update_stop(trade: dict, price: float) -> float | None:
    """Returns new stop if it should move, else None"""
    entry     = trade["entry"]
    direction = trade["direction"]
    current_stop = trade["stop"]

    pts_profit = (price - entry) if direction == "bull" else (entry - price)

    # Update high/low water
    if direction == "bull":
        trade["high_water"] = max(trade.get("high_water", entry), price)
    else:
        trade["low_water"] = min(trade.get("low_water", entry), price)

    proposed = current_stop

    # Stage 1: at +15pts move stop to +5pts
    if not trade.get("stage1_done") and pts_profit >= REDUCE_SL_AT_PTS:
        new = (entry + REDUCE_SL_TO_PTS) if direction == "bull" else (entry - REDUCE_SL_TO_PTS)
        proposed = max(proposed, new) if direction == "bull" else min(proposed, new)
        trade["stage1_done"] = True

    # Stage 2: at +25pts trail 10pts behind
    if pts_profit >= TRAIL_START_PTS:
        hw = trade.get("high_water", entry)
        lw = trade.get("low_water",  entry)
        trail = (hw - TRAIL_BEHIND_PTS) if direction == "bull" else (lw + TRAIL_BEHIND_PTS)
        proposed = max(proposed, trail) if direction == "bull" else min(proposed, trail)
        trade["trailing"] = True

    if proposed != current_stop:
        return round(proposed, 2)
    return None

# ─── TRADE EXECUTION ────────────────────────────────────────────────────────
def open_trade(setup: dict) -> None:
    price     = setup["price"]
    direction = setup["direction"]
    sl        = round(price - INITIAL_SL_PTS, 2) if direction == "bull" \
                else round(price + INITIAL_SL_PTS, 2)

    trade = {
        "direction":   direction,
        "entry":       price,
        "stop":        sl,
        "contracts":   CONTRACTS,
        "window":      setup["window"],
        "high_water":  price,
        "low_water":   price,
        "stage1_done": False,
        "trailing":    False,
        "open_time":   setup["time"],
    }
    daily_state["active_trade"] = trade

    context = claude_market_read(setup)
    dir_emoji = "🟢" if direction == "bull" else "🔴"
    action    = "LONG" if direction == "bull" else "SHORT"

    msg = (
        f"{dir_emoji} <b>CLAUDECODEBOT — {action}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Window: {setup['window']}  Time: {setup['time']} EST\n"
        f"Entry: <b>{price:.2f}</b>\n"
        f"Stop:  <b>{sl:.2f}</b>  ({INITIAL_SL_PTS:.0f} pts)\n"
        f"Target: None — trail to exit\n"
        f"Contracts: {CONTRACTS} MNQ\n\n"
        f"<b>Signals:</b>\n" +
        "\n".join(f"• {r}" for r in setup["reason"]) +
        (f"\n\n<i>{context}</i>" if context else "") +
        f"\n\nConfidence: {setup['confidence']}/10"
    )
    send_telegram(msg)
    print(f"\n  {'PAPER ' if PAPER_MODE else ''}TRADE OPEN — {action} @ {price:.2f}  SL {sl:.2f}")

def close_trade(price: float, reason: str) -> None:
    trade = daily_state.get("active_trade")
    if not trade:
        return

    direction  = trade["direction"]
    entry      = trade["entry"]
    contracts  = trade["contracts"]
    pts_profit = round((price - entry) if direction == "bull" else (entry - price), 2)
    pnl_usd    = round(pts_profit * 2.0 * contracts, 2)

    daily_state["daily_pnl"] += pnl_usd
    daily_state["trade_history"].append({
        "window":    trade["window"],
        "direction": direction,
        "entry":     entry,
        "exit":      price,
        "pts":       pts_profit,
        "pnl":       pnl_usd,
        "reason":    reason,
    })
    daily_state["active_trade"] = None

    result = "✅ WIN" if pnl_usd > 0 else "❌ LOSS" if pnl_usd < 0 else "➖ SCRATCH"
    msg = (
        f"{result} <b>TRADE CLOSED</b>\n"
        f"Exit: {price:.2f}  Reason: {reason}\n"
        f"P&L: <b>{pts_profit:+.2f} pts  (${pnl_usd:+.2f})</b>\n"
        f"Daily P&L: ${daily_state['daily_pnl']:+.2f}"
    )
    send_telegram(msg)
    print(f"\n  TRADE CLOSED — {pts_profit:+.2f}pts  ${pnl_usd:+.2f}  ({reason})")

# ─── TRADE MONITOR ───────────────────────────────────────────────────────────
def monitor_trade(candles: list[dict]) -> None:
    trade = daily_state.get("active_trade")
    if not trade or not candles:
        return

    price     = candles[-1]["close"]
    direction = trade["direction"]
    entry     = trade["entry"]
    stop      = trade["stop"]

    pts_profit = round((price - entry) if direction == "bull" else (entry - price), 2)
    pnl_usd    = round(pts_profit * 2.0 * trade["contracts"], 2)

    print(f"  MONITORING | Entry {entry:.2f}  Now {price:.2f}  "
          f"({pts_profit:+.1f}pts  ${pnl_usd:+.2f})  Stop {stop:.2f}")

    # Stop hit
    if (direction == "bull" and price <= stop) or \
       (direction == "short" and price >= stop):
        close_trade(price, "stop hit")
        return

    # EOD close
    now_est = datetime.now(EST)
    if now_est.hour > 16 or (now_est.hour == 16 and now_est.minute >= 5):
        close_trade(price, "EOD auto-close")
        return

    # Update stop
    new_stop = update_stop(trade, price)
    if new_stop is not None:
        old_stop = trade["stop"]
        trade["stop"] = new_stop
        pts_in = round((price - entry) if direction == "bull" else (entry - price), 2)
        stage = "TRAILING" if trade.get("trailing") else "STOP MOVED"
        msg = (
            f"📈 <b>{stage}</b>\n"
            f"Stop: {old_stop:.2f} → <b>{new_stop:.2f}</b>\n"
            f"Current: {price:.2f}  ({pts_in:+.1f}pts)\n"
            f"TPT: Move stop to {new_stop:.2f}"
        )
        send_telegram(msg)
        print(f"  {stage}: {old_stop:.2f} → {new_stop:.2f}")

# ─── DAILY SETUP ─────────────────────────────────────────────────────────────
def reset_daily() -> None:
    today = datetime.now(EST).date()
    if daily_state["date"] == today:
        return
    print(f"\n  New trading day: {today} — fetching daily levels...")
    levels = get_daily_levels()
    daily_state.update({
        "date":            today,
        "daily_pnl":       0.0,
        "active_trade":    None,
        "trade_history":   [],
        "window_done":     {},
        **levels,
    })
    print(f"  Prior close:    {daily_state.get('prior_close')}")
    print(f"  Prev day high:  {daily_state.get('prev_day_high')}")
    print(f"  Prev day low:   {daily_state.get('prev_day_low')}")
    print(f"  Premarket bias: {daily_state.get('open_bias')}")
    print(f"  Gap:            {daily_state.get('gap_pts'):+.1f}pts" if daily_state.get('gap_pts') else "  Gap: unknown")
    print(f"  Pre-open 3c:    {daily_state.get('precandles_dir')}")

def load_nine_thirty_open(candles: list[dict]) -> None:
    """Cache 9:30 open price once we're past it"""
    if daily_state.get("nine_thirty_open"):
        return
    now_est = datetime.now(EST)
    if now_est.hour == 9 and now_est.minute >= 30:
        # Find the 9:30 candle
        for c in candles:
            if c["time"] == "09:30":
                daily_state["nine_thirty_open"] = c["open"]
                print(f"  9:30 open cached: {c['open']:.2f}")
                break

def in_window(window_name: str) -> bool:
    now_est = datetime.now(EST)
    start_s, end_s = WINDOWS[window_name]
    sh, sm = int(start_s[:2]), int(start_s[3:])
    eh, em = int(end_s[:2]),   int(end_s[3:])
    start = now_est.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end   = now_est.replace(hour=eh, minute=em, second=59, microsecond=0)
    return start <= now_est <= end

def is_trading_hours() -> bool:
    now_est = datetime.now(EST)
    t = now_est.hour * 60 + now_est.minute
    return 570 <= t <= 960  # 9:30 AM to 4:00 PM

def check_daily_limits() -> bool:
    if daily_state["daily_pnl"] <= -MAX_DAILY_LOSS:
        print("  MAX DAILY LOSS HIT — no more trades today")
        return True
    if daily_state["daily_pnl"] >= MAX_DAILY_PROFIT:
        print("  DAILY PROFIT TARGET HIT — no more trades today")
        return True
    return False

# ─── KEEP-ALIVE ──────────────────────────────────────────────────────────────
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class KeepAlive(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        status = {
            "bot": "claudecodebot",
            "date": str(daily_state.get("date")),
            "daily_pnl": daily_state.get("daily_pnl", 0),
            "active_trade": bool(daily_state.get("active_trade")),
            "windows_done": list(daily_state.get("window_done", {}).keys()),
        }
        self.wfile.write(json.dumps(status).encode())
    def log_message(self, *args): pass

def start_keepalive():
    port = int(os.getenv("PORT", "8080"))
    try:
        s = HTTPServer(("0.0.0.0", port), KeepAlive)
        threading.Thread(target=s.serve_forever, daemon=True).start()
        print(f"  Keep-alive running on port {port}")
    except Exception as e:
        print(f"  Keep-alive error: {e}")

# ─── MAIN LOOP ───────────────────────────────────────────────────────────────
def main():
    print("=" * 55)
    print("  claudecodebot — NQ/MNQ Strategy Bot")
    print(f"  Contracts:    {CONTRACTS} MNQ")
    print(f"  Initial SL:   {INITIAL_SL_PTS:.0f} pts")
    print(f"  Trail start:  {TRAIL_START_PTS:.0f} pts")
    print(f"  Trail behind: {TRAIL_BEHIND_PTS:.0f} pts")
    print(f"  Mode:         {'PAPER' if PAPER_MODE else 'LIVE'}")
    print(f"  Data:         Polygon.io")
    print("=" * 55)

    start_keepalive()

    while True:
        try:
            reset_daily()
            now_est = datetime.now(EST)

            if not is_trading_hours():
                time.sleep(30)
                continue

            if check_daily_limits():
                time.sleep(60)
                continue

            candles = get_candles(minutes=1, count=120)
            if not candles:
                print("  No candle data — retrying in 30s")
                time.sleep(30)
                continue

            load_nine_thirty_open(candles)

            # Monitor active trade
            if daily_state.get("active_trade"):
                monitor_trade(candles)
                time.sleep(15)
                continue

            # Check windows
            for window_name in WINDOWS:
                if not in_window(window_name):
                    continue
                if daily_state["window_done"].get(window_name):
                    break

                # Mark done immediately to prevent re-entry
                daily_state["window_done"][window_name] = True

                print(f"\n  [{window_name.upper()}] {now_est.strftime('%H:%M')} EST")
                setup = analyze_window(window_name, candles)

                if setup:
                    print(f"  SETUP FOUND — {setup['direction'].upper()} | conf {setup['confidence']}")
                    for r in setup["reason"]:
                        print(f"    • {r}")
                    open_trade(setup)
                else:
                    print(f"  No qualifying setup")
                break

            time.sleep(10)

        except KeyboardInterrupt:
            print(f"\n  Bot stopped. Daily P&L: ${daily_state['daily_pnl']:+.2f}")
            break
        except Exception as e:
            import traceback
            print(f"\n  Error: {e}")
            print(traceback.format_exc())
            time.sleep(15)

if __name__ == "__main__":
    main()
