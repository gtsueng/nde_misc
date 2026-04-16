---
name: usidnet-coriell-crawler
description: Crawl Coriell USIDNET catalog pages and extract all USIDNET biobank sample detail URLs (Sample_Detail.aspx with Ref=ID...). Use this when asked to collect or refresh the full URL list of USIDNET samples.
---

# USIDNET Coriell Crawler

Use this skill when the user asks to retrieve all Coriell URLs for USIDNET biobanked samples.

## Workflow

1. Run the bundled crawler script:
   - `python scripts/crawl_usidnet_samples.py`
2. Default output is one URL per line in `usidnet_sample_urls.txt`.
3. Optional JSON output:
   - `python scripts/crawl_usidnet_samples.py --json usidnet_sample_urls.json`
4. If needed, add extra USIDNET seed pages:
   - `--seed-url "https://catalog.coriell.org/0/sections/BrowseCatalog/Genes.aspx?PgId=3&coll=ID"`
5. Edit dead-link skips in `known_dead_urls.txt` (one URL per line, `#` for comments).
6. Optional override for dead-link file:
   - `--known-dead-file path\\to\\dead_urls.txt`

## Notes

- The crawler is scoped to `catalog.coriell.org` and USIDNET-related paths.
- It collects only `Sample_Detail.aspx` URLs with `Ref=ID...`.
- Tuning knobs: `--max-pages`, `--delay`, `--timeout`, `--retries`.
