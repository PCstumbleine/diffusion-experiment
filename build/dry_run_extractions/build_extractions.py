"""
One-off script that writes the 9 dry-run extraction JSON files. Kept here
(not deleted after use) as a durable, inspectable record of exactly how
each file was constructed and which evidence_span was verified against
which document -- every span below was independently confirmed via Grep
against the actual saved raw_content in sources/*.txt before being written
here; this script does not re-verify them, it just records the result.
"""
import json
import os

OUT_DIR = os.path.dirname(__file__)

PV = "1.1.0"


def write(doc_id, events):
    out = {"document_id": doc_id, "extraction_prompt_version": PV, "events": events}
    path = os.path.join(OUT_DIR, f"{doc_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print("wrote", path)


# ---------------------------------------------------------------------
# CAT primary (cover page) -- no figures stated, minimal event
# ---------------------------------------------------------------------
write("1ab03952-2d9d-4ab6-b78e-c4505646b2f6", [
    {
        "event_category": "earnings_surprise",
        "catalyst_description": "Caterpillar Inc. 8-K cover page announcing Q2 2026 results were reported in an attached press release (Exhibit 99.1); no figures are stated in the cover page itself.",
        "entities": [
            {"entity_name": "Caterpillar Inc.", "role": "issuer",
             "evidence_span": "Caterpillar Inc. issued a press release reporting its financial results for the quarter ended June 30, 2026"}
        ],
        "relationships": [],
        "surprise": None,
        "source_published_at": "2026-08-04",
        "explicit_correction": False,
    }
])

# ---------------------------------------------------------------------
# CAT EX-99.2 (retail statistics supplement)
# ---------------------------------------------------------------------
write("c01f0bbd-4e68-4ccf-b5db-c9ed1e4f90ba", [
    {
        "event_category": "other_material_event",
        "catalyst_description": "Caterpillar furnished supplemental Reg FD retail-sales statistics (machines and power systems) by segment and region; the document itself disclaims that this is not an audited or estimate-grade figure.",
        "entities": [
            {"entity_name": "Caterpillar Inc.", "role": "issuer",
             "evidence_span": "Caterpillar Inc. (&#8220;Caterpillar&#8221;, &#8220;we&#8221; or &#8220;our&#8221;) is furnishing supplemental information concerning"}
        ],
        "relationships": [],
        "surprise": None,
        "source_published_at": "2026-08-04",
        "explicit_correction": False,
    }
])

# ---------------------------------------------------------------------
# CAT EX-99.1 (Q2 2026 earnings release)
# ---------------------------------------------------------------------
write("003d1990-3ffb-445d-a871-f47beb2eaca2", [
    {
        "event_category": "earnings_surprise",
        "catalyst_description": "Caterpillar reported Q2 2026 sales and revenues of $20.5 billion, up 24% year-over-year.",
        "entities": [
            {"entity_name": "Caterpillar Inc.", "role": "issuer",
             "evidence_span": "Caterpillar Reports Second-Quarter 2026 Results"}
        ],
        "relationships": [],
        "surprise": {
            "surprise_type": "revenue_yoy_change",
            "observed_value": 20.5,
            "reference_value": None,
            "reference_source": "Caterpillar's own prior-year quarter (Q2 2025) results, as restated in this same release",
            "reference_timestamp": "2025-08-05",
            "unit": "USD_billions",
            "period": "Q2 2026",
            "evidence_span": "Second-quarter 2026 sales and revenues increased 24% to $20.5 billion"
        },
        "source_published_at": "2026-08-04",
        "explicit_correction": False,
    },
    {
        "event_category": "buyback_or_capital_return",
        "catalyst_description": "Caterpillar deployed $2.2 billion of cash for share repurchases and dividends in Q2 2026.",
        "entities": [
            {"entity_name": "Caterpillar Inc.", "role": "issuer",
             "evidence_span": "Caterpillar Reports Second-Quarter 2026 Results"}
        ],
        "relationships": [],
        "surprise": {
            "surprise_type": "capital_return_amount",
            "observed_value": 2.2,
            "reference_value": None,
            "reference_source": "not stated -- no comparable prior-period figure given in this passage",
            "reference_timestamp": None,
            "unit": "USD_billions",
            "period": "Q2 2026",
            "evidence_span": "Deployed $2.2 billion of cash for share repurchases and dividends in the second quarter"
        },
        "source_published_at": "2026-08-04",
        "explicit_correction": False,
    },
])

# ---------------------------------------------------------------------
# JPM primary (8-K, Item 8.01 -- closed debt offerings; no exhibit)
# ---------------------------------------------------------------------
write("ce47bfbd-5a40-458a-9655-9718bec53689", [
    {
        "event_category": "other_material_event",
        "catalyst_description": "JPMorgan Chase & Co. closed public offerings of four series of notes (Floating Rate Notes due 2030, Fixed-to-Floating Rate Notes due 2030 and 2032, and Fixed-Rate Reset Subordinated Notes due 2041) totaling approximately $9 billion in aggregate principal.",
        "entities": [
            {"entity_name": "JPMorgan Chase & Co.", "role": "issuer",
             "evidence_span": "JPMorgan Chase&#160;&amp; Co. closed public offerings of"}
        ],
        "relationships": [],
        "surprise": None,
        "source_published_at": "2026-07-23",
        "explicit_correction": False,
    }
])

# ---------------------------------------------------------------------
# LLY primary (cover page) -- no figures stated
# ---------------------------------------------------------------------
write("38ec0ced-9aef-49bc-a1ee-6ea20e9a3a32", [
    {
        "event_category": "earnings_surprise",
        "catalyst_description": "Eli Lilly 8-K cover page announcing Q2 2026 results were reported in an attached press release (Exhibit 99.1); no figures are stated in the cover page itself.",
        "entities": [
            {"entity_name": "Eli Lilly and Company", "role": "issuer",
             "evidence_span": "announcing the financial results of Eli Lilly and Company for the quarter ended June 30, 2026"}
        ],
        "relationships": [],
        "surprise": None,
        "source_published_at": "2026-08-05",
        "explicit_correction": False,
    }
])

# ---------------------------------------------------------------------
# LLY EX-99.1 (Q2 2026 earnings release + guidance + M&A + capacity)
# ---------------------------------------------------------------------
write("8b89a41e-b98a-4e19-8525-5fa9f1bb8f1b", [
    {
        "event_category": "earnings_surprise",
        "catalyst_description": "Lilly reported Q2 2026 revenue of $23.0 billion, up 48% year-over-year, driven by Mounjaro and Zepbound volume.",
        "entities": [
            {"entity_name": "Eli Lilly and Company", "role": "issuer",
             "evidence_span": "Eli Lilly and Company (NYSE&#58; LLY) today announced"}
        ],
        "relationships": [],
        "surprise": {
            "surprise_type": "revenue_yoy_change",
            "observed_value": 23.0,
            "reference_value": None,
            "reference_source": "Lilly's own prior-year quarter (Q2 2025) results, as restated in this same release",
            "reference_timestamp": "2025-08-06",
            "unit": "USD_billions",
            "period": "Q2 2026",
            "evidence_span": "Revenue in Q2 2026 increased 48% to $23.0 billion driven primarily by Mounjaro and Zepbound volume."
        },
        "source_published_at": "2026-08-05",
        "explicit_correction": False,
    },
    {
        "event_category": "guidance_revision",
        "catalyst_description": "Lilly raised full-year 2026 revenue guidance to a range of $85.0-$87.0 billion.",
        "entities": [
            {"entity_name": "Eli Lilly and Company", "role": "issuer",
             "evidence_span": "Eli Lilly and Company (NYSE&#58; LLY) today announced"}
        ],
        "relationships": [],
        "surprise": {
            "surprise_type": "revenue_guidance",
            "observed_value": None,
            "reference_value": None,
            "reference_source": "Lilly's own prior full-year 2026 guidance (not itself re-stated with a figure in this specific passage)",
            "reference_timestamp": None,
            "unit": "USD_billions",
            "period": "FY2026",
            "evidence_span": "Increased 2026 full-year revenue guidance to be in the range of $85.0 billion to $87.0 billion"
        },
        "source_published_at": "2026-08-05",
        "explicit_correction": False,
    },
    {
        "event_category": "guidance_revision",
        "catalyst_description": "Lilly updated full-year 2026 non-GAAP EPS guidance to a range of $35.50-$36.50 after acquired IPR&D charges.",
        "entities": [
            {"entity_name": "Eli Lilly and Company", "role": "issuer",
             "evidence_span": "Eli Lilly and Company (NYSE&#58; LLY) today announced"}
        ],
        "relationships": [],
        "surprise": {
            "surprise_type": "eps_guidance",
            "observed_value": None,
            "reference_value": None,
            "reference_source": "Lilly's own prior full-year 2026 non-GAAP EPS guidance (not itself re-stated with a figure in this specific passage)",
            "reference_timestamp": None,
            "unit": "USD_per_share",
            "period": "FY2026",
            "evidence_span": "resulting in an updated range of $35.50 to $36.50"
        },
        "source_published_at": "2026-08-05",
        "explicit_correction": False,
    },
    {
        "event_category": "acquisition_or_divestiture",
        "catalyst_description": "Lilly completed its acquisition of Orna Therapeutics, Inc. during Q2 2026 as part of business development activity.",
        "entities": [
            {"entity_name": "Eli Lilly and Company", "role": "issuer",
             "evidence_span": "Eli Lilly and Company (NYSE&#58; LLY) today announced"},
            {"entity_name": "Orna Therapeutics, Inc.", "role": "target",
             "evidence_span": "Orna Therapeutics, Inc."}
        ],
        "relationships": [
            {
                "entity_a": "Eli Lilly and Company", "entity_b": "Orna Therapeutics, Inc.",
                "relationship_type": "acquirer_target",
                "relationship_evidence": "explicit_named", "source_authority": "company",
                "document_explicitly_states_transmission_history": False,
                "evidence_span": "completed acquisitions of Orna Therapeutics, Inc., Ajax Therapeutics, Inc., Centessa Pharmaceuticals plc. and Kelonia Therapeutics, Inc.",
                "raw_llm_relationship_score": 0.95
            }
        ],
        "surprise": None,
        "source_published_at": "2026-08-05",
        "explicit_correction": False,
    },
    {
        "event_category": "acquisition_or_divestiture",
        "catalyst_description": "Lilly completed its acquisition of Ajax Therapeutics, Inc. during Q2 2026 as part of business development activity.",
        "entities": [
            {"entity_name": "Eli Lilly and Company", "role": "issuer",
             "evidence_span": "Eli Lilly and Company (NYSE&#58; LLY) today announced"},
            {"entity_name": "Ajax Therapeutics, Inc.", "role": "target",
             "evidence_span": "Ajax Therapeutics, Inc."}
        ],
        "relationships": [
            {
                "entity_a": "Eli Lilly and Company", "entity_b": "Ajax Therapeutics, Inc.",
                "relationship_type": "acquirer_target",
                "relationship_evidence": "explicit_named", "source_authority": "company",
                "document_explicitly_states_transmission_history": False,
                "evidence_span": "completed acquisitions of Orna Therapeutics, Inc., Ajax Therapeutics, Inc., Centessa Pharmaceuticals plc. and Kelonia Therapeutics, Inc.",
                "raw_llm_relationship_score": 0.95
            }
        ],
        "surprise": None,
        "source_published_at": "2026-08-05",
        "explicit_correction": False,
    },
    {
        "event_category": "capacity_change",
        "catalyst_description": "Lilly committed an additional $4.5 billion to expand its Indiana manufacturing sites.",
        "entities": [
            {"entity_name": "Eli Lilly and Company", "role": "issuer",
             "evidence_span": "Eli Lilly and Company (NYSE&#58; LLY) today announced"}
        ],
        "relationships": [],
        "surprise": {
            "surprise_type": "capex_commitment",
            "observed_value": 4.5,
            "reference_value": None,
            "reference_source": "not stated -- a new capital commitment, not a comparison against a prior figure",
            "reference_timestamp": None,
            "unit": "USD_billions",
            "period": None,
            "evidence_span": "Committed an additional $4.5 billion to expand Indiana manufacturing sites"
        },
        "source_published_at": "2026-08-05",
        "explicit_correction": False,
    },
])

# ---------------------------------------------------------------------
# NVDA primary (cover page) -- no figures stated
# ---------------------------------------------------------------------
write("daa5a135-bac6-4eb4-b453-a4ff3fdfd101", [
    {
        "event_category": "earnings_surprise",
        "catalyst_description": "NVIDIA 8-K cover page announcing Q2 FY2027 results were reported in attached press release and CFO commentary exhibits; no figures are stated in the cover page itself.",
        "entities": [
            {"entity_name": "NVIDIA Corporation", "role": "issuer",
             "evidence_span": "NVIDIA Corporation, or the Company, issued a press release announcing its results"}
        ],
        "relationships": [],
        "surprise": None,
        "source_published_at": "2026-08-26",
        "explicit_correction": False,
    }
])

# ---------------------------------------------------------------------
# NVDA EX-99.2 (CFO commentary) -- restates the SAME Q2 revenue figure as
# the press release (EX-99.1), for catalyst-level canonicalization
# ---------------------------------------------------------------------
write("850832f9-b1f7-45ad-95bd-71e332131b1e", [
    {
        "event_category": "earnings_surprise",
        "catalyst_description": "CFO commentary restating NVIDIA's Q2 FY2027 revenue of $96.2 billion, up 106% year-over-year.",
        "entities": [
            {"entity_name": "NVIDIA Corporation", "role": "issuer",
             "evidence_span": "NVIDIA CORPORATION "}
        ],
        "relationships": [],
        "surprise": {
            "surprise_type": "revenue_yoy_change",
            "observed_value": 96.2,
            "reference_value": None,
            "reference_source": "NVIDIA's own prior-year quarter (Q2 FY2026) results, as restated in this same document",
            "reference_timestamp": "2025-08-27",
            "unit": "USD_billions",
            "period": "Q2 FY2027",
            "evidence_span": "Revenue for the second quarter was a record $96.2 billion, up 106% from a year ago and up 18% sequentially."
        },
        "source_published_at": "2026-08-26",
        "explicit_correction": False,
    }
])

# ---------------------------------------------------------------------
# NVDA EX-99.1 (press release) -- earnings, guidance, buyback, partnerships
# ---------------------------------------------------------------------
write("33a7d374-9878-4d14-a039-ee18e591f959", [
    {
        "event_category": "earnings_surprise",
        "catalyst_description": "NVIDIA reported Q2 FY2027 revenue of $96.2 billion, up 106% year-over-year.",
        "entities": [
            {"entity_name": "NVIDIA", "role": "issuer",
             "evidence_span": "NVIDIA (NASDAQ&#58; NVDA) today reported revenue"}
        ],
        "relationships": [],
        "surprise": {
            "surprise_type": "revenue_yoy_change",
            "observed_value": 96.2,
            "reference_value": None,
            "reference_source": "NVIDIA's own prior-year quarter (Q2 FY2026) results, as restated in this same release",
            "reference_timestamp": "2025-08-27",
            "unit": "USD_billions",
            "period": "Q2 FY2027",
            "evidence_span": "of $96.2 billion, up 18% from the previous quarter and up 106% from a year ago"
        },
        "source_published_at": "2026-08-26",
        "explicit_correction": False,
    },
    {
        "event_category": "guidance_revision",
        "catalyst_description": "NVIDIA guided Q3 FY2027 revenue to approximately $108.0 billion, plus or minus 2%.",
        "entities": [
            {"entity_name": "NVIDIA", "role": "issuer",
             "evidence_span": "NVIDIA (NASDAQ&#58; NVDA) today reported revenue"}
        ],
        "relationships": [],
        "surprise": {
            "surprise_type": "revenue_guidance",
            "observed_value": 108.0,
            "reference_value": None,
            "reference_source": "No explicit prior Q3 FY2027 guidance figure is restated in this passage -- this is the first stated guidance for that quarter",
            "reference_timestamp": None,
            "unit": "USD_billions",
            "period": "Q3 FY2027",
            "evidence_span": "Revenue is expected to be $108.0 billion, plus or minus 2%."
        },
        "source_published_at": "2026-08-26",
        "explicit_correction": False,
    },
    {
        "event_category": "buyback_or_capital_return",
        "catalyst_description": "NVIDIA returned approximately $26.0 billion to shareholders via share repurchases and dividends in Q2 FY2027.",
        "entities": [
            {"entity_name": "NVIDIA", "role": "issuer",
             "evidence_span": "NVIDIA (NASDAQ&#58; NVDA) today reported revenue"}
        ],
        "relationships": [],
        "surprise": {
            "surprise_type": "capital_return_amount",
            "observed_value": 26.0,
            "reference_value": None,
            "reference_source": "not stated -- no comparable prior-period figure given in this passage",
            "reference_timestamp": None,
            "unit": "USD_billions",
            "period": "Q2 FY2027",
            "evidence_span": "NVIDIA returned approximately $26.0 billion to shareholders in the form of shares repurchased and cash dividends"
        },
        "source_published_at": "2026-08-26",
        "explicit_correction": False,
    },
    {
        "event_category": "other_material_event",
        "catalyst_description": "NVIDIA announced strategic compute-financing partnerships (naming Goldman Sachs among others) and named cloud partners running Vera Rubin racks (including CoreWeave).",
        "entities": [
            {"entity_name": "NVIDIA", "role": "issuer",
             "evidence_span": "NVIDIA (NASDAQ&#58; NVDA) today reported revenue"},
            {"entity_name": "Goldman Sachs", "role": "partner",
             "evidence_span": "strategic partnerships to establish independent compute financing platforms with Apollo, BlackRock, Blackstone, Brookfield, Goldman Sachs and KKR"},
            {"entity_name": "CoreWeave", "role": "partner",
             "evidence_span": "racks running at partners including CoreWeave, Google Cloud, Microsoft Azure, Oracle Cloud Infrastructure and Nebius"}
        ],
        "relationships": [
            {
                "entity_a": "NVIDIA", "entity_b": "Goldman Sachs", "relationship_type": "partner",
                "relationship_evidence": "explicit_named", "source_authority": "company",
                "document_explicitly_states_transmission_history": False,
                "evidence_span": "strategic partnerships to establish independent compute financing platforms with Apollo, BlackRock, Blackstone, Brookfield, Goldman Sachs and KKR",
                "raw_llm_relationship_score": 0.85
            },
            {
                "entity_a": "NVIDIA", "entity_b": "CoreWeave", "relationship_type": "partner",
                "relationship_evidence": "explicit_named", "source_authority": "company",
                "document_explicitly_states_transmission_history": False,
                "evidence_span": "racks running at partners including CoreWeave, Google Cloud, Microsoft Azure, Oracle Cloud Infrastructure and Nebius",
                "raw_llm_relationship_score": 0.85
            }
        ],
        "surprise": None,
        "source_published_at": "2026-08-26",
        "explicit_correction": False,
    },
])

print("Done.")
