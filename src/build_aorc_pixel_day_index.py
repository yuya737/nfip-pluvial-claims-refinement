"""Build the list of (pixel, day, timezone) values actually needed from AORC.

Only pluvial claims (causeOfDamage == "4") get their reported date checked
against precipitation — see docs/methods.md for why "4" is used as a proxy
for rain-driven flooding and what that does and doesn't capture. For each
such claim we need the hourly precipitation at its location for every day
in a +/- search-window-day range around the reported date, so we can
(a) accept the reported date if its day's precip already clears the
threshold, or (b) find the wettest nearby day otherwise.

Each claim's precipitation is sampled by bilinear interpolation across the
4 AORC pixels surrounding its location (see aorc_grid.latlon_to_bilinear_corners),
not by snapping to the nearest pixel — the location almost never lands
exactly on a grid point, and precipitation is a continuous field. The
interpolation itself (combining the 4 corners' hourly values with the
claim's specific fractional weights, then taking the day's max) happens
per claim in 04c, since those weights are continuous and claim-specific;
this script only figures out which (pixel, day) values need fetching.

Time zone is resolved once per claim, at the claim's own point (see
timezone_utils.py), and applied uniformly to all 4 of that claim's corner
pixels — not resolved separately per pixel. That guarantees the 4 corners'
fetched hourly series share identical UTC timestamps for a given claim-day,
which is what lets 04c combine them with a plain weighted sum instead of
having to align mismatched hour sets in the rare case corners straddle a
time zone boundary.

Requesting the full hourly CONUS grid for the full period of record is
infeasible (multiple TB); instead this script computes the much smaller
set of distinct (pixel, day, timezone) triples actually referenced by any
claim's 4 corners, so the fetch step only has to touch that set. Claims
sharing a flood event (same storm, nearby locations, nearby dates) collapse
onto largely overlapping sets, so the deduplicated index is typically far
smaller than (n_claims * 4 corners * window_size).

Claims before AORC's coverage start (1979-01-01 CONUS), or whose location
falls outside the AORC CONUS grid extent (e.g. Puerto Rico), cannot be
checked; they're recorded in EXCLUDED_PLUVIAL_CLAIMS_PARQUET and
correct_pluvial_dates.py applies the matching status to them instead
of silently dropping them from the published dataset.
"""

import geopandas as gpd
import numpy as np
import pandas as pd

from aorc_grid import is_within_grid, latlon_to_bilinear_corners
from timezone_utils import point_timezones
from paths import (
    TRIANGULATED_PARQUET,
    AORC_REQUEST_INDEX_PARQUET,
    CLAIM_PIXEL_LOOKUP_PARQUET,
    EXCLUDED_PLUVIAL_CLAIMS_PARQUET,
    PLUVIAL_SEARCH_WINDOW_DAYS,
    AORC_COVERAGE_START,
    PLUVIAL_CAUSE_CODE,
)
N_CORNERS = 4


def representative_point_wgs84(claims_gdf: gpd.GeoDataFrame) -> gpd.GeoSeries:
    """Centroid of the triangulated polygon, in WGS84.

    Falls back to the claim's raw reported lat/lon where the triangulated
    geometry is missing (should be rare — see triangulate_claims.py).
    """
    centroids = claims_gdf.geometry.centroid.to_crs("EPSG:4326")
    points = gpd.GeoSeries(
        gpd.points_from_xy(claims_gdf["longitude"], claims_gdf["latitude"]),
        crs="EPSG:4326",
        index=claims_gdf.index,
    )
    return centroids.fillna(points)


def main():
    """Build and write the per-claim corner-pixel lookup and the deduplicated request index."""
    print(f"Loading triangulated claims from {TRIANGULATED_PARQUET}...")
    claims_gdf = gpd.read_parquet(TRIANGULATED_PARQUET)

    pluvial = claims_gdf[claims_gdf["causeOfDamage"] == PLUVIAL_CAUSE_CODE].copy()
    print(f"{len(pluvial):,} / {len(claims_gdf):,} claims have causeOfDamage == '4' (pluvial)")

    pluvial["dateOfLoss"] = pd.to_datetime(pluvial["dateOfLoss"])
    coverage_start = pd.Timestamp(AORC_COVERAGE_START)
    before_coverage = pluvial["dateOfLoss"] < coverage_start
    print(
        f"{before_coverage.sum():,} pluvial claims predate AORC coverage "
        f"({AORC_COVERAGE_START}) and will be excluded from correction"
    )

    print("Computing representative WGS84 point per claim (triangulated centroid)...")
    points = representative_point_wgs84(pluvial)
    within_grid = pd.Series(
        [is_within_grid(lat, lon) for lat, lon in zip(points.y, points.x)], index=points.index
    )
    print(
        f"{(~within_grid).sum():,} pluvial claims fall outside the AORC CONUS grid extent "
        "(e.g. Puerto Rico, other territories) and will be excluded from correction"
    )

    excluded = pluvial[before_coverage | ~within_grid][["id"]].copy()
    excluded["exclusion_reason"] = np.where(
        before_coverage[before_coverage | ~within_grid],
        "before_aorc_coverage",
        "outside_aorc_grid_extent",
    )
    excluded.to_parquet(EXCLUDED_PLUVIAL_CLAIMS_PARQUET)
    print(f"Wrote {len(excluded):,} excluded claims to {EXCLUDED_PLUVIAL_CLAIMS_PARQUET}")

    keep = ~before_coverage & within_grid
    pluvial, points = pluvial[keep], points[keep]

    if "id" not in pluvial.columns:
        raise KeyError(
            "Expected a claim identifier column named 'id' (present in the OpenFEMA "
            "FimaNfipClaims redacted-claims-v2 schema). Update claim_id_col below if "
            "your source extract names it differently."
        )

    print("Resolving each claim's own local time zone...")
    claim_tz = point_timezones(points.y, points.x)
    claim_tz.index = pluvial.index

    print("Computing the 4 bilinear-interpolation corner pixels per claim...")
    corner_lists = [latlon_to_bilinear_corners(lat, lon) for lat, lon in zip(points.y, points.x)]
    flat_corners = [corner for corners in corner_lists for corner in corners]
    rows, cols, weights = zip(*flat_corners)

    n_claims = len(pluvial)
    lookup = pd.DataFrame(
        {
            "id": np.repeat(pluvial["id"].values, N_CORNERS),
            "dateOfLoss": np.repeat(pluvial["dateOfLoss"].values, N_CORNERS),
            "iana_timezone": np.repeat(claim_tz.values, N_CORNERS),
            "corner_index": np.tile(np.arange(N_CORNERS), n_claims),
            "aorc_pixel_row": rows,
            "aorc_pixel_col": cols,
            "weight": weights,
        }
    )
    lookup.to_parquet(CLAIM_PIXEL_LOOKUP_PARQUET)
    print(
        f"Wrote per-claim corner-pixel lookup ({len(lookup):,} rows, "
        f"{N_CORNERS} per claim) to {CLAIM_PIXEL_LOOKUP_PARQUET}"
    )

    # One row per (pixel, candidate day, timezone): repeat each corner-pixel
    # row window_size times and tile the day offsets alongside it, then dedupe.
    offsets_days = np.arange(-PLUVIAL_SEARCH_WINDOW_DAYS, PLUVIAL_SEARCH_WINDOW_DAYS + 1)
    n_corner_rows, window_size = len(lookup), len(offsets_days)

    request_index = pd.DataFrame(
        {
            "aorc_pixel_row": np.repeat(lookup["aorc_pixel_row"].values, window_size),
            "aorc_pixel_col": np.repeat(lookup["aorc_pixel_col"].values, window_size),
            "iana_timezone": np.repeat(lookup["iana_timezone"].values, window_size),
            "date": (
                np.repeat(lookup["dateOfLoss"].values, window_size)
                + np.tile(offsets_days, n_corner_rows) * np.timedelta64(1, "D")
            ),
        }
    )
    request_index["date"] = pd.to_datetime(request_index["date"]).dt.normalize()
    request_index = (
        request_index.drop_duplicates()
        .sort_values(["date", "aorc_pixel_row", "aorc_pixel_col", "iana_timezone"])
        .reset_index(drop=True)
    )

    naive_total = n_corner_rows * window_size
    print(
        f"Request index: {len(request_index):,} unique (pixel, day, timezone) triples "
        f"(vs. {naive_total:,} without deduplication, "
        f"{100 * len(request_index) / naive_total:.1f}% retained)"
    )

    request_index.to_parquet(AORC_REQUEST_INDEX_PARQUET)
    print(f"Wrote request index to {AORC_REQUEST_INDEX_PARQUET}")


if __name__ == "__main__":
    main()
