---
name: geospatial-analysis
description: Analyze geospatial data using geopandas with proper coordinate projections. Use when calculating distances between geographic features, performing spatial filtering, or working with plate boundaries and earthquake data.
license: MIT
---

 Geospatial Analysis GeoPandas 
 
 Overview 
 
 working with geographic data earthquakes plate boundaries geopandas proper coordinate projections provides accurate distance calculations efficient spatial operations guide covers best practices geospatial analysis 
 
 Key Concepts 
 
 Geographic vs Projected Coordinate Systems 
 
 Type Units Case 
 
 EPSG : 4326 ( WGS84 ) Geographic Degrees Data storage 
 EPSG : 4087 World Equidistant Cylindrical ) Projected Meters Distance calculations 
 
 calculate distances in geographic coordinates EPSG : 4326 ) project metric coordinate system first 
 
 
 
 
 Calculating distance in EPSG : 4326 
 equal distances Earth 
 
 distance point1. distance ( point2 
 
 Project metric CRS 
 EPSG : 4087 
 distance meters = point1 _ proj. distance point2 
 / 1000. 
 
 
 Geospatial Data 
 
 GeoJSON 
 
 
 
 
 
 
f _ boundaries = gpd. read _ file ( boundaries. json 
 
 
 Regular Data with Coordinates 
 
 python 
 shapely geometry import Point 
 geopandas gpd 
 
 Convert coordinate data to GeoDataFrame 
 = 
 id 1 lat 35. 140 5. 5 
 36 141 6. 0 
 
 
 geometry = Point ( row lon lat row data 
 gdf = gpd. GeoDataFrame EPSG : 4326 
 
 
 Spatial Filtering 
 
 Finding Points Polygon 
 
 
 
 target _ poly = gdf _ plates 
 
 Filter points within polygon 
 points inside = gdf _ points 
 
 
 
 
 Multiple Geometries 
 
 polygons one 
 
 
 Combine boundary segments into one geometry 
 all boundaries = gdf _ boundaries geometry 
 
 
 pacific boundaries = gdf _ boundaries 
 
 
 
 
 Distance Calculations 
 
 Point to Line Boundary Distance 
 
 
Load data 
 gdf points = gpd. read _ file. json 
 
 
 Project metric coordinate system 
 METRIC _ CRS = " EPSG : 4087 " 
 points proj = gdf _ points. to crs 
 
 
 Combine boundary segments 
 geom = boundaries proj geometry unary union 
 
 Calculate distances returns meters ) 
 gdf _ points distance _ m points proj geometry distance boundary geom 
 gdf km gdf 1000. 
 
 
 Finding Furthest Point 
 
 
 Sort distance furthest point 
 = gdf _ points. nlargest distance _ km 
 
 
 
 
 
 
 
 example analyzing earthquakes near plate boundaries 
 
 
 
 
 
 Load data 
 earthquakes 
 gdf _ plates = gpd. read _ file plates. json 
 gpd read _ file boundaries. json 
 
 Create earthquake GeoDataFrame 
 geometry = Point ( eq longitude latitude earthquakes _ data 
eq = gpd. GeoDataFrame earthquakes data geometry EPSG : 4326 " 
 
 Spatial filtering find earthquakes specific plate 
 target plate = gdf _ plates PA geometry union 
 earthquakes in plate = gdf _ eq 
 
 Calculate distances project to metric CRS ) 
 CRS = EPSG : 4087 " 
 proj = earthquakes _ in _ plate. to _ crs 
 
 Filter boundaries 
 = gdf _ boundaries 
 PA 
 geometry 
 
 Calculate distances 
 earthquakes in plate distance _ km = geometry distance / 1000. 0 
 
 Find furthest earthquake 
 eq = earthquakes _ in _ plate. nlargest distance km 
 
 
 Filtering Attributes 
 
 
 
 pacific _ plate = gdf _ plates 
 
 
 Filter boundaries specific plate 
 pacific _ bounds = gdf _ boundaries 
 PA " 
 PA 
 
 
 String pattern matching 
 pa _ related = gdf _ boundariesName " str. contains " PA " 
 
 
 Performance Tips 
 
 Filter before projecting Reduce data size before expensive operations 
 Project once Convert to metric CRS not in loops 
 Use unary _ union Combine geometries before distance calculations 
 Copy modifying Use copy creating filtered DataFrames 
 
 python 
 
 subset = gdf large Pacific 
. to crs ( METRIC _ CRS 
 
 Projecting large dataset filter 
 
 
 
 
 Common Pitfalls 
 
 Issue Solution 
 
 Distance in degrees Using EPSG : 4326 for distance calculations Project 4087 metric CRS 
 Antimeridian issues longitude adjustments ±360 Use geopandas spatial operations 
 performance Calculating distance to each boundary point unary _ union distance 
 Missing geometries no geometry Filter with geometry notna 
 
 Manual Calculations 
 
 implementing 
 - Haversine distance formulas geopandas projections 
 Point - in - polygon checks 
boundary points distance unary union 
 
 approaches slower error - prone less accurate. 
 
 Practices 
 
 GeoJSON read file 
 spatial filtering 
 metric CRS 4087 
 Combine geometries unary union 
 distance point - geometry distances 
 nlargest nsmallest extreme values 
 distances EPSG 4326 
 Avoid Haversine implementations 
 boundary points 
