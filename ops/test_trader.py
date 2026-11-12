import json, os, time
from datetime import datetime, timezone
from pathlib import Path
os.environ.update(OPS_AGENT_TOKEN="t", CHAIRMAN_TOKEN="c", TG_WEBHOOK_SECRET="s", TELEGRAM_CHAT_ID="1")

import httpx, pytest
from starlette.testclient import TestClient
import ops_server, trader_job

TID = "T:20261112-1"
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
                  "summary": "BUY 3 F, market, good for today", "status": status, "at": time.time() - age}


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


def press(chat_id):
    # no `with`: /tg needs no MCP lifespan, and the session manager runs once per process
    return TestClient(ops_server.app()).post(
        "/tg", headers={"x-telegram-bot-api-secret-token": "s"},
        json={"callback_query": {"data": f"a|{TID}", "message": {"chat": {"id": chat_id}}}})


def test_tg_foreign_chat_places_nothing(fake):
    ticket()
    assert press(999).status_code == 200
    assert fake[0] == [] and record()["status"] == "pending"


def test_tg_chairman_tap_places(fake):
    ticket()
    assert press(1).status_code == 200
    assert [r.url.path for r in fake[0]] == ["/v2/orders"] and record()["status"] == "placed"


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
    assert trader_job.propose().startswith("Ticket T:20261116-1")
    assert ("size.py", ("--limit", "--write", "--json")) in calls
    order = record_of("T:20261116-1")["order"]
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
