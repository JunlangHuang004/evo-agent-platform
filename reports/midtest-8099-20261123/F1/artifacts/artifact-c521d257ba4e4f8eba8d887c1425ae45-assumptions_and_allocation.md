# Confirmed Assumptions and Target Allocation

**Task:** Confirm assumptions and finalize allocation (task-9307ccf5a078485e)
**Date:** 2026-11-22 (currentDateUTC)
**Status:** Assumptions confirmed by client via clarification receipt human-reply-304fadbc5ac74880 (choice: "Use defaults").

## 1. Confirmed Assumptions

The client selected **"Use defaults"**, which confirms the following inputs. These are now treated as confirmed (not merely defaulted):

| # | Assumption | Confirmed value |
|---|-----------|-----------------|
| 1 | Age | 25 |
| 2 | Investable capital | HKD 1,000,000 |
| 3 | Market | US-listed equities / ETFs |
| 4 | Investment horizon | 10+ years (long-term wealth building) |
| 5 | Risk tolerance | Moderately aggressive (tolerates ~-30% to -40% drawdown in a bad year) |
| 6 | Objective | Long-term capital growth (not income, not capital preservation) |
| 7 | Currency | USD-denominated assets acceptable; HKD/USD FX risk modest (pegged band) |
| 8 | Liquidity | No large withdrawal needed within 5 years (not a house down-payment fund) |
| 9 | Access | Can trade US-listed ETFs/stocks via a broker (IBKR/Futu/Tiger); prefers simple low-cost ETF portfolio over stock picking |
| 10 | Constraints | No special exclusions (no ESG screen, no leverage, no crypto, no individual-stock preference) |

**Note on defaults:** Because the client explicitly chose "use defaults", items 4-10 are confirmed rather than assumed. If any of these change, the allocation below must be re-derived.

## 2. Target Allocation

A simple, low-cost, globally diversified, US-equity-tilted ETF portfolio consistent with a 10+ year, moderately aggressive growth mandate.

| Sleeve | Weight | Vehicle (example) | Role / Rationale |
|--------|--------|-------------------|------------------|
| US broad market core | 45% | VTI / ITOT (total US market) | Core growth engine; broadest US exposure, lowest concentration risk |
| US large-cap growth/tech tilt | 15% | QQQ / VUG | Higher expected long-run return; accepts higher volatility |
| Developed ex-US equities | 15% | VEA / IEFA | Diversification away from single-country risk |
| Emerging markets | 10% | VWO / IEMG | Higher growth potential, higher volatility |
| US investment-grade bonds | 10% | BND / AGG | Drawdown cushion and rebalancing ballast |
| Cash / T-bills | 5% | SGOV / BIL | Liquidity buffer and dry powder |
| **Total** | **100%** | | |

**Equity / fixed income split:** 85% / 15% (equity 85%, bonds+cash 15%).

### Rationale
- **Long horizon (10+ yrs)** justifies a high equity weight; time allows recovery from drawdowns.
- **Moderately aggressive tolerance** supports the 85/15 split and the growth/tech tilt, while the 15% bond+cash sleeve keeps the worst-case drawdown within the stated -30% to -40% band.
- **Diversification** across US, developed ex-US and emerging markets reduces single-market concentration risk.
- **Low-cost ETFs** match the client's stated preference and minimize fee drag over a long horizon.

### Trade-offs
- **Higher equity weight = higher expected return but larger drawdowns.** In a severe bear market the portfolio could fall ~30-40%.
- **Growth/tech tilt** raises expected return but increases concentration and volatility versus a pure total-market core.
- **International sleeves** add diversification but introduce currency and geopolitical risk and have historically lagged US returns.
- **Bond sleeve** reduces volatility but caps upside in strong bull markets.
- **No leverage / no crypto** keeps risk bounded but forgoes higher-risk higher-return options.

## 3. Risks (summary; full assessment by risk agent)
- Market risk: broad US equity drawdowns.
- Concentration risk: US mega-cap/tech dominance.
- Currency risk: HKD/USD peg limits but does not eliminate FX risk.
- Inflation risk: long-horizon purchasing power erosion.
- Behavioral risk: selling during drawdowns.
- Liquidity risk: low for these ETFs, but market stress can widen spreads.

## 4. Open Items / Handoff
- Research agent to refresh current US market and candidate ETF evidence (task-f41831b7ef4843d5).
- Risk agent to assess suitability and quantify drawdown (task-eca2f9c5e5af4844).
- Marketing to write plain-language introduction and package (task-d4e3d33536714be0, task-269e41a5094f45b7).
- Fund manager final synthesis (task-bf1baf4de0554fd9).

**Limitations:** This is a simulated proposal, not personalized financial advice. ETF tickers are illustrative examples, not recommendations. No live market data was fetched in this task; research agent will supply current evidence.
