---
name: coriell-sample-to-nde-json
description: Parse Coriell Sample_Detail pages (for USIDNET records) into the target NDE JSON structure using the ID00016 value-based metadata mapping and curated defaults.
---

# Coriell Sample To NDE JSON

Use this skill when a user asks to convert Coriell sample detail pages to the NDE JSON format.

## Files

- Parser script: `scripts/parse_coriell_sample.py`
- Value mapping reference: `references/mapping_ID00016_page_to_json.csv`
- Curated-only reference: `references/mapping_ID00016_curated_not_on_page.csv`

## Workflow

1. Parse from live page URL:
   - `python scripts/parse_coriell_sample.py --url "https://www.coriell.org/0/sections/Search/Sample_Detail.aspx?Ref=ID00016" --output ID00016_parsed.json`
2. Parse from saved HTML:
   - `python scripts/parse_coriell_sample.py --input-html ../../sample_detail.html --url "https://www.coriell.org/0/sections/Search/Sample_Detail.aspx?Ref=ID00016" --output ID00016_parsed.json`

## Mapping Notes

- Direct page extraction is used for identifier, disease text, age, sex, sample type/source, quantity, and key overview fields.
- Derived mapping combines page fields for `name` and `description`.
- `DefinedTerm` objects are name-focused: output only `@type` and `name` (no ontology URI/identifier fields).
- Curated defaults are still used for non-DefinedTerm metadata such as JSON-LD context and author block.
