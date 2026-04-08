import textwrap

import nbformat as nbf


NOTEBOOK_PATH = "bv_brc_zika_genome_grouping.ipynb"


def md_cell(text: str):
    return nbf.v4.new_markdown_cell(textwrap.dedent(text).strip())


def code_cell(text: str):
    return nbf.v4.new_code_cell(textwrap.dedent(text).strip())


nb = nbf.v4.new_notebook()
nb.cells = [
    md_cell(
        """
        # Analyze Zika Virus Genomes from BV-BRC

        This notebook pulls Zika virus genome records from the BV-BRC API for NCBI taxon `3048459`,
        normalizes the key metadata fields, and groups the genomes by:

        - genome name and a genome-name-resolved NCBI taxon ID
        - isolation country
        - host common name, host scientific name, and host taxon ID

        The result is a count of genomes in each unique group.

        The notebook also includes a second host-focused grouping that ignores isolation country and
        summarizes genomes by genome name/taxon plus host common and scientific names with host taxon ID.

        Notes:
        - The fetch step uses the higher-level Zika lineage taxon `3048459` only to retrieve all records in
          that taxonomy scope via `taxon_lineage_ids`.
        - BV-BRC's `taxon_id` field can reflect a higher-level mapped taxon for some genomes in taxonomy views.
          To make the grouping easier to interpret, this notebook resolves a genome-level NCBI taxon ID from
          `genome_name` using NCBI Taxonomy and keeps the original BV-BRC taxon in a separate column.
        - Host taxon IDs are taken directly from BV-BRC when present and resolved through NCBI Taxonomy only
          when BV-BRC does not provide them.
        - The code is written to run in the `anaconda3/envs/nde` environment with standard packages
          already available there (`requests`, `pandas`).
        """
    ),
    code_cell(
        """
        from __future__ import annotations

        from pathlib import Path
        from functools import lru_cache
        import time
        from typing import Iterable

        import pandas as pd
        import requests


        BV_BRC_API = "https://www.bv-brc.org/api/genome/"
        NCBI_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        TAXON_ID = 3048459
        EXPECTED_TOTAL = 2729
        PAGE_SIZE = 1000
        CHECKPOINT_PATH = Path("zika_genomes_with_taxon_ids.tsv")
        LOAD_FROM_CHECKPOINT = False

        session = requests.Session()
        session.headers.update({"accept": "application/json"})
        """
    ),
    md_cell(
        """
        ## Run Mode

        Set `LOAD_FROM_CHECKPOINT = True` to skip the BV-BRC fetch and NCBI lookup steps and instead
        reload the normalized dataset from `zika_genomes_with_taxon_ids.tsv`.
        """
    ),
    code_cell(
        """
        def fetch_bvbrc_genomes(taxon_id: int, page_size: int = PAGE_SIZE) -> pd.DataFrame:
            \"\"\"Fetch the full BV-BRC taxonomy genome table for a taxon lineage.

            The taxonomy page includes descendant genomes. To match the website table, query
            `taxon_lineage_ids` instead of filtering only on the genome's own `taxon_id`.

            BV-BRC defaults to a 25-row page size when no limit is supplied. This function
            uses explicit `limit(COUNT,START)` pagination to retrieve every matching record.
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

            all_rows = []
            start = 0
            while True:
                query = f"{base_query}&limit({page_size},{start})"
                response = session.get(f"{BV_BRC_API}?{query}", timeout=120)
                response.raise_for_status()
                rows = response.json()

                if not rows:
                    break

                all_rows.extend(rows)
                print(f"Fetched {len(rows):,} rows starting at offset {start:,}")

                if len(rows) < page_size:
                    break

                start += page_size

            return pd.DataFrame(all_rows)


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
            if len(df_raw) != EXPECTED_TOTAL:
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


            def clean_text(value):
                if pd.isna(value):
                    return pd.NA
                text = str(value).strip()
                return text if text else pd.NA


            for column in ["genome_name", "isolation_country", "host_common_name", "host_scientific_name"]:
                df[column] = df[column].map(clean_text)

            for column in ["bvbrc_taxon_id", "host_taxon_id"]:
                df[column] = pd.to_numeric(df[column], errors="coerce").astype("Int64")

        df.head()
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
        checkpoint_path = "zika_genomes_with_taxon_ids.tsv"
        df.to_csv(checkpoint_path, sep="\\t", index=False)
        print(f"Wrote checkpoint dataset to {checkpoint_path}")
        """
    ),
    code_cell(
        """
        grouped = (
            df.assign(
                isolation_country=df["isolation_country"].fillna("Unknown"),
                host_common_name=df["host_common_name"].fillna("Unknown"),
                host_scientific_name=df["host_scientific_name"].fillna("Unknown"),
            )
            .groupby(
                [
                    "genome_name",
                    "genome_taxon_id",
                    "bvbrc_taxon_id",
                    "isolation_country",
                    "host_common_name",
                    "host_scientific_name",
                    "host_taxon_id",
                ],
                dropna=False,
            )
            .size()
            .reset_index(name="genome_count")
            .sort_values(
                by=[
                    "genome_count",
                    "genome_name",
                    "genome_taxon_id",
                    "isolation_country",
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
        grouped.head(20)
        """
    ),
    code_cell(
        """
        output_path = "zika_genome_group_counts.tsv"
        grouped.to_csv(output_path, sep="\\t", index=False)
        print(f"Wrote grouped counts to {output_path}")
        """
    ),
    md_cell(
        """
        ## Host-Focused Grouping

        This section groups the same 2,729 genomes by:

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
            df.assign(
                host_common_name=df["host_common_name"].fillna("Unknown"),
                host_scientific_name=df["host_scientific_name"].fillna("Unknown"),
            )
            .groupby(
                [
                    "genome_name",
                    "genome_taxon_id",
                    "bvbrc_taxon_id",
                    "host_common_name",
                    "host_scientific_name",
                    "host_taxon_id",
                ],
                dropna=False,
            )
            .size()
            .reset_index(name="genome_count")
            .sort_values(
                by=[
                    "genome_count",
                    "genome_name",
                    "genome_taxon_id",
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
        host_grouped.head(20)
        """
    ),
    code_cell(
        """
        host_output_path = "zika_genome_host_group_counts.tsv"
        host_grouped.to_csv(host_output_path, sep="\\t", index=False)
        print(f"Wrote host-focused grouped counts to {host_output_path}")
        """
    ),
    md_cell(
        """
        ## Taxon-ID Grouping With Isolation Country

        This section mirrors the first grouping, but uses the resolved genome NCBI taxon ID as the
        primary genome key instead of genome name. To avoid splitting counts unnecessarily, this
        summary groups only on genome taxon ID, host taxon ID, and isolation country.
        """
    ),
    code_cell(
        """
        taxon_grouped = (
            df.assign(
                isolation_country=df["isolation_country"].fillna("Unknown"),
            )
            .groupby(
                [
                    "genome_taxon_id",
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
                    "genome_taxon_id",
                    "isolation_country",
                    "host_taxon_id",
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
        taxon_grouped.head(20)
        """
    ),
    code_cell(
        """
        taxon_output_path = "zika_genome_taxon_group_counts.tsv"
        taxon_grouped.to_csv(taxon_output_path, sep="\\t", index=False)
        print(f"Wrote taxon-based grouped counts to {taxon_output_path}")
        """
    ),
    md_cell(
        """
        ## Taxon-ID Host-Focused Grouping

        This section mirrors the host-focused grouping, but uses the resolved genome NCBI taxon ID as
        the primary genome key instead of genome name. To avoid splitting counts unnecessarily, this
        summary groups only on genome taxon ID and host taxon ID.
        """
    ),
    code_cell(
        """
        host_taxon_grouped = (
            df
            .groupby(
                [
                    "genome_taxon_id",
                    "host_taxon_id",
                ],
                dropna=False,
            )
            .size()
            .reset_index(name="genome_count")
            .sort_values(
                by=[
                    "genome_count",
                    "genome_taxon_id",
                    "host_taxon_id",
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
        host_taxon_grouped.head(20)
        """
    ),
    code_cell(
        """
        host_taxon_output_path = "zika_genome_host_taxon_group_counts.tsv"
        host_taxon_grouped.to_csv(host_taxon_output_path, sep="\\t", index=False)
        print(f"Wrote host-focused taxon-based grouped counts to {host_taxon_output_path}")
        """
    ),
    md_cell(
        """
        ## BV-BRC-Taxon Grouping With Isolation Country

        This section mirrors the genome-taxon-ID grouping with isolation country, but uses the
        BV-BRC-provided taxon ID instead of the resolved genome NCBI taxon ID.
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
        bvbrc_output_path = "zika_genome_bvbrc_group_counts.tsv"
        bvbrc_grouped.to_csv(bvbrc_output_path, sep="\\t", index=False)
        print(f"Wrote BV-BRC taxon-based grouped counts to {bvbrc_output_path}")
        """
    ),
    md_cell(
        """
        ## BV-BRC-Taxon Host-Focused Grouping

        This section mirrors the host-focused genome-taxon-ID grouping, but uses the BV-BRC-provided
        taxon ID instead of the resolved genome NCBI taxon ID.
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
        host_bvbrc_output_path = "zika_genome_host_bvbrc_group_counts.tsv"
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
