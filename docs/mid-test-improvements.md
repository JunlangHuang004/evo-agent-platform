# Mid-Test Review and Delivery Improvements

These changes build on `backend-core-upgrade` commit `0edf573` and the original Mid_Test scenario pack. The R13 report documented repeated software checks, modified Reviewer test sources, rejection of corrected drafts using old artifacts, and a packaged presentation whose added delivery notes had no final review. The implementation repairs those shared workflow paths for future runs.

## Current version checks

`backend/review_versions.py` compares a review proposal's optional `targetSnapshot`, cited tool snapshots, and artifact IDs in its findings with the current producer. A mismatched proposal receives `stale_review`. The producer stays on the same revision and retains the same rework allowance. The response supplies the current revision and hashes for a corrected proposal. Current findings can still request rework through the existing action.

This check detects explicit version mismatches. It cannot decide whether an otherwise current natural-language criticism is substantively correct. Reviewers must reread the changed draft and identify the unresolved finding.

Before an independent browser test, the engine reads only paths explicitly selected in that test through the existing confined file resolver. It compares their SHA-256 hashes with current published runtime sources from the declared review targets. A mismatch returns `review_source_mismatch` before a browser call is recorded or a container starts. The response includes an import path only when the Reviewer already has access. Unreadable paths continue through the existing tool error path. This preflight allows byte-identical local copies and cannot create permissions or test results.

## Final-package assessment

Delivery task snapshots include the latest current formal package's selected artifact hashes and a digest of its entrypoint, file manifest, and use instructions. Reviewers receive the explicit package files through the existing artifact handoff. Unselected scratch files stay outside the package snapshot.

A final review task depends on the release task and names it in `reviewOf`. Its initial context includes `deliveryReviewTargets`, with the current snapshot, package identity, entrypoint/instructions, and unchanged, modified, added, or removed paths. The planner describes this review when delivery changes the product. The same QA role can perform the source review and own a separate final-package review task.

For unchanged runtime files, `backend/delivery_delta.py` can reuse the producer's current independent browser evidence. Reuse requires the same complete runtime file set, relative paths, entrypoint, and runtime mapping. The Reviewer must read every packaged Markdown/text document through actual full-file calls, cite those calls, explicitly assess the package, and submit the supplied `targetSnapshot` with `review`. New configuration or changed runtime files fall back to fresh executable verification. A byte-identical repack continues using the original integrity proof.

The accepted record has kind `delivery_delta` and names the original runtime call plus the final documentation reads. It is revalidated against current sources, review revisions, grants, and package metadata. Changing instructions, source revisions, package files, or reviewer revisions invalidates it. A repeated identical pass reuses the original decision. Semantic quality continues to require an independent product assessment.

A formal package containing newly authored, unreviewed files now leaves an acceptance gap at finalization. Existing direct packaging of reviewed originals remains supported. New packaging documentation can close its gap through the explicit final review without repeatedly testing unchanged JS/CSS.

## Producer handoff after publication

Previously, the overdue handoff reminder applied only to unpublished tested files. Publishing a candidate removed that reminder even when the producer kept repeating successful tests. The reminder now also accepts a current validated candidate when its published runtime still matches the actual successful browser observations and current workspace hashes. It asks the producer to compare remaining criteria and explicitly finalize for independent QA. Necessary testing and repair remain available within the original deadline.

The advice does not automatically mark a task done or claim full business coverage. A candidate with stale runtime bytes or a mismatched revision does not qualify.

## Product quality guidance

The shared action prompt asks stateful products to prevent destructive writes after failed reads and to distinguish unavailable data from zero usage. Existing browser actions can test denied storage reads/writes, stale tabs, reloads, and cancellation flows. Review guidance checks metric units, periods, denominators, and underlying holdings before issuer-concentration claims. Actual generated application behavior and factual quality still need independent checks.

## Reproduce verification

```bash
python3 -m unittest tests.test_mid_test_review_versions tests.test_handoff_priority -v
RUN_BROWSER_DOCKER_TESTS=1 RUN_AGENT_SANDBOX_DOCKER_TESTS=1 \
  python3 -m unittest discover -s tests -q
```

The new version tests cover a modified private script, byte-identical copies, old-artifact rejection, obsolete tool evidence, current package documentation, changed instructions, changed runtime, duplicate acceptance, and a real Chromium source-review-to-package-review path. Existing regression tests cover dependency repair, checkpoints, ACLs, memory, evaluation, tools, and context compression. Tests use scripted models and temporary state.

The original attached pack was parsed successfully as six ordered cases and 24 evaluator checks. The initial regression was completed before a course key was configured, so it did not include a six-case model run. Historical R13 results retain their original source version and verdicts.

## Verification recorded on 19 November 2026

The full regression command with both Docker flags enabled ran 960 tests in 205.378 seconds. It passed 959 tests and skipped the Node UI smoke because Node was absent from the shell PATH. The UI smoke then passed separately using the bundled Node runtime. The eight version/package tests also passed with the real-browser flag enabled, including reloading the saved final-package evidence from SQLite. Subsequent focused prompt/context checks passed. Compilation and `git diff --check` passed.

These checks use scripted model responses, actual immutable artifact storage, and real isolated Chromium/Python where enabled. They establish the repaired workflow behavior. A new six-task model-driven success rate and generated-product quality comparison remain unmeasured.


## Course API connection follow-up

After configuring the course endpoint and requested e7 model locally, the model-list request returned HTTP 200 and listed the configured model. Five generation smoke requests returned HTTP 502, including standard and compatible transports, plain and JSON responses, the production output budget, and a direct connection bypassing the local proxy. The raw gateway response identified `bad_response_status_code`. These observations do not identify the underlying service failure or establish a task success rate.

The local `.env` remains ignored by Git. No credential is included in source, reports, or examples. The bulk runner now accepts `--time-only --deadline 1800` and records its effective call, token, and turn limits. Its bounded default remains available. The harness and time-only regression group passed all 22 tests.

## R14 diagnostics and follow-up fixes on 20 November 2026

The API accepted generation requests again. Requests named e7 while response metadata named e10. Large requests through this machine's system proxy repeatedly disconnected; the actual platform client succeeded with standard transport and a process-scoped direct course-API route. The first case resumed its checkpoint with its original deadline. The [frozen diagnostic report](../benchmarks/reports/mid-test-r14-20261120.json) records **zero done and six partial**, including all failures. This observation is not an official score or a controlled comparison with R13.

The assistant answered actual clarification questions as the user-authorized simulated requester and recorded the resulting assumptions. An automated test operator approved only exact model-proposed edits to existing generated workspace files after checking the confined path and current SHA-256. No generated product bytes were manually edited. Raw prompts, full receipts, exact approvals and databases remain local.

Four research cases exposed a deadline propagation defect: a search provider's attempt deadline was mistaken for the whole run deadline. Search now separates its operation budget from the caller's absolute deadline and returns retryable `search_unavailable` when its local budget is exhausted. The engine expires a run from its actual remaining wall time, preserving unknown-effect reconciliation and the original deadline. Regressions prove that a later valid action can run after a local timeout and that true expiry still stops execution.

Both original search entrances timed out on this machine while HKUST and arXiv pages were directly readable. A bounded Bing fallback now extracts direct links from organic result headings, excludes adverts/answer sections and unsupported wrappers, and preserves earlier provider failures. All providers share the same operation budget and the existing pinned, per-redirect URL checks. Returned titles are unfetched discovery links; claims still need actual source reads.

Software QA recorded two identical successful current-source tests with 24 assertions and 36 steps each, then proposed a third instead of reviewing. Successful compressed browser views now explain that `review.evidenceCallIds` resolves the original full host receipt. A third identical reviewer proposal can receive one nonexecuting handoff prompt, bound to two real full successful calls, reviewer identity/revision, current target snapshots, selected readable source hashes and unchanged test setup. The Reviewer must explicitly decide pass/rework or report missing coverage. The prompt never creates a test, pass or extra time; an unchanged repeat reaches the original loop stop. The marker persists across restart, and the observation is protected through the existing authenticated context-feedback path.

For this machine's verified direct API route, use a process-local launch setting:

```bash
NO_PROXY=api1.hkgai.net,127.0.0.1,localhost \
no_proxy=api1.hkgai.net,127.0.0.1,localhost \
LLM_TRANSPORT_MODE=standard python3 server.py --time-only
```

This changes only that process's provider route. Public web retrieval retains its existing validated transport. Other environments should use the route appropriate to their network.

The final regression on 20 November ran **974 tests in 204.612 seconds, all passing with no skips**, with both Docker flags enabled and the bundled Node directory on PATH. This includes two actual Chromium successes followed by an explicit review citing the original receipt without a third execution. Scripted decision-loop tests cover the one-prompt bound, continued repetition stopping, current-source checks, invalid evidence, and authenticated feedback preservation. Compilation and `git diff --check` also passed. The live web diagnostic reached Bing after retaining two real provider timeouts in 13.994 seconds. These checks do not establish the outcome of the new six-case model round.
