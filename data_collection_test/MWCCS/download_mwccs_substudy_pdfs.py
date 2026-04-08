#!/usr/bin/env python3
"""Download MWCCS substudy visual summary PDFs into the local Substudies directory."""

from __future__ import annotations

import argparse
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


SUBSTUDY_PAGE_URL = "https://statepi.jhsph.edu/mwccs/substudy-science/"
USER_AGENT = "mwccs-substudy-fetch/1.0"


class PdfLinkParser(HTMLParser):
    """Collect PDF links from anchor tags on the MWCCS substudy page."""

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.pdf_links: dict[str, str] = {}
        self._current_href: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return

        href = dict(attrs).get("href")
        if href:
            self._current_href = urljoin(self.base_url, href)
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._current_href is None:
            return

        href = self._current_href
        text = " ".join(part.strip() for part in self._current_text if part.strip())
        if href.lower().endswith(".pdf") and "substudy" in text.lower():
            self.pdf_links[href] = text

        self._current_href = None
        self._current_text = []


def fetch_text(url: str, timeout: int) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_binary(url: str, timeout: int) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def extract_pdf_links(page_url: str, timeout: int) -> dict[str, str]:
    parser = PdfLinkParser(base_url=page_url)
    parser.feed(fetch_text(page_url, timeout=timeout))
    return parser.pdf_links


def filename_from_url(url: str) -> str:
    name = Path(urlparse(url).path).name
    if not name:
        raise ValueError(f"Could not determine filename for URL: {url}")
    return name


def download_pdfs(pdf_urls: Iterable[str], output_dir: Path, timeout: int, overwrite: bool) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: list[Path] = []

    for pdf_url in pdf_urls:
        output_path = output_dir / filename_from_url(pdf_url)
        if output_path.exists() and not overwrite:
            print(f"Skipping existing file: {output_path.name}")
            saved_paths.append(output_path)
            continue

        print(f"Downloading {pdf_url}")
        output_path.write_bytes(fetch_binary(pdf_url, timeout=timeout))
        saved_paths.append(output_path)

    return saved_paths


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download MWCCS substudy visual summary PDFs into a local directory."
    )
    parser.add_argument(
        "--page-url",
        default=SUBSTUDY_PAGE_URL,
        help=f"MWCCS substudy page URL. Default: {SUBSTUDY_PAGE_URL}",
    )
    parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).resolve().with_name("Substudies")),
        help="Directory where PDFs will be saved. Default: MWCCS/Substudies",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="HTTP timeout in seconds. Default: 60",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite files that already exist.",
    )
    return parser.parse_args(list(argv))


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)
    if args.timeout <= 0:
        print("--timeout must be positive", file=sys.stderr)
        return 2

    output_dir = Path(args.output_dir).resolve()

    try:
        pdf_links = extract_pdf_links(page_url=args.page_url, timeout=args.timeout)
        if not pdf_links:
            print(f"No substudy PDF links found on {args.page_url}", file=sys.stderr)
            return 1

        print(f"Found {len(pdf_links)} substudy PDF links on {args.page_url}")
        saved_paths = download_pdfs(
            pdf_urls=pdf_links.keys(),
            output_dir=output_dir,
            timeout=args.timeout,
            overwrite=args.overwrite,
        )
    except Exception as exc:  # pragma: no cover
        print(f"Failed: {exc}", file=sys.stderr)
        return 1

    print(f"Saved {len(saved_paths)} PDF files to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
