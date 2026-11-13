---
name: geospatial-analysis
description: Analyze geospatial data using geopandas with proper coordinate projections. Use when calculating distances between geographic features, performing spatial filtering, or working with plate boundaries and earthquake data.
license: MIT
---
# Geospatial Analysis with GeoPandas

## Key Concepts

### Geographic vs Projected Coordinate Systems

| Coordinate System | Type | Units | Use Case |
|-------------------|------|-------|----------|
| EPSG:4326 (WGS84) | Geographic | Degrees (lat/lon) | Data storage, display |
| EPSG:4087 (World Equidistant Cylindrical) | Projected | Meters | Distance calculations |

**Critical Rule**: Never calculate distances directly in geographic coordinates (EPSG:4326). Always project to a metric coordinate system first.

### Why Projection Matters

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

## Distance Calculations

### Point to Line/Boundary Distance

```python
# 1. Load your data
gdf_points = gpd.read_file("points.json")
gdf_boundaries = gpd.read_file("boundaries.json")

# 2. Project to metric coordinate system
METRIC_CRS = "EPSG:4087"
points_proj = gdf_points.to_crs(METRIC_CRS)
boundaries_proj = gdf_boundaries.to_crs(METRIC_CRS)

# 3. Combine boundary segments if needed
boundary_geom = boundaries_proj.geometry.unary_union

# 4. Calculate distances (returns meters)
gdf_points["distance_m"] = points_proj.geometry.distance(boundary_geom)
gdf_points["distance_km"] = gdf_points["distance_m"] / 1000.0
```

## Common Workflow Pattern

Here's a complete example for analyzing earthquakes near plate boundaries:

```python
import geopandas as gpd
from shapely.geometry import Point

# 1. Load data
earthquakes_data = [...]  # Your earthquake data
gdf_plates = gpd.read_file("plates.json")
gdf_boundaries = gpd.read_file("boundaries.json")

# 2. Create earthquake GeoDataFrame
geometry = [Point(eq["longitude"], eq["latitude"]) for eq in earthquakes_data]
gdf_eq = gpd.GeoDataFrame(earthquakes_data, geometry=geometry, crs="EPSG:4326")

# 3. Spatial filtering - find earthquakes in specific plate
target_plate = gdf_plates[gdf_plates["Code"] == "PA"].geometry.unary_union
earthquakes_in_plate = gdf_eq[gdf_eq.within(target_plate)].copy()

# 4. Calculate distances (project to metric CRS)
METRIC_CRS = "EPSG:4087"
eq_proj = earthquakes_in_plate.to_crs(METRIC_CRS)

# Filter and combine relevant boundaries
plate_boundaries = gdf_boundaries[
    gdf_boundaries["Name"].str.contains("PA")
].to_crs(METRIC_CRS).geometry.unary_union

# Calculate distances
earthquakes_in_plate["distance_km"] = eq_proj.geometry.distance(plate_boundaries) / 1000.0

# 5. Find the furthest earthquake
furthest_eq = earthquakes_in_plate.nlargest(1, "distance_km").iloc[0]
```

## Best Practices Summary

1. ✅ Load GeoJSON with `gpd.read_file()`
2. ✅ Use `.within()` for spatial filtering
3. ✅ Project to metric CRS (`EPSG:4087`) before distance calculations
4. ✅ Combine geometries with `.unary_union` before distance calculation
5. ✅ Use `.distance()` method for point-to-geometry distances
6. ✅ Use `.nlargest()` / `.nsmallest()` for finding extreme values
7. ❌ Never calculate distances in EPSG:4326
8. ❌ Avoid manual Haversine implementations
9. ❌ Don't iterate through individual boundary points
