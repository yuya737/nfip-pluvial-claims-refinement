"""Download the Census block-group/ZCTA boundary shapefiles into shapefile_root.
"""

import argparse
import time
from pathlib import Path

import requests

from conus import CONUS_STATE_FIPS
from paths import SHAPEFILE_ROOT

BASE = "https://www2.census.gov/geo/tiger"

# {vintage_year: per-state URL template} -- "{state}" is the 2-digit FIPS code.
# 2010 is cb_2019 (Census's final republish of the 2010-vintage geography,
# genuinely newer than the original gz_2010 release and verified to cost
# zero spatial-validation accuracy vs. it) -- see docs/methods.md and
# config.yaml's block_group_vintages comment.
BLOCK_GROUP_URL_TEMPLATES = {
    1990: f"{BASE}/PREVGENZ/bg/bg90shp/bg{{state}}_d90_shp.zip",
    2000: f"{BASE}/PREVGENZ/bg/bg00shp/bg{{state}}_d00_shp.zip",
    2010: f"{BASE}/GENZ2019/shp/cb_2019_{{state}}_bg_500k.zip",
    2020: f"{BASE}/GENZ2020/shp/cb_2020_{{state}}_bg_500k.zip",
}

# {vintage_year: nationwide URL} -- ZCTAs ship as one file for the whole US.
ZCTA_URLS = {
    2000: f"{BASE}/TIGER2009/tl_2009_us_zcta500.zip",
    2010: f"{BASE}/GENZ2019/shp/cb_2019_us_zcta510_500k.zip",
    2020: f"{BASE}/GENZ2020/shp/cb_2020_us_zcta520_500k.zip",
}


MAX_RETRIES = 6
REQUEST_DELAY_SECONDS = 1.0


def _download(url: str, dest_path):
    if dest_path.exists():
        print(f"    already have {dest_path.name}")
        return

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"    downloading {dest_path.name}" + (f" (retry {attempt - 1})" if attempt > 1 else ""))
        response = requests.get(url, timeout=300)
        if response.status_code == 429:
            # Census doesn't send Retry-After on this endpoint in practice, so
            # back off exponentially (2, 4, 8, 16, 32, 64s) rather than guess a
            # single fixed delay -- confirmed by hitting a real 429 mid-run at
            # a flat 0.2s/request, so "some delay" alone isn't sufficient.
            wait = int(response.headers.get("Retry-After", 2**attempt))
            print(f"      429 rate-limited, waiting {wait}s before retry...")
            time.sleep(wait)
            continue
        response.raise_for_status()
        dest_path.write_bytes(response.content)
        time.sleep(REQUEST_DELAY_SECONDS)
        return

    raise RuntimeError(f"{url}: still rate-limited after {MAX_RETRIES} retries")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        default=str(SHAPEFILE_ROOT),
        help="Where to write the downloaded files (default: config.yaml's shapefile_root).",
    )
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Writing to {output_dir}\n")

    print("Block-group vintages:")
    for vintage, template in BLOCK_GROUP_URL_TEMPLATES.items():
        print(f"  vintage {vintage}:")
        for state in sorted(CONUS_STATE_FIPS):
            url = template.format(state=state)
            dest = output_dir / url.rsplit("/", 1)[-1]
            _download(url, dest)

    print("\nZCTA vintages:")
    for vintage, url in ZCTA_URLS.items():
        print(f"  vintage {vintage}:")
        dest = output_dir / url.rsplit("/", 1)[-1]
        _download(url, dest)

    print("\nDone.")


if __name__ == "__main__":
    main()
