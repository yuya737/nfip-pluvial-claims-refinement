"""Triangulate a spatial uncertainty polygon for each claim.

Each NFIP claim record ships with three independent, imprecise location
signals: a census block group FIPS code, a reported ZIP code, and a
lat/lon pair (itself deliberately coarsened by FEMA to 1 decimal place
for privacy). None of these alone pins down a claim's location; each
implies a region the claim must fall within. We intersect whichever of
the three are available for a given claim into a single polygon
("triangulation"), which is a strict upper bound on the claim's true
location and is usually far smaller than any single source's region alone.

Matching strategy is a flag, not a fixed choice
--------------------------------------------------
Which boundary vintage a claim's block-group FIPS / ZIP code gets checked
against is a real methodological judgment call, and reasonable people can
weigh the tradeoffs differently. Rather than bake in one answer,
`--block-group-strategy` and `--zcta-strategy` each independently select
one of:

  default       Block group: < matching_strategy_defaults.block_group_cutover_year
                (2020) uses the 2010 vintage, >= it uses the 2020 vintage —
                matches the empirical crossover in real censusBlockGroupFips
                values. ZIP: < zcta_coverage_start_year
                (2000) is dropped entirely (no ZCTA vintage existed yet, and
                unlike block group, FEMA doesn't geocode ZIP — it's raw WYO-
                reported data, so there's no vintage-drift argument for
                checking it against a boundary that postdates it); >= 2000
                uses whichever configured ZCTA vintage is most recent.
  closest       Whichever configured vintage's year is numerically closest
                to the claim's yearOfLoss, ties broken toward the newer one.
  most_recent   Always the newest configured vintage, regardless of year.
  drop          Never use this source at all.

config.yaml's block_group_vintages/zcta_vintages just declare what's
available (keyed by the vintage's own year); which one a given claim
actually uses is decided per-claim at run time from the strategy above,
so adding a vintage there makes it selectable by closest/most_recent
without touching anything else.

Validating matches against the lat/lon box
---------------------------------------------
FEMA's lat/lon fields are rounded to 1 decimal place, which means the
claim's true location is guaranteed to fall within a 0.1 x 0.1 degree box
centered on the reported point (create_lat_lon_rect). That's a genuine
spatial constraint, not just another imprecise source, so we use it to
validate block-group/ZIP matches rather than trusting a GEOID string
match blindly: a code that matches the shapefile's GEOID list but whose
polygon does *not* overlap the lat/lon box is treated as spatially
inconsistent and excluded from that claim's intersection, rather than
combined in to produce an empty or misleading polygon.
"""

import argparse
import glob
from typing import Dict, Optional, Tuple

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry
from tqdm import tqdm

from conus import is_conus_block_group_fips
from paths import (
    SHAPEFILE_ROOT,
    BLOCK_GROUP_VINTAGES,
    ZCTA_VINTAGES,
    BLOCK_GROUP_DEFAULT_CUTOVER_YEAR,
    ZCTA_DEFAULT_COVERAGE_START_YEAR,
    INFLATION_ADJUSTED_PARQUET,
    TRIANGULATED_PARQUET,
)

GeometryByCode = Dict[str, BaseGeometry]
GeometryByVintage = Dict[int, GeometryByCode]

STRATEGIES = ["default", "closest", "most_recent", "drop"]


def create_lat_lon_rect(lat: float, lon: float, buffer_degrees: float = 0.05):
    """Rectangular box around a lat/lon point, in WGS84 (EPSG:4326).

    buffer_degrees=0.05 gives a 0.1-degree box, matching FEMA's stated
    1-decimal-place rounding of the reported coordinates (assuming
    round-to-nearest; FEMA's data dictionary doesn't specify truncation
    vs. rounding, and we assume the former as the more standard practice).
    """
    return box(
        lon - buffer_degrees,
        lat - buffer_degrees,
        lon + buffer_degrees,
        lat + buffer_degrees,
    )


# ---------------------------------------------------------------------------
# Loading vintages
# ---------------------------------------------------------------------------


def _spec_files(spec):
    if "glob" in spec:
        return glob.glob(str(SHAPEFILE_ROOT / spec["glob"]))
    return [str(SHAPEFILE_ROOT / spec["file"])]


def load_block_group_spec(spec) -> GeometryByCode:
    files = _spec_files(spec)
    if not files:
        raise FileNotFoundError(f"No block-group files matched {spec}")
    geoms = {}
    for f in tqdm(files, desc=f"  block groups ({spec.get('glob') or spec.get('file')})"):
        columns = gpd.read_file(f, rows=0).columns
        if "GEOID" in columns:
            gdf = gpd.read_file(f)
            geoms.update(zip(gdf["GEOID"], gdf["geometry"]))
        elif "GEO_ID" in columns:
            # GENZ2010's old "gz_" naming ships GEO_ID like
            # "1500000US060014057002" (summary-level prefix + 12-digit GEOID).
            gdf = gpd.read_file(f)
            geoms.update(zip(gdf["GEO_ID"].str[-12:], gdf["geometry"]))
        elif {"STATE", "COUNTY", "TRACT", "BLKGROUP"}.issubset(columns):
            # The 2000-vintage PREVGENZ release ships no GEOID field at all;
            # TRACT is inconsistently zero-padded across states, so it has
            # to be zfill(6)'d before concatenating.
            gdf = gpd.read_file(f)
            geoid = gdf["STATE"] + gdf["COUNTY"] + gdf["TRACT"].str.zfill(6) + gdf["BLKGROUP"]
            geoms.update(zip(geoid, gdf["geometry"]))
        else:
            raise KeyError(f"{f}: no GEOID/GEO_ID/STATE+COUNTY+TRACT+BLKGROUP columns found ({list(columns)})")
    return geoms


def load_zcta_spec(spec) -> GeometryByCode:
    files = _spec_files(spec)
    if not files:
        raise FileNotFoundError(f"No ZCTA files matched {spec}")
    geoms = {}
    for f in files:
        gdf = gpd.read_file(f, columns=[spec["field"]])
        geoms.update(zip(gdf[spec["field"]], gdf["geometry"]))
    return geoms


def load_all_vintages(vintages: dict, load_one_spec) -> GeometryByVintage:
    result = {}
    for year, spec in vintages.items():
        print(f"  loading {year} vintage: {spec}")
        result[year] = load_one_spec(spec)
        print(f"    {len(result[year]):,} geometries")
    return result


# ---------------------------------------------------------------------------
# Matching strategies
# ---------------------------------------------------------------------------


def closest_vintage(year, vintages: GeometryByVintage) -> Optional[int]:
    if not vintages:
        return None
    if year is None or pd.isna(year):
        return max(vintages)
    year = int(year)
    # Tie-break toward the newer vintage.
    return min(vintages, key=lambda v: (abs(v - year), -v))


def most_recent_vintage(vintages: GeometryByVintage) -> Optional[int]:
    return max(vintages) if vintages else None


def block_group_default_vintage(year, vintages: GeometryByVintage, cutover_year: int) -> int:
    if 2010 not in vintages or 2020 not in vintages:
        raise KeyError(
            "block-group 'default' strategy requires both the 2010 and 2020 vintages "
            f"to be configured in block_group_vintages (have: {sorted(vintages)})"
        )
    if year is None or pd.isna(year) or year < cutover_year:
        return 2010
    return 2020


def zcta_default_vintage(year, vintages: GeometryByVintage, coverage_start_year: int) -> Optional[int]:
    if year is None or pd.isna(year) or year < coverage_start_year:
        return None
    return most_recent_vintage(vintages)


def vintage_for_strategy(strategy: str, year, vintages: GeometryByVintage, default_fn) -> Optional[int]:
    if strategy == "drop":
        return None
    if strategy == "most_recent":
        return most_recent_vintage(vintages)
    if strategy == "closest":
        return closest_vintage(year, vintages)
    if strategy == "default":
        return default_fn(year, vintages)
    raise ValueError(f"Unknown matching strategy {strategy!r}; expected one of {STRATEGIES}")


def select_validated_geometry(
    code, geometry_by_code: GeometryByCode, latlon_box
) -> Tuple[Optional[BaseGeometry], str]:
    """Look up a code in a single vintage's geometry set, spatially validated.

    Returns (geometry, status), where status is one of:
      not_found                code isn't in this vintage's shapefile
      validated                the polygon overlaps latlon_box
      unvalidated_no_latlon    a match exists but there's no lat/lon to check it against
      spatially_inconsistent   the polygon doesn't overlap latlon_box
    """
    geom = geometry_by_code.get(code)
    if geom is None:
        return None, "not_found"
    if latlon_box is None:
        return geom, "unvalidated_no_latlon"
    if geom.intersects(latlon_box):
        return geom, "validated"
    return None, "spatially_inconsistent"


def triangulate_geometry(
    row,
    block_group_vintages: GeometryByVintage,
    zcta_vintages: GeometryByVintage,
    bg_strategy: str,
    zcta_strategy: str,
    bg_cutover_year: int,
    zcta_coverage_start_year: int,
):
    latlon_box = None
    if pd.notna(row["latitude"]) and pd.notna(row["longitude"]):
        latlon_box = create_lat_lon_rect(row["latitude"], row["longitude"])

    year = row.get("yearOfLoss")
    geometries, sources = [], []

    bg_vintage = vintage_for_strategy(
        bg_strategy, year, block_group_vintages,
        lambda y, v: block_group_default_vintage(y, v, bg_cutover_year),
    )
    if bg_vintage is None:
        bg_geom = None
        bg_status = "dropped_by_strategy" if bg_strategy == "drop" else "no_vintage_selected"
    else:
        bg_geom, bg_status = select_validated_geometry(
            row["censusBlockGroupFips"], block_group_vintages[bg_vintage], latlon_box
        )
    if bg_geom is not None:
        geometries.append(bg_geom)
        sources.append("block_group")

    zip_vintage = vintage_for_strategy(
        zcta_strategy, year, zcta_vintages,
        lambda y, v: zcta_default_vintage(y, v, zcta_coverage_start_year),
    )
    if zip_vintage is None:
        zip_geom = None
        if zcta_strategy == "drop":
            zip_status = "dropped_by_strategy"
        elif zcta_strategy == "default" and (year is None or pd.isna(year) or year < zcta_coverage_start_year):
            zip_status = "before_zcta_coverage"
        else:
            zip_status = "no_vintage_selected"
    else:
        zip_geom, zip_status = select_validated_geometry(
            row["reportedZipCode"], zcta_vintages[zip_vintage], latlon_box
        )
    if zip_geom is not None:
        geometries.append(zip_geom)
        sources.append("zip")

    if latlon_box is not None:
        geometries.append(latlon_box)
        sources.append("latlon")

    if len(geometries) == 0:
        geometry, is_empty = None, None
    else:
        geometry = shapely.intersection_all(geometries)
        is_empty = geometry.is_empty

    return pd.Series(
        {
            "geometry": geometry,
            "n_geometry_sources": len(geometries),
            "geometry_sources": "+".join(sources),
            "geometry_is_empty": is_empty,
            "block_group_vintage_used": bg_vintage,
            "block_group_match_status": bg_status,
            "zip_vintage_used": zip_vintage,
            "zip_match_status": zip_status,
        }
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", default=str(INFLATION_ADJUSTED_PARQUET))
    parser.add_argument("--output", default=str(TRIANGULATED_PARQUET))
    parser.add_argument("--block-group-strategy", choices=STRATEGIES, default="default")
    parser.add_argument("--zcta-strategy", choices=STRATEGIES, default="default")
    parser.add_argument(
        "--cause-of-damage",
        default=None,
        help='Restrict to a single causeOfDamage code, e.g. "4" for pluvial claims '
        "(see paths.PLUVIAL_CAUSE_CODE). Default: no filter, all claims.",
    )
    args = parser.parse_args()

    print(f"Block-group matching strategy: {args.block_group_strategy}")
    print(f"ZCTA matching strategy: {args.zcta_strategy}")

    print(f"\nLoading claims from {args.input}...")
    claims_df = pd.read_parquet(args.input)
    print(f"Loaded {len(claims_df):,} claims")

    if args.cause_of_damage is not None:
        claims_df = claims_df[claims_df["causeOfDamage"] == args.cause_of_damage]
        print(f"Restricted to causeOfDamage == {args.cause_of_damage!r}: {len(claims_df):,} claims")

    claims_df = claims_df.dropna(subset=["censusBlockGroupFips", "reportedZipCode"])
    print(f"Filtered to {len(claims_df):,} claims with a block-group FIPS and ZIP code")

    is_conus = claims_df["censusBlockGroupFips"].map(is_conus_block_group_fips)
    print(
        f"Excluding {(~is_conus).sum():,} non-CONUS claims (Alaska, Hawaii, Puerto Rico, "
        f"other territories — see conus.py); keeping {is_conus.sum():,}"
    )
    claims_df = claims_df[is_conus]

    print("\nLoading block-group vintages...")
    block_group_vintages = load_all_vintages(BLOCK_GROUP_VINTAGES, load_block_group_spec)
    print("Loading ZCTA vintages...")
    zcta_vintages = load_all_vintages(ZCTA_VINTAGES, load_zcta_spec)

    claims_df[["latitude", "longitude"]] = claims_df[["latitude", "longitude"]].astype(float)

    print("\nTriangulating claim geometries...")
    tqdm.pandas()
    tri_result = claims_df.progress_apply(
        triangulate_geometry,
        args=(
            block_group_vintages,
            zcta_vintages,
            args.block_group_strategy,
            args.zcta_strategy,
            BLOCK_GROUP_DEFAULT_CUTOVER_YEAR,
            ZCTA_DEFAULT_COVERAGE_START_YEAR,
        ),
        axis=1,
    )
    claims_df[tri_result.columns] = tri_result

    successful = claims_df["geometry"].notna().sum()
    print(
        f"\nTriangulated {successful:,} / {len(claims_df):,} claims "
        f"({100 * successful / len(claims_df):.1f}%)"
    )
    print("\nSource-combination breakdown:")
    for combo, count in claims_df["geometry_sources"].value_counts().items():
        label = combo if combo else "(none)"
        print(f"  {label}: {count:,} ({100 * count / len(claims_df):.1f}%)")

    n_empty = claims_df["geometry_is_empty"].fillna(False).sum()
    if n_empty:
        print(
            f"\n{n_empty:,} claims had a non-empty source list but an EMPTY final "
            "intersection (sources validated individually against lat/lon but don't "
            "overlap each other)"
        )
    print("\nBlock-group match status breakdown:")
    print(claims_df["block_group_match_status"].value_counts())
    print("\nZIP match status breakdown:")
    print(claims_df["zip_match_status"].value_counts())

    has_geometry = claims_df["geometry"].notna() & ~claims_df["geometry_is_empty"].fillna(True)
    claims_gdf = (
        gpd.GeoDataFrame(claims_df[has_geometry], geometry="geometry")
        .set_crs("EPSG:4326")
        .to_crs("EPSG:5070")
    )

    print(f"\nMean claim area:   {claims_gdf.geometry.area.mean() / 1e6:.3f} km^2")
    print(f"Median claim area: {claims_gdf.geometry.area.median() / 1e6:.3f} km^2")

    claims_gdf.to_parquet(args.output)
    print(f"\nWrote {len(claims_gdf):,} rows to {args.output}")


if __name__ == "__main__":
    main()
