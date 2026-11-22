# Can AI-writing detectors reliably tell whether a student's assignment was written by AI?

**A fact-check for students and faculty**

**By:** Editor, Newsroom | **Published:** 22 November 2026

---

## The claim

> "AI-writing detectors can reliably determine whether a university student's assignment was written by AI."

## Our verdict: **Not supported as stated.**

Read "reliably" as a strict standard — near-zero error, defensible in a misconduct hearing — and the claim fails. The best independent evidence shows that AI-text detectors are **probabilistic signals, not determinations**. No detector in the largest independent evaluations reached 80% overall accuracy; false positives never fell to zero; and accuracy collapses under paraphrasing, translation, or light editing. A detector score alone cannot establish that a student used AI.

This does **not** mean detectors are useless. The research supports a narrower conclusion: their error rates are incompatible with using a score as *proof* in a high-stakes accusation. As one screening signal among several, they may still have a limited role.

---

## What the evidence shows

### 1. Accuracy is far below "reliable"

- **Weber-Wulff et al. (2023), *International Journal for Educational Integrity* 19:26** tested 14 tools across six conditions. Reported finding: *"Detection tools for AI-generated text do fail, they are neither accurate nor reliable (all scored below 80% of accuracy and only 5 over 70%)."* Reported per-condition accuracy: human-written text 96%; human text machine-translated −20 points; unmodified AI text 74%; AI text with human editing 42%; AI text machine-paraphrased 26%. The authors reportedly concluded the systems *"should not be used in academic settings."*
- **OpenAI's own AI Text Classifier (2023)** — the vendor's launch figures: it correctly identified 26% of AI-written text and flagged human-written text 9% of the time. It was retired on 20 July 2023, reportedly *"due to its low rate of accuracy."*
- **Perkins et al. (2024), arXiv:2403.19148** — 6 detectors, n=805, higher-education setting. Reported baseline accuracy 39.5%, falling to 17.4% under simple adversarial manipulation. The authors reportedly wrote that *"the potential for false accusations demonstrate that these tools cannot currently be recommended for determining whether violations of academic integrity have occurred."*
- **RAID (Dugan et al., ACL 2024, arXiv:2405.07940)** — 12 detectors, 6M+ generations, 11 models, 8 domains, 11 attacks. Reported finding: *"Default False Positive Rates (FPRs) of open-source detectors are dangerously high,"* and *"the problem of false positives remains unsolved."* Its central methodological point: **an accuracy number is meaningless without the false-positive rate it was measured at.**

### 2. False positives fall hardest on non-native English writers

- **Liang et al. (2023), *Patterns* 4(7):100779 (arXiv:2304.02819)** — 7 detectors on 91 human-written TOEFL essays (non-native writers) versus 88 US 8th-grade essays. Reported average false-positive rate on the TOEFL essays: **61.3%**; 19.8% were flagged by *all* detectors and 97.8% by at least one, while the same detectors were near-perfect on the US essays. AI-rewriting the flagged essays reportedly dropped detection to near zero. The authors reportedly cautioned *"against their use in evaluative or educational settings."*
- **Weber-Wulff et al.** — machine-translated human text reportedly lost 20 points of accuracy; 6 of 14 tools produced false positives; for one tool, reportedly *"half of the positive classifications would be false accusations."*
- **OpenAI** reportedly withheld its watermark partly because it *"could stigmatize use of AI as a useful writing tool for non-native English speakers"* and because at scale it *"would lead to a large number of total false positives."*

### 3. The evasion arms race

- **Krishna et al. (arXiv:2303.13408)** — the DIPPER paraphraser reportedly dropped DetectGPT detection from 70.3% to 4.6% at a fixed 1% false-positive rate, *"without appreciably modifying the input semantics."*
- **Sadasivan et al. (TMLR, arXiv:2303.11156)** — recursive paraphrasing reportedly degrades watermarks, neural classifiers, zero-shot and retrieval detectors, and spoofing can make human text read as AI. Reported theoretical bound: the best-possible detector AUROC is capped by the total-variation distance between human and AI text distributions, which shrinks as models improve.
- **Watermarking** — the Kirchenbauer et al. (ICML 2023) green-token scheme; Google's SynthID-Text deployed in Gemini (*Nature* 634, Oct 2024) is reportedly *"weakened by edits… such as through LLM paraphrasing"* and *"less effective on responses to factual prompts."* OpenAI's watermark was reportedly *"trivial to circumvention."* Zhang et al. (ICML 2024) reportedly argued that *"strong watermarking is impossible"* under stated quality-oracle and perturbation-oracle assumptions.

---

## What the evidence does *not* establish

- **Current 2026 error rates.** Every study above evaluated the tools of its moment (2023–2024). None measures today's commercial detectors against today's models. The *structural* findings — threshold sensitivity, paraphrase fragility, the theoretical ceiling — are the durable part; the specific percentages are historical.
- **Per-student error rates in real classrooms.** These are corpus studies, not classroom trials.
- **That detectors are useless.** The literature's claim is narrower: error rates are incompatible with using a score as *proof* in a high-stakes accusation.

---

## What this means for students and faculty

**Recommendation: detector output should never be used as sole evidence in an academic-integrity determination.**

- **For faculty and administrators:** Treat a detector score as, at most, one weak screening signal — never as a finding. Corroborate with process-based evidence (drafts, version history, keystroke records, in-person discussion of the work) and human judgment. State this explicitly in policy, and give students a documented, timely path to contest a flag. Be especially cautious with multilingual and non-native-English writers, who face the highest measured false-positive rates.
- **For students:** A detector flag is not proof you used AI. If flagged, ask what evidence beyond the score is being relied on, and use your institution's appeal process. Keep drafts and version history as a matter of habit.

---

## How confident are we?

**High confidence** in the direction of the finding: the claim is not supported, and detector output is a probabilistic signal rather than a determination. This rests on the convergence of independent lines of evidence and on structural arguments (threshold sensitivity, paraphrase fragility, the theoretical bound) that do not depend on any single number.

**Lower confidence in exact figures.** The specific percentages and quotations above were retrieved second-hand through a vendor research review (Authmarks, a provenance vendor) and contemporaneous news reporting; the primary sources (arXiv, PMC, *Nature*, the journal) were not read directly in this fact-check. They are internally consistent and match widely cited literature, but they are **attributed, not primary-verified**, and are labeled as such. Treat the numbers as indicative, not exact.

**Residual uncertainty** concerns exact current error rates, not the direction of the finding.

---

## Sources

All figures are as reported in the sources below; primary literature was not read directly in this fact-check (see confidence note).

- Weber-Wulff et al., *Int. J. for Educational Integrity* 19:26 (2023) — https://edintegrity.biomedcentral.com/articles/10.1007/s40979-023-00146-z
- Liang et al., *Patterns* 4(7):100779 (2023) — https://arxiv.org/abs/2304.02819 ; https://pmc.ncbi.nlm.nih.gov/articles/PMC10382961/
- Perkins et al. (2024) — https://arxiv.org/abs/2403.19148
- Dugan et al., RAID, ACL 2024 — https://arxiv.org/abs/2405.07940
- Sadasivan et al., TMLR — https://arxiv.org/abs/2303.11156
- Krishna et al. — https://arxiv.org/abs/2303.13408
- Kirchenbauer et al., ICML 2023 — https://arxiv.org/abs/2301.10226
- Dathathri et al., *Nature* 634 (23 Oct 2024) — https://www.nature.com/articles/s41586-024-08025-4
- Zhang et al., ICML 2024 — https://arxiv.org/abs/2311.04378
- OpenAI, "Understanding the source of what we see and hear online" — https://openai.com/index/understanding-the-source-of-what-we-see-and-hear-online/
- The Register (26 Jul 2023) — https://www.theregister.com/2023/07/26/openai_ai_classifier/
- Synthedia (24 Jul 2023) — https://synthedia.substack.com/p/openai-shuts-down-ai-written-text
- Google DeepMind SynthID blog — https://deepmind.google/discover/blog/watermarking-ai-generated-text-and-video-with-synthid/

**Sourcing note:** Primary quotations and figures were retrieved via the Authmarks research reviews (fetched 2026-11-22, snapshots retained in this run) and contemporaneous news reporting. Authmarks is a provenance vendor; its reviews quote primary literature and link to it, but the underlying primary sources were not read directly in this fact-check. Two attempted fetches failed (journals.physiology.org returned 403; a University of San Diego law library guide returned 404) and are **not** cited as read.
