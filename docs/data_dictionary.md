# Data dictionary

Current end-of-pipeline output: `data/interim/claims_pluvial_corrected.parquet`.

All original columns from the FEMA OpenFEMA `FimaNfipClaims` extract are
preserved unchanged (see FEMA's own data dictionary for those:
https://www.fema.gov/openfema-data-page/fima-nfip-redacted-claims-v2).
The columns below are the ones this pipeline adds, grouped by the step
that produces them — see `docs/methods.md` for what each step does.

## Added by inflation adjustment (`adjust_inflation.py`)

| Column | Type | Description |
|---|---|---|
| `{field}Real2021` | float | Each of the 15 dollar fields listed in `DOLLAR_FIELDS` (paid amounts, coverage limits, building/contents value, replacement cost), rescaled to 2021 dollars via the FRED PCE price index. |
| `amountPaid` | float | Sum of the three `amountPaidOn*Claim` fields, nominal dollars. |
| `amountPaidReal2021` | float | Same sum, 2021 dollars. |
| `damageAmount` | float | `buildingDamageAmount + contentsDamageAmount`, nominal dollars. |
| `damageAmountReal2021` | float | Same sum, 2021 dollars. |

## Added by triangulation (`triangulate_claims.py`)

| Column | Type | Description |
|---|---|---|
| `geometry` | polygon (EPSG:5070) | Intersection of whichever of {block-group boundary, ZCTA boundary, lat/lon box} were available *and spatially validated* for the claim (see `docs/methods.md`) — a strict upper bound on the claim's true location. |
| `n_geometry_sources` | int | How many of the three sources contributed (1-3) in this file. Claims with 0 contributing sources, or whose sources intersect to an empty polygon, are dropped before this file is written. |
| `geometry_sources` | string | Which sources contributed, e.g. `"block_group+zip+latlon"`, `"block_group+latlon"`. |
| `block_group_vintage_used` | int or null | Which vintage year (e.g. `1990`, `2000`, `2010`, `2020`) `--block-group-strategy` selected for this claim — a lookup key into `config.yaml`'s `block_group_vintages`. Null when the strategy is `drop`, or when no vintage was selected for another reason. Independent of `zip_vintage_used`: the two strategies (and therefore the two vintages chosen) don't have to agree, even both left at `default` — see `docs/methods.md`. |
| `zip_vintage_used` | int or null | Same, for `--zcta-strategy` and `config.yaml`'s `zcta_vintages`. |
| `block_group_match_status` | string | `not_found`, `validated`, `unvalidated_no_latlon`, `spatially_inconsistent`, `dropped_by_strategy` (`--block-group-strategy drop`), or `no_vintage_selected` — see `select_validated_geometry` / `triangulate_geometry` in `triangulate_claims.py`. |
| `zip_match_status` | string | Same status values as `block_group_match_status`, plus `before_zcta_coverage`: under the `default` strategy specifically, claims with `yearOfLoss` before `zcta_coverage_start_year` (2000) get this status rather than `dropped_by_strategy` — no ZCTA vintage existed yet, so the ZIP source isn't attempted at all. (`closest`/`most_recent` don't observe this gate and will check even pre-2000 claims against a modern ZCTA — deliberately, see `docs/methods.md`.) |

## Added by pluvial date correction (`build_aorc_pixel_day_index.py` / `fetch_aorc_daily_max.py` / `correct_pluvial_dates.py`)

Only meaningful for `causeOfDamage == "4"` claims; see `docs/methods.md`
for why that code is used and its limits as a pluvial-claim filter.

| Column | Type | Description |
|---|---|---|
| `correctedDateOfLoss` | date | Reported `dateOfLoss` if it already coincided with meaningful precipitation, the wettest nearby day if a correction was made, otherwise unchanged from `dateOfLoss`. Always populated (equals `dateOfLoss` when not corrected or not evaluated). |
| `pluvialCorrectionStatus` | string | One of: `not_pluvial`, `before_aorc_coverage`, `outside_aorc_grid_extent`, `accepted_as_reported`, `corrected`, `no_qualifying_precip_in_window`, `no_aorc_data_in_window`. |
| `reportedDateMaxPrecipMm` | float | Max hourly precipitation (mm) found at the claim's location on the originally reported `dateOfLoss`, before any correction — the value checked against the threshold to decide whether correction is attempted at all. |
| `pluvialCorrectionMaxPrecipMm` | float | Max daily precipitation (mm) found at the claim's location on the date `correctedDateOfLoss` refers to. `NaN` when no AORC data was available in the search window. |

## Coordinate reference systems

- `geometry` is in **EPSG:5070** (Albers Equal Area), meters — the CRS
  used throughout the pipeline for area calculations.
- Original FEMA `latitude`/`longitude` fields remain in WGS84 (EPSG:4326)
  degrees, unchanged.
