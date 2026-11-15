# EvoMAC Multi-Agent Collaboration Platform

EvoMAC is a course project that connects a three-page web interface to HKGAI or DeepSeek. The Python backend provides a task DAG, parallel scheduling, a tool gateway, Docker isolation, human approvals, artifact handoffs, and per-agent memory.

## Start the platform

Use Python 3.10 or later, Git, and Docker. The Python backend uses only the standard library.

```bash
cp .env.example .env
# Set the course API key in .env. Keep existing local settings if already configured.
docker pull python:3.11-alpine
python3 server.py
```

Open <http://127.0.0.1:8080/#workspace>. Press Ctrl+C to stop the server. Run `python3 server.py --port 8081` to change ports. Use the backend server for the API and the web interface.

The health endpoint reports Docker and image availability. Python tools require Docker. Keep model credentials in the local `.env` file.

## Run independent acceptance

```bash
python3 benchmarks/acceptance.py \
  --suite benchmarks/fixtures/acceptance/suite.json \
  --runs benchmarks/fixtures/acceptance/runs.json \
  --data-dir .runtime/acceptance-demo \
  --output .runtime/acceptance-demo/result.json
```

The demo uses handwritten run receipts and real Docker reruns of the registered fixture scripts. It includes a pass, a completed run with incorrect code, a timeout, and a crash. Expect exit code `1` and a complete JSON report. This command requires the local Docker image and no model key.

The [acceptance guide](benchmarks/ACCEPTANCE.md) documents the scoring rules, sample results, interchangeable suites, and the handoff from saved platform runs.

## Execution lifecycle

1. Group task mode asks the model for a structured plan. The backend validates IDs, participants, dependencies, and cycles.
2. The scheduler chooses assignees by capability, tool permissions, workload, and cost. Independent tasks can run in parallel. Dependent tasks wait for their prerequisites.
3. Agents use a JSON action protocol to call permitted tools, delegate, await results, decline work, request clarification, and exchange handoff messages.
4. Agents register text files as immutable artifacts. The orchestrator authorizes read-only copies along task dependencies and records each grant.
5. Overwrites, patches, deletions, and Git pushes pause for action-specific approval. Approvals, denials, argument changes, and expirations are persisted.
6. Execution produces episodic records and candidate reflections. Only strategies confirmed by a person or trusted evidence are eligible for future retrieval.
7. After a service interruption, resume from the saved checkpoint. An external write with an uncertain result requires reconciliation before replay.

Ordinary chat uses only the model. Private chat defaults to ordinary chat and can switch to single-agent task mode.

## Roles

| Agent | Capabilities |
| --- | --- |
| Coordinator | Plans, delegates, requests clarification, handles files, and synthesizes results |
| Research | Handles private files, searches public sources, and fetches web pages |
| Executor | Handles private files, runs and tests Python, repairs code, and uses Git |
| Reviewer | Uses a separate workspace to test Python, reviews evidence, and can request bounded rework |

The backend defines each tool's argument schema, permissions, risk, time limits, and idempotency rules. Actions and results are recorded as events. The run is scoped to selected participants.

## Isolation and handoffs

Python runs in an isolated Docker container with a writable run workspace and read-only artifact imports. Network access and Linux capabilities are disabled. The root filesystem is read-only. CPU, memory, process count, file size, output, and runtime are bounded. The Docker socket, host project, and API key stay outside the container.

File tools reject traversal, hidden configuration paths, symbolic links, hard links, and special files. Change approvals bind the original SHA-256. A changed file invalidates the approval. Public web tools check each redirect and pin a resolved public IP to block private and metadata addresses and credential-bearing URLs.

Git metadata and hooks live outside the code workspace. Push is limited to the configured HTTPS `GIT_REMOTE`, the current agent's run branch, and the exact approved commit. Force push is disabled. An optional `GIT_TOKEN` is passed only to the approved Git subprocess. Commit authorization and push authorization are separate decisions.

## Memory and evolution

- Episodic memory records real tasks, actions, results, and failure reasons.
- Procedural and semantic memory hold strategies or knowledge with provenance, applicability, confidence, evidence, and outcome counts.
- Working memory holds bounded per-agent context. The full action history remains in the audit log after compaction.
- Candidate strategies become retrievable after explicit confirmation. Private chat experience stays private by default. Sharing across owners requires authorization.
- Confirmed strategies can replace older strategies while retaining history. Later outcome feedback is deduplicated by task. Repeated failures reduce confidence and may require reconfirmation.

## Reliability and scope

The runtime supports bounded retries for models and idempotent tools, token and request budgets, time and delegation depth limits, cycle and repeated-action detection, pause and cancellation, checkpoints, and an idempotency log for external side effects. One human-approved budget extension is available per run. Requests already in flight may finish, and cancellation cannot undo completed changes.

Code execution supports Python. Search may be rate-limited, and models may time out or return invalid actions. The evaluation view distinguishes execution state from independent acceptance. The course's official dataset and end-to-end performance comparison require separate runs.

## Project layout

```text
server.py                   # Server entry point
backend/
  config.py                 # Local settings and credential redaction
  server.py                 # Same-origin JSON API and static file allowlist
  engine.py                 # Scheduling, agent runtime, approvals, and recovery
  runtime.py                # Action protocol and DAG validation
  agents.py                 # Role capabilities, tools, and costs
  provider.py               # HKGAI and DeepSeek HTTP transport
  tools.py                  # File, web, Python, and Git gateway
  sandbox.py                # Restricted Docker and safe file operations
  artifacts.py              # Registration, snapshots, grants, and read-only imports
  memory.py                 # Memory confirmation, retrieval, feedback, and retention
  persistence.py            # SQLite storage, migrations, checkpoints, and audit records
  evaluation.py             # Acceptance results, scoring, and persistence adapter
  evaluation_platform.py    # Evidence adapter for stored platform runs
  order_acceptance.py       # CSV order acceptance and exact JSON verification
benchmarks/                 # Acceptance and research benchmark entry points
js/                         # Web views and API client
tests/                      # Unit and regression checks
.env.example                # Settings template without credentials
.runtime/                   # Ignored local databases and workspaces
```

## API and tests

- `GET /api/health` reports server, model, and Docker capabilities.
- `GET /api/state` returns channels, tasks, tools, approvals, artifacts, memory, and evaluation records.
- `GET /api/runs/<run-id>` returns the public view of a saved run.
- `POST /api/action` supports `send`, `participants`, `pause`, `resume`, `recover`, `cancel`, `reset`, `approval`, `memory_confirm`, `memory_share`, and `refresh_capabilities`.

POST requests require `Content-Type: application/json` and `X-EvoMAC-Client: 1`. The server is intended for local use and rejects cross-origin requests. The static allowlist blocks access to local configuration, the database, and backend source. Public deployments need authentication.

```bash
python3 -m unittest discover -s tests -v
```

Tests use scripted models, temporary databases, and tool doubles where appropriate. The acceptance demo exercises real Docker. Public web and live course model checks run separately.

## Four-role verification

When all four roles participate in group task mode, the plan must cover all four roles. The Coordinator's final synthesis depends on the three specialists. Capability constraints enforce any explicit role assignment.

Code tasks require a successful independent Reviewer `code.test` tied to the current task revision and artifact hashes. Missing evidence or a stale revision can leave execution partial. Documentation-only work does not require Python tests.

The baseline passed a scripted-model integration with real public fetch, Docker execution, read-only handoff, and review. Live HKGAI chat, short actions, and planning have returned successfully. Full live model runs have encountered API timeouts. DeepSeek and an agent-driven remote Git push still require team integration. Graceful shutdown cleans up tracked containers. After a forced termination, inspect any containers left running.

## Memory retrieval benchmark

Retrieval uses BM25F-style ranking across triggers, applicability, titles, and lesson text, with confidence and recency signals. Verification, scope, and authorization filter the corpus before ranking. Deduplication applies only to equivalent strategies with the same owner, scope, kind, and retrieval fields. It preserves original evidence and avoids letting duplicates distort corpus statistics.

The implementation draws on [A-Mem](https://proceedings.neurips.cc/paper_files/paper/2025/hash/19909c36f51abc4856b4560aff3d36d6-Abstract-Conference.html) and [ACE](https://proceedings.iclr.cc/paper_files/paper/2026/hash/8a94ff6f922d995d7d3f4ebf4143e442-Abstract-Conference.html). It implements lightweight retrieval changes. The papers' model training and reported improvements are outside this benchmark.

The frozen, manually labeled v2 fixture has 55 strategies and 24 English and Chinese queries. Results use the same labels for baseline commit `94b7030` and the upgraded implementation.

| Metric | Baseline | Upgraded |
| --- | ---: | ---: |
| Top-1 hit rate | 83.33% | 91.67% |
| MRR | 0.899306 | 0.940972 |
| Precision@3 | 0.722222 | 0.763889 |
| Duplicate slots in the top three | 5 | 0 |

The fixture measures retrieval on a small labeled dataset. Some queries regress, and average injected context length does not improve. The v2 fixture documents eight missing labels and four incorrect relevance labels corrected from v1. The fixed-denominator Precision@3 ceiling is 0.944444 because two queries each have a single directly relevant strategy.

```bash
python3 benchmarks/memory_retrieval.py
```

To compare another checkout, use `--source /path/to/baseline-checkout --output /path/to/results.json`. The benchmark reports full rankings and the fixture hash without using a model or the network.

Inspired by [ToolScope](https://aclanthology.org/2026.acl-long.1573/) and [MCP-Zero](https://arxiv.org/abs/2506.01056), the on-demand action documentation experiment reduced valid actions from 12 out of 18 to 9 out of 18 across nine paired synthetic scenarios and increased latency. That runtime change was reverted. `benchmarks/protocol_live.py` retains the comparison harness and contacts the course API only with `--live`.

`benchmarks/four_agent_live.py` is a separate opt-in smoke test for live course models, public fetch, and Docker. It requires a new `--data-dir`, a local `--config-root`, and an `--output` path. Its preset roles and task cover a synthetic programming scenario.
