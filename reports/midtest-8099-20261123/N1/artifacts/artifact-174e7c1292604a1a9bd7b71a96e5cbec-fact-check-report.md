# Fact-Check Report — HKUST AI Common Core Draft (task-729b6230609449e0)

**Reviewer:** fact_checker (task-1c8290e521ad4281)
**Date:** 2026-11-22 (currentDateUTC)
**Run:** run-0005bd701419416b
**Reviewed artifacts (current revision):**
- artifact-3765d97fe0ca4d5683d2189e8924896d (web-article.md, sha256 da886525…)
- artifact-8ec7b43faba14e5c9b610f1b0d05e376 (newsletter.md, sha256 d46d4396…)
- artifact-eecfb5e31f42468db3759a4b5b34eaac (social.md, sha256 cb7c96df…)

**Evidence base actually read by this reviewer:**
- evidence-brief.md (artifact-5b62c878cb0f447d88f704a3d1a441f2) — read in full.
- story-angle-brief.md (artifact-dc69545bc180492191890eaa66a5324f) — read in full.
- The three draft channel files above — read in full.
- **NOT read:** the primary HKUST source bodies. The reporter's fetch snapshots (body.txt) are NOT accessible to this reviewer: `imports/web-source-call-99ed4fbdcffe4563/body.txt` returned `permanent: Cannot access a confined workspace path`. Only URL + SHA256 metadata is available to me. Per the evidence rule, a producer's fetch metadata is not source text and is not verification.

## Verdict: REWORK (bounded)

The drafts are internally consistent and faithfully reproduce the reporter's evidence brief, and the brief's claims are all attributed to a single official HKUST announcement. However, **no claim in the drafts has been verified against a primary source body by this reviewer**, because the source snapshots are not readable in this task. The drafts are therefore *unverified*, not *verified*. Two specific issues require correction before publication.

## Claim-by-claim map (material claims)

| # | Claim (as drafted) | Source cited | Status |
|---|---|---|---|
| C1 | HKUST will require all incoming undergraduates to complete a six-credit AI Common Core Program from 2026/27 | S1 (HKUST news, 2026-07-14) | **Unverified** (primary body not read; consistent across brief + all 3 drafts) |
| C2 | Program has two parts: "Foundations of AI Literacy: Human–AI Orchestration" and "AI Horizon" courses by schools/departments | S1 | **Unverified** |
| C3 | Framing concepts "Human–AI Co-Creation" and "Data Literacy" | S1 | **Unverified** |
| C4 | Quote from Prof. Jimmy Fung, Associate Provost (Teaching and Learning) | S1 | **Unverified** — direct quotation; must be checked verbatim against source before publication |
| C5 | "Major + X" framework introduced 2021/22; "more than 1,450 students have enrolled in AI-related courses under this initiative to date" | S1 | **Unverified** — numerical claim; denominator/scope ("to date", which courses) not independently confirmed |
| C6 | BSc in Integrative Systems and Design renamed BSc in Innovation, Design and Technology in 2026/27 | S1 | **Unverified** (context; not carried into drafts) |
| C7 | Article byline "By the HKUST Newsroom" | — | **Inaccurate attribution** — see Issue 1 |
| C8 | "Published 22 November 2026" as the article's publication date | — | **Misleading** — see Issue 2 |

## Issues requiring rework

### Issue 1 — Byline misattributes authorship to HKUST (web-article.md)
The web article is bylined "*By the HKUST Newsroom | Published 22 November 2026*". This is an independent newsroom report *about* HKUST, not an HKUST publication. Attributing the byline to "the HKUST Newsroom" falsely implies the university wrote it. **Fix:** replace with the actual newsroom byline (or "Staff report"), and keep HKUST as the subject/source, not the author.

### Issue 2 — Publication date conflates report date with event date (web-article.md, newsletter.md, social.md)
The drafts present "22 November 2026" as the publication date while the underlying event (the announcement) occurred 14 July 2026. The article body correctly says "announced the new requirement on 14 July 2026", but the header date and the newsletter/social framing can read as if the news is from 22 November. **Fix:** clearly label the report as published 22 November 2026 *about* a 14 July 2026 announcement; ensure the newsletter and social posts do not imply the requirement was announced in November. (The social posts and newsletter do state 14 July 2026, so this is a labeling/clarity fix, not a factual error.)

### Issue 3 — Unverified direct quotation (C4)
The Prof. Jimmy Fung quote is reproduced verbatim in the web article. A direct quotation must be checked against the source body before publication. Since the source body is not readable in this task, either (a) the editor re-verifies the quote against the retained snapshot, or (b) the quote is paraphrased and attributed. Do not publish a verbatim quote that no one in the pipeline has read in the source.

### Issue 4 — Unverified numerical claim (C5)
"more than 1,450 students have enrolled in AI-related courses under this initiative to date" is a specific figure with an undefined denominator (which courses, which period, whether it counts unique students or enrollments). It is attributed to the university, which is acceptable, but the drafts should attribute it explicitly ("according to the university") and not present it as independently confirmed. The web article does say "According to the university", which is good; the newsletter/social do not repeat the figure, so no change needed there.

## What is NOT a defect
- Internal consistency across the three channels: verified — the same core facts (6 credits, 2026/27, two-part structure, 14 July 2026 announcement) appear consistently.
- The drafts correctly flag open questions (department allocation, credit split, assessment, current-student impact, integrity/privacy) as unconfirmed. This is good practice and should be retained.
- The drafts correctly note that all sources are official HKUST pages with no independent/critical source. Retain this disclosure.

## Required bounded rework
1. Fix the byline (Issue 1).
2. Clarify publication vs. event date labeling (Issue 2).
3. Re-verify or paraphrase the direct quote (Issue 3).
4. Keep the explicit attribution on the 1,450 figure (Issue 4) — already present in the web article.

## Limitation of this review
This is an editorial fact-check based on the reporter's evidence brief and the drafts. It is **not** verification against primary source bodies, because the HKUST source snapshots were not readable in this task. If the editor can read the retained snapshot (sha256 e353fdb5…), the quote and the 1,450 figure should be confirmed there before publication.
