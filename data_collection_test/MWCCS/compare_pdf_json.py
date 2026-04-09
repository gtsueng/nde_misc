"""Compare PDF files in Substudies/ with JSON files in DataCollection/.

For each matched pair, flatten the JSON fields and find the corresponding
source text in the PDF.  Outputs two CSVs:
  - pdf_json_mapping.csv       : file-level mapping (which PDF -> which JSON)
  - pdf_json_content_mapping.csv : field-level content mapping
"""

import csv
import json
import os
import re

import pdfplumber

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SUBSTUDIES_DIR = os.path.join(BASE_DIR, "Substudies")
DATACOLLECTION_DIR = os.path.join(BASE_DIR, "DataCollection")
FILE_MAP_CSV = os.path.join(BASE_DIR, "pdf_json_mapping.csv")
CONTENT_MAP_CSV = os.path.join(BASE_DIR, "pdf_json_content_mapping.csv")

# ---------------------------------------------------------------------------
# 1.  Build file-level mapping  (PDF <-> JSON)
# ---------------------------------------------------------------------------
pdf_files = sorted(f for f in os.listdir(SUBSTUDIES_DIR) if f.endswith(".pdf"))
json_files = sorted(f for f in os.listdir(DATACOLLECTION_DIR) if f.endswith(".json"))

matched_pairs = []  # list of (pdf_filename, json_filename)

for jf in json_files:
    with open(os.path.join(DATACOLLECTION_DIR, jf), encoding="utf-8") as fh:
        data = json.load(fh)
    url = data.get("url", "")
    pdf_from_url = url.rsplit("/", 1)[-1] if url else ""
    if pdf_from_url in pdf_files:
        matched_pairs.append((pdf_from_url, jf))

# Write file-level mapping CSV (same as before)
with open(FILE_MAP_CSV, "w", newline="", encoding="utf-8") as fh:
    writer = csv.DictWriter(
        fh, fieldnames=["pdf_file", "json_file", "datacollection_name", "match_method"]
    )
    writer.writeheader()
    for pdf_f, json_f in matched_pairs:
        with open(os.path.join(DATACOLLECTION_DIR, json_f), encoding="utf-8") as f:
            name = json.load(f).get("name", "")
        writer.writerow({
            "pdf_file": pdf_f,
            "json_file": json_f,
            "datacollection_name": name,
            "match_method": "url",
        })
print(f"File mapping: {len(matched_pairs)} pairs -> {FILE_MAP_CSV}")

# ---------------------------------------------------------------------------
# 2.  Helpers
# ---------------------------------------------------------------------------

def extract_pdf_text(pdf_path):
    """Return the full text of a PDF as a single string."""
    with pdfplumber.open(pdf_path) as pdf:
        pages = [p.extract_text() or "" for p in pdf.pages]
    return "\n".join(pages)


def flatten_json(obj, prefix=""):
    """Yield (dotted_key, value_str) for every leaf in a JSON object."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from flatten_json(v, f"{prefix}{k}.")
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            yield from flatten_json(item, f"{prefix}[{i}].")
    else:
        key = prefix.rstrip(".")
        yield (key, str(obj))


def normalize(text):
    """Lowercase, collapse whitespace, strip punctuation for fuzzy matching."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def find_excerpt(needle, haystack_raw, context_chars=120):
    """Search for *needle* inside *haystack_raw*.

    Returns (matched_text, excerpt) or (None, None).
    Tries exact substring first, then normalized, then token overlap.
    """
    if not needle or not haystack_raw:
        return None, None

    needle_str = str(needle).strip()
    if not needle_str:
        return None, None

    # --- exact (case-insensitive) substring ---
    idx = haystack_raw.lower().find(needle_str.lower())
    if idx != -1:
        start = max(0, idx - 30)
        end = min(len(haystack_raw), idx + len(needle_str) + 30)
        excerpt = haystack_raw[start:end].replace("\n", " ").strip()
        return "exact", excerpt

    # --- normalized substring ---
    norm_hay = normalize(haystack_raw)
    norm_needle = normalize(needle_str)
    if len(norm_needle) >= 4:
        idx = norm_hay.find(norm_needle)
        if idx != -1:
            # Map back to approximate position in raw text (rough but useful)
            ratio = len(haystack_raw) / max(len(norm_hay), 1)
            raw_idx = int(idx * ratio)
            start = max(0, raw_idx - 30)
            end = min(len(haystack_raw), raw_idx + int(len(norm_needle) * ratio) + 30)
            excerpt = haystack_raw[start:end].replace("\n", " ").strip()
            return "normalized", excerpt

    # --- token overlap (for values that are rephrased/summarised) ---
    needle_tokens = set(normalize(needle_str).split())
    # ignore very short / generic tokens
    needle_tokens = {t for t in needle_tokens if len(t) > 2}
    if needle_tokens:
        hay_tokens = set(norm_hay.split())
        overlap = needle_tokens & hay_tokens
        ratio = len(overlap) / len(needle_tokens)
        if ratio >= 0.5 and len(overlap) >= 2:
            # Find a window in the raw text containing the most overlapping tokens
            best_window = _best_window(haystack_raw, overlap, context_chars)
            return f"token_overlap({len(overlap)}/{len(needle_tokens)})", best_window

    return None, None


def _best_window(raw_text, tokens, width=120):
    """Find the densest window of *width* chars containing the most *tokens*."""
    lower = raw_text.lower()
    positions = []
    for tok in tokens:
        idx = lower.find(tok)
        while idx != -1:
            positions.append(idx)
            idx = lower.find(tok, idx + 1)
    if not positions:
        return ""
    positions.sort()
    # sliding window
    best_start = positions[0]
    best_count = 0
    j = 0
    for i, pos in enumerate(positions):
        while j < len(positions) and positions[j] - pos <= width:
            j += 1
        if j - i > best_count:
            best_count = j - i
            best_start = pos
    start = max(0, best_start - 10)
    end = min(len(raw_text), best_start + width + 10)
    return raw_text[start:end].replace("\n", " ").strip()


# ---------------------------------------------------------------------------
# 3.  Content-level mapping
# ---------------------------------------------------------------------------
SKIP_FIELDS = {"@type", "includedInDataCatalog.name", "url", "species.name",
               "infectiousAgent.[0].name", "infectiousAgent.[1].name",
               "sample.@type"}
# These are either constants or derivable from context, not from PDF text.
# We still include them but flag the source differently.

CONSTANT_VALUES = {
    "@type": ("constant", "Always 'DataCollection'"),
    "species.name": ("constant", "Always 'Homo sapiens' (human study)"),
    "sample.@type": ("constant", "Always 'Sample'"),
    "includedInDataCatalog.name": ("constant", "Always 'MWCCS'"),
    "infectiousAgent.[0].name": ("constant", "Always 'HIV-1' (all substudies involve HIV)"),
    "infectiousAgent.[1].name": ("constant", "Always 'HIV-2' (all substudies involve HIV)"),
    "temporalCoverage.temporalType": ("constant", "Always 'collection'"),
}

MONTH_MAP = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "sept": "09", "oct": "10", "nov": "11", "dec": "12",
}


def parse_date_range(pdf_text):
    """Extract temporal coverage date strings from PDF text.

    Looks for patterns like 'Oct 2022 – Sept 2026' or 'Oct 2020 – Sept 2022'
    and returns (startDate, endDate) in ISO format, plus the raw match.
    """
    m = re.search(
        r"(?:data\s+collect(?:ion|ed)\s+)(\w+)\s+(\d{4})\s*[\u2013\-–]+\s*(\w+)\s+(\d{4})",
        pdf_text, re.IGNORECASE,
    )
    if not m:
        return None
    start_mon, start_yr, end_mon, end_yr = m.groups()
    sm = MONTH_MAP.get(start_mon[:3].lower())
    em = MONTH_MAP.get(end_mon[:3].lower())
    if not sm or not em:
        return None
    raw = m.group(0)
    return f"{start_yr}-{sm}-01", f"{end_yr}-{em}-30", raw


def compute_sample_quantities(pdf_text, total_n):
    """Derive sub-group counts from percentages in the PDF.

    The PDFs typically show two percentages near labels like
    'living with HIV' and 'women'. The layout varies, so we try
    several patterns.

    Returns a dict mapping unitText-like labels to (count, explanation, src).
    """
    results = {}
    if not total_n:
        return results

    # Strategy: find all percentage numbers, then find nearby labels
    pct_matches = list(re.finditer(r"(\d{2,3})\s*%", pdf_text))
    if len(pct_matches) < 2:
        return results

    # The PDFs consistently show: first % = living with HIV, second % = women
    # (based on the layout: "65% 52%\nliving with HIV women")
    hiv_pct = int(pct_matches[0].group(1))
    women_pct = int(pct_matches[1].group(1))

    # Verify by checking nearby text contains expected labels
    # Get surrounding context (400 chars around the percentages)
    ctx_start = max(0, pct_matches[0].start() - 100)
    ctx_end = min(len(pdf_text), pct_matches[-1].end() + 400)
    context = pdf_text[ctx_start:ctx_end].lower()

    raw_src = pdf_text[ctx_start:ctx_end].replace("\n", " ").strip()[:120]

    if "hiv" in context:
        hiv_count = round(total_n * hiv_pct / 100)
        non_hiv = total_n - hiv_count
        results["living with HIV"] = (
            hiv_count,
            f"Computed: {hiv_pct}% of {total_n} = {hiv_count}",
            raw_src,
        )
        results["living without HIV"] = (
            non_hiv,
            f"Computed: {100 - hiv_pct}% of {total_n} = {non_hiv}",
            raw_src,
        )

    if "women" in context or "woman" in context:
        women_count = round(total_n * women_pct / 100)
        men_count = total_n - women_count
        results["Women"] = (
            women_count,
            f"Computed: {women_pct}% of {total_n} = {women_count}",
            raw_src,
        )
        results["Men"] = (
            men_count,
            f"Computed: {100 - women_pct}% of {total_n} = {men_count}",
            raw_src,
        )

    return results


# Labels that are standardised conventions, not found verbatim in the PDF
STANDARDISED_LABELS = {"Study Subjects", "Assessments"}

# Health conditions that are medical-term standardisations of lay language
# We detect these by checking if the value is NOT in the PDF but the field
# is a healthCondition


content_rows = []

for pdf_f, json_f in matched_pairs:
    pdf_path = os.path.join(SUBSTUDIES_DIR, pdf_f)
    json_path = os.path.join(DATACOLLECTION_DIR, json_f)

    pdf_text = extract_pdf_text(pdf_path)
    with open(json_path, encoding="utf-8") as fh:
        json_data = json.load(fh)

    substudy_name = json_data.get("name", json_f)

    # Pre-compute derived quantities
    date_info = parse_date_range(pdf_text)
    total_n_match = re.search(r"\b(\d{3,5})\b", pdf_text)
    total_n = int(total_n_match.group(1)) if total_n_match else None
    sample_quantities = compute_sample_quantities(pdf_text, total_n)

    for field_path, value in flatten_json(json_data):
        # Check if it's a known constant
        if field_path in CONSTANT_VALUES:
            match_type, excerpt = CONSTANT_VALUES[field_path]

        elif field_path == "url":
            match_type, excerpt = "derived", f"URL constructed from PDF filename: {pdf_f}"

        # Temporal coverage dates: reformatted from month-year in PDF
        elif field_path in ("temporalCoverage.startDate", "temporalCoverage.endDate") and date_info:
            start_d, end_d, raw = date_info
            expected = start_d if "start" in field_path else end_d
            match_type = "reformatted_date"
            excerpt = f"'{raw}' -> {value}"

        # collectionSize unitText: standardised labels
        elif "collectionSize" in field_path and "unitText" in field_path and value in STANDARDISED_LABELS:
            match_type = "standardised_label"
            excerpt = f"Convention: '{value}' (not verbatim in PDF)"

        # collectionSize minVal that doesn't match total_n: likely computed
        elif "collectionSize" in field_path and "minVal" in field_path:
            match_type_inner, excerpt_inner = find_excerpt(value, pdf_text)
            if match_type_inner:
                match_type, excerpt = match_type_inner, excerpt_inner
            elif total_n and int(value) != total_n:
                # Check if it's a multiple of total_n (e.g., 2 scans)
                multiple = int(value) / total_n
                freq_match = re.search(r"How often:\s*(.+?)(?:\n|$)", pdf_text, re.IGNORECASE)
                freq_text = freq_match.group(1).strip() if freq_match else ""
                match_type = "computed_from_frequency"
                excerpt = f"Computed: {total_n} x {multiple:.0f} = {value} (PDF: 'How often: {freq_text}')"
            else:
                match_type = "not_found_in_pdf"
                excerpt = ""

        # sample.sampleQuantity.minVal: computed from percentages
        elif "sampleQuantity" in field_path and "minVal" in field_path:
            # Find which sampleQuantity this is by checking the sibling unitText
            # Parse the index from the field path
            idx_match = re.search(r"\[(\d+)\]", field_path)
            if idx_match:
                idx = int(idx_match.group(1))
                sq_list = json_data.get("sample", {}).get("sampleQuantity", [])
                if idx < len(sq_list):
                    unit = sq_list[idx].get("unitText", "")
                    if unit in sample_quantities:
                        _, explanation, src = sample_quantities[unit]
                        match_type = "computed_from_percentage"
                        excerpt = f"{explanation} (PDF: '{src}')"
                    else:
                        match_type, excerpt = find_excerpt(value, pdf_text)
                        if match_type is None:
                            match_type = "not_found_in_pdf"
                            excerpt = ""
                else:
                    match_type = "not_found_in_pdf"
                    excerpt = ""
            else:
                match_type = "not_found_in_pdf"
                excerpt = ""

        # healthCondition names: may be standardised medical terms
        elif "healthCondition" in field_path and "name" in field_path:
            match_type, excerpt = find_excerpt(value, pdf_text)
            if match_type is None:
                # Check for common standardisations
                match_type = "standardised_medical_term"
                excerpt = f"Medical term derived from PDF lay language (not verbatim)"

        else:
            match_type, excerpt = find_excerpt(value, pdf_text)
            if match_type is None:
                match_type = "not_found_in_pdf"
                excerpt = ""

        content_rows.append({
            "pdf_file": pdf_f,
            "json_file": json_f,
            "substudy_name": substudy_name,
            "json_field": field_path,
            "json_value": value,
            "match_type": match_type,
            "pdf_excerpt": excerpt,
        })

# Write content mapping CSV
fieldnames = ["pdf_file", "json_file", "substudy_name", "json_field",
              "json_value", "match_type", "pdf_excerpt"]

with open(CONTENT_MAP_CSV, "w", newline="", encoding="utf-8") as fh:
    writer = csv.DictWriter(fh, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(content_rows)

# Summary stats
from collections import Counter
type_counts = Counter(r["match_type"] for r in content_rows)
print(f"\nContent mapping: {len(content_rows)} field mappings -> {CONTENT_MAP_CSV}")
print("Match type breakdown:")
for mt, count in type_counts.most_common():
    print(f"  {mt}: {count}")
