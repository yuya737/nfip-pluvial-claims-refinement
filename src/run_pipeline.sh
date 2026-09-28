#!/usr/bin/env bash
# Runs the full pipeline end to end, in dependency order. Each step reads
# the previous step's output via paths.py / config.yaml, so steps can also
# be re-run individually once their inputs exist.
#
# Optional overrides, forwarded to the relevant step (all default to that
# step's own default -- see triangulate_claims.py / correct_pluvial_dates.py
# --help for what each one does):
#   --min-hourly-precip-mm MM     correct_pluvial_dates.py's threshold
#   --block-group-strategy NAME   triangulate_claims.py's block-group vintage strategy
#   --zcta-strategy NAME          triangulate_claims.py's ZCTA vintage strategy
set -euo pipefail
cd "$(dirname "$0")"

PRECIP_MM=""
BG_STRATEGY=""
ZCTA_STRATEGY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --min-hourly-precip-mm) PRECIP_MM="$2"; shift 2 ;;
    --block-group-strategy) BG_STRATEGY="$2"; shift 2 ;;
    --zcta-strategy)        ZCTA_STRATEGY="$2"; shift 2 ;;
    *) echo "run_pipeline.sh: unknown flag: $1" >&2; exit 1 ;;
  esac
done

echo "== 1/6: download raw claims + inflation series =="
python download_claims.py

echo "== 2/6: adjust claim dollar fields for inflation =="
python adjust_inflation.py

echo "== 3/6: download Census block-group/ZCTA boundary shapefiles (skip-if-exists) =="
python download_shapefiles.py

echo "== 4/6: triangulate claim geometries (pluvial claims only, causeOfDamage=4) =="
python triangulate_claims.py --cause-of-damage 4 \
  ${BG_STRATEGY:+--block-group-strategy "$BG_STRATEGY"} \
  ${ZCTA_STRATEGY:+--zcta-strategy "$ZCTA_STRATEGY"}

echo "== 5/6: build AORC bilinear-corner request index (pluvial claims only) =="
python build_aorc_pixel_day_index.py

echo "== 5/6b: fetch AORC hourly precip for requested pixel/days =="
python fetch_aorc_daily_max.py

echo "== 6/6: correct pluvial claim dates against AORC (bilinear interpolation + daily max) =="
python correct_pluvial_dates.py ${PRECIP_MM:+--min-hourly-precip-mm "$PRECIP_MM"}

echo "Done. Current output: ../data/interim/claims_pluvial_corrected.parquet"
