"""Canonical continuous sampling for the TDOC terrain-height contract."""
from __future__ import annotations

import math
from typing import Any


def sample_bilinear(dataset: Any, east_m: float, north_m: float) -> float:
    """Sample band 1 at a projected position using pixel-centre bilinear interpolation.

    ``rasterio.DatasetReader.sample`` is nearest-cell sampling.  Using it as a
    continuous AGL surface creates an artificial elevation step at every raster
    boundary.  This sampler interpolates the four pixel-centre elevations and
    fails closed if any contributing cell is nodata or non-finite.
    """
    import numpy as np
    from rasterio.windows import Window

    east = float(east_m)
    north = float(north_m)
    if not (math.isfinite(east) and math.isfinite(north)):
        raise ValueError("terrain_position_non_finite")
    if not (
        dataset.bounds.left <= east <= dataset.bounds.right
        and dataset.bounds.bottom <= north <= dataset.bounds.top
    ):
        raise ValueError("position_outside_dem_coverage")

    col_corner, row_corner = (~dataset.transform) * (east, north)
    # Raster values live at pixel centres, not at the upper-left pixel corners.
    col = min(max(float(col_corner) - 0.5, 0.0), float(dataset.width - 1))
    row = min(max(float(row_corner) - 0.5, 0.0), float(dataset.height - 1))
    c0 = int(math.floor(col))
    r0 = int(math.floor(row))
    c1 = min(c0 + 1, dataset.width - 1)
    r1 = min(r0 + 1, dataset.height - 1)
    dc = col - c0
    dr = row - r0
    values = dataset.read(
        1,
        window=Window(c0, r0, c1 - c0 + 1, r1 - r0 + 1),
        masked=True,
    )
    samples = {
        (r0, c0): values[0, 0],
        (r0, c1): values[0, c1 - c0],
        (r1, c0): values[r1 - r0, 0],
        (r1, c1): values[r1 - r0, c1 - c0],
    }
    weighted = (
        ((r0, c0), (1.0 - dr) * (1.0 - dc)),
        ((r0, c1), (1.0 - dr) * dc),
        ((r1, c0), dr * (1.0 - dc)),
        ((r1, c1), dr * dc),
    )
    terrain = 0.0
    for key, weight in weighted:
        if weight <= 1e-15:
            continue
        value = samples[key]
        if bool(np.ma.is_masked(value)):
            raise ValueError("dem_nodata_or_non_finite")
        scalar = float(value)
        if not math.isfinite(scalar) or (
            dataset.nodata is not None and math.isclose(scalar, float(dataset.nodata))
        ):
            raise ValueError("dem_nodata_or_non_finite")
        terrain += weight * scalar
    if not math.isfinite(terrain):
        raise ValueError("dem_nodata_or_non_finite")
    return float(terrain)
