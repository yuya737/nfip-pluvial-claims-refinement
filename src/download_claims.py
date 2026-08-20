"""Download the raw FEMA OpenFEMA claims extract and the FRED inflation series.

Sources (both public, no authentication required):
  - FEMA OpenFEMA "FIMA NFIP Redacted Claims v2":
    https://www.fema.gov/about/reports-and-data/openfema/FimaNfipClaims.parquet
  - FRED personal consumption expenditures price index (DPCERD3Q086SBEA),
    used downstream to inflation-adjust dollar fields:
    https://fred.stlouisfed.org/series/DPCERD3Q086SBEA

Both files are timestamped/versioned by FEMA and FRED respectively, not by
this script, so re-running it later will pick up whatever the source has
published since. Record the download date if you need exact reproducibility
of a specific pull.
"""

from datetime import date

import requests

from paths import RAW_DIR, RAW_CLAIMS_PARQUET

FEMA_CLAIMS_URL = "https://www.fema.gov/about/reports-and-data/openfema/FimaNfipClaims.parquet"
FRED_SERIES_ID = "DPCERD3Q086SBEA"
FRED_URL_TEMPLATE = (
    "https://fred.stlouisfed.org/graph/fredgraph.csv"
    "?id={series_id}&cosd=1947-01-01&coed={today}"
)


def _download(url: str, dest_path):
    if dest_path.exists():
        print(f"Already downloaded: {dest_path}")
        return
    print(f"Downloading {url} -> {dest_path}")
    response = requests.get(url, timeout=300)
    response.raise_for_status()
    dest_path.write_bytes(response.content)


def main():
    _download(FEMA_CLAIMS_URL, RAW_CLAIMS_PARQUET)

    today = date.today().strftime("%Y-%m-%d")
    fred_url = FRED_URL_TEMPLATE.format(series_id=FRED_SERIES_ID, today=today)
    fred_dest = RAW_DIR / f"FRED_Inflation_{FRED_SERIES_ID}_{today}.csv"
    _download(fred_url, fred_dest)


if __name__ == "__main__":
    main()
