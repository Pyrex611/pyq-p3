# Binance "restricted location" error on GitHub Actions — diagnosis & options

## What's happening

As of mid-October 2026, every GitHub Actions cron run has been failing.
Each run dies in a consistent ~60 seconds, during the "Run one orchestra
tick" step. The error Binance returns is a **compliance/geofencing
block** — something like:

```
Service unavailable from a restricted location according to 'b. Eligibility'
```

This is **not** a credentials problem, not a network blip, and not
something that clears up if you wait or retry. Binance's compliance
system blocks API access from IP ranges it has classified as
ineligible — and this net is cast much wider than "certain countries."
It is understood to also catch broad swaths of **cloud/datacenter IP
ranges** (AWS, GCP, Azure, DigitalOcean, Oracle Cloud, Hetzner, and —
relevant here — GitHub Actions' own hosted-runner ranges), specifically
*because* those ranges are common sources of automated bot traffic that
bypasses normal jurisdiction checks. A residential/home IP is generally
not subject to this.

## Root cause in this codebase (two separate bugs, now both addressed)

1. **Already fixed, this session, earlier**: `pyq_p4.py` was retraining
   all 18 UKF filters from scratch on every single tick (a full Alpaca
   historical fetch + training loop), only to immediately discard that
   work and restore from persisted state anyway. Expensive and pointless,
   but not what was crashing the process — see DOCS.md Section 10.

2. **The actual cause of the current 100% failure rate**: `p4_utils.py`
   builds the Binance client **unconditionally at module import time**:
   `binance_client = _create_binance_client()`, with a 5-retry × 5s-delay
   loop that raises `ConnectionError` if every retry fails. Because
   `cron_runner.py` does a plain top-level `import pyq_p4` with no
   try/except around it, that one raised exception kills the **entire
   process** — not just the trading-related tasks, but signal generation,
   logging, everything — on every tick, before any of `pyq_p4.py`'s own
   logic (including `SIGNAL_ONLY_MODE`'s checks) ever runs. That's the
   exact ~60-second failure window seen in every recent run (5 retries ×
   5s delay ≈ 25-30s, plus import/connection overhead).

## The fix, round 2 — a live run found the first fix was incomplete

Round 1 (previous delivery) only patched `_create_binance_client()`
itself. A live GitHub Actions run after that fix was applied surfaced
the **next** crash in the same chain: `PositionGuard.__init__()` (called
unconditionally at `pyq_p4.py` import time, same as before) calls
`self._detect_hedge_mode()`, which assumed `self.binance` was always a
*working* client. With `binance_client` now either `None` or an
unvalidated/geo-blocked `Client` object (round 1's deliberate change),
`futures_get_position_mode()` either raised `AttributeError` or the same
geo-block `APIError` on every one of 5 retries, then raised its own
`ConnectionError` — uncaught, crashing the whole process again, just one
call site further down. Observed live:

```
[PositionGuard] Mode detection attempt 1/5: 'NoneType' object has no attribute 'futures_get_position_mode'. Retrying…
...
ConnectionError: [PositionGuard] Cannot confirm position mode: 'NoneType' object has no attribute 'futures_get_position_mode'
```

**The actual fix** (round 2, this delivery): `_detect_hedge_mode()` now
checks `SIGNAL_ONLY_MODE` directly — not "is `self.binance` `None`" —
before attempting anything. A geo-blocked but non-`None` `Client` object
would fail every real call just as fatally, so "is it `None`" was the
wrong test; "do we even need a correct answer" is the right one. Hedge/
one-way mode only matters for the `positionSide` param on a *real*
bracket order, which is never placed in signal-only mode — so the
function now skips detection entirely and defaults to `False` (one-way)
when `SIGNAL_ONLY_MODE` is on, logging a diagnostic instead of retrying
against a connection that's known not to work. With `SIGNAL_ONLY_MODE`
off, the original retry-then-raise behavior is unchanged.

A full audit of every other `binance_client`/`self.binance` call site in
`p4_utils.py` and `pyq_p4.py` was done alongside this fix (not just a
reactive patch of the one that crashed) — see "Audit of every Binance
call site" below for what else was checked and why it's already safe.

Verified with an isolated unit test (4 cases: `SIGNAL_ONLY_MODE` on with
`binance=None`, on with a geo-blocked client object, off with
`binance=None`, off with a working client) — all behave correctly,
including reproducing the exact live failure (a geo-blocked, non-`None`
client object) and confirming it no longer crashes.

### Round 1 fix (for reference — still in effect, just not sufficient alone)

`_create_binance_client()` in `p4_utils.py` checks the
`SIGNAL_ONLY_MODE` env var directly. If Binance is unreachable after
retries:

- **`SIGNAL_ONLY_MODE=true`**: logs a clear diagnostic (detects
  "restricted location"/"eligibility" in the error text and calls it out
  by name) and returns a `Client` object constructed without the
  validating `.ping()` call, rather than crashing the process. Every
  *other* Binance call site in this codebase (`check_open_position()`,
  `get_equities()`, etc.) already wraps its own calls in try/except with
  a graceful fallback — this fix just stops the crash from happening
  before those call sites ever get a chance to run. If even constructing
  the `Client` object fails, `binance_client` becomes `None` instead,
  and those same call sites' existing error handling takes it from there.
- **`SIGNAL_ONLY_MODE=false`** (real trading intended): unchanged —
  still raises and crashes loudly. Silently continuing without a
  validated exchange connection while believing real order placement is
  active would be dangerous; that failure mode should stay loud.

This does **not** make Binance reachable from GitHub Actions. It makes
the signal-generation/logging/simulated-TP-SL pipeline (which is all
`SIGNAL_ONLY_MODE` does, and which never calls the exchange) survive
Binance being unreachable, instead of the whole process dying because of
a dependency it doesn't actually need in that mode.

## Audit of every other Binance call site (done alongside round 2)

To avoid finding a third crash the same reactive way, every
`binance_client`/`self.binance` reference in `p4_utils.py` and
`pyq_p4.py` was checked against two questions: (1) does it run
unconditionally at import time, before any try/except gets a chance to
catch it, and (2) if it does run, is it actually reached when
`SIGNAL_ONLY_MODE` is on?

- **`PositionGuard`'s other methods** (`check_all_orders_and_positions()`,
  `_check_and_place_bracket()`, `_place_bracket_order()`,
  `_place_bracket_order_fallback()`, `_check_bracket_fills()`,
  `_cancel_sibling_algo_order()`) — all already wrapped in their own
  try/except (catch-and-log, not crash), **and** `check_all_orders_and_positions()`
  returns immediately when `monitored_orders`/`monitored_positions`/
  `_bracket_orders` are all empty — which they always are in signal-only
  mode, since `start_guard_for_order()` (the only thing that populates
  them) is never called (that call site is skipped in favor of
  `simulate_signal_entry()`). Safe as-is.
- **`check_open_position()`, `get_equities()`** (module-level functions) —
  already wrapped in try/except with a graceful zero/default fallback.
  Safe as-is.
- **`signal_executor()`, `start_guard_for_order()`** — never called at
  all when `SIGNAL_ONLY_MODE` is on (the `hourly_task()` branches route
  through `simulate_signal_entry()` instead). Not reached, not a risk.
- **`hourly_task()`'s stale-open-order cancellation** (the
  `futures_get_open_orders()`/`futures_cancel_all_open_orders()` block) —
  was already non-fatal (wrapped in try/except), but ran and logged a
  spurious error every single tick under `SIGNAL_ONLY_MODE` for no
  reason, since no real order is ever placed in that mode. Fixed this
  pass: now gated on `not TEST_MODE and not SIGNAL_ONLY_MODE`.
- **`PositionGuard.__init__`'s `_detect_hedge_mode()` call** — the one
  that was actually crashing the process. Fixed as described above.

Nothing else in either file touches `binance_client`/`self.binance`
unconditionally at import time.

## What this means for real (live/paper) trading

The geo-block is almost certainly not specific to GitHub Actions. If
Binance is blocking GitHub's hosted-runner IP ranges as "ineligible,"
the same compliance system very likely also blocks most of the free-tier
cloud/VPS options previously considered in `VPS_SETUP.md` — Oracle Cloud
Free Tier, GCP/AWS free tier, DigitalOcean, Hetzner, etc. — since they're
all well-known datacenter ASNs, exactly the category this kind of
geofencing targets. This hasn't been empirically tested against each of
those specific providers, but it's the expected outcome given what's
already been observed with GitHub Actions, and it's worth treating as
the default assumption rather than re-discovering it the hard way on a
second platform.

**Recommendation**: run the two concerns on two different hosts.

- **Signal generation + storage + simulated TP/SL tracking**
  (`SIGNAL_ONLY_MODE=true`): keep this on GitHub Actions. It needs no
  working Binance connection at all (with this fix), it's free, and it's
  already set up.
- **Real order placement** (`SIGNAL_ONLY_MODE=false`, paper or live):
  needs to run from a host whose IP Binance doesn't geo-block — in
  practice, that most reliably means a residential/home IP (your own
  machine, or a small always-on box like a Raspberry Pi on your home
  network), not a datacenter VPS. This is a change from the earlier
  VPS-first plan in `VPS_SETUP.md`, driven directly by this new evidence.

The status report in this delivery's accompanying message goes into the
trade-offs of this split in more depth.
