# mozg-eval: proposal

Status: draft for review, revision 5, 2026-11-14. Nothing is built yet.

Revision 2 added the connector model (drivers and sources), streaming event logs, a storage layer
that can move from SQLite to Postgres, a single-user server for trends, and a runner agent. It also
folded in the competitor research
([report](https://claude.ai/artifact/GFfAWhVFxTL8w7sMBxcZJJ), private).

Revision 3 puts event logs and artifacts behind pluggable storage backends (disk, S3, ClickHouse),
makes the runner a thin executor with a local and a daemon mode, moves the judge onto PydanticAI,
and settles the UI on HTMX.

Revision 4 closes the open questions: judges use API keys only (Anthropic, OpenAI, OpenRouter),
isolation stays local-only with Docker later, the package is MIT `mozg-eval` with the `mzeval` CLI,
and cases stay in each plugin repo.

Revision 5 drops the runner daemon: remote and scheduled runs go through CI (`workflow_dispatch`
and `on: schedule` in the plugin repos), and the CLI ships as Python with PyApp single-file
binaries.

## 1. What we are building

A vendor-neutral eval system for agent plugins and skills, in three parts:

- **A runner (CLI).** One case file runs on **Claude Code, Codex CLI, Pi and OpenCode**, and on any
  other runtime through a driver connector. It works offline and in CI.
- **A results server (Docker, single-user for now).** It stores every run and shows how scores move
  over time, so quality can be measured on a schedule instead of by hand.
- **Remote runs through CI.** The server's Run button and nightly schedules trigger a workflow in
  the plugin repo, which runs the same CLI with `--push`. Your own machine joins as a GitHub
  self-hosted runner when a run needs it.

It answers three questions per plugin:

1. **Routing:** does the right skill (or subagent, or command) fire, and stay silent on near-misses?
2. **Behaviour:** once it fires, does the agent do the job? This is graded by deterministic checks
   first, then an LLM judge.
3. **Lift and regressions:** is the plugin better than no plugin? Is this commit worse than the
   last one? Is vendor A worse than vendor B? Every answer comes with error bars.

The product doesn't author plugins or render them per vendor. Plugin repos keep their cases and pin
`mozg-eval` as a tool (`uvx mozg-eval@X.Y.Z` or a PyApp binary), so its Python version is
independent of theirs.

```
 DRIVERS (we start the run)                    SOURCES (someone else ran it)
 claude │ codex │ pi │ opencode │ stdio driver   OTLP │ JSONL push │ file import
            │                                              │
            └──────────────┬───────────────────────────────┘
                           ▼  canonical events, streamed (JSONL)
 ┌────────────────────────── mzeval core ─────────────────────────────┐
 │ run log (append-only) ──► live guards: cost cap, max turns, kill   │
 │        ▼                                                           │
 │ graders: deterministic ─► LLM judge (cross-vendor, cached)         │
 │        ▼                                                           │
 │ stats (Wilson, pass^k, paired) ─► Result / Event / Artifact stores │
 └────────┬───────────────────────────────────────────┬───────────────┘
          ▼                                           ▼
 CLI / CI: exit code, JUnit,               server (Docker): ingest API,
 static HTML report, --push ─────────────► trends UI, compare, drill-down
                                                      │ (phase C) Run button
                                                      ▼ workflow_dispatch
                                           CI job in the plugin repo:
                                           mzeval run --push --run-id …
```

## 2. What exists today (evidence from both repos)

|                 | claude-mozg-pipelines                                                         | mozg-video-plugins                        |
| --------------- | ----------------------------------------------------------------------------- | ----------------------------------------- |
| Runner          | `claude plugin eval` + custom Codex runner (≈2.5k LOC in `tests/eval_tools/`) | `claude plugin eval`, run by hand         |
| Cases / graders | 350 / 949, 69 `fixture.sh`                                                    | 184 / 249                                 |
| Case format     | `case.yaml` schema 1.1                                                        | same, under `plugins/<p>/evals/`          |
| Grader types    | `tool_used` 595, `regex` 352, `file_exists` 2, **no `llm`**                   | `tool_used` 241, `regex` 4, `llm` 4       |
| Kind mix        | ~80% routing (fire / near-miss), plus gate, behaviour, args, hooks            | 98% routing, 4 traps                      |
| Results         | `aggregate-result.json` + built-in `report.html`, gitignored                  | same, archived by hand in `.mz/evidence/` |
| Spend tracking  | `--max-cost-usd`                                                              | `docs/spend_log.md` by hand ($85.90)      |

Pain points this product must remove, all observed in the repos:

- **Graders are welded to Claude's wire format.** On Codex, 613 of 931 assertions came back
  "unsupported". `tool_used: Skill`, `Agent` and `Write`, and trace regexes over `"type":"tool_use"`,
  have no Codex equivalent.
- **One plugin per run** in `claude plugin eval`, so cross-plugin near-misses can't be measured
  (dramaturgy README).
- **Silent unknown keys.** The video repo wrote ~1.8k lines of per-plugin pytest linters to catch
  typos.
- **No cost estimate before a run, no spend ledger, no token counts** (Claude) or **no USD** (Codex).
- **Traces are deleted without `--keep-temp`**, so a passing negative proves only silence.
- **Threshold 1.0 plus 1–3 runs and no confidence intervals**: one flake fails a case, and nobody can
  tell noise from regression.
- **No trends, no graphs, no CI target.** Summaries come from ad-hoc per-run `summarize.py` scripts.
- **Judge drift**: the publish trap judge went 1/4 → 4/4 across prompt tweaks, with no versioning
  and no calibration.
- Hand-rolled infrastructure that belongs in a product: hash-freezing inputs, mutation testing
  (`mutants.py`), the Codex hook bridge.

## 3. Competitors and where we fit

The full comparison (19 tools, 16 planes) is in the report linked above. What matters for the
design:

| Tool                                       | Runs real CLIs            | CC / Codex / Pi / OC | Trigger metrics                           | Stats                            | Threat                        |
| ------------------------------------------ | ------------------------- | -------------------- | ----------------------------------------- | -------------------------------- | ----------------------------- |
| **UiPath coder-eval** (Apache-2.0, Python) | yes, subprocess or Docker | Y / Y / Y / Y        | `skill_triggered`, per-label P/R/F1 gates | mean per replicate               | **high**                      |
| promptfoo (OpenAI-owned, MIT)              | via agent SDKs            | Y / Y / - / Y        | `skill-used`, native or heuristic         | `--repeat` only                  | high                          |
| `claude plugin eval`                       | yes                       | Y / - / - / -        | per-case `tool_used`                      | mean, with−without Δ             | high for Claude-only users    |
| Harbor (Apache-2.0)                        | yes, containers           | Y / Y / Y / Y        | none                                      | mean reward                      | medium-high; optional backend |
| Inspect AI + inspect_swe (MIT)             | via model proxy           | Y / Y / - / Y        | none                                      | **best**: pass@k, pass^k, Wilson | medium; reference for stats   |
| Skillgrade (MIT, TS)                       | yes, Docker or local      | Y / Y / - / Y        | Codex only (regex)                        | degenerate pass@k                | medium                        |
| Langfuse, LangSmith, Braintrust, Phoenix   | no (tracing)              | trace ingest only    | none                                      | none                             | low; export sinks             |

**What changed after the research:** coder-eval already covers all four agents and reports trigger
precision and recall, so routing metrics alone no longer set us apart. What still does:

1. **Statistics.** Case-level Wilson CIs, unbiased pass^k, paired tests for with/without and A/B,
   and regression flags only when a drop is significant. No competitor has built-in paired tests.
2. **Native CLI fidelity.** The installed CLI with temp-HOME isolation, plus skill evidence marked
   `native` or `inferred`, rather than an SDK or a model proxy.
3. **An auditable judge.** Cross-vendor, evidence quotes checked against the transcript, cached and
   versioned, κ against human labels.
4. **Drivers and sources behind one event stream.** Coding CLIs and other agent runtimes graded the
   same way, with trends on a server you own.
5. **Migration** of 534 `claude plugin eval` cases and skill-creator `evals.json`.

Before building drivers (milestone 4), spend one day importing 20 cases into coder-eval and running
them on Claude and Codex. Contribute upstream if its traces expose per-call skill evidence and its
scoring can be made case-level. Otherwise build as planned and borrow its `plan` command and
`suite_thresholds`.

## 4. Key design decisions

### 4.1 Connectors: drivers and sources

A connector turns some runtime's activity into canonical events. There are two kinds, because they
allow different things:

|                                  | Driver                                                          | Source                                                              |
| -------------------------------- | --------------------------------------------------------------- | ------------------------------------------------------------------- |
| Who starts the run               | mozg-eval                                                       | the runtime itself (your app, another tool)                         |
| Examples                         | Claude Code, Codex, Pi, OpenCode, Gemini CLI, any stdio adapter | a LangGraph service, an in-house agent, OTel traces from production |
| Repeats, with/without ablation   | yes                                                             | no                                                                  |
| Isolation (temp HOME, container) | yes                                                             | no                                                                  |
| Routing precision/recall         | yes, on labelled cases                                          | only if each run carries an expected label                          |
| Graders, judge, trends           | yes                                                             | yes                                                                 |

Coding CLIs should be **drivers** whenever possible, because only drivers give controlled
experiments. Agent apps we don't launch become **sources**.

**Driver protocol.** A driver is either built in (Python, in-process) or any executable that speaks
JSONL over stdio, the way LSP and MCP servers do. Both implement the same contract, so the four
built-ins prove the protocol. A Go or TypeScript runtime then needs only a small adapter.

```yaml
# drivers/gemini/driver.yaml
name: gemini
command: [gemini-mzeval-driver]          # any executable on PATH
version_cmd: [gemini, --version]         # recorded in every result
capabilities:
  skills: inferred                       # native | inferred | none
  subagents: none
  hooks: none
  usage: tokens                          # usd | tokens | none
  isolation: home                        # none | home | container
```

```jsonl
→ {"type":"start","trial":"t-7f3a","prompt":"...","workspace":"/tmp/mz/ws","env":{"HOME":"/tmp/mz/home"},"plugins":["/repo/plugins/davinci-resolve"],"limits":{"max_turns":8,"timeout_s":240}}
← {"seq":1,"type":"message","role":"assistant","text":"Checking the render first..."}
← {"seq":2,"type":"skill_activated","skill":"resolve-verify","evidence":"inferred"}
← {"seq":3,"type":"usage","in":1820,"out":412}
← {"seq":4,"type":"end","status":"ok"}
→ {"type":"cancel"}                      # sent by a guard; the driver must stop within 5 s
```

Capabilities feed the grader coverage rule from 4.2: a grader that needs `subagents` reports `n/a`
on a driver without them, never `fail`.

**Sources.** Three ways in, all producing the same events:

- **OTLP/HTTP**, mapping OpenTelemetry GenAI spans to events. The GenAI conventions are still
  marked experimental, so the mapping is versioned and has its own tests.
- **JSONL push**: `POST /v1/runs/{run}/events`, resumable by `seq`.
- **File import**: `mzeval ingest trace.jsonl --format otel|atif|jsonl`, which also covers Harbor's
  ATIF trajectories.

Source runs are graded by **observe suites**: graders and judge criteria attached to runs by a
selector (`source: billing-agent`, `tag: nightly`) instead of a prompt.

### 4.2 Canonical events: graders never see vendor JSON

Every connector emits one event model. Graders read only that model, which fixes the "613
unsupported" problem.

Envelope: `run, trial, seq, t, type, payload, evidence (native | inferred)`. Types: `start`,
`message`, `tool_call`, `tool_result`, `skill_activated`, `subagent_spawned`, `file_change`,
`hook_fired`, `usage`, `log`, `error`, `end`, and `check_result` (post-run workspace checks, 4.11).

| Canonical event    | Claude Code                          | Codex                                                    | Pi                     | OpenCode V2                      |
| ------------------ | ------------------------------------ | -------------------------------------------------------- | ---------------------- | -------------------------------- |
| `skill_activated`  | `tool_use` name `Skill`              | inferred: a command that reads the installed `SKILL.md`  | `read` of `…/SKILL.md` | `tool_use`, `part.tool=skill`    |
| `subagent_spawned` | `tool_use` name `Agent`              | — (`spawn_agent` names no agent; a `subagent` tool call) | — (no subagents)       | `tool_use`, `part.tool=subagent` |
| `tool_call{kind}`  | `Bash/Read/Write/Edit/…`             | `command_execution`, `file_change`, `mcp_tool_call`      | `tool_execution_*`     | `tool_use` (`shell`, `edit`, …)  |
| `file_change`      | from `Write`/`Edit` + workspace diff | `file_change` + workspace diff                           | workspace diff         | workspace diff                   |
| `hook_fired`       | `hook_response`                      | capture wrapper around native plugin hooks               | extension              | plugin                           |
| `usage`            | `result.total_cost_usd` + usage      | tokens only → priced by us                               | `usage.cost.total`     | sum of `step_finish`             |

Every grader declares which capabilities it needs. An unsupported grader reports `n/a`, never
`fail`, and the report shows a per-connector coverage column so gaps stay visible instead of looking
like regressions.

Workspace diff (snapshot before and after) is the vendor-neutral ground truth for files. Tool calls
are only the evidence for *how* the agent got there.

### 4.3 Streaming and the run log

Connectors yield events as they happen (an async iterator in Python, stdout lines for stdio
drivers). The core fans each event out to:

- **the run log**: the `EventStore` (4.7), append-only, the source of truth for grading. Locally
  that is an `events.jsonl` per trial. The vendor's raw stream goes to the `ArtifactStore` as
  `raw.jsonl`, so a normaliser bug can be fixed with `mzeval renormalize <run>` instead of paying
  for a rerun (the same idea as `rejudge`);
- **guards**: cost cap, max turns, timeout, forbidden tool. A guard sends `cancel` and records why;
- **progress**: a live line per trial in the terminal;
- **the server** when `--push` is on: batched, resumable by `seq`, so a dropped connection resends
  only the missing tail.

Graders run after `end`, on the stored log, so live and replayed runs grade identically. The only
exception is the post-run workspace checks, which the runner records as events (4.11).

### 4.4 Case format: v2, a strict superset of `claude plugin eval` 1.1

Before, in today's Claude-specific form:

```yaml
graders:
  - name: skill-silent
    type: tool_used
    tool: Skill
    input_match: '"skill"\s*:\s*"(?:[\w-]+:)?resolve-render"'
    min: 0
    max: 0
    arm: both
```

After, vendor-neutral and strict (unknown keys fail the load):

```yaml
schema: mzeval/2
name: render-negative-1
kind: near-miss                 # fire | near-miss | gate | behaviour | trap
expect:
  skill: { not: resolve-render, owner: resolve-verify }   # feeds the confusion matrix
bundle: [davinci-resolve, dramaturgy]                     # several plugins loaded at once
execution:
  prompt: >-
    ...
  tools: [skill]                # canonical tool kinds, mapped per driver
  max_turns: 8
  timeout_s: 240
  runs: 3
graders:
  - type: judge                 # only when deterministic checks can't decide
    checklist:
      - Says the file must be checked with ffprobe before upload
drivers: { exclude: [pi] }      # optional, e.g. needs subagents
```

`expect.skill` is first-class rather than a regex over tool input. That turns the 400+ routing
graders into labelled data for precision, recall and the confusion matrix.

Deterministic graders: `skill`, `subagent`, `tool_called{kind, match, path_match}`, `tool_order`,
`file_changed{path_match, content_match, change?}`,
`regex{target: final|transcript|replies|tool_results|created_files|{file}}`, `file_exists`,
`command` (a script that runs in the workspace after the session and passes on exit 0, which
`claude plugin eval` lacks), and `hook_fired{event?, match}`. Escape hatch:
`raw_regex{driver: claude}` for the trace regexes that really are Claude-specific.

Each driver declares what it reports (`capabilities`: skills, subagents, hooks, and which tool
kinds it emits and with Claude-shaped input). A grader that needs more grades `n/a`, never `fail`
or a vacuous `pass`, and a case with nothing gradable on a driver is skipped before it runs.

As built (milestone 2), so v2 stays a superset of 1.1:

- The file is `case.mzeval.yaml`, so it can sit next to a 1.1 `case.yaml` during migration.
  `mzeval lint` and `mzeval schema` validate and export it.
- Every count is `min`/`max` (default `min: 1`). The 1.1 regex modes `contains`, `not_contains` and
  `count:N` become `{}`, `{min: 0, max: 0}` and `{min: N, max: N}`.
- `arm: both | with` keeps the 1.1 meaning. In a with/without-bundle run, `both` graders count in
  both arms and `with` graders are reported as indicators only. Skill graders and `expect` default
  to `with`, like 1.1's Skill exclusion; everything else defaults to `both`. `weight` is kept.
- `expect.skill.owner` is graded: the owner must fire. The importer sets it only where the 1.1 case
  asserted it, so migrated cases make the same assertions as before.
- `file_exists` globs over files the agent created during the run, as in 1.1.
- `file_changed` counts workspace files created, modified or deleted during the run (from the
  workspace diff), whatever tool changed them. The importer maps 1.1 `Write`/`Edit` path checks to
  it: the spike showed agents writing the same file through `Bash` or a script, which a tool-name
  check misses ([spike note](SPIKE-coder-eval.md)).
- Also carried over from 1.1: `runs` (default 3), `max_turns` (10), `timeout_s` (300), `model`,
  `append_system_prompt`, `env`, `history_file`, `add_dirs`, judge `focus` and `baseline`.
  Not yet: the `mock_calls` target, which arrives with MCP mocks (section 6, Later), and the
  `prompt.md` layout, which none of the 534 cases use.

### 4.5 LLM judge

Defaults come from the research (Anthropic and OpenAI eval guides, MT-Bench bias work, Thakur et
al. on kappa):

- **Binary checklist.** One verdict per criterion: `pass | fail | unknown`. The judge writes its
  reasoning before the verdict and must give an `evidence` quote.
- **Evidence must be a real substring** of the transcript or the output files, at least 8
  characters long (whitespace aside), or the verdict is rejected and retried twice. This is a
  cheap anti-hallucination check.
- **Cross-vendor by default.** Judge Claude runs with a GPT-family judge and the reverse, to avoid
  self-preference bias.
- **Cheap judge first, escalate on `unknown` or disagreement** to a 3-judge majority vote. Until
  escalation exists, an `unknown` grades `n/a`: never `pass`, and not a failure of the agent.
- **Cached and versioned.** The cache key is (trace hash, criterion, judge model, judge prompt hash).
  `mzeval rejudge <run> <cases>` regrades stored traces without rerunning agents; a judge that reads
  files grades `n/a` there, since the trial's workspace is gone. Trend charts mark
  judge-version changes.
- **Calibration.** `mzeval judge calibrate labels.jsonl` reports Cohen's κ and a confusion matrix.
  Ship a judge criterion at κ ≥ 0.6; that formalises what the video repo already does by hand for
  its drama and physical judges.

**Built on PydanticAI** (mandatory dependency; 2.68.0, MIT, v2 stable since 2026-06-23). Each judge
call is one PydanticAI `Agent` with a typed output:

```python
class Verdict(BaseModel):
    reasoning: str  # first, so the model argues before deciding
    verdict: Literal["pass", "fail", "unknown"]
    evidence_quote: str


judge = Agent(model, output_type=Verdict, deps_type=JudgeContext, retries={"output": 2})


@judge.output_validator
def evidence_is_real(ctx: RunContext[JudgeContext], v: Verdict) -> Verdict:
    if v.verdict != "unknown" and v.evidence_quote not in ctx.deps.transcript:
        raise ModelRetry(f"Quote not found in the transcript: {v.evidence_quote[:80]!r}")
    return v
```

| Need                                   | Comes from PydanticAI                                                                           | We write                                                                   |
| -------------------------------------- | ----------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| Typed verdicts                         | `output_type`; tool output by default, native JSON schema where the provider supports it        | the flat `Verdict` schema (Anthropic strict mode limits schema complexity) |
| Evidence check                         | `output_validator` + `ModelRetry` (the default retry budget is 1, so set 2)                     | the substring check, against transcript and output files                   |
| Cross-vendor models                    | providers for Anthropic, OpenAI, Google, Bedrock, OpenRouter, Ollama, any OpenAI-compatible API | the "never the same vendor as the agent" rule                              |
| Outages                                | `FallbackModel` (API errors only, not bad output)                                               | —                                                                          |
| Rate limits                            | `ConcurrencyLimitedModel` with one limiter shared across judges                                 | per-vendor limits from config                                              |
| Spend                                  | `RunUsage.cost` in USD (None when the price is unknown), `UsageLimits.cost_limit`               | judge rows in the spend ledger; unknown shown as "unavailable", never $0   |
| Cheaper repeats                        | prompt caching for the shared rubric block (Anthropic)                                          | —                                                                          |
| Tracing                                | OpenTelemetry spans with GenAI conventions; Logfire is optional and off                         | sending judge spans to our own OTLP endpoint when wanted                   |
| Escalation, voting, cache, calibration | —                                                                                               | all of it                                                                  |

**Judge credentials: API keys only.** Judges read keys from the environment (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `OPENROUTER_API_KEY`); other PydanticAI providers work the same way if needed.
Subscription logins (the Codex `auth.json`, `claude -p`) are not used for judging.

| Agent's model vendor                              | Default judge                                                      |
| ------------------------------------------------- | ------------------------------------------------------------------ |
| Anthropic (Claude Code, or OpenCode/Pi on Claude) | `openai:` GPT model, or a non-Anthropic model via `openrouter:`    |
| OpenAI (Codex, or OpenCode/Pi on GPT)             | `anthropic:` Claude model, or an Anthropic model via `openrouter:` |
| Anything else                                     | any judge from a different vendor                                  |

The vendor rule compares the judge model's vendor with the model the agent ran on, not with the
CLI; for OpenRouter the vendor is the model prefix (`openai/…`, `anthropic/…`). If no key for a
different vendor is set, the run stops before any agent starts and names the missing key, unless the
suite names its judge explicitly. Keys come from the shell locally and from CI secrets in CI. The
server holds judge keys only once it grades (phase C), and never agent credentials.

**Pinning.** PydanticAI ships two to three minor releases a week, each with compatibility notes. Pin
the exact version (`pydantic-ai-slim[anthropic,openai,openrouter]`) and keep all PydanticAI imports
inside `mozg_eval.judge`, so an upgrade touches one package.

**pydantic-evals** (the evals package in the same repo) is general-purpose: datasets, evaluators,
repeats and a console report, with no coding-agent or skill support and no confidence intervals or
pass@k. Its `LLMJudge` has no `unknown` verdict, no evidence quotes and no voting. We don't build
on it: our case format, event model and statistics are the core of the product, and it pins
`pydantic-ai-slim` to the exact same version, which would couple our upgrades. A later importer for
its YAML datasets is cheap if anyone asks.

### 4.6 Statistics (small n, done honestly)

- **Unit of analysis is the case**, not the run. Repeats of one case aren't independent: the
  existing data shows 100 of 108 case-rounds were unanimous.
- **Per case:** pass@1 (mean), pass^k (all runs pass; the reliability metric), and a flaky flag
  when 0 < passes < runs.
- **Per suite:** pass rate with a **Wilson 95% CI**. At this n, normal-approximation error bars are
  too narrow (Bowyer et al., ICML 2025).
- **Routing:** per-skill precision, recall and F1, near-miss false-trigger rate reported separately
  from easy negatives, and a skill × skill confusion matrix.
- **Comparisons** (with vs without plugin, commit A vs B, driver A vs B): paired per case.
  - One run per case: exact McNemar.
  - Several runs per case: paired bootstrap over cases.
- **Threshold:** a gate is "CI lower bound ≥ X", replacing "every run passes". Exit codes stay the
  same as `claude plugin eval` (0 / 1 / 2 / 130), so it slots into Makefiles.

As built (milestone 13): a trial's score is the weighted share of its applicable grades that
passed, 0 for an errored trial; in an ablation run only `arm: both` graders count, as in 1.1's
`meanDelta`. The bootstrap resamples cases, then each case's runs in both arms, so a one-case
comparison still gets an interval. Exact McNemar treats a trial as passed when its score is 1.
`run --ablation` skips the without arm for a case with no bundle or no `arm: both` grader.

### 4.7 Storage layer: pluggable backends, tenant-ready

Three stores sit between the core and any storage. Nothing above them writes SQL, builds paths or
calls a bucket API. Each store has several **storage backends**, chosen by URL. They are called
backends here so they don't clash with the agent drivers in 4.1.

| Store           | Holds                                                                        | Backends                       | Phase     |
| --------------- | ---------------------------------------------------------------------------- | ------------------------------ | --------- |
| `ResultStore`   | runs, trials, grades, judge cache, spend ledger, tokens: what the UI queries | SQLite → Postgres              | A → C     |
| `EventStore`    | canonical events per trial, append-only                                      | disk (JSONL) → S3 → ClickHouse | A → later |
| `ArtifactStore` | raw vendor streams, workspace diffs, output files, HTML reports              | disk → S3                      | A → later |

```toml
# mzeval.toml (env vars override: MZEVAL_RESULTS, MZEVAL_EVENTS, MZEVAL_ARTIFACTS)
[storage]
results   = "sqlite:///data/mzeval.db"     # postgresql+psycopg://user@host/mzeval
events    = "file:///data/events"          # s3://bucket/mzeval/events | clickhouse://host:8123/mzeval
artifacts = "file:///data/artifacts"       # s3://bucket/mzeval/artifacts
```

```python
class EventStore(Protocol):
    async def append(self, ws: str, run: str, trial: str, events: Sequence[Event]) -> None: ...
    def read(self, ws: str, run: str, trial: str, after_seq: int = 0) -> AsyncIterator[Event]: ...
    async def delete_run(self, ws: str, run: str) -> None: ...  # retention


class ArtifactStore(Protocol):
    async def put(self, ws: str, ref: ArtifactRef, data: AsyncIterator[bytes]) -> None: ...
    def get(self, ws: str, ref: ArtifactRef) -> AsyncIterator[bytes]: ...
```

Design rules for the backends:

- **`EventStore` takes typed events, not bytes.** ClickHouse stores rows, not files, so the
  interface can't be a byte stream.
- **`append` is idempotent on `(trial, seq)`** in every backend, because a resumed push resends the
  tail. The contract tests check it.
- **Per-backend layout:**
  - disk: `{workspace}/{run}/{trial}/events.jsonl`, appended in place;
  - S3: objects can't be appended to, so events are written as chunks
    (`events-{first_seq:08d}.jsonl`), flushed every N events or T seconds, and read back in order;
  - ClickHouse: one `events` table, `ReplacingMergeTree` ordered by
    `(workspace_id, run_id, trial_id, seq)`, payload as a JSON column. This is the backend for
    analytics across many runs, such as "which tool calls come before a wrong skill pick".
- **The interface stays minimal.** Cross-run event analytics run as SQL directly on ClickHouse;
  they don't go into `EventStore` until a second backend needs them.
- **Third-party backends** register through the Python entry points `mozg_eval.event_stores` and
  `mozg_eval.artifact_stores`, so a new store needs no change to mozg-eval.
- **SQL for `ResultStore` goes through SQLAlchemy Core** (not the ORM), with **Alembic** migrations.
  Postgres is then mostly a new connection URL plus whatever dialect differences the tests catch.
- **One contract test suite per store** runs against every backend: disk and SQLite in every CI
  build; S3 (MinIO), ClickHouse and Postgres in throwaway containers when we add them.
- **Multi-tenant from the first migration**, used as single-user:
  - every table carries `workspace_id`, and every unique key and index starts with it;
  - every store method takes the workspace explicitly, and there are no cross-workspace queries;
  - IDs are UUIDv7 (sortable, no autoincrement clashes when data moves between databases);
  - a default workspace `local` and one API token are created on first start.
- **When to switch:**
  - SQLite in WAL mode is fine for one user and one server process. Postgres becomes necessary with
    several concurrent writers (many CI shards pushing at once) or more than one server
    replica.
  - Disk events and artifacts move to S3 when the server runs on more than one host or outgrows
    its volume. ClickHouse is worth adding once you want to query events across runs.

Core tables: `workspaces`, `api_tokens`, `projects` (a plugin repo), `suites`, `cases`
(`content_hash`), `runs` (git SHA, trigger, connector, versions), `trials` (case × arm × repeat ×
driver, blob ref), `grades` (verdict, evidence), `judge_cache`, `spend_ledger`.

### 4.8 Trends: what makes a series comparable

A score over time only means something if each point records exactly what ran. Every trial stores
its **series identity**:

- `case_hash` (prompt plus graders) and `suite_version`;
- a **skill lock**: name, SHA-256 and git commit of every skill or plugin loaded (borrowed from
  Harbor);
- driver name and CLI version, and the model;
- judge model and judge prompt hash.

When any part changes, the UI draws a **break marker** on the series rather than joining the line.
Each point shows its Wilson band. A point is flagged as a **regression only when a paired test
against the baseline is significant**, so n = 3 noise doesn't page anyone.

### 4.9 Server and UI

`mzeval serve` ships as a Docker image. It stores and shows results; it doesn't run agents.

- **Ingest API:** results pushed by the CLI, events from sources (4.1), and OTLP.
- **Query API:** the same data the UI uses, so scripts and CI can read trends.
- **Auth:** one bearer token per workspace (one workspace for now).
- **UI views:**
  - trends per suite, case, skill and driver, with Wilson bands, break markers and regression flags;
  - run comparison: a paired-Δ forest plot and the case heatmap;
  - routing: per-skill precision/recall and the confusion matrix over time;
  - trial drill-down: the event log as a timeline, the judge's verdicts with evidence highlighted,
    and the workspace diff;
  - spend per day, suite and driver.
- **UI technology:** server-rendered Jinja2 pages with HTMX, and charts as Vega-Lite specs. The static
  HTML report uses the **same Vega-Lite specs**, so each chart is defined once.

Decided: HTMX + Vega-Lite, one Python codebase with no Node toolchain. HTMX covers filters and
drill-down, and Vega-Lite does brushing. Revisit only if the UI needs real client-side state.

### 4.10 CLI and CI

| Command                                                    | Purpose                                                                                                     |
| ---------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `mzeval run <suite> [--driver ...] [--push] [--run-id ID]` | run locally; exit 0 / 1 / 2 / 130; `--junit FILE`; `--run-id` attaches to a run the server queued (phase C) |
| `mzeval plan <suite>`                                      | validate cases and estimate cost from past runs, no tokens spent                                            |
| `mzeval compare <run> [<baseline-run>]`                    | paired per-case score delta with its CI: a run's with/without arms, or a run against a baseline run         |
| `mzeval push <run-id>`                                     | upload a stored run (retry after a failed `--push`)                                                         |
| `mzeval ingest <file> --format ...`                        | import external traces as a source run                                                                      |
| `mzeval rejudge / renormalize <run>`                       | regrade or re-parse stored runs at no agent cost                                                            |
| `mzeval serve`                                             | the server (also the Docker entrypoint)                                                                     |
| `mzeval remote run <suite>`                                | ask the server to dispatch the suite's CI workflow, same as the Run button (phase C)                        |

Server URL and token come from `MZEVAL_SERVER` and `MZEVAL_TOKEN`. In CI the eval result alone
decides the exit code. A failed push is reported, the run dir keeps everything, and `mzeval push`
retries later. A published GitHub Action wraps `run` with a pass-rate threshold and JUnit upload.

As built (milestone 14):

- JUnit has one testcase per case, so a reporter agrees with the exit code; a skipped case keeps
  its reason. In an ablation run only the with-bundle arm is reported.
- `--push` and `mzeval push` use four idempotent calls, so a resend is safe:
  `PUT /v1/runs/{run}`, `PUT /v1/runs/{run}/trials/{trial}` (the trial with its grades and judge
  spend), `POST …/trials/{trial}/events` (the server skips a resent `(trial, seq)`) and
  `PUT …/trials/{trial}/artifacts/{name}`. The HTTP client is `httpx2`, which the judge SDKs
  already bring in, not `httpx`.
- The action is `action.yml` at the repo root (`uses: DoctorMozg/mozg-eval-pipelines@v0`). It
  writes the output to the job summary, which needs no `checks: write` and so also works on PRs
  from forks, and uploads the JUnit file. Its self-test replays a recorded run through the fake
  `claude`, then seeds a regression and asserts that the action fails.
- Release binaries come from `scripts/build-pyapp.sh`: PyApp 0.31.0 embeds a python-build-standalone
  3.13 that already holds the wheel and its locked dependencies (`PYAPP_SKIP_INSTALL`), so the
  binary installs nothing and needs no network. Linux, macOS (arm64 and x86_64) and Windows build
  on native runners. The Linux binary is built on Ubuntu 22.04 and needs glibc 2.35 or newer. The
  macOS binaries are unsigned, so a downloaded one needs `xattr -d com.apple.quarantine`.

### 4.11 Runner: a thin executor, run locally or in CI

The runner's only job is to **execute trials and emit their results in the canonical format**. It
never needs a database or the UI. The server never holds agent credentials and never executes
agents.

```
                 ┌──────────────────────────── runner ─────────────────────────┐
 suite, cases,   │ isolate ─► driver ─► events ─► post-run checks in workspace │──► events + artifacts
 drivers ────────┤            (stream)             (command, file_exists, diff)│    (canonical format)
                 └─────────────────────────────────────────────────────────────┘
```

**What runs where.** Only checks that need the live workspace run on the runner: `command`,
`file_exists` and the workspace diff. They run right after `end` and are emitted as `check_result`
and `file_change` events. Everything else is a pure function over the stored events: trace graders,
the judge and statistics. That function can run anywhere:

- **locally**, so the CLI can produce an exit code with no server, offline or in CI;
- **on the server**, so a new grader or judge version can be applied to old runs without rerunning
  the agents.

**One command, three places.** `mzeval run <suite> [--push]` runs, grades locally for the exit
code, writes everything to the run dir and optionally streams events, artifacts and grades to the
server. It is the same on a laptop, in a CI check and in a CI job the server started.

**Remote and scheduled runs go through CI, not a daemon of ours** (phase C):

```
 UI "Run" ──▶ server ──POST workflow_dispatch (run_id, suite, filter)──▶ GitHub Actions
   ▲            │ creates the run as "queued"                              (plugin repo)
   │            │                                                              │
   │            └◀── events stream to /v1/runs/{run_id}/events ◀── mzeval run --push --run-id …
   └── trends, drill-down                                             hosted or self-hosted runner
 schedules: `on: schedule` in the plugin repo's workflow, versioned with the cases
```

- CI already has checkout, secrets, logs, retries, concurrency and runner pools, and the cases live
  in the plugin repos, so the workflow runs where the cases are.
- The server holds one fine-grained GitHub token (`actions: write` on the plugin repos) and no agent
  credentials.
- A run that needs your own machine (local tools, subscription logins) uses a GitHub self-hosted
  runner, which is already a pull-based runner with outbound HTTPS only. Bind it to
  `workflow_dispatch` and `schedule` workflows only, never `pull_request`, because the repos are
  public and a fork's PR would otherwise run on your machine.
- Large suites shard across a job matrix (hosted jobs stop at 6 hours); the shards push to the same
  `run_id` and the server joins them.
- GitHub may start cron runs late and disables scheduled workflows after 60 days without activity
  in a public repo, so the trends UI flags a series whose expected point is missing.
- Agent auth in CI (checked at milestone 14): Claude Code reads `ANTHROPIC_API_KEY` or a
  `claude setup-token` token in `CLAUDE_CODE_OAUTH_TOKEN` with no login on disk, and the driver
  passes both through. When both are set the API key wins, so a judge key in the same env bills
  the agent to the API. Codex reads `CODEX_API_KEY`. Anthropic's terms point automated use at API
  keys, so CI should use one.

A pull daemon of our own (`mzeval agent`) is deferred until a host outside GitHub Actions needs it;
a GitLab pipeline trigger would be a second dispatch adapter of the same size.

### 4.12 Isolation

The research found that a config-dir variable alone leaks the user's skills: Codex, Pi and OpenCode
also scan `~/.agents/skills`, and OpenCode scans `~/.claude/skills`. Every driver trial therefore
gets:

- a temp `HOME` and the vendor's config dir (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
  `PI_CODING_AGENT_DIR`, `XDG_*` plus `--standalone` for OpenCode);
- a git-initialised workspace where `fixture.sh` runs, with the eval directory hidden from the agent;
- credentials linked in and removed afterwards;
- a hash freeze of the bundle and the case, so the run aborts if either changes mid-run.

Pi and OpenCode have no sandbox at all. Shell-granted cases for them run with the **Docker
isolation** option, not on the host. Until that option lands (section 6, Later), the runner skips
those cases with a reason instead of running them on the host.

Pi loads skills with its `read` tool, so a case that grants only `skill` also lets the agent read
any file on Pi. Routing results on Pi can differ from Claude Code for that reason alone.

**Known limitation: running mzeval from inside Claude Code in auto mode** (found 2026-11-14). The
Claude driver starts `claude -p --permission-mode=bypassPermissions`, because a headless session
can't answer permission prompts. Claude Code's auto-mode classifier lets that through when the case
offers only `Skill`, but blocks it once `Bash` is in the tool set. So an agent session in auto mode
can run routing cases and not shell-granted ones. Run those from a plain terminal or in CI, or add
a permission rule for `mzeval run`.

## 5. Tech stack

| Concern                  | Choice                                                                                                                                                                      | Why                                                                                                                                     |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| Language                 | **Python 3.13, uv, hatchling**                                                                                                                                              | Both plugin repos are Python and uv; the existing Codex runner, graders and mutants port directly; best stats ecosystem                 |
| Schemas                  | **Pydantic v2** (`extra="forbid"`), mandatory                                                                                                                               | Strict case spec and event model, which removes the bespoke pytest linters; JSON Schema export for editors and for stdio driver authors |
| LLM judge                | **PydanticAI**, mandatory, exact pin (`pydantic-ai-slim[anthropic,openai,openrouter]`)                                                                                      | Typed verdicts with validator retries, Anthropic, OpenAI and OpenRouter by API key, fallback, concurrency limits, USD cost, OTel (4.5)  |
| CLI                      | **Typer**                                                                                                                                                                   | Commands in 4.10                                                                                                                        |
| Concurrency              | **asyncio subprocesses**                                                                                                                                                    | One trial is one CLI child process; per-driver rate limits and global `-j`                                                              |
| Storage                  | **SQLAlchemy Core + Alembic** behind `ResultStore`; disk `EventStore` and `ArtifactStore`                                                                                   | SQLite and disk now; Postgres, S3 and ClickHouse by URL (4.7); JSONL event logs stay diffable and greppable                             |
| Server                   | **FastAPI + uvicorn**                                                                                                                                                       | Typed ingest and query API from the same Pydantic models; OTLP/HTTP endpoint                                                            |
| UI                       | **Jinja2 + HTMX + Vega-Lite** (vendored JS)                                                                                                                                 | Same chart specs as the static report; no Node toolchain                                                                                |
| HTTP client              | **httpx**                                                                                                                                                                   | `--push`, `remote run`, and `workflow_dispatch` from the server                                                                         |
| Stats                    | Hand-written (~200 LOC: Wilson, pass@k/pass^k, McNemar, bootstrap); `numpy` only                                                                                            | Avoids pulling all of Inspect for five formulas                                                                                         |
| Packaging                | Python wheel (`uvx mozg-eval`, `uv tool install mozg-eval`), **PyApp single-file binaries** per OS and arch, **Docker image** (`mzeval serve` entrypoint, data on a volume) | One codebase for CLI and server; machines without Python or uv get one downloadable file                                                |
| Optional backend (later) | **Harbor** runner                                                                                                                                                           | Containers at scale and cloud sandboxes; ATIF in and out                                                                                |
| Quality                  | ruff, mypy (strict), pytest, pre-commit                                                                                                                                     | Same as the sibling repos                                                                                                               |

Approved dependencies: pydantic, pydantic-ai-slim, typer, jinja2, numpy, pyyaml, sqlalchemy,
alembic, fastapi, uvicorn, httpx. The server pieces (fastapi, uvicorn) go in a `[server]` extra so
CI installs stay small. The S3 and ClickHouse client libraries need a separate OK when those
backends are built.

**Two views on the language choice:**

- **Perfectionist:** all four agents ship TypeScript SDKs (Claude Agent SDK, `@openai/codex-sdk`, the
  Pi SDK, `@opencode/sdk`). A TS core could drive them in-process, with typed events and no JSONL
  parsing.
- **Pragmatist:** SDKs drift from what users actually run (the CLI), and the main claim is "it
  triggers in the real CLI". Subprocess plus JSONL is the common denominator, the stdio driver
  protocol lets TS adapters plug in anyway, and the existing ~2.5k lines of Python port directly.

Decided: Python with subprocess drivers. A Rust CLI was considered for portability and rejected:
the CLI must run the PydanticAI judge for the offline exit code, so it needs Python anyway.
Portability comes from distribution instead. PyApp is a small Rust launcher built per target
(through Hatch's `binary` builder); it embeds the wheel (`PYAPP_PROJECT_PATH`) and can embed the
Python distribution, so with `PYAPP_SKIP_INSTALL` the binary makes no network calls at runtime.

## 6. Scope and phases

**Phase A, runner (MVP):**

- Spec v2 with lint, and importers from `claude plugin eval` 1.1 and skill-creator `evals.json`.
- Event model, the stdio driver protocol, the streaming run log and guards.
- Storage layer with the SQLite implementation and the contract tests.
- Drivers for Claude Code and Codex first, then OpenCode V2 (pinned version, because its JSON
  stream is still changing; built against `anomalyco/opencode` 2.3.0), then Pi (built against
  `@earendil-works/pi-coding-agent` 1.5.2).
- Local runner with full isolation, fixtures and hash-freeze.
- Deterministic graders, the LLM judge on PydanticAI (API keys: Anthropic, OpenAI, OpenRouter), the cache, `rejudge` and `renormalize`.
- Stats from 4.6, the static HTML report with local trends, `plan` and the spend ledger.
- Ablation (with and without the bundle), paired comparisons, and the routing confusion matrix.
- CI: JUnit, the GitHub Action, `--push`.

**Phase B, server:** `mzeval serve` in Docker, the ingest and query API, token auth, and the trends,
compare, routing and drill-down views.

**Phase C, sources and remote runs:** OTLP, JSONL and file sources with observe suites;
server-side grading, the Run button and `remote run` through `workflow_dispatch`, and a reusable
workflow with scheduled runs; the Postgres `ResultStore` when concurrent
writers need it; S3 and ClickHouse storage backends when the disk backend stops being enough.

**Later:** our own runner daemon (`mzeval agent`) if a host outside GitHub Actions needs one, Docker
isolation for Pi and OpenCode shell cases (pulled forward if those cases are needed in phase A),
Harbor backend, mutation testing (ports `mutants.py`), judge calibration UI, pairwise A/B judge, MCP
mocks, multi-turn history replay, description optimisation with a train/test split, ATIF and
OpenInference export, and real multi-tenancy (sign-up, roles, quotas).

**Out:** hosted SaaS, plugin authoring and per-vendor rendering (plugin repos own that and hand us a
render command), and general LLM-app metric libraries (DeepEval's and promptfoo's job; sources
cover grading agent apps).

## 7. Migration of existing repos

`mzeval import claude-plugin-eval <dir>` writes v2 cases next to the old ones and a migration
report. The old files stay until you delete them.

| 1.1 pattern                                         | v2 result                                                                                                                                 | Count (pipelines / video) |
| --------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- | ------------------------- |
| `tool_used Skill` with `input_match` `"skill":"…X"` | `expect.skill: X` (or `not: X` when `max: 0`)                                                                                             | 401 / 241                 |
| `tool_used Agent` `subagent_type`                   | `subagent: X`                                                                                                                             | 53 / 0                    |
| `tool_used Write/Edit` + `file_path` regex          | `file_changed{path_match}` (`Read` and `Bash` stay `tool_called`)                                                                         | 90 / 0                    |
| `regex` on the last message or a file               | unchanged                                                                                                                                 | ~220 / 4                  |
| `regex target: trace`                               | rewritten by shape to `hook_fired`, `regex{replies\|tool_results}` or `file_changed`, flagged; the rest stays `raw_regex{driver: claude}` | 97 + 31 kept / 0          |
| `llm criteria`                                      | `judge.checklist` (one item per sentence, reviewed by hand)                                                                               | 0 / 4                     |
| `context.scaffold_script`                           | `fixture:` (scripts unchanged)                                                                                                            | 69 / 0                    |
| `negative` description "belongs to X"               | `expect.skill.owner: X`                                                                                                                   | — / 57                    |
| `allowed_tools`                                     | canonical `tools:`                                                                                                                        | all                       |

Acceptance check: run the imported suite on Claude and compare per-case pass rates with the last
`claude plugin eval` result. Any case outside the noise band is a migration bug until proven
otherwise.

## 8. Milestones

Each milestone stays at or under 10 files and 400 changed lines, except mechanical case imports.

**Phase A**

01. **Skeleton:** package, CLI stub, pre-commit, CI. → verify: `uv run mzeval --help`, `pytest` green.
02. **Spec v2 and lint:** Pydantic models, loader, JSON Schema. → verify: lint passes on hand-written
    examples and rejects an unknown key with file:line.
03. **Importer:** 1.1 → v2 with a migration report. → verify: imports all 534 cases; the report
    lists every flagged grader; a round-trip test on fixtures.
04. **coder-eval spike: done 2026-11-14.** Verdict: build mozg-eval; borrow coder-eval's Codex skill
    install. Findings and the changes below are in [SPIKE-coder-eval.md](SPIKE-coder-eval.md).
05. **Event model, driver protocol, run log and guards.** The `start` event records every resolved
    setting with the layer that set it (case, suite, config file, CLI flag), as coder-eval's config
    lineage does. → verify: a fake stdio driver (a 30-line test script) streams events into
    `events.jsonl`; the cost guard cancels it mid-run; replaying the log grades identically to the
    live run.
06. **Storage layer:** `ResultStore`, `EventStore` and `ArtifactStore`; SQLite and disk backends;
    URL-based config; first Alembic migration; contract tests. → verify: contract suites green on
    SQLite and disk; appending the same `(trial, seq)` twice stores it once; a test proves
    workspace B can't read workspace A's runs.
07. **Claude driver and isolation.** A case's `tools` are the only tools the session gets (`--tools`),
    not an auto-approve list: in the spike, giving Claude every tool dropped 6 of 16 routing cases.
    → verify: replay recorded `stream-json` fixtures (free); one paid smoke run of 5 cases; no file
    outside the temp HOME is read from `~/.claude`; a `tools: [skill]` case offers no other tool.
08. **Deterministic graders, stats, static report with local trends.** → verify: unit tests on
    recorded traces; the HTML renders the heatmap, Wilson bars and a trend from two stored runs.
09. **Parity gate:** run the imported mz-memory and publish suites on Claude. → verify: per-case
    pass rates agree with the last `claude plugin eval` JSON within the CI.
10. **Codex driver:** each Claude plugin is rendered as a native Codex plugin (`.codex-plugin/`) in a
    per-trial local marketplace and installed into a temp `CODEX_HOME`, so skills and hooks run
    natively; the hook bridge is gone. Each hook command runs through a capture wrapper that logs it,
    since `codex exec --json` records no hooks. A command that reads an installed `SKILL.md` is
    emitted as `skill_activated` with `evidence: inferred`. Codex has no turn cap, so the driver stops
    once messages or tool calls exceed `max_turns`, and ends that trial `ok`. → verify: the
    16 spike routing cases that the current runner marks unsupported are graded; the
    hook-dependent spike case (inject-recent-decision) passes; the "unsupported" share drops from
    66% to the honestly vendor-specific remainder (~6%).
11. **LLM judge on PydanticAI:** `Verdict` agent, evidence validator, cache, `rejudge`. → verify:
    the 4 publish traps regrade from stored traces at $0 agent cost; a test model that returns a
    made-up quote is retried and ends as `unknown`, never `pass`.
12. **OpenCode V2 driver, then Pi driver.** → verify: recorded-stream fixtures plus a 5-case paid
    smoke run each.
13. **Ablation, paired comparisons, routing confusion matrix, `plan` and the spend ledger.**
    → verify: a with/without run on mz-knowledge reproduces the known Δ≈0.67 within the CI.
14. **CI and release:** JUnit, GitHub Action, `--push` client (against a stub server), PyApp
    binaries for Linux, macOS and Windows in the release workflow, agent auth in CI checked. → verify:
    the Action fails a PR with a seeded regression and passes after the revert; the Linux binary
    runs `mzeval --version` in a container with no Python and no network.
15. **Cut over both repos:** Makefile targets call `mzeval`; delete `tests/eval_tools/` and the
    per-plugin eval linters. → verify: `make eval PLUGIN=…` works in both repos; nothing references
    the removed modules.

**Phase B**

16. **Server skeleton:** `serve`, ingest and query API, token auth, Dockerfile. → verify:
    `docker run` with a volume, `mzeval run --push` from the host, the run is returned by the query
    API, and it survives a container restart.
17. **Trends UI:** series identity, Wilson bands, break markers, regression flags. → verify: two
    pushed runs on different commits plus one edited case show as one series with a break marker;
    a seeded significant drop is flagged and an n = 3 wobble is not.
18. **Compare, routing and drill-down views.** → verify: a with/without run shows the paired-Δ
    plot; a trial page shows events, judge evidence and the workspace diff.

**Phase C**

19. **Sources:** OTLP/HTTP, JSONL push, file import, observe suites. → verify: an OTel GenAI trace
    fixture from a sample LangGraph app arrives as a graded run in the UI.
20. **Server-side grading and CI dispatch:** Run button and `remote run` via `workflow_dispatch`,
    a reusable workflow with `on: schedule`, sharded runs joined by `run_id`. → verify: the Run
    button starts a workflow whose two shards push into one run; the server's grades match a local
    `mzeval run` of the same trials; the server's config and database hold no agent credentials.
21. **Postgres `ResultStore`** (when concurrent writers need it). → verify: the contract suite
    passes on Postgres in a throwaway container; `mzeval db migrate` moves a SQLite database across
    with identical query results.
22. **S3 event and artifact backends, then ClickHouse events** (when needed). → verify: the
    contract suites pass on MinIO and ClickHouse containers; a run pushed with `events = s3://…`
    replays and grades identically to the disk backend.

## 9. Decisions and open questions

**Decided (2026-11-13):**

- Connectors cover **both** other coding CLIs (as drivers) and agent apps (as sources).
- **Single-user** server for now, with a tenant-ready schema; SQLite behind a storage layer so
  Postgres is a configuration change plus a migration.
- The server **stores and shows**. The runner is a thin executor that emits canonical events and
  artifacts with `mzeval run [--push]`, on a laptop or in CI.
- Event logs and artifacts sit behind **pluggable storage backends**: disk first, then S3 and
  ClickHouse.
- **Pydantic and PydanticAI are mandatory**; the rest of the dependency list in section 5 is
  approved.
- UI: **HTMX + Vega-Lite**.
- Isolation is **local-only first**; Docker isolation for Pi and OpenCode shell cases comes later.
- Judges use **API keys only** (Anthropic, OpenAI, OpenRouter), never subscription logins.
- **MIT** licence, PyPI package `mozg-eval`, CLI `mzeval`.
- Cases **stay in each plugin repo**; mozg-eval is the tool, not the case store.
- Remote and scheduled runs go **through CI** (`workflow_dispatch`, `on: schedule`); no runner
  daemon of our own for now (2026-11-14).
- **Python + PyApp:** the CLI stays Python and ships as a wheel, PyApp single-file binaries and a
  Docker image (2026-11-14).

**Open:** none.
