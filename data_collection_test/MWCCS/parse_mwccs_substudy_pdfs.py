#!/usr/bin/env python3
"""Parse MWCCS substudy visual abstract PDFs into NDE-style DataCollection objects."""

from __future__ import annotations

import argparse
import calendar
import json
import re
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from pypdf import PdfReader


SUBSTUDY_PAGE_URL = "https://statepi.jhsph.edu/mwccs/substudy-science/"
USER_AGENT = "mwccs-substudy-parse/1.0"
MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
}
STUDY_OVERRIDES = {
    "fibroscan substudy": {
        "healthCondition": ["liver fibrosis", "liver steatosis", "HIV infection"],
        "infectiousAgent": ["HIV-1", "HIV-2"],
        "measurementTechnique": [
            "Fibroscan with Controlled Attenuation Parameter (CAP)",
            "ultrasound",
        ],
        "variableMeasured": [
            "amount of liver fibrosis (liver scarring)",
            "amount of liver steatosis (fat)",
        ],
        "sample_exclusions": ["participants with implant devices", "pregnant participants"],
    },
    "echocardiogram substudy": {
        "healthCondition": ["cardiovascular disease", "heart disease", "HIV infection"],
        "infectiousAgent": ["HIV-1", "HIV-2"],
    },
    "brief cognitive function substudy": {
        "healthCondition": ["cognitive impairment", "HIV infection"],
        "infectiousAgent": ["HIV-1", "HIV-2"],
    },
    "full cognitive function substudy": {
        "healthCondition": ["cognitive impairment", "HIV infection"],
        "infectiousAgent": ["HIV-1", "HIV-2"],
    },
    "hearing and balance substudy": {
        "healthCondition": ["hearing disorder", "vertigo", "HIV infection"],
        "infectiousAgent": ["HIV-1", "HIV-2"],
    },
    "microbiome substudy": {
        "healthCondition": [
            "gut microbiome alteration",
            "insulin resistance",
            "diabetes mellitus",
            "hypercholesterolemia",
            "hypertension",
            "fatty liver disease",
            "emphysema",
            "chronic obstructive pulmonary disease",
            "HIV infection",
        ],
        "infectiousAgent": ["HIV-1", "HIV-2"],
    },
    "pulmonary function substudy": {
        "healthCondition": ["lung disease", "pulmonary disease", "HIV infection"],
        "infectiousAgent": ["HIV-1", "HIV-2"],
    },
    "tooth count substudy": {
        "healthCondition": ["oral disease", "dental disease", "HIV infection"],
        "infectiousAgent": ["HIV-1", "HIV-2"],
    },
    "mental health and substance use substudy": {
        "healthCondition": ["mental disorder", "substance use disorder", "HIV infection"],
        "infectiousAgent": ["HIV-1", "HIV-2"],
    },
}


@dataclass
class ParsedStudy:
    name: str
    data_phrase: str
    what_is_measured: str
    measures: list[str]
    how_often: str
    participant_count: int | None
    participant_description: str
    median_age: int | None
    hiv_percent: int | None
    women_percent: int | None
    learning_points: list[str]
    authors: list[str]
    source_url: str | None
    pdf_path: Path


class PdfLinkParser(HTMLParser):
    """Collect substudy PDF links from the MWCCS substudy page."""

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.links: dict[str, str] = {}
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self._href = urljoin(self.base_url, href)
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._href is None:
            return
        text = " ".join(part.strip() for part in self._text if part.strip())
        if self._href.lower().endswith(".pdf") and "substudy" in text.lower():
            self.links[Path(urlparse(self._href).path).name] = self._href
        self._href = None
        self._text = []


def fetch_text(url: str, timeout: int) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_source_urls(page_url: str, timeout: int) -> dict[str, str]:
    try:
        parser = PdfLinkParser(base_url=page_url)
        parser.feed(fetch_text(page_url, timeout=timeout))
        return parser.links
    except Exception:
        return {}


def read_pdf_text(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    parts = [page.extract_text() or "" for page in reader.pages]
    text = "\n".join(parts)
    text = text.replace("\u2010", "-").replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")
    text = text.replace("\u00ae", "")
    text = text.replace("\uf071", " ")
    return text


def normalize_whitespace(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text).strip()


def collapse_value(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def clean_lines(text: str) -> list[str]:
    return [normalize_whitespace(line) for line in text.splitlines() if normalize_whitespace(line)]


def normalize_study_key(name: str) -> str:
    return normalize_whitespace(name).lower()


def slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()


def find_first_number_line(lines: list[str]) -> tuple[int | None, int | None]:
    for index, line in enumerate(lines):
        if re.fullmatch(r"\d{3,6}", line):
            return int(line), index
    return None, None


def extract_date_phrase(text: str) -> str:
    match = re.search(r"\((data collect(?:ed|ion)\s+[^)]+)\)", text, flags=re.IGNORECASE)
    if not match:
        return ""
    return normalize_whitespace(match.group(1))


def label_to_pattern(label: str) -> str:
    parts = [re.escape(part) for part in label.split()]
    return r"\s+".join(parts)


def extract_named_section(text: str, label: str, next_labels: list[str]) -> str:
    label_pattern = label_to_pattern(label)
    next_pattern = "|".join(label_to_pattern(item) for item in next_labels)
    pattern = label_pattern + r"\s*:?[\s\n]*(.*?)\s*(?=" + next_pattern + r")"
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return normalize_whitespace(match.group(1))


def split_measure_items(measures_text: str) -> list[str]:
    if not measures_text:
        return []

    raw_lines = [line.rstrip() for line in measures_text.splitlines() if line.strip()]
    items: list[str] = []

    for raw_line in raw_lines:
        stripped = raw_line.strip()
        is_bullet = stripped.startswith("•")
        clean = collapse_value(stripped.lstrip("•").strip())
        if not clean:
            continue

        if is_bullet:
            items.append(clean)
            continue

        if not items:
            items.append(clean)
            continue

        items[-1] = collapse_value(f"{items[-1]} {clean}")

    return [item for item in items if item]


def parse_learning_points(text: str) -> list[str]:
    section = extract_named_section(
        text,
        "What can we learn from the collected data?",
        ["Scientific lead(s) for this study:"],
    )
    if not section:
        return []

    raw_lines = [line.rstrip() for line in section.splitlines() if line.strip()]
    if any(line.strip().startswith(("", "")) for line in raw_lines):
        points: list[str] = []
        for raw_line in raw_lines:
            stripped = raw_line.strip()
            is_bullet = stripped.startswith(("", ""))
            clean = collapse_value(stripped.lstrip("").strip())
            if not clean:
                continue
            if is_bullet or not points:
                points.append(clean)
            else:
                points[-1] = collapse_value(f"{points[-1]} {clean}")
        return points

    lines = [normalize_whitespace(line) for line in section.splitlines() if normalize_whitespace(line)]
    if len(lines) <= 1:
        return lines

    merged: list[str] = []
    for line in lines:
        if not merged:
            merged.append(line)
            continue

        if re.match(r"^(and\b|or\b|[a-z])", line):
            merged[-1] = collapse_value(f"{merged[-1]} {line}")
        else:
            merged.append(line)

    return merged


def parse_author_names(text: str) -> list[str]:
    match = re.search(
        r"Scientific lead\(s\) for this study:\s*(.*?)\s*Learn more about MWCCS",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return []

    block = normalize_whitespace(match.group(1))
    block = block.replace(", and ", ", ").replace(" and ", ", ")
    names = []
    for part in [item.strip() for item in block.split(",") if item.strip()]:
        clean = re.sub(r"^Dr\.?\s*", "", part).strip()
        if clean:
            names.append(clean)
    return names


def month_to_number(value: str) -> int:
    return MONTHS[value.strip().lower()]


def parse_temporal_coverage(date_phrase: str) -> dict[str, str] | None:
    match = re.search(
        r"data collect(?:ed|ion)\s+([A-Za-z]+)\s+(\d{4})\s*-\s*([A-Za-z]+)\s+(\d{4})",
        date_phrase,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    start_month = month_to_number(match.group(1)[:4].lower())
    start_year = int(match.group(2))
    end_month = month_to_number(match.group(3)[:4].lower())
    end_year = int(match.group(4))
    end_day = calendar.monthrange(end_year, end_month)[1]
    return {
        "startDate": f"{start_year:04d}-{start_month:02d}-01",
        "endDate": f"{end_year:04d}-{end_month:02d}-{end_day:02d}",
        "temporalType": "collection",
    }


def compute_count_from_percent(total: int | None, percent: int | None) -> int | None:
    if total is None or percent is None:
        return None
    return round(total * percent / 100)


def title_case_phrase(value: str) -> str:
    value = normalize_whitespace(value)
    return value[:1].upper() + value[1:] if value else value


def make_property_values(values: Iterable[str]) -> list[dict[str, str]]:
    seen: set[str] = set()
    items = []
    for value in values:
        clean = collapse_value(value)
        if not clean:
            continue
        lowered = clean.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        items.append({"name": clean})
    return items


def infer_health_conditions(parsed: ParsedStudy, override: dict[str, object]) -> list[dict[str, str]]:
    explicit = override.get("healthCondition")
    if isinstance(explicit, list):
        return make_property_values(str(item) for item in explicit)

    inferred = []
    measured = parsed.what_is_measured.lower()
    if "heart" in measured:
        inferred.append("heart disease")
    if "liver" in measured:
        inferred.extend(["liver disease", "HIV infection"])
    if "cognitive" in measured:
        inferred.append("cognitive impairment")
    if "hearing" in measured:
        inferred.append("hearing disorder")
    if "balance" in measured:
        inferred.append("balance disorder")
    if "pulmonary" in measured or "lung" in measured:
        inferred.append("lung disease")
    if "tooth" in measured or "oral" in measured:
        inferred.append("oral disease")
    if "mental health" in measured:
        inferred.append("mental disorder")
    if "substance" in measured:
        inferred.append("substance use disorder")
    if "hiv" in parsed.participant_description.lower() or "hiv" in " ".join(parsed.learning_points).lower():
        inferred.append("HIV infection")
    return make_property_values(inferred)


def infer_measurement_techniques(parsed: ParsedStudy, override: dict[str, object]) -> list[dict[str, str]]:
    explicit = override.get("measurementTechnique")
    if isinstance(explicit, list):
        return make_property_values(str(item) for item in explicit)
    return make_property_values(parsed.measures)


def infer_variable_measured(parsed: ParsedStudy, override: dict[str, object]) -> list[dict[str, str]]:
    explicit = override.get("variableMeasured")
    if isinstance(explicit, list):
        return make_property_values(str(item) for item in explicit)

    values = [parsed.what_is_measured]
    for point in parsed.learning_points:
        if point.lower().startswith("how common"):
            values.append(point)
    return make_property_values(values)


def infer_infectious_agents(parsed: ParsedStudy, override: dict[str, object]) -> list[dict[str, str]]:
    explicit = override.get("infectiousAgent")
    if isinstance(explicit, list):
        return make_property_values(str(item) for item in explicit)

    combined = " ".join([parsed.participant_description, parsed.what_is_measured] + parsed.learning_points)
    if "HIV" in combined:
        return make_property_values(["HIV"])
    return []


def parse_frequency_multiplier(how_often: str) -> tuple[int | None, str | None]:
    lowered = how_often.lower()
    if "one time" in lowered:
        return 1, "Assessment"

    for word, number in NUMBER_WORDS.items():
        if word in lowered and ("scan" in lowered or "visit" not in lowered):
            unit = "Scan" if "scan" in lowered else "Assessment"
            return number, unit

    return None, None


def parse_study(pdf_path: Path, source_url: str | None) -> ParsedStudy:
    text = read_pdf_text(pdf_path)
    lines = clean_lines(text)
    if not lines:
        raise ValueError(f"No text extracted from {pdf_path}")

    participant_count, participant_index = find_first_number_line(lines)
    participant_description = ""
    if participant_index is not None:
        collected: list[str] = []
        for line in lines[participant_index + 1 :]:
            lowered = line.lower()
            if re.search(r"\d+\s+years old.*median age", lowered):
                break
            if re.fullmatch(r"\d+%", line):
                break
            if lowered.startswith("what can we learn") or lowered.startswith("scientific lead"):
                break
            collected.append(line)
        participant_description = collapse_value(" ".join(collected))

    median_age = None
    hiv_percent = None
    women_percent = None
    for index, line in enumerate(lines):
        age_match = re.search(r"(\d+)\s+years old.*median age", line, flags=re.IGNORECASE)
        if age_match and median_age is None:
            median_age = int(age_match.group(1))
        percent_match = re.fullmatch(r"(\d+)%", line)
        if percent_match:
            next_line = lines[index + 1] if index + 1 < len(lines) else ""
            percent = int(percent_match.group(1))
            if "living with hiv" in next_line.lower() and hiv_percent is None:
                hiv_percent = percent
            elif "women" in next_line.lower() and women_percent is None:
                women_percent = percent

    return ParsedStudy(
        name=lines[0],
        data_phrase=extract_date_phrase(text),
        what_is_measured=extract_named_section(
            text,
            "What is being measured",
            ["Measures:", "Measure:", "How often:"],
        ),
        measures=split_measure_items(
            extract_named_section(
                text,
                "Measures",
                ["How often:"],
            )
            or extract_named_section(
                text,
                "Measure",
                ["How often:"],
            )
        ),
        how_often=extract_named_section(
            text,
            "How often",
            ["Who did we collect", "What can we learn from the collected data?"],
        ),
        participant_count=participant_count,
        participant_description=participant_description,
        median_age=median_age,
        hiv_percent=hiv_percent,
        women_percent=women_percent,
        learning_points=parse_learning_points(text),
        authors=parse_author_names(text),
        source_url=source_url,
        pdf_path=pdf_path,
    )


def make_author_object(name: str) -> dict[str, str]:
    parts = [part for part in name.split() if part]
    if not parts:
        return {"name": name}
    return {
        "name": name,
        "givenName": parts[0],
        "familyName": parts[-1],
    }


def build_description(parsed: ParsedStudy) -> str:
    participant_sentence = parsed.participant_description
    if parsed.participant_count is not None:
        participant_sentence = f"Data collected from {parsed.participant_count} {participant_sentence}"
    else:
        participant_sentence = f"Data collected from {participant_sentence}"

    if parsed.learning_points:
        return collapse_value(
            participant_sentence + ". This data can help researchers learn more about " + "; ".join(parsed.learning_points)
        )

    return collapse_value(participant_sentence)


def build_collection_size(parsed: ParsedStudy) -> list[dict[str, object]]:
    sizes: list[dict[str, object]] = []
    if parsed.participant_count is not None:
        sizes.append({"minVal": parsed.participant_count, "unitText": "Study Subjects"})

    multiplier, unit = parse_frequency_multiplier(parsed.how_often)
    if parsed.participant_count is not None and multiplier and unit:
        count = parsed.participant_count * multiplier
        unit_text = f"{unit}s" if count != 1 else unit
        sizes.append({"minVal": count, "unitText": unit_text})

    return sizes


def build_sample(parsed: ParsedStudy, override: dict[str, object]) -> dict[str, object]:
    sample = {
        "@type": "Sample",
        "sex": ["Men", "Women"],
    }

    quantities: list[dict[str, object]] = []
    hiv_count = compute_count_from_percent(parsed.participant_count, parsed.hiv_percent)
    if hiv_count is not None:
        quantities.append({"minVal": hiv_count, "unitText": "living with HIV"})
        without_hiv = parsed.participant_count - hiv_count if parsed.participant_count is not None else None
        if without_hiv is not None:
            quantities.append({"minVal": without_hiv, "unitText": "living without HIV"})

    women_count = compute_count_from_percent(parsed.participant_count, parsed.women_percent)
    if women_count is not None:
        quantities.append({"minVal": women_count, "unitText": "Women"})
        men_count = parsed.participant_count - women_count if parsed.participant_count is not None else None
        if men_count is not None:
            quantities.append({"minVal": men_count, "unitText": "Men"})

    if parsed.median_age is not None:
        sample["age"] = {"minVal": parsed.median_age, "unitText": "years", "valueType": "median"}

    exclusions = override.get("sample_exclusions")
    if isinstance(exclusions, list) and exclusions:
        sample["description"] = "Excluded: " + "; ".join(str(item) for item in exclusions)

    if quantities:
        sample["sampleQuantity"] = quantities

    return sample


def build_datacollection(parsed: ParsedStudy) -> dict[str, object]:
    override = STUDY_OVERRIDES.get(normalize_study_key(parsed.name), {})
    obj: dict[str, object] = {
        "@type": "DataCollection",
        "name": parsed.name,
        "description": build_description(parsed),
        "species": {"name": "Homo sapiens"},
        "measurementTechnique": infer_measurement_techniques(parsed, override),
        "variableMeasured": infer_variable_measured(parsed, override),
        "author": [make_author_object(name) for name in parsed.authors],
        "collectionSize": build_collection_size(parsed),
        "sample": build_sample(parsed, override),
    }

    temporal_coverage = parse_temporal_coverage(parsed.data_phrase)
    if temporal_coverage:
        obj["temporalCoverage"] = temporal_coverage

    health_condition = infer_health_conditions(parsed, override)
    if health_condition:
        obj["healthCondition"] = health_condition

    infectious_agent = infer_infectious_agents(parsed, override)
    if infectious_agent:
        obj["infectiousAgent"] = infectious_agent

    if parsed.source_url:
        obj["url"] = parsed.source_url

    obj["includedInDataCatalog"] = {"name": "MWCCS"}
    return obj


def write_datacollection_files(results: list[tuple[ParsedStudy, dict[str, object]]], output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written_paths: list[Path] = []

    for parsed, obj in results:
        output_path = output_dir / f"{slugify(parsed.pdf_path.stem)}.json"
        output_path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
        written_paths.append(output_path)

    return written_paths


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    default_input = Path(__file__).resolve().with_name("Substudies")
    default_output = Path(__file__).resolve().with_name("DataCollection")
    parser = argparse.ArgumentParser(
        description="Parse MWCCS substudy PDFs into per-study JSON DataCollection objects."
    )
    parser.add_argument(
        "--input-dir",
        default=str(default_input),
        help="Directory containing MWCCS substudy PDFs.",
    )
    parser.add_argument(
        "--output",
        default=str(default_output),
        help="Output directory for per-study JSON files. Default: MWCCS/DataCollection",
    )
    parser.add_argument(
        "--page-url",
        default=SUBSTUDY_PAGE_URL,
        help=f"MWCCS substudy page URL used to recover source PDF links. Default: {SUBSTUDY_PAGE_URL}",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Network timeout in seconds for source-link lookup. Default: 60",
    )
    return parser.parse_args(list(argv))


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)
    input_dir = Path(args.input_dir).resolve()
    output_dir = Path(args.output).resolve()

    if not input_dir.exists():
        print(f"Input directory not found: {input_dir}", file=sys.stderr)
        return 2

    pdf_paths = sorted(input_dir.glob("*.pdf"))
    if not pdf_paths:
        print(f"No PDFs found in {input_dir}", file=sys.stderr)
        return 2

    source_urls = fetch_source_urls(args.page_url, timeout=args.timeout)
    results = []
    for pdf_path in pdf_paths:
        parsed = parse_study(pdf_path, source_urls.get(pdf_path.name))
        results.append((parsed, build_datacollection(parsed)))

    written_paths = write_datacollection_files(results, output_dir)
    print(f"Wrote {len(written_paths)} DataCollection JSON files to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
