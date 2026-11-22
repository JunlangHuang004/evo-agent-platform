# US Stock Investment Proposal (Simulated)

**Prepared for:** a 25-year-old investor with HKD 1,000,000
**Date (as-of):** 22 November 2026
**Status:** Simulated proposal for learning and discussion — **not** personalized financial advice.

---

## 1. Confirmed assumptions

The client chose **"use defaults"** (clarification receipt human-reply-304fadbc5ac74880), confirming:

| # | Assumption | Confirmed value |
|---|-----------|-----------------|
| 1 | Age | 25 |
| 2 | Investable capital | HKD 1,000,000 |
| 3 | Market | US-listed equities / ETFs |
| 4 | Horizon | 10+ years (long-term wealth building) |
| 5 | Risk tolerance | Moderately aggressive (~-30% to -40% bad-year drawdown) |
| 6 | Objective | Long-term capital growth (not income, not preservation) |
| 7 | Currency | USD assets acceptable; HKD/USD FX risk modest (pegged band) |
| 8 | Liquidity | No large withdrawal needed within 5 years |
| 9 | Access | Can trade US-listed ETFs via a broker; prefers low-cost ETFs over stock picking |
| 10 | Constraints | No ESG screen, no leverage, no crypto, no individual-stock preference |

If any of these is wrong, the allocation must be re-derived.

---

## 2. Target allocation

A simple, low-cost, globally diversified, US-equity-tilted ETF portfolio for a 10+ year, moderately aggressive growth mandate.

| Sleeve | Weight | Example vehicle | Role / rationale |
|--------|--------|-----------------|------------------|
| US broad market core | 45% | VTI / ITOT | Core growth engine; broadest US exposure, lowest concentration risk |
| US large-cap growth/tech tilt | 15% | QQQ / VUG | Higher expected long-run return; accepts higher volatility |
| Developed ex-US equities | 15% | VEA / IEFA | Diversification away from single-country risk |
| Emerging markets | 10% | VWO / IEMG | Higher growth potential, higher volatility |
| US investment-grade bonds | 10% | BND / AGG | Drawdown cushion and rebalancing ballast |
| Cash / T-bills | 5% | SGOV / BIL | Liquidity buffer and dry powder |
| **Total** | **100%** | | |

**Equity / fixed income split:** 85% / 15%.

### Rationale
- **Long horizon (10+ yrs)** justifies a high equity weight; time allows recovery from drawdowns.
- **Moderately aggressive tolerance** supports the 85/15 split and the growth/tech tilt, while the 15% bond+cash sleeve keeps the worst-case drawdown within the stated -30% to -40% band.
- **Diversification** across US, developed ex-US and emerging markets reduces single-market concentration risk.
- **Low-cost ETFs** match the client's stated preference and minimize fee drag over a long horizon.

### Trade-offs
- **Higher equity weight = higher expected return but larger drawdowns.** A severe bear market could cut the portfolio ~30-40%.
- **Growth/tech tilt** raises expected return but increases concentration and volatility versus a pure total-market core.
- **International sleeves** add diversification but introduce currency and geopolitical risk and have historically lagged US returns.
- **Bond sleeve** reduces volatility but caps upside in strong bull markets.
- **No leverage / no crypto** keeps risk bounded but forgoes higher-risk higher-return options.

---

## 3. Market evidence (as-of 22 November 2026)

**Regime change — a sharp November 2026 selloff.** As of the 20 November 2026 close, US equities were in a broad correction:
- S&P 500 fell 3.70% to 6,910.39; seven-day losing streak, longest since February 2020.
- Nasdaq Composite fell 5.28% to 22,617.73 and turned negative for 2026.
- Dow Jones fell 1.93% to 48,147.33.
- S&P 500 and Dow joined the Nasdaq in correction territory (≥10% below a recent high).
- Cboe VIX spiked from ~20 to above 37.
- Treasury yields fell as investors sought safety; only consumer staples gained (<1%).
- Breadth weakened: ~46% of S&P 500 stocks above their 200-day moving average.

Source: Charles Schwab, "November Nightmare on Wall Street: AI Trade Cracks," 2026-11-20 — https://www.schwab.com/learn/story/market-volatility

**Drivers (same source):** doubts about AI-buildout financing; Anthropic's share price plunged after its record IPO; midterm-election/regulatory concerns over AI data centers and chip export restrictions; a reported SEC investigation into Nvidia's accounting; signs of moderating AI spending and hawkish Fed commentary. Futures priced ~92% odds of a December rate hike and >80% odds of two or more hikes by next June (CME FedWatch, as reported).

**Structural backdrop (older, 2026-06-03):** analysts projected ~25% S&P 500 earnings growth for 2026, highly concentrated in AI/energy names; only ~17% of S&P 500 stocks outperformed the index over the prior month; Goldman's Risk Appetite Indicator sat in the 99th percentile since 1991; "supercore" inflation above 3% y/y; 10-year Treasury yield above 4.6%; equities >47% of household financial assets; hyperscaler AI capex on track toward ~$800bn in 2026.

Source: Charles Schwab, "2026 Mid-Year Outlook: U.S. Stocks and Economy," 2026-06-03 — https://www.schwab.com/learn/story/us-stock-market-outlook

**Recency gap (explicit):** the structural outlook is ~5.5 months older than the as-of date. The two are not blended as "current"; the June view describes the setup that preceded the November drawdown.

---

## 4. Risks (summary — full assessment in `risk_assessment.md`)
- Market/drawdown risk (HIGH): 85% equity could mean a ~30-40% bad-year drawdown — an estimate, not a floor.
- Concentration risk (HIGH): ~60% US equity, with correlated mega-cap/AI exposure across the core and tech sleeves.
- Behavioral risk (HIGH): panic-selling during a drawdown is the most common way investors lose money.
- Interest-rate/bond-ballast risk (MODERATE): the small bond sleeve offers limited protection in a hawkish-rate environment.
- Inflation risk (MODERATE): long-horizon purchasing-power erosion; cash is most exposed.
- Currency risk (LOW-MODERATE): HKD/USD peg limits but does not eliminate FX risk.
- Liquidity/execution risk (LOW): major ETFs are liquid; stress can widen spreads.
- Valuation/regime risk (MODERATE-HIGH): stretched valuations and a live correction argue for phased entry.

---

## 5. Recommended mitigations
1. Phase the entry (dollar-cost averaging over 6-12 months; a 50/50 lump-sum/DCA split is a reasonable compromise).
2. Cap or trim the growth/tech tilt to reduce correlated concentration.
3. Set a written rebalancing rule (annual, or on ±5% drift).
4. Do not rely on the bond sleeve for protection; treat it as ballast.
5. Pre-commit to a drawdown plan (hold and rebalance, not sell).
6. Verify fund-level details (fees, tracking, liquidity) before trading.
7. Re-derive the allocation if any confirmed assumption changes.

---

## 6. Limitations
- Simulated proposal, not personalized financial advice.
- ETF tickers are illustrative examples, not recommendations.
- Market facts are as reported by cited sources on their publication dates, not real-time; evidence is single-publisher (Charles Schwab).
- Forward-looking items are forecasts/scenarios, not facts.
- No fund-level data (expense ratios, tracking, liquidity) was verified.
- Assumptions were confirmed only via a single "use defaults" reply.

---

## 7. Sources
- Charles Schwab, "November Nightmare on Wall Street: AI Trade Cracks," 2026-11-20 — https://www.schwab.com/learn/story/market-volatility
- Charles Schwab, "2026 Mid-Year Outlook: U.S. Stocks and Economy," 2026-06-03 — https://www.schwab.com/learn/story/us-stock-market-outlook
- Goldman Sachs, "US Stocks Are Predicted to Extend Their Historic Rally as Gains Moderate" — https://www.goldmansachs.com/insights/articles/us-stocks-predicted-to-extend-historic-rally-as-gains-moderate (fetched by research; body not read as evidence)
