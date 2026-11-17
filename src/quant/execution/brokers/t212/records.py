"""Typed records of every documented Trading 212 read response (Public API v0, OpenAPI 3.0.1 "Trading 212 Public API v0", read 2026-10-27), and their strict parsers.

Field names follow the API in snake case, with the unit or time zone in the name: `_shares` for share quantities, `_instrument_ccy` for prices in the
instrument's currency, `_account_ccy` or `_wallet_ccy` for amounts in the account's primary currency (the API does not support multi-currency accounts, so the
two are the same), `_utc` for instants. Parsers take decoded JSON (quant.execution.brokers.t212.schema.loads) and refuse anything outside the documented schema (quant.execution.brokers.t212.schema).

Required or optional: the OpenAPI marks no field required, so the rule is the documented meaning. A field is optional (absent or null) only where the reference
says it applies to some objects and not others: an order's quantity, value, limit and stop prices and time in force (by strategy and type), a historical order's
fill (an order that never filled has none), a position's FX impact (only where the instrument currency differs from the account's), a report's download link
(until it is finished). Every other documented field is required, and the fields an optional one depends on are cross-checked (a LIMIT order needs its limit price).
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from quant.execution.brokers.t212.schema import Fields, parse_array, parse_object

T = TypeVar("T")

ORDER_STATUSES = frozenset({"LOCAL", "UNCONFIRMED", "CONFIRMED", "NEW", "CANCELLING", "CANCELLED", "PARTIALLY_FILLED", "FILLED", "REJECTED", "REPLACING", "REPLACED"})
ORDER_FINAL_STATUSES = frozenset({"FILLED", "CANCELLED", "REJECTED", "REPLACED"})           # nothing further happens to an order in these states
ORDER_NOT_EXECUTED_STATUSES = frozenset({"CANCELLED", "REJECTED"})                         # final, and nothing was (or will be) bought or sold
ORDER_TYPES = frozenset({"LIMIT", "STOP", "MARKET", "STOP_LIMIT"})
ORDER_SIDES = frozenset({"BUY", "SELL"})
ORDER_STRATEGIES = frozenset({"QUANTITY", "VALUE"})
INITIATED_FROM = frozenset({"API", "IOS", "ANDROID", "WEB", "SYSTEM", "AUTOINVEST", "INSTRUMENT_AUTOINVEST"})
TIME_VALIDITIES = frozenset({"DAY", "GOOD_TILL_CANCEL"})
TAX_NAMES = frozenset({"COMMISSION_TURNOVER", "CURRENCY_CONVERSION_FEE", "FINRA_FEE", "FRENCH_TRANSACTION_TAX", "PTM_LEVY", "STAMP_DUTY", "STAMP_DUTY_RESERVE_TAX",
                       "TRANSACTION_FEE"})
FILL_TYPES = frozenset({"TRADE", "STOCK_SPLIT", "STOCK_DISTRIBUTION", "FOP", "FOP_CORRECTION", "CUSTOM_STOCK_DISTRIBUTION", "EQUITY_RIGHTS", "SCRIP_STOCK_DIVIDENDS",
                        "STOCK_DIVIDENDS", "STOCK_ACQUISITION", "CASH_AND_STOCK_ACQUISITION", "SPIN_OFF"})
TRADING_METHODS = frozenset({"TOTV", "OTC"})
TRANSACTION_TYPES = frozenset({"WITHDRAW", "DEPOSIT", "FEE", "TRANSFER", "INTEREST_ON_FREE_CASH", "LENDING_INTEREST"})
TIME_EVENT_TYPES = frozenset({"OPEN", "CLOSE", "BREAK_START", "BREAK_END", "PRE_MARKET_OPEN", "AFTER_HOURS_OPEN", "AFTER_HOURS_CLOSE", "OVERNIGHT_OPEN"})
INSTRUMENT_TYPES = frozenset({"CRYPTOCURRENCY", "ETF", "FOREX", "FUTURES", "INDEX", "STOCK", "WARRANT", "CRYPTO", "CVR", "CORPACT"})
REPORT_STATUSES = frozenset({"Queued", "Processing", "Running", "Canceled", "Failed", "Finished"})
DIVIDEND_TYPES = frozenset({
    "ORDINARY", "BONUS", "PROPERTY_INCOME", "RETURN_OF_CAPITAL_NON_US", "DEMERGER", "INTEREST", "CAPITAL_GAINS_DISTRIBUTION_NON_US", "INTERIM_LIQUIDATION",
    "ORDINARY_MANUFACTURED_PAYMENT", "BONUS_MANUFACTURED_PAYMENT", "PROPERTY_INCOME_MANUFACTURED_PAYMENT", "RETURN_OF_CAPITAL_NON_US_MANUFACTURED_PAYMENT",
    "DEMERGER_MANUFACTURED_PAYMENT", "INTEREST_MANUFACTURED_PAYMENT", "CAPITAL_GAINS_DISTRIBUTION_NON_US_MANUFACTURED_PAYMENT",
    "INTERIM_LIQUIDATION_MANUFACTURED_PAYMENT", "INTEREST_PAID_BY_US_OBLIGORS", "INTEREST_PAID_BY_FOREIGN_CORPORATIONS", "DIVIDENDS_PAID_BY_US_CORPORATIONS",
    "DIVIDENDS_PAID_BY_FOREIGN_CORPORATIONS", "CAPITAL_GAINS", "REAL_PROPERTY_INCOME_AND_NATURAL_RESOURCES_ROYALTIES", "OTHER_INCOME", "QUALIFIED_INVESTMENT_ENTITY",
    "TRUST_DISTRIBUTION", "PUBLICLY_TRADED_PARTNERSHIP_DISTRIBUTION", "CAPITAL_GAINS_DISTRIBUTION", "RETURN_OF_CAPITAL", "OTHER_DIVIDEND_EQUIVALENT",
    "TAX_EVENT_1446F_FOR_PUBLICLY_TRADED_SECURITIES", "PTP_UNCHARACTERISED_INCOME", "MULTIPLE_1042S_TAX_COMPONENTS", "DIVIDEND", "SHORT_TERM_CAPITAL_GAINS",
    "LONG_TERM_CAPITAL_GAINS", "PROPERTY_INCOME_DISTRIBUTION", "TAX_EXEMPTED", "INTEREST_PAID_BY_US_OBLIGORS_MANUFACTURED_PAYMENT",
    "INTEREST_PAID_BY_FOREIGN_CORPORATIONS_MANUFACTURED_PAYMENT", "DIVIDENDS_PAID_BY_US_CORPORATIONS_MANUFACTURED_PAYMENT",
    "DIVIDENDS_PAID_BY_FOREIGN_CORPORATIONS_MANUFACTURED_PAYMENT", "CAPITAL_GAINS_MANUFACTURED_PAYMENT",
    "REAL_PROPERTY_INCOME_AND_NATURAL_RESOURCES_ROYALTIES_MANUFACTURED_PAYMENT", "OTHER_INCOME_MANUFACTURED_PAYMENT", "QUALIFIED_INVESTMENT_ENTITY_MANUFACTURED_PAYMENT",
    "TRUST_DISTRIBUTION_MANUFACTURED_PAYMENT", "PUBLICLY_TRADED_PARTNERSHIP_DISTRIBUTION_MANUFACTURED_PAYMENT", "CAPITAL_GAINS_DISTRIBUTION_MANUFACTURED_PAYMENT",
    "RETURN_OF_CAPITAL_MANUFACTURED_PAYMENT", "OTHER_DIVIDEND_EQUIVALENT_MANUFACTURED_PAYMENT", "TAX_EVENT_1446F_FOR_PUBLICLY_TRADED_SECURITIES_MANUFACTURED_PAYMENT",
    "PTP_UNCHARACTERISED_INCOME_MANUFACTURED_PAYMENT", "MULTIPLE_1042S_TAX_COMPONENTS_MANUFACTURED_PAYMENT", "DIVIDEND_MANUFACTURED_PAYMENT",
    "SHORT_TERM_CAPITAL_GAINS_MANUFACTURED_PAYMENT", "LONG_TERM_CAPITAL_GAINS_MANUFACTURED_PAYMENT", "PROPERTY_INCOME_DISTRIBUTION_MANUFACTURED_PAYMENT",
    "TAX_EXEMPTED_MANUFACTURED_PAYMENT",
})


@dataclass(frozen=True, slots=True, kw_only=True)
class Instrument:
    """An instrument as embedded in positions, orders and dividends. `currency` is the instrument's currency."""
    ticker: str
    name: str
    isin: str
    currency: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Cash:
    available_to_trade_account_ccy: float
    reserved_for_orders_account_ccy: float
    in_pies_account_ccy: float


@dataclass(frozen=True, slots=True, kw_only=True)
class Investments:
    current_value_account_ccy: float
    total_cost_account_ccy: float
    realized_profit_loss_account_ccy: float
    unrealized_profit_loss_account_ccy: float


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountSummary:
    """GET account summary: the account number, its primary currency and the cash and investment amounts in that currency."""
    account_id: int
    account_currency: str
    total_value_account_ccy: float
    cash: Cash
    investments: Investments


@dataclass(frozen=True, slots=True, kw_only=True)
class PositionWalletImpact:
    """A position's effect on the account, in `currency` (the account's primary currency). `fx_impact_wallet_ccy` is None for an instrument in the account currency."""
    currency: str
    current_value_wallet_ccy: float
    total_cost_wallet_ccy: float
    unrealized_profit_loss_wallet_ccy: float
    fx_impact_wallet_ccy: float | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Position:
    """One open position. Prices are per share in the instrument's currency."""
    instrument: Instrument
    quantity_shares: float
    quantity_available_for_trading_shares: float
    quantity_in_pies_shares: float
    average_price_paid_instrument_ccy: float
    current_price_instrument_ccy: float
    created_at_utc: dt.datetime
    wallet_impact: PositionWalletImpact

    @property
    def ticker(self) -> str:
        return self.instrument.ticker


@dataclass(frozen=True, slots=True, kw_only=True)
class Order:
    """An order, as the pending-orders read and the order placement answer it. Monetary fields are in `currency`, the order's currency; quantity and value fields
    apply to QUANTITY and VALUE orders respectively, limit and stop prices to the order types that have them."""
    order_id: int
    ticker: str
    instrument: Instrument
    order_type: str
    side: str
    status: str
    strategy: str
    quantity_shares: float | None
    filled_quantity_shares: float | None
    value_order_ccy: float | None
    filled_value_order_ccy: float | None
    limit_price_order_ccy: float | None
    stop_price_order_ccy: float | None
    currency: str
    time_in_force: str | None
    extended_hours: bool
    initiated_from: str
    created_at_utc: dt.datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class Tax:
    """A fee or tax charged on a fill (`name`), `amount` in `currency`."""
    name: str
    amount: float
    currency: str
    charged_at_utc: dt.datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class FillWalletImpact:
    """A fill's effect on the account in `currency`. `fx_rate` is as the API reports it (its direction is to be confirmed against a real fill:
    quant.execution.brokers.t212.fills infers it from the values)."""
    currency: str
    fx_rate: float
    net_value_wallet_ccy: float
    realised_profit_loss_wallet_ccy: float
    taxes: tuple[Tax, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class Fill:
    fill_id: int
    filled_at_utc: dt.datetime
    price_instrument_ccy: float
    quantity_shares: float
    trading_method: str
    fill_type: str
    wallet_impact: FillWalletImpact


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoricalOrder:
    """One item of the historical-orders read: the order and, if it executed, its fill."""
    order: Order
    fill: Fill | None


@dataclass(frozen=True, slots=True, kw_only=True)
class DividendItem:
    """A paid dividend: `amount_account_ccy` in the account currency (`account_currency`), the gross amount per share in the instrument's currency."""
    ticker: str
    instrument: Instrument
    dividend_type: str
    quantity_shares: float
    gross_amount_per_share_instrument_ccy: float
    amount_account_ccy: float
    amount_eur: float
    account_currency: str
    ticker_currency: str
    paid_on_utc: dt.datetime
    reference: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Transaction:
    """A cash movement to or from the account (deposit, withdrawal, fee, transfer, interest), `amount` in `currency`."""
    transaction_type: str
    amount: float
    currency: str
    at_utc: dt.datetime
    reference: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TimeEvent:
    event_type: str
    at_utc: dt.datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkingSchedule:
    schedule_id: int
    time_events: tuple[TimeEvent, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class Exchange:
    exchange_id: int
    name: str
    working_schedules: tuple[WorkingSchedule, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class TradableInstrument:
    """One entry of the instrument list; `currency` is the instrument's currency and `working_schedule_id` refers to an exchange's working schedule."""
    ticker: str
    instrument_type: str
    name: str
    short_name: str
    isin: str
    currency: str
    max_open_quantity_shares: float
    extended_hours: bool
    working_schedule_id: int
    added_on_utc: dt.datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ReportDataIncluded:
    dividends: bool
    interest: bool
    orders: bool
    transactions: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportReport:
    """A requested CSV export and its state; `download_link` is set once it is finished (this layer never follows it: it points away from the practice host)."""
    report_id: int
    status: str
    time_from_utc: dt.datetime
    time_to_utc: dt.datetime
    data_included: ReportDataIncluded
    download_link: str | None


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    """One page of a paginated read: its items and the path of the next page (None on the last page)."""
    items: tuple[T, ...]
    next_page_path: str | None


def _instrument(f: Fields) -> Instrument:
    return Instrument(ticker=f.text("ticker"), name=f.text("name"), isin=f.text("isin"), currency=f.currency("currency"))


def _cash(f: Fields) -> Cash:
    return Cash(available_to_trade_account_ccy=f.number("availableToTrade"), reserved_for_orders_account_ccy=f.number("reservedForOrders"),
                in_pies_account_ccy=f.number("inPies"))


def _investments(f: Fields) -> Investments:
    return Investments(current_value_account_ccy=f.number("currentValue"), total_cost_account_ccy=f.number("totalCost"),
                       realized_profit_loss_account_ccy=f.number("realizedProfitLoss"), unrealized_profit_loss_account_ccy=f.number("unrealizedProfitLoss"))


def _account_summary(f: Fields) -> AccountSummary:
    return AccountSummary(account_id=f.integer("id"), account_currency=f.currency("currency"), total_value_account_ccy=f.number("totalValue"),
                          cash=f.record("cash", _cash), investments=f.record("investments", _investments))


def _position_wallet_impact(f: Fields) -> PositionWalletImpact:
    return PositionWalletImpact(currency=f.currency("currency"), current_value_wallet_ccy=f.number("currentValue"), total_cost_wallet_ccy=f.number("totalCost"),
                                unrealized_profit_loss_wallet_ccy=f.number("unrealizedProfitLoss"), fx_impact_wallet_ccy=f.number_or_none("fxImpact"))


def _position(f: Fields) -> Position:
    return Position(instrument=f.record("instrument", _instrument), quantity_shares=f.number("quantity"),
                    quantity_available_for_trading_shares=f.number("quantityAvailableForTrading"), quantity_in_pies_shares=f.number("quantityInPies"),
                    average_price_paid_instrument_ccy=f.number("averagePricePaid"), current_price_instrument_ccy=f.number("currentPrice"),
                    created_at_utc=f.timestamp("createdAt"), wallet_impact=f.record("walletImpact", _position_wallet_impact))


def _order(f: Fields) -> Order:
    o = Order(order_id=f.integer("id"), ticker=f.text("ticker"), instrument=f.record("instrument", _instrument), order_type=f.choice("type", ORDER_TYPES),
              side=f.choice("side", ORDER_SIDES), status=f.choice("status", ORDER_STATUSES), strategy=f.choice("strategy", ORDER_STRATEGIES),
              quantity_shares=f.number_or_none("quantity"), filled_quantity_shares=f.number_or_none("filledQuantity"), value_order_ccy=f.number_or_none("value"),
              filled_value_order_ccy=f.number_or_none("filledValue"), limit_price_order_ccy=f.number_or_none("limitPrice"),
              stop_price_order_ccy=f.number_or_none("stopPrice"), currency=f.currency("currency"),
              time_in_force=f.choice_or_none("timeInForce", TIME_VALIDITIES),
              extended_hours=f.flag("extendedHours"), initiated_from=f.choice("initiatedFrom", INITIATED_FROM), created_at_utc=f.timestamp("createdAt"))
    if o.ticker != o.instrument.ticker:
        f.fail(f"ticker {o.ticker!r} differs from instrument.ticker {o.instrument.ticker!r}")
    if o.strategy == "QUANTITY" and o.quantity_shares is None:
        f.fail("a QUANTITY order without quantity")
    if o.strategy == "VALUE" and o.value_order_ccy is None:
        f.fail("a VALUE order without value")
    if o.order_type in ("LIMIT", "STOP_LIMIT") and o.limit_price_order_ccy is None:
        f.fail(f"a {o.order_type} order without limitPrice")
    if o.order_type in ("STOP", "STOP_LIMIT") and o.stop_price_order_ccy is None:
        f.fail(f"a {o.order_type} order without stopPrice")
    return o


def _tax(f: Fields) -> Tax:
    return Tax(name=f.choice("name", TAX_NAMES), amount=f.number("quantity"), currency=f.currency("currency"), charged_at_utc=f.timestamp("chargedAt"))


def _fill_wallet_impact(f: Fields) -> FillWalletImpact:
    return FillWalletImpact(currency=f.currency("currency"), fx_rate=f.number("fxRate"), net_value_wallet_ccy=f.number("netValue"),
                            realised_profit_loss_wallet_ccy=f.number("realisedProfitLoss"), taxes=f.items("taxes", _tax))


def _fill(f: Fields) -> Fill:
    return Fill(fill_id=f.integer("id"), filled_at_utc=f.timestamp("filledAt"), price_instrument_ccy=f.number("price"), quantity_shares=f.number("quantity"),
                trading_method=f.choice("tradingMethod", TRADING_METHODS), fill_type=f.choice("type", FILL_TYPES), wallet_impact=f.record("walletImpact", _fill_wallet_impact))


def _historical_order(f: Fields) -> HistoricalOrder:
    return HistoricalOrder(order=f.record("order", _order), fill=f.record_or_none("fill", _fill))


def _dividend(f: Fields) -> DividendItem:
    d = DividendItem(ticker=f.text("ticker"), instrument=f.record("instrument", _instrument), dividend_type=f.choice("type", DIVIDEND_TYPES),
                     quantity_shares=f.number("quantity"), gross_amount_per_share_instrument_ccy=f.number("grossAmountPerShare"), amount_account_ccy=f.number("amount"),
                     amount_eur=f.number("amountInEuro"), account_currency=f.currency("currency"), ticker_currency=f.currency("tickerCurrency"),
                     paid_on_utc=f.timestamp("paidOn"), reference=f.text("reference"))
    if d.ticker != d.instrument.ticker:
        f.fail(f"ticker {d.ticker!r} differs from instrument.ticker {d.instrument.ticker!r}")
    return d


def _transaction(f: Fields) -> Transaction:
    return Transaction(transaction_type=f.choice("type", TRANSACTION_TYPES), amount=f.number("amount"), currency=f.currency("currency"),
                       at_utc=f.timestamp("dateTime"), reference=f.text("reference"))


def _time_event(f: Fields) -> TimeEvent:
    return TimeEvent(event_type=f.choice("type", TIME_EVENT_TYPES), at_utc=f.timestamp("date"))


def _working_schedule(f: Fields) -> WorkingSchedule:
    return WorkingSchedule(schedule_id=f.integer("id"), time_events=f.items("timeEvents", _time_event))


def _exchange(f: Fields) -> Exchange:
    return Exchange(exchange_id=f.integer("id"), name=f.text("name"), working_schedules=f.items("workingSchedules", _working_schedule))


def _tradable_instrument(f: Fields) -> TradableInstrument:
    return TradableInstrument(ticker=f.text("ticker"), instrument_type=f.choice("type", INSTRUMENT_TYPES), name=f.text("name"), short_name=f.text("shortName"),
                              isin=f.text("isin"), currency=f.currency("currencyCode"), max_open_quantity_shares=f.number("maxOpenQuantity"),
                              extended_hours=f.flag("extendedHours"), working_schedule_id=f.integer("workingScheduleId"), added_on_utc=f.timestamp("addedOn"))


def _report_data_included(f: Fields) -> ReportDataIncluded:
    return ReportDataIncluded(dividends=f.flag("includeDividends"), interest=f.flag("includeInterest"), orders=f.flag("includeOrders"),
                              transactions=f.flag("includeTransactions"))


def _export_report(f: Fields) -> ExportReport:
    r = ExportReport(report_id=f.integer("reportId"), status=f.choice("status", REPORT_STATUSES), time_from_utc=f.timestamp("timeFrom"),
                     time_to_utc=f.timestamp("timeTo"), data_included=f.record("dataIncluded", _report_data_included), download_link=f.text_or_none("downloadLink"))
    if r.status == "Finished" and r.download_link is None:
        f.fail("a Finished report without downloadLink")
    return r


def _page(item: Callable[[Fields], T]) -> Callable[[Fields], Page[T]]:
    def read(f: Fields) -> Page[T]:
        f.require_key("nextPagePath")                       # documented as always present: null on the last page
        return Page(items=f.items("items", item), next_page_path=f.text_or_none("nextPagePath"))
    return read


def parse_account_summary(value: object) -> AccountSummary:
    return parse_object(value, _account_summary)


def parse_positions(value: object) -> tuple[Position, ...]:
    return parse_array(value, _position)


def parse_order(value: object, path: str = "$") -> Order:
    """One order: the body of a pending-order read or of an order placement answer (as the engine records it in the audit log)."""
    return parse_object(value, _order, path)


def parse_orders(value: object) -> tuple[Order, ...]:
    return parse_array(value, _order)


def parse_historical_orders_page(value: object) -> Page[HistoricalOrder]:
    return parse_object(value, _page(_historical_order))


def parse_dividends_page(value: object) -> Page[DividendItem]:
    return parse_object(value, _page(_dividend))


def parse_transactions_page(value: object) -> Page[Transaction]:
    return parse_object(value, _page(_transaction))


def parse_exchanges(value: object) -> tuple[Exchange, ...]:
    return parse_array(value, _exchange)


def parse_instruments(value: object) -> tuple[TradableInstrument, ...]:
    return parse_array(value, _tradable_instrument)


def parse_exports(value: object) -> tuple[ExportReport, ...]:
    return parse_array(value, _export_report)
