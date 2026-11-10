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


# ── Optimizer objective — trade-frequency + win-rate aware (2026-11-08) ─────
# Added after the owner's own observation from the live engine's first
# two trades (both winners, ~5% avg): "the thresholds are too strict...
# we're not testing the platform much if no signal is being generated."
# Both walkforward.py's and p4_search.py's _evaluate_params()/objective()
# previously scored a candidate (strict/stricts) purely by total % return
# over the search window — nothing in that objective rewards firing more
# often or penalizes firing too rarely, so a pure-return-maximizing
# search naturally gravitates toward whatever thresholds are strict
# enough to be rare-but-accurate, if that happens to compound to a
# higher total return than trading more often — which is exactly what's
# been observed live. This adds two smooth penalty terms on top of the
# existing pnl_pct objective so the search explicitly favors landing in
# a target trading-frequency band with a minimum win-rate floor, while
# still preferring higher return among candidates that satisfy both.
#
# TARGET_TRADES_PER_DAY_RANGE reflects the owner's own two statements in
# the same message: "about 3-4 trades a day are okay" and, restated more
# aggressively a few lines later, "2-5 times a day" — (2.0, 5.0) covers
# both without contradicting either.
#
# MIN_WIN_RATE_FLOOR is set to 0.70, not the owner's stated "about 75%"
# target — deliberately, to leave margin for backtest-to-live execution
# differences a backtest can't fully capture (slippage, fees, and the
# day-bucketing/partial-bar effects documented in PLAN.md's 2026-11-07/
# 2026-11-08 entries). A search tuned to exactly hit 75% in-backtest has
# no slack left once real execution differs even slightly.
#
# The penalty scale constants below (*_PENALTY_PER_*) are a first pass,
# not empirically validated against real data — this sandbox has no
# network access to Alpaca, so there's no way to run the real optimizer
# here and see where it actually converges. Check the next optimizer
# run's resulting trades_per_day/win_rate (now surfaced in its returned
# dict — see score_candidate()'s docstring) and adjust these scales if
# the search isn't landing in the target band as intended.
TARGET_TRADES_PER_DAY_RANGE = (2.0, 5.0)
MIN_WIN_RATE_FLOOR = 0.70
FREQUENCY_PENALTY_PER_TRADE_PER_DAY = 40.0  # score lost per 1.0 trades/day outside the target range
WIN_RATE_PENALTY_PER_PCT_POINT = 3.0        # score lost per percentage point of win rate below the floor


def score_candidate(pnl_pct: float, n_trades: int, n_wins: int, days_evaluated: float) -> dict:
    """Composite optimizer objective: total return, penalized for
    landing outside TARGET_TRADES_PER_DAY_RANGE or below
    MIN_WIN_RATE_FLOOR. Both walkforward.py's and p4_search.py's
    objective()/run_grid_search() call this on every candidate instead
    of returning pnl_pct directly, so the two optimizers can't silently
    diverge on what "better" means (same standing rule as the SL/TP math
    above: shared scoring logic lives here only).

    Returns a dict (not just the score) so every call site can surface
    the real numbers behind a candidate — n_trades/win_rate/trades_per_day
    — not just the opaque composite value, which previously hid what the
    "best" params actually DO beyond their aggregate return.

    `n_trades`/`n_wins` count only real closed trades (SL/TP/force-close
    exits) — never the end-of-window mark-to-market used for ranking
    only (see both _evaluate_params()'s docstrings), since that's not a
    trade that was ever actually realized.
    """
    win_rate = (n_wins / n_trades) if n_trades > 0 else 0.0
    trades_per_day = (n_trades / days_evaluated) if days_evaluated > 0 else 0.0

    score = pnl_pct

    lo, hi = TARGET_TRADES_PER_DAY_RANGE
    if trades_per_day < lo:
        score -= (lo - trades_per_day) * FREQUENCY_PENALTY_PER_TRADE_PER_DAY
    elif trades_per_day > hi:
        score -= (trades_per_day - hi) * FREQUENCY_PENALTY_PER_TRADE_PER_DAY

    if n_trades > 0 and win_rate < MIN_WIN_RATE_FLOOR:
        score -= (MIN_WIN_RATE_FLOOR - win_rate) * 100.0 * WIN_RATE_PENALTY_PER_PCT_POINT

    return {
        "score": score,
        "pnl_pct": pnl_pct,
        "n_trades": n_trades,
        "win_rate": win_rate,
        "trades_per_day": trades_per_day,
    }
