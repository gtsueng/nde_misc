"""Compare PDF files in Substudies/ with JSON files in DataCollection/
and produce a CSV mapping between them."""

import csv
import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SUBSTUDIES_DIR = os.path.join(BASE_DIR, "Substudies")
DATACOLLECTION_DIR = os.path.join(BASE_DIR, "DataCollection")
OUTPUT_CSV = os.path.join(BASE_DIR, "pdf_json_mapping.csv")

pdf_files = sorted(f for f in os.listdir(SUBSTUDIES_DIR) if f.endswith(".pdf"))
json_files = sorted(f for f in os.listdir(DATACOLLECTION_DIR) if f.endswith(".json"))

# Build a lookup: extract the PDF filename from each JSON's url field
json_to_pdf = {}
json_metadata = {}
for jf in json_files:
    with open(os.path.join(DATACOLLECTION_DIR, jf)) as fh:
        data = json.load(fh)
    url = data.get("url", "")
    pdf_from_url = url.rsplit("/", 1)[-1] if url else ""
    json_to_pdf[jf] = pdf_from_url
    json_metadata[jf] = data.get("name", "")

# Also try filename-based matching: lowercase, replace '.' with '_', swap extension
def pdf_to_expected_json(pdf_name):
    base = os.path.splitext(pdf_name)[0]
    return base.lower().replace(".", "_") + ".json"

rows = []

# Match PDFs to JSONs
matched_pdfs = set()
matched_jsons = set()

for jf, pdf_from_url in json_to_pdf.items():
    if pdf_from_url in pdf_files:
        rows.append({
            "pdf_file": pdf_from_url,
            "json_file": jf,
            "datacollection_name": json_metadata[jf],
            "match_method": "url",
        })
        matched_pdfs.add(pdf_from_url)
        matched_jsons.add(jf)

# Fallback: filename-based matching for any unmatched
for pf in pdf_files:
    if pf not in matched_pdfs:
        expected = pdf_to_expected_json(pf)
        if expected in json_files and expected not in matched_jsons:
            with open(os.path.join(DATACOLLECTION_DIR, expected)) as fh:
                data = json.load(fh)
            rows.append({
                "pdf_file": pf,
                "json_file": expected,
                "datacollection_name": data.get("name", ""),
                "match_method": "filename",
            })
            matched_pdfs.add(pf)
            matched_jsons.add(expected)

# Report unmatched
for pf in pdf_files:
    if pf not in matched_pdfs:
        rows.append({
            "pdf_file": pf,
            "json_file": "",
            "datacollection_name": "",
            "match_method": "unmatched_pdf",
        })

for jf in json_files:
    if jf not in matched_jsons:
        rows.append({
            "pdf_file": "",
            "json_file": jf,
            "datacollection_name": json_metadata.get(jf, ""),
            "match_method": "unmatched_json",
        })

rows.sort(key=lambda r: r["pdf_file"] or r["json_file"])

with open(OUTPUT_CSV, "w", newline="") as fh:
    writer = csv.DictWriter(fh, fieldnames=["pdf_file", "json_file", "datacollection_name", "match_method"])
    writer.writeheader()
    writer.writerows(rows)

print(f"Wrote {len(rows)} rows to {OUTPUT_CSV}")
for r in rows:
    print(f"  [{r['match_method']}] {r['pdf_file']} <-> {r['json_file']} ({r['datacollection_name']})")
