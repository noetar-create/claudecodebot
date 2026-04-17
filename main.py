"""
claudecodebot relay — receives TradingView webhooks, sends Telegram cards, logs trades.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, request, abort, jsonify

# ─── LOGGING ────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
log = logging.getLogger("claudecodebot")

# ─── CONFIG ─────────────────────────────────────────────────────────────────
def require_env(key: str) -> str:
    v = os.environ.get(key)
    if not v:
        log.error(f"Missing required env var: {key}")
        sys.exit(1)
    return v

TELEGRAM_TOKEN   = require_env("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = require_env("TELEGRAM_CHAT_ID")
WEBHOOK_SECRET   = require_env("WEBHOOK_SECRET")

DEFAULT_DB = "/data/trades.db" if Path("/data").exists() else "/tmp/claudecodebot.db"
DB_PATH    = Path(os.environ.get("DB_PATH", DEFAULT_DB))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
log.info(f"Database: {DB_PATH}")

# ─── DATABASE ────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        TEXT NOT NULL,
            event     TEXT NOT NULL,
            action    TEXT,
            window    TEXT,
            rule      TEXT,
            price     REAL,
            contracts INTEGER,
            stop_pts  REAL,
            pnl_pts   REAL,
            pnl_usd   REAL,
            raw_json  TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn

def log_trade(conn, payload: dict):
    conn.execute("""
        INSERT INTO trades (ts, event, action, window, rule, price, contracts, stop_pts, pnl_pts, pnl_usd, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.utcnow().isoformat(),
        payload.get("event", "unknown"),
        payload.get("action"),
        payload.get("window"),
        payload.get("rule"),
        payload.get("price"),
        payload.get("contracts"),
        payload.get("stop_pts"),
        payload.get("pnl_pts"),
        payload.get("pnl_usd"),
        json.dumps(payload),
    ))
    conn.commit()

# ─── TELEGRAM ────────────────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False

# ─── MESSAGE FORMATTING ──────────────────────────────────────────────────────
RULE_NAMES = {
    "failed_auction_long":   "Failed Auction",
    "failed_auction_short":  "Failed Auction",
    "continuation_long":     "Continuation (no wick)",
    "continuation_short":    "Continuation (no wick)",
    "wick_reversal_long":    "Wick Reversal",
    "wick_reversal_short":   "Wick Reversal",
    "three_candle_long":     "3-Candle Reversal",
    "three_candle_short":    "3-Candle Reversal",
    "four_hr_long":          "4hr Context",
    "four_hr_short":         "4hr Context",
    "ten_thirty_long":       "10:30 Fade",
    "ten_thirty_short":      "10:30 Fade",
}

def format_entry(p: dict) -> str:
    action   = p.get("action", "")
    is_long  = "long" in action
    emoji    = "🟢" if is_long else "🔴"
    side     = "LONG" if is_long else "SHORT"
    rule     = RULE_NAMES.get(p.get("rule", ""), p.get("rule", "unknown"))
    price    = p.get("price", 0) or 0
    stop_pts = p.get("stop_pts", 30) or 30
    stop_px  = price - stop_pts if is_long else price + stop_pts
    window   = p.get("window", "?").replace("_", " ").title()
    rng      = p.get("window_range", 0) or 0
    pdh      = p.get("pd_high", 0) or 0
    pdl      = p.get("pd_low",  0) or 0

    lines = [
        f"{emoji} <b>CLAUDECODEBOT — {side}</b>",
        f"━━━━━━━━━━━━━━━━━━",
        f"Window: <b>{window}</b>",
        f"Setup:  <b>{rule}</b>",
        f"",
        f"📍 Entry:  <b>{price:.2f}</b>",
        f"🛑 Stop:   <b>{stop_px:.2f}</b>  ({stop_pts:.0f} pts)",
        f"🎯 Target: Trail to exit (no TP)",
        f"📊 Contracts: {p.get('contracts', 5)} MNQ",
        f"",
        f"Window range: {rng:.1f} pts",
    ]
    if pdh:
        lines.append(f"PDH: {pdh:.2f}  ({price - pdh:+.1f})")
    if pdl:
        lines.append(f"PDL: {pdl:.2f}  ({price - pdl:+.1f})")
    lines += [
        f"",
        f"<i>Stop trail: +{15:.0f}pts → +5pts locked | +{25:.0f}pts → trail {10:.0f}pts</i>",
    ]
    return "\n".join(lines)

def format_exit(p: dict) -> str:
    pnl_usd = p.get("pnl_usd", 0) or 0
    pnl_pts = p.get("pnl_pts", 0) or 0
    emoji   = "✅" if pnl_usd > 0 else "❌" if pnl_usd < 0 else "➖"
    return (
        f"{emoji} <b>TRADE CLOSED</b>\n"
        f"Exit: {(p.get('price') or 0):.2f}\n"
        f"P&L: <b>{pnl_pts:+.2f} pts  (${pnl_usd:+.2f})</b>"
    )

# ─── FLASK APP ───────────────────────────────────────────────────────────────
app    = Flask(__name__)
db_conn = init_db()

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        payload = json.loads(request.get_data(as_text=True))
    except Exception:
        return jsonify({"error": "bad json"}), 400

    if payload.get("secret") != WEBHOOK_SECRET:
        log.warning(f"Bad secret from {request.remote_addr}")
        abort(403)

    event = payload.get("event", "unknown")
    log.info(f"Received: {event} {payload.get('action','')} {payload.get('window','')}")
    log_trade(db_conn, payload)

    if event == "entry":
        text = format_entry(payload)
    elif event == "exit":
        text = format_exit(payload)
    else:
        text = f"claudecodebot: {event}\n<pre>{json.dumps(payload, indent=2)}</pre>"

    ok = send_telegram(text)
    return jsonify({"status": "ok", "telegram": ok}), 200

@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "alive",
        "bot": "claudecodebot",
        "db": str(DB_PATH),
    }), 200

@app.route("/trades", methods=["GET"])
def trades():
    secret = request.args.get("secret", "")
    if secret != WEBHOOK_SECRET:
        abort(403)
    date_filter = request.args.get("date")
    limit = int(request.args.get("limit", 200))
    cur = db_conn.execute(
        """SELECT id, ts, event, action, window, rule, price, contracts, pnl_pts, pnl_usd
           FROM trades WHERE (? IS NULL OR date(ts) = ?) ORDER BY ts DESC LIMIT ?""",
        (date_filter, date_filter, limit),
    )
    rows = [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
    return jsonify({"count": len(rows), "trades": rows}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    log.info(f"Starting claudecodebot relay on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
