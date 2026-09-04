# Recall check 001 — Lilly Q2 2026 release, independent blind read

Companion to `blind_recall_instructions.md` / `chatgpt_prompt_next_step.md`.
This is the "test recall directly" step from the proposed three-part next
move — done before Dry Run 002, as planned.

## What was compared

- **Document:** the same Lilly Q2 2026 earnings exhibit Dry Run 001 used
  (`q226lillysalesandearningsp.htm`, source saved at
  `build/dry_run_extractions/sources/8b89a41e-....txt`) — also the filing
  the "Eli Lilly and Company" alias bug (Dry Run 001 fix #1) came from.
- **Baseline:** Dry Run 001's actual hand-extraction for this document —
  `build/dry_run_extractions/8b89a41e-....json`, extraction prompt v1.1.0,
  **6 events**.
- **Independent read:** a blind read against the *current* extraction
  prompt (v1.2.0), done by a different model than the extractor, per
  `blind_recall_instructions.md`. Delivered as raw JSON matching the
  pipeline's own output schema (`extraction_prompt_v1.md`) rather than the
  requested human-readable inventory — noted below as a format gap, but the
  schema-shaped output is actually more directly comparable, so it was used
  as received: **34 events**.

Everything below is from re-checking that JSON against the actual source
file and the actual entity-resolution code myself — not taken on the
independent read's word, same discipline as every other round.

## Headline finding: the recall gap is real and large

6 vs. 34 events on the identical document. Category breakdown of the 34:

| category | count |
|---|---|
| other_material_event | 20 |
| acquisition_or_divestiture | 6 |
| earnings_surprise | 3 |
| guidance_revision | 3 |
| capacity_change | 2 |

The acquisition count is a useful sanity check: the independent read found
all 4 explicitly-named completed acquisitions (Orna, Ajax, Centessa,
Kelonia) plus an aggregate "3 acquisitions post-quarter-end" statement and
a forward "agreement to acquire AtaiBeckley" — 6 total. Dry Run 001 had
exactly 2 of the 4 named ones. That matches the discrepancy already on
record ("2 of Lilly's 4 named acquisitions") and confirms the independent
read is finding genuine, distinct, extractable facts — not padding the
count with noise.

The bulk of the gap (20 of 28 additional events) is `other_material_event`
— clinical trial readouts, FDA/EMA regulatory actions, and pipeline
magnitude figures (e.g. "Jaypirca reduced risk of progression or death by
45%", "VERVE-102 reduced PCSK9 by up to 88%"). These fit the prompt's own
category definition (a material fact that doesn't fit the other nine
categories) and are exactly the kind of catalyst a pharma-issuer pipeline
should care about — Dry Run 001's manual pass simply didn't have the
patience to enumerate all of them by hand, which is the whole reason this
check exists.

## Verification performed (not just eyeballed)

**Evidence-span exactness** — the prompt's own hard rule ("must be an
exact substring of the document... do not paraphrase and call it a span").
Checked all 62 `evidence_span` fields (entities + relationships +
surprise) programmatically against the actual saved source text:

- **59 / 62 are exact, verified substrings.**
- **3 / 62 fail** — all three are the range-valued guidance figures pulled
  from the same HTML `<table>` (revenue guidance, EPS guidance,
  performance-margin guidance). Each span has spurious backslash-escaping
  (`\<`, `\:`, `\/`) in front of characters that do **not** appear in the
  real document — e.g. it captured `\<td colspan="3"...` where the source
  literally has `<td colspan="3"...` with no backslash anywhere in the
  file (confirmed: zero backslash characters exist in the source text at
  all). This reads like markdown-style escaping leaking into the span
  during generation, not a fabricated quote.
- **Net effect if unfixed:** these are exactly the three events the
  existing validator (`validate_extraction_output_drops_claim_with_bad_evidence_span`)
  would silently drop — the real guidance-range numbers extracted
  correctly, discarded anyway on a technicality in how the span was typed
  out.

**Underlying numbers, independently cross-checked against the table** (not
just trusted because the span matched):

| figure | old (reference) | new (observed) | independent read | correct? |
|---|---|---|---|---|
| Revenue guidance | $82–85B | $85–87B | ref 82–85 / obs 85–87 | yes |
| EPS guidance | $35.50–$37.00 | $35.50–$36.50 | ref 35.5–37.0 / obs 35.5–36.5 | yes |
| Performance margin | 47.0–48.5% | 49.0–50.5% | ref 47.0–48.5 / obs 49.0–50.5 | yes |

So the span-formatting bug is cosmetic — the actual old/new values and
which side is "reference" vs. "observed" are all correct.

**A second, new entity-resolution gap** (distinct from the Dry Run 001
"Eli Lilly and Company" fix): 6 of the 34 events name the issuer as the
bare informal **"Lilly"** rather than "Eli Lilly and Company" or
"ELI LILLY & Co". Traced through the actual code
(`entity_resolution.normalize_entity_name` + `seed_entities.seed_one`):
normalization only strips a *trailing* corporate suffix, never a leading
word, so "ELI LILLY & Co" normalizes to `eli lilly`, but bare "Lilly"
normalizes to `lilly` — no seeded alias covers that, including the
`seed_ampersand_and_form` alias added for the Dry Run 001 fix (that one
only spells out "&" as "and", it doesn't add a short-form alias). Every
one of those 6 events would come back as an **unresolved mention** today,
not a resolution error, but silent under-attribution to the Lilly entity.

## Format gap, for the record

`blind_recall_instructions.md` asked for a human-scannable inventory
(events / deliberately-excluded-and-why / summary counts) specifically to
surface borderline judgment calls generously. What came back is the raw
pipeline-schema JSON with a 3-item `unsupported_claims_noted` list instead.
It's arguably more useful for this particular comparison (directly
diffable against Dry Run 001's own JSON), but it means the "debatable"
middle ground — e.g. is "Foundayo and Zepbound became covered for millions
of Americans" (no payer named, no figure) really an extractable event, or
should it have been logged as unsupported? — wasn't surfaced explicitly.
Worth deciding whether to re-ask for the requested format on a future
recall check, or to standardize on schema-JSON since it's more directly
comparable.

## Recommendation

Both real findings here are small and independent of the filing-volume
safety valve (which is done, tested, verified, and out of scope for this
document). Before Dry Run 002:

1. Decide whether to fix evidence-span capture for table-sourced figures,
   or relax/normalize the validator's exact-substring check for this
   class of span (nested-table HTML is genuinely awkward to quote
   verbatim) — currently these silently vanish.
2. Seed a "Lilly" bare-name alias (and worth a quick audit of the other
   107 watchlist companies for the same short-form pattern — first name
   only, ticker-style short forms, etc. — since this is a company-naming
   convention rather than a Lilly-specific one).

Neither blocks Dry Run 002 from running, but both mean Dry Run 002's own
manual read would need to know to expect this class of gap, or the same
surprises get rediscovered instead of confirmed-fixed.
