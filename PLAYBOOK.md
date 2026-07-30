# Options Selection Playbook

Built from a statistical review of all 21 completed round trips (2026-07-02 →
2026-07-29): **net −$81 on $1,180 deployed.** Every rule below is derived from
that data, not from general trading advice.

---

## 1. What the record actually says

### Hold time is the dominant variable

| Hold time | n | Win rate | P&L |
|---|---|---|---|
| Under 30 min | 6 | 17% | **−$50** |
| 30–120 min | 5 | 20% | **−$45** |
| 2–8 hours | 4 | **0%** | **−$53** |
| Overnight or longer | 6 | **50%** | **+$67** |

**Fifteen intraday trades lost $148. Six overnight trades made $67.** Every
intraday bucket is negative; the 2–8 hour bucket has never produced a single
winner.

### But this is not "intraday doesn't work"

The 2026-07-29 SPY 731P settles the question. It was a **day trade**, bought at
$0.06 and sold at $0.03 for −$6. Held to the 2:45 PM sellout — still a day
trade — it was worth **~$1.54, about +$296.**

So the finding is not that the intraday timeframe loses. It is that **exits were
premature**, and intraday trades are where premature exits happen. A day trade
held to the sellout is a different animal from a day trade cut at the 2/3 mark.

### Days to expiry

| DTE at entry | n | Win rate | P&L |
|---|---|---|---|
| 0–2 | 9 | 22% | −$32 |
| 3–9 | 3 | **0%** | −$28 |
| 10–30 | 6 | 33% | −$50 |
| 30+ | 3 | 33% | **+$29** |

3–9 DTE is the worst band: too little time for a thesis, not enough gamma to pay
for the theta. **Avoid it.** For day trades use 0–2 DTE (gamma); for swings use
30+ (time).

### Contract price

| Entry price | n | Win rate | P&L | Avg |
|---|---|---|---|---|
| Under $0.30 | 10 | 20% | −$15 | −$1.5 |
| $0.30–0.99 | 8 | 25% | −$11 | −$1.4 |
| **$1.00+** | 3 | 33% | **−$55** | **−$18.3** |

Win rate is flat across price bands, but **dollar damage concentrates in
expensive contracts** — KO (−$56), CLF (−$28), CCL (−$24) were all $0.83–1.38.
At this account size a $1.00+ contract is 100% of a position with no ability to
scale out.

### A rule the data does NOT support — corrected

I previously claimed entries should wait 20 minutes past the open. The record
disagrees:

| Entry timing | n | Win rate | P&L |
|---|---|---|---|
| First 20 min of session | 12 | 25% | **+$5** |
| After first 20 min | 9 | 22% | **−$86** |

First-20-minute entries are roughly break-even; later entries lost $86. The
*spread* argument remains mechanically true (XLE quoted 43% wide at the open,
4.4% twenty minutes later), so **check the spread, don't watch the clock.** The
gate is "spread ≤ 10% of mark," not "wait until 8:50."

---

## 2. The real gap: no quantitative option selection

Research to date has been **narrative** — news, macro, sector rotation, catalysts.
That layer has actually been good: on 7/29 the calls on the Korean liquidation,
the hawkish dissents and the bond/equity divergence were all correct, and the
day still lost money.

The missing layer is **quantitative contract selection.** Across 21 trades, not
once was any of the following computed:

| Never measured | Why it matters | Tool |
|---|---|---|
| **ATR reachability** | Is the strike even reachable in the holding period? | `get_equity_technical_indicators` type=atr |
| **IV vs HV** | Am I buying cheap or expensive premium? | scanner `IMPLIED_VOLATILITY` / `HISTORICAL_VOLATILITY` |
| **Breakeven vs implied move** | Does the contract need more than the market prices? | `break_even_price` vs computed implied move |
| **VWAP position** | The reference level for every intraday trade | `type=vwap` |
| **Support / resistance** | Where the move stalls | scanner `FILTER_TYPE_SUPPORT`/`RESISTANCE` |
| **Relative options volume** | Flow / unusual activity | scanner `RELATIVE_OPTIONS_VOLUME` |

`get_equity_technical_indicators` was **never called once** in the entire
session. That is the single largest unforced omission.

---

## 3. The reachability test (the most important new gate)

**SPY ATR(14) = 7.57 points.** That is the empirical daily range. Compute:

```
reachability = |strike − spot| ÷ ATR(14)
```

| Ratio | Meaning | Action |
|---|---|---|
| < 0.5× | Very likely to reach | High probability, modest payoff |
| **0.5–1.0×** | Reachable in one normal session | **Day-trade sweet spot** |
| 1.0–1.5× | Needs a strong trend day | Only with a hard catalyst — and hold to the sellout |
| > 1.5× | Lottery | Only if IV is cheap; size for zero |

Applied retroactively to 7/29:

- **SPY 731P**: spot 742.03, strike 731 → 11.03 pts → **1.46× ATR.** Needed a
  near-1.5-sigma day. It got one (SPY fell 13.5 pts = 1.78× ATR) — so the
  contract was fine, and the ratio was telling me *this needs a violent move,
  therefore hold it to the bell*. Instead I cut it at the 2/3 mark.
- **SPY 721P**: spot 737.65, strike 721 → 16.65 pts → **2.2× ATR** with 1 day
  left. That is a lottery ticket priced as a directional trade, and it should
  have been sized and held as one.

The ratio does not only screen entries — **it tells you the required holding
behaviour.** Above 1.0× ATR, cutting early is guaranteed to lose, because the
whole payoff lives in the tail of the distribution.

---

## 4. Pre-trade checklist (all gates must pass)

**Liquidity — hard gates, no exceptions**
- [ ] Spread ≤ 10% of mark
- [ ] Open interest ≥ 500
- [ ] Volume ≥ 100 today
- [ ] Never pay above mark on a wide market; on a penny market paying the offer is fine

**Quantitative fit — the new layer**
- [ ] ATR(14) computed; reachability ratio in the intended band
- [ ] RSI checked for exhaustion against the trade direction
- [ ] VWAP: calls above and holding, puts below and holding
- [ ] IV not obviously inflated post-event (do not buy premium after a gap —
      the KO lesson: entered at 20% IV *after* a 6% earnings pop, paid ~$0.25
      above fair value)
- [ ] DTE is 0–2 (day trade) or 30+ (swing) — **never 3–9**

**Structure**
- [ ] Position sized assuming total loss
- [ ] Exit rule written down *before* entry: target, sellout time, and the one
      condition that invalidates
- [ ] No stop-loss on a long option whose thesis is intact
- [ ] Day trades: **hold to 2:30–2:45 PM CST unless the target hits.** Not 2:00.

---

## 5. The size constraint, stated plainly

Every position in the record was 1–2 contracts because that is the minimum
tradeable unit at this account level. Consequences:

- **Cannot scale out.** No taking half off at +50% and letting the rest run —
  which is the natural way to trade convexity.
- **Cannot diversify.** One contract is the whole position; conviction cannot be
  expressed through size.
- **Cannot fine-tune strikes.** Budget dictates strike, not analysis. On 7/28
  the best contract (SPY 710P, 35k OI, penny spread) cost $25.04 against $24.37
  of buying power — the trade was chosen by $0.67, not by merit.

Below roughly $500, the hold-to-expiry discipline matters **more**, not less,
because partial exits are unavailable. Above ~$1,000, scaling out becomes
possible and the exit rules should be revisited.

---

## 6. Daily routine

**Pre-open**
1. Overnight: Asia, futures, oil, yields, any escalation headlines
2. Catalyst calendar with **exact times in CST**, including intraday events
   (auctions, Fed speakers) — not just the 7:30 data drop
3. Compute ATR(14) and RSI on candidate underlyings *before* looking at chains
4. Scanner run: gainers/losers filtered for liquidity, plus relative options
   volume for flow

**Entry**
5. Confirm direction on the tape (VWAP, higher/lower closes on 5-min bars)
6. Check spread — enter when it is tight, regardless of clock time
7. Run the full checklist; write the exit rule down first

**Management**
8. Hold to plan. An intraday counter-move is not invalidation — require a
   structural break (a higher/lower *close*, not a 17-minute bounce)
9. Day trades: flat by 2:45 PM CST for T+1 settled cash

**Post-trade**
10. Log entry/exit with the reachability ratio, IV, spread, and hold time so
    these tables keep growing and the rules stay evidence-based
