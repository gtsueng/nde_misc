import textwrap
from pathlib import Path

import nbformat as nbf


NOTEBOOK_PATH = Path(__file__).with_name("bei_resources_crawler.ipynb")


def md_cell(text: str):
    return nbf.v4.new_markdown_cell(textwrap.dedent(text).strip())


def code_cell(text: str):
    return nbf.v4.new_code_cell(textwrap.dedent(text).strip())


nb = nbf.v4.new_notebook()
nb.cells = [
    md_cell(
        """
        # Crawl the First 100 In-Stock BEI Resources Records

        This notebook crawls the BEI Resources search results page for records that are either in stock
        or temporarily out of stock, follows the first 100 catalog result links, and saves each record
        under `records/`.

        Output preference:

        - save structured `.json` when the HTML detail page can be parsed into labeled sections
        - fall back to `.xml` when the response is XML
        - fall back to raw `.html` otherwise

        The notebook is written for the `anaconda3/envs/nde` environment and uses `requests`,
        `beautifulsoup4`, and the Python standard library.
        """
    ),
    code_cell(
        """
        from __future__ import annotations

        from collections import OrderedDict
        from datetime import datetime, timezone
        import json
        import os
        from pathlib import Path
        import re
        from urllib.parse import urljoin

        import requests
        from bs4 import BeautifulSoup


        SEARCH_URL = "https://www.beiresources.org/Catalog.aspx?f_instockflag=In+Stock%23~%23Temporarily+Out+of+Stock"
        BASE_URL = "https://www.beiresources.org"
        PAGER_EVENT_TARGET = "dnn$ctr14176$XPNBEI_SearchResults$rptResults"
        MAX_RECORDS = 100
        REQUEST_TIMEOUT = 60
        CURRENT_DIR = Path(os.getcwd()).resolve()
        NOTEBOOK_DIR = CURRENT_DIR if CURRENT_DIR.name == "BEI_resources" else CURRENT_DIR / "BEI_resources"
        OUTPUT_DIR = NOTEBOOK_DIR / "records"
        MANIFEST_PATH = OUTPUT_DIR / "manifest.json"

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (compatible; BEIResourcesCrawler/1.0)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )
        """
    ),
    md_cell(
        """
        ## Crawl Helpers

        BEI Resources uses an ASP.NET pager. Page 1 is a normal `GET`, while later pages are reached
        through a form postback with `__EVENTTARGET` and `__EVENTARGUMENT`.
        """
    ),
    code_cell(
        """
        def clean_text(value: str | None) -> str:
            return " ".join((value or "").split())


        def slugify(value: str) -> str:
            return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")


        def build_form_payload(soup: BeautifulSoup) -> tuple[str, dict[str, str]]:
            form = soup.find("form")
            if form is None:
                raise RuntimeError("Could not find the BEI search form needed for pagination.")

            payload: dict[str, str] = {}
            for input_tag in form.select("input[name]"):
                input_type = (input_tag.get("type") or "").lower()
                if input_type in {"submit", "button", "image", "file"}:
                    continue
                payload[input_tag["name"]] = input_tag.get("value", "")

            action = form.get("action") or SEARCH_URL
            return urljoin(SEARCH_URL, action), payload


        def fetch_search_page(page_number: int) -> str:
            if page_number < 1:
                raise ValueError("page_number must be >= 1")

            if page_number == 1:
                response = session.get(SEARCH_URL, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                return response.text

            first_page = session.get(SEARCH_URL, timeout=REQUEST_TIMEOUT)
            first_page.raise_for_status()
            soup = BeautifulSoup(first_page.text, "html.parser")
            action_url, payload = build_form_payload(soup)
            payload["__EVENTTARGET"] = PAGER_EVENT_TARGET
            payload["__EVENTARGUMENT"] = f"Page${page_number}"

            response = session.post(action_url, data=payload, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.text


        def parse_listing_page(html: str) -> list[dict[str, str]]:
            soup = BeautifulSoup(html, "html.parser")
            listings: list[dict[str, str]] = []

            for anchor in soup.select('a[id*="hlATCCNum_"][href]'):
                bei_number = clean_text(anchor.get_text(" ", strip=True))
                record_url = urljoin(BASE_URL, anchor["href"])
                row = anchor.find_parent("table", class_="resultsRow")

                title = ""
                category = ""
                bei_level = ""
                if row is not None:
                    product_name_node = row.select_one("td.resultsproductname div[style*='float:left']")
                    if product_name_node is None:
                        product_name_node = row.select_one("td.resultsproductname")
                    if product_name_node is not None:
                        title_text = clean_text(product_name_node.get_text(" ", strip=True))
                        if title_text.startswith(bei_number):
                            title_text = title_text[len(bei_number):].strip()
                        title = title_text

                    category_cell = row.select_one("td.resultscategory")
                    if category_cell is not None:
                        category = clean_text(category_cell.get_text(" ", strip=True))

                    level_cell = row.select_one("td.resultsbiosafetylevel")
                    if level_cell is not None:
                        bei_level = clean_text(level_cell.get_text(" ", strip=True))

                listings.append(
                    {
                        "bei_number": bei_number,
                        "title": title,
                        "category": category,
                        "bei_level": bei_level,
                        "record_url": record_url,
                    }
                )

            return listings


        def collect_first_n_listings(max_records: int = MAX_RECORDS) -> list[dict[str, str]]:
            listings_by_number: OrderedDict[str, dict[str, str]] = OrderedDict()
            page_number = 1

            while len(listings_by_number) < max_records:
                page_html = fetch_search_page(page_number)
                page_listings = parse_listing_page(page_html)
                if not page_listings:
                    break

                for listing in page_listings:
                    listings_by_number.setdefault(listing["bei_number"], listing)
                    if len(listings_by_number) >= max_records:
                        break

                print(
                    f"Collected {len(page_listings):,} listings from page {page_number}; "
                    f"running total: {len(listings_by_number):,}"
                )
                page_number += 1

            return list(listings_by_number.values())[:max_records]
        """
    ),
    md_cell(
        """
        ## Detail Page Parsing

        Each record detail page exposes a labeled metadata table. When that structure is present, the
        notebook saves a structured JSON record. If a page does not expose parseable sections, the code
        falls back to XML or raw HTML.
        """
    ),
    code_cell(
        """
        def parse_detail_sections(soup: BeautifulSoup) -> dict[str, str]:
            sections: OrderedDict[str, str] = OrderedDict()

            for table in soup.select('table[id$="tblPSItemDetails"]'):
                for row in table.select("tr"):
                    spans = [clean_text(span.get_text(" ", strip=True)) for span in row.select("span")]
                    if len(spans) < 2:
                        continue
                    label = spans[0].rstrip(":").strip()
                    value = spans[1].strip()
                    if label and value:
                        sections[label] = value

            return dict(sections)


        def extract_attachment_links(soup: BeautifulSoup) -> list[dict[str, str]]:
            attachments: list[dict[str, str]] = []
            seen: set[str] = set()

            for anchor in soup.select("a[href]"):
                href = anchor.get("href", "")
                absolute_url = urljoin(BASE_URL, href)
                lowered = absolute_url.lower()
                if not (
                    "productinformationsheet" in lowered
                    or "doc=" in lowered
                    or lowered.endswith((".pdf", ".xml", ".json", ".html", ".htm"))
                ):
                    continue

                if absolute_url in seen:
                    continue
                seen.add(absolute_url)

                attachments.append(
                    {
                        "label": clean_text(anchor.get_text(" ", strip=True)) or Path(absolute_url).name,
                        "url": absolute_url,
                    }
                )

            return attachments


        def choose_output_format(
            parsed_sections: dict[str, str],
            response_text: str,
            content_type: str,
        ) -> str:
            if parsed_sections:
                return "json"

            xml_markers = (
                "xml" in content_type.lower()
                or response_text.lstrip().startswith("<?xml")
                or response_text.lstrip().startswith("<root")
            )
            if xml_markers:
                return "xml"

            return "html"


        def save_record(listing: dict[str, str], index: int) -> dict[str, str | int]:
            response = session.get(listing["record_url"], timeout=REQUEST_TIMEOUT)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")
            parsed_sections = parse_detail_sections(soup)
            page_heading = clean_text((soup.select_one("h2") or soup.title).get_text(" ", strip=True))
            output_format = choose_output_format(
                parsed_sections=parsed_sections,
                response_text=response.text,
                content_type=response.headers.get("Content-Type", ""),
            )
            output_path = OUTPUT_DIR / f"{index:03d}_{slugify(listing['bei_number'])}.{output_format}"

            if output_format == "json":
                record = {
                    "index": index,
                    "bei_number": listing["bei_number"],
                    "listing_title": listing["title"],
                    "listing_category": listing["category"],
                    "listing_bei_level": listing["bei_level"],
                    "source_url": response.url,
                    "page_heading": page_heading,
                    "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
                    "sections": parsed_sections,
                    "attachments": extract_attachment_links(soup),
                }
                output_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
            else:
                output_path.write_text(response.text, encoding="utf-8")

            return {
                "index": index,
                "bei_number": listing["bei_number"],
                "title": listing["title"],
                "record_url": response.url,
                "saved_path": output_path.relative_to(NOTEBOOK_DIR).as_posix(),
                "format": output_format,
                "section_count": len(parsed_sections),
            }


        def crawl_and_save(max_records: int = MAX_RECORDS) -> list[dict[str, str | int]]:
            listings = collect_first_n_listings(max_records=max_records)
            if len(listings) < max_records:
                print(f"Warning: requested {max_records} listings but found only {len(listings)}.")

            manifest: list[dict[str, str | int]] = []
            for index, listing in enumerate(listings, start=1):
                saved = save_record(listing=listing, index=index)
                manifest.append(saved)
                print(
                    f"[{index:03d}/{len(listings):03d}] "
                    f"Saved {saved['bei_number']} as {Path(saved['saved_path']).name}"
                )

            MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            print(f"Wrote manifest: {MANIFEST_PATH}")
            return manifest
        """
    ),
    code_cell(
        """
        manifest = crawl_and_save(max_records=MAX_RECORDS)
        manifest[:5]
        """
    ),
]

nb.metadata["kernelspec"] = {
    "display_name": "Python [conda env:nde]",
    "language": "python",
    "name": "conda-env-nde-py",
}
nb.metadata["language_info"] = {
    "name": "python",
    "version": "3",
}

notebook_text = nbf.writes(nb).replace("__NOTEBOOK_DIR__", NOTEBOOK_PATH.parent.as_posix())
NOTEBOOK_PATH.write_text(notebook_text, encoding="utf-8")
print(f"Wrote {NOTEBOOK_PATH}")
