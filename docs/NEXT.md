# Next steps: Tender Copilot

Status at close of Day 2 (13 Nov 2026). Commit only when Arthur asks.

## Blocking (do first)

1. **Explain the 2 deadline mismatches.** Read `publicatieIDLaatsteRectificatie` for 409819 and 408259 from the stored detail data. If a rectification explains them, record it in `docs/DECISIONS.md`. If not, leave the cause as unknown.
2. **Decide on the full deadline check.** Re-check the API `sluitingsDatum` for all 150 selected publications using the existing probe functions: about 5 minutes at 2 s per request, no documents. Needs Arthur's approval before running.
3. **Decide on 431048.** It is selected but has no spreadsheet closing date. Keep it and take the deadline from the API, or replace it with the next draw in its CPV group and month.
4. **Move the snapshot script into the repo** as `scripts/snapshot_select.py`, so the selection can be reproduced. Needs Arthur's approval; the script currently sits outside the repo.

## Commit (after approval)

5. Done: parser commit `0bb36e4` is on `origin/main`. Commit the updated `docs/DECISIONS.md` (snapshot section) and this file next.
6. Snapshot files: commit `data/.gitignore`, `data/snapshot_selection.csv` and `data/snapshot_deadline_check.csv`. Delete `data/snapshot_candidates.csv` (it duplicates the selection file without the `selected` column). The raw `.psv` stays ignored.

## Security

7. Rotate the TenderNed password. Update `.env` locally only. Remove the plain-text credentials from the old `call.py`. Never paste a password into a chat.

## Open external input

8. TenderNed (functioneel beheer): reply on `public-xml` 403 and on automated download and storage terms (Q1). Record the reply in `docs/DECISIONS.md`.

## Docs and config

9. Update `CLAUDE.md`: deadline rule (API `sluitingsDatum`); stack additions (python-pptx, openpyxl, LibreOffice), after approval.
10. Update `docs/COMPANY.md`: NL only, Dutch and English, will-not-bid wording (decision S3).
11. Update `docs/PLAN.md` from section 9 of `docs/ARCHITECTURE.md`.
12. Confirm the 5 Claude Code architecture choices and the 3 "(confirm)" items in `docs/ARCHITECTURE.md`.

## Infrastructure

13. Install Docker Desktop and run `make up` locally.
14. Change the Dockerfile to `uv sync --frozen` from `uv.lock`. Add an image build to CI later.

## Time-bound

15. **Saturday 14 Nov:** TED probe and eForms fixtures (Day 2 step 5), using Fable 5.2 before the offer ends on Mon 17 Nov, 08:59 Amsterdam time.

## Later phases

- Phase 2: agent loop, schemas, LLM gateway, extraction, matching, memo, Streamlit UI, HubSpot.
- Phase 3: eval runner, 3-model comparison, one measured improvement.
- Week 3 option: `--repeats N` for `evals/run.py`, to measure run-to-run variation (the API rejects `temperature`). Run each case N times and report the spread per metric next to the Wilson interval, which covers sampling only. Not built (decision of 15 Nov 2026).
- Phase 4: deployment on one VM, README with GDPR and AI Act section, CV update, Sia application follow-up.
