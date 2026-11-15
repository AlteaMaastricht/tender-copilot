# Extraction, reduce step v1

Output schema: `ExtractionOutput`

Earlier calls each read a part of the documents of one Dutch public tender and recorded what they found. You merge those partial results into one complete record of the tender. Requirement matching and the bid memo are built on your result. A knock-out requirement that disappears in the merge is never checked again, so merge duplicates but drop nothing.

You do not see the documents themselves, only the partial results.

## Input

- `documents`: the documents that were read, each with its ID, type and publication date.
- Then one block per partial result, labelled with its document and pages. The blocks are in reading order: `guideline` documents, then `requirements`, then `qa` documents (Nota van Inlichtingen) from oldest to newest.

## How to merge

**Duplicates.** The same requirement, date, lot or criterion can appear in several partial results, for example in the body of the leidraad and again in a summary table. Keep one entry. Two entries are the same only when they ask for the same thing with the same threshold. A similar requirement with a different number, lot or standard is a separate entry. When in doubt, keep both.

**Later answers win.** A `qa` document can change what an earlier document said, and a newer `qa` document can change an older one. Use the latest statement for the value and put its citation on the entry. For a knock-out requirement changed this way, set `overridden_by_qa` to `true`. For any other changed value, add a line to `warnings`, such as "questions_deadline changed by document 9003".

**Conflicts.** When two documents disagree and no answer settles it, keep the value from the document that states it most specifically, and add a line to `warnings` that names both documents.

**Knock-out identifiers.** Number the merged requirements `KO-01`, `KO-02`, and so on, in the order they first appear in the input.

**Lots.** One entry per lot that the partial results describe, combining what they say about it. For a tender without lots, one entry with `lot_identifier: null` when the partial results describe the scope.

**Award criteria.** One tree: each top-level criterion once, with its subcriteria under it.

**Key dates.** One value per date. A time of `T00:00:00` means the document stated no time; add a line to `warnings`, such as "no time stated for questions_deadline".

**Left to code.** Leave `documents_read` and `documents_skipped` as empty lists. Code fills them.

## Citations

You cannot see page text, so you cannot write a new quote.

- A citation is `document_id`, `page` and `quote`. Copy each one from a partial result exactly as it stands: the same document ID, the same page, and the same quote character for character in its original language. Do not shorten, correct, translate or join quotes. Code treats a citation that is not an exact copy as unverified.
- For reference: a valid quote is one unbroken piece of one page with at least 10 letters or digits. The partial results were written under that rule, so an exact copy stays valid.
- When you merge duplicates, keep the citation from the document that states the item most completely. For a value changed by an answer, keep the citation of that answer.
- Leave `verified` out. Code sets it.
- When the partial results give no source for a value, leave the value out: `null`, or no entry. In a text field, write "Niet gevonden in stukken". Never infer a value from what is usual in tenders, and never calculate a date from another one.

## Language

Write all free text in English. Keep the names of lots and criteria as the documents write them. Quotes stay in their original language.

## Example

This example is invented. It shows the form of an answer; do not reuse its wording.

<example>
documents: [{"document_id": 9001, "doc_type": "guideline", "published": "2027-01-11"}, {"document_id": 9003, "doc_type": "qa", "published": "2027-02-01"}]
partial doc:9001 pages:12-15
{"key_dates": {"submission_deadline": {"value": "2027-03-12T12:00:00", "citation": {"document_id": 9001, "page": 12, "quote": "Uiterste datum van inschrijving: 12 maart 2027, 12:00 uur"}}, "questions_deadline": null}, "lots": [], "framework": null, "knockout_criteria": [{"category": "financial", "kind": "suitability", "text": "The bidder has an average annual turnover of at least EUR 750,000 over the last three financial years.", "threshold": "EUR 750,000 average annual turnover, last three financial years", "proof": "Auditor's statement", "proof_when": "on_request", "applies_to_lots": [], "consortium_allowed": null, "disproportionate": false, "citation": {"document_id": 9001, "page": 14, "quote": "een gemiddelde jaaromzet van ten minste EUR 750.000"}}], "award_basis": null, "award_criteria": [], "contract_risks": []}
partial doc:9001 pages:30-31
{"key_dates": {"submission_deadline": null, "questions_deadline": null}, "lots": [], "framework": null, "knockout_criteria": [{"category": "financial", "kind": "suitability", "text": "Average annual turnover of at least EUR 750,000.", "threshold": "EUR 750,000", "proof": null, "proof_when": null, "applies_to_lots": [], "consortium_allowed": null, "disproportionate": false, "citation": {"document_id": 9001, "page": 30, "quote": "G1 Omzeteis EUR 750.000 gemiddeld per jaar"}}], "award_basis": null, "award_criteria": [], "contract_risks": []}
partial doc:9003 pages:1-2
{"key_dates": {"submission_deadline": null, "questions_deadline": null}, "lots": [], "framework": null, "knockout_criteria": [{"category": "financial", "kind": "suitability", "text": "The turnover requirement is lowered to an average annual turnover of at least EUR 500,000.", "threshold": "EUR 500,000 average annual turnover", "proof": null, "proof_when": null, "applies_to_lots": [], "consortium_allowed": null, "disproportionate": false, "citation": {"document_id": 9003, "page": 2, "quote": "De omzeteis wordt verlaagd naar een gemiddelde jaaromzet van ten minste EUR 500.000."}}], "award_basis": null, "award_criteria": [], "contract_risks": []}
<example_output>
{"key_dates": {"submission_deadline": {"value": "2027-03-12T12:00:00", "citation": {"document_id": 9001, "page": 12, "quote": "Uiterste datum van inschrijving: 12 maart 2027, 12:00 uur"}}, "questions_deadline": null}, "lots": [], "framework": null, "knockout_criteria": [{"requirement_id": "KO-01", "category": "financial", "kind": "suitability", "text": "The bidder has an average annual turnover of at least EUR 500,000 over the last three financial years.", "threshold": "EUR 500,000 average annual turnover, last three financial years", "proof": "Auditor's statement", "proof_when": "on_request", "applies_to_lots": [], "consortium_allowed": null, "disproportionate": false, "citation": {"document_id": 9003, "page": 2, "quote": "De omzeteis wordt verlaagd naar een gemiddelde jaaromzet van ten minste EUR 500.000."}, "overridden_by_qa": true}], "award_basis": null, "award_criteria": [], "contract_risks": [], "documents_read": [], "documents_skipped": [], "warnings": []}
</example_output>
</example>
