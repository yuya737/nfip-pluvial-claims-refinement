"""Spatial threshold surface for "this day had meaningful precipitation".

correct_pluvial_dates.py needs a minimum-precipitation threshold to decide
whether a day counts as a plausible flood day. There's no reason that
threshold has to be spatially uniform — a desert claim and a rainforest
claim probably shouldn't need the same number of mm to count as "a lot of
rain" — so this is a real, swappable abstraction rather than a bare
constant. Two implementations exist:

  UniformThresholdGrid  the same constant everywhere (the default, and
                         what this pipeline used before this module existed)
  NetCDFThresholdGrid    sampled from a user-supplied NetCDF file, nearest-
                         neighbor, at whatever resolution the file has —
                         doesn't need to match the AORC grid

Both expose the same interface, so correct_pluvial_dates.py doesn't need
to know or care which one is in play:

  grid(lat, lon) -> float                     one point
  grid.resolve_for_claims(lats, lons) -> array  many points, vectorized

resolve_for_claims is the one real pipeline runs use — evaluating
thousands of individual Python-level grid(lat, lon) calls is exactly the
per-row overhead this is designed to avoid. UniformThresholdGrid's
version is trivial (broadcast the constant); NetCDFThresholdGrid's uses
xarray's vectorized ("pointwise") indexing so the whole claim set is one
batched nearest-neighbor lookup, not one per claim.
"""

import numpy as np


class UniformThresholdGrid:
    """A threshold grid that returns the same constant value everywhere."""

    def __init__(self, value_mm: float):
        self.value_mm = value_mm

    def __call__(self, lat: float = None, lon: float = None) -> float:
        return self.value_mm

    def resolve_for_claims(self, lats, lons) -> np.ndarray:
        return np.full(len(lats), self.value_mm, dtype=float)

    def __repr__(self):
        return f"UniformThresholdGrid({self.value_mm} mm)"


class NetCDFThresholdGrid:
    """A threshold grid sampled from a user-supplied NetCDF file.

    Auto-detects the lat/lon coordinate names (tries the common
    lat/latitude/y and lon/longitude/x spellings) and, if the file has
    exactly one data variable, which one to read — pass `var` explicitly
    if it has more than one. Sampling is nearest-neighbor: the file isn't
    assumed to be on the AORC grid or any particular resolution, so
    interpolation would require assumptions about the source grid's
    structure this class has no way to verify.
    """

    def __init__(self, path: str, var: str = None):
        import xarray as xr

        self.path = path
        ds = xr.open_dataset(path)

        lat_candidates = ["lat", "latitude", "y"]
        lon_candidates = ["lon", "longitude", "x"]
        self.lat_name = next((c for c in lat_candidates if c in ds.coords), None)
        self.lon_name = next((c for c in lon_candidates if c in ds.coords), None)
        if self.lat_name is None or self.lon_name is None:
            raise ValueError(
                f"{path}: couldn't auto-detect lat/lon coordinates among {list(ds.coords)} "
                f"(tried {lat_candidates} / {lon_candidates})"
            )

        if var is None:
            data_vars = list(ds.data_vars)
            if len(data_vars) != 1:
                raise ValueError(
                    f"{path} has {len(data_vars)} data variables ({data_vars}); "
                    "pass --threshold-netcdf-var to pick one"
                )
            var = data_vars[0]
        if var not in ds.data_vars:
            raise KeyError(f"{path} has no variable {var!r}; available: {list(ds.data_vars)}")
        self.var = var
        self.da = ds[var]

    def __call__(self, lat: float, lon: float) -> float:
        value = self.da.sel(**{self.lat_name: lat, self.lon_name: lon}, method="nearest").item()
        return float(value)

    def resolve_for_claims(self, lats, lons) -> np.ndarray:
        import xarray as xr

        points = {
            self.lat_name: xr.DataArray(np.asarray(lats), dims="points"),
            self.lon_name: xr.DataArray(np.asarray(lons), dims="points"),
        }
        return self.da.sel(**points, method="nearest").values.astype(float)

    def __repr__(self):
        return f"NetCDFThresholdGrid({self.path}, var={self.var!r})"


def default_threshold_grid() -> UniformThresholdGrid:
    """The grid config.yaml's pluvial_correction.min_hourly_precip_mm describes."""
    from paths import PLUVIAL_MIN_HOURLY_PRECIP_MM

    return UniformThresholdGrid(PLUVIAL_MIN_HOURLY_PRECIP_MM)
