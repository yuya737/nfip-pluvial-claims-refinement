"""CONUS-only scope.

This pipeline covers the 48 contiguous states + DC only. NFIP claims exist
for Alaska, Hawaii, Puerto Rico, and other territories too, but they're
out of scope here: the AORC pluvial-correction step is CONUS-only by
construction (s3://noaa-nws-aorc-v1-1-1km's grid doesn't extend to them),
and the block-group/ZCTA shapefiles staged in config.yaml's
shapefile_root only cover CONUS. Rather than let non-CONUS claims fall out
silently later (e.g. via missing shapefile lookups), they're filtered out
explicitly and up front, in triangulate_claims.py, so every downstream
step only ever sees claims within scope.
"""

# 2-digit state FIPS codes for the 48 contiguous states + DC.
CONUS_STATE_FIPS = {
    "01", "04", "05", "06", "08", "09", "10", "11", "12", "13",
    "16", "17", "18", "19", "20", "21", "22", "23", "24", "25",
    "26", "27", "28", "29", "30", "31", "32", "33", "34", "35",
    "36", "37", "38", "39", "40", "41", "42", "44", "45", "46",
    "47", "48", "49", "50", "51", "53", "54", "55", "56",
}


def is_conus_block_group_fips(fips: str) -> bool:
    return isinstance(fips, str) and fips[:2] in CONUS_STATE_FIPS
