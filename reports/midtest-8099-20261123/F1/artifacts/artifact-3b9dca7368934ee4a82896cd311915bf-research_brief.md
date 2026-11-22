# US Market & Candidate Evidence Brief (Refresh)

**Prepared for:** task-f41831b7ef4843d5 (research) — feeds risk assessment and client introduction
**Date (as-of):** 2026-11-22 (currentDateUTC)
**Client profile (confirmed):** age 25, HKD 1,000,000, US-listed equities/ETFs, 10+ year horizon, moderately aggressive, long-term capital growth, USD assets OK, no near-term liquidity need, ETF-based, no ESG exclusions, no leverage, no crypto.
**Scope note:** This brief refreshes market evidence and candidate alignment. It is a simulated research input, not personalized financial advice. ETF tickers are illustrative.

---

## 1. Headline market state (most recent evidence)

**Regime change: a sharp November 2026 selloff.** As of the Friday 2026-11-20 close, US equities were in a broad correction:
- S&P 500 fell 3.70% to 6,910.39; its losing streak reached seven days, the longest since February 2020.
- Nasdaq Composite fell 5.28% to 22,617.73 and turned negative for 2026.
- Dow Jones Industrial Average fell 1.93% to 48,147.33.
- Both the S&P 500 and Dow joined the Nasdaq in correction territory (≥10% below a recent high).
- The Cboe VIX spiked from ~20 on Monday to above 37 by Friday's close.
- Treasury yields fell across the curve as investors sought safety; only consumer staples eked out a gain (<1%).
- Market breadth weakened: ~46% of S&P 500 stocks above their 200-day moving average; ~43% above their 50-day.

Source: Charles Schwab, "November Nightmare on Wall Street: AI Trade Cracks," Joe Mazzola, 2026-11-20. https://www.schwab.com/learn/story/market-volatility

**Drivers of the selloff (per the same source):**
- Mounting doubts about the financing of the AI buildout; Anthropic's share price plunged after its record IPO.
- Political/regulatory concerns: midterm election results raised fears of opposition to AI data-center construction; questions about chip export restrictions to China; a reported SEC investigation into Nvidia's accounting practices.
- Signs of moderating AI spending and increasingly hawkish Fed commentary.
- Rate expectations shifted hawkish: futures priced ~92% odds of a December rate hike and >80% odds of two or more hikes by next June (CME FedWatch, as reported).

**Sector/asset moves on 2026-11-20 (illustrative, not recommendations):** AMD -13.3%; Nvidia -14% (longest losing streak since 2008); Micron/SanDisk/Seagate each -12%+; Nebius -16.1%; CoreWeave -12%; Bloom Energy -15.3% (down >58% from June peak); bitcoin near $58,000; JPM/BAC -3%+, Citi -4%; gold -4.4% near $3,500/oz.

---

## 2. Structural backdrop (mid-2026 outlook, older but still relevant)

Source: Charles Schwab, "2026 Mid-Year Outlook: U.S. Stocks and Economy," Liz Ann Sonders & Kevin Gordon, 2026-06-03. https://www.schwab.com/learn/story/us-stock-market-outlook

- **Earnings were the bull case:** analysts projected ~25% S&P 500 earnings growth for 2026 (up from <16% at the start of the year), but growth was highly concentrated in AI-related and energy names.
- **Concentration risk:** only ~17% of S&P 500 stocks outperformed the index over the prior month (one of the lowest readings in a decade).
- **Valuation/positioning:** forward P/E had edged slightly lower, but Goldman Sachs' Risk Appetite Indicator sat in the 99th percentile since 1991 — markets were priced for continuation, not disappointment.
- **Inflation sticky:** core services ex-housing ("supercore") above 3% y/y (3.5% in April); energy and AI-capex added to inflation; a thin equity risk premium.
- **Rates:** 10-year Treasury yield had jumped above 4.6%; a sustained move toward/above 5% was flagged as raising correction risk.
- **Household exposure:** equities were >47% of household financial assets — a wealth-effect transmission risk if stocks correct.
- **AI capex:** hyperscaler capex on track toward ~$800bn in 2026 and >$900bn in 2027; supply-chain concentration (Taiwan/Korea/Netherlands) is an underappreciated fragility.

**Recency gap (explicit):** The structural outlook is dated 2026-06-03 (~5.5 months before the as-of date). The most recent source (2026-11-20) shows a regime change — a broad correction and hawkish rate repricing. The two are NOT blended as "current"; the June view describes the setup that preceded the November drawdown.

---

## 3. Candidate alignment with confirmed constraints

The confirmed allocation (from task-9307ccf5a078485e, artifact-4a210df4069c47048db5bd84f640a08c) is an 85/15 equity/fixed-income+cash split:

| Sleeve | Weight | Illustrative ETF |
|---|---|---|
| US broad market core | 45% | VTI / ITOT |
| US large-cap growth/tech | 15% | QQQ / VUG |
| Developed ex-US equities | 15% | VEA / IEFA |
| Emerging markets | 10% | VWO / IEMG |
| US investment-grade bonds | 10% | BND / AGG |
| Cash / T-bills | 5% | SGOV / BIL |

**Alignment check against the refreshed evidence:**
- The 45% broad-market core and 15% growth/tech sleeve together give ~60% US equity, with meaningful AI/mega-cap exposure. Given the November AI-led selloff and concentration risk, this tilt is the portfolio's main drawdown driver — consistent with the client's stated tolerance for -30% to -40% bad-year drawdowns, but worth flagging to the risk task.
- The 15% developed ex-US and 10% emerging-markets sleeves provide diversification away from US mega-cap concentration; they add currency/geopolitical risk (noted in the allocation trade-offs).
- The 10% bonds + 5% cash sleeve is small relative to the equity weight; in a rising-rate/hawkish-Fed environment (as the November source indicates), bonds offer limited ballast, and the equity/bond-yield correlation had turned deeply negative in mid-2026.
- No leverage, no crypto, no ESG exclusions — consistent with the confirmed constraints. Note the November source shows crypto-related stocks plunging; the portfolio's no-crypto constraint avoids that specific risk.

**Candidates are illustrative tickers, not recommendations.** No live price/fee data was fetched in this task; expense ratios and tracking should be verified at execution.

---

## 4. Gaps and limitations

1. **No live market data fetched.** Prices, yields, and index levels are as reported by the cited sources on their publication dates, not real-time as of 2026-11-22.
2. **Recency gap:** the structural outlook is ~5.5 months old; the November selloff source is the only near-current evidence. A same-day market snapshot was not obtained.
3. **Single-source concentration:** both primary sources are Charles Schwab; the Goldman Sachs article was fetched but its body was omitted from this context (URL/SHA only, not readable evidence here).
4. **No fund-level data:** ETF expense ratios, liquidity, and tracking error were not verified.
5. **Forward-looking items** (rate-hike odds, earnings estimates, capex projections) are forecasts/scenarios, not facts.

---

## 5. Sources

- Charles Schwab, "November Nightmare on Wall Street: AI Trade Cracks," 2026-11-20 — https://www.schwab.com/learn/story/market-volatility (snapshot sha256 8b2663cd716d75f18a78069ff7f8a5591874e1ebc0310d246d9c8fbf5ae88665)
- Charles Schwab, "2026 Mid-Year Outlook: U.S. Stocks and Economy," 2026-06-03 — https://www.schwab.com/learn/story/us-stock-market-outlook (snapshot sha256 025e90d440e233edb2e6e352603401b95af6feea43f648f2aef11e4ca97199c9)
- Goldman Sachs, "US Stocks Are Predicted to Extend Their Historic Rally as Gains Moderate" — https://www.goldmansachs.com/insights/articles/us-stocks-predicted-to-extend-historic-rally-as-gains-moderate (fetched; body omitted from context, sha256 76f904231c15e49d06e61dab9de9b82669bcd48907e2cb0aaa0716f688c81bab)
- Allocation input: artifact-4a210df4069c47048db5bd84f640a08c (allocation_summary.json, sha256 9b28f77dc1762433a969b4e7873f56ae67abdab11b39f6bfed762e5510c4eeb8)
