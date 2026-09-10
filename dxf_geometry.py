"""Shared DXF geometry stitching.

Onshape (and other CAD) DXF exports represent a face boundary as many individual
LINE / ARC / ELLIPSE / SPLINE / open-POLYLINE segments rather than one closed
polyline. Turning those back into closed boundary paths is needed in two places:

  1. 2D import  - frc_cam_postprocessor.load_dxf (machines the paths directly)
  2. 2.5D build - onshape_integration._convert_geometry_to_solid_hatch (rebuilds a
                  solid-HATCH negative-space DXF from per-depth Onshape exports)

Both call entities_to_closed_paths() so the sampling and stitching live in ONE place
(a missing entity type here previously had to be fixed twice - e.g. ELLIPSE).
"""

import math

from shapely.geometry import LineString, Polygon
from shapely.ops import linemerge
from shapely.validation import make_valid


# Max deviation (sagitta) allowed when flattening a curve to line segments, in drawing
# units (inches). All curve samplers below share it so arcs, ellipses, and splines are
# tessellated to the SAME fidelity. Well under machining tolerance, so the linearized
# cut is visually and dimensionally indistinguishable from the true curve.
CHORD_TOLERANCE = 0.001

# Decimals to quantize a raw CAD coordinate to before snapping it to the grid.
# CAD exports carry float-representation noise: the SAME shared vertex arrives as
# 9.6875 from an ARC and 9.687499999999998 from the LINE that meets it. Grid snapping
# alone does not fuse them - a value sitting exactly on a grid half-step rounds UP from
# one entity and DOWN from the other, splitting a coincident junction into two points a
# full grid step apart. Quantizing first collapses the noise so both snap identically.
# 1e-9" is ~5 orders of magnitude below the snap grid and far below any real geometry.
COORD_DECIMALS = 9


def sample_arc(arc, distance=CHORD_TOLERANCE):
    """Sample an ARC entity into a list of (x, y) points.

    Uses ezdxf's chord-tolerance flattening (like sample_ellipse/sample_spline) so the
    deviation from the true arc stays within `distance`, which means big-radius arcs get
    proportionally more points. This was previously a FIXED 20 points regardless of radius,
    which visibly facets a large-radius perimeter arc - e.g. a 2" radius / 205 deg outer
    profile came out as ~0.36" chords (8 mil off true). The stitched perimeter is emitted
    as linear moves, so this sampling density is exactly what the cut curve inherits.
    Falls back to manual fixed-count sampling if flattening is unavailable.
    """
    try:
        pts = [(p.x, p.y) for p in arc.flattening(distance)]
        if len(pts) >= 2:
            return pts
    except Exception:
        pass
    center = (arc.dxf.center.x, arc.dxf.center.y)
    radius = arc.dxf.radius
    start_angle = math.radians(arc.dxf.start_angle)
    end_angle = math.radians(arc.dxf.end_angle)
    if end_angle <= start_angle:
        end_angle += 2 * math.pi
    num_points = 20
    return [
        (center[0] + radius * math.cos(start_angle + (end_angle - start_angle) * k / num_points),
         center[1] + radius * math.sin(start_angle + (end_angle - start_angle) * k / num_points))
        for k in range(num_points + 1)
    ]


def sample_ellipse(ellipse, distance=CHORD_TOLERANCE):
    """Sample an ELLIPSE entity (full or arc) into a list of (x, y) points.

    Onshape exports curved perimeter transitions/fillets as ELLIPSE arcs; a full
    ellipse (an elliptical hole/pocket) samples to a loop whose ends coincide, which
    entities_to_closed_paths then recognizes as already-closed.
    """
    try:
        return [(p.x, p.y) for p in ellipse.flattening(distance)]
    except Exception:
        return []


def sample_spline(spline, distance=CHORD_TOLERANCE):
    """Sample a SPLINE entity into a list of (x, y) points (control points as fallback)."""
    try:
        points = [(p[0], p[1]) for p in spline.flattening(distance=distance)]
        if points:
            return points
    except Exception:
        pass
    try:
        control_points = [(p[0], p[1]) for p in spline.control_points]
        return control_points if len(control_points) > 1 else []
    except Exception:
        return []


def entities_to_closed_paths(lines=(), arcs=(), ellipses=(), splines=(), polylines=(),
                             snap=0.001, close_tolerance=0.1, on_open_loop=None):
    """Sample and stitch open DXF entities into closed boundary paths.

    Shared endpoints in a CAD export land sub-micron apart, so exact-match stitching
    fragments a loop and it never closes. Snapping every coordinate to a fine grid
    (default 0.001", far below machining tolerance and the closure check) unifies
    coincident-but-not-identical junctions so shapely.linemerge can join them.

    Args:
        lines/arcs/ellipses/splines: ezdxf LINE/ARC/ELLIPSE/SPLINE entities.
        polylines: ezdxf open LWPOLYLINE entities (contribute their points as a segment).
        snap: coordinate snap grid in drawing units (inches).
        close_tolerance: max end-to-end gap for a merged chain to count as closed.
        on_open_loop: optional callback(coords, gap) invoked for a merged chain that did
            NOT close - lets callers warn about a dropped boundary loop (e.g. a lost
            perimeter) instead of silently discarding it.

    Returns:
        List of closed paths, each a list of (x, y) points (no duplicated closing point).
    """
    def snap_point(x, y):
        # Quantize float noise away FIRST (see COORD_DECIMALS) so coincident endpoints
        # never straddle a grid half-step and land on different grid cells.
        return (round(round(x, COORD_DECIMALS) / snap) * snap,
                round(round(y, COORD_DECIMALS) / snap) * snap)

    segments = []
    for line in lines:
        segments.append(LineString([snap_point(line.dxf.start.x, line.dxf.start.y),
                                    snap_point(line.dxf.end.x, line.dxf.end.y)]))
    for arc in arcs:
        pts = [snap_point(x, y) for x, y in sample_arc(arc)]
        if len(pts) >= 2:
            segments.append(LineString(pts))
    for ellipse in ellipses:
        pts = [snap_point(x, y) for x, y in sample_ellipse(ellipse)]
        if len(pts) >= 2:
            segments.append(LineString(pts))
    for spline in splines:
        pts = [snap_point(x, y) for x, y in sample_spline(spline)]
        if len(pts) >= 2:
            segments.append(LineString(pts))
    for polyline in polylines:
        pts = [snap_point(p[0], p[1]) for p in polyline.get_points('xy')]
        if len(pts) >= 2:
            segments.append(LineString(pts))

    if not segments:
        return []

    merged = linemerge(segments)
    geoms = list(merged.geoms) if hasattr(merged, 'geoms') else [merged]

    closed_paths = []
    for geom in geoms:
        coords = list(geom.coords)
        if len(coords) < 3:
            continue
        gap = math.hypot(coords[0][0] - coords[-1][0], coords[0][1] - coords[-1][1])
        if gap >= close_tolerance:
            if on_open_loop is not None:
                on_open_loop(coords, gap)
            continue
        # A last point that lands on - or within a snap step of - the first is the
        # CLOSING point, not a distinct vertex, and must be dropped. Left in place it
        # makes Polygon() close the ring with a hair-length segment that doubles back
        # along the first edge: a zero-area spike that renders the polygon INVALID, and
        # invalid polygons get discarded downstream, so an entire cutout silently
        # vanishes from the part. Exact equality is not a sufficient test - snapping can
        # itself leave the two ends one grid step apart (diagonally, up to snap*sqrt(2)).
        if gap <= snap * 1.5:
            coords = coords[:-1]
            if len(coords) < 3:
                continue
        closed_paths.append(coords)
    return closed_paths


def polygon_from_path(points):
    """Build a Shapely Polygon from a closed boundary path, repairing an invalid ring.

    Every consumer of these paths tests `is_valid`, so a ring that self-touches - a
    zero-area spike from a near-duplicate closing point, a figure-eight from a
    mis-stitched loop - is dropped. That drop is silent and lands on real hardware: the
    feature just disappears and the machine cuts a plate with a missing cutout. So
    repair first, and only report failure when repair yields no enclosed area at all.

    Returns (polygon, coords), coords being the (possibly repaired) ring's points with
    no duplicated closing point, or (None, None) if the path is unusable as a polygon.
    """
    if not points or len(points) < 3:
        return None, None
    try:
        poly = Polygon(points)
    except Exception:
        return None, None
    if poly.is_valid and not poly.is_empty:
        return poly, list(points)
    try:
        repaired = make_valid(poly)
    except Exception:
        return None, None
    # make_valid can hand back a collection: a figure-eight becomes two polygons, a
    # fully collapsed sliver becomes a line. Keep the largest polygon; anything else
    # means there was no real area to machine.
    candidates = [g for g in getattr(repaired, 'geoms', [repaired])
                  if g.geom_type == 'Polygon' and not g.is_empty and g.area > 0]
    if not candidates:
        return None, None
    best = max(candidates, key=lambda g: g.area)
    return best, list(best.exterior.coords)[:-1]
