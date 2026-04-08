"""
Fetch all unique target organisms from the DBAASP REST API.

DBAASP (Database of Antimicrobial Activity and Structure of Peptides)
API base URL: https://dbaasp.org
API version: 4.0.1

Strategy:
1. Page through /peptides to collect all peptide IDs
2. Fetch /peptides/{id} in parallel batches to get targetActivities
3. Extract unique targetSpecies names
4. Save results to TSV

Usage:
    python fetch_dbaasp_target_organisms.py
"""

import requests
import json
import csv
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

BASE_URL = "https://dbaasp.org"
PAGE_SIZE = 100          # peptides per list-page request
MAX_WORKERS = 10         # concurrent detail-fetch threads
RETRY_LIMIT = 3          # retries per failed request
RETRY_DELAY = 2          # seconds between retries
OUTPUT_DIR = Path(__file__).parent / "data"


def get_all_peptide_ids() -> list[int]:
    """Page through /peptides and collect every peptide ID."""
    peptide_ids = []
    page = 0
    total = None

    logger.info("Fetching peptide ID list...")
    while True:
        params = {"size": PAGE_SIZE, "page": page}
        for attempt in range(1, RETRY_LIMIT + 1):
            try:
                resp = requests.get(f"{BASE_URL}/peptides", params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as exc:
                logger.warning("Page %d attempt %d failed: %s", page, attempt, exc)
                if attempt == RETRY_LIMIT:
                    raise
                time.sleep(RETRY_DELAY)

        if total is None:
            total = data.get("totalCount", 0)
            logger.info("Total peptides: %d", total)

        items = data.get("data", [])
        if not items:
            break

        for item in items:
            peptide_ids.append(item["id"])

        fetched = page * PAGE_SIZE + len(items)
        logger.info("  Collected %d / %d peptide IDs", fetched, total)

        if fetched >= total:
            break
        page += 1

    logger.info("Done collecting peptide IDs: %d total", len(peptide_ids))
    return peptide_ids


def fetch_peptide_target_species(peptide_id: int) -> set[str]:
    """Fetch detail for one peptide and return its unique target species names."""
    for attempt in range(1, RETRY_LIMIT + 1):
        try:
            resp = requests.get(
                f"{BASE_URL}/peptides/{peptide_id}", timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as exc:
            if attempt == RETRY_LIMIT:
                logger.warning("Peptide %d failed after %d attempts: %s", peptide_id, RETRY_LIMIT, exc)
                return set()
            time.sleep(RETRY_DELAY)

    species = set()

    # Collect from top-level peptide targetActivities
    for activity in data.get("targetActivities", []):
        ts = activity.get("targetSpecies")
        if ts and ts.get("name"):
            species.add(ts["name"].strip())

    # Collect from monomers (for multimers/dimers)
    for monomer in data.get("monomers", []):
        for activity in monomer.get("targetActivities", []):
            ts = activity.get("targetSpecies")
            if ts and ts.get("name"):
                species.add(ts["name"].strip())

    return species


def fetch_all_target_organisms(peptide_ids: list[int]) -> dict[str, int]:
    """
    Fetch target species for all peptides in parallel batches.

    Returns a dict mapping species name -> count of peptides it appears in.
    """
    organism_counts: dict[str, int] = {}
    total = len(peptide_ids)
    completed = 0

    logger.info("Fetching target organisms from %d peptides (workers=%d)...", total, MAX_WORKERS)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_id = {
            executor.submit(fetch_peptide_target_species, pid): pid
            for pid in peptide_ids
        }

        for future in as_completed(future_to_id):
            pid = future_to_id[future]
            try:
                species_set = future.result()
                for name in species_set:
                    organism_counts[name] = organism_counts.get(name, 0) + 1
            except Exception as exc:
                logger.warning("Unexpected error for peptide %d: %s", pid, exc)

            completed += 1
            if completed % 500 == 0 or completed == total:
                logger.info(
                    "  Progress: %d / %d peptides (unique organisms so far: %d)",
                    completed, total, len(organism_counts),
                )

    return organism_counts


def save_results(organism_counts: dict[str, int], output_path: Path) -> None:
    """Save organism name + peptide count to a TSV file, sorted by count desc."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sorted_organisms = sorted(organism_counts.items(), key=lambda x: x[1], reverse=True)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(["target_organism", "peptide_count"])
        writer.writerows(sorted_organisms)

    logger.info("Saved %d unique target organisms to %s", len(sorted_organisms), output_path)


def main():
    output_path = OUTPUT_DIR / "dbaasp_target_organisms.tsv"

    # Step 1: collect all peptide IDs
    peptide_ids = get_all_peptide_ids()

    # Step 2: fetch target species from each peptide in parallel
    organism_counts = fetch_all_target_organisms(peptide_ids)

    # Step 3: save
    save_results(organism_counts, output_path)

    logger.info("Done. Total unique target organisms: %d", len(organism_counts))


if __name__ == "__main__":
    main()
