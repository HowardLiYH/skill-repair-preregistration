---
name: geospatial-analysis
description: Analyze geospatial data using geopandas with proper coordinate projections. Use when calculating distances between geographic features, performing spatial filtering, or working with plate boundaries and earthquake data.
license: MIT
---

# Geospatial Analysis with GeoPandas

## When to use
Distance calculations, spatial filtering (points in polygons), or plate boundary/earthquake analysis with geopandas.

## Inputs and outputs
- Inputs: GeoJSON files (`gpd.read_file`) or coordinate records (lat/lon).
- Outputs: filtered GeoDataFrames, distance columns in meters/km, extreme-value rows.

## Core workflow
1. Load data: `gpd.read_file("plates.json")`; build GeoDataFrame from coords with `Point(lon, lat)` and `crs="EPSG:4326"`.
2. Filter by attributes first (e.g. `Code == "PA"`, `str.contains("PA")`).
3. Spatial filter: `gdf_points[gdf_points.within(poly.unary_union)]`.
4. Project to metric CRS `EPSG:4087` via `.to_crs()`.
5. Combine geometries with `.unary_union`, then one `.distance()` call.
6. Convert: `distance_km = distance_m / 1000.0`; extremes via `.nlargest(1, col)` / `.nsmallest()`.

## Tools and APIs
`geopandas.read_file`, `GeoDataFrame.to_crs`, `shapely.geometry.Point`, `.within()`, `.unary_union`, `.distance()`, `.nlargest()`, `.nsmallest()`, `.copy()`.

## Validation checks
- CRS is metric before any distance call.
- Distances are meters, then divided by 1000 for km.
- Geometries present: `gdf[gdf.geometry.notna()]`.
- Filtered frames copied before mutation.
- Extreme selection matches the task: nearest uses `.nsmallest(1, col)`, furthest uses `.nlargest(1, col)`.
- Rounding and output fields match the task spec (e.g. `distance_km` rounded to 2 decimals).

## Pitfalls and constraints
- Never compute distances in EPSG:4326 (returns degrees).
- Do not manually adjust longitude for the antimeridian; geopandas handles it.
- Do not iterate boundary points; union then single distance.
- Avoid manual Haversine or point-in-polygon code.
- Filter before projecting; project once, not in loops.
- "Nearest to any boundary" means distance to the union of all boundaries, not one plate's boundary.

```python
# ❌ INCORRECT: Calculating distance in EPSG:4326
# This treats degrees as if they were equal distances everywhere on Earth
gdf = gpd.GeoDataFrame(..., crs="EPSG:4326")
distance = point1.distance(point2)  # Wrong! Returns degrees, not meters

# ✅ CORRECT: Project to metric CRS first
gdf_projected = gdf.to_crs("EPSG:4087")
distance_meters = point1_proj.distance(point2_proj)  # Correct! Returns meters
distance_km = distance_meters / 1000.0
```

## Compact example
```python
METRIC_CRS = "EPSG:4087"
plate = gdf_plates[gdf_plates["Code"] == "PA"].geometry.unary_union
eq = gdf_eq[gdf_eq.within(plate)].copy()
bounds = gdf_boundaries[gdf_boundaries["Name"].str.contains("PA")].to_crs(METRIC_CRS).geometry.unary_union
eq["distance_km"] = eq.to_crs(METRIC_CRS).geometry.distance(bounds) / 1000.0
furthest = eq.nlargest(1, "distance_km").iloc[0]
nearest = eq.nsmallest(1, "distance_km").iloc[0]
```
