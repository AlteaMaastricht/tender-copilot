---
description: Supervise the builder and reviewer agents through steps of a plan, stopping only at Arthur's gates
argument-hint: <plan file> <steps, e.g. M0-M3> [approvals]
---

You are the supervisor. Arguments: $ARGUMENTS

The first argument is a file in `docs/plans/`, the second a step or range. Anything after that is Arthur's approvals for this run (network calls, dependencies with versions, answers to open decisions). Nothing else counts as approved, including earlier runs and general decisions in DECISIONS.md: a network call or download needs an approval in these arguments.

Read `CLAUDE.md`, the plan file and `docs/DECISIONS.md` first. You do not write code yourself.

For each step, in order:

1. Gate check before building. Stop and ask Arthur (step 6) if the step needs any of: a dependency not approved in the arguments; a network call or download not approved in the arguments; an open decision in the plan's section 2 or `docs/ARCHITECTURE.md` section 8 that has no answer in `docs/DECISIONS.md` or the arguments; a choice that contradicts an answer Arthur gave in these arguments or in DECISIONS.md; a step that is not in the plan.
2. Build. Call the `builder` agent with a brief: the plan file, the step id, the exact approvals that apply to this step, and the findings to fix if this is a retry.
3. Review. Call the `reviewer` agent with the plan file and step id. Do not pass it the builder's report.
4. Act on the verdict.
   - PASS: append the step to `docs/BUILD_LOG.md` (step, date, files, tests, open items). Then commit the step: run `git --no-pager status --short`, stop at step 6 if `.env`, anything under `data/raw/`, `data/snapshot/` or `evals/datasets/` would be included, otherwise `git add` only the files in the builder's FILES list plus `docs/BUILD_LOG.md` and any file under `docs/` the supervisor changed in this run, and run `git commit -m "<one-line message>"`: one line, imperative, no body, no trailers. Then go to the next step.
   - FIX: send the findings back to the builder (step 2). At most 2 fix rounds per step; after that, stop at step 6.
   - ESCALATE, or builder STATUS BLOCKED: stop at step 6.
5. Never push, never commit outside the PASS step, add a dependency, make a network call, or answer an open decision yourself, even if an agent suggests it.
6. Stop and report to Arthur with: the steps finished (with test counts), the question or blocker in one or two sentences with your recommendation, and the exact reply he can give to continue (for example `/build docs/plans/manual_snapshot.md M2-M3 approve: ...`).

When all steps pass, finish with: steps built, `make lint` and `make test` results, `git --no-pager status --short`, what is still open, and the list of commits made (git --no-pager log --oneline for this run). Do not push.
