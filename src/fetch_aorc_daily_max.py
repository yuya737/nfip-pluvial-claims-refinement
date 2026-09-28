"""Fetch exactly the pixel/day hourly values in the request index from public AORC.

Reads directly from s3://noaa-nws-aorc-v1-1-1km (anonymous, no AWS account
needed) — see aorc_grid.py for how the store is laid out. For every
(pixel, day, timezone) triple in the request index
(build_aorc_pixel_day_index.py) this pulls the hourly APCP_surface
values covering that *local* calendar day (correctly handling DST — see
timezone_utils.py) and keeps the full hourly series (paired with its UTC
timestamps), not just a scalar max. We still never materialize the full
hourly grid — only the (pixel, day) values actually referenced by some
claim's bilinear-interpolation corners.

The full series (rather than a precomputed max) is needed because
correct_pluvial_dates.py bilinearly interpolates the 4 corner pixels'
*hourly* values at each claim's exact location before taking the day's
max — see docs/methods.md for why that ordering matters. Since 04a
resolves time zone once per claim and applies it to all 4 of that claim's
corners, the 4 corners' hourly series are guaranteed to share identical
UTC timestamps for a given claim-day, so 04c can combine them with a
plain elementwise weighted sum.

A local day converts to a UTC range that isn't always exactly 24 hours
(23 or 25 across a DST transition) and, for Dec 31 specifically, spills
a few hours into the *next* year's Zarr store, since every US time zone
sits behind UTC. This is handled by computing each request's UTC hours
first and grouping by whichever store-year each hour actually falls in,
rather than assuming a request's hours all live in its local date's year.

Uses xarray's vectorized ("fancy") indexing so the (pixel, hour) values we
need are read as one batched request per store-year rather than the cross
product of all pixels x all hours in that year.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import xarray as xr

from paths import AORC_REQUEST_INDEX_PARQUET, AORC_HOURLY_PARQUET

AORC_S3_TEMPLATE = "s3://noaa-nws-aorc-v1-1-1km/{year}.zarr"
YEAR_FETCH_WORKERS = 6
REQUEST_INDEX_CHUNK_SIZE = 2_000_000
KEY_COLS = ["aorc_pixel_row", "aorc_pixel_col", "iana_timezone", "date"]
NEW_ROWS_TMP_PARQUET = AORC_HOURLY_PARQUET.with_suffix(".new.parquet")
MERGED_TMP_PARQUET = AORC_HOURLY_PARQUET.with_suffix(".merged.parquet")


def local_day_utc_hours(date: pd.Timestamp, iana_timezone: str) -> pd.DatetimeIndex:
    """UTC hourly timestamps spanning one local calendar day at the given time zone.

    Localizing midnight specifically (rather than an arbitrary hour) is safe
    from DST ambiguity/nonexistence for every US zone, since their DST
    transitions happen at 2 a.m. local, never at midnight.
    """
    tz = ZoneInfo(iana_timezone)
    start_local = pd.Timestamp(date.year, date.month, date.day).tz_localize(tz)
    end_local = start_local + pd.Timedelta(days=1)
    start_utc = start_local.tz_convert("UTC").tz_localize(None)
    end_utc = end_local.tz_convert("UTC").tz_localize(None)
    return pd.date_range(start_utc, end_utc, freq="1h", inclusive="left")


def explode_to_hours(request_index: pd.DataFrame) -> pd.DataFrame:
    """One row per (group_id, store_year, hour_of_store_year, pixel_row, pixel_col, utc_hour).

    group_id is the row position in request_index (0-indexed) that this
    hour belongs to, so results can be grouped back per (pixel, day) later.

    local_day_utc_hours depends only on (date, timezone), not on which
    pixel — and the number of distinct (date, timezone) pairs is far
    smaller than the number of (pixel, date, timezone) request rows (many
    pixels share a date and time zone). So we compute it once per unique
    (date, timezone) pair, then broadcast to every matching pixel row via
    a vectorized merge, instead of once per request-index row — the
    difference between a few hundred thousand Python-level calls and tens
    of millions.
    """
    unique_date_tz = request_index[["date", "iana_timezone"]].drop_duplicates().reset_index(drop=True)
    print(f"  {len(unique_date_tz):,} unique (date, timezone) pairs behind {len(request_index):,} pixel rows")

    date_tz_id, utc_hour = [], []
    for i, dt in enumerate(unique_date_tz.itertuples(index=False)):
        hours = local_day_utc_hours(dt.date, dt.iana_timezone)
        date_tz_id.extend([i] * len(hours))
        utc_hour.extend(hours.tolist())

    hours_by_date_tz = pd.DataFrame({"date_tz_id": date_tz_id, "utc_hour": utc_hour})
    years = pd.DatetimeIndex(hours_by_date_tz["utc_hour"]).year
    year_start = pd.to_datetime(years.astype(str) + "-01-01")
    hours_by_date_tz["store_year"] = years
    hours_by_date_tz["hour_of_year"] = (
        (hours_by_date_tz["utc_hour"] - year_start) / pd.Timedelta(hours=1)
    ).astype(int)
    hours_by_date_tz = hours_by_date_tz.merge(
        unique_date_tz.reset_index(names="date_tz_id"), on="date_tz_id"
    )

    result = (
        request_index.reset_index(names="group_id")
        .merge(
            hours_by_date_tz[["date", "iana_timezone", "utc_hour", "store_year", "hour_of_year"]],
            on=["date", "iana_timezone"],
            how="left",
        )
    )
    return result[["group_id", "store_year", "hour_of_year", "aorc_pixel_row", "aorc_pixel_col", "utc_hour"]]


def fetch_store_year(year: int, hours_for_year: pd.DataFrame) -> pd.DataFrame:
    """Fetch APCP_surface at the given (pixel, hour_of_year) triples for one AORC year.

    AORC publishing lags real time, so a store-year requested by a claim's
    search window (e.g. a Dec-2025-loss claim's window, or the window
    itself spilling into the next UTC year — see 04a) may not exist yet.
    That's treated as "no data available" for those hours, not a fatal
    error — the claim still gets evaluated using whatever other candidate
    days in its window do have data.
    """
    store = AORC_S3_TEMPLATE.format(year=year)
    print(f"  Opening {store} ...")
    try:
        ds = xr.open_dataset(store, engine="zarr", storage_options={"anon": True}, mask_and_scale=True)
    except Exception as e:
        print(f"  {store} not available ({e.__class__.__name__}); treating {len(hours_for_year):,} hour(s) as missing")
        return pd.DataFrame(columns=["group_id", "utc_hour", "precip_mm"])

    valid = hours_for_year["hour_of_year"] < ds.sizes["time"]
    if not valid.all():
        print(f"  {(~valid).sum():,} hour(s) fell outside {year}'s time range, dropping")
    hours_for_year = hours_for_year[valid].copy()

    points = dict(
        time=xr.DataArray(hours_for_year["hour_of_year"].values, dims="points"),
        latitude=xr.DataArray(hours_for_year["aorc_pixel_row"].values, dims="points"),
        longitude=xr.DataArray(hours_for_year["aorc_pixel_col"].values, dims="points"),
    )
    hours_for_year["precip_mm"] = ds["APCP_surface"].isel(**points).values  # NaN where missing
    return hours_for_year[["group_id", "utc_hour", "precip_mm"]]


def process_chunk(chunk: pd.DataFrame) -> pd.DataFrame:
    """Fetch and aggregate one slice of the request index into hourly-array rows.

    Keeps chunk's original (global) index throughout — explode_to_hours
    turns that index into the group_id column, so the final join back
    below aligns correctly without needing to track a separate id.
    """
    hourly = explode_to_hours(chunk)
    print(f"  {len(hourly):,} hourly reads across {hourly['store_year'].nunique()} store-year(s)")

    year_groups = {int(year): g for year, g in hourly.groupby("store_year")}
    with ThreadPoolExecutor(max_workers=YEAR_FETCH_WORKERS) as executor:
        fetched_frames = list(
            executor.map(lambda item: fetch_store_year(*item), year_groups.items())
        )
    fetched = pd.concat(fetched_frames, ignore_index=True).sort_values(["group_id", "utc_hour"])

    grouped = fetched.groupby("group_id").agg(
        hours_utc=("utc_hour", list), precip_mm=("precip_mm", list)
    )
    result = chunk.join(grouped)
    # Explicit None (not float NaN) for unmatched rows' list columns, so
    # pyarrow reads a clean null in the list<...> column instead of having
    # to reconcile a stray float among otherwise-list values across chunks.
    result["hours_utc"] = result["hours_utc"].where(result["hours_utc"].notna(), None)
    result["precip_mm"] = result["precip_mm"].where(result["precip_mm"].notna(), None)
    return result


def missing_request_rows(request_index: pd.DataFrame) -> pd.DataFrame:
    """request_index rows not already present in AORC_HOURLY_PARQUET, by (pixel, day, timezone).

    Re-running this script (e.g. after triangulation picks up more claims)
    used to always re-fetch every requested (pixel, day, timezone) triple
    from S3 from scratch, even the ones an earlier run already pulled --
    slow and wasteful of a public NOAA bucket's bandwidth. Only the key
    columns are read here (not the heavy hours_utc/precip_mm list columns),
    so checking what's already fetched is cheap even against a large
    existing file.
    """
    if not AORC_HOURLY_PARQUET.exists():
        print("No existing output found -- fetching all requested (pixel, day, timezone) triples")
        return request_index
    existing = pd.read_parquet(AORC_HOURLY_PARQUET, columns=KEY_COLS).drop_duplicates()
    print(f"Found existing {AORC_HOURLY_PARQUET} with {len(existing):,} (pixel, day, timezone) triples already fetched")
    merged = request_index.merge(existing, on=KEY_COLS, how="left", indicator=True)
    missing = merged.loc[merged["_merge"] == "left_only", KEY_COLS].reset_index(drop=True)
    print(f"{len(missing):,} / {len(request_index):,} triples not yet fetched -- pulling only these")
    return missing


def stream_copy(src_path, writer: pq.ParquetWriter, batch_size=200_000):
    """Copy one parquet file's rows into an already-open writer, batch by batch.

    Never materializes the source as a pandas DataFrame -- hours_utc/precip_mm
    are list<...> columns that are cheap in Arrow's columnar layout but heavy
    once unpacked into Python list objects (see process_chunk's docstring
    below), so merging old+new output by reading both fully into pandas and
    concatenating would reintroduce the same OOM risk that made this script
    chunk its fetches in the first place.
    """
    pf = pq.ParquetFile(src_path)
    for batch in pf.iter_batches(batch_size=batch_size):
        table = pa.Table.from_batches([batch]).cast(writer.schema)
        writer.write_table(table)


def main():
    """Fetch only the not-yet-fetched chunk of the request index, and merge
    the result into AORC_HOURLY_PARQUET alongside whatever was already there."""
    print(f"Loading request index from {AORC_REQUEST_INDEX_PARQUET}...")
    request_index = pd.read_parquet(AORC_REQUEST_INDEX_PARQUET)
    request_index["date"] = pd.to_datetime(request_index["date"])
    print(f"{len(request_index):,} unique (pixel, day, timezone) triples requested")

    had_existing_output = AORC_HOURLY_PARQUET.exists()
    request_index = missing_request_rows(request_index)
    if len(request_index) == 0:
        print(f"\nNothing new to fetch -- {AORC_HOURLY_PARQUET} already covers every requested triple.")
        return

    # Processed in chunks, not all at once: exploding to hourly rows and
    # fetching/sorting/grouping them is memory-hungry (each step roughly
    # duplicates a ~24x-larger intermediate table). Each chunk's finished
    # result is written to disk immediately and dropped, rather than
    # accumulated in memory for one final concat — a list of Python
    # datetime/float objects (what a "list of hourly values" column holds
    # in pandas) is far heavier per row than the same data packed into a
    # parquet file, so holding every chunk's result until the end amounts
    # to holding close to the entire final output's memory footprint
    # anyway. Both this and the un-chunked original design OOM-killed an
    # earlier version of this script; only writing incrementally fixed it.
    n = len(request_index)
    n_chunks = -(-n // REQUEST_INDEX_CHUNK_SIZE)  # ceil div
    print(f"Processing in {n_chunks} chunk(s) of up to {REQUEST_INDEX_CHUNK_SIZE:,} rows...")

    writer = None
    total_written, total_missing = 0, 0
    try:
        for i in range(n_chunks):
            chunk = request_index.iloc[i * REQUEST_INDEX_CHUNK_SIZE : (i + 1) * REQUEST_INDEX_CHUNK_SIZE]
            print(f"\n--- chunk {i + 1}/{n_chunks} ({len(chunk):,} rows) ---")
            result = process_chunk(chunk)

            table = pa.Table.from_pandas(result, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(NEW_ROWS_TMP_PARQUET, table.schema)
            else:
                table = table.cast(writer.schema)  # keep every chunk's types consistent
            writer.write_table(table)

            total_written += len(result)
            total_missing += result["hours_utc"].isna().sum()
    finally:
        if writer is not None:
            writer.close()

    if total_missing:
        print(f"\n{total_missing:,} / {total_written:,} newly-fetched (pixel, day) pairs got zero hourly values")

    if had_existing_output:
        print(f"\nMerging {total_written:,} newly-fetched rows into the existing {AORC_HOURLY_PARQUET}...")
        pf = pq.ParquetFile(NEW_ROWS_TMP_PARQUET)
        merge_writer = pq.ParquetWriter(MERGED_TMP_PARQUET, pf.schema_arrow)
        try:
            stream_copy(AORC_HOURLY_PARQUET, merge_writer)
            stream_copy(NEW_ROWS_TMP_PARQUET, merge_writer)
        finally:
            merge_writer.close()
        os.replace(MERGED_TMP_PARQUET, AORC_HOURLY_PARQUET)
        os.remove(NEW_ROWS_TMP_PARQUET)
    else:
        os.replace(NEW_ROWS_TMP_PARQUET, AORC_HOURLY_PARQUET)

    print(f"\nWrote {total_written:,} newly-fetched rows to {AORC_HOURLY_PARQUET}")


if __name__ == "__main__":
    main()
