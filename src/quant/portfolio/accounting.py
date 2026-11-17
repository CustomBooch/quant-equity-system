"""The monthly account return under the paper account's retail terms: the one definition of how a month's legs become an account return.

    r = L x long + h x hedge - Ss x short - Ss x borrow / 12 + max(1 - L - h, 0) x rf - max(L + h - 1 - Ss, 0) x (rf + margin spread / 12) - cost

long, short and hedge are the returns of each leg per unit of weight (a basket whose weights sum to 1); L, Ss and h are the legs' weights in the account. Short
proceeds earn nothing; cash beyond the legs earns rf; a debit beyond the short proceeds pays rf plus the margin spread; borrow is charged on the short leg only.
`cost` is the month's trading cost, which each caller measures its own way (the backtest: basket turnover plus changes of leg size; the shadow recorder: the sum of
absolute weight changes), so it is an input here.

The backtest's accounting (quant.research.risk.shortn.account, LONGSHORT V1's retail terms) and the shadow recorder (quant.portfolio.shadow.arm_return) are
registered code and keep their own lines; tests/contracts/test_accounting.py checks that both equal this function month by month, so a forward evaluator that uses
it scores months the same way the research did.
"""
from __future__ import annotations

COST, BORROW, MARGIN_SPREAD = 2.6e-4, 0.005, 0.025          # cost per unit of weight traded, borrow a year on the short leg, margin spread a year


def financing(L: float, Ss: float, h: float, rf: float, margin_spread: float = MARGIN_SPREAD) -> float:
    """Interest on the account's cash or debit for one month (rf monthly)."""
    return max(1 - L - h, 0.0) * rf - max(L + h - 1 - Ss, 0.0) * (rf + margin_spread / 12)


def account_month(long: float, short: float, hedge: float, L: float, Ss: float, h: float, rf: float, cost: float = 0.0, borrow: float = BORROW,
                  margin_spread: float = MARGIN_SPREAD) -> float:
    """The account's return for one month (see the module docstring)."""
    return L * long + h * hedge - Ss * short - Ss * borrow / 12 + financing(L, Ss, h, rf, margin_spread) - cost


def sleeve_month(short: float, hedge: float, Ss: float, h: float, borrow: float = BORROW) -> float:
    """The short sleeve's part of the month: shorts, their borrow and the hedge (trading cost and financing excluded)."""
    return -Ss * short + h * hedge - Ss * borrow / 12
