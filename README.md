# EvoMAC Multi-Agent Collaboration Platform

EvoMAC is a course project with configurable four-agent teams, task DAGs, independent memory and workspaces, human clarification and approvals, public web tools, isolated execution, immutable artifact handoffs, and downloadable products. The backend uses HKGAI or DeepSeek and the Python standard library. JavaScript and browser checks use the dedicated Docker image.

The latest recorded real-model round, R13, produced outputs for all six Mid-Test tasks, with two runs marked done and four partial. Those historical results remain in the [R13 evidence report](benchmarks/reports/mid-test-r13-20261116.json) and [project status report](docs/project-status-2026-11-16.md). The current fixes address the version and delivery gaps documented in that round. A new live six-task success rate requires another observed course-model run.

## Start

Use Python 3.10 or later, Git, and Docker.

```bash
cp .env.example .env
# Set the course API key in .env and preserve any existing local settings.
docker pull python:3.11-alpine
docker build -t evo-agent-browser:1.64.1 docker/browser
python3 server.py
```

Open <http://127.0.0.1:8080/#workspace>. Use `--port 8081` for a separate local instance. Model keys and runtime data stay local. The health endpoint reports model configuration and image availability.

The default profile has bounded calls, turns, tokens, and runtime. `python3 server.py --delivery` selects a larger bounded profile. `python3 server.py --time-only` sets a 30-minute wall deadline including human waiting, pauses, and restarts, with no total call, task-turn, or token caps. A time-only run can consume substantial course API usage. The original wall deadline still applies to generation recovery, tool calls, and rework.

## Mid-Test workflow improvements

- Reviewer browser tests check current published runtime hashes before execution. An altered private JS/CSS copy receives a source mismatch with the actual authorized references. Byte-identical private copies remain supported.
- Rework and rejection proposals that cite superseded artifacts, test snapshots, or explicit target snapshots are rejected before changing the producer's revision or consuming its rework allowance.
- Final-package review includes the actual selected files, entrypoint, and run instructions in its snapshot. The reviewer receives the package changes and authorized source files. For an identical runtime and URL layout, a separate explicit package assessment can reuse current browser evidence after reading the packaged documentation. Changed runtime bytes or configuration need fresh executable verification.
- A producer with an already published, tested candidate continues receiving the overdue handoff reminder. The reminder directs attention to missing criteria and independent QA while preserving actual outcomes and the original budget.
- Stateful product guidance covers failed reads, destructive stale writes, and misleading zero statistics. Review guidance also checks metric units, periods, denominators, and issuer-concentration evidence.

All 960 regression cases were verified, including real Docker checks and a separate run of the Node UI smoke. The [implementation and verification guide](docs/mid-test-improvements.md) documents the protocol, compatibility, test commands, and evidence limits.

## Run checks

```bash
python3 -m unittest discover -s tests -q
```

Enable actual Docker browser and Python isolation checks after preparing both images.

```bash
RUN_BROWSER_DOCKER_TESTS=1 RUN_AGENT_SANDBOX_DOCKER_TESTS=1 \
  python3 -m unittest discover -s tests -q
```

On machines whose HTTP proxy intercepts local requests, set `NO_PROXY=127.0.0.1,localhost` and `no_proxy=127.0.0.1,localhost` for the test command. The default test suite uses scripted model responses. Explicit real-model harnesses require local course credentials.

The original Mid_Test pack defines three teams with two sequential tasks each. Task 2 continues the same team's Task 1 context. Parse its original role prompts and tasks without calling a model.

```bash
python3 benchmarks/mid_test_acceptance.py \
  --suite-root /path/to/Mid_Test --suite-only \
  --output .runtime/mid-test-suite.json
```

For an observed live run, use a fresh runtime/output location and answer actual clarification or permission requests through the local interface.

```bash
python3 benchmarks/mid_test_live.py --live \
  --suite-root /path/to/Mid_Test --config-root . \
  --data-dir .runtime/mid-test-live --output .runtime/mid-test-exports
```

Add `--time-only --deadline 1800` to select the platform wall-deadline profile for each task without total call, turn, or token caps. The run report records the effective profile and limits.

Use `benchmarks/mid_test_acceptance.py --help` to score frozen exports with evaluator-owned evidence. Process completion, product integrity, and independent product verdicts are recorded separately. Failed, timed-out, and unassessed cases remain in the scoring denominators. The [acceptance guide](benchmarks/ACCEPTANCE.md) also documents the replaceable CSV suite and SQLite evaluation storage.

## Architecture and tools

Each team has its own `Platform` and durable state. The built-in teams retain the original Software Development, Newsroom, and Securities Investment Fund identities. The model determines task decomposition within capability and dependency checks. Agents can delegate, exchange messages, ask the human, publish artifacts, and request independent review.

Code runs in per-agent Docker sandboxes with resource limits, offline networking, and read-only imported artifacts. File tools enforce workspace confinement and reject unsafe links or traversal. Public fetch checks destinations and redirects. Publication creates immutable snapshots with explicit grants. Git commits and action-specific approved pushes use the configured remote and exact approved commit.

The scheduler records revisions, tool results, approvals, checkpoints, and uncertain effects. Reconciliation gates preserve an interrupted operation's actual status before any replay. The 90-second progress watchdog queues observations for normal action boundaries without making additional model calls. Browser failure recovery and generation retries remain bounded by their original policies.

Memory is owner-scoped and distinguishes factual episodes, unverified candidates, and verified strategies. Learning records whether a candidate was actually tried and whether later evidence supports it. Memory effectiveness needs a controlled comparison with matched model, budget, prompts, and independent acceptance. Historical retrieval improvements and candidate counts alone cannot establish better product delivery.

Detailed architecture is retained in [the architecture document](docs/architecture.md). Historical rounds and their limitations remain under `benchmarks/reports/` and in the project status report.

## Source layout

```text
backend/engine.py             # Scheduling, action admission, review, approvals, recovery
backend/runtime.py            # Plan/action protocol and bounded model context
backend/teams.py              # Team identities and capability registries
backend/tools.py              # File, web, code, browser, and Git gateway
backend/sandbox.py            # Docker execution and confined file operations
backend/artifacts.py          # Immutable publication, explicit grants, inherited outputs
backend/delivery*.py           # Packaging, reviewed sources, and package delta evidence
backend/review*.py             # Current review receipts and version checks
backend/handoff_priority.py   # Evidence-based producer handoff observations
backend/memory.py              # Owner-scoped memory management
backend/learning.py            # Candidate trials and evidence-based learning
backend/persistence.py         # SQLite state and audit storage
backend/evaluation*.py         # Independent scoring and saved-run adapters
backend/product_acceptance.py # Evaluator-owned Mid-Test product rubrics
benchmarks/                   # Reproducible observation and evaluation commands
js/                           # Browser interface and local API client
.runtime/                     # Ignored local state, exports, and credentials-free evidence
```

`GET /api/health`, `GET /api/state`, and `GET /api/runs/<id>` expose public platform state. `POST /api/action` requires JSON and `X-EvoMAC-Client: 1`. The server binds to localhost and enforces same-origin access. Public hosting requires authentication.
