#!/usr/bin/env python3
"""Parse a Coriell sample detail page into the target NDE JSON format."""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from html import unescape
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urljoin
from urllib.request import Request, urlopen

DEFAULT_URL = "https://www.coriell.org/0/sections/Search/Sample_Detail.aspx?Ref=ID00016"

JSON_CONTEXT = {
    "schema": "http://schema.org/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "dct": "http://purl.org/dc/terms/",
    "bioschemas": "https://discovery.biothings.io/ns/bioschemas/",
    "bioschemastypesdrafts": "https://discovery.biothings.io/ns/bioschemastypesdrafts/",
    "niaid": "http://discovery.biothings.io/ns/niaid/",
    "nde": "https://discovery.biothings.io/ns/nde/",
}

AUTHOR_BLOCK = {
    "@type": "Person",
    "name": "Abigail Amberson",
    "familyName": "Amberson",
    "givenName": "Abigail",
    "affiliation": {
        "@type": "Thing",
        "name": "Coriell Institute for Medical Research",
        "sameAs": "https://ror.org/04npwsp41",
    },
    "title": "Senior Project Manager",
}

def fetch_html(url: str, timeout: float = 30.0) -> str:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def clean_text(value: str) -> str:
    value = unescape(value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = value.replace("\xa0", " ").replace("Âµ", "µ")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def extract(pattern: str, text: str, flags: int = re.IGNORECASE | re.DOTALL) -> Optional[str]:
    m = re.search(pattern, text, flags)
    if not m:
        return None
    return clean_text(m.group(1))


def parse_rows(html: str) -> Dict[str, str]:
    rows: Dict[str, str] = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.IGNORECASE | re.DOTALL):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.IGNORECASE | re.DOTALL)
        if len(tds) < 2:
            continue
        label = clean_text(tds[0]).rstrip(":")
        value = clean_text(tds[1])
        if not label or label == "&":
            continue
        if label.lower() in {"ncbi gene", "ncbi gtr", "omim", "omim description"}:
            continue
        if label not in rows:
            rows[label] = value
    return rows


def parse_quantity(raw_value: str) -> tuple[Optional[float], Optional[str]]:
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*(\S+)?", raw_value)
    if not m:
        return None, None
    number = float(m.group(1))
    unit = (m.group(2) or "").strip() or None
    if number.is_integer():
        number = int(number)
    return number, unit


def format_name_from_disease(disease: str, sample_type: str, product_source: str) -> str:
    base = disease
    if ";" in disease:
        first, second = [p.strip() for p in disease.split(";", 1)]
        if second:
            base = f"{first} ({second})"
        else:
            base = first
    return f"{base} {sample_type} from {product_source}"


def build_json(url: str, html: str) -> dict:
    rows = parse_rows(html)

    identifier = extract(r'<span\s+id="lblRef"[^>]*>(.*?)</span>', html) or ""
    product_type = extract(r'<span\s+class="pull-right"><b>(.*?)</b>\s+from\s+<b>.*?</b></span>', html) or ""
    product_source = extract(r'<span\s+class="pull-right"><b>.*?</b>\s+from\s+<b>(.*?)</b></span>', html) or ""
    disease_desc = extract(r'<span\s+title="([^"]+)"\s*>\s*DIGEORGE SYNDROME;\s*DGS\s*</span>', html)
    if not disease_desc:
        disease_desc = extract(r'<div\s+id="Description".*?<span\s+title="([^"]+)"', html)
    remarks = rows.get("Remarks", "")
    sex = extract(r'<span\s+id="lblGender"[^>]*>(.*?)</span>', html) or ""
    age_raw = extract(r'<span\s+id="lblAge"[^>]*>(.*?)</span>', html) or ""
    age_unit = extract(r'<span\s+id="lblAgeunit_id"[^>]*>(.*?)</span>', html) or ""

    repository = extract(r'<span\s+id="lblCollection_Name"[^>]*>(.*?)</span>', html) or ""
    biopsy_source = rows.get("Biopsy Source", "")
    cell_type = rows.get("Cell Type", "")
    tissue_type = rows.get("Tissue Type", "")
    subcollection = rows.get("Subcollection", "")
    sample_class = rows.get("Class", "")
    quantity_raw = rows.get("Quantity", "")
    quantity_value, quantity_unit = parse_quantity(quantity_raw)

    order_rel = extract(r'href="(/0/Sections/Collections/USIDNET/Ordering\.aspx\?PgId=162&coll=ID)"', html)
    usage_url = urljoin("https://www.coriell.org", order_rel) if order_rel else None

    condition_name = (disease_desc or "").title()

    description = disease_desc or ""
    if remarks:
        description = f"{description}. {remarks}" if description else remarks

    sample_available = "Add to Cart" in html
    is_free = False if "$" in html else True

    out = {
        "@type": "nde:Sample",
        "@context": JSON_CONTEXT,
        "name": format_name_from_disease(disease_desc or "", product_type or "", product_source or ""),
        "description": description,
        "identifier": identifier,
        "url": url,
        "usageInfo": {
            "@type": "CreativeWork",
            "url": usage_url,
            "name": "How To Order",
        },
        "conditionsOfAccess": "Open",
        "isAccessibleForFree": is_free,
        "includedInDataCatalog": {
            "@type": "DataCatalog",
            "name": "USIDNET DNA and Cell Repository",
            "url": "https://www.coriell.org/1/USIDNET",
            "versionDate": date.today().isoformat(),
        },
        "associatedPhenotype": {"@type": "DefinedTerm", "name": "Hypoplasia of the thymus"},
        "cellType": {"@type": "DefinedTerm", "name": cell_type},
        "healthCondition": {"@type": "DefinedTerm", "name": condition_name},
        "species": {
            "@type": "DefinedTerm",
            "name": "Homo sapiens",
        },
        "anatomicalSystem": {"@type": "DefinedTerm", "name": tissue_type},
        "anatomicalStructure": {"@type": "DefinedTerm", "name": biopsy_source},
        "sex": {"@type": "DefinedTerm", "name": sex},
        "developmentalStage": {
            "@type": "QuantitativeValue",
            "value": int(age_raw) if age_raw.isdigit() else age_raw,
            "unitText": "years old" if age_unit.upper() == "YR" else age_unit,
        },
        "sampleAvailability": sample_available,
        "sampleQuantity": {
            "@type": "QuantitativeValue",
            "value": quantity_value,
            "unitText": quantity_unit,
        },
        "sampleStorageTemperature": {
            "@type": "QuantitativeValue",
            "value": -20 if (product_type or "").upper() == "DNA" else None,
            "unitText": "C" if (product_type or "").upper() == "DNA" else None,
        },
        "sampleType": {"@type": "DefinedTerm", "name": product_type},
        "author": AUTHOR_BLOCK,
        "collector": {
            "@type": "Organization",
            "name": "USIDNET" if "USID" in repository.upper() else repository,
        },
        "keywords": [v for v in [subcollection, sample_class] if v],
        "creativeWorkStatus": "Available" if sample_available else "Unavailable",
        "additionalType": "BioSample",
    }

    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse Coriell Sample_Detail page into NDE JSON.")
    parser.add_argument("--url", default=DEFAULT_URL, help="Sample detail URL.")
    parser.add_argument("--input-html", help="Optional local HTML file instead of fetching URL.")
    parser.add_argument("--output", default="parsed_sample.json", help="Output JSON path.")
    args = parser.parse_args()

    html = Path(args.input_html).read_text(encoding="utf-8", errors="replace") if args.input_html else fetch_html(args.url)
    data = build_json(args.url, html)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"Wrote parsed JSON to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
