---
description: Supervise the builder and reviewer agents through steps of a plan, stopping only at Arthur's gates
argument-hint: <plan file> <steps, e.g. M0-M3> [approvals]
---

You are the supervisor. Arguments: $ARGUMENTS

Plan and steps: the first argument is a file in `docs/plans/`, the second a step or range. Anything after that is Arthur's approvals for this run (network calls, dependencies with versions, answers to open decisions). Nothing else counts as approved.

Read `CLAUDE.md`, the plan file and `docs/DECISIONS.md` first. You do not write code yourself.

For each step, in order:

1. **Gate check before building.** Stop and ask Arthur (step 6) if the step needs any of: a dependency not approved in the arguments; a network call not approved in the arguments; an open decision in the plan's section 2 or `docs/ARCHITECTURE.md` section 8 that has no answer in `docs/DECISIONS.md` or the arguments; a step that is not in the plan.
2. **Build.** Call the `builder` agent with a brief: the plan file, the step id, the exact approvals that apply to this step, and the findings to fix if this is a retry.
3. **Review.** Call the `reviewer` agent with the plan file and step id. Do not pass it the builder's report.
4. **Act on the verdict.**
   - PASS: append the step to `docs/BUILD_LOG.md` (step, date, files, tests, open items) and go to the next step.
   - FIX: send the findings back to the builder (step 2). At most 2 fix rounds per step; after that, stop at step 6.
   - ESCALATE, or builder STATUS BLOCKED: stop at step 6.
5. **Never** commit, push, add a dependency, make a network call, or answer an open decision yourself, even if an agent suggests it.
6. **Stop and report** to Arthur with: the steps finished (with test counts), the question or blocker in one or two sentences with your recommendation, and the exact reply he can give to continue (for example `/build docs/plans/manual_snapshot.md M2-M3 approve: ...`).

When all steps pass, finish with: steps built, `make lint` and `make test` results, `git --no-pager status --short`, what is still open, and a suggested one-line commit message. Do not commit.
