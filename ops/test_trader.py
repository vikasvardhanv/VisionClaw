import json, os, time
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


def test_config_is_the_global_constraints():
    assert json.loads(Path(__file__).with_name("trader_config.json").read_text()) == {
        "budget": 50, "cash_buffer_pct": 10, "max_position_pct": 90, "max_order_usd": 45,
        "min_order_usd": 5, "max_loss_per_trade_usd": 1.0, "max_daily_loss_usd": 2.0,
        "max_trades_per_day": 1, "max_orders_per_day": 3, "min_reward_risk": 1.5,
        "require_stop": True, "no_entries_before": "09:45", "no_entries_after": "11:30",
        "flat_by": "15:55", "fractional": False, "broker": "alpaca", "paper": True,
        "blocked": [], "allow_options": False, "allow_margin": False, "allow_short": False,
        "pdt_guard": True}
