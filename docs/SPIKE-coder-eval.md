# Spike: build mozg-eval or build on coder-eval?

Milestone 4, run 2026-11-14. **Verdict: build mozg-eval; don't build on or contribute to
coder-eval.** Borrow its Codex skill install and a few config ideas (end of this note).

## Setup

- coder-eval 0.12.18 (UiPath, Apache-2.0, commit `715fad7`), in a throwaway venv with telemetry
  off (`TELEMETRY_ENABLED=false`; it is on by default and reports to an Azure endpoint).
- 20 cases from claude-mozg-pipelines, picked with a fixed seed: 8 fire and 8 near-miss routing
  cases (mz-dev-pipe, mz-dev-git, mz-creative) and 4 workspace cases with fixtures (mz-memory,
  mz-dev-pipe, mz-dev-git). Ported to coder-eval tasks by a throwaway script.
- Claude Code on `claude-sonnet-5-5`, Codex on `gpt-6.1-sol`, 3 repeats each.
- References on the same cases: `claude plugin eval` (Sonnet, the repo's `make eval` settings) and
  the repo's own Codex runner.

## Results

Runs passed out of 3 (the two long cases ran once in the Claude reference):

| Case                                       | `claude plugin eval` | coder-eval Claude, all tools       | coder-eval Claude, Skill only | coder-eval Codex |
| ------------------------------------------ | -------------------- | ---------------------------------- | ----------------------------- | ---------------- |
| audit-fire-inherited-service               | 0/3                  | 0/3                                | 3/3                           | 3/3              |
| audit-fire-security-review                 | 0/3                  | 0/3                                | 1/3                           | 3/3              |
| optimize-fire-latency-slo                  | 2/3                  | 3/3                                | 3/3                           | 3/3              |
| optimize-fire-tokenizer-speedup            | 3/3                  | 1/3                                | 3/3                           | 3/3              |
| polish-near-miss-slow-script               | 0/3                  | 0/3                                | 3/3                           | 3/3              |
| debug-near-miss-repo-sweep                 | 0/3                  | 0/3                                | 1/3                           | 3/3              |
| optimize-near-miss-dead-helpers            | 3/3                  | 0/3                                | 0/3                           | 3/3              |
| audit-near-miss-cart-rounding              | 0/3                  | 0/3                                | 0/3                           | 3/3              |
| github-scan-prs-fire-morning-triage        | 3/3                  | 3/3                                | 3/3                           | 3/3              |
| github-review-pr-fire-signoff              | 3/3                  | 3/3                                | 3/3                           | 3/3              |
| github-scan-prs-near-miss-single-pr-url    | 3/3                  | 3/3                                | 3/3                           | 3/3              |
| github-review-pr-near-miss-unpushed-branch | 3/3                  | 0/3                                | 3/3                           | 3/3              |
| expert-fire-kafka-migration                | 2/3                  | 0/3                                | 3/3                           | 3/3              |
| copywrite-fire-investor-leave-behind       | 2/3                  | 3/3                                | 3/3                           | 3/3              |
| brainstorm-near-miss-rfc-review            | 3/3                  | 1/3                                | 3/3                           | 3/3              |
| expert-near-miss-docs-ideas                | 3/3                  | 0/3                                | 3/3                           | 3/3              |
| memory-note-gate-too-long                  | 3/3                  | 3/3                                | —                             | 3/3              |
| inject-recent-decision                     | 3/3                  | 3/3                                | —                             | 0/3              |
| optimize-gate-contract                     | 0/1                  | 3/3 (5 of 10 graders not portable) | —                             | 3/3 (same)       |
| review-branch-behavior-main-branch-stop    | 1/1                  | 0/3                                | —                             | 0/3              |

The repo's own Codex runner marked all 16 routing cases `unsupported`: it can't see skill use on
Codex. Spend: `claude plugin eval` $3.09; coder-eval Claude $8.89 (all tools) and $5.23 (Skill
only, 16 cases); coder-eval Codex $1.31.

## What coder-eval does well

- **Codex skill routing works.** It copies each skill into `<sandbox>/.agents/skills/` and counts a
  skill as fired when a tool call's parameters contain `skills/<name>/`. Every Codex run read the
  right `SKILL.md` (`cat .agents/skills/optimize/SKILL.md`), so it measured 16 cases our current
  runner can't. The evidence is weaker than Claude's `Skill` call: a wildcard `cat` over several
  skills would count them all.
- Dataset-backed routing suites with per-skill precision, recall and CI thresholds; a free `plan`
  command; `run_limits` budgets; and a per-field config lineage (`task_config.lineage` records which
  layer set each value).

## Why not build on it

1. **The harness changes the answer.** coder-eval passes `allowed_tools` as auto-approval, not as
   the tool set, so Claude gets every tool. With `Bash` available, Sonnet explored the empty
   workspace instead of calling the skill: 6 routing cases dropped against the reference (2 rose).
   Restricting tools takes a hand-written `disallowed_tools` list of every other built-in tool.
   A routing run also costs about twice as much ($0.11–0.13 against $0.057), with or without the
   restriction, because the session context is larger.
2. **Even with matching tools, 3 of 16 cases flip** between 0/3 and 3/3 against `claude plugin eval`
   on the same model and prompts (audit-fire-inherited-service, polish-near-miss-slow-script,
   optimize-near-miss-dead-helpers). The system prompt and CLI build differ. A routing number is
   only comparable within one harness, which our series identity (section 4.8) already requires.
3. **Codex drops plugin hooks and agents.** inject-recent-decision needs the mz-memory SessionStart
   hook; on Codex the hook never ran and the answer was "UNKNOWN" (0/3). The repo's runner bridges
   hooks, and so must our Codex driver.
4. **Tool-name graders are brittle across harnesses.** In review-branch both agents recorded the
   right state, but wrote it with `Bash` or Python instead of `Write`, so the ported 1.1 assertion
   failed. A check on the workspace (the file exists after the run) passes on both.
5. **Fidelity gaps in the grader set:** no regex over the final message (graded offline here), no
   trace-level checks (5 of optimize-gate-contract's 10 graders could not be ported).
6. **Operational issues:** it pins Codex 0.156.1, which can't run `gpt-6.1-sol` on a ChatGPT login,
   and has no setting for the Codex binary, so the spike overrode the pinned package. Codex shares
   the real `~/.codex` unless you set `CODEX_HOME` yourself. Telemetry is on by default.
7. **Out of scope for it:** a results server, trends, pluggable storage, external sources, judge
   calibration, confidence intervals and paired tests. Contributing those would mean replacing most
   of its architecture.

## Changes to our plan

- **Codex driver (milestone 10):** install skills into `.agents/skills/` like coder-eval, record the
  `skills/<name>/` read as `skill_activated` with `evidence: inferred`, and bridge plugin hooks as
  the repo's runner does.
- **Tools are the tool set, not an approval list.** The Claude driver must pass the case's `tools`
  as the available tools (`--tools`), or routing results won't match `claude plugin eval`.
- **Importer:** map 1.1 `Write`/`Edit` `file_path` assertions to `file_exists` on the created path
  where the pattern allows, instead of a tool-name check. Keep `tool_called` only when the tool
  itself is the point.
- **Run metadata:** borrow the config lineage idea: every resolved setting in a run records where it
  came from.
