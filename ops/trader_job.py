"""Daily $50 paper-trading loop: digest, ticket, chairman tap, paper bracket order.

    python trader_job.py morning    # 08:45 ET: scan, news, Pelosi PTRs, account -> one Telegram message
    python trader_job.py propose    # 10:00 and 10:30 ET: ORB -> entry -> exit -> size -> risk -> ticket
    python trader_job.py flatten    # 15:55 ET: cancel open orders, close positions, end-of-day line

The day-trader-skill scripts (cloned at /opt/dts) do the analysis and never
place orders. This file only runs them and moves their output around. The one
place an order is sent is place_ticket(), which ops_server calls from the
Telegram webhook after the chairman taps Approve.

Env: ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL (paper only),
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OPS_STATE, TRADER_DIR, TRADER_SYMBOLS,
SEC_USER_AGENT (optional), DTS_DIR (default /opt/dts).
"""
import fcntl, json, os, re, secrets, shutil, subprocess, sys, time
from contextlib import contextmanager
from datetime import datetime, time as dtime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

NY = ZoneInfo("America/New_York")
PAPER_HOST = "paper-api.alpaca.markets"
EXPIRY_S = 600
STALE_MIN = 10  # an ORB trigger older than this is a different trade
# New York slots per job. Cron fires in UTC for both EDT and EST; main() runs only within WINDOW_MIN of a slot.
SLOTS = {"morning": ("08:45",), "propose": ("10:00", "10:30"), "flatten": ("15:55",)}
WINDOW_MIN = 10
SYMBOLS = ("F,SOFI,T,PFE,INTC,SNAP,AAL,CCL,RIVN,LCID,KMI,VZ,BAC,WBD,NU,HBAN,KEY,GRAB,PLUG,NIO,"
           "KVUE,CMCSA,HPE,UBER")
HTTP = httpx.Client(timeout=20)  # tests swap in an httpx.MockTransport client


def _now() -> datetime:
    return datetime.now(NY)


def _home() -> Path:
    """TRADER_DIR with trader/config.json in place (copied from trader_config.json if absent)."""
    home = Path(os.environ.get("TRADER_DIR", "/data/trader-home"))
    cfg = home / "trader" / "config.json"
    if not cfg.exists():
        cfg.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(Path(__file__).with_name("trader_config.json"), cfg)
    return home


def _cfg() -> dict:
    return json.loads((_home() / "trader" / "config.json").read_text())


def _run(script: str, *args: str) -> tuple[int, str, str]:
    """One day-trader-skill script, cwd TRADER_DIR. Secrets are not passed to it."""
    env = {k: v for k, v in os.environ.items() if not re.search("KEY|SECRET|TOKEN", k)}
    p = subprocess.run([sys.executable, f"{os.environ.get('DTS_DIR', '/opt/dts')}/{script}", *args],
                       cwd=_home(), env=env, capture_output=True, text=True, timeout=900)
    return p.returncode, p.stdout, p.stderr


def _json(out: str):
    try:
        return json.loads(out)
    except ValueError:
        return None


def _last(err: str, out: str = "") -> str:
    lines = [l.strip() for l in (err or out).splitlines() if l.strip()]
    return lines[-1][:300] if lines else "no output"


# ---------------------------------------------------------------- Alpaca and Telegram

def _base() -> str:
    return os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")


def is_paper() -> bool:
    u = urlparse(_base())
    return u.scheme == "https" and u.hostname == PAPER_HOST


def alpaca(method: str, path: str, **kw) -> httpx.Response:
    """Paper account only: any other host raises before a request is made."""
    if not is_paper():
        raise RuntimeError(f"ALPACA_BASE_URL host is not {PAPER_HOST}")
    return HTTP.request(method, _base() + path, **kw, headers={
        "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", "")})


def _get(path: str, **kw):
    r = alpaca("GET", path, **kw)
    r.raise_for_status()
    return r.json()


def new_nonces() -> dict:
    """Fresh per-verb nonces for a record. A tap counts only if its callback_data carries the
    stored nonce, so holding the webhook secret is not enough to forge an Approve."""
    return {"nonce_a": secrets.token_hex(6), "nonce_r": secrets.token_hex(6)}


def buttons(key: str, rec: dict) -> dict | None:
    """Approve/Reject keyboard "a|<key>|<nonce_a>" / "r|<key>|<nonce_r>".
    None when either callback_data exceeds Telegram's 64-byte cap: the caller must not send."""
    a, r = f"a|{key}|{rec['nonce_a']}", f"r|{key}|{rec['nonce_r']}"
    if max(len(a.encode()), len(r.encode())) > 64:
        return None
    return {"inline_keyboard": [[{"text": "Approve", "callback_data": a},
                                 {"text": "Reject", "callback_data": r}]]}


def nonce_ok(rec: dict | None, verb: str, nonce: str) -> bool:
    """True only when the tap's nonce equals the one stored for that verb."""
    want = (rec or {}).get(f"nonce_{verb}")
    # compare_digest raises on non-ASCII str, so reject anything else first
    ok = all(isinstance(x, str) and x.isascii() for x in (want, nonce))
    return ok and bool(want) and secrets.compare_digest(want, nonce)


def tg_send(text: str, markup: dict | None = None) -> bool:
    """Plain-text Telegram message, with an inline keyboard when markup is given.
    Never raises and never prints: the URL holds the bot token."""
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return False
    body = {"chat_id": chat, "text": text[:4000]}
    if markup:
        body["reply_markup"] = markup
    try:
        return HTTP.post(f"https://api.telegram.org/bot{tok}/sendMessage", json=body).status_code == 200
    except httpx.HTTPError:
        return False


def trading_day() -> dict | None:
    """Today's calendar row ({"date", "open", "close"}), or None when there is no session."""
    d = _now().date().isoformat()
    return next((c for c in _get("/v2/calendar", params={"start": d, "end": d}) if c.get("date") == d), None)


# ---------------------------------------------------------------- ticket records (OPS_STATE)

@contextmanager
def _state():
    """Read-modify-write of OPS_STATE under an exclusive file lock (cron and the server share it)."""
    path = Path(os.environ.get("OPS_STATE", "approvals.json"))
    with open(f"{path}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        d = json.loads(path.read_text()) if path.exists() else {}
        yield d
        tmp = Path(f"{path}.tmp")
        tmp.write_text(json.dumps(d, indent=1))
        os.replace(tmp, path)


def _today_tickets(d: dict) -> dict:
    prefix = f"T:{_now():%Y%m%d}-"
    return {k: v for k, v in d.items() if k.startswith(prefix)}


def rest_order(ti: dict, ticket_id: str) -> dict:
    """ticket.py's place_stock_order input -> Alpaca REST bracket body."""
    o = {k: ti[k] for k in ("symbol", "qty", "type", "limit_price") if k in ti}
    o.update(side="buy", time_in_force="day", order_class="bracket", client_order_id=ticket_id,
             take_profit={"limit_price": ti["take_profit_limit_price"]},
             stop_loss={"stop_price": ti["stop_loss_stop_price"]})
    return o


def _show(index: int):
    rc, out, err = _run("trader-broker/ticket.py", "show", "--index", str(index), "--json")
    return _json(out)


def _claim(ticket_id: str, verb: str):
    """Moves a pending, fresh ticket to 'placing' or 'rejected'. Returns (record, refusal)."""
    with _state() as d:
        rec = d.get(ticket_id)
        if not rec or rec.get("kind") != "trade":
            return None, f"Refused: no ticket {ticket_id}."
        if rec["status"] != "pending":
            return None, f"Refused: ticket {ticket_id} is {rec['status']}, not pending."
        age = int(time.time() - rec["at"])
        if verb == "a" and age > EXPIRY_S:
            rec["status"] = "expired"
            return None, f"Refused: ticket {ticket_id} expired ({age} s old, limit {EXPIRY_S} s). Nothing placed."
        rec["status"] = "placing" if verb == "a" else "rejected"
        return dict(rec), None


def _finish(ticket_id: str, **fields) -> None:
    with _state() as d:
        d[ticket_id].update(fields)


def place_ticket(ticket_id: str) -> str:
    """The only code that sends an order. Called from /tg after the chairman's Approve tap."""
    if not is_paper():
        return f"Refused: ALPACA_BASE_URL host is not {PAPER_HOST}. Nothing placed."
    rec, refusal = _claim(ticket_id, "a")
    if refusal:
        return refusal
    n = str(rec["index"])
    t = _show(rec["index"])
    if not t or t.get("fingerprint") != rec.get("fingerprint"):
        _finish(ticket_id, status="failed", note="orders.json changed since the ticket was sent")
        return f"Refused: the proposal behind {ticket_id} changed since it was sent. Nothing placed."
    rc, out, err = _run("trader-broker/ticket.py", "approve", "--index", n, "--reply", "yes")
    if rc != 0:
        _finish(ticket_id, status="failed", note=_last(err, out))
        return f"Refused by ticket.py: {_last(err, out)}. Nothing placed."
    try:
        r = alpaca("POST", "/v2/orders", json=rec["order"])
        ok, note = r.status_code < 300, f"HTTP {r.status_code}: {r.text[:200]}"
    except httpx.HTTPError as e:
        r, ok, note = None, False, type(e).__name__
    if ok:
        oid = r.json().get("id", "")
        _run("trader-broker/ticket.py", "result", "--index", n, "--status", "placed", "--broker-id", oid)
        _finish(ticket_id, status="placed", broker_id=oid)
        return f"Placed {ticket_id} on the paper account: {rec['summary'].splitlines()[0]} (order {oid})."
    _run("trader-broker/ticket.py", "result", "--index", n, "--status", "failed", "--note", note)
    _finish(ticket_id, status="failed", note=note)
    return f"Failed {ticket_id}: Alpaca refused it ({note})."


def reject_ticket(ticket_id: str) -> str:
    rec, refusal = _claim(ticket_id, "r")
    if refusal:
        return refusal
    t = _show(rec["index"])
    if t and t.get("fingerprint") == rec.get("fingerprint"):
        _run("trader-broker/ticket.py", "decline", "--index", str(rec["index"]), "--reply", "no")
    return f"Rejected {ticket_id}. Nothing placed."


# ---------------------------------------------------------------- the three jobs

def morning() -> str:
    home, cfg = _home(), _cfg()
    cap = cfg["max_order_usd"]
    rc, out, err = _run("trader-scanner/scan.py", "daily", "--symbols",
                        os.environ.get("TRADER_SYMBOLS", SYMBOLS), "--json")
    scan = _json(out) or {}
    hits = [h for h in scan.get("hits", []) if h["price"] <= cap][:10]
    lines = [f"Morning digest {_now():%a %Y-%m-%d} (paper)", ""]
    if hits:
        lines.append(f"Setups under ${cap} ({scan.get('scanned', '?')} of {scan.get('universe', '?')} "
                     f"scanned, closes {scan.get('as_of')}):")
        lines += [f"  {h['symbol']} ${h['price']:.2f} {', '.join(h['setups'])}" for h in hits]
        sec = [] if os.environ.get("SEC_USER_AGENT") else ["--no-sec"]
        rc, out, err = _run("trader-news/news.py", "--tickers", *[h["symbol"] for h in hits],
                            "--hours", "24", "--no-moves", "--json", *sec)
        items = [i for i in (_json(out) or {}).get("items", []) if i.get("new")]
        lines += ["", "New headlines (24 h):"]
        for h in hits:
            mine = [i for i in items if i["ticker"] == h["symbol"]][:3]
            lines += [f"  {h['symbol']}: {i['title']}" for i in mine]
        if not items:
            lines.append("  none")
    else:
        lines.append(f"Setups under ${cap}: none ({'no hits' if scan else _last(err)}).")
    rc, out, err = _run("trader-pelosi/ptr.py", "new", "--member", "pelosi", "--json")
    fresh = (_json(out) or {}).get("new", [])
    lines += ["", "New Pelosi PTRs:" if fresh else "New Pelosi PTRs: none"]
    lines += [f"  {r['name']} filed {r['filed']}: {r['url']}" for r in fresh]
    try:
        a = _get("/v2/account")
        lines += ["", f"Account: equity ${float(a['equity']):,.2f}, cash ${float(a['cash']):,.2f}"]
    except Exception as e:  # the digest still goes out without the account line
        lines += ["", f"Account: unavailable ({type(e).__name__})"]
    lines.append(f"Budget ${cfg['budget']:g}, max loss ${cfg['max_loss_per_trade_usd']:g}/trade, "
                 f"${cfg['max_daily_loss_usd']:g}/day, {cfg['max_trades_per_day']} trade/day, paper")
    text = "\n".join(lines)[:4000]
    (home / "last_digest.txt").write_text(text)
    tg_send(text)
    return text


def _age_min(at: str, now: datetime) -> float:
    """Minutes since a scanner clock label like "10:15 AM" (New York, today)."""
    t = datetime.strptime(at, "%I:%M %p").time()
    return (now - datetime.combine(now.date(), t, NY)).total_seconds() / 60


def propose(notify_none: bool = True) -> str:
    """One proposal attempt. Sends a ticket with buttons, or on the last run (10:30)
    a one-line "No trade today". Returns what happened either way."""
    cfg, now = _cfg(), _now()
    final = now.time() >= dtime(10, 25)

    def none(reason: str) -> str:
        msg = f"No trade today: {reason}"
        if final and notify_none:
            tg_send(msg)
        return msg

    day = trading_day()
    if not day:
        return none("no trading session today")
    if day.get("close") != "16:00":  # the bracket's day legs would end before the 15:55 flatten
        return none(f"early close at {day.get('close')}, no entries on a short session")
    with _state() as d:
        today = _today_tickets(d)
    placed = sum(1 for r in today.values() if r["status"] in ("placed", "placing"))
    if placed >= cfg["max_trades_per_day"]:
        return none(f"{placed} of {cfg['max_trades_per_day']} trade(s) already placed today")
    waiting = [k for k, r in today.items() if r["status"] == "pending" and time.time() - r["at"] <= EXPIRY_S]
    if waiting:
        return f"Ticket {waiting[0]} is still waiting for the chairman; no new proposal."
    rc, out, err = _run("trader-scanner/scan.py", "orb", "--watchlist", "trader/watchlist.json",
                        "--out", "trader/orb.json", "--json")
    orb = _json(out)
    hits = [h for h in (orb or {}).get("hits", []) if h["price"] <= cfg["max_order_usd"]
            and _age_min(h["at"], now) <= STALE_MIN]
    if not hits:
        why = _last(err) if orb is None else \
            f"{len(orb.get('hits', []))} breakout(s), none priced in and under {STALE_MIN} minutes old"
        return none(f"no fresh opening-range breakout at or under ${cfg['max_order_usd']} ({why})")
    sym = hits[0]["symbol"]
    rc, out, err = _run("trader-entry/entry.py", "check", sym, "--write", "--json")
    ent = _json(out) or {}
    if rc != 0:
        return none(f"{sym} entry {ent.get('status') or _last(err, out)}")
    if ent.get("minutes_since", 0) > STALE_MIN:
        return none(f"{sym} triggered {ent['minutes_since']} minutes ago, too late to enter")
    rc, out, err = _run("trader-exit/exit.py", "plan", "--write")
    if rc != 0:
        return none(f"{sym} exit plan refused: {_last(err, out)}")
    # a limit at the plan's entry caps the fill, so a slow tap cannot raise the loss at the stop
    rc, out, err = _run("trader-budget/size.py", "--limit", "--write", "--json")
    if rc != 0:
        return none(f"{sym} sizing refused: {((_json(out) or {}).get('sizing') or {}).get('refusal') or _last(err, out)}")
    rc, out, err = _run("trader-risk/check.py", "--json")
    if rc != 0:
        res = (_json(out) or {}).get("results") or [{}]
        return none(f"{sym} risk check BLOCK: {'; '.join(res[0].get('reasons', [])) or _last(err, out)}")
    t = _show(1)
    if not t or not t.get("ready") or not t.get("bracket") or t["tool_input"].get("type") != "limit":
        return none(f"{sym} ticket not ready: {(t or {}).get('status', 'ticket.py failed')}")
    with _state() as d:
        tid = f"T:{_now():%Y%m%d}-{len(_today_tickets(d)) + 1}"
        summary = f"{t['plain']}\nwhy: {t['why']}"
        rec = d[tid] = {"kind": "trade", "index": t["index"], "fingerprint": t["fingerprint"],
                        "order": rest_order(t["tool_input"], tid), "summary": summary,
                        "status": "pending", "at": time.time(), **new_nonces()}
    kb = buttons(tid, rec)
    sent = bool(kb) and tg_send(f"Paper trade ticket {tid}\n{summary}\nRisk check: PASS. "
                                f"Approve within 10 minutes to place it on the paper account.", kb)
    return f"Ticket {tid} {'sent' if sent else 'NOT sent (Telegram unavailable)'}: {t['plain']}"


def flatten() -> str:
    """Exits only reduce risk, so no tap is needed: cancel every open order, close every position.
    Alpaca answers 207 with a status per symbol; a failed close is reported, and anything still
    held after the closes had time to fill raises an ALERT in Telegram. Any exception (the
    DELETE itself, a non-paper URL, the account read) still sends an ALERT."""
    try:
        return _flatten()
    except Exception as e:
        text = f"ALERT: flatten failed ({type(e).__name__}). Close positions by hand."
        tg_send(text)
        return text


def _flatten() -> str:
    r = alpaca("DELETE", "/v2/positions", params={"cancel_orders": "true"})
    r.raise_for_status()
    results = r.json() or []
    closed = [c.get("symbol", "?") for c in results if c.get("status") == 200]
    failed = [f"{c.get('symbol', '?')} ({c.get('status')}: {(c.get('body') or {}).get('message', '')})"
              for c in results if c.get("status") != 200]
    left = []
    for _ in range(10):  # market sells usually fill in seconds
        left = _get("/v2/positions")
        if not left:
            break
        time.sleep(3)
    a = _get("/v2/account")
    pnl = float(a["equity"]) - float(a["last_equity"])
    text = (f"End of day {_now():%Y-%m-%d} (paper): P&L today ${pnl:+,.2f}, equity ${float(a['equity']):,.2f}. "
            f"Closed: {', '.join(closed) if closed else 'nothing'}.")
    if failed:
        text += f" NOT closed: {'; '.join(failed)}."
    if left:
        text += (" ALERT: still open after flatten, with no stop overnight: "
                 + ", ".join(f"{p.get('symbol')} {p.get('qty')}" for p in left) + ". Close it by hand.")
    tg_send(text)
    return text


def status() -> dict:
    a = _get("/v2/account")
    with _state() as d:
        tickets = {k: {"status": v["status"], "summary": v["summary"], "at": v["at"]}
                   for k, v in _today_tickets(d).items()}
    return {"account": {k: a.get(k) for k in ("equity", "cash", "last_equity")},
            "positions": [{k: p.get(k) for k in ("symbol", "qty", "avg_entry_price", "unrealized_pl")}
                          for p in _get("/v2/positions")],
            "open_orders": [{k: o.get(k) for k in ("symbol", "side", "qty", "type", "order_class", "status")}
                            for o in _get("/v2/orders", params={"status": "open"})],
            "tickets_today": tickets}


def in_window(job: str, now: datetime) -> bool:
    """True within WINDOW_MIN minutes of one of the job's New York slots."""
    mins = now.hour * 60 + now.minute
    return any(abs(mins - int(s[:2]) * 60 - int(s[3:])) <= WINDOW_MIN for s in SLOTS[job])


def main(argv: list[str]) -> int:
    jobs = {"morning": morning, "propose": propose, "flatten": flatten}
    if len(argv) != 2 or argv[1] not in jobs:
        print("usage: python trader_job.py morning|propose|flatten")
        return 2
    now = _now()
    if not in_window(argv[1], now):
        print(f"{now:%Y-%m-%d %H:%M} ET: outside the {argv[1]} window {'/'.join(SLOTS[argv[1]])}, skipping.")
        return 0
    # flatten skips the calendar: cancelling and closing is harmless on a holiday, and a failed
    # calendar call must not leave a position open overnight
    if argv[1] != "flatten" and not trading_day():
        print(f"{now:%Y-%m-%d %H:%M} ET: no trading session today, skipping {argv[1]}.")
        return 0
    print(f"{_now():%Y-%m-%d %H:%M} ET {argv[1]}: {jobs[argv[1]]()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
