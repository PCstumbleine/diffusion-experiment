# Extraction prompt — version 1.1.0

Used against `raw_documents.raw_content` for every document that passes an
initial relevance filter. Produces `extracted_events.raw_llm_output` and the
fields that populate `extracted_events`, `event_entities`, and the raw (not
calibrated) columns of `entity_relationships`.

**Hard boundary, enforced by what this prompt is allowed to ask for:** the
LLM extracts facts and its own raw confidence scores. It never computes
normalized/standardized surprise, a calibrated probability, an abnormal
return, an expected transmission effect, or a trade selection threshold.
Those are all downstream, deterministic, code-computed values (Sections 1,
3, 4, 8 of the spec). This isn't a style preference — mixing the two would
make it impossible to tell, later, whether a bad outcome came from bad
extraction or a bad statistical model.

---

## System prompt

```
You are an information-extraction system for a financial research project.
Your job is narrow: read the attached document and extract structured facts
about corporate events it describes. You are not asked for and must not
provide: opinions about whether this is a good trade, a probability that
has not been explicitly requested below, or any number that requires
statistical modeling rather than reading the document.

For every factual claim you extract, you must be able to point to the
specific text span that supports it. If you cannot point to a span, do not
extract the claim — log it as unsupported instead.

If the document contains no extractable corporate event in the categories
below, return an empty events array. Do not force a weak or speculative
event into the schema to avoid returning nothing.

Never use knowledge of what happened to this company AFTER this document's
publication date, even if you have that knowledge from training. Extract
only what this document itself states or directly supports.
```

## Task instructions

```
Read the document below and extract every distinct economic event it
describes, in one of these categories:

  guidance_revision | capacity_change | order_win | order_loss |
  supply_agreement | customer_agreement | earnings_surprise |
  buyback_or_capital_return | acquisition_or_divestiture | other_material_event

A single document commonly contains more than one event (e.g., one earnings
release can contain an earnings surprise AND a guidance revision AND a
capacity announcement). Extract each as a SEPARATE event object. Do not
merge distinct economic propositions into one event.

For each event, extract:

1. CATALYST CONTEXT
   - catalyst_description: one sentence describing the originating disclosure
   - event_category: one of the categories above

2. ENTITIES AND ROLES
   For every company mentioned as party to this event, extract:
     - entity_name (as written in the document)
     - role: one of issuer | subject | supplier | customer | buyer |
             target | partner | counterparty
     - evidence_span: the exact text supporting this entity's role
   If a relationship between two entities is described (e.g., "Company A
   is a supplier to Company B"), extract it separately:
     - entity_a, entity_b, relationship_type
     - relationship_evidence: one of explicit_named (both companies named
       together with an explicit relationship stated) | quantified_named
       (a named company plus a quantified dependency, e.g. "40% of revenue
       from Company B") | inferred_structured (relationship implied by
       structured data in the document itself, not stated in prose --
       still something you are reading off the document, not inferring
       from outside knowledge)
     - source_authority: one of government | regulatory_filing | company |
       licensed_commercial | secondary_inference
     - document_explicitly_states_transmission_history: true | false | null
       — true ONLY if THIS DOCUMENT itself explicitly describes a past
       instance of a shock transmitting through this kind of relationship
       (e.g. "when Company B cut orders in 2024, our revenue fell 8%
       within the quarter"). false if the document says nothing about
       transmission history. Never infer this from what you know about
       how these kinds of relationships generally behave — that is
       exactly the kind of external-knowledge judgment this field must
       NOT contain (see "Hard boundary" above and the system prompt's
       "never use knowledge... from training" rule). If true, also give:
     - transmission_history_evidence_span: the exact text supporting it
     - raw_llm_relationship_score: your own 0-1 confidence that this
       relationship exists as described (RAW — this is not a calibrated
       probability and will not be treated as one)
     - evidence_span: exact supporting text

   Note what is DELIBERATELY not asked for here: a "does this shock
   transmit" judgment, an economic-reasoning-based transmission score, or
   anything resembling a probability of transmission. An earlier version
   of this prompt asked for exactly that (shock_transmission_evidence:
   historical/economic_model/new_or_unobserved, justified by "this document
   OR YOUR KNOWLEDGE") — a code review round correctly caught that as a
   direct contradiction of this prompt's own extraction-only boundary, and
   it's removed. Any genuine economic-transmission reasoning happens in a
   separate, downstream, explicitly-versioned model (Sections 3/4 of the
   spec) that takes document_explicitly_states_transmission_history as ONE
   input among several — never inside this extraction step.

   A second review round caught the same contradiction hiding one level
   down: relationship_evidence originally also allowed a value called
   model_inferred, described as "you are inferring this relationship
   yourself, not reading it from the text" — directly contradicting this
   same system prompt's "if you cannot point to a supporting span, do not
   extract the claim" rule, just for a relationship instead of a
   transmission judgment. Removed for the same reason; every relationship
   this prompt extracts must be something the document itself states or
   structurally supports, never the model's own inference.

3. SURPRISE / MAGNITUDE (raw values only — do not compute a ratio, a
   percentage change, or any standardized score yourself; a downstream
   script applies the correct transform for this event_type)
     - surprise_type (e.g. "revenue_guidance", "eps_guidance", "order_size")
     - observed_value, reference_value (the new figure and what it's being
       compared against)
     - reference_source (e.g. "company's prior quarter guidance") — for
       Phase I-A, this must be the company's own prior disclosure, never
       analyst consensus
     - reference_timestamp
     - unit, period
     - evidence_span

4. TIMING
     - source_published_at: the timestamp this document itself claims for
       publication, exactly as stated (do not infer or estimate)
     - explicit_correction: true/false — does this document explicitly
       correct or supersede an earlier disclosure of the same fact?

5. CITATIONS
     - For every extracted field above, the evidence_span given must be an
       exact substring of the document. Do not paraphrase and call it a span.

Return valid JSON matching the schema below. If a field cannot be
determined from the document, use null rather than guessing.
```

## Output schema (structured output / JSON mode)

```json
{
  "type": "object",
  "properties": {
    "document_id": {"type": "string"},
    "extraction_prompt_version": {"const": "1.1.0"},
    "events": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "event_category": {"type": "string", "enum": [
            "guidance_revision", "capacity_change", "order_win", "order_loss",
            "supply_agreement", "customer_agreement", "earnings_surprise",
            "buyback_or_capital_return", "acquisition_or_divestiture", "other_material_event"
          ]},
          "catalyst_description": {"type": "string"},
          "entities": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "entity_name": {"type": "string"},
                "role": {"type": "string", "enum": [
                  "issuer","subject","supplier","customer","buyer","target","partner","counterparty"
                ]},
                "evidence_span": {"type": "string"}
              },
              "required": ["entity_name", "role", "evidence_span"]
            }
          },
          "relationships": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "entity_a": {"type": "string"},
                "entity_b": {"type": "string"},
                "relationship_type": {"type": "string"},
                "relationship_evidence": {"type": "string", "enum": [
                  "explicit_named","quantified_named","inferred_structured"
                ]},
                "source_authority": {"type": "string", "enum": [
                  "government","regulatory_filing","company","licensed_commercial","secondary_inference"
                ]},
                "document_explicitly_states_transmission_history": {"type": ["boolean", "null"]},
                "transmission_history_evidence_span": {"type": ["string", "null"]},
                "raw_llm_relationship_score": {"type": "number", "minimum": 0, "maximum": 1},
                "evidence_span": {"type": "string"}
              },
              "required": ["entity_a", "entity_b", "relationship_type",
                           "relationship_evidence", "source_authority",
                           "document_explicitly_states_transmission_history", "evidence_span"]
            }
          },
          "surprise": {
            "type": ["object", "null"],
            "properties": {
              "surprise_type": {"type": "string"},
              "observed_value": {"type": ["number", "null"]},
              "reference_value": {"type": ["number", "null"]},
              "reference_source": {"type": "string"},
              "reference_timestamp": {"type": ["string", "null"]},
              "unit": {"type": ["string", "null"]},
              "period": {"type": ["string", "null"]},
              "evidence_span": {"type": "string"}
            }
          },
          "source_published_at": {"type": ["string", "null"]},
          "explicit_correction": {"type": "boolean"}
        },
        "required": ["event_category", "catalyst_description", "entities"]
      }
    },
    "unsupported_claims_noted": {
      "type": "array",
      "items": {"type": "string"},
      "description": "Claims the document seems to make but that lack a clean evidence span — logged, never fabricated into an event."
    }
  },
  "required": ["document_id", "extraction_prompt_version", "events"]
}
```

## What happens after this prompt runs (not the LLM's job)

- A deterministic script maps `surprise_type` to a row in
  `surprise_transform_registry` and computes `surprise_transformed` —
  never the LLM.
- `raw_llm_relationship_score` is stored as-is in `entity_relationships`.
  `calibrated_p_relationship` stays NULL until an actual calibration model
  exists and is versioned.
- `entity_relationships.shock_transmission_evidence` is NOT set from this
  prompt's output directly (the prompt no longer produces anything at that
  level of judgment — see the note above). Until a real downstream
  transmission-classification step exists, the ingestion code sets it to
  the conservative default `'new_or_unobserved'`, using
  `document_explicitly_states_transmission_history` only as one input a
  future classification step can read — never as a stand-in verdict itself.
- `expected_transmission_effect` is populated later, if at all, only by a
  separately trained, frozen, prior-only model — never by this prompt.
- Candidate eligibility (Section 3's `candidate_eligible` rule) is computed
  by a script reading `relationship_evidence` and
  `evidence_publicly_available_at`, not asserted by the LLM.

## Versioning

**1.1.0:** removed `model_inferred` from `relationship_evidence` (a second
review round caught it contradicting this prompt's own "must point to a
text span" rule — see the note in section 2 above). No other change.

Bump `extraction_prompt_version` on any change to the instructions or
schema above, and never silently reprocess old documents under a new
version number without recording which version produced which
`raw_llm_output` — Section 9's promotion gates are measured per prompt
version, and a hidden version change would make that comparison meaningless.
