"""Inflation-adjust claim dollar fields using the FRED PCE price index.

Every dollar-denominated claim field is converted to a fixed target year's
dollars by matching each claim to the CPI-style price index for its loss
quarter and rescaling to the target year's index value.

Target year defaults to 2021. Pass --target-year to adjust to a
different year for other uses.
"""

import argparse
import glob

import numpy as np
import pandas as pd

from paths import RAW_CLAIMS_PARQUET, FRED_INFLATION_GLOB, INFLATION_ADJUSTED_PARQUET

DOLLAR_FIELDS = [
    "amountPaidOnBuildingClaim",
    "amountPaidOnContentsClaim",
    "amountPaidOnIncreasedCostOfComplianceClaim",
    "totalBuildingInsuranceCoverage",
    "totalContentsInsuranceCoverage",
    "buildingDamageAmount",
    "netBuildingPaymentAmount",
    "buildingPropertyValue",
    "contentsDamageAmount",
    "netContentsPaymentAmount",
    "contentsPropertyValue",
    "iccCoverage",
    "netIccPaymentAmount",
    "buildingReplacementCost",
    "contentsReplacementCost",
]

# Fields where a missing value means "$0", not "unknown" — safe to fill.
FILL_ZERO_FIELDS = [
    "amountPaidOnBuildingClaim",
    "amountPaidOnContentsClaim",
    "amountPaidOnIncreasedCostOfComplianceClaim",
    "buildingDamageAmount",
    "netBuildingPaymentAmount",
    "contentsDamageAmount",
    "netContentsPaymentAmount",
    "netIccPaymentAmount",
    "buildingReplacementCost",
    "contentsReplacementCost",
]


def adjust_to_target_year(claims_df: pd.DataFrame, target_year: int) -> pd.DataFrame:
    fred_files = sorted(glob.glob(FRED_INFLATION_GLOB))
    if not fred_files:
        raise FileNotFoundError(
            f"No FRED inflation CSV found matching {FRED_INFLATION_GLOB}. "
            "Run download_claims.py first."
        )
    inflation_df = pd.read_csv(fred_files[-1])
    inflation_df["observation_date"] = pd.to_datetime(inflation_df["observation_date"])

    target_row = inflation_df[inflation_df["observation_date"].dt.year == target_year]
    if target_row.empty:
        raise ValueError(
            f"Target year {target_year} not present in FRED series "
            f"(available: {inflation_df['observation_date'].dt.year.min()}-"
            f"{inflation_df['observation_date'].dt.year.max()})"
        )
    # Use the first (Q1) observation of the target year as the reference index,
    # matching the convention used elsewhere in this pipeline.
    target_index = target_row.iloc[0]["DPCERD3Q086SBEA"]
    print(f"Adjusting all dollar fields to {target_year} dollars (index={target_index})")

    claims_df = claims_df.copy()
    claims_df["dateOfLoss"] = pd.to_datetime(claims_df["dateOfLoss"], errors="coerce")
    claims_df["quarter_start"] = claims_df["dateOfLoss"].dt.to_period("Q").dt.start_time

    claims_df = claims_df.merge(
        inflation_df.rename(
            columns={"observation_date": "quarter_start", "DPCERD3Q086SBEA": "price_index"}
        ),
        on="quarter_start",
        how="left",
    )

    for field in DOLLAR_FIELDS:
        real_field = f"{field}Real{target_year}"
        claims_df[real_field] = pd.to_numeric(claims_df[field], errors="coerce").astype(
            float
        ) * (target_index / claims_df["price_index"])

    fill_fields = FILL_ZERO_FIELDS + [f"{f}Real{target_year}" for f in FILL_ZERO_FIELDS]
    claims_df[fill_fields] = claims_df[fill_fields].fillna(0)

    round_fields = DOLLAR_FIELDS + [f"{f}Real{target_year}" for f in DOLLAR_FIELDS]
    claims_df[round_fields] = claims_df[round_fields].round(2)

    claims_df["amountPaid"] = (
        claims_df["amountPaidOnBuildingClaim"]
        + claims_df["amountPaidOnContentsClaim"]
        + claims_df["amountPaidOnIncreasedCostOfComplianceClaim"]
    )
    claims_df[f"amountPaidReal{target_year}"] = (
        claims_df[f"amountPaidOnBuildingClaimReal{target_year}"]
        + claims_df[f"amountPaidOnContentsClaimReal{target_year}"]
        + claims_df[f"amountPaidOnIncreasedCostOfComplianceClaimReal{target_year}"]
    )
    claims_df["damageAmount"] = (
        claims_df["buildingDamageAmount"] + claims_df["contentsDamageAmount"]
    )
    claims_df[f"damageAmountReal{target_year}"] = (
        claims_df[f"buildingDamageAmountReal{target_year}"]
        + claims_df[f"contentsDamageAmountReal{target_year}"]
    )

    return claims_df.drop(columns=["quarter_start", "price_index"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target-year",
        type=int,
        default=2021,
        help="Dollar-year to adjust all monetary fields to (default: 2021)",
    )
    args = parser.parse_args()

    print(f"Loading raw claims from {RAW_CLAIMS_PARQUET}...")
    claims_df = pd.read_parquet(RAW_CLAIMS_PARQUET)
    print(f"Loaded {len(claims_df):,} claims")

    adjusted_df = adjust_to_target_year(claims_df, args.target_year)
    adjusted_df.to_parquet(INFLATION_ADJUSTED_PARQUET, index=False)
    print(f"Wrote {len(adjusted_df):,} rows to {INFLATION_ADJUSTED_PARQUET}")


if __name__ == "__main__":
    main()
