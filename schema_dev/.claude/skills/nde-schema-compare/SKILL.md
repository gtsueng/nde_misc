---
name: nde-schema-compare
description: Class-by-class changelog between two versions of the NDE schema JSON-LD (by default NDE_schema_v2.json as NEW vs NDE_schema_working.json as OLD, in nde_misc/schema_dev). Use when the user asks what changed between NDE schema versions, wants a schema changelog or release notes, or wants to compare properties, marginality (required/recommended/optional), cardinality, or expected types across versions.
---

# NDE schema compare

`scripts/compare_schemas.py` diffs two NDE schema files class by class and writes a Markdown
changelog. You then add a holistic summary at the top. **Neither schema file is modified.**

## Run

Use the **nde** conda env's Python:

```powershell
& "C:\Users\gtsueng\Anaconda3\envs\nde\python.exe" "C:\Users\gtsueng\Anaconda3\envs\nde\nde_misc\schema_dev\.claude\skills\nde-schema-compare\scripts\compare_schemas.py"
```

- Arguments: `[NEW] [OLD]`. The defaults are `schema_dev/NDE_schema_v2.json` (NEW) and
  `schema_dev/NDE_schema_working.json` (OLD). The report is written from OLD → NEW, i.e. as changelog entries.
- Output: `schema_dev/schema_changelog_<new>_vs_<old>.md`. Use `--out PATH` to change it, and
  `--json PATH` to also save the raw diff.
- A file that isn't valid JSON is parsed with the tolerant parser from the sibling `nde-schema-check`
  skill. The report then starts with a ⚠ line listing the syntax errors. Mention this to the user and
  suggest running `nde-schema-check`.
- The script runs offline in a few seconds.

## What is compared

For each `rdfs:Class`, using only what is declared in that class's **own** `$validation`. Properties
merged in from a parent by biothings are not counted, but a change of `rdfs:subClassOf` is reported.

| Report section | Meaning |
|---|---|
| Class metadata | `rdfs:subClassOf`, `schema:schemaVersion`, `dct:provenance` changes |
| Added / Removed properties | Keys of `$validation.properties`, with marginality, cardinality and expected type |
| Marginality / cardinality | Movement between `required` / `recommended` / `optional` (or none of them, "unlisted"); `owl:cardinality` one ↔ many. Also names fixed for stray whitespace, and list entries that never matched a property |
| Expected-type changes | The set of accepted value shapes changed: JSON type + `format`, single vs `array of`, which object type (`$ref`s are resolved to the definition's `@type`, e.g. `object (ScholarlyArticle)`) |
| Restructured / nested-object changes | The top-level type is the same, but the definition was renamed or moved (e.g. `citation` → `citationobject`, or inline object → `adminarea`), or fields inside the nested object were added or dropped |
| Allowed values / vocabulary | `enum` / `const` values, and NDE `vocabulary` ontologies / `children_of` |
| Nested definition changes | `$validation.definitions`: new, removed and renamed definitions (a rename is detected when a removed and an added definition have the same object type), plus property, marginality, cardinality and type changes inside each one |
| Property records | `rdf:Property` records: added, removed, and `domainIncludes` / `rangeIncludes` changes |

"Changes shared by several classes" groups identical changes across classes. Most NDE changes are
made to shared definitions (`person`, `organization`, …) that are copied into every class, so this
section is the quickest way to see what really changed.

Description/abstract/label text is **not** compared.

## After running: write the holistic summary

The report contains the placeholder line
`<!-- HOLISTIC SUMMARY: written by Claude after reading the sections below -->` under `## Summary`.
Read the whole report, then replace that line (Edit tool) with:

1. 1–2 sentences on the overall nature of the release (e.g. mostly refactoring of shared definitions vs
   new modelling).
2. Bullets for the **themes**, each naming the classes affected. Group repeated changes into one bullet,
   e.g. "`citation` definition renamed `citationobject` in 6 classes; 8 citation-type properties now
   point to it". Lead with changes that affect data producers or curators: removed properties, stricter
   marginality, cardinality or type changes, parent-class changes, removed fields in nested objects.
   Then additions, then pure refactors and fixes.
3. Classes with no changes, in one line.
4. Anything that looks unintended (e.g. a property removed from one class but not its siblings, a `format`
   dropped from one class only). Phrase these as questions for the schema author, not as errors.

Only state what the report shows. Then give the user the summary and a link to the report.
