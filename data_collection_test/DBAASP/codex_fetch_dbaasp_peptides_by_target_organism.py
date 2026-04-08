#!/usr/bin/env python3
r"""Fetch DBAASP peptides filtered by target organism.

This script queries the DBAASP REST API endpoint:
    GET https://dbaasp.org/peptides

The organism filter is passed as:
    targetSpecies.value=<organism>

Example:
    C:\Users\gtsueng\Anaconda3\envs\nde\python.exe fetch_dbaasp_peptides_by_target_organism.py ^
        "Escherichia coli" --output escherichia_coli_peptides.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://dbaasp.org/peptides"
DEFAULT_LIMIT = 500
DEFAULT_OUTPUT = "dbaasp_peptides_by_target_organism.json"


def fetch_page(organism: str, limit: int, offset: int, timeout: int) -> dict:
    params = {
        "targetSpecies.value": organism,
        "limit": limit,
        "offset": offset,
    }
    url = f"{BASE_URL}?{urlencode(params)}"
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "dbaasp-target-organism-fetch/1.0",
        },
    )

    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def collect_peptides(organism: str, limit: int, timeout: int, pause: float) -> list[dict]:
    peptides: list[dict] = []
    offset = 0
    total_count: int | None = None

    while True:
        payload = fetch_page(organism=organism, limit=limit, offset=offset, timeout=timeout)
        batch = payload.get("data", [])

        if total_count is None:
            total_count = int(payload.get("totalCount", 0))

        if not batch:
            break

        peptides.extend(batch)
        print(
            f"Fetched {len(batch)} peptides at offset {offset}; "
            f"collected {len(peptides)} of {total_count}",
            file=sys.stderr,
        )

        offset += len(batch)
        if len(batch) < limit:
            break

        if pause > 0:
            time.sleep(pause)

    return peptides


def write_output(path: str, organism: str, peptides: list[dict]) -> None:
    payload = {
        "target_organism": organism,
        "count": len(peptides),
        "data": peptides,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch DBAASP peptides filtered by target organism."
    )
    parser.add_argument(
        "organism",
        help='Target organism name, for example "Escherichia coli".',
    )
    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Output JSON path. Default: {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Records to request per page. Default: {DEFAULT_LIMIT}",
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
        help="Optional delay between paged requests in seconds. Default: 0",
    )
    parser.add_argument(
        "--print-url",
        action="store_true",
        help="Print the equivalent DBAASP query URL and exit.",
    )
    return parser.parse_args(list(argv))


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)

    if args.limit <= 0:
        print("--limit must be positive", file=sys.stderr)
        return 2

    query_url = f"{BASE_URL}?{urlencode({'targetSpecies.value': args.organism, 'limit': args.limit, 'offset': 0})}"
    if args.print_url:
        print(query_url)
        return 0

    try:
        peptides = collect_peptides(
            organism=args.organism,
            limit=args.limit,
            timeout=args.timeout,
            pause=args.pause,
        )
        write_output(args.output, args.organism, peptides)
    except (HTTPError, URLError) as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {len(peptides)} peptides for {args.organism} to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
