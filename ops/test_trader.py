import json, os, time
from datetime import datetime, timezone
from pathlib import Path
os.environ.update(OPS_AGENT_TOKEN="t", CHAIRMAN_TOKEN="c", TG_WEBHOOK_SECRET="s", TELEGRAM_CHAT_ID="1")

import httpx, pytest
from starlette.testclient import TestClient
import ops_server, trader_job

TID = "T:20261112-1"
NA, NR = "aaaaaaaaaaaa", "bbbbbbbbbbbb"
ORDER = {"symbol": "F", "qty": "3", "type": "market", "side": "buy", "time_in_force": "day",
         "order_class": "bracket", "client_order_id": TID,
         "take_profit": {"limit_price": "11.60"}, "stop_loss": {"stop_price": "11.20"}}


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """No network: Alpaca and Telegram answer from a MockTransport, scripts are stubbed."""
    monkeypatch.setenv("OPS_STATE", str(tmp_path / "approvals.json"))
    monkeypatch.setenv("TRADER_DIR", str(tmp_path / "home"))
    monkeypatch.setenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    http, scripts = [], []

    def alpaca(req):
        http.append(req)
        return httpx.Response(200, json={"id": "ord-1"})

    def run(script, *args):
        scripts.append(args[0])
        return 0, json.dumps({"index": 1, "fingerprint": "fp1", "ready": True}) if args[0] == "show" else "", ""

    monkeypatch.setattr(trader_job, "HTTP", httpx.Client(transport=httpx.MockTransport(alpaca)))
    monkeypatch.setattr(trader_job, "_run", run)
    return http, scripts


def ticket(age=0, status="pending"):
    with trader_job._state() as d:
        d[TID] = {"kind": "trade", "index": 1, "fingerprint": "fp1", "order": ORDER,
                  "summary": "BUY 3 F, market, good for today", "status": status, "at": time.time() - age,
                  "nonce_a": NA, "nonce_r": NR}


def record():
    with trader_job._state() as d:
        return d[TID]


def test_refuses_live_base_url(fake, monkeypatch):
    ticket()
    monkeypatch.setenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
    assert trader_job.place_ticket(TID).startswith("Refused")
    assert fake[0] == [] and record()["status"] == "pending"


def test_refuses_unknown_id(fake):
    ticket()
    assert trader_job.place_ticket("T:20261112-9").startswith("Refused: no ticket")
    assert fake[0] == []


def test_refuses_expired(fake):
    ticket(age=601)
    assert "expired" in trader_job.place_ticket(TID)
    assert fake[0] == [] and record()["status"] == "expired"


@pytest.mark.parametrize("status", ["placed", "rejected", "failed"])
def test_refuses_not_pending(fake, status):
    ticket(status=status)
    assert trader_job.place_ticket(TID).startswith("Refused")
    assert fake[0] == []


def test_places_one_bracket_order(fake):
    ticket()
    out = trader_job.place_ticket(TID)
    http, scripts = fake
    assert out.startswith("Placed") and record()["status"] == "placed"
    assert [(r.method, r.url.path) for r in http] == [("POST", "/v2/orders")]
    assert json.loads(http[0].content)["order_class"] == "bracket"
    assert scripts == ["show", "approve", "result"]
    assert trader_job.place_ticket(TID).startswith("Refused") and len(http) == 1  # a second tap does nothing


def test_rest_order_from_ticket_input():
    ti = {"symbol": "TSLA", "side": "buy", "qty": "8", "type": "market", "time_in_force": "day",
          "order_class": "bracket", "take_profit_limit_price": "370.04", "stop_loss_stop_price": "358.32"}
    assert trader_job.rest_order(ti, "T:20261019-1") == {
        "symbol": "TSLA", "side": "buy", "qty": "8", "type": "market", "time_in_force": "day",
        "order_class": "bracket", "client_order_id": "T:20261019-1",
        "take_profit": {"limit_price": "370.04"}, "stop_loss": {"stop_price": "358.32"}}


def press(chat_id, data=f"a|{TID}|{NA}"):
    # no `with`: /tg needs no MCP lifespan, and the session manager runs once per process
    return TestClient(ops_server.app()).post(
        "/tg", headers={"x-telegram-bot-api-secret-token": "s"},
        json={"callback_query": {"data": data, "message": {"chat": {"id": chat_id}}}})


def test_tg_foreign_chat_places_nothing(fake):
    ticket()
    assert press(999).status_code == 200
    assert fake[0] == [] and record()["status"] == "pending"


def test_tg_chairman_tap_places(fake):
    ticket()
    assert press(1).status_code == 200
    assert [r.url.path for r in fake[0]] == ["/v2/orders"] and record()["status"] == "placed"


def test_tg_non_ascii_nonce_changes_nothing(fake):
    ticket()
    assert press(1, data=f"a|{TID}|aaaaaaaaaaaé").status_code == 200
    assert fake[0] == [] and record()["status"] == "pending"
    assert trader_job.nonce_ok({"nonce_a": "é"}, "a", "é") is False
    assert trader_job.nonce_ok({"nonce_a": None}, "a", "x") is False
    assert trader_job.nonce_ok({"nonce_a": "x"}, "a", 5) is False


def test_tg_non_str_data_changes_nothing(fake):
    ticket()
    assert press(1, data=7).status_code == 200
    r = TestClient(ops_server.app()).post(
        "/tg", headers={"x-telegram-bot-api-secret-token": "s"}, json={"callback_query": {}})
    assert r.status_code == 200
    assert fake[0] == [] and record()["status"] == "pending"


def use(monkeypatch, routes):
    """Fake Alpaca from {(method, path): (status, json)}; returns the request list."""
    seen = []

    def handler(req):
        seen.append(req)
        status, body = routes[(req.method, req.url.path)]
        return httpx.Response(status, json=body)
    monkeypatch.setattr(trader_job, "HTTP", httpx.Client(transport=httpx.MockTransport(handler)))
    return seen


def at(monkeypatch, hh, mm, day=(2026, 11, 16)):  # a Monday, EST
    monkeypatch.setattr(trader_job, "_now", lambda: datetime(*day, hh, mm, tzinfo=trader_job.NY))


CAL = {("GET", "/v2/calendar"): (200, [{"date": "2026-11-16", "open": "09:30", "close": "16:00"}])}


def test_flatten_reports_a_failed_close(fake, monkeypatch):  # I1
    seen = use(monkeypatch, {
        ("DELETE", "/v2/positions"): (207, [
            {"symbol": "F", "status": 200, "body": {}},
            {"symbol": "AAL", "status": 403, "body": {"message": "insufficient qty available for order"}}]),
        ("GET", "/v2/positions"): (200, [{"symbol": "AAL", "qty": "3"}]),
        ("GET", "/v2/account"): (200, {"equity": "100000", "last_equity": "100001"})})
    monkeypatch.setattr(trader_job.time, "sleep", lambda s: None)
    out = trader_job.flatten()
    assert seen[0].url.params["cancel_orders"] == "true"
    assert "Closed: F." in out and "NOT closed: AAL (403: insufficient qty" in out
    assert "ALERT" in out and "AAL 3" in out


def test_propose_refuses_an_early_close(fake, monkeypatch):  # I2
    use(monkeypatch, {("GET", "/v2/calendar"): (200, [{"date": "2026-11-27", "open": "09:30", "close": "13:00"}])})
    at(monkeypatch, 10, 30, (2026, 11, 27))
    assert trader_job.propose().startswith("No trade today: early close at 13:00")
    assert fake[1] == []  # no script ran


def test_main_runs_only_in_its_new_york_window(fake, monkeypatch, capsys):  # I3
    ran = []
    monkeypatch.setattr(trader_job, "trading_day", lambda: ran.append("cal") or {"close": "16:00"})
    monkeypatch.setattr(trader_job, "propose", lambda: ran.append("propose") or "done")
    at(monkeypatch, 9, 30)  # the 14:30 UTC cron line in winter
    assert trader_job.main(["trader_job.py", "propose"]) == 0 and ran == []
    assert "outside the propose window" in capsys.readouterr().out
    at(monkeypatch, 10, 31)
    assert trader_job.main(["trader_job.py", "propose"]) == 0 and ran == ["cal", "propose"]


def test_crontab_runs_each_slot_once_in_summer_and_winter():  # I3
    lines = [l.split() for l in Path(__file__).with_name("crontab").read_text().splitlines()
             if l.strip() and not l.startswith("#")]
    for day in ((2026, 7, 6), (2026, 12, 7)):  # Mondays in EDT and in EST
        runs = {}
        for f in lines:
            job = f[f.index("trader_job.py") + 1]
            ny = datetime(*day, int(f[1]), int(f[0]), tzinfo=timezone.utc).astimezone(trader_job.NY)
            runs[job] = runs.get(job, 0) + trader_job.in_window(job, ny)
        assert runs == {"morning": 1, "propose": 2, "flatten": 1}, day


def chain(monkeypatch, orb_at):
    """Stubs every script of a proposal; the ORB hit triggered at orb_at."""
    calls = []
    out = {"orb": {"hits": [{"symbol": "F", "price": 12.0, "at": orb_at}]},
           "check": {"status": "triggered", "minutes_since": 0},
           "show": {"index": 1, "fingerprint": "fp1", "ready": True, "bracket": True, "plain": "BUY 3 F",
                    "why": "orb", "tool_input": {"symbol": "F", "side": "buy", "qty": "3", "type": "limit",
                                                 "limit_price": "12.00", "time_in_force": "day",
                                                 "take_profit_limit_price": "12.30",
                                                 "stop_loss_stop_price": "11.80"}}}

    def run(script, *args):
        calls.append((script.split("/")[-1], args))
        return 0, json.dumps(out.get(args[0], {})), ""
    monkeypatch.setattr(trader_job, "_run", run)
    return calls


def record_of(tid):
    with trader_job._state() as d:
        return d[tid]


def test_propose_sizes_a_limit_order(fake, monkeypatch):  # I4
    use(monkeypatch, CAL)
    at(monkeypatch, 10, 30)
    calls = chain(monkeypatch, "10:25 AM")
    out = trader_job.propose()
    assert out.startswith("Ticket T:20261116-1")
    assert ("size.py", ("--limit", "--write", "--json")) in calls
    rec = record_of("T:20261116-1")
    assert len(rec["nonce_a"]) == 12 and rec["nonce_a"] != rec["nonce_r"] and rec["nonce_a"] not in out
    order = rec["order"]
    assert order["type"] == "limit" and order["limit_price"] == "12.00" and order["order_class"] == "bracket"


def test_propose_drops_a_stale_trigger(fake, monkeypatch):  # I4
    use(monkeypatch, CAL)
    at(monkeypatch, 10, 30)
    calls = chain(monkeypatch, "10:05 AM")
    assert trader_job.propose().startswith("No trade today: no fresh opening-range breakout")
    assert [c[0] for c in calls] == ["scan.py"]


def test_config_is_the_global_constraints():
    assert json.loads(Path(__file__).with_name("trader_config.json").read_text()) == {
        "budget": 50, "cash_buffer_pct": 10, "max_position_pct": 90, "max_order_usd": 45,
        "min_order_usd": 5, "max_loss_per_trade_usd": 1.0, "max_daily_loss_usd": 2.0,
        "max_trades_per_day": 1, "max_orders_per_day": 3, "min_reward_risk": 1.5,
        "require_stop": True, "no_entries_before": "09:45", "no_entries_after": "11:30",
        "flat_by": "15:55", "fractional": False, "broker": "alpaca", "paper": True,
        "blocked": [], "allow_options": False, "allow_margin": False, "allow_short": False,
        "pdt_guard": True}


# ---------------------------------------------------------------- final review fixes

@pytest.mark.parametrize("data", [f"a|{TID}", f"a|{TID}|", f"a|{TID}|cccccccccccc", f"a|{TID}|{NR}",
                                  f"r|{TID}|{NA}", f"r|{TID}"])
def test_tg_forged_ticket_tap_changes_nothing(fake, data):  # C2: secret header alone is not enough
    ticket()
    assert press(1, data).status_code == 200
    assert fake[0] == [] and fake[1] == [] and record()["status"] == "pending"


def test_tg_ticket_reject_with_its_nonce(fake):
    ticket()
    assert press(1, f"r|{TID}|{NR}").status_code == 200
    assert fake[0] == [] and record()["status"] == "rejected"


def merge_request(monkeypatch, repo="o/r", pr=7):
    """request_merge with a fake GitHub; returns (tool result, the Telegram sendMessage bodies)."""
    sent = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot")
    monkeypatch.setattr(ops_server, "REPOS", {repo})
    monkeypatch.setattr(ops_server, "_gh", lambda m, path, **kw: {"head": {"sha": "abc123def456"}})
    monkeypatch.setattr(trader_job, "HTTP", httpx.Client(transport=httpx.MockTransport(
        lambda req: sent.append(json.loads(req.content)) or httpx.Response(200, json={"ok": True}))))
    return ops_server.request_merge(repo, pr, "fix"), sent


def pr_record(key="o/r#7"):
    with trader_job._state() as d:
        return d[key]


def test_pr_tap_needs_the_nonce_it_was_sent_with(fake, monkeypatch):  # C2
    out, sent = merge_request(monkeypatch)
    rec = pr_record()
    assert out["notified"] is True and rec["nonce_a"] not in json.dumps(out) and rec["nonce_r"] not in json.dumps(out)
    a, r = [b["callback_data"] for b in sent[0]["reply_markup"]["inline_keyboard"][0]]
    assert a == f"a|o/r#7|{rec['nonce_a']}" and r == f"r|o/r#7|{rec['nonce_r']}"
    assert all(len(x.encode()) <= 64 for x in (a, r))
    for forged in ("a|o/r#7", "a|o/r#7|000000000000", f"a|o/r#7|{rec['nonce_r']}"):
        assert press(1, forged).status_code == 200
        assert pr_record()["status"] == "pending"
    assert press(1, a).status_code == 200 and pr_record()["status"] == "approved"


def test_pr_request_refuses_callback_over_64_bytes(fake, monkeypatch):
    repo = "o/" + "r" * 60
    out, sent = merge_request(monkeypatch, repo=repo)
    assert out["notified"] is False and sent == []
    assert trader_job.buttons("x" * 60, {"nonce_a": NA, "nonce_r": NR}) is None


def test_trading_status_shows_no_nonce(fake, monkeypatch):
    ticket()
    use(monkeypatch, {("GET", "/v2/account"): (200, {"equity": "50", "cash": "50", "last_equity": "50"}),
                      ("GET", "/v2/positions"): (200, []), ("GET", "/v2/orders"): (200, [])})
    out = json.dumps(ops_server.trading_status())
    assert TID in out and NA not in out and NR not in out and "nonce" not in out


def test_tg_ticket_exception_tells_the_chairman(fake, monkeypatch):  # I3
    ticket()
    said = []
    monkeypatch.setattr(trader_job, "tg_send", lambda text, markup=None: said.append(text) or True)
    monkeypatch.setattr(trader_job, "place_ticket", lambda k: 1 / 0)
    assert press(1).status_code == 200
    assert said == [f"Error on {TID}: ZeroDivisionError. Check trading_status before acting."]


def test_flatten_failure_alerts(fake, monkeypatch):  # I4
    use(monkeypatch, {("DELETE", "/v2/positions"): (500, {"message": "down"})})
    said = []
    monkeypatch.setattr(trader_job, "tg_send", lambda text, markup=None: said.append(text) or True)
    out = trader_job.flatten()
    assert out == said[0] == "ALERT: flatten failed (HTTPStatusError). Close positions by hand."


def test_flatten_runs_without_the_calendar(fake, monkeypatch):  # I4: holiday or calendar outage
    ran = []
    monkeypatch.setattr(trader_job, "trading_day", lambda: 1 / 0)
    monkeypatch.setattr(trader_job, "flatten", lambda: ran.append("flatten") or "done")
    at(monkeypatch, 15, 55)
    assert trader_job.main(["trader_job.py", "flatten"]) == 0 and ran == ["flatten"]
    at(monkeypatch, 12, 0)  # the NY window still applies
    assert trader_job.main(["trader_job.py", "flatten"]) == 0 and ran == ["flatten"]
