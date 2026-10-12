"""Apply the AORC-based date correction to pluvial claims.

For each pluvial (causeOfDamage == "4") claim within AORC coverage, this
checks whether the reported dateOfLoss already coincides with a day of
meaningful precipitation at the claim's location; if not, it looks for the
wettest day within the search window and, if that clears the threshold,
treats it as the corrected date. Every claim keeps its original dateOfLoss
column; correctedDateOfLoss and pluvialCorrectionStatus record what
happened so downstream users can choose which date to trust, rather than
having the correction silently overwrite the source record.
pluvialCorrectionMaxPrecipMm is the max hourly precip on whichever day was
ultimately used (reported or corrected); reportedDateMaxPrecipMm is always
the reported dateOfLoss's value specifically, even for a "corrected" claim,
so the original day's precip is never lost.

Each claim's precipitation value is a bilinear interpolation of its 4
AORC corner pixels' hourly values (04a/04b), combined at the claim's exact
location before taking each day's max — not a nearest-pixel snap. Because
04a resolves one time zone per claim and applies it to all 4 corners, the
4 corners' hourly series are guaranteed to share identical UTC timestamps
for a given claim-day, so combining them is a plain per-hour weighted sum
(compute_interpolated_daily_max), not something requiring timestamp
alignment.

Processed in batches of pluvial claims (not all ~1.1M at once): expanding
every claim to (4 corners x search-window days) is a large intermediate
table, and batching keeps peak memory bounded to one batch's share of it
rather than the whole dataset's.

pluvialCorrectionStatus values:
  not_pluvial                    causeOfDamage != "4"; not evaluated
  before_aorc_coverage           dateOfLoss predates AORC (pre-1979); not evaluated
  outside_aorc_grid_extent       location outside the AORC CONUS grid (e.g. Puerto Rico); not evaluated
  accepted_as_reported           reported date already clears the precip threshold
  corrected                      a day within the search window clears the threshold
  no_qualifying_precip_in_window data present for the window, but nothing cleared threshold
  no_aorc_data_in_window         AORC had no valid (non-missing) values anywhere in the window
"""

import argparse

import geopandas as gpd
import numpy as np
import pandas as pd

from aorc_grid import pixel_to_latlon
from paths import (
    TRIANGULATED_PARQUET,
    CLAIM_PIXEL_LOOKUP_PARQUET,
    EXCLUDED_PLUVIAL_CLAIMS_PARQUET,
    AORC_HOURLY_PARQUET,
    PLUVIAL_CORRECTED_PARQUET,
    PLUVIAL_SEARCH_WINDOW_DAYS,
)
from precip_threshold import UniformThresholdGrid, NetCDFThresholdGrid, default_threshold_grid

N_CORNERS = 4
CLAIM_BATCH_SIZE = 50_000


def compute_interpolated_daily_max(lookup_batch: pd.DataFrame, hourly: pd.DataFrame) -> pd.DataFrame:
    """Bilinearly-interpolated daily-max precip for every (claim, candidate day).

    lookup_batch: a slice of CLAIM_PIXEL_LOOKUP_PARQUET (4 corner rows/claim).
    hourly: AORC_HOURLY_PARQUET, keyed on (pixel_row, pixel_col, candidate_date,
        timezone) — caller renames its "date" column before passing it in,
        since that rename is wasteful to repeat once per batch — each row's
        precip_mm a list of hourly values for that local day.

    Returns one row per (id, day_offset) that had all 4 corners' data
    available, with columns [id, day_offset, candidate_date, precip_mm].
    Claim-days missing one or more corners are simply absent — the caller
    treats those candidate days as having no valid value, same as any
    other AORC gap.
    """
    offsets_days = np.arange(-PLUVIAL_SEARCH_WINDOW_DAYS, PLUVIAL_SEARCH_WINDOW_DAYS + 1)
    n, w = len(lookup_batch), len(offsets_days)

    expanded = pd.DataFrame(
        {
            "id": np.repeat(lookup_batch["id"].values, w),
            "corner_index": np.repeat(lookup_batch["corner_index"].values, w),
            "aorc_pixel_row": np.repeat(lookup_batch["aorc_pixel_row"].values, w),
            "aorc_pixel_col": np.repeat(lookup_batch["aorc_pixel_col"].values, w),
            "iana_timezone": np.repeat(lookup_batch["iana_timezone"].values, w),
            "weight": np.repeat(lookup_batch["weight"].values, w),
            "day_offset": np.tile(offsets_days, n),
            "candidate_date": (
                np.repeat(lookup_batch["dateOfLoss"].values, w)
                + np.tile(offsets_days, n) * np.timedelta64(1, "D")
            ),
        }
    )
    expanded["candidate_date"] = pd.to_datetime(expanded["candidate_date"]).dt.normalize()

    merged = expanded.merge(
        hourly,  # already has its date column renamed to candidate_date by the caller
        on=["aorc_pixel_row", "aorc_pixel_col", "candidate_date", "iana_timezone"],
        how="inner",  # missing corners simply drop that (id, day_offset) candidate
    )
    if merged.empty:
        return pd.DataFrame(columns=["id", "day_offset", "candidate_date", "precip_mm"])

    merged["array_len"] = merged["precip_mm"].str.len()
    group_key = ["id", "day_offset"]
    complete = merged[merged.groupby(group_key)["corner_index"].transform("size") == N_CORNERS]
    complete = complete[complete.groupby(group_key)["array_len"].transform("nunique") == 1]
    if complete.empty:
        return pd.DataFrame(columns=["id", "day_offset", "candidate_date", "precip_mm"])

    results = []
    for length, sub in complete.groupby("array_len"):
        sub = sub.sort_values(group_key + ["corner_index"])
        n_groups = len(sub) // N_CORNERS
        arr = np.stack(sub["precip_mm"].to_numpy())  # (n_rows, length)
        weighted = arr * sub["weight"].to_numpy()[:, None]
        daily_max = weighted.reshape(n_groups, N_CORNERS, length).sum(axis=1).max(axis=1)

        keys = sub.iloc[::N_CORNERS][["id", "day_offset", "candidate_date"]].reset_index(drop=True)
        keys["precip_mm"] = daily_max
        results.append(keys)

    return pd.concat(results, ignore_index=True)


def classify(row):
    """Decide accepted/corrected/no-qualifying-precip/no-data for one claim.

    row["threshold_mm"] is resolved by the caller (see main) from the precip
    threshold grid before classify ever runs — for a uniform grid every row
    gets the same value; for a NetCDFThresholdGrid each claim's own value,
    both computed in one batched call rather than looked up per-row here.
    """
    threshold_mm = row["threshold_mm"]
    if pd.notna(row["reported_day_precip_mm"]) and (
        row["reported_day_precip_mm"] >= threshold_mm
    ):
        return pd.Series([row["dateOfLoss"], "accepted_as_reported", row["reported_day_precip_mm"]])
    if pd.notna(row["best_precip_mm"]):
        if row["best_precip_mm"] >= threshold_mm:
            return pd.Series([row["best_date"], "corrected", row["best_precip_mm"]])
        return pd.Series([row["dateOfLoss"], "no_qualifying_precip_in_window", row["best_precip_mm"]])
    return pd.Series([row["dateOfLoss"], "no_aorc_data_in_window", np.nan])


def classify_batch(lookup_batch: pd.DataFrame, hourly: pd.DataFrame, threshold_by_id: pd.Series) -> pd.DataFrame:
    per_day = compute_interpolated_daily_max(lookup_batch, hourly)

    claims = lookup_batch[["id", "dateOfLoss"]].drop_duplicates("id")

    reported = (
        per_day[per_day["day_offset"] == 0][["id", "precip_mm"]]
        .rename(columns={"precip_mm": "reported_day_precip_mm"})
    )

    best_per_claim = per_day.groupby("id")["precip_mm"].max()
    per_day = per_day.assign(is_best=per_day["precip_mm"] == per_day["id"].map(best_per_claim))
    best_rows = (
        per_day[per_day["is_best"]]
        .assign(abs_offset=lambda d: d["day_offset"].abs())
        .sort_values(["id", "abs_offset", "candidate_date"])
        .drop_duplicates("id", keep="first")[["id", "candidate_date", "precip_mm"]]
        .rename(columns={"candidate_date": "best_date", "precip_mm": "best_precip_mm"})
    )

    per_claim = claims.merge(reported, on="id", how="left").merge(best_rows, on="id", how="left")
    per_claim["threshold_mm"] = per_claim["id"].map(threshold_by_id)
    per_claim[["correctedDateOfLoss", "pluvialCorrectionStatus", "pluvialCorrectionMaxPrecipMm"]] = (
        per_claim.apply(classify, axis=1)
    )
    return (
        per_claim.drop(columns="threshold_mm")
        .rename(columns={"reported_day_precip_mm": "reportedDateMaxPrecipMm"})
    )


def main():
    """Classify every pluvial claim in batches, then write the full corrected claims table."""
    parser = argparse.ArgumentParser(description=__doc__)
    threshold_source = parser.add_mutually_exclusive_group()
    threshold_source.add_argument(
        "--min-hourly-precip-mm",
        type=float,
        default=None,
        help="Override config.yaml's pluvial_correction.min_hourly_precip_mm for this run "
        "with a different constant (still a uniform grid). Mutually exclusive with "
        "--threshold-netcdf.",
    )
    threshold_source.add_argument(
        "--threshold-netcdf",
        default=None,
        help="Path to a NetCDF file defining a spatially-varying threshold instead of a "
        "constant — see precip_threshold.NetCDFThresholdGrid. Sampled nearest-neighbor at "
        "each claim's location; doesn't need to be on the AORC grid.",
    )
    parser.add_argument(
        "--threshold-netcdf-var",
        default=None,
        help="Which data variable to read from --threshold-netcdf, if it has more than one.",
    )
    args = parser.parse_args()

    if args.threshold_netcdf:
        threshold_grid = NetCDFThresholdGrid(args.threshold_netcdf, var=args.threshold_netcdf_var)
    elif args.min_hourly_precip_mm is not None:
        threshold_grid = UniformThresholdGrid(args.min_hourly_precip_mm)
    else:
        threshold_grid = default_threshold_grid()
    print(f"Precip threshold grid: {threshold_grid}")

    print(f"Loading claim/pixel lookup from {CLAIM_PIXEL_LOOKUP_PARQUET}...")
    lookup = pd.read_parquet(CLAIM_PIXEL_LOOKUP_PARQUET)
    lookup["dateOfLoss"] = pd.to_datetime(lookup["dateOfLoss"])

    print("Reconstructing each claim's location from its 4 corner pixels, for the threshold grid...")
    corner_lat, corner_lon = pixel_to_latlon(
        lookup["aorc_pixel_row"].to_numpy(), lookup["aorc_pixel_col"].to_numpy()
    )
    claim_latlon = (
        pd.DataFrame(
            {
                "id": lookup["id"].values,
                "weighted_lat": corner_lat * lookup["weight"].values,
                "weighted_lon": corner_lon * lookup["weight"].values,
            }
        )
        .groupby("id")[["weighted_lat", "weighted_lon"]]
        .sum()
    )
    threshold_by_id = pd.Series(
        threshold_grid.resolve_for_claims(claim_latlon["weighted_lat"].values, claim_latlon["weighted_lon"].values),
        index=claim_latlon.index,
    )

    print(f"Loading AORC hourly table from {AORC_HOURLY_PARQUET}...")
    hourly = pd.read_parquet(
        AORC_HOURLY_PARQUET,
        columns=["aorc_pixel_row", "aorc_pixel_col", "date", "iana_timezone", "precip_mm"],
    )
    hourly["date"] = pd.to_datetime(hourly["date"])
    hourly = hourly.dropna(subset=["precip_mm"]).rename(columns={"date": "candidate_date"})

    claim_ids = lookup["id"].drop_duplicates().values
    n_batches = -(-len(claim_ids) // CLAIM_BATCH_SIZE)  # ceil div
    print(f"\nProcessing {len(claim_ids):,} claims in {n_batches} batch(es) of up to {CLAIM_BATCH_SIZE:,}...")

    lookup_indexed = lookup.set_index("id", drop=False)
    per_claim_batches = []
    for i in range(n_batches):
        batch_ids = claim_ids[i * CLAIM_BATCH_SIZE : (i + 1) * CLAIM_BATCH_SIZE]
        lookup_batch = lookup_indexed.loc[batch_ids].reset_index(drop=True)
        per_claim_batches.append(classify_batch(lookup_batch, hourly, threshold_by_id))
        if (i + 1) % 5 == 0 or i + 1 == n_batches:
            print(f"  batch {i + 1}/{n_batches} done")

    per_claim = pd.concat(per_claim_batches, ignore_index=True)
    n_corrected = (per_claim["pluvialCorrectionStatus"] == "corrected").sum()
    print(
        f"\n{n_corrected:,} / {len(per_claim):,} evaluated pluvial claims had their date "
        f"corrected ({100 * n_corrected / len(per_claim):.1f}%)"
    )
    print(per_claim["pluvialCorrectionStatus"].value_counts())

    print(f"\nLoading full triangulated claims from {TRIANGULATED_PARQUET}...")
    claims_gdf = gpd.read_parquet(TRIANGULATED_PARQUET)
    claims_gdf["dateOfLoss"] = pd.to_datetime(claims_gdf["dateOfLoss"])

    print(f"Loading excluded-claim reasons from {EXCLUDED_PLUVIAL_CLAIMS_PARQUET}...")
    excluded = pd.read_parquet(EXCLUDED_PLUVIAL_CLAIMS_PARQUET).set_index("id")["exclusion_reason"]

    claims_gdf["correctedDateOfLoss"] = claims_gdf["dateOfLoss"]
    claims_gdf["pluvialCorrectionStatus"] = "not_pluvial"
    is_pluvial = claims_gdf["causeOfDamage"] == "4"
    claims_gdf.loc[is_pluvial, "pluvialCorrectionStatus"] = claims_gdf.loc[is_pluvial, "id"].map(
        excluded
    )
    claims_gdf["pluvialCorrectionMaxPrecipMm"] = np.nan
    claims_gdf["reportedDateMaxPrecipMm"] = np.nan

    corrected_indexed = per_claim.set_index("id")
    evaluated = claims_gdf["id"].isin(corrected_indexed.index)
    evaluated_ids = claims_gdf.loc[evaluated, "id"]
    for col in [
        "correctedDateOfLoss",
        "pluvialCorrectionStatus",
        "pluvialCorrectionMaxPrecipMm",
        "reportedDateMaxPrecipMm",
    ]:
        claims_gdf.loc[evaluated, col] = evaluated_ids.map(corrected_indexed[col])

    claims_gdf.to_parquet(PLUVIAL_CORRECTED_PARQUET)
    print(f"\nWrote {len(claims_gdf):,} rows to {PLUVIAL_CORRECTED_PARQUET}")


if __name__ == "__main__":
    main()
