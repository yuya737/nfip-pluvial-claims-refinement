"""Shared constants for indexing into the public NOAA AORC v1.1 1km grid.

Source: s3://noaa-nws-aorc-v1-1-1km (AWS Open Data, public, no-sign-request),
one Zarr group per year (e.g. "2020.zarr"), variable "APCP_surface" with
dims (time, latitude, longitude), int16 storage with scale_factor=0.1 and
fill_value=-32767 (i.e. value_mm = raw * 0.1; -32767 means missing).

Grid parameters below were read directly off the live store's coordinate
arrays (not taken from documentation), by decoding the first Zarr chunk of
`latitude` and `longitude`:
  latitude:  4201 points, 20.0 to 55.0 deg N, step 1/120 deg (30 arcsec)
  longitude: 8401 points, -130.0 to -60.0 deg E, step 1/120 deg (30 arcsec)
This comfortably covers CONUS (~24-49N, -125 to -66.9W).

Precipitation values are sampled by bilinear interpolation, not by
snapping to the nearest pixel (see latlon_to_bilinear_corners) — the
claim's location almost never lands exactly on a grid point, and
precipitation is a continuous field.
"""

AORC_LAT_START = 20.0
AORC_LON_START = -130.0
AORC_GRID_STEP_DEG = 1.0 / 120.0  # 30 arcsec
AORC_N_LAT = 4201
AORC_N_LON = 8401


def is_within_grid(lat: float, lon: float) -> bool:
    """Whether a WGS84 point falls inside the AORC CONUS grid's extent.

    NFIP claims include territories (Puerto Rico, Guam, etc.) that fall
    outside this grid entirely. Silently clamping those to an edge pixel
    would attribute the wrong location's precipitation to them, so callers
    must check this before calling latlon_to_bilinear_corners.
    """
    lat_span = (AORC_N_LAT - 1) * AORC_GRID_STEP_DEG
    lon_span = (AORC_N_LON - 1) * AORC_GRID_STEP_DEG
    return (
        AORC_LAT_START <= lat <= AORC_LAT_START + lat_span
        and AORC_LON_START <= lon <= AORC_LON_START + lon_span
    )


def pixel_to_latlon(row, col):
    """Center lat/lon of a grid pixel (inverse of latlon_to_bilinear_corners'
    row/col). Vectorized — row/col may be scalars or numpy arrays.

    Used to reconstruct a claim's representative (lat, lon) from its 4
    stored corner pixels + bilinear weights (see precip_threshold.py) when
    a downstream step needs real coordinates but only pixel indices were
    persisted — the weighted sum of the 4 corners' pixel_to_latlon values,
    using the same weights latlon_to_bilinear_corners produced, exactly
    reconstructs the original point (both are affine in row/col), not an
    approximation.
    """
    lat = AORC_LAT_START + row * AORC_GRID_STEP_DEG
    lon = AORC_LON_START + col * AORC_GRID_STEP_DEG
    return lat, lon


def latlon_to_bilinear_corners(lat: float, lon: float):
    """The 4 grid pixels surrounding a WGS84 point, with bilinear weights.

    Returns a list of 4 (row, col, weight) tuples; weights sum to 1.0.
    Precipitation is a continuous field and claim locations rarely land
    exactly on a grid point, so callers should combine these 4 pixels'
    values by weight rather than snapping to the single nearest one.

    Raises ValueError if the point is outside the grid's extent — check
    is_within_grid first if you need to handle that case rather than fail.
    """
    if not is_within_grid(lat, lon):
        raise ValueError(f"({lat}, {lon}) is outside the AORC grid extent")
    row_f = (lat - AORC_LAT_START) / AORC_GRID_STEP_DEG
    col_f = (lon - AORC_LON_START) / AORC_GRID_STEP_DEG
    # Clamp the lower corner to AORC_N_{LAT,LON} - 2 so the upper corner
    # (row0+1 / col0+1) is always a valid in-bounds index, even for a point
    # essentially exactly on the grid's outer edge.
    row0 = min(max(int(row_f), 0), AORC_N_LAT - 2)
    col0 = min(max(int(col_f), 0), AORC_N_LON - 2)
    frac_row = min(max(row_f - row0, 0.0), 1.0)
    frac_col = min(max(col_f - col0, 0.0), 1.0)
    return [
        (row0, col0, (1 - frac_row) * (1 - frac_col)),
        (row0, col0 + 1, (1 - frac_row) * frac_col),
        (row0 + 1, col0, frac_row * (1 - frac_col)),
        (row0 + 1, col0 + 1, frac_row * frac_col),
    ]
