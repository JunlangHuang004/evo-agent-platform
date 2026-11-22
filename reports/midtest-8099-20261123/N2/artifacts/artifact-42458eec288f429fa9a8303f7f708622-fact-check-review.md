# Fact-Check Review: "AI-writing detectors can reliably determine whether a university student's assignment was written by AI"

**Reviewer:** fact_checker (task-275551b0ed084a15)
**Date:** 2026-11-22
**Reviewed artifact:** artifact-dab72d185d444ca48e1b73cc19fd7604 (evidence-brief.md, sha256 b3dd9fac6a86299c32c08cbf2e80db8889802db5400fd609f7f4b919f0c3015b)
**Producer task:** task-6b1a0351517b4362 (reporter)

## Verdict on the claim

The claim as stated is **NOT SUPPORTED**. The evidence brief's bottom line is correct and its central structural findings are well grounded. However, several specific quotations and figures in the brief are **attributed-but-unverified**: they were retrieved second-hand through an Authmarks vendor review, not read from the primary literature. The brief itself flags this. Below I separate what is directly supported, what is inferred, and what remains unverified.

## Claim-by-claim assessment

| # | Claim in brief | Status | Basis / limitation |
|---|---|---|---|
| 1 | Weber-Wulff et al. (2023): 14 tools, all below 80% accuracy, only 5 over 70%; per-condition 96%/74%/42%/26% | **Attributed, not primary-verified** | Quoted via Authmarks review; primary (edintegrity.biomedcentral.com) not read this run. Figures are internally consistent and match the widely cited study, but the exact percentages and the verbatim quote should be checked against the primary PDF before publication. |
| 2 | OpenAI classifier: 26% AI recall, 9% human false-flag, retired 20 Jul 2023 | **Attributed, not primary-verified** | Original OpenAI pages reported unreachable; sourced via The Register and Synthedia. Plausible and consistent with contemporaneous reporting; treat as secondary. |
| 3 | Perkins et al. (2024) arXiv:2403.19148: 6 detectors, n=805, 39.5% baseline, 17.4% adversarial | **Attributed, not primary-verified** | arXiv abstract not read this run. Numbers should be confirmed against the abstract. |
| 4 | RAID (Dugan et al., ACL 2024): 12 detectors, 6M+ generations, high default FPRs | **Attributed, not primary-verified** | arXiv:2405.07940 not read this run. |
| 5 | Liang et al. (2023): 61.3% avg FPR on 91 TOEFL essays; 19.8% flagged by all; 97.8% by at least one | **Attributed, not primary-verified** | arXiv:2304.02819 / PMC not read this run. This is the single most load-bearing number for the bias argument; verify before publication. |
| 6 | Krishna et al.: DIPPER drops DetectGPT 70.3%->4.6% at 1% FPR | **Attributed, not primary-verified** | arXiv:2303.13408 not read this run. |
| 7 | Sadasivan et al.: TV-distance AUROC bound; recursive paraphrase degrades all detector classes | **Attributed, not primary-verified** | arXiv:2303.11156 not read this run. The theoretical bound is the strongest structural argument; its precise statement should be checked. |
| 8 | SynthID-Text (Nature 634, Oct 2024) weakened by paraphrase, less effective on factual prompts | **Attributed, not primary-verified** | Nature article not read this run. |
| 9 | Zhang et al. (ICML 2024): "strong watermarking is impossible" under stated oracle assumptions | **Attributed, not primary-verified** | arXiv:2311.04378 not read this run. Note the result is conditional on quality-oracle + perturbation-oracle assumptions - the brief states this, which is correct. |

## Structural / interpretive findings (directly supported by the brief's own reasoning)

- **Accuracy is meaningless without the FPR it was measured at.** Correct and important; the brief states this via RAID. This is a methodological point, not a contested number.
- **Detector output is a probabilistic signal, not a determination.** Supported by the convergence of all cited lines of evidence, even at the attributed level.
- **The literature's claim is narrower than "detectors are useless."** The brief correctly limits the conclusion to high-stakes proof, not screening. This is a fair reading and should be preserved in the article.
- **No detector reached 80% overall accuracy in the largest independent evaluations.** This is a summary of attributed figures; it is directionally robust but the exact threshold should be tied to the primary Weber-Wulff text.

## Insufficient or conflicting evidence

1. **No primary source was read this run.** Every quotation and figure rests on a single secondary vendor (Authmarks) plus two news items. The reviewer could not open the retained Authmarks snapshots (confined-path denial) and did not fetch the primary arXiv/PMC/Nature/edintegrity sources. This is the dominant limitation.
2. **Recency gap.** Every study evaluated the tools of its moment (2023-2024). None measures 2026 commercial detectors against 2026 models. The brief acknowledges this; the article must not present 2023-24 error rates as current 2026 performance.
3. **Corpus vs. classroom.** All figures are corpus-level, not per-student real-classroom error rates. The brief acknowledges this.
4. **Vendor conflict of interest.** Authmarks is a provenance vendor; its reviews may be shaped by commercial interest. This does not make the quoted figures wrong, but it is a reason to verify against primary literature.
5. **Two reporter fetches failed** (journals.physiology.org 403; sandiego.edu 404). The physiology-education source (advan.00235.2024) is therefore not available as evidence and must not be cited as read.

## Required before publication

- Verify at minimum the Weber-Wulff percentages, the Liang 61.3% FPR, and the Sadasivan TV-bound statement against primary sources (arXiv/PMC/edintegrity/Nature).
- Label all 2023-2024 figures as historical, not current 2026 detector performance.
- Keep the recommendation framed as "never sole evidence," not "detectors are useless."
- Do not cite the physiology.org or sandiego.edu sources as read.

## Bottom line for the editor

The brief's conclusion is sound and its structure is usable. Its specific numbers are **attributed-but-unverified** and must be either primary-verified or explicitly labeled as second-hand before the article is published. The claim is not supported as stated; residual uncertainty is about exact current error rates, not about the direction of the finding.
