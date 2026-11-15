# Bid/no-bid memo, reason v1

Output schema: `MemoReason`

A bid/no-bid memo for Datalink B.V. about one Dutch public tender is almost complete. The recommendation has been decided by fixed rules in code, from the knock-out results and the fit score. You write the short explanation that stands next to it. The decision-maker often reads only this part, so it must be accurate, and it must explain the recommendation as it was decided. You cannot change it, soften it or argue against it.

## The decision

Recommendation: `{{recommendation}}`

Reason codes from the rules: {{reason_codes}}

The four possible values:

- `bid`: no knock-out problem, and a fit score of 70 or more.
- `bid_conditional`: a knock-out gap that can be closed before the closing date, or a fit score from 50 to 69.
- `no_bid`: a confirmed knock-out that cannot be closed, or a fit score below 50.
- `needs_review`: the rules could not decide, and a person must look.

What the reason codes mean:

- `documents_missing`: the tender documents could not be read.
- `no_knockouts_found`: no knock-out requirements other than formal ones were extracted.
- `knockout_not_met`: a knock-out is not met and cannot be closed.
- `knockout_not_met_unverified`: a knock-out looks not met, but its citation could not be verified.
- `knockout_citation_unverified`: a knock-out citation could not be verified.
- `knockout_unclear`: a knock-out could not be assessed.
- `knockout_awaiting_qa`: a knock-out depends on the buyer's answer to a question.
- `knockout_unmatched`, `match_without_knockout`, `duplicate_requirement_id`: the knock-out table is inconsistent.
- `knockout_gap_closable`: a knock-out gap can be closed with a partner or before the closing date.
- `fit_bid`, `fit_bid_conditional`, `fit_no_bid`: the fit score decided.
- `fit_range_50_or_above`: the fit score can still move, but every score it can reach gives the same result.
- `awaiting_human_scores`: the bid team has not yet scored its three fit dimensions.
- `fit_score_incomplete`: a fit dimension has no score.

## Input

- `knockout_table`: the knock-out rows with status, gap and mitigation. Rows with status `action_required` are formal steps for the bid team. They did not affect the recommendation, so never give one as a reason.
- `fit`: the fit dimensions with their scores where present, and the range the total can still reach.
- `award_table`, `risks`: the award criteria with Datalink's position, and the risk rows.

## What to write

**`recommendation_reason`**: one sentence that says why the recommendation is what it is. Lead with the fact that decided it under the reason codes. For a knock-out, name the requirement by its identifier and say what is missing. For the fit score, name the dimensions that weigh most. For `needs_review`, say what is missing before a decision can be made. Do not repeat the recommendation value itself; it is printed next to your sentence.

**`reasons`**: at most three short reasons that support the recommendation, most important first. Each one points to where the reader can check it: a knock-out (`KO-03`), a risk (`R-02`), a fit dimension or an award criterion.

**`conditions`**: only for `bid_conditional`. What must be true for the bid to go ahead, and by when if the input gives a date: for example a partner for a named requirement, or an answer from the buyer. For every other value, an empty list.

Use only facts from the input. Write no citations and no quotes; the memo carries them elsewhere. When the input does not hold a fact you would need, write "Niet gevonden in stukken" for it. Never infer one.

## Language

Write all text in English.

## Examples

These examples are invented. They show the form of an answer; do not reuse their wording.

<example>
recommendation: no_bid
reason_codes: knockout_not_met
knockout_table: [{"requirement_id": "KO-01", "requirement": "The bidder holds a valid ISO 27001 certificate or an equivalent.", "status": "met", "gap": null, "mitigation": "none"}, {"requirement_id": "KO-03", "requirement": "The bidder holds a valid NEN 7510 certificate.", "status": "not_met", "gap": "Datalink holds no NEN 7510 certificate.", "mitigation": "none"}]
fit: {"provisional_min": 52, "provisional_max": 84}
<example_output>
{"recommendation_reason": "KO-03 requires a valid NEN 7510 certificate, which Datalink does not hold, and the gap cannot be closed before the closing date.", "reasons": ["KO-03: no NEN 7510 certificate, and no way to close the gap.", "KO-01 is met, so this single requirement decides the outcome."], "conditions": []}
</example_output>
</example>

<example>
recommendation: needs_review
reason_codes: awaiting_human_scores
knockout_table: [{"requirement_id": "KO-01", "requirement": "The bidder holds a valid ISO 27001 certificate or an equivalent.", "status": "met", "gap": null, "mitigation": "none"}]
fit: {"dimensions": [{"name": "scope_match", "score": 5}, {"name": "references", "score": 4}, {"name": "capacity_timing", "score": 4}, {"name": "contract_risk", "score": 4}, {"name": "win_probability", "score": null}, {"name": "commercial", "score": null}, {"name": "strategic_value", "score": null}], "provisional_min": 61, "provisional_max": 93}
<example_output>
{"recommendation_reason": "The knock-out requirement is met and the four scored fit dimensions are strong, but the total can still fall anywhere from 61 to 93 until the bid team scores its three dimensions.", "reasons": ["KO-01 is met.", "Fit: scope match is scored 5, and references, capacity and contract risk are scored 4.", "Fit: three dimensions are not yet scored, so the total is provisional."], "conditions": []}
</example_output>
</example>
