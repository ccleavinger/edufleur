"""Conservative geometry tests for lower-48 GeoJSON in longitude/latitude.

60,000 metres per degree is below either axis scale throughout our supported
latitude range. Using that smaller scale intentionally excludes extra boundary
records instead of accepting an uncertainty circle that crosses a boundary.
This is a screening operation, not a general purpose GIS distance calculation.
"""
import math


def segment_distance(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0, min(1, ((p[0]-a[0])*dx + (p[1]-a[1])*dy)/(dx*dx+dy*dy))) if dx or dy else 0
    return math.hypot(p[0]-a[0]-t*dx, p[1]-a[1]-t*dy)


def in_ring(p, ring):
    inside = False
    for a, b in zip(ring, ring[1:] + ring[:1]):
        if (a[1] > p[1]) != (b[1] > p[1]) and p[0] < (b[0]-a[0])*(p[1]-a[1])/(b[1]-a[1])+a[0]:
            inside = not inside
    return inside


def contains_uncertainty(geometry, lon, lat, uncertainty):
    if not all(math.isfinite(x) for x in [lon, lat, uncertainty]):
        return False
    if not (-126 <= lon <= -66 and 24 <= lat <= 50 and 0 <= uncertainty <= 1000):
        return False
    polygons = [geometry['coordinates']] if geometry['type'] == 'Polygon' else geometry['coordinates']
    for polygon in polygons:
        if not polygon or not in_ring((lon, lat), polygon[0]) or any(in_ring((lon, lat), r) for r in polygon[1:]):
            continue
        distance = min(segment_distance((lon, lat), a, b) for ring in polygon for a, b in zip(ring, ring[1:] + ring[:1]))
        if distance * 60000 > max(uncertainty, 1):
            return True
    return False


def bounds(geometry):
    polygons = [geometry['coordinates']] if geometry['type'] == 'Polygon' else geometry['coordinates']
    min_lon, min_lat, max_lon, max_lat = 180, 90, -180, -90
    for polygon in polygons:
        for ring in polygon:
            for lon, lat, *_ in ring:
                min_lon, min_lat = min(min_lon, lon), min(min_lat, lat)
                max_lon, max_lat = max(max_lon, lon), max(max_lat, lat)
    return min_lon, min_lat, max_lon, max_lat
