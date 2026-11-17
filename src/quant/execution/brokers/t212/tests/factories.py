"""Synthetic Trading 212 response bodies in the documented shapes (Public API v0), for the lab.t212 tests. Values are invented; no key, account number or real
position appears here. Each builder returns decoded JSON (dicts and lists) with every documented field, so a test removes or changes exactly what it is about."""
from __future__ import annotations

import datetime as dt
import email.message
import io
import json
import urllib.error
import urllib.request
import urllib.response
from typing import Any

import pytest

Json = dict[str, Any]
Answer = tuple[int, email.message.Message, bytes]          # (HTTP status, response headers, body)
T0 = dt.datetime(2026, 10, 27, 14, 30, 0, tzinfo=dt.UTC)


def instrument(ticker: str = "AAA_US_EQ", currency: str = "USD") -> Json:
    return {"ticker": ticker, "name": f"{ticker.split('_')[0]} Inc", "isin": "US0000000001", "currency": currency}


def account_summary(total: float = 10000.0, available: float = 2500.0, reserved: float = 0.0, invested: float = 7500.0, currency: str = "GBP") -> Json:
    return {"id": 1001, "currency": currency, "totalValue": total,
            "cash": {"availableToTrade": available, "reservedForOrders": reserved, "inPies": 0.0},
            "investments": {"currentValue": invested, "totalCost": invested - 100.0, "realizedProfitLoss": 0.0, "unrealizedProfitLoss": 100.0}}


def position(ticker: str = "AAA_US_EQ", quantity: float = 10.0, average_price: float = 50.0, current_price: float = 51.0, total_cost: float = 400.0,
             currency: str = "USD") -> Json:
    return {"instrument": instrument(ticker, currency), "quantity": quantity, "quantityAvailableForTrading": quantity, "quantityInPies": 0.0,
            "averagePricePaid": average_price, "currentPrice": current_price, "createdAt": "2026-10-20T14:31:02.120Z",
            "walletImpact": {"currency": "GBP", "currentValue": total_cost * current_price / average_price, "totalCost": total_cost,
                             "unrealizedProfitLoss": total_cost * (current_price / average_price - 1.0), "fxImpact": None if currency == "GBP" else 1.25}}


def order(order_id: int = 501, ticker: str = "AAA_US_EQ", quantity: float = 10.0, status: str = "NEW", filled: float = 0.0, side: str | None = None) -> Json:
    """A MARKET order placed by quantity through the API, as the pending-orders read and the order placement answer it."""
    return {"id": order_id, "ticker": ticker, "instrument": instrument(ticker), "type": "MARKET", "side": side or ("BUY" if quantity > 0 else "SELL"),
            "status": status, "strategy": "QUANTITY", "quantity": quantity, "filledQuantity": filled, "currency": "GBP", "extendedHours": False,
            "initiatedFrom": "API", "createdAt": "2026-10-27T14:30:00.000Z"}


def tax(name: str = "CURRENCY_CONVERSION_FEE", amount: float = 0.6, currency: str = "GBP") -> Json:
    return {"name": name, "quantity": amount, "currency": currency, "chargedAt": "2026-10-27T14:30:01Z"}


def historical_order(order_id: int = 501, ticker: str = "AAA_US_EQ", quantity: float = 10.0, price: float = 50.1, fx_rate: float = 0.8, net_value: float | None = None,
                     taxes: list[Json] | None = None, filled_at: str = "2026-10-27T14:30:01.250Z") -> Json:
    """A filled MARKET order and its fill; the wallet impact is in GBP, net value = quantity x price x fx_rate unless given."""
    net = quantity * price * fx_rate if net_value is None else net_value
    return {"order": order(order_id, ticker, quantity, status="FILLED", filled=quantity),
            "fill": {"id": order_id * 10, "filledAt": filled_at, "price": price, "quantity": quantity, "tradingMethod": "TOTV", "type": "TRADE",
                     "walletImpact": {"currency": "GBP", "fxRate": fx_rate, "netValue": net, "realisedProfitLoss": 0.0,
                                      "taxes": [tax()] if taxes is None else taxes}}}


def dividend(ticker: str = "AAA_US_EQ") -> Json:
    return {"ticker": ticker, "instrument": instrument(ticker), "type": "ORDINARY", "quantity": 10.0, "grossAmountPerShare": 0.24, "amount": 1.63,
            "amountInEuro": 1.88, "currency": "GBP", "tickerCurrency": "USD", "paidOn": "2026-10-15T00:00:00Z", "reference": "D-1"}


def transaction(kind: str = "DEPOSIT", amount: float = 1000.0) -> Json:
    return {"type": kind, "amount": amount, "currency": "GBP", "dateTime": "2026-10-01T09:00:00Z", "reference": "T-1"}


def exchange() -> Json:
    return {"id": 1, "name": "NASDAQ", "workingSchedules": [{"id": 53, "timeEvents": [{"type": "OPEN", "date": "2026-10-27T13:30:00Z"},
                                                                                        {"type": "CLOSE", "date": "2026-10-27T20:00:00Z"}]}]}


def tradable_instrument(ticker: str = "AAA_US_EQ") -> Json:
    return {"ticker": ticker, "type": "STOCK", "name": "AAA Inc", "shortName": ticker.split("_")[0], "isin": "US0000000001", "currencyCode": "USD",
            "maxOpenQuantity": 1000.0, "extendedHours": False, "workingScheduleId": 53, "addedOn": "2019-01-01T00:00:00Z"}


def export_report(status: str = "Finished") -> Json:
    return {"reportId": 9, "status": status, "timeFrom": "2026-10-01T00:00:00Z", "timeTo": "2026-10-27T00:00:00Z",
            "dataIncluded": {"includeDividends": True, "includeInterest": True, "includeOrders": True, "includeTransactions": True},
            "downloadLink": "https://example.invalid/report.csv" if status == "Finished" else None}


def page(items: list[Json], next_page_path: str | None = None) -> Json:
    return {"items": items, "nextPagePath": next_page_path}


def body(value: object) -> bytes:
    """The bytes the API would send for `value`."""
    return json.dumps(value).encode()


class Clock:
    """The injected clock and pause: a pause advances the clock by its length."""

    def __init__(self, start: dt.datetime = T0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> dt.datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += dt.timedelta(seconds=seconds)


def headers(content_type: str = "application/json", rate: tuple[int, int, int, float] | None = None, now: dt.datetime = T0) -> email.message.Message:
    """Response headers; `rate` is (limit, period, remaining, seconds from `now` to the reset)."""
    m = email.message.Message()
    m["Content-Type"] = content_type
    if rate is not None:
        limit, period, remaining, reset_in = rate
        for k, v in {"limit": limit, "period": period, "remaining": remaining, "reset": int(now.timestamp() + reset_in), "used": limit - remaining}.items():
            m[f"x-ratelimit-{k}"] = str(v)
    return m


def ok(value: object, **kw: Any) -> Answer:
    """A 200 answer carrying `value` as JSON."""
    return 200, headers(**kw), body(value)


def status(code: int, payload: bytes = b'{"code": "x"}', **kw: Any) -> Answer:
    """An answer with HTTP status `code` (raised by the opener as urllib's HTTPError, as the real one does)."""
    return code, headers(**kw), payload


class Broker:
    """The practice host behind the opener the read client builds (urllib.request.build_opener, replaced): each request is recorded with its timeout and
    answered with the next scripted answer, an Answer or an exception to raise. A status of 300 or more is raised as HTTPError, a 2xx one returned."""

    def __init__(self) -> None:
        self.requests: list[tuple[urllib.request.Request, float]] = []
        self.answers: list[Answer | Exception] = []
        self.handlers: tuple[object, ...] = ()

    def open(self, req: urllib.request.Request, timeout: float) -> urllib.response.addinfourl:
        self.requests.append((req, timeout))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        code, hdrs, payload = a
        if code >= 300:                                     # as urllib does from 400 up, and the client's RefuseRedirects for 3xx
            raise urllib.error.HTTPError(req.full_url, code, "error", hdrs, io.BytesIO(payload))
        return urllib.response.addinfourl(io.BytesIO(payload), hdrs, req.full_url, code)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> Broker:
        def build_opener(*handlers: object) -> Broker:
            self.handlers = handlers
            return self

        monkeypatch.setattr(urllib.request, "build_opener", build_opener)
        return self

    def paths(self) -> list[str]:
        """The requested URLs, without the practice host."""
        return [r.full_url.split("/api/v0", 1)[1] for r, _ in self.requests]


class Engine:
    """The PAPER engine and its records in a temporary folder: plans written by quant.portfolio.plan.write_plan, sent by quant.execution.engine.send and reconciled by
    quant.execution.engine.reconcile_sent, against a broker replaced at urllib.request.urlopen (the engine's HTTP boundary). `positions` is what the account holds
    (answered to the engine's GET, in the documented shape); `answers` maps a ticker to the broker's answer to its order, or an exception to raise; without
    one the answer is a NEW market order. `kill_after` creates the kill-switch file after that many orders have been answered. Needs the `keys` fixture."""

    def __init__(self, root: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from quant.execution import audit as A
        from quant.execution import engine as E
        from quant.execution import ledger as LG
        from quant.portfolio import plan as P

        self.A, self.E, self.LG, self.P = A, E, LG, P
        for name in ("CONFIRM", "KILL", "LOCK"):
            monkeypatch.setattr(E, name, root / name.lower())
        monkeypatch.setattr(E, "SEND_BROKER", "t212")                           # the Trading 212 path stays in the code (A10 sends to Alpaca); these tests exercise it
        monkeypatch.setattr(P, "PLANS", root / "plans")
        monkeypatch.setattr(A, "PATH", root / "audit.jsonl")
        monkeypatch.setattr(LG, "LEDGER", root / "ledger.json")
        (root / "confirm").mkdir()
        self.positions: list[Json] = []
        self.answers: dict[str, Json | Exception] = {}
        self.kill_after: int | None = None
        self.orders_answered = 0
        monkeypatch.setattr(urllib.request, "urlopen", self._urlopen)

    def _urlopen(self, req: urllib.request.Request, timeout: float) -> io.BytesIO:
        if req.get_method() == "GET":
            return io.BytesIO(json.dumps(self.positions).encode())
        sent = json.loads(req.data)                                               # type: ignore[arg-type]  # the engine always sends a JSON body
        self.orders_answered += 1
        if self.kill_after is not None and self.orders_answered >= self.kill_after:
            self.E.KILL.write_text("stop")
        a = self.answers.get(sent["ticker"])
        if isinstance(a, Exception):
            raise a
        return io.BytesIO(json.dumps(a if a is not None else order(500 + self.orders_answered, sent["ticker"], sent["quantity"])).encode())

    def plan(self, tranche: int, target: dict[str, float], prices: dict[str, float], account_usd: float = 10000.0) -> dict[str, Any]:
        """A plan for `tranche` from its recorded holdings to `target` shares at `prices` (quant.portfolio.plan.make_plan), not yet written."""
        held = self.LG.tranche(self.LG.load(), tranche)
        p: dict[str, Any] = self.P.make_plan(target, held, prices, account_usd)
        p.update({"tranche": tranche, "held": held, "slot": "2026-10-27", "price_date": "2026-10-23", "exposure": 1.0})
        return p

    def send(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        """Write the plan, confirm it as the owner would, and send it through every gate."""
        self.P.write_plan(plan)
        (self.E.CONFIRM / f"{plan['plan_hash']}.token").write_text(plan["plan_hash"])
        out: list[dict[str, Any]] = self.E.send(plan, True, sleep=0)
        return out

    def reconcile(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = self.E.reconcile_sent(plan)
        return out
