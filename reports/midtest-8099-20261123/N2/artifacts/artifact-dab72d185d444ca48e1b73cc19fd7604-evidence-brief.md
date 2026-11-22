# Evidence Brief: Can AI-writing detectors reliably determine whether a university student's assignment was written by AI?

**Prepared by:** Reporter (task-6b1a0351517b4362)
**Date:** 2026-11-22
**Scope (per chief_editor, task-244b9ee9ea594abc):** General reliability of current commercial/academic AI-text detectors on typical university assignments (essays, short-answer, code); audience = students and faculty; angle = the claim is NOT supported as stated. 'Reliably' read as a strict standard (near-zero error, defensible in a misconduct hearing). Include evasion/arms-race and general policy; include a concrete recommendation.

**Bottom line:** The claim is **not supported**. Across the largest independent evaluations, no detector reached 80% overall accuracy; false positives never reached zero; accuracy collapses under paraphrase, translation, or light editing; and a theoretical result bounds any detector's ceiling. Detector output is a probabilistic signal, not a reliable determination.

---

## 1. Headline accuracy findings

- **Weber-Wulff et al. (2023), Int. J. for Educational Integrity 19:26** — tested 14 tools (12 public + Turnitin, PlagiarismCheck) across 6 conditions. Verbatim: *"Detection tools for AI-generated text do fail, they are neither accurate nor reliable (all scored below 80% of accuracy and only 5 over 70%)."* Per-condition: human-written text 96%; human text machine-translated −20%; unmodified AI text 74%; AI text with human editing 42%; AI text machine-paraphrased 26%. Conclusion: *"the systems we tested should not be used in academic settings."* Source: https://edintegrity.biomedcentral.com/articles/10.1007/s40979-023-00146-z (as of 2026-11-01, via Authmarks review).

- **OpenAI AI Text Classifier (2023)** — vendor's own launch figures: correctly identified 26% of AI-written text; flagged human-written text 9% of the time. Retired 20 July 2023 *"due to its low rate of accuracy."* (Cited via The Register 26 Jul 2023 and Synthedia 24 Jul 2023; original OpenAI pages no longer reachable.)

- **Perkins et al. (2024), arXiv:2403.19148** — 6 detectors, n=805, higher-ed setting. Baseline accuracy 39.5%; under simple adversarial manipulation 17.4%. Verbatim: *"the potential for false accusations demonstrate that these tools cannot currently be recommended for determining whether violations of academic integrity have occurred."*

- **RAID (Dugan et al., ACL 2024, arXiv:2405.07940)** — 12 detectors, 6M+ generations, 11 models, 8 domains, 11 attacks. Finding: *"Default False Positive Rates (FPRs) of open-source detectors are dangerously high."* Conclusion: *"Detectors are not yet robust enough for widespread deployment or high-stakes use… the problem of false positives remains unsolved."* Central methodological point: an accuracy number is meaningless without the FPR it was measured at.

## 2. False positives and bias

- **Liang et al. (2023), Patterns 4(7):100779 (arXiv:2304.02819)** — 7 detectors on 91 human TOEFL essays (non-native writers) vs 88 US 8th-grade essays. Average false-positive rate on TOEFL essays **61.3%**; 19.8% flagged by *all* detectors; 97.8% flagged by at least one; near-perfect accuracy on the US essays. AI-rewriting the flagged essays ("elevate… literary language") dropped detection to near zero. Authors: *"caution against their use in evaluative or educational settings."*
- **Weber-Wulff et al.** — machine-translated human text lost 20 points of accuracy; 6 of 14 tools produced false positives; for GPT Zero, *"half of the positive classifications would be false accusations."*
- **OpenAI** — withheld its watermark partly because it *"could stigmatize use of AI as a useful writing tool for non-native English speakers"* and because at scale it *"would lead to a large number of total false positives."*

## 3. Evasion / arms race

- **Krishna et al. (arXiv:2303.13408)** — DIPPER paraphraser dropped DetectGPT detection from 70.3% to 4.6% at a fixed 1% FPR, *"without appreciably modifying the input semantics."* Proposed retrieval defense needs a provider-side generation database that does not exist publicly.
- **Sadasivan et al. (TMLR, arXiv:2303.11156)** — recursive paraphrasing degrades watermarks, neural classifiers, zero-shot and retrieval detectors; spoofing can make human text read as AI. Theoretical bound: best-possible detector AUROC is capped by the total-variation distance between human and AI text distributions, which shrinks as models improve.
- **Watermarking** — Kirchenbauer et al. (ICML 2023) green-token scheme; SynthID-Text deployed in Gemini (Nature 634, Oct 2024) but *"weakened by edits… such as through LLM paraphrasing"* and *"less effective on responses to factual prompts."* OpenAI's watermark *"trivial to circumvention"* (character-insertion trick). Zhang et al. (ICML 2024): *"strong watermarking is impossible"* under quality-oracle + perturbation-oracle assumptions.

## 4. What the evidence does NOT establish

- Precise error rates for current (2026) commercial detectors against current models — every study evaluated the tools of its moment; the structural findings (threshold sensitivity, paraphrase fragility, TV-distance bound) are the durable part, not any specific percentage.
- Per-student error rates in real classrooms — these are corpus studies.
- That detectors are useless as one screening signal — the literature's claim is narrower: error rates are incompatible with using a score as proof in a high-stakes accusation.

## 5. Recommendation (per brief)

Detector output should **never be used as sole evidence** in an academic-integrity determination. It may serve at most as one weak screening signal, always corroborated by process-based evidence (drafts, version history, keystroke records) and human judgment. Institutions should state this in policy and give students a documented path to contest a flag.

## Sources (all as of 2026-11-01 unless noted)

- Weber-Wulff et al., Int. J. for Educational Integrity 19:26 (2023) — https://edintegrity.biomedcentral.com/articles/10.1007/s40979-023-00146-z
- Liang et al., Patterns 4(7):100779 (2023) — https://arxiv.org/abs/2304.02819 ; https://pmc.ncbi.nlm.nih.gov/articles/PMC10382961/
- Perkins et al. (2024) — https://arxiv.org/abs/2403.19148
- Dugan et al., RAID, ACL 2024 — https://arxiv.org/abs/2405.07940
- Sadasivan et al., TMLR — https://arxiv.org/abs/2303.11156
- Krishna et al. — https://arxiv.org/abs/2303.13408
- Kirchenbauer et al., ICML 2023 — https://arxiv.org/abs/2301.10226
- Dathathri et al., Nature 634 (23 Oct 2024) — https://www.nature.com/articles/s41586-024-08025-4
- Zhang et al., ICML 2024 — https://arxiv.org/abs/2311.04378
- OpenAI, "Understanding the source of what we see and hear online" — https://openai.com/index/understanding-the-source-of-what-we-see-and-hear-online/
- The Register (26 Jul 2023) — https://www.theregister.com/2023/07/26/openai_ai_classifier/
- Synthedia (24 Jul 2023) — https://synthedia.substack.com/p/openai-shuts-down-ai-written-text
- Google DeepMind SynthID blog — https://deepmind.google/discover/blog/watermarking-ai-generated-text-and-video-with-synthid/

**Sourcing note:** Primary quotations were retrieved via the Authmarks research reviews (fetched 2026-11-22, snapshots retained in this run). Authmarks is a provenance vendor; its reviews quote primary literature verbatim and link to it, but the fact-checker should verify key quotations against the primary sources (arXiv/PMC/Nature) before publication.
