#!/usr/bin/env python3
"""Crawl Coriell USIDNET pages and extract all sample detail URLs."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import deque
from html import unescape
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

USER_AGENT = "Mozilla/5.0 (compatible; usidnet-crawler/1.0)"
SAMPLE_REF_RE = re.compile(r"\bRef=(ID\d+)\b", re.IGNORECASE)
DEFAULT_KNOWN_DEAD_FILE = Path(__file__).resolve().parent.parent / "known_dead_urls.txt"


def fetch_html(url: str, timeout: float, retries: int, delay: float) -> str:
    last_error = None
    for attempt in range(retries + 1):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(delay)
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def extract_hrefs(html: str) -> Iterable[str]:
    for match in re.finditer(r"href\s*=\s*['\"]([^'\"]+)['\"]", html, re.IGNORECASE):
        yield unescape(match.group(1).strip())


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    clean_query = []
    for key in sorted(query.keys()):
        for value in sorted(query[key]):
            clean_query.append((key, value))
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            parsed.params,
            urlencode(clean_query, doseq=True),
            "",
        )
    )


def load_known_dead_urls(file_path: Path) -> set[str]:
    if not file_path.exists():
        print(f"WARN: Known dead URL file not found: {file_path}", file=sys.stderr)
        return set()

    known_dead_urls: set[str] = set()
    with file_path.open("r", encoding="utf-8") as f:
        for line in f:
            candidate = line.strip()
            if not candidate or candidate.startswith("#"):
                continue
            known_dead_urls.add(normalize_url(candidate))
    return known_dead_urls


def is_in_scope(url: str) -> bool:
    p = urlparse(url)
    if p.scheme not in {"http", "https"}:
        return False
    if "coriell.org" not in p.netloc.lower():
        return False
    u = url.lower()
    return (
        "catalog.coriell.org" in p.netloc.lower()
        and (
            "/1/usidnet" in u
            or "coll=id" in u
            or "sample_detail.aspx" in u
            or "browsecatalog" in u
            or "diseases" in u
            or "genes" in u
        )
    )


def looks_like_sample_url(url: str) -> bool:
    u = url.lower()
    return "sample_detail.aspx" in u and bool(SAMPLE_REF_RE.search(url))


def is_known_dead(url: str, known_dead_urls: set[str]) -> bool:
    return url in known_dead_urls


def crawl_usidnet_samples(
    seed_urls: list[str],
    known_dead_urls: set[str],
    timeout: float,
    retries: int,
    delay: float,
    max_pages: int,
) -> list[str]:
    queue = deque(normalize_url(s) for s in seed_urls)
    seen_pages: set[str] = set()
    sample_urls: set[str] = set()

    while queue and len(seen_pages) < max_pages:
        url = queue.popleft()
        if url in seen_pages:
            continue
        if is_known_dead(url, known_dead_urls):
            continue
        seen_pages.add(url)

        try:
            html = fetch_html(url, timeout=timeout, retries=retries, delay=delay)
        except Exception as exc:
            print(f"WARN: {exc}", file=sys.stderr)
            continue

        if looks_like_sample_url(url):
            text = html.lower()
            if "repository niaid - usidnet" in text or "usidnet" in text:
                sample_urls.add(url)

        for href in extract_hrefs(html):
            absolute = normalize_url(urljoin(url, href))
            if not is_in_scope(absolute):
                continue
            if is_known_dead(absolute, known_dead_urls):
                continue

            if looks_like_sample_url(absolute):
                sample_urls.add(absolute)

            if absolute not in seen_pages:
                queue.append(absolute)

        time.sleep(delay)

    return sorted(sample_urls)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crawl Coriell USIDNET catalog pages and output sample URLs."
    )
    parser.add_argument(
        "--seed-url",
        action="append",
        dest="seed_urls",
        help="Optional extra seed URL (can be passed multiple times).",
    )
    parser.add_argument(
        "--output",
        default="usidnet_sample_urls.txt",
        help="Output file path for one-URL-per-line results.",
    )
    parser.add_argument("--json", dest="json_output", help="Optional JSON output path.")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--max-pages", type=int, default=1200)
    parser.add_argument(
        "--known-dead-file",
        default=str(DEFAULT_KNOWN_DEAD_FILE),
        help="Path to newline-delimited dead URLs to skip.",
    )
    args = parser.parse_args()

    seeds = [
        "https://catalog.coriell.org/1/USIDNET",
        "https://catalog.coriell.org/0/sections/BrowseCatalog/Diseases.aspx?PgId=3&coll=ID",
    ]
    if args.seed_urls:
        seeds.extend(args.seed_urls)

    known_dead_urls = load_known_dead_urls(Path(args.known_dead_file))
    sample_urls = crawl_usidnet_samples(
        seed_urls=seeds,
        known_dead_urls=known_dead_urls,
        timeout=args.timeout,
        retries=args.retries,
        delay=args.delay,
        max_pages=args.max_pages,
    )

    with open(args.output, "w", encoding="utf-8") as f:
        for url in sample_urls:
            f.write(f"{url}\n")

    if args.json_output:
        with open(args.json_output, "w", encoding="utf-8") as f:
            json.dump(sample_urls, f, indent=2)

    print(f"Wrote {len(sample_urls)} sample URLs to {args.output}")
    if args.json_output:
        print(f"Wrote JSON output to {args.json_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
