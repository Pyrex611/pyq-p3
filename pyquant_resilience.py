"""
pyquant_resilience.py

Phase 1 (Pre-UI stabilization) — retry/backoff for the Binance Algo Order
API calls that place and cancel bracket (TP/SL) orders.

Why: `PositionGuard._place_bracket_order()` / `_place_bracket_order_fallback()`
/ `_cancel_sibling_algo_order()` previously wrapped a single network call in
a bare try/except — one transient error (timeout, 5xx, a dropped
connection) and the function just logs and gives up, leaving a position
with NO stop-loss or take-profit on the exchange. Given this project's own
history (per the original grading: "Binance API errors, positions not
closing" was the single biggest live-reliability complaint), this is the
highest-value resilience fix available.

Idempotency note: Binance's Algo Order "post" endpoint has no
client-supplied order-id field to de-duplicate a retry against (unlike
`futures_create_order`'s `newClientOrderId`). A request that times out
server-side-success/client-side-failure and then succeeds again on retry
could in principle place two STOP_MARKET or TAKE_PROFIT_MARKET orders for
the same position. `retry_algo_order()` guards against this by checking
the symbol's currently open algo orders before each retry attempt (not
before the first attempt, to avoid an extra API call on the common case)
and aborting the retry if an order of the same type already exists.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("pyquant.resilience")

# Errors worth retrying: timeouts, connection resets, and Binance's own
# transient/rate-limit codes. NOT retried: anything that looks like a
# rejected order (bad params, insufficient margin, etc.) — retrying those
# would just fail the same way N times and delay the fallback path.
_RETRYABLE_SNIPPETS = (
    "timed out", "timeout", "connection", "reset by peer",
    "-1021",  # INVALID_TIMESTAMP (clock skew — often transient)
    "-1001",  # DISCONNECTED
    "-1003",  # TOO_MANY_REQUESTS
    "502", "503", "504",
)


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(snippet in msg for snippet in _RETRYABLE_SNIPPETS)


def retry_api_call(fn: Callable, *args, max_retries: int = 3, base_delay: float = 1.0,
                    description: str = "API call", **kwargs) -> Any:
    """
    Calls fn(*args, **kwargs), retrying on a transient-looking failure with
    exponential backoff + jitter. Re-raises the last exception if every
    attempt fails. Use this for calls that are naturally idempotent to
    retry (cancels, reads) — for order-placement calls, use
    retry_algo_order() instead, which adds the duplicate-order guard.
    """
    last_err: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_err = e
            if attempt >= max_retries or not _is_retryable(e):
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            logger.warning(
                f"{description} failed (attempt {attempt}/{max_retries}): {e}. "
                f"Retrying in {delay:.1f}s…"
            )
            time.sleep(delay)
    raise last_err  # pragma: no cover — unreachable, keeps type-checkers happy


def retry_algo_order(place_fn: Callable, check_existing_fn: Callable[[], bool],
                      *, max_retries: int = 3, base_delay: float = 1.0,
                      description: str = "algo order placement") -> Any:
    """
    Places an algo order with retry, guarding against a duplicate on
    retry: before every attempt AFTER the first, calls
    `check_existing_fn()` — a zero-arg callable the caller builds to check
    whether an order of this type already exists for this symbol (e.g. via
    `algoOrders/current`) — and aborts the retry (returning None) if so,
    on the assumption the previous attempt actually succeeded server-side
    even though the response was lost.
    """
    last_err: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            try:
                if check_existing_fn():
                    logger.info(
                        f"{description}: an order already exists after a prior "
                        f"attempt's response was lost — not retrying (would duplicate)."
                    )
                    return None
            except Exception as e:
                logger.warning(f"{description}: existing-order check failed ({e}); retrying anyway.")
        try:
            return place_fn()
        except Exception as e:
            last_err = e
            if attempt >= max_retries or not _is_retryable(e):
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            logger.warning(
                f"{description} failed (attempt {attempt}/{max_retries}): {e}. "
                f"Retrying in {delay:.1f}s…"
            )
            time.sleep(delay)
    raise last_err  # pragma: no cover
