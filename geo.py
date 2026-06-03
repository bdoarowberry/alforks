"""Shared geometry primitives.

Dependency-free (stdlib only) so the Flask app and the route builder can
share one implementation. `point_in_polygon` previously lived as
byte-identical copies in both app.py and route_builder.py.

(Haversine is deliberately NOT centralised here: detection.py and
trail_match.py each carry their own, and although mathematically equal
they round differently at the ULP level — detection does
`radians(b) - radians(a)`, trail_match does `radians(b - a)`. Unifying
would perturb their version-cached GPS outputs for no real benefit.)
"""
from __future__ import annotations


def point_in_polygon(lat: float, lon: float, ring) -> bool:
    """Ray-casting point-in-polygon test. `ring` is [[lat, lon], ...] pairs."""
    n = len(ring)
    inside = False
    j = n - 1
    for i in range(n):
        yi, xi = ring[i]
        yj, xj = ring[j]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside
