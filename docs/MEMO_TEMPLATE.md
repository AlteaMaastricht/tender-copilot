# Bid/No-Bid Memo — Template (TenderNed)

> One memo per tender (or per perceel/lot; see Q8 in `DECISIONS.md`, still open). Written for a decision-maker who reads only §1 and §8.

**Ground rules**
- **Cite everything.** Every date, number, requirement and weighting gets a source tag: `[LEIDRAAD §3.2 p.14]`, `[NvI-2 #17 p.3]`, `[PvE §4.1.3 p.22]`. No source → write *Niet gevonden in stukken*. Never infer.
- **Latest document wins.** Nota's van Inlichtingen override earlier stukken (check the rangorde clause in the leidraad). State which NvI the memo reflects.
- **Knock-outs gate the decision.** One confirmed ❌ in §4 = NO-BID, unless the gap can be closed (combinatie, beroep op derden, onderaanneming) before sluiting.
- Status: ✅ voldoet · ⚠️ onzeker · ❌ voldoet niet. Confidence: hoog / middel / laag.
- **Who fills what.** `[copilot]` = model output, verified in code where stated. `[human]` = filled in by the bid team in the UI, never generated. `[code]` = taken from structured data or computed, not written by the model. Only `[copilot]` parts belong in the model's output schema.

**Citations**
- The schema stores every citation as **document ID + page + verbatim quote**. The source tag is generated from these; the section or question number is optional extra.
- A page number is always required. A citation whose quote is not found on that page (citation verifier) is shown as *niet geverifieerd* and does not count as a source.
- The closing date (*sluiting inschrijving*) comes from the TenderNed API (`sluitingsDatum`), tagged `[TenderNed API]`. It is never taken from a document or the notice.

**Recommendation values** (used in §1 and §8; decided in code, not by the model)

| Value | Shown as | Rule |
|---|---|---|
| `bid` | BID | No ❌ knock-out, no unresolved ⚠️ knock-out, fit score ≥ 70 |
| `bid_conditional` | BID ONDER VOORWAARDEN | A ❌ or ⚠️ knock-out whose gap can be closed before sluiting, or fit score 50–69 |
| `no_bid` | NO-BID | A confirmed ❌ knock-out that cannot be closed, or fit score < 50 |
| `needs_review` | TER BEOORDELING | The copilot cannot decide: knock-outs missing or low confidence, unverified citations on a knock-out, or the provisional fit score is incomplete |

---

## 1. Summary `[copilot]`, `[code]`
**Advice (BLUF):** the recommendation value above, with one sentence on why. `[code]` decides the value; `[copilot]` writes the sentence.

| Field | Value | Source | Filled by |
|---|---|---|---|
| Aanbestedende dienst | | | `[code]` notice |
| Title · TenderNed-kenmerk · link | | | `[code]` TenderNed |
| What is being bought (1–2 sentences) | | | `[copilot]` |
| Type opdracht · procedure | levering / dienst / werk · openbaar / niet-openbaar / … | | `[code]` notice |
| Europees / nationaal · CPV-codes | | | `[code]` notice |
| Percelen we target | | | `[copilot]` |
| Geraamde waarde · plafondbedrag | | | `[copilot]`, checked against notice |
| Looptijd + verlengingsopties | | | `[copilot]` |
| Raamovereenkomst? Single or multi-party | | | `[copilot]` |
| Inschrijven via (TenderNed / other platform) | | | `[code]` TenderNed |
| Incumbent (zittende partij), if known | | | `[human]` |

## 2. Deadlines `[copilot]`, `[code]`, `[human]`
Times as stated in the stukken (NL local time). Internal milestones are back-scheduled from the external ones.

| Moment | Date & time | Source | Internal action / owner `[human]` |
|---|---|---|---|
| Publicatie `[code]` | | TenderNed API | |
| Deadline vragen (NvI) `[copilot]` | | | Freeze our questions 2 werkdagen earlier |
| Publicatie NvI 1, 2, … `[copilot]` | | | Re-check §4–§6 |
| **Sluiting inschrijving** `[code]` | | `[TenderNed API]` | Upload ≥ 1 werkdag early |
| Presentatie / interview / demo `[copilot]` | | | |
| Voorlopige gunning → Alcatel-termijn (≥ 20 kalenderdagen) `[copilot]` | | | |
| Gestanddoeningstermijn `[copilot]` | | | |
| Start opdracht `[copilot]` | | | |
| *Internal:* go/no-go · red team · sign-off `[human]` | | — | |

`[code]` flags the working days left until sluiting. `[human]` says whether that is realistic for the bid team.

## 3. Fit score (0–100) `[copilot]`, `[human]`, `[code]`
Scored against our bedrijfsprofiel (`COMPANY.md`). 1 = poor … 5 = excellent. `[code]` computes Score = Σ(weight × score) / 5.

| Dimension | Weight | Scored by | 1–5 | Rationale + source |
|---|---|---|---|---|
| Scope match with core capabilities | 25% | `[copilot]` | | |
| Win probability (incumbent, relationship, competition, award model suits us) | 20% | `[human]` | | |
| Strength of references / track record | 15% | `[copilot]` | | |
| Commercial attractiveness (volume, margin, duration) | 15% | `[human]` | | |
| Capacity & timing | 10% | `[copilot]` | | |
| Contract & risk profile | 10% | `[copilot]` | | |
| Strategic value | 5% | `[human]` | | |
| **Total** | 100% | `[code]` | **__/100** | Confidence: |

- Until the `[human]` dimensions are scored, the total is **provisional**: `[code]` shows the copilot share (60 of 100 weight points) and the range the total can still reach.
- Bands: ≥ 70 bid · 50–69 bid with conditions · < 50 no-bid. Knock-outs override the score.
- A `[copilot]` dimension with no source gets no score (*Niet gevonden in stukken*), not a guess.

## 4. Knock-out table `[copilot]`, `[human]`
Covers every pass/fail requirement: uitsluitingsgronden, geschiktheidseisen (draagkracht, referenties, certificering), minimumeisen in the PvE, and formal submission requirements. Status comes from requirement matching against the company knowledge base, with evidence from both sides.

| # | Requirement (short, near-verbatim) | Type | Proof & when (UEA / bij inschrijving / op verzoek) | Status | Our evidence (KB) | Gap closure `[copilot]` · owner `[human]` | Source |
|---|---|---|---|---|---|---|---|
| KO-1 | | uitsluiting / geschiktheid / minimumeis / formeel | | ✅ ⚠️ ❌ | | | |

Mark any requirement that looks disproportionate (Gids Proportionaliteit) and carry it into §7.

## 5. Award-criteria strategy `[copilot]`, `[human]`
Gunningsmaatstaf: BPKV / laagste prijs / laagste kosten (TCO). Scoring method: fictieve korting / weighted points / relative vs absolute price scoring.

| Criterion / subcriterion | Weight / max pts | How it is scored (scale, panel, page limit) | Our position H/M/L | Win theme + proof | Owner `[human]` |
|---|---|---|---|---|---|
| | | | | | |
| **Price** | | formula: | | target / price-to-win `[human]` | |

`[copilot]` states the quality-vs-price trade-off from the award formula (e.g. *1 quality point ≈ €X fictieve korting ≈ Y% of price*). `[human]` decides where we can actually beat the competition.

## 6. Risks and gaps `[copilot]`, `[human]`
Check contract terms (ARVODI / ARIV / ARBIT / UAV-GC / own terms), aansprakelijkheid and boetes, IE, indexering, volume uncertainty, SROI, duurzaamheid, AVG/BIO, onderaanneming and our own capacity.

| # | Risk / gap | Category | Likelihood H/M/L | Impact H/M/L | Mitigation (price in / ask in NvI / accept) | Source |
|---|---|---|---|---|---|---|
| R-1 | | | | | | |

`[human]` confirms or changes likelihood, impact and mitigation.

## 7. Questions for the Nota van Inlichtingen `[copilot]`, `[human]`
Questions are anonymised and published to all bidders, so they must never reveal our solution or strategy. Raise anything unclear, contradictory or disproportionate **now**: under the Grossmann doctrine, objections not raised in time are generally lost. `[copilot]` drafts; `[human]` approves before anything is sent.

| # | Question (Dutch, neutral, cites doc + §) | Purpose: verduidelijking / tegenstrijdigheid / proportionaliteit / fout | Linked to | Priority |
|---|---|---|---|---|
| Q-1 | | | KO-# / R-# | |

## 8. Recommendation and citations `[code]`, `[copilot]`, `[human]`
**Decision:** the recommendation value (`[code]`, rules above). The final decision is `[human]`: approve or reject in the UI.
- **Why** `[copilot]`: top 3 reasons, each pointing to §3–§6.
- **Conditions** `[copilot]` (if `bid_conditional`): what must be true, and by when (e.g. NvI answer on KO-3, partner signed for perceel 2).
- **Bid investment** `[human]`: estimated hours or cost, team and key people.
- **Decision owner · date** `[human]`.

**Documents reviewed** `[code]` (the IDs used in source tags)

| ID | Document | Version / date |
|---|---|---|
| AANK | Aankondiging (TenderNed) | |
| LEIDRAAD | Aanbestedingsleidraad / selectieleidraad | |
| PvE | Programma van Eisen / bestek | |
| OVK | (Concept)overeenkomst + algemene voorwaarden | |
| NvI-n | Nota van Inlichtingen n | |
| BIJL-x | Bijlagen (UEA, prijsformat, …) | |

Document IDs come from classifying each downloaded document by type: name-based rules first, a model only where unclear. TenderNed's `typeDocument` gives the file format, not the document type. Version / date is the TenderNed `datumPublicatie`.
