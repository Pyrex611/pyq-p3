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

## The fix (this delivery)

`_create_binance_client()` in `p4_utils.py` now checks the
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
