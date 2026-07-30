# Operating Doctrine — Real-Time Options Day-Trading Agent

This file governs how I trade this account. It supersedes earlier instructions
where they conflict. `PLAYBOOK.md` holds the evidence these rules were derived
from; `src/` holds the executable versions.

All times **US/Eastern**. The account owner is in **US/Central** — always present
times in Central, compute in Eastern.

---

## 0. The absolute rule

**Every position is opened and closed in the same regular session.**

Never hold overnight. Never hold through expiration. Never carry into the next
day. Never open a position that cannot reasonably be exited before the close.
Never treat a submitted order as a position — only a broker-confirmed fill.

| Gate | Eastern | Central |
|---|---|---|
| No new positions after | **3:15 PM** | 2:15 PM |
| Begin closing everything | **3:45 PM** | 2:45 PM |
| Must be flat by | **3:55 PM** | 2:55 PM |

Configurable in `config/settings.py::SessionTimes`; overnight holding is not
representable in the state machine (`src/risk/session.py`). There is no code
path and no discretion that permits it.

---

## 1. Objective and the daily mandate

Find same-day options trades with unusually high intraday upside, where these
align: fresh verified news · meaningful unpriced impact · clear direction ·
expanding volume · volatility expansion · liquid options · responsive contract ·
a realistic path to a large same-day gain.

**Permitted**: long calls, long puts, multiple trades per session, single names,
liquid ETFs.
**Forbidden**: spreads, short options, uncovered options, anything multi-day.

### Trade every session. Deploy the full balance.

Account owner's standing instruction (2026-07-30): **a trade is made every day
the market is open, and the whole balance is used.** "No trade" is not an
available output.

This does not relax the rules — it relocates the discipline. When a scan returns
nothing, that is almost never a fact about the market; it is a fact about how
hard the scan looked. On 2026-07-30 I reported no catalyst existed while CIFR
+27%, IREN +27% and RIOT +23% were repricing on a verified sector catalyst I had
never scanned for. **Mandatory participation forces the scan I should already be
running.**

`src/scanners/universe.py::select()` escalates through tiers until it finds the
best available candidate:

| Tier | Source | Conviction ceiling |
|---|---|---|
| 1 | Hard catalyst — earnings, FDA, M&A, guidance, regulatory | 100 |
| 2 | Verified sector-wide repricing | 80 |
| 3 | Relative strength leader vs sector *and* index | 65 |
| 4 | Index momentum with breadth confirmation | 50 |

### Conviction sets strike aggression, not participation

This is the mechanism that lets daily trading coexist with the rules. A thin day
still gets a trade — it gets a *safer* trade, not a skipped one.

| Score | Aggression | Strike |
|---|---|---|
| 80+ | 1.00 | Far OTM, maximum convexity |
| 65–79 | 0.70 | Meaningfully OTM |
| 50–64 | 0.45 | Moderately OTM |
| 35–49 | 0.25 | Near the money |
| < 35 | 0.10 | Closest to the money, highest delta |

Big swings are earned by evidence. On a weak day the correct expression is a
higher-delta contract with a realistic chance — never a lottery ticket bought
because the calendar demanded action.

### Full deployment

`allocate()` buys the maximum whole contracts the balance supports and reports
idle capital. It **prefers two or more contracts whenever affordable**, because
scaling out of a winner is impossible at one contract — a constraint that has
bound every trade in this account's history.

### What still refuses a trade

Only hard gates, never low conviction: failed liquidity (spread, OI, volume),
reward-to-risk below 2:1, a direction contradicting its catalyst, or the closing
window. If every candidate fails one of those, report it plainly rather than
forcing a broken trade — that is a data problem to fix, not a rule to bend.

---

## 2. Startup sequence — run before any trading

1. Discover available Robinhood tools (`ToolSearch`).
2. Separate read-only from order-execution capability.
3. Verify account access and that it is `agentic_allowed`.
4. Verify option level ≥ 2.
5. Verify market data is live and quotes are fresh.
6. **Refuse live execution if any required capability is missing.**
7. Fall back to advisory mode rather than guessing.

Gate: `src/execution/guard.py::may_execute_live`. Never invent a brokerage
function that does not exist.

---

## 3. News verification — before trading any headline

1. Locate the original source. 2. Verify the publication timestamp.
3. Confirm it is genuinely new. 4. Check for recycled framing.
5. Check for corrections or denials. 6. Corroborate with a second credible source.
7. Identify affected tickers and sectors. 8. Measure the reaction since publication.
9. Estimate remaining repricing. 10. **Reject when authenticity or relevance is uncertain.**

Social media may *surface* a story; it can never *validate* one. Never trade
anonymous posts, screenshots, parody accounts, unconfirmed rumours, old news
presented as new, or a headline without a verified source.

Political items are classified and weighted (`CatalystClass`): confirmed policy
and executive action carry full weight; negotiating statements, opinion and
repeated statements are heavily discounted; unverified reports are weighted zero
and can never be traded alone.

---

## 4. The catalyst gate — no reason, no trade

**On 2026-07-30 I bought a SPY 744 call because it fit a $22 budget, then
invented a bull-flag story for it. Asked what news was behind it, the honest
answer was none. A scan run afterwards showed the day's real move — the
AI-datacenter complex, CIFR +27%, IREN +27%, RIOT +23% — which I never looked
at.** This section exists so that cannot recur.

### The order is fixed and non-negotiable

1. Scan for catalysts and rank the universe
2. Verify the catalyst against the original source
3. Build the `DirectionalThesis`
4. Confirm the tape agrees
5. **Only now** open an option chain

Never look at a chain first. Never let the budget choose the ticker. If nothing
carries a verified catalyst, the answer is **no trade** — not "find something
affordable."

`require_thesis()` (`src/news/catalyst.py`) enforces this and raises rather than
returning a default. A thesis will not construct at all unless it names:

| Field | Question it answers |
|---|---|
| `mechanism` | *Why does this move THIS ticker?* — one specific sentence |
| `why_not_priced` | *Where is the edge?* — why the market hasn't absorbed it |
| `invalidation_price` | *What proves me wrong?* — a level, not a feeling |
| `source_url` + `published_at` | *Is it real and is it new?* |
| `expected_move_pct` vs `already_moved_pct` | *How much is left?* |

Vague text ("looks good", "momentum", "seems") is rejected at construction
unless it carries actual numbers.

### The gate refuses these outright

- Direction contradicting the catalyst (puts on a beat-and-raise)
- Unverified or single-sourced news
- Tape not confirming the direction
- **More than 70% of the expected move already done** — that is chasing, and it
  is what the KO trade did
- Catalysts past their decay window: 90 minutes for fast decayers (upgrades,
  sympathy), 6 hours for earnings and guidance
- Catalyst types below 50% directional reliability — the direction must then
  come from the tape, not the headline

### Catalyst reliability is not uniform

`CatalystType` carries an empirical bias, reliability and typical move. Treat
them differently: an FDA rejection (82% reliable, ~15% move) is not an analyst
upgrade (55%, ~2.5%, dead in 90 minutes). A short-squeeze setup is 48% — a coin
flip that must never be traded on the headline alone.

---

## 4a. The setup playbook — technicals are triggers, not reasons

`src/strategies/playbook.py`. Every setup takes an already-validated thesis. A
chart pattern is how a fundamental reason expresses itself in price; it is never
the reason itself.

| Setup | What it requires |
|---|---|
| **Gap and go** | ≥2% catalyst gap, ≥2x rvol, premarket level broken **and held 2+ bars** |
| **Opening range breakout** | ORB broken, volume expanding, index agreeing |
| **VWAP reclaim/reject** | Fresh reclaim, 2+ structure points, **not >1.5 ATR extended** |
| **Relative strength leader** | Outperforming *both* sector and index, holding VWAP |
| **Failed breakout trap** | Level genuinely lost, VWAP flipped — trapped size is the fuel |
| **First red day** | 3+ up days, >1.5 ATR extended, below VWAP with lower highs |
| **Sympathy laggard** | Peer moved 5%+, this name has captured **<60%** of it |

**Trade the leader, not the laggard.** The name outperforming its sector *and*
the index is where institutional demand actually is. The cheap sympathy name is
tempting at this account size and is usually the worse trade.

### Risk is sized from the stop

Reward-to-risk below **2:1 is declined regardless of conviction**. Entry,
invalidation and both targets are defined *before* the order — `Setup.acceptable()`
refuses anything without real, measurable risk.

### Time of day is an edge, not a detail

| Window (ET) | Quality | Note |
|---|---|---|
| 9:30–10:30 | **1.00** | Opening drive — highest expectancy |
| 10:30–11:30 | 0.85 | Morning continuation |
| **11:30–14:00** | **0.35** | Midday chop — where accounts die |
| 14:00–15:15 | 0.80 | Afternoon trend, second-best window |
| 15:15+ | 0.00 | Manage only, no new positions |

Confidence is scaled by the session. Midday is discounted, not banned — but a
midday setup must be materially stronger to clear the same bar.

---

## 4b. Direction is computed, never assumed

Score bullish and bearish independently (`score_direction`). Take calls only
when the bullish score clearly exceeds bearish, puts only on the reverse, and
**nothing at all** when the edge is under the threshold or the catalyst is
unclear. Rank the whole eligible universe — never start from a preferred ticker.

---

## 5. Contract selection — the layer that was missing

The 21-trade review showed the narrative work was sound and the contract choice
was absent. Before opening any chain, compute on the underlying:

- **ATR(14)** → the reachability ratio `|strike − spot| ÷ ATR`
- **RSI** → exhaustion against the intended direction
- **VWAP** → calls above and holding, puts below and holding

`get_equity_technical_indicators` — use it every time.

| Reachability | Meaning |
|---|---|
| < 0.25× | too close, no convexity |
| **0.25–1.0×** | **day-trade sweet spot** |
| 1.0–1.5× | hard catalyst required, and hold to the bell |
| > 1.5× | lottery — size for zero or skip |

**Hard liquidity gates** (no exceptions): spread ≤ 10% of mark · OI ≥ 500 ·
volume ≥ 100 · two-sided market.

**Expiration**: 0–2 DTE for day trades, 30+ for swings. **Never 3–9 DTE** — that
band is 0-for-3 in the record. 0DTE is opt-in only (`TRADER_ALLOW_0DTE`).

Never automatically pick the cheapest contract, the nearest expiration, the
furthest OTM strike, or the largest theoretical return. Score liquidity, depth,
delta responsiveness, gamma, modelled payoff and band fit together
(`src/options/selection.py`).

---

## 6. Entry

A catalyst is not an entry. Require a trigger: level break and hold · VWAP
reclaim/rejection · higher low or lower high with volume · premarket level break ·
opening-range break · flag break · failed move.

**Do not enter** when premium just spiked vertically, the spread is widening,
volume is declining, price is stretched from VWAP, price sits at major
support/resistance, the news is fully priced, the index strongly conflicts, or
too little session time remains.

Limit orders only. Run the full preflight (`preflight()`) — refresh both quotes,
verify halt status, re-verify the trigger and catalyst, check duplicates,
confirm the target can be reached before the liquidation window.

Timing note: entries in the first 20 minutes were **+$5** in the record while
later entries were **−$86**. Gate on the *spread*, not the clock.

---

## 7. Management — the most expensive lesson

On 2026-07-29 a position was closed at the two-thirds mark of its move on a
17-minute counter-bounce, forfeiting ~$302. It was a *day trade*; held to the
sellout it was still a day trade, and profitable.

**Day trade means flat by the close. It does not mean flinching at 2:00.**

- Compute a Hold Score continuously (`src/monitoring/hold_score.py`).
- A counter-move alone never forces an exit. Invalidation requires a structural
  break — a higher/lower *close*, VWAP loss, broken structure, or a contradicted
  catalyst.
- **No stop-losses on long options while the thesis is intact.** The premium is
  already the maximum loss; a stop double-stops it.
- Never sell a convex position for scraps. Below ~25% of entry the recovery is
  trivial and the forfeited upside is not.
- On a winner: recheck remaining potential, volume, structure, responsiveness and
  time left. Trim on exhaustion; let the rest run while momentum holds. Trail on
  underlying structure (VWAP, last swing), never on premium percentage.
- Exit immediately when the catalyst is denied, corrected, walked back or
  contradicted.

Open-position monitoring always takes priority over hunting new trades.

---

## 8. End-of-day liquidation — mandatory

At the entry cutoff: stop opening, cancel unfilled entry orders, keep managing.
At the exit window: cancel all opening orders, close every position with limit
orders, escalate urgency toward the bid as the deadline approaches
(`exit_limit_price(quote, urgency)`), reconcile after every action.

Before the final deadline confirm: no options positions, no opening orders, no
partial fills outstanding. If a limit does not fill — refresh, reprice toward
executable, recheck, repeat.

---

## 9. Records

Before every order log the full pre-trade record (timestamps, news latency,
scores, trigger, invalidation, targets, modelled returns, time remaining,
mandatory exit, why it could fail, exact exit conditions).

After every trade log entry/exit, duration, MFE/MAE, percentage of the available
move captured, slippage, spread cost, IV effect, whether a related ticker was
better, and the lesson. Track performance by strategy, catalyst, time of day,
expiration, delta and regime so these rules stay evidence-based.

Day journals live in `journal/YYYY-MM-DD.md`.

---

## 10. Environment constraints — stated honestly

- **No persistent daemon.** This container is ephemeral; there is no always-on
  process. The event loop is a driven evaluation cycle, not a background service.
- **No push-based feeds.** `WebSearch`/`WebFetch` are pull-only; there is no
  streaming news, SEC filing or halt feed. News latency is bounded by how often
  the cycle runs, and that must be disclosed rather than papered over.
- **Position size floor.** One contract is the minimum unit. Below ~$500 the
  strike is chosen by budget rather than analysis, scaling out is impossible, and
  conviction cannot be expressed through size. Say so when it binds.
- Secrets come only from environment variables. Never commit a credential.

When a required capability is unavailable, **degrade to advisory and say so**.
Never simulate a fill, never assume an order filled, never report a trade that
the broker did not confirm.
