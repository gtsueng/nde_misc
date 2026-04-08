import textwrap

import nbformat as nbf


NOTEBOOK_PATH = "bv_brc_taxon_genome_grouping.ipynb"


def md_cell(text: str):
    return nbf.v4.new_markdown_cell(textwrap.dedent(text).strip())


def code_cell(text: str):
    return nbf.v4.new_code_cell(textwrap.dedent(text).strip())


nb = nbf.v4.new_notebook()
nb.cells = [
    md_cell(
        """
        # Analyze BV-BRC Taxon Genomes

        This notebook pulls genome records from the BV-BRC API for a user-supplied higher-level
        BV-BRC or NCBI taxon ID, normalizes key metadata, and generates several grouped summaries.

        Supported summaries:

        - genome name plus resolved genome NCBI taxon ID, isolation country, host names, and host taxon ID
        - genome name plus resolved genome NCBI taxon ID, host names, and host taxon ID
        - resolved genome NCBI taxon ID with host taxon ID, with and without isolation country
        - BV-BRC taxon ID with host taxon ID, with and without isolation country

        Notes:
        - The fetch step uses the higher-level input taxon only to retrieve all descendant records via
          `taxon_lineage_ids`.
        - BV-BRC's `taxon_id` field can reflect a mapped higher-level taxon for some genomes, so the
          notebook also resolves a genome-level NCBI taxon ID from `genome_name` and keeps both.
        - Host taxon IDs are taken from BV-BRC when present and resolved through NCBI Taxonomy only
          when needed.
        """
    ),
    code_cell(
        """
        from __future__ import annotations

        from functools import lru_cache
        import json
        from pathlib import Path
        import re
        import time
        from typing import Iterable

        import pandas as pd
        import requests


        BV_BRC_API = "https://www.bv-brc.org/api/genome/"
        NCBI_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"

        TAXON_ID = 3048459
        TAXON_LABEL = "Zika virus"
        EXPECTED_TOTAL = None
        PAGE_SIZE = 1000
        LOAD_FROM_CHECKPOINT = False
        RESUME_BVBRC_FETCH = True
        GROUP_GENOME_BY = "genome_taxon_id"  # "genome_taxon_id" or "bvbrc_taxon_id"
        GROUP_HOST_BY = "host_taxon_id"  # "host_taxon_id", "host_scientific_name", or "host_common_name"


        def slugify(value: str) -> str:
            return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


        OUTPUT_STEM = f"{slugify(TAXON_LABEL)}_{TAXON_ID}"
        CHECKPOINT_DIR = Path("checkpoints")
        OUTPUT_DIR = Path("outputs")
        RAW_FETCH_DIR = Path("raw_fetch")
        RAW_FETCH_RUN_DIR = RAW_FETCH_DIR / OUTPUT_STEM
        CHECKPOINT_PATH = CHECKPOINT_DIR / f"{OUTPUT_STEM}_genomes_with_taxon_ids.tsv"
        RAW_COMPLETE_PATH = CHECKPOINT_DIR / f"{OUTPUT_STEM}_bvbrc_raw.tsv"
        FETCH_MANIFEST_PATH = RAW_FETCH_RUN_DIR / "fetch_manifest.json"

        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        RAW_FETCH_RUN_DIR.mkdir(parents=True, exist_ok=True)

        session = requests.Session()
        session.headers.update({"accept": "application/json"})
        """
    ),
    md_cell(
        """
        ## Run Mode

        Update `TAXON_ID` and `TAXON_LABEL` for the lineage you want to analyze.

        Set `LOAD_FROM_CHECKPOINT = True` to skip the BV-BRC fetch and NCBI lookup steps and reload the
        normalized dataset from the taxon-specific checkpoint TSV in `checkpoints/`.

        Set `RESUME_BVBRC_FETCH = True` to save each fetched BV-BRC page under `raw_fetch/` and resume
        from the last saved offset if a prior retrieval was interrupted.

        Grouping controls:

        - `GROUP_GENOME_BY = "genome_taxon_id"` uses the resolved NCBI genome taxon ID
        - `GROUP_GENOME_BY = "bvbrc_taxon_id"` uses the BV-BRC-provided taxon ID
        - `GROUP_HOST_BY = "host_taxon_id"` uses the resolved or BV-BRC host taxon ID
        - `GROUP_HOST_BY = "host_scientific_name"` or `"host_common_name"` uses host names instead
        """
    ),
    code_cell(
        """
        def load_fetch_manifest() -> dict:
            if FETCH_MANIFEST_PATH.exists():
                return json.loads(FETCH_MANIFEST_PATH.read_text(encoding="utf-8"))
            return {
                "taxon_id": TAXON_ID,
                "page_size": PAGE_SIZE,
                "next_start": 0,
                "completed": False,
                "pages": [],
            }


        def save_fetch_manifest(manifest: dict) -> None:
            FETCH_MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


        def page_path(start: int) -> Path:
            return RAW_FETCH_RUN_DIR / f"page_{start:09d}.tsv"


        def clear_saved_pages() -> None:
            for path in RAW_FETCH_RUN_DIR.glob("page_*.tsv"):
                path.unlink()
            if FETCH_MANIFEST_PATH.exists():
                FETCH_MANIFEST_PATH.unlink()


        def load_saved_pages() -> pd.DataFrame:
            page_files = sorted(RAW_FETCH_RUN_DIR.glob("page_*.tsv"))
            if not page_files:
                return pd.DataFrame()

            frames = [pd.read_csv(path, sep="\\t", dtype=str) for path in page_files]
            return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


        def fetch_bvbrc_genomes(taxon_id: int, page_size: int = PAGE_SIZE) -> pd.DataFrame:
            \"\"\"Fetch the full BV-BRC taxonomy genome table for a lineage taxon.

            Pages are checkpointed to disk after every successful request so interrupted downloads
            can resume without restarting from offset 0.
            \"\"\"
            select_fields = [
                "genome_id",
                "genome_name",
                "taxon_id",
                "isolation_country",
                "host_common_name",
                "host_name",
                "host_scientific_name",
                "host_taxon_id",
            ]
            base_query = "&".join(
                [
                    f"in(taxon_lineage_ids,({taxon_id}))",
                    f"select({','.join(select_fields)})",
                    "sort(+genome_id)",
                    "http_accept=application/json",
                ]
            )

            manifest = load_fetch_manifest()
            if manifest["taxon_id"] != taxon_id or manifest["page_size"] != page_size:
                clear_saved_pages()
                manifest = {
                    "taxon_id": taxon_id,
                    "page_size": page_size,
                    "next_start": 0,
                    "completed": False,
                    "pages": [],
                }
                save_fetch_manifest(manifest)
            elif not RESUME_BVBRC_FETCH and manifest.get("pages"):
                clear_saved_pages()
                manifest = {
                    "taxon_id": taxon_id,
                    "page_size": page_size,
                    "next_start": 0,
                    "completed": False,
                    "pages": [],
                }
                save_fetch_manifest(manifest)

            if RESUME_BVBRC_FETCH and manifest.get("completed") and RAW_COMPLETE_PATH.exists():
                print(f"Loaded completed BV-BRC raw checkpoint from {RAW_COMPLETE_PATH}")
                return pd.read_csv(RAW_COMPLETE_PATH, sep="\\t", dtype=str)

            start = manifest.get("next_start", 0) if RESUME_BVBRC_FETCH else 0
            if RESUME_BVBRC_FETCH and start > 0:
                print(f"Resuming BV-BRC retrieval from offset {start:,}")

            while True:
                query = f"{base_query}&limit({page_size},{start})"
                response = session.get(f"{BV_BRC_API}?{query}", timeout=120)
                response.raise_for_status()
                rows = response.json()

                if not rows:
                    break

                page_df = pd.DataFrame(rows)
                page_df.to_csv(page_path(start), sep="\\t", index=False)
                manifest["taxon_id"] = taxon_id
                manifest["page_size"] = page_size
                manifest["next_start"] = start + len(rows)
                manifest["completed"] = False
                page_name = page_path(start).name
                if page_name not in manifest["pages"]:
                    manifest["pages"].append(page_name)
                save_fetch_manifest(manifest)
                print(f"Fetched {len(rows):,} rows starting at offset {start:,}")

                if len(rows) < page_size:
                    break

                start += page_size

            raw_df = load_saved_pages()
            raw_df.to_csv(RAW_COMPLETE_PATH, sep="\\t", index=False)
            manifest["completed"] = True
            manifest["next_start"] = len(raw_df)
            save_fetch_manifest(manifest)
            print(f"Wrote completed BV-BRC raw checkpoint to {RAW_COMPLETE_PATH}")
            return raw_df


        if LOAD_FROM_CHECKPOINT:
            if not CHECKPOINT_PATH.exists():
                raise FileNotFoundError(
                    f"Checkpoint file not found: {CHECKPOINT_PATH}. "
                    "Set LOAD_FROM_CHECKPOINT = False to rebuild it."
                )

            df = pd.read_csv(CHECKPOINT_PATH, sep="\\t")
            for column in ["genome_taxon_id", "bvbrc_taxon_id", "host_taxon_id"]:
                if column in df.columns:
                    df[column] = pd.to_numeric(df[column], errors="coerce").astype("Int64")
            print(f"Loaded {len(df):,} normalized rows from {CHECKPOINT_PATH}")
        else:
            df_raw = fetch_bvbrc_genomes(TAXON_ID)
            print(f"Fetched {len(df_raw):,} BV-BRC genome records for lineage taxon {TAXON_ID}")
            if EXPECTED_TOTAL is not None and len(df_raw) != EXPECTED_TOTAL:
                print(
                    f"Warning: expected {EXPECTED_TOTAL:,} genomes based on the BV-BRC table, "
                    f"but retrieved {len(df_raw):,}."
                )
        """
    ),
    code_cell(
        """
        if LOAD_FROM_CHECKPOINT:
            print(f"Checkpoint columns: {len(df.columns)}")
            display(df.head())
        else:
            print(f"Columns returned: {len(df_raw.columns)}")
            display(sorted(df_raw.columns.tolist())[:80])
            display(df_raw.head())
        """
    ),
    code_cell(
        """
        allowed_genome_group_fields = {"genome_taxon_id", "bvbrc_taxon_id"}
        allowed_host_group_fields = {"host_taxon_id", "host_scientific_name", "host_common_name"}

        if GROUP_GENOME_BY not in allowed_genome_group_fields:
            raise ValueError(f"Unsupported GROUP_GENOME_BY: {GROUP_GENOME_BY}")
        if GROUP_HOST_BY not in allowed_host_group_fields:
            raise ValueError(f"Unsupported GROUP_HOST_BY: {GROUP_HOST_BY}")


        FIELD_ALIASES = {
            "genome_name": ["genome_name", "genomeName"],
            "bvbrc_taxon_id": ["taxon_id", "genome_taxon_id", "ncbi_taxonomy_id"],
            "isolation_country": ["isolation_country", "country", "geo_loc_name"],
            "host_common_name": ["host_common_name", "host_name", "host", "host_common"],
            "host_scientific_name": ["host_scientific_name", "host_scientific", "host_organism"],
            "host_taxon_id": ["host_taxon_id", "host_taxonomy_id", "host_ncbi_taxon_id", "host_tax_id"],
        }


        def first_present(frame: pd.DataFrame, names: Iterable[str]) -> pd.Series:
            for name in names:
                if name in frame.columns:
                    return frame[name]
            return pd.Series([pd.NA] * len(frame), index=frame.index)


        def clean_text(value):
            if pd.isna(value):
                return pd.NA
            text = str(value).strip()
            return text if text else pd.NA


        if not LOAD_FROM_CHECKPOINT:
            df = pd.DataFrame(
                {
                    "genome_name": first_present(df_raw, FIELD_ALIASES["genome_name"]),
                    "bvbrc_taxon_id": first_present(df_raw, FIELD_ALIASES["bvbrc_taxon_id"]),
                    "isolation_country": first_present(df_raw, FIELD_ALIASES["isolation_country"]),
                    "host_common_name": first_present(df_raw, FIELD_ALIASES["host_common_name"]),
                    "host_scientific_name": first_present(df_raw, FIELD_ALIASES["host_scientific_name"]),
                    "host_taxon_id": first_present(df_raw, FIELD_ALIASES["host_taxon_id"]),
                }
            )

            for column in ["genome_name", "isolation_country", "host_common_name", "host_scientific_name"]:
                df[column] = df[column].map(clean_text)

            for column in ["bvbrc_taxon_id", "host_taxon_id"]:
                df[column] = pd.to_numeric(df[column], errors="coerce").astype("Int64")

        df.head()
        """
    ),
    code_cell(
        """
        group_df = df.copy()
        if GROUP_HOST_BY == "host_taxon_id":
            group_df["group_host_value"] = group_df["host_taxon_id"]
        elif GROUP_HOST_BY == "host_scientific_name":
            group_df["group_host_value"] = group_df["host_scientific_name"].fillna("Unknown")
        else:
            group_df["group_host_value"] = group_df["host_common_name"].fillna("Unknown")

        group_df["group_genome_value"] = group_df[GROUP_GENOME_BY]
        group_df["group_isolation_country"] = group_df["isolation_country"].fillna("Unknown")

        print(f"Grouping genomes by: {GROUP_GENOME_BY}")
        print(f"Grouping hosts by: {GROUP_HOST_BY}")
        group_df[[
            "genome_name",
            "genome_taxon_id",
            "bvbrc_taxon_id",
            "host_common_name",
            "host_scientific_name",
            "host_taxon_id",
            "group_genome_value",
            "group_host_value",
        ]].head()
        """
    ),
    code_cell(
        """
        if not LOAD_FROM_CHECKPOINT:
            def ncbi_taxonomy_lookup(name: str) -> int | pd.NA:
                if not name or pd.isna(name):
                    return pd.NA

                return cached_ncbi_taxonomy_lookup(str(name))


            @lru_cache(maxsize=None)
            def cached_ncbi_taxonomy_lookup(name: str) -> int | pd.NA:
                params = {
                    "db": "taxonomy",
                    "term": f"{name}[Scientific Name] OR {name}[All Names]",
                    "retmode": "json",
                    "retmax": 1,
                }
                for attempt in range(3):
                    try:
                        response = session.get(NCBI_ESEARCH, params=params, timeout=60)
                        response.raise_for_status()
                        id_list = response.json().get("esearchresult", {}).get("idlist", [])
                        time.sleep(0.34)
                        return int(id_list[0]) if id_list else pd.NA
                    except requests.RequestException:
                        if attempt == 2:
                            return pd.NA
                        time.sleep(1.5 * (attempt + 1))


            genome_names = df["genome_name"].dropna().drop_duplicates().tolist()
            genome_taxid_map = {name: ncbi_taxonomy_lookup(name) for name in genome_names}
            df["genome_taxon_id"] = df["genome_name"].map(genome_taxid_map)
            df["genome_taxon_id"] = df["genome_taxon_id"].fillna(df["bvbrc_taxon_id"])
            df["genome_taxon_id"] = pd.to_numeric(df["genome_taxon_id"], errors="coerce").astype("Int64")

            missing_host_taxid = df["host_taxon_id"].isna()
            lookup_names = (
                df.loc[missing_host_taxid, "host_scientific_name"]
                .fillna(df.loc[missing_host_taxid, "host_common_name"])
                .dropna()
                .drop_duplicates()
                .tolist()
            )

            host_taxid_map = {name: ncbi_taxonomy_lookup(name) for name in lookup_names}

            fill_names = df["host_scientific_name"].fillna(df["host_common_name"])
            df.loc[missing_host_taxid, "host_taxon_id"] = fill_names.loc[missing_host_taxid].map(host_taxid_map)
            df["host_taxon_id"] = pd.to_numeric(df["host_taxon_id"], errors="coerce").astype("Int64")

            print(
                f"Resolved genome taxon IDs for "
                f"{sum(pd.notna(v) for v in genome_taxid_map.values())} genome names"
            )
            print(
                f"Resolved host taxon IDs for {sum(pd.notna(v) for v in host_taxid_map.values())} host names"
            )

        df[[
            "genome_name",
            "genome_taxon_id",
            "bvbrc_taxon_id",
            "host_common_name",
            "host_scientific_name",
            "host_taxon_id",
        ]].head()
        """
    ),
    md_cell(
        """
        ## Checkpoint Export

        This export captures the normalized dataset immediately after genome and host taxon IDs have
        been resolved. You can reload this file later to skip the BV-BRC fetch and NCBI lookup steps.
        """
    ),
    code_cell(
        """
        df.to_csv(CHECKPOINT_PATH, sep="\\t", index=False)
        print(f"Wrote checkpoint dataset to {CHECKPOINT_PATH}")
        """
    ),
    code_cell(
        """
        grouped = (
            group_df.assign(
                host_common_name=group_df["host_common_name"].fillna("Unknown"),
                host_scientific_name=group_df["host_scientific_name"].fillna("Unknown"),
            )
            .groupby(
                [
                    "genome_name",
                    "group_genome_value",
                    "bvbrc_taxon_id",
                    "group_isolation_country",
                    "host_common_name",
                    "host_scientific_name",
                    "group_host_value",
                ],
                dropna=False,
            )
            .size()
            .reset_index(name="genome_count")
            .sort_values(
                by=[
                    "genome_count",
                    "genome_name",
                    "group_genome_value",
                    "group_isolation_country",
                    "host_common_name",
                    "host_scientific_name",
                ],
                ascending=[False, True, True, True, True, True],
            )
            .reset_index(drop=True)
        )

        total_grouped = int(grouped["genome_count"].sum())
        assert total_grouped == len(df), (
            f"Grouped counts sum to {total_grouped:,}, expected {len(df):,}"
        )

        print(f"Unique groups: {len(grouped):,}")
        print(f"Genome rows accounted for by grouping: {total_grouped:,}")
        grouped = grouped.rename(
            columns={
                "group_genome_value": GROUP_GENOME_BY,
                "group_isolation_country": "isolation_country",
                "group_host_value": GROUP_HOST_BY,
            }
        )
        grouped.head(20)
        """
    ),
    code_cell(
        """
        output_path = OUTPUT_DIR / f"{OUTPUT_STEM}_genome_group_counts.tsv"
        grouped.to_csv(output_path, sep="\\t", index=False)
        print(f"Wrote grouped counts to {output_path}")
        """
    ),
    md_cell(
        """
        ## Host-Focused Grouping

        This section groups the same genomes by:

        - genome name and genome taxon ID
        - host common name
        - host scientific name
        - host taxon ID

        Unlike the earlier summary, this grouping does not split on isolation country.
        """
    ),
    code_cell(
        """
        host_grouped = (
            group_df.assign(
                host_common_name=group_df["host_common_name"].fillna("Unknown"),
                host_scientific_name=group_df["host_scientific_name"].fillna("Unknown"),
            )
            .groupby(
                [
                    "genome_name",
                    "group_genome_value",
                    "bvbrc_taxon_id",
                    "host_common_name",
                    "host_scientific_name",
                    "group_host_value",
                ],
                dropna=False,
            )
            .size()
            .reset_index(name="genome_count")
            .sort_values(
                by=[
                    "genome_count",
                    "genome_name",
                    "group_genome_value",
                    "host_common_name",
                    "host_scientific_name",
                ],
                ascending=[False, True, True, True, True],
            )
            .reset_index(drop=True)
        )

        total_host_grouped = int(host_grouped["genome_count"].sum())
        assert total_host_grouped == len(df), (
            f"Host-grouped counts sum to {total_host_grouped:,}, expected {len(df):,}"
        )

        print(f"Unique host-focused groups: {len(host_grouped):,}")
        print(f"Genome rows accounted for by host-focused grouping: {total_host_grouped:,}")
        host_grouped = host_grouped.rename(
            columns={
                "group_genome_value": GROUP_GENOME_BY,
                "group_host_value": GROUP_HOST_BY,
            }
        )
        host_grouped.head(20)
        """
    ),
    code_cell(
        """
        host_output_path = OUTPUT_DIR / f"{OUTPUT_STEM}_genome_host_group_counts.tsv"
        host_grouped.to_csv(host_output_path, sep="\\t", index=False)
        print(f"Wrote host-focused grouped counts to {host_output_path}")
        """
    ),
    md_cell(
        """
        ## Taxon-ID Grouping With Isolation Country

        This section uses the resolved genome NCBI taxon ID as the primary genome key and groups only
        on genome taxon ID, host taxon ID, and isolation country.
        """
    ),
    code_cell(
        """
        taxon_grouped = (
            group_df.groupby(
                [
                    "group_genome_value",
                    "group_isolation_country",
                    "group_host_value",
                ],
                dropna=False,
            )
            .size()
            .reset_index(name="genome_count")
            .sort_values(
                by=[
                    "genome_count",
                    "group_genome_value",
                    "group_isolation_country",
                    "group_host_value",
                ],
                ascending=[False, True, True, True],
            )
            .reset_index(drop=True)
        )

        total_taxon_grouped = int(taxon_grouped["genome_count"].sum())
        assert total_taxon_grouped == len(df), (
            f"Taxon-grouped counts sum to {total_taxon_grouped:,}, expected {len(df):,}"
        )

        print(f"Unique taxon-based groups: {len(taxon_grouped):,}")
        print(f"Genome rows accounted for by taxon-based grouping: {total_taxon_grouped:,}")
        taxon_grouped = taxon_grouped.rename(
            columns={
                "group_genome_value": GROUP_GENOME_BY,
                "group_isolation_country": "isolation_country",
                "group_host_value": GROUP_HOST_BY,
            }
        )
        taxon_grouped.head(20)
        """
    ),
    code_cell(
        """
        taxon_output_path = OUTPUT_DIR / f"{OUTPUT_STEM}_genome_taxon_group_counts.tsv"
        taxon_grouped.to_csv(taxon_output_path, sep="\\t", index=False)
        print(f"Wrote taxon-based grouped counts to {taxon_output_path}")
        """
    ),
    md_cell(
        """
        ## Taxon-ID Host-Focused Grouping

        This section groups only on resolved genome NCBI taxon ID and host taxon ID.
        """
    ),
    code_cell(
        """
        host_taxon_grouped = (
            group_df.groupby(
                [
                    "group_genome_value",
                    "group_host_value",
                ],
                dropna=False,
            )
            .size()
            .reset_index(name="genome_count")
            .sort_values(
                by=[
                    "genome_count",
                    "group_genome_value",
                    "group_host_value",
                ],
                ascending=[False, True, True],
            )
            .reset_index(drop=True)
        )

        total_host_taxon_grouped = int(host_taxon_grouped["genome_count"].sum())
        assert total_host_taxon_grouped == len(df), (
            f"Host taxon-grouped counts sum to {total_host_taxon_grouped:,}, expected {len(df):,}"
        )

        print(f"Unique host-focused taxon-based groups: {len(host_taxon_grouped):,}")
        print(
            f"Genome rows accounted for by host-focused taxon-based grouping: "
            f"{total_host_taxon_grouped:,}"
        )
        host_taxon_grouped = host_taxon_grouped.rename(
            columns={
                "group_genome_value": GROUP_GENOME_BY,
                "group_host_value": GROUP_HOST_BY,
            }
        )
        host_taxon_grouped.head(20)
        """
    ),
    code_cell(
        """
        host_taxon_output_path = OUTPUT_DIR / f"{OUTPUT_STEM}_genome_host_taxon_group_counts.tsv"
        host_taxon_grouped.to_csv(host_taxon_output_path, sep="\\t", index=False)
        print(f"Wrote host-focused taxon-based grouped counts to {host_taxon_output_path}")
        """
    ),
    md_cell(
        """
        ## BV-BRC-Taxon Grouping With Isolation Country

        This section groups on BV-BRC taxon ID, host taxon ID, and isolation country.
        """
    ),
    code_cell(
        """
        bvbrc_grouped = (
            df.assign(
                isolation_country=df["isolation_country"].fillna("Unknown"),
            )
            .groupby(
                [
                    "bvbrc_taxon_id",
                    "isolation_country",
                    "host_taxon_id",
                ],
                dropna=False,
            )
            .size()
            .reset_index(name="genome_count")
            .sort_values(
                by=[
                    "genome_count",
                    "bvbrc_taxon_id",
                    "isolation_country",
                    "host_taxon_id",
                ],
                ascending=[False, True, True, True],
            )
            .reset_index(drop=True)
        )

        total_bvbrc_grouped = int(bvbrc_grouped["genome_count"].sum())
        assert total_bvbrc_grouped == len(df), (
            f"BV-BRC-grouped counts sum to {total_bvbrc_grouped:,}, expected {len(df):,}"
        )

        print(f"Unique BV-BRC taxon-based groups: {len(bvbrc_grouped):,}")
        print(f"Genome rows accounted for by BV-BRC taxon-based grouping: {total_bvbrc_grouped:,}")
        bvbrc_grouped.head(20)
        """
    ),
    code_cell(
        """
        bvbrc_output_path = OUTPUT_DIR / f"{OUTPUT_STEM}_genome_bvbrc_group_counts.tsv"
        bvbrc_grouped.to_csv(bvbrc_output_path, sep="\\t", index=False)
        print(f"Wrote BV-BRC taxon-based grouped counts to {bvbrc_output_path}")
        """
    ),
    md_cell(
        """
        ## BV-BRC-Taxon Host-Focused Grouping

        This section groups only on BV-BRC taxon ID and host taxon ID.
        """
    ),
    code_cell(
        """
        host_bvbrc_grouped = (
            df.groupby(
                [
                    "bvbrc_taxon_id",
                    "host_taxon_id",
                ],
                dropna=False,
            )
            .size()
            .reset_index(name="genome_count")
            .sort_values(
                by=[
                    "genome_count",
                    "bvbrc_taxon_id",
                    "host_taxon_id",
                ],
                ascending=[False, True, True],
            )
            .reset_index(drop=True)
        )

        total_host_bvbrc_grouped = int(host_bvbrc_grouped["genome_count"].sum())
        assert total_host_bvbrc_grouped == len(df), (
            f"Host BV-BRC-grouped counts sum to {total_host_bvbrc_grouped:,}, expected {len(df):,}"
        )

        print(f"Unique host-focused BV-BRC taxon-based groups: {len(host_bvbrc_grouped):,}")
        print(
            f"Genome rows accounted for by host-focused BV-BRC taxon-based grouping: "
            f"{total_host_bvbrc_grouped:,}"
        )
        host_bvbrc_grouped.head(20)
        """
    ),
    code_cell(
        """
        host_bvbrc_output_path = OUTPUT_DIR / f"{OUTPUT_STEM}_genome_host_bvbrc_group_counts.tsv"
        host_bvbrc_grouped.to_csv(host_bvbrc_output_path, sep="\\t", index=False)
        print(f"Wrote host-focused BV-BRC taxon-based grouped counts to {host_bvbrc_output_path}")
        """
    ),
]

nb.metadata["kernelspec"] = {
    "display_name": "Python [conda env:nde]",
    "language": "python",
    "name": "conda-env-nde-py",
}
nb.metadata["language_info"] = {"name": "python", "version": "3.x"}

with open(NOTEBOOK_PATH, "w", encoding="utf-8") as f:
    nbf.write(nb, f)

print(f"Wrote {NOTEBOOK_PATH}")
