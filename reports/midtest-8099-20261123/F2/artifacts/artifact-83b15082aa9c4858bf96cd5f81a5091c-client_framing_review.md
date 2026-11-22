# Client-Facing Framing Review — Semiconductor Deck (task-a2e6693f514641b6)

**Reviewer:** Marketing specialist (client-facing framing)
**Review of:** task-3c4b9221c3aa4859 (Build HTML slide deck), revision 0
**Artifacts reviewed (read this run, SHA256 pinned):**
- index.html — d6bd02955b8c89ea561ddfaa7b0fd00d732f081122c2f6a886886368dabcbe0d
- slides.md — 6d86f9acc1a428ba1f7656d702d25a11e2e25c61bd732c855ac970d68bc98c5b
- README.md — fcfde4bb2d1df334d741e855f94a5e323600c87509e8d244188bb649e22d5530
- Upstream evidence brief (research) — c85a189e96ac6ba2ac945c9f9e03a643eca3216231839b8d9c3efa1e8e69d808
**As-of:** 2026-11-22. **Scope:** clarity and audience fit for the fund team; concrete messaging fixes. This is an editorial/framing review, not code execution or investment validation.

---

## Verdict: PASS WITH MINOR MESSAGING FIXES

The deck is clear, well-structured and audience-appropriate for an internal fund investment team. It meets the stated acceptance criteria (clarity and audience fit; concrete messaging fixes or confirmation of readiness). The fixes below are optional polish, not blockers. No rework of the producer is required for framing reasons.

---

## 1. Audience fit — assessment

- **Audience is correctly identified and consistent.** Title slide, footer and README all state "Internal fund team briefing / not investment advice." The register is analytical, not retail-salesy — appropriate for an investment team.
- **Structure matches how an investment team consumes a briefing:** summary → drivers → supply/geopolitics → valuation → implications → risks → scenarios → gaps → sources. This is a sound decision-first order.
- **The core question is stated plainly** ("can earnings catch up with the expectations already in prices?") and repeated in the summary — good for a team that needs the thesis in one line.
- **Honesty is a strength, not a weakness, for this audience.** The explicit evidence-gaps slide and the "conflict surfaced, not blended" note build internal credibility. Keep them.

## 2. Clarity issues found (with concrete fixes)

1. **Jargon without a gloss on first use.** "HBM", "SOX", "DUV", "CXMT", "CPO/LPO", "heterogeneous integration" appear without expansion. For a mixed fund team (PMs, analysts, client-facing staff) add a one-line gloss on first use, e.g. "SOX (Philadelphia Semiconductor Index)", "HBM (high-bandwidth memory)". *Fix: add parenthetical expansions on the first occurrence of each acronym.*
2. **Numbers without units/context in a few places.** "92 GW" and "$120.6bn" are fine, but "+104.1% YoY" on the summary slide could read as a typo to a skimming reader. *Fix: add a short qualifier, e.g. "+104.1% YoY (vs May 2025)".*
3. **"Own the bottleneck" is punchy but ambiguous** for a team that may include non-specialists. *Fix: keep the phrase but add a half-line: "i.e., favor segments where supply scarcity persists (HBM/memory, advanced packaging, power, optics)."*
4. **Scenario slide label is good but could be stronger.** It already says "illustrative, not forecasts" — keep, and consider adding "do not assign probabilities" so no one treats them as a house view.
5. **Source-conflict slide is dense.** It is the most important slide for credibility but reads as a list. *Fix: lead with one bold sentence — "Fundamentals (Deloitte) and equity pricing (Leverage Shares) diverged in 2026; we report both rather than blend them."*

## 3. Messaging risks for a client-facing audience

- **Do not let this deck leave the internal team unchanged.** It is explicitly "not investment advice" and contains secondary-source figures. If any slide is reused externally, the disclaimer and the evidence-gaps slide must travel with it. *Recommendation: keep the disclaimer on the title and final slides (already done) and add a one-line "internal use only" to the footer (already present).*
- **Single-publisher market data.** The 29% drawdown and Broadcom guidance come from one secondary source (Leverage Shares, an ETP issuer). The deck already flags this. *Recommendation: keep the caveat; do not upgrade these to "verified" in any client version.*
- **Recency gap is disclosed, which is correct.** Latest primary SIA data is May 2026 and latest market data is 24 Aug 2026, while the deck is dated 22 Nov 2026. This is honestly stated. *Recommendation: on the title slide, keep the as-of date but ensure the gaps slide is not skipped in live presentation.*

## 4. Confirmation of readiness

- Entrypoint and usage are clear: `index.html` opens in a browser; arrow/space/Home/End and Prev/Next buttons navigate; README documents files, coverage, sources and limitations.
- Content covers the required lenses (outlook, investment implications, key risks) plus scenarios and gaps.
- **Framing readiness: READY for internal fund-team presentation.** The five fixes above are optional clarity improvements; none changes the analytical claims or requires re-verification of sources.

## 5. Limits of this review

- Editorial/framing only. I did not execute the HTML/JS or verify rendering; that is the risk/technical reviewer's scope.
- I did not re-fetch or independently verify any source figure; I relied on the read artifacts and the research brief.
- No producer status was changed by this review.

---

*Internal marketing/framing review. Not investment advice.*
