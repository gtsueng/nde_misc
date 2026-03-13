#!/usr/bin/env python3
"""Fetch the full EMDB organism list via the paged search API.

The output is one row per NCBI taxonomy code. Organism names are only kept
when the observed EMDB mapping is unambiguous enough to trust; otherwise the
name is left blank rather than risking a wrong assignment.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from collections import Counter, defaultdict
from typing import Dict, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://www.ebi.ac.uk/emdb/api/search/natural_source_ncbi_code%3A%5B*%20TO%20*%5D"
DEFAULT_ROWS = 10000
DEFAULT_OUTPUT = "all_emdb_ncbi_taxonomy_codes.csv"


def fetch_page(page: int, rows: int, timeout: int) -> list[dict[str, str]]:
    params = {
        "rows": rows,
        "page": page,
        "fl": "natural_source_ncbi_code,natural_source_organism",
    }
    url = f"{BASE_URL}?{urlencode(params)}"
    request = Request(url, headers={"Accept": "text/csv", "User-Agent": "emdb-organism-fetch/1.0"})

    with urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8")

    reader = csv.DictReader(text.splitlines())
    return [row for row in reader if row.get("natural_source_ncbi_code")]


def split_names(value: str) -> list[str]:
    if not value:
        return []
    parts = re.split(r"(?<!\\),", value)
    return [part.replace(r"\,", ",").strip() for part in parts if part.strip()]


def iter_code_name_pairs(row: dict[str, str]) -> Iterable[tuple[str, str]]:
    codes = [part.strip() for part in row["natural_source_ncbi_code"].split(",") if part.strip()]
    names = split_names(row.get("natural_source_organism", ""))

    if len(names) == len(codes):
        return zip(codes, names)

    if len(names) == 1 and len(codes) == 1:
        return [(codes[0], names[0])]

    return [(code, "") for code in codes]


def choose_trusted_name(counter: Counter[str]) -> str:
    if not counter:
        return ""

    if len(counter) == 1:
        return next(iter(counter))

    ranked = counter.most_common()
    top_name, top_count = ranked[0]
    second_count = ranked[1][1]
    total = sum(counter.values())

    # Only keep a name if it is clearly dominant; otherwise leave it blank.
    if top_count >= 3 and top_count >= second_count * 3 and top_count / total >= 0.75:
        return top_name

    return ""


def collect_organisms(rows: int, timeout: int, pause: float) -> Dict[str, str]:
    name_counts: dict[str, Counter[str]] = defaultdict(Counter)
    page = 1
    total_rows = 0

    while True:
        batch = fetch_page(page=page, rows=rows, timeout=timeout)
        if not batch:
            break

        total_rows += len(batch)
        for row in batch:
            for code, name in iter_code_name_pairs(row):
                if name:
                    name_counts[code][name] += 1
                else:
                    # Keep track of codes seen even if this row could not be named reliably.
                    name_counts[code]

        print(f"Fetched page {page}: {len(batch)} rows, {len(name_counts)} unique taxonomy codes", file=sys.stderr)

        if len(batch) < rows:
            break

        page += 1
        if pause > 0:
            time.sleep(pause)

    organisms = {code: choose_trusted_name(counter) for code, counter in name_counts.items()}
    print(f"Completed: {total_rows} rows scanned, {len(organisms)} unique taxonomy codes", file=sys.stderr)
    return organisms


def write_output(path: str, organisms: Dict[str, str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ncbi_taxonomy_code", "natural_source_organism"])
        for code, name in sorted(organisms.items(), key=lambda item: int(item[0]) if item[0].isdigit() else item[0]):
            writer.writerow([code, name])


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch all EMDB organisms from the paged search API and save them to CSV."
    )
    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Output CSV path. Default: {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=DEFAULT_ROWS,
        help=f"Rows per page. Default: {DEFAULT_ROWS}",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="HTTP timeout in seconds. Default: 60",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=0.0,
        help="Optional delay between requests in seconds. Default: 0",
    )
    return parser.parse_args(list(argv))


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)

    if args.rows <= 0:
        print("--rows must be positive", file=sys.stderr)
        return 2

    try:
        organisms = collect_organisms(rows=args.rows, timeout=args.timeout, pause=args.pause)
        write_output(args.output, organisms)
    except (HTTPError, URLError) as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {len(organisms)} unique organisms to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
