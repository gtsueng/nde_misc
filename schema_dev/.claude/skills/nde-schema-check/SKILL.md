---
name: nde-schema-check
description: Validate the NDE schema JSON-LD file (NDE_schema_v2.json in nde_misc/schema_dev) and report exact line numbers with explanations. Use when the user asks to check, validate, lint, or debug the NDE schema, or when json.load / biothings_schema fails on it. Checks unclosed or mismatched brackets and braces, unnecessary commas, stray leading/trailing spaces in property names and requirement lists, missing $validation definitions, property inheritance from parent classes, and the cause of any biothings_schema error.
---

# NDE schema check

Runs `scripts/check_schema.py` against an NDE schema file and writes a Markdown report with
the exact line, an explanation and a suggested fix for each problem. **The schema file is
never modified.** Fixes are made by the user (or by you, if the user asks).

## How to run

Always use the **nde** conda env's Python, which has `biothings_schema` and `jsonschema`:

```powershell
& "C:\Users\gtsueng\Anaconda3\envs\nde\python.exe" "C:\Users\gtsueng\Anaconda3\envs\nde\nde_misc\schema_dev\.claude\skills\nde-schema-check\scripts\check_schema.py" "C:\Users\gtsueng\Anaconda3\envs\nde\nde_misc\schema_dev\NDE_schema_v2.json"
```

- The default schema is `schema_dev/NDE_schema_v2.json`. Pass another path to check a different file.
- The report goes to `<schema name>_check_report.md` next to the schema. Use `--out PATH` to write it elsewhere.
- `--skip-biothings`: no network access needed. Runs everything except step 4 in a few seconds.
- `--include-published`: also loads the currently published `nde` schema from DDE as a base, which is
  what DDE does by default. Without this flag, the new file is validated **on its own**, which is the
  stricter check (see "Known biothings quirks").
- The biothings step downloads schema.org, bioschemas, niaid, etc. and takes about 1–3 minutes. Run it in
  the background, or with a long timeout.
- Exit code: 1 if there are any ERRORs, 0 otherwise.

## What it checks

| # | Section in report | What | Severity |
|---|---|---|---|
| 1 | Syntax | Unclosed / mismatched / extra `{ } [ ]`; unterminated strings (missing closing `"`); stray unquoted text; missing `:`; duplicate keys (JSON silently keeps only the last one) | ERROR |
| 2 | Commas | Trailing comma before `}`/`]`, double commas, a comma right after `{`/`[`, missing commas | ERROR |
| 3 | Stray whitespace in names | Leading/trailing spaces (or tabs) in: property names (keys of any `properties`), definition names, entries in `required`/`recommended`/`optional`, `@id` / `rdfs:label` / `$ref` values, and any other key. Example: `"givenName "` in `recommended` matches no property, so the requirement silently has no effect, and the report says so when the stripped name exists. Spaces *inside* a property, definition or list name are a WARNING | ERROR / WARNING |
| 4 | `$validation` | Every `"$ref": "#/definitions/X"` resolves inside that class's `definitions`. If `X` exists only in a parent class: WARNING (biothings merges it, but standalone JSON Schema tools don't). Names in `required`/`recommended`/`optional` exist in the sibling `properties` (at every nesting level). A name in more than one of those lists. Whether each `$validation` is valid draft-07 JSON Schema. Unused definitions (INFO) | ERROR / WARNING |
| 5 | Records & inheritance | `@id` matches `rdfs:label`; prefixes are declared in `@context`; `@context` prefix IRIs match the IRI the registered DDE schema uses; `rdfs:subClassOf` targets exist; no cycles; each `$validation` property's `schema:domainIncludes` includes the class or an ancestor; parent/child `$validation` conflicts | ERROR / WARNING |
| 6 | biothings_schema | Runs `biothings_schema.Schema(...)` (as in `test_file.py`) and collects **all** errors, not just the first. Each one is mapped to a line, with its cause. Knock-on errors are grouped under their root cause | ERROR / WARNING |

### How syntax errors are located

`json.load` stops at the first error and often reports it far from the real cause. This script:

1. Tokenizes the file tolerantly (an unterminated string is closed at the end of its line).
2. Matches brackets using **indentation**. In this pretty-printed file, a block's closer is at the same
   indentation as the line that opened it. Where that stops being true is where a brace went missing
   (the closer is shallower than the open block) or was added (a block is closed twice).
3. Repairs each problem in memory, keeping line numbers unchanged, and continues. That way all syntax
   errors are reported in one run, and the semantic checks still run on the repaired copy.

The file has a few cosmetic over-indented closers (e.g. line 2802). These are tolerated and not reported.

### Parent/child inheritance (how biothings merges)

`biothings_schema` merges the parent's `$validation` into the child's, recursively for in-file parents:
- **Scalars:** the child's value wins.
- **Lists** (`anyOf`, `oneOf`, `enum`, `required`, `recommended`, `optional`, …): the two are **unioned**.
  A child cannot narrow an inherited list, and a property that is `required` in the parent stays required.
  The report flags every place where a child's list differs from its parent's.
- **Properties:** the child inherits the parent's properties even if it doesn't list them, and cannot remove them (INFO).

## Known biothings quirks (built into the script)

1. **Registry limit.** `BaseSchemaLoader.registered_dde_schemas` only requests `?size=20`, but the DDE
   registry has more than 20 schemas, and `niaid` is not in the first 20. As a result, niaid is never
   loaded, every `niaid:`-derived class gets `no_path_to_root`, and every property in those classes is
   then rejected. The script uses a loader that lists the whole registry.
2. **Published version masks problems.** Because `nde` is registered in DDE, biothings loads the
   *published* NDE schema as a base by default. A property is then accepted if the **old** version
   allows it on the class. The script excludes the file's own namespace unless `--include-published` is given.
3. **@context prefix IRIs.** The script compares each prefix in `@context` with the IRI that the
   registered schema of the same name uses (e.g. `niaid` is `https://…` in DDE). If they differ, it reports
   a WARNING, corrects the prefix in memory and only then runs biothings, so that real errors aren't
   buried under the resulting cascade.
4. Biothings errors are matched to a property record **by `rdfs:label`, across this file and all base
   schemas**. A record with that label but no `schema:domainIncludes` makes biothings crash with
   `KeyError: 'http://schema.org/domainIncludes'`. The script catches such crashes and names the culprit.

## Presenting results to the user

1. Run the script. Then read the generated report, not just the console output.
2. Summarize for the user: whether the file is valid JSON, counts by severity, then the ERRORs, grouped
   by section and listed with line numbers. Then the WARNINGs, grouped where they share a cause (e.g.
   "`role` listed in `optional` but missing from `researchproject.properties` in 6 classes").
3. Link the report file. Don't paste the whole report.
4. Don't edit the schema unless the user asks. If asked, fix syntax errors first and re-run: line numbers
   don't change for in-line fixes, but a fix that adds or removes lines shifts them.
5. Treat WARNINGs about inheritance and requirement levels as points for the schema author to decide,
   not definite bugs. Some are intentional.
