# P5_PLAN.md — V5/P5 signal generation: design, data collection, build plan

Isolated out of `PLAN.md` on 2026-11-07 (that file's own scope is
checklist-level status; this is a real feature design and belongs in its
own file, same convention as `BINANCE_GEOBLOCK.md`/`KAGGLE.md`). Status
at the bottom of every section marks what's actually been checked against
code vs. what's still a design intent. See `PLAN.md`'s Backlog for the
one-line pointer back to this file, and its Done log for P4.1 (done,
2026-11-07) — P5 depends on P4.1's fix.

## 1. Why this exists

Owner's framing (2026-11-07): live signals have all been *accurate* so
far, just too rare. This is not "the model is wrong" — it's "the entry
condition is too strict/too rarely satisfied." P5's job is to find a
different, better-confirmed entry trigger that fires more often without
giving up the accuracy V4 already has — not to replace V4's math, but to
run alongside it, prove itself on real data, and only then be considered
for live trading.

## 2. Core concept

V4 today: pick a direction from a single static comparison (`pred >
price` → up, `price > pred` → down), then place a limit order at the
*opposite* band edge (`low_pred` for up, `high_pred` for down) and wait
for price to reach it. Direction is pre-committed from one snapshot.

P5: **wait for a confirmed touch of a band edge, and let the touch pick
the direction** — touch near `low_pred` → bet on reversion toward
`high_pred`; touch near `high_pred` → bet toward `low_pred` — instead of
asserting direction from a snapshot that can legitimately flip tick-to-
tick within the hour (see Section 9 of `CLAUDE.md`'s 2026-11-06/11-07
entries for why live and backtest can disagree at a given hour even with
identical math: backtest only ever evaluates one pred/price pair per
hour, live re-evaluates the same fixed hourly band against the moving
spot price throughout the hour).

## 3. Hard invariant: every UKF, every cadence, closed bars only

Owner's explicit assumption, confirmed correct and now enforced in code
(P4.1, done 2026-11-07): **no UKF in this project may ever be stepped
with a still-forming bar**, regardless of cadence. Every filter here
assumes one full bar's worth of elapsed time per `update()`/`predict()`
call (the `dt=1` baked into `_make_ukf()`'s `fx` in `p4_utils.py`).
Handing it a bar that only reflects a few minutes of real price action —
labeled as if it were a complete period — understates that bar's true
range (corrupting the high/low filters' sense of band width) and
misestimates the close filter's rate-of-change term, and the error
compounds on every subsequent incremental step, since these filters are
never fully reset except once a day.

**This is now enforced once, centrally**: `p4_utils.py`'s `data_download()`
pins an explicit `end=` bound (`_closed_bar_cutoff()`) to the last fully
closed bar for whichever timeframe is requested (1H/15T/5T/1T today).
Every cadence P5 adds (30T) must go through this same function, not a
bespoke fetch, so this invariant is structural rather than something each
new predictor has to remember to re-implement.

**The granular advantage is NOT "fresher partial data in the filter"** —
it's finer time *resolution* with its own closed-bar history. The thing
that actually moves tick-to-tick is the live spot price used for touch
detection, and that spot price is never fed into any UKF — it's only
ever *compared against* the bands produced by closed-bar-only filters at
each cadence, exactly the way V4 already compares live spot price against
the 1H band. Nothing about P5's edge requires feeding any filter partial
data; the edge comes entirely from watching real-time price against
multiple already-correct bands.

Status: implemented and verified at the mechanism level (P4.1); not yet
confirmed against a live Alpaca feed (no network access from this
sandbox).

## 4. Predictor inventory — what exists, what's new

Checked directly in `pyq_p4.py` before writing this:

| Cadence | UKF objects | Status |
|---|---|---|
| 1H | `btc_ukf`/`btc_high_ukf`/`btc_low_ukf` (+eth) | Live, drives V4's actual signal. |
| 15M | `btc_fift_ukf`/`btc_fift_high_ukf`/`btc_fift_low_ukf` (+eth) | **Already exists and runs every 15 min** (`fifteen_minute_task()`), state persisted/restored across restarts (`_UKF_NAMES`) — but the `ukf_handler()` return value (pred/high/low) is discarded, never captured. Dormant, not broken. |
| 5M | `btc_five_ukf`/`btc_five_high_ukf`/`btc_five_low_ukf` (+eth) | Same situation as 15M — already exists, already stepped every 5 min (`five_minute_task()`), already persisted, output discarded. Dormant. |
| 30M | none | **Does not exist in any version** (checked `pyq_p4.py` and legacy `pyq_p3.py`). Needs building fresh. |

**Decision: keep both 5M and 15M (they're already running, free to wire
up) and build 30M new — three confirmation layers below the 1H band, not
a tradeoff between them.** Reasoning: 15M and 30M sit at horizons that
span a meaningfully different fraction of the hour (useful for "how much
of the hour is left" reasoning in the confluence resolver, Section 6);
5M sits close to the live tick cadence itself, so its marginal
independent-confirmation value is the least certain of the three — but
since it already exists and costs nothing new to wire up, there's no
reason to discard it before the data says it's redundant. Once shadow
data (Section 8) accumulates, check empirically whether 5M's vote ever
changes a confluence outcome that 15M/30M didn't already settle — if not,
it can be dropped from the decision without having cost anything to try.

30M build (new): parallel to `create_ukf_models("15T", ...)`/
`make_ukf_shell()` — add a `"30T"` case to `create_ukf_models()`'s
`tf_map`/aggregation branch (`mins=30`), a `thirty_minute_task()` (or a
30-minute branch inside the existing `fifteen_minute_task()`'s run-guard
bucket), and a `current_30min_bucket()` in `pyquant_state.py` paralleling
`current_15min_bucket()`. All of it goes through the now-closed-bar-safe
`data_download()`.

Status: inventory confirmed by reading code; 30M build not started.

## 5. Touch tolerance — bootstrap, then calibrate (not circular)

"We'd need the data to set the tolerance, but we need the tolerance to
collect the data" is solved in two stages, which is exactly the owner's
own framing ("build it first so it can start collecting"):

- **Stage 0 (provisional, needed just to start collecting):** seed the
  touch tolerance with a simple heuristic — a fraction of ATR, or a
  fraction of the band width (`high_pred - low_pred`) itself. Doesn't
  need to be good, just reasonable enough to not fire constantly or
  never, so the shadow pipeline (Section 8) has something to run.
- **Stage 1 (calibration, once real data exists):** for every hour,
  record how close price actually got to each edge (minimum distance
  reached, using Stage-0's tolerance as the initial detection trigger),
  whether it then reached the opposite edge before the hour ended, and
  the volatility reading (ATR or the existing `vol`/`pct_diff` fields)
  at that moment. Bucket by volatility regime. That table — "in regime
  X, price gets within Y% of the edge Z% of the time, and reverses W%
  of the time it does" — *becomes* the real tolerance function, keyed by
  regime rather than a constant, fed back into the detector before any
  paper/live use.

This directly answers "variable based on market conditions" — tolerance
(and entry timing, Section 7) are lookups keyed by volatility regime, not
fixed constants, once Stage 1 has enough samples.

Status: design only, no code. Needs Section 8's data to run Stage 1.

## 6. Confluence resolver — what to do on a late touch or a punch-through

On a clean, on-time 1H-band touch: proceed with the P5 entry as
designed. On a **late touch** (little of the hour left) or a
**punch-through** (price breaches the touched edge rather than
reversing), check the 15M/30M (and optionally 5M) models' own current
pred/high/low — their recent-window view of what's actually been
happening — and resolve to one of **four** actions (owner follow-up,
2026-11-07, refining the original three-way place/adjust/abandon split):

1. **Place the order anyway** — finer timeframe(s) still agree with the
   reversal hypothesis; the 1H touch was just slow to arrive.
2. **Adjust TP/SL** — finer timeframe(s) agree on direction but disagree
   on magnitude; move TP to the finer model's own predicted edge, and/or
   tighten SL given the band has already been partially violated.
3. **Flip and follow** — finer timeframe(s) actively agree the price will
   *continue* through the touched edge rather than revert. Rather than
   just abandoning the reversal bet, open a **new** trade in the
   continuation direction, sized/targeted to the extent of whichever
   finer-timeframe prediction(s) agree (e.g. target the 15M/30M model's
   own opposite band edge for that continuation move, with TP/SL derived
   from it the same way V4 derives them today — reusing
   `pyquant_signal_math.py`'s existing SL/TP calculation, just anchored
   to the finer timeframe's band instead of the 1H one). This turns what
   would otherwise be a pure loss/non-event into a second, independently
   evaluated opportunity, following the move that's actually happening
   instead of fighting it.
4. **Abandon, no replacement** — the finer timeframes disagree *with each
   other* (no clear reversion signal, no clear continuation signal
   either) — genuinely ambiguous, so do nothing rather than guess.

Status: design only, no code. The "flip and follow" sizing mechanics
(exactly which finer-timeframe band becomes the new trade's target, how
its SL is derived, whether it reuses `generate_signal()`'s existing
entry/TP/SL math wholesale or needs its own variant) still needs to be
worked out in the minute-bar backtest (Section 9) before it's specified
precisely enough to code — flagging this as the one piece of Section 6
genuinely not design-complete yet.

## 7. Entry timing — backtested, regime-conditional

Two variants to run head-to-head on the minute-bar backtest harness
(Section 9): fire right at the touch (better entry price, lower
confidence) vs. wait N ticks for confirmed reversal (worse entry price,
higher confidence). Rather than picking one globally, this is a
volatility-regime-keyed parameter, same as Section 5's tolerance — e.g.
wait-for-confirmation may win in choppy/high-vol regimes (filters out
more false touches), fire-at-touch may win in clean/low-vol regimes
(less slippage, cleaner reversals) — but this is for the backtest to
settle, not to assume. It slots in as another dimension on the existing
Optuna/grid-search optimizer rather than a hardcoded constant.

Status: design only, no code. Needs Section 9's harness to run the
comparison.

## 8. Market data persistence — start accumulating now, use later

Not for immediate use in P5's own calibration (there's no retroactive
backfill — this only accumulates forward from whenever it's turned on),
but worth starting as soon as possible since every day not stored is
history permanently lost to Alpaca's own lookback limit. The eventual
goal: let a P5-specific backtest run on **real 1-minute historical data**
(not synthesized/approximated) to mimic live conditions as closely as
practical — sub-minute dynamics are still at play live and this won't
capture those, but minute-level fidelity meaningfully narrows the gap
between "what the backtest assumed happened intra-hour" and "what
actually happened," limiting surprises when a P5 variant graduates from
backtest to shadow to paper.

**Design (not yet implemented — explicitly out of scope for this round,
per owner instruction; this is the "ready to start" spec):**

- New table, `pyquant_market_bars` (both the Postgres and SQLite init
  blocks in `pyquant_state.py`, same dual-backend pattern as every other
  table there):
  - `symbol` (text, e.g. `"BTC/USD"`), `timeframe` (text, e.g. `"1T"`),
    `ts` (timestamp, the bar's own open/close boundary — pick one
    convention and document it inline), `open`/`high`/`low`/`close`
    (float), `volume` (float, nullable — Alpaca crypto bars may or may
    not always populate this), `inserted_at` (timestamp, server-side
    default now).
  - Unique constraint/index on `(symbol, timeframe, ts)` so writes are
    idempotent (`ON CONFLICT DO NOTHING` / `INSERT OR IGNORE` — a bar
    already stored for a given symbol/timeframe/ts is never
    overwritten, since historical bars don't change once closed).
  - A `record_market_bars()` / `get_market_bars()` pair following the
    same column-list-driven pattern already used for
    `_DIAG_HISTORY_COLS`/`_BACKTEST_DIAG_COLS`.
- **Write path: piggyback on calls already happening, zero extra API
  load.** `one_minute_task()`/`five_minute_task()`/`fifteen_minute_task()`/
  `hourly_task()` already call `data_download()` every tick at 1T/5T/15T/
  1H — after P4.1, every row returned is guaranteed closed. Persist
  every *newly* fully-closed bar from each of those calls (not just the
  single `.iloc[-1]` row currently used — the DataFrame already contains
  a short recent window, so write whichever of those rows aren't already
  in `pyquant_market_bars`) as a best-effort, try/except call, same
  reliability posture as every other state-store write from the engine.
  1T is the highest-value cadence to persist (it's the finest grain
  already being fetched, and 5T/15T/1H are all derivable from it via
  `aggregate_ohlcv_data()` later if ever needed) — 5T/15T/1H can be
  persisted too since they're nearly free once the table/write-path
  exist, but 1T is the one that actually extends what's recoverable
  beyond Alpaca's own lookback window.

Status: fully specified, not implemented. First concrete thing to build
once this plan is approved to move from brainstorm to build.

## 9. P5 diagnostic / calibration data — what to collect for Section 5/7

Separate from raw market bars (Section 8): this is the record of P5's
*own* touch/confluence evaluations, needed to run Section 5's tolerance
calibration and Section 7's entry-timing comparison, and later to watch
P5 run in shadow mode (Section 10) without it ever touching a real order.

**Design (not yet implemented):**

- New table, `pyquant_v5_diagnostics` (not an extension of the existing
  `pyquant_diagnostics_history`/`pyquant_backtest_diagnostics` — those
  are V4's gate-diagnostic schema and don't have a natural home for
  touch-specific fields; keeping P5's diagnostics in their own table
  avoids bolting mismatched columns onto V4's validated schema). Fields:
  - `symbol`, `ts` (the touch-evaluation timestamp), `source`
    (`"v5_touch"`, for consistency with the existing `source` convention
    if this ever needs to sit next to V4 diagnostics in a shared view).
  - `band_price`, `band_pred`, `band_high_pred`, `band_low_pred` — the
    1H band in effect at touch time.
  - `touched_edge` (`"high"`/`"low"`/null), `touch_distance` (how close
    price actually got, signed), `touch_tolerance_used` (whatever
    Stage-0/Stage-1 value was active at the time — so later recalibration
    can tell which historical rows used which tolerance), `minutes_into_hour`
    (the time-remaining context Section 6 needs), `reversed_before_hour_end`
    (bool, filled in after the fact once the hour completes — this is the
    label Section 5's calibration actually needs).
  - `fifteen_pred`/`fifteen_high_pred`/`fifteen_low_pred`,
    `thirty_pred`/`thirty_high_pred`/`thirty_low_pred` (and `five_*` if
    kept per Section 4) — the finer-timeframe readings consulted during
    confluence resolution.
  - `confluence_action` (`"place"`/`"adjust"`/`"flip_follow"`/`"abandon"`,
    Section 6), `entry_timing_variant` (`"at_touch"`/`"wait_confirm"`,
    Section 7), `volatility_bucket` (whatever regime key Section 5/7 end
    up using).
  - `would_have_fired` (bool) and, once the minute-bar backtest can
    simulate forward, `simulated_outcome` fields (hit TP/SL/neither) —
    this is what actually lets shadow-mode data be graded without ever
    placing a real order.
- **Write path**: a new `record_v5_diagnostic()`/`list_v5_diagnostics()`
  pair in `pyquant_state.py`, called from the new `pyquant_v5.py` module
  (Section 10) exactly where the existing shadow-comparison code already
  calls `_persist_signal_diagnostic()` — same tick, same zero-extra-fetch
  principle (reuses the 1H price/pred/band already in hand that tick), a
  separate try/except so a P5 diagnostic failure can never affect V4's
  real diagnostic write next to it.

Status: fully specified, not implemented. This is the second concrete
thing to build (after or alongside Section 8), since it's what starts
producing the data Sections 5–7 need.

## 10. Build plan — a true side-by-side extension, not a replacement

Reuses the existing 2026-11-03 shadow-comparison architecture
(`shadow_comparison_enabled`, the `source` column, per-source diagnostic
filtering) rather than inventing a new one:

- New, isolated module `pyquant_v5.py` — kept off `walkforward.py`/
  `p4_utils.py`'s validated V4 math entirely (same discipline as the
  standing "shared math changes happen in one place" rule). Houses the
  touch detector + tolerance lookup (Section 5), the confluence resolver
  (Section 6), and a `diagnose_v5_touch()` entry point that returns a
  diagnostic dict and **never** a tradeable signal — same hard guarantee
  the existing shadow code already has ("can never affect `my_signal`,
  an order, or a Binance call").
- New `v5_shadow_enabled` kv flag (default off), same Settings-page
  on/off pattern as `shadow_comparison_enabled`.
- Compare/Live Monitor pages' existing per-source diagnostic filtering
  just needs `"v5_touch"` added as a selectable value — no new UI
  plumbing beyond that, once Section 9's table/API exist.
- Called from `hourly_task()` (for the 1H touch check) and
  `fifteen_minute_task()`/`thirty_minute_task()` (for the sub-hour
  confirmation reads), each wrapped in its own try/except.

Status: design only. Depends on Section 4's 30M predictor and Section
8/9's tables existing first.

## 11. Progression gate (same discipline as V4)

No-capital minute-bar backtest harness (tolerance calibration + entry-
timing sweep, Sections 5/7) → shadow mode on the live system (Sections
8–10, zero trading impact, real samples accumulating) → paper/testnet →
live. Nothing places a real order until shadow data says P5 is actually
better than V4, not assumed to be.

## 12. Readiness checklist

**Done:**
- P4.1 (closed-bar-only data for every cadence) — implemented
  2026-11-07, see `PLAN.md`'s Done log. This is a hard prerequisite for
  everything else here (Section 3).
- Architecture, data schemas (market bars + P5 diagnostics), confluence
  logic (4-way), touch-tolerance bootstrap/calibrate plan, entry-timing
  plan, and build/progression plan are all written up above.

**Ready to start, not yet built (next concrete steps, in dependency
order):**
1. `pyquant_market_bars` table + write-path (Section 8) — no dependency
   on anything else; the sooner this starts, the more history
   accumulates before it's needed.
2. 30-minute predictor (Section 4) — needed before the confluence
   resolver has all three finer-timeframe votes available.
3. `pyquant_v5_diagnostics` table + write-path + wiring the (already-
   running) 15M/5M predictors' discarded output into it (Section 9) —
   needed before any shadow-mode data exists to calibrate against.
4. Minute-bar backtest harness (Sections 5/7/9) — needed before the
   touch-tolerance table or the entry-timing comparison can be computed
   from real history rather than the Stage-0 heuristic.
5. `pyquant_v5.py` module + shadow wiring (Section 10) — needed before
   P5 can run (read-only) on the live system.

**Genuinely open, not yet resolved:**
- Section 6's "flip and follow" exact sizing/TP-SL mechanics — needs the
  minute-bar backtest to pin down precisely, flagged in Section 6.
- Whether 5M's confluence vote ever adds anything 15M/30M don't already
  decide — an empirical question for once shadow data exists (Section
  4), not a blocker to starting.

Nothing above blocks starting on step 1 or step 2 immediately.
