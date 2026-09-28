# Methods

This document describes the cleaning pipeline in the order the code runs
it (`src/run_pipeline.sh`). It is intended as the basis for the Methods
section of the Scientific Data descriptor: each step is described
precisely, and known limitations are stated explicitly rather than
omitted.

## Overview

```
FEMA OpenFEMA claims (raw)
        |
        v
[1] inflation adjustment  (FRED PCE price index -> 2021 dollars)
        |
        v
[2] triangulation  (block group boundary) n (ZCTA boundary) n (lat/lon box)
        |
        v
[3] pluvial date correction  (causeOfDamage == "4" claims only, vs. NOAA AORC precip)
        |
        v
data/interim/claims_pluvial_corrected.parquet
```

## Input data

| Source | What | Access | License / terms |
|---|---|---|---|
| FEMA OpenFEMA | FIMA NFIP Redacted Claims v2 (`FimaNfipClaims.parquet`) | Public bulk download, no authentication | US government work, public domain |
| FRED (St. Louis Fed) | Personal consumption expenditures price index, series `DPCERD3Q086SBEA` | Public CSV endpoint, no authentication | Public |
| US Census Bureau | Cartographic boundary shapefiles: block groups and ZCTAs, staged as a small set of vintages and assigned to claims by a run-time strategy flag rather than one file per claim year (see Step 2) | Public bulk download | Public domain |
| NOAA | Analysis of Record for Calibration (AORC) v1.1, 30 arc-second (~800 m) / hourly | Public, `s3://noaa-nws-aorc-v1-1-1km`, no-sign-request | Public |

None of these sources are redistributed in this repository in raw form
(see `README.md`). Each pipeline step downloads or expects a locally
staged copy, documented at the point of use.

## Scope: CONUS, pluvial claims only

This dataset covers the 48 contiguous states and the District of
Columbia. NFIP claims also exist for Alaska, Hawaii, Puerto Rico, and
other territories, which are excluded by design rather than dropped as a
side effect.

`run_pipeline.sh` also restricts triangulation to pluvial claims
(`causeOfDamage == "4"`, `--cause-of-damage 4` on `triangulate_claims.py`
— see `paths.PLUVIAL_CAUSE_CODE`), matching the current project focus on
the AORC date-correction step, which only ever evaluates that subset
anyway. This is a flag, not a hardcoded restriction: dropping
`--cause-of-damage 4` from the pipeline invocation triangulates every
claim regardless of cause, the same way `--block-group-strategy` and
`--zcta-strategy` are flags rather than fixed choices (see Step 2). Note
that `causeOfDamage == "4"` is itself a coarse proxy for "pluvial," not a
clean label — see Step 3 for what that does and doesn't capture.

## Step 1: Inflation adjustment (`adjust_inflation.py`)

Every dollar-denominated claim field (paid amounts, coverage limits,
building/contents value, replacement cost) is rescaled to a fixed target
year's dollars using the FRED PCE price index, matched to each claim's
loss quarter. The default target year is 2021, configurable via
`--target-year`.

## Step 2: Triangulation (`triangulate_claims.py`)

FEMA's public claims extract deliberately coarsens location to protect
policyholder privacy. No street address is provided, and reported
latitude/longitude are rounded. Each claim ships with three independent,
imprecise location signals — census block group FIPS code, ZIP code, and
rounded lat/lon — each of which implies a region the claim must fall
within. Triangulation intersects whichever of these three are available
into a single polygon per claim. This places a strict upper bound on the claim's true
location, typically substantially smaller than any one source's region
alone.

### Block-group and ZCTA shapefile assignment is a flag, not a fixed rule

Census block-group and ZCTA boundaries are redrawn each decennial cycle,
so the naive assumption would be to match each claim to the boundary
vintage in effect at its loss year. Testing against the real claims data
does not support that assumption. Matching `censusBlockGroupFips` values
against the official 2010 decennial block-group release yields a 92-100%
match rate for claims in every decade back to the 1970s, including 1978 —
a full decade before block groups existed as a nationwide census
geography. This indicates that FEMA's geocoding retroactively assigns a
recent-vintage boundary to each record at processing time, rather than
using the historical loss-year boundary. There is also no sharp cutover
to the newer (2020) vintage: 77.7% of block-group GEOIDs are identical
between the 2010 and 2020 releases, and among the 23% that changed, the
match rate against the 2020 release increases gradually with loss year
rather than switching at a specific year — even claims with a 2020s loss
year are still approximately 10% exclusively-matched to the 2010 vintage.

Rather than pick one rule and defend it as *the* answer, which vintage a
claim's block-group FIPS / ZIP code gets checked against is a run-time
choice: `triangulate_claims.py`'s `--block-group-strategy` and
`--zcta-strategy` flags each independently select one of four strategies,
and `config.yaml`'s `block_group_vintages`/`zcta_vintages` just declare
which vintages are available to choose from (keyed by the vintage's own
year, not a claim decade) — adding a vintage there makes it selectable
without changing anything else.

- **`default`** — Block group: claims before
  `matching_strategy_defaults.block_group_cutover_year` (2020) use the
  2010 vintage, claims from that year on use the 2020 vintage. ZIP: claims
  before `zcta_coverage_start_year` (2000) drop the ZIP source entirely
  (ZCTAs didn't exist yet); claims from 2000 on use whichever configured
  ZCTA vintage is most recent.
- **`closest`** — whichever configured vintage's year is numerically
  closest to the claim's `yearOfLoss`, ties broken toward the newer one.
- **`most_recent`** — always the newest configured vintage, regardless of
  loss year.
- **`drop`** — never use that source at all, for either geography.

The shipped defaults differ by geography — `--block-group-strategy`
defaults to `default`, `--zcta-strategy` defaults to `closest` — because
the tradeoff each strategy makes lands differently for the two sources
(see below for why). The `default` *strategy* still exists and is fully
usable for ZIP via `--zcta-strategy default`; it just isn't what runs if
the flag is omitted.

A per-claim membership test against every available shapefile release
(`closest`, roughly) recovers more matches than the `default` decade rule
for block group, since the transition between releases is gradual rather
than sharp — prototyped and confirmed to recover matches `default` misses
in every decade. For block group, `default` is the shipped default anyway
because it's a tradeoff worth making deliberately, not avoiding: an
explicit two-vintage cutover can be stated and audited in one sentence
("pre-2020 claims use the 2010 Census block-group release, 2020-on claims
use the 2020 release"), whereas per-claim membership testing requires
justifying a data-driven selection with no documented counterpart in
FEMA's own process. The measured cost is that testing both the 2010 and
2020 releases per claim (`closest`/`most_recent` would each get most of
the way there) recovers approximately 88,000 additional CONUS claims
(3.4%) that match only the release `default` doesn't assign to their era
— `--block-group-strategy closest` is there specifically to quantify or
recover that gap when it matters more than auditability does.

For ZIP, that auditability argument is much weaker, which is why
`closest` ships as the default there instead: `default`'s own rule (drop
below 2000, most-recent above it) is already less auditable than block
group's two-vintage cutover — it's two different behaviors stitched
together at a threshold, not one clean sentence — and `closest` recovers
the pre-2000 claims that rule drops entirely (see below) for a
comparably-small cost, since ZCTA-vintage choice barely affects match
rate to begin with.

`block_group_vintage_used` / `zip_vintage_used` (see
`docs/data_dictionary.md`) record which vintage year was actually applied
to each claim, and `block_group_match_status` / `zip_match_status` record
`dropped_by_strategy` when a `drop` strategy was in effect for that
source.

Under `default`, block group draws from just the 2010 and 2020 official
decennial releases (Census's GENZ2010 release, in the legacy "gz_" naming
convention, and the modern `cb_2020` release), leaving 1,017 CONUS claims
(0.04%) matching neither — a residual accepted without further
investigation. Adding the 1990 and 2000 census-cycle releases as
additional `default` candidates was tested and found to reduce this
residual by only a few thousand claims, not justifying hardcoding them
into `default`; they're staged in `block_group_vintages` regardless, so
`closest`/`most_recent` already have access to them.

Once a vintage is selected (by whichever strategy), the match is
validated against FEMA's rounded lat/lon rather than accepted on GEOID
string match alone. FEMA rounds reported coordinates to one decimal
place, which guarantees the claim's true location falls within a 0.1 x
0.1 degree box centered on the reported point (the same box used as the
third triangulation source — see `create_lat_lon_rect`). A code that
matches the selected vintage's GEOID but whose polygon does not overlap
that box is treated as spatially inconsistent and excluded from the
claim's intersection — this doesn't trigger falling back to a different
vintage, regardless of strategy. `block_group_match_status` and
`zip_match_status` record this outcome per claim.

ZCTAs exist only from the 2000 census onward. There is no pre-2000 ZCTA
release, official or otherwise. This was verified directly: Census's
`PREVGENZ` archive contains no pre-2000 ZIP-tabulation product, and the
pre-ZCTA-era `TIGER1992` files contain raw street-segment network data
with a ZIP attribute per segment rather than assembled area polygons — a
legacy format Census's own documentation recommends against using
directly, pointing instead to NHGIS (https://www.nhgis.org) for
historical boundary needs. Unlike `censusBlockGroupFips` and
`censusTract`, `reportedZipCode` is also not a field FEMA's geocoding
service assigns. Per FEMA's own data dictionary it is raw data "as
reported by WYO partners," so the retroactive-geocoding rationale used
for the block-group vintage above does not apply to ZIP-vintage
selection — which is exactly why the `default` *strategy*'s ZIP rule is a
hard drop below 2000 rather than borrowing a later vintage the way block
group does. `closest` (the shipped default) doesn't observe that gate —
it'll check even a 1978 claim's ZIP against whichever configured vintage
is numerically nearest — which recovers real matches for pre-2000 claims
that `default` would drop outright, at negligible cost (see below).
`most_recent` also doesn't observe the gate, for the same reason.

Unlike the block-group case, ZCTA-vintage choice has little effect on
match rate: testing `reportedZipCode` against the 2000, 2010, and 2020
ZCTA releases shows all three track each other closely within any given
decade, and the `default` strategy's post-2000 rule (always the
most-recent vintage) loses only on the order of a couple thousand claims
relative to testing all three — negligible relative to the block-group
tradeoff above, and part of why shipping `closest` as ZIP's default was a
safe call: with vintage choice mattering this little, there's little risk
in also picking up the pre-2000 claims `default` drops. This is also why
`default`'s own ZIP rule doesn't need block group's two-vintage cutover
machinery: with vintage choice mattering this little, "always use
whichever configured vintage is newest" already captures nearly all the
achievable coverage, whereas an earlier version of this pipeline instead
gave each decade its own contemporaneous release (e.g. 2000s claims used
the 2000 vintage) for consistency rather than because it recovered more
matches — measurably worse than always-most-recent, so `default` no
longer does that. The larger, unrelated finding for 1970s and 1980s
claims is that a substantial share of `reportedZipCode` values are empty
strings — frequently paired with `state == "UN"` (unknown) — rather than
real ZIP codes that failed to match a ZCTA. This is a data-completeness
gap in the source records, not a boundary-matching problem.

Match rate is one thing; spatial-*validation* rate (of the claims whose
code matches a vintage's GEOID list, what fraction of those matches also
overlap the claim's lat/lon box) is another, and there the 2000 ZCTA
vintage was a real outlier: 86.5% mean validated rate vs. ~99.1% for both
2010 and 2020 (a ~12.6pt gap), which propagates directly into claims
dropped for an empty final intersection (see below) for pre-2005 claims
(`closest`, the shipped ZIP strategy, picks the 2000 vintage for every
claim before 2005 — see `vintage_for_strategy`). Two candidate explanations were tested against
each other rather than assumed: (1) the 2000 vintage's *boundaries*
genuinely differ from 2010's (real ZCTA redefinition between census
decades — ZCTAs are redrawn each census from population-weighted ZIP-code
centroids, not just redigitized), or (2) the specific *file* used for the
2000 vintage was simply lower-resolution. The 2000 vintage as originally
configured here was Census's PREVGENZ `z500shp` release
(`zt{state}_d00_shp.zip`), a 500k-scale cartographic generalization —
median 58 vertices per ZCTA polygon. Re-running the same validation check
against `tl_2009_us_zcta500.zip` (TIGER/Line, full resolution, median
1,198 vertices per ZCTA, `ZCTA5CE00` field) — the *same* 2000-census ZCTA
delineation, just not generalized — closed the entire gap: 99.1% mean
validated rate, statistically indistinguishable from 2010/2020. So
explanation (2) accounts for essentially all of it; the boundaries
weren't meaningfully wrong, the PREVGENZ file was just too coarse to
validate correctly against a 0.1-degree lat/lon box. `config.yaml`'s
`zcta_vintages.2000` now points at the TIGER/Line file for exactly this
reason.

A claim's three location sources can each individually pass validation
against the lat/lon box while failing to overlap one another — for
example, a block group and a ZCTA that are each individually plausible
given the rounded coordinates but that do not share any area. This is a
distinct failure mode from the vintage-mismatch problem described above.
`triangulate_claims.py` checks for this explicitly (an empty final
intersection despite a non-empty source list) and excludes the claim
rather than publishing a zero-area polygon; the run's printed diagnostic
count is the only trace of it; it isn't a column in the output, since
every surviving row is non-empty by construction.

## Step 3: Pluvial date correction (`build_aorc_pixel_day_index.py` / `fetch_aorc_daily_max.py` / `correct_pluvial_dates.py`)

NFIP's `causeOfDamage` field uses code `"4"` ("Other") as a catch-all
that in practice captures most rain-driven (pluvial) flood claims, but it
is a coarse proxy rather than a clean pluvial-only label. Some
non-pluvial claims may be coded `"4"` for unrelated reasons. This code is
used as the best available filter, not as ground truth, and this
limitation applies to the correction described below rather than being
resolved by it.

For each claim coded `"4"`, the reported `dateOfLoss` is checked against
whether it coincides with a day of meaningful precipitation (maximum
hourly APCP exceeding a threshold, default 7.62 mm/hr — NOAA/NWS's
"Heavy Rain" bar, 0.30 in/hr) at the claim's
triangulated location, using NOAA's public AORC v1.1 archive
(30 arc-second / ~800 m grid, hourly, CONUS, 1979-present). If it does not, a
`search_window_days` window (default +/- 7 days) is searched for the
wettest day, and if that day clears the threshold it is treated as the
corrected date. The original `dateOfLoss` is never overwritten.
`correctedDateOfLoss` and `pluvialCorrectionStatus` record the outcome
per claim (see `docs/data_dictionary.md` for status values), allowing
downstream users to decide whether to apply the correction.

The threshold itself is a "grid" (`precip_threshold.py`), not a bare
constant — there's no principled reason a desert claim and a rainforest
claim need the same number of mm to count as meaningful rain. Two
implementations share one interface (`grid(lat, lon)` for a single point,
`grid.resolve_for_claims(lats, lons)` for a vectorized batch — see the
module for why the batched form is the one real runs use):

- `UniformThresholdGrid` — the default, and what this pipeline used
  before this module existed: the same constant everywhere.
  `--min-hourly-precip-mm` overrides `config.yaml`'s
  `pluvial_correction.min_hourly_precip_mm` for a single run without
  editing the config file.
- `NetCDFThresholdGrid` — `--threshold-netcdf <path>` samples a
  user-supplied NetCDF nearest-neighbor at each claim's location instead;
  `--threshold-netcdf-var` picks the data variable if the file has more
  than one. The file doesn't need to be on the AORC grid or any
  particular resolution. Since each claim's exact (lat, lon) isn't
  persisted in `CLAIM_PIXEL_LOOKUP_PARQUET` (only its 4 bilinear corner
  pixel indices are, to keep that table small), it's reconstructed from
  the weighted sum of the 4 corners' coordinates using the same weights
  used for precipitation itself (`aorc_grid.pixel_to_latlon`) — this is
  exact, not an approximation, since both the corner lookup and the
  reconstruction are affine in row/col.

The two CLI flags are mutually exclusive. Tested end to end with a
synthetic coarse NetCDF (threshold 5mm west of -95 deg, an unreachable
999mm east of it): the correction rate dropped from 16.8% (uniform 5mm)
to 4.3%, confirming the spatial lookup is genuinely per-claim rather than
falling back to a single value.

Day boundaries are evaluated in each claim's own local time zone rather
than a single national reference clock. `timezone_utils.py` resolves the
IANA time zone for every AORC pixel referenced by a claim (via
`timezonefinder`, computed once per unique pixel), and
`fetch_aorc_daily_max.py` converts each local calendar day to the correct
UTC hour range using that zone, correctly handling daylight saving
transitions (a local day spans 23 or 25 hours, not always 24, across a
spring-forward or fall-back date) rather than assuming a fixed offset. A
local day's hours can also span two different UTC calendar years, since
every US time zone is behind UTC and a December 31 local day's final
hours fall in UTC on January 1 of the following year. The fetch step
groups by the UTC year each hour actually falls in, rather than by the
request's local date, so these hours are not dropped.

**Coverage.** AORC begins 1979-01-01 for CONUS. Pluvial claims before
this date are not evaluated (`pluvialCorrectionStatus =
"before_aorc_coverage"`) and retain their reported date rather than being
dropped from the dataset. Pluvial correction is also restricted to CONUS:
claims whose location falls outside the AORC CONUS grid extent
(approximately 20-55N, 130-60W — including Puerto Rico and other
territories) receive `pluvialCorrectionStatus =
"outside_aorc_grid_extent"` and are likewise retained with their reported
date.

**Data volume.** Rather than downloading AORC's full hourly CONUS
archive (on the order of multiple terabytes for the full 1979-present
period of record), the pipeline computes the substantially smaller set of
distinct (pixel, day) pairs actually referenced by any pluvial claim's
search window (`build_aorc_pixel_day_index.py`), fetches only those
values via vectorized reads against the public Zarr store
(`fetch_aorc_daily_max.py`), and retains only the daily maximum rather
than the full hourly series. Claims sharing a flood event collapse onto
overlapping pixel/day requests, so the deduplicated index is
substantially smaller than `n_claims * window_size`. The achieved
reduction is printed at run time.

## Known limitations

- **CONUS only.** Alaska, Hawaii, Puerto Rico, and other territories are
  excluded (see "Scope" above) because shapefile and AORC coverage do not
  extend there, not because their claims are lower quality.
- **Pluvial only, by default.** `run_pipeline.sh` restricts triangulation
  to `causeOfDamage == "4"` claims (see "Scope" above); the rest of the
  claims population is triangulated correctly if `--cause-of-damage` is
  dropped, but isn't part of the default run's output.
- Under the `default` matching strategy, a small residual of CONUS claims
  with a block-group FIPS (0.04%, 1,017 claims) matches neither the 2010
  nor the 2020 block-group release — accepted without further
  investigation into the specific cause (see Step 2); `--block-group-strategy
  closest`/`most_recent` can recover some of these if it matters more than
  the residual's size suggests it should.
- `causeOfDamage == "4"` is a coarse proxy for "pluvial," not a clean
  label (see Step 3).
- The `default` *strategy* (not the shipped default for ZIP, which is
  `closest` — see Step 2) cannot use a ZCTA boundary before 2000. `closest`
  and `most_recent` don't have this limitation, and check pre-2000 claims
  against whichever configured ZCTA vintage applies under their own rule.
- Pre-1979 claims are not eligible for pluvial date correction, since
  AORC coverage begins in 1979 (see Step 3).
- The precip threshold is spatially uniform by default
  (`UniformThresholdGrid`, see Step 3); a real spatially-varying grid
  requires supplying one via `--threshold-netcdf` — none ships with the
  pipeline.
- **`data/processed/` is not currently populated.**
  `data/interim/claims_pluvial_corrected.parquet` is the actual
  end-of-pipeline output today, despite `data/processed/` being described
  elsewhere as where the released dataset lives.
