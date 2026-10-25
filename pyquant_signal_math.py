"""
pyquant_signal_math.py

Shared signal / stop-loss / take-profit / profit-target math for
pyq_p4.py, p4_utils.py and walkforward.py.

Added as part of the "Phase 0 — port walkforward's validated logic into
live" unification pass (see the PyQuant Stabilization Sprint plan doc).
walkforward.py is the project's trusted, validated reference
implementation; the constants and formulas below are extracted here so
live and walkforward can no longer drift apart from each other by
accident — a change to a shared value now has to be made once, in one
place, and is immediately visible to both.

These values reflect walkforward.py's "v4 Production Edition" validated
baseline (a 15-day walk-forward run: +276.95% return, 86.7% win rate,
Sharpe 16.73), itself the product of substantially more historical
walkforward/backtest tuning than that one run alone — NOT a single
experiment. Do not "fix" these by re-deriving them from first principles;
change walkforward.py first, confirm there, then port the change here.
"""
from __future__ import annotations

from typing import Optional

# ── Leverage ────────────────────────────────────────────────────────────────
# 10x, confirmed by extensive walkforward/backtest tuning (not an
# arbitrary live-only choice).
LEVERAGE_DEFAULT = 10

# ── ATR-adaptive stop-loss ───────────────────────────────────────────────────
# Per-symbol fixed-dollar SL delta, used when ATR is unavailable, and the
# floor beneath which the ATR-adaptive delta is never allowed to shrink.
# $225 BTC ( = 7.5 * 30 ) / $16 ETH ( = 16.0 * 1 ) fixed deltas;
# $20 BTC / $8 ETH ATR floors — exactly walkforward.py's BTC_SL_DELTA /
# ETH_SL_DELTA / BTC_SL_FLOOR / ETH_SL_FLOOR.
SL_FIXED_DELTA = {"BTC": 7.5 * 30, "ETH": 16.0 * 1}
SL_FLOOR = {"BTC": 20.0, "ETH": 8.0}
DEFAULT_ATR_MULTIPLIER = 1.8

# ── Profit-target early exit ─────────────────────────────────────────────────
# Fraction of margin-at-risk (the capital actually allocated to a trade,
# i.e. (position_size * entry_price) / leverage) at which a position is
# closed early via MARKET even though neither TP nor SL has been hit yet.
# Ported directly from walkforward.py's validated run.
PROFIT_TARGET_FRACTION = {"BTC": 0.225, "ETH": 0.175}


def symbol_key(symbol: str) -> str:
    """Normalizes any of the system's symbol spellings (BTC/USD, BTC, BTCUSDT, ...) to 'BTC' or 'ETH'."""
    s = symbol.upper()
    if "BTC" in s:
        return "BTC"
    if "ETH" in s:
        return "ETH"
    raise ValueError(f"Unknown symbol: {symbol}")


def sl_delta(symbol: str, atr: float = 0.0, atr_mult: Optional[float] = None) -> float:
    """
    The stop-loss distance (in price units) from entry for `symbol`.
    ATR-adaptive when `atr` and `atr_mult` are supplied (and atr > 0);
    otherwise the fixed-dollar delta. Matches walkforward.py's
    `sl_price()` delta calculation exactly.
    """
    key = symbol_key(symbol)
    floor = SL_FLOOR[key]
    if atr and atr > 0 and atr_mult is not None:
        return max(atr_mult * atr, floor)
    return SL_FIXED_DELTA[key]


def compute_profit(side: str, entry: float, exit_price: float, qty: float) -> float:
    """Signed PnL in quote currency. Matches walkforward.py's compute_profit()."""
    return (exit_price - entry) * qty if side.capitalize() == "Buy" else (entry - exit_price) * qty


def outcome_pct(side: str, entry_price: float, exit_price: float,
                 leverage: float = LEVERAGE_DEFAULT) -> float:
    """Signed, LEVERAGED percentage return on the capital allocated to a
    trade — e.g. 0.0183 means "+1.83%" of the margin actually at risk, not
    of the full wallet balance.

    Added 2026-10-25: SIGNAL_ONLY_MODE has no real wallet balance to diff
    a close against (Binance is geo-blocked / never called), so a
    dollar-based realized_pnl is either unavailable or, if computed
    against a stale/placeholder balance figure, meaningless — see
    pyq_p4.py's `_check_signal_only_closes()` docstring for the Oct 25
    "+$3.00 on every SL hit" incident this replaces. A plain price-return
    percentage needs no balance at all, and multiplying by leverage keeps
    it comparable to how a real leveraged account's return would look."""
    if entry_price <= 0:
        return 0.0
    raw = (exit_price - entry_price) / entry_price if side.capitalize() == "Buy" \
        else (entry_price - exit_price) / entry_price
    return raw * leverage


def margin_at_risk(position_size: float, entry_price: float, leverage: float = LEVERAGE_DEFAULT) -> float:
    """The capital actually allocated to a position: (qty * entry) / leverage."""
    if leverage <= 0:
        return 0.0
    return (position_size * entry_price) / leverage


def profit_target_threshold(symbol: str, position_size: float, entry_price: float,
                             leverage: float = LEVERAGE_DEFAULT) -> float:
    """
    The unrealized-PnL level at which `symbol`'s open position should be
    closed early (MARKET), per walkforward's validated profit-target
    rule: margin_at_risk * PROFIT_TARGET_FRACTION[symbol].
    """
    key = symbol_key(symbol)
    return margin_at_risk(position_size, entry_price, leverage) * PROFIT_TARGET_FRACTION[key]


def expected_max_loss(entry_price: float, sl_price: float, allocated_capital: float,
                       leverage: float = LEVERAGE_DEFAULT) -> float:
    """
    The dollar loss expected if SL is hit, calibrated against the capital
    actually allocated to THIS trade — not the full wallet balance at
    signal-generation time (that mismatch was Phase 0's expected_max_loss
    bug: single-signal trades allocate 80% of wallet balance and
    double-signal trades split 50/50, but the original balance-based
    figure didn't know which split would apply yet).
    """
    if entry_price <= 0:
        return 0.0
    pos_est = (allocated_capital * leverage) / entry_price
    return abs(entry_price - sl_price) * pos_est
