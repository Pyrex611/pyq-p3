# INTENT.md — Why this project exists

This file answers "why are we building this, and for whom" — the one
thing that doesn't change when the architecture does. If a decision in
`SPEC.md` or `PLAN.md` ever seems to contradict something here, this
file wins; flag the conflict and resolve it explicitly rather than
quietly drifting away from the stated intent.

## 1. What problem this solves

The owner (Sesugh) researches and runs a BTC/ETH crypto futures trading
strategy (UKF-based signal generation, Bayesian-optimized parameters,
bracket-order risk management) entirely by hand: SSH into a box, tail
logs, eyeball whether a signal fired, manually re-run a backtest when a
parameter needs checking. That works, but it doesn't scale past one
person watching one terminal, it has no history you can browse later,
and there's no way to see what the system is doing from a phone or
another machine.

**The goal of this project is to turn that into a system Sesugh can
operate without SSHing in** — a UI to watch it work, a database that
remembers what it did, and a hosting setup that doesn't require a
server to be babysat. The trading *strategy itself* (the UKF signal
model, the SL/TP math, the optimizer) is not what this project is
improving — it's already built and is treated as a given input. This
project wraps that strategy in the operational infrastructure it was
missing.

## 2. Who this is for

One person: the owner, who is both the trader and the sole developer.
This is explicitly **not** a multi-tenant product, not something other
people will sign up for, and not something that needs to scale beyond
one operator's BTC/ETH futures positions. Any design decision that
trades simplicity for multi-user readiness is the wrong trade for this
project unless and until that changes.

## 3. What "done" looks like

Not a single finish line — the project is explicitly phased (see
`PLAN.md`) — but the destination each phase is walking toward:

- **The strategy runs unattended**, generating signals (and, once
  validated, placing real orders) on a schedule, without the owner
  needing to keep a terminal open or a laptop awake.
- **The owner can see what it's doing from anywhere** — a dashboard, a
  live monitor, a signal history, a way to kick off a backtest — without
  SSH.
- **A single, trustworthy off-switch** exists that the owner can hit
  from that UI if something looks wrong, without that UI ever being
  able to cause harm on its own (it holds no exchange credentials).
- **Nothing silently fails.** A failure anywhere in the pipeline is
  either survived (logged, degraded gracefully) or surfaced loudly
  (crashes, alerts) — never swallowed in a way that leaves the owner
  believing something is working when it isn't. (This is the exact
  category of bug the Binance geo-block incident — see
  `BINANCE_GEOBLOCK.md` — kept surfacing: a crash killing silent
  monitoring is itself a failure of this goal, even though nothing was
  losing money.)
- **A strategy change is provably safe before it risks capital** — the
  walkforward backtest is the one source of truth a change must pass
  before it's trusted in paper or live mode, and the two can never be
  allowed to drift apart (this is why `pyquant_signal_math.py` exists).

## 4. Non-goals (explicitly out of scope unless this file is revised)

- **Not a multi-asset or multi-strategy platform.** BTC/ETH futures,
  one strategy family. Generalizing to other assets/strategies is a
  possible future direction, not a current requirement, and should not
  be designed in preemptively at the cost of complexity now.
- **Not a public product.** No signup flow, no billing, no other users.
  The repo being public (for GitHub Actions' free cron tier) is a
  hosting detail, not a change in audience.
- **Not trying to out-perform the strategy's own backtested numbers.**
  Performance/alpha-model work is a separate, ongoing concern tracked
  in the Claude Docs plan doc referenced from `CLAUDE.md` Section 6 —
  this project's job is the operational wrapper, not the edge.
- **Not chasing 100% uptime at any cost.** A missed signal tick is
  recoverable (the next tick catches up); the project should fail safe
  and cheap, not add complexity chasing five-nines for a single-user
  system.

## 5. Risk posture (read this before touching anything trading-related)

Real capital is involved once `PYQUANT_MODE=live`. The standing rule
from `CLAUDE.md` Section 7 — a strategy change proves itself in
`walkforward.py` before being ported to the live engine — exists because
of this, not as process for its own sake. When in doubt between "ship
the convenient version" and "ship the version that fails loud and safe,"
this project always chooses the latter for anything that touches order
placement, and the former is acceptable for anything that's purely
observational (dashboards, logs, backtest UI).

## 6. Keep this file current

Revise this file only when the actual intent changes (a new asset class
is in scope, the single-owner assumption changes, risk tolerance
shifts) — not every time a feature ships. Routine progress belongs in
`PLAN.md`; this file should rarely need touching.
