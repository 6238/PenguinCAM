#!/usr/bin/env python3
"""
PenguinCAM - FRC Team 6238 CAM Post-Processor
Generates G-code from DXF files with predefined operations for:
- Circular holes (helical + spiral clearing)
- Pockets
- Perimeter with tabs
"""

# Standard library
import argparse
import datetime
import json
import math
import os
import re
from collections import defaultdict
from typing import List, Tuple
from zoneinfo import ZoneInfo

# Third-party
import ezdxf

# Local modules
from shapely.geometry import Point, Polygon, LineString, MultiPolygon
from shapely.ops import unary_union, linemerge


# Material presets based on team 6238 feeds/speeds document
MATERIAL_PRESETS = {
    'plywood': {
        'name': 'Plywood',
        'feed_rate': 75.0,        # Cutting feed rate (IPM)
        'ramp_feed_rate': 50.0,   # Ramp feed rate (IPM)
        'plunge_rate': 35.0,      # Plunge feed rate (IPM) for tab Z moves
        'spindle_speed': 18000,   # RPM
        'ramp_angle': 20.0,       # Ramp angle in degrees
        'ramp_start_clearance': 0.150,  # Clearance above material to start ramping (inches)
        'stepover_percentage': 0.65,    # Radial stepover as fraction of tool diameter (65% for plywood)
        'tab_width': 0.25,        # Tab width (inches)
        'tab_height': 0.1,        # Tab height (inches)
        'description': 'Standard plywood settings - 18K RPM, 75 IPM cutting'
    },
    'aluminum': {
        'name': 'Aluminum',
        'feed_rate': 55.0,        # Cutting feed rate (IPM)
        'ramp_feed_rate': 35.0,   # Ramp feed rate (IPM)
        'plunge_rate': 15.0,      # Plunge feed rate (IPM) for tab Z moves - slower for aluminum
        'spindle_speed': 18000,   # RPM
        'ramp_angle': 4.0,        # Ramp angle in degrees
        'ramp_start_clearance': 0.050,  # Clearance above material to start ramping (inches)
        'stepover_percentage': 0.25,    # Radial stepover as fraction of tool diameter (25% conservative for aluminum)
        'tab_width': 0.160,       # Tab width (inches) - smaller for aluminum
        'tab_height': 0.040,      # Tab height (inches) - thinner for aluminum
        'description': 'Aluminum box tubing - 18K RPM, 55 IPM cutting, 4° ramp'
    },
    'polycarbonate': {
        'name': 'Polycarbonate',
        'feed_rate': 75.0,        # Same as plywood
        'ramp_feed_rate': 50.0,   # Same as plywood
        'plunge_rate': 20.0,      # Same as plywood - matches Fusion 360
        'spindle_speed': 18000,   # RPM
        'ramp_angle': 20.0,       # Same as plywood
        'ramp_start_clearance': 0.100,  # Clearance above material to start ramping (inches)
        'stepover_percentage': 0.55,    # Radial stepover as fraction of tool diameter (55% moderate for polycarbonate)
        'tab_width': 0.25,        # Tab width (inches) - same as plywood
        'tab_height': 0.1,        # Tab height (inches) - same as plywood
        'description': 'Polycarbonate - same as plywood settings'
    }
}


class FRCPostProcessor:
    def __init__(self, material_thickness: float, tool_diameter: float, units: str = "inch"):
        """
        Initialize the post-processor
        
        Args:
            material_thickness: Thickness of material in inches
            tool_diameter: Diameter of cutting tool in inches (e.g., 4mm = 0.157")
            units: "inch" or "mm"
        """
        self.material_thickness = material_thickness
        self.tool_diameter = tool_diameter
        self.tool_radius = tool_diameter / 2
        self.units = units
        self.tolerance = 0.02  # Tolerance for hole detection (inches)

        # Minimum hole diameter that can be milled (must be > tool diameter for chip evacuation)
        # Holes smaller than this are skipped
        self.min_millable_hole = tool_diameter * 1.2  # 20% larger than tool for chip clearance
        
        # Z-axis reference: Z=0 is at BOTTOM (sacrifice board surface)
        # This allows zeroing to the sacrifice board instead of material top
        self.sacrifice_board_depth = 0.02  # How far to cut into sacrifice board (inches)
        self.clearance_height = 0.5  # Clearance above material top for rapid moves (inches)

        # Calculated Z positions (Z=0 at sacrifice board)
        self.safe_height = 1.5  # Safe height for rapid moves (absolute, not relative to material)
        self.retract_height = material_thickness + self.clearance_height  # Retract above material
        self.material_top = material_thickness  # Top surface of material
        self.cut_depth = -self.sacrifice_board_depth  # Cut slightly into sacrifice board

        # Cutting parameters (defaults - can be overridden by material presets)
        self.spindle_speed = 18000  # RPM
        self.feed_rate = 75.0 if units == "inch" else 1905  # Cutting feed rate (IPM or mm/min)
        self.ramp_feed_rate = 50.0 if units == "inch" else 1270  # Ramp feed rate (IPM or mm/min)
        self.plunge_rate = 35.0 if units == "inch" else 889  # Plunge feed rate (IPM or mm/min) for tab Z moves
        self.traverse_rate = 100.0 if units == "inch" else 2540  # Lateral moves above material (IPM or mm/min)
        self.approach_rate = 50.0 if units == "inch" else 1270  # Z approach to ramp start height (IPM or mm/min)
        self.ramp_angle = 20.0  # Ramp angle in degrees (for helical bores and perimeter ramps)
        self.ramp_start_clearance = 0.15 if units == "inch" else 3.8  # Clearance above material to start ramping
        self.stepover_percentage = 0.6  # Radial stepover as fraction of tool diameter (default 60%)

        # Tab parameters
        self.tab_width = 0.25  # Width of tabs (inches)
        self.tab_height = 0.1  # How much material to leave in tab (inches) - per team standards
        self.num_tabs = 4  # Number of tabs around perimeter

    def apply_material_preset(self, material: str):
        """
        Apply a material preset to set feeds, speeds, and ramp angles.

        Args:
            material: Material name ('plywood', 'aluminum', 'polycarbonate')
        """
        if material not in MATERIAL_PRESETS:
            print(f"Warning: Unknown material '{material}'. Available: {', '.join(MATERIAL_PRESETS.keys())}")
            print("Using default plywood settings.")
            material = 'plywood'

        preset = MATERIAL_PRESETS[material]
        self.material_name = preset['name']  # Store material name for header

        # Preset values are defined in IPM - convert to mm/min if needed
        if self.units == 'mm':
            self.feed_rate = preset['feed_rate'] * 25.4
            self.ramp_feed_rate = preset['ramp_feed_rate'] * 25.4
            self.plunge_rate = preset['plunge_rate'] * 25.4
            self.ramp_start_clearance = preset['ramp_start_clearance'] * 25.4
        else:
            self.feed_rate = preset['feed_rate']
            self.ramp_feed_rate = preset['ramp_feed_rate']
            self.plunge_rate = preset['plunge_rate']
            self.ramp_start_clearance = preset['ramp_start_clearance']

        self.spindle_speed = preset['spindle_speed']
        self.ramp_angle = preset['ramp_angle']
        self.stepover_percentage = preset['stepover_percentage']

        # Tab sizes (convert to mm if needed)
        if self.units == 'mm':
            self.tab_width = preset['tab_width'] * 25.4
            self.tab_height = preset['tab_height'] * 25.4
        else:
            self.tab_width = preset['tab_width']
            self.tab_height = preset['tab_height']

        print(f"\nApplied material preset: {preset['name']}")
        print(f"  {preset['description']}")
        if self.units == 'mm':
            print(f"  Feed rate: {preset['feed_rate']} IPM ({self.feed_rate:.0f} mm/min)")
            print(f"  Ramp feed rate: {preset['ramp_feed_rate']} IPM ({self.ramp_feed_rate:.0f} mm/min)")
            print(f"  Plunge rate: {preset['plunge_rate']} IPM ({self.plunge_rate:.0f} mm/min)")
            print(f"  Ramp start clearance: {preset['ramp_start_clearance']}\" ({self.ramp_start_clearance:.1f} mm)")
        else:
            print(f"  Feed rate: {self.feed_rate} IPM")
            print(f"  Ramp feed rate: {self.ramp_feed_rate} IPM")
            print(f"  Plunge rate: {self.plunge_rate} IPM")
            print(f"  Ramp start clearance: {self.ramp_start_clearance}\"")
        print(f"  Ramp angle: {self.ramp_angle}°")
        print(f"  Stepover: {self.stepover_percentage*100:.0f}% of tool diameter")
        print(f"  Tab size: {preset['tab_width']}\" x {preset['tab_height']}\" (W x H)")

    def _distance_2d(self, p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
        """Calculate 2D Euclidean distance between two points"""
        return math.sqrt((p2[0] - p1[0])**2 + (p2[1] - p1[1])**2)

    def load_dxf(self, filename: str):
        """Load DXF file and extract geometry"""
        print(f"Loading {filename}...")
        doc = ezdxf.readfile(filename)
        msp = doc.modelspace()

        # Extract circles (holes)
        self.circles = []
        for entity in msp.query('CIRCLE'):
            center = (entity.dxf.center.x, entity.dxf.center.y)
            radius = entity.dxf.radius
            self.circles.append({'center': center, 'radius': radius, 'diameter': radius * 2})

        # Initialize geometry lists for transform_coordinates compatibility
        self.lines = []  # Individual lines (converted to polylines)
        self.arcs = []   # Individual arcs (converted to polylines)
        self.splines = []  # Individual splines (converted to polylines)

        # Extract polylines and lines (boundaries/pockets)
        self.polylines = []
        
        # Method 1: Look for LWPOLYLINE entities
        for entity in msp.query('LWPOLYLINE'):
            points = [(p[0], p[1]) for p in entity.get_points('xy')]
            if entity.closed and len(points) > 2:
                self.polylines.append(points)
        
        # Method 2: Look for POLYLINE entities
        for entity in msp.query('POLYLINE'):
            if entity.is_2d_polyline:
                points = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
                if entity.is_closed and len(points) > 2:
                    self.polylines.append(points)
        
        # Method 3: Collect individual LINE, ARC, SPLINE entities and try to form closed paths
        # This is needed for OnShape exports which use individual entities
        lines = list(msp.query('LINE'))
        arcs = list(msp.query('ARC'))
        splines = list(msp.query('SPLINE'))
        
        if lines or arcs or splines:
            print(f"Found {len(lines)} lines, {len(arcs)} arcs, {len(splines)} splines - attempting to form closed paths...")
            closed_paths = self._chain_entities_to_paths(lines, arcs, splines)
            self.polylines.extend(closed_paths)
        
        print(f"Found {len(self.circles)} circles and {len(self.polylines)} closed paths")
    
    def _chain_entities_to_paths(self, lines, arcs, splines):
        """
        Chain individual LINE, ARC, and SPLINE entities into closed paths.
        This handles DXF exports from OnShape and other CAD programs that don't use polylines.
        """
        # First, try the graph-based approach for exact geometry
        print("  Attempting to connect segments into exact paths...")
        exact_paths = self._connect_segments_graph_based(lines, arcs, splines)
        if exact_paths:
            return exact_paths
        
        # Fallback: Convert all entities to linestrings and try merge
        print("  Falling back to linestring merge...")
        all_linestrings = []
        
        # Add LINE entities
        for line in lines:
            start = (line.dxf.start.x, line.dxf.start.y)
            end = (line.dxf.end.x, line.dxf.end.y)
            all_linestrings.append(LineString([start, end]))
        
        # Add ARC entities (sample them into line segments)
        for arc in arcs:
            points = self._sample_arc(arc, num_points=20)
            if len(points) >= 2:
                all_linestrings.append(LineString(points))
        
        # Add SPLINE entities (sample them into line segments)
        for spline in splines:
            points = self._sample_spline(spline, num_points=30)
            if len(points) >= 2:
                all_linestrings.append(LineString(points))
        
        if not all_linestrings:
            return []
        
        try:
            # Merge connected line segments
            merged = linemerge(all_linestrings)
            
            # Extract closed paths
            closed_paths = []
            tolerance = 0.1  # 0.1" tolerance for "almost closed"
            
            # Check if we got a single geometry or multiple
            geoms_to_check = []
            if hasattr(merged, 'geoms'):
                geoms_to_check = list(merged.geoms)
            else:
                geoms_to_check = [merged]
            
            for geom in geoms_to_check:
                coords = list(geom.coords)
                if len(coords) < 3:
                    continue
                
                # Check if path is closed or nearly closed
                start = Point(coords[0])
                end = Point(coords[-1])
                distance = start.distance(end)
                
                is_closed = (coords[0] == coords[-1]) or distance < tolerance
                
                if is_closed:
                    # Remove duplicate closing point if present
                    if coords[0] == coords[-1]:
                        coords = coords[:-1]
                    
                    if len(coords) > 2:
                        closed_paths.append(coords)
                        print(f"  Found closed path with {len(coords)} points (gap: {distance:.4f}\")")
            
            # If we still didn't find closed paths, try creating convex hull (last resort)
            if not closed_paths and all_linestrings:
                print("  Attempting to form polygon from all segments (APPROXIMATE)...")
                try:
                    union = unary_union(all_linestrings)
                    if hasattr(union, 'convex_hull'):
                        hull = union.convex_hull
                        if isinstance(hull, Polygon) and len(hull.exterior.coords) > 3:
                            coords = list(hull.exterior.coords)[:-1]
                            closed_paths.append(coords)
                            print(f"  ⚠️  Created convex hull with {len(coords)} points (LOSES DETAIL!)")
                            print(f"  ⚠️  This is approximate - concave features will be lost!")
                except Exception as e:
                    print(f"  Could not create polygon: {e}")
            
            return closed_paths
            
        except Exception as e:
            print(f"Warning: Could not automatically chain entities into paths: {e}")
            return []
    
    def _connect_segments_graph_based(self, lines, arcs, splines):
        """
        Build a connectivity graph and find closed cycles.
        This preserves exact geometry including curves.
        """
        # Build list of all segments with their endpoints
        segments = []
        
        # Add lines
        for line in lines:
            start = (line.dxf.start.x, line.dxf.start.y)
            end = (line.dxf.end.x, line.dxf.end.y)
            points = [start, end]
            segments.append({'type': 'line', 'points': points, 'start': start, 'end': end})
        
        # Add arcs (sampled)
        for arc in arcs:
            points = self._sample_arc(arc, num_points=20)
            if len(points) >= 2:
                segments.append({'type': 'arc', 'points': points, 'start': points[0], 'end': points[-1]})
        
        # Add splines (sampled)
        for spline in splines:
            points = self._sample_spline(spline, num_points=30)
            if len(points) >= 2:
                segments.append({'type': 'spline', 'points': points, 'start': points[0], 'end': points[-1]})
        
        if not segments:
            return []
        
        # Build adjacency graph
        tolerance = 0.01  # 0.01" tolerance for matching endpoints

        def points_match(p1, p2, tol=tolerance):
            return self._distance_2d(p1, p2) < tol
        
        # Find which segments connect to which
        graph = defaultdict(list)  # endpoint -> list of (segment_idx, is_start)
        
        for idx, seg in enumerate(segments):
            # Add connections for start point
            start_key = self._round_point(seg['start'], 3)
            graph[start_key].append((idx, True))
            
            # Add connections for end point
            end_key = self._round_point(seg['end'], 3)
            graph[end_key].append((idx, False))
        
        # Find closed cycles
        visited = set()
        closed_paths = []
        
        for start_idx in range(len(segments)):
            if start_idx in visited:
                continue
            
            # Try to build a path starting from this segment
            path_segments = []
            path_points = []
            current_idx = start_idx
            current_end = segments[start_idx]['end']
            
            # Add first segment
            path_segments.append(current_idx)
            path_points.extend(segments[current_idx]['points'][:-1])  # Don't duplicate endpoints
            visited.add(current_idx)
            
            # Try to find next segments
            max_iterations = len(segments)
            for _ in range(max_iterations):
                # Look for a segment that starts where we ended
                end_key = self._round_point(current_end, 3)
                
                next_found = False
                for next_idx, is_start in graph[end_key]:
                    if next_idx == current_idx or next_idx in path_segments:
                        continue
                    
                    # Found a connection!
                    seg = segments[next_idx]
                    
                    if is_start:
                        # Segment starts where we ended - add it forward
                        path_segments.append(next_idx)
                        path_points.extend(seg['points'][:-1])
                        current_end = seg['end']
                    else:
                        # Segment ends where we ended - add it reversed
                        path_segments.append(next_idx)
                        reversed_points = list(reversed(seg['points']))
                        path_points.extend(reversed_points[:-1])
                        current_end = seg['start']
                    
                    visited.add(next_idx)
                    next_found = True
                    break
                
                if not next_found:
                    break
                
                # Check if we've closed the loop
                start_point = segments[start_idx]['start']
                if points_match(current_end, start_point):
                    # Closed path found!
                    if len(path_points) > 3:
                        closed_paths.append(path_points)
                        print(f"  Found exact closed path with {len(path_points)} points using {len(path_segments)} segments")
                    break
        
        return closed_paths
    
    def _round_point(self, point, decimals=3):
        """Round a point to create a hashable key for graph"""
        return (round(point[0], decimals), round(point[1], decimals))
    
    def _sample_arc(self, arc, num_points=20):
        """Sample an ARC entity into a series of points"""
        center = (arc.dxf.center.x, arc.dxf.center.y)
        radius = arc.dxf.radius
        start_angle = math.radians(arc.dxf.start_angle)
        end_angle = math.radians(arc.dxf.end_angle)
        
        # Handle angle wrapping
        if end_angle < start_angle:
            end_angle += 2 * math.pi
        
        points = []
        for i in range(num_points + 1):
            t = i / num_points
            angle = start_angle + t * (end_angle - start_angle)
            x = center[0] + radius * math.cos(angle)
            y = center[1] + radius * math.sin(angle)
            points.append((x, y))
        
        return points
    
    def _sample_spline(self, spline, num_points=30):
        """Sample a SPLINE entity into a series of points"""
        try:
            # Use ezdxf's built-in spline sampling
            points = []
            for point in spline.flattening(distance=0.01):
                points.append((point[0], point[1]))
            return points if points else []
        except:
            # Fallback: use control points
            try:
                control_points = [(p[0], p[1]) for p in spline.control_points]
                return control_points if len(control_points) > 1 else []
            except:
                return []
        
    def transform_coordinates(self, origin_corner: str, rotation_angle: int):
        """
        Transform all coordinates based on origin corner and rotation.
        
        Args:
            origin_corner: 'bottom-left', 'bottom-right', 'top-left', 'top-right'
            rotation_angle: 0, 90, 180, 270 degrees clockwise
        """
        # First, find bounding box of ALL entities
        all_x = []
        all_y = []
        
        # Collect all X,Y coordinates
        for circle in self.circles:
            all_x.append(circle['center'][0])
            all_y.append(circle['center'][1])
        
        for line in self.lines:
            all_x.extend([line['start'][0], line['end'][0]])
            all_y.extend([line['start'][1], line['end'][1]])
        
        for arc in self.arcs:
            all_x.append(arc['center'][0])
            all_y.append(arc['center'][1])
            # Approximate arc bounds
            radius = arc['radius']
            all_x.extend([arc['center'][0] - radius, arc['center'][0] + radius])
            all_y.extend([arc['center'][1] - radius, arc['center'][1] + radius])
        
        for spline in self.splines:
            points = self._sample_spline(spline)
            for x, y in points:
                all_x.append(x)
                all_y.append(y)

        for polyline in self.polylines:
            for x, y in polyline:
                all_x.append(x)
                all_y.append(y)

        if not all_x or not all_y:
            print("Warning: No geometry found for transformation")
            return
        
        minX, maxX = min(all_x), max(all_x)
        minY, maxY = min(all_y), max(all_y)
        centerX = (minX + maxX) / 2
        centerY = (minY + maxY) / 2
        
        print(f"\nApplying transformation:")
        print(f"  Origin corner: {origin_corner}")
        print(f"  Rotation: {rotation_angle}°")
        print(f"  Original bounds: X=[{minX:.3f}, {maxX:.3f}], Y=[{minY:.3f}, {maxY:.3f}]")
        
        # Step 1: Rotate around center if needed
        if rotation_angle != 0:
            angle_rad = -math.radians(rotation_angle)  # Negative for clockwise
            cos_a = math.cos(angle_rad)
            sin_a = math.sin(angle_rad)
            
            def rotate_point(x, y):
                # Translate to origin
                x -= centerX
                y -= centerY
                # Rotate
                new_x = x * cos_a - y * sin_a
                new_y = x * sin_a + y * cos_a
                # Translate back
                new_x += centerX
                new_y += centerY
                return new_x, new_y
            
            # Rotate all entities
            for circle in self.circles:
                circle['center'] = rotate_point(*circle['center'])
            
            for line in self.lines:
                line['start'] = rotate_point(*line['start'])
                line['end'] = rotate_point(*line['end'])
            
            for arc in self.arcs:
                arc['center'] = rotate_point(*arc['center'])
                # Update angles for rotation
                arc['start_angle'] = (arc['start_angle'] - rotation_angle) % 360
                arc['end_angle'] = (arc['end_angle'] - rotation_angle) % 360
            
            for spline in self.splines:
                # For splines, we need to recreate - for now, skip
                # This is a limitation but rarely matters for FRC parts
                pass

            for i, polyline in enumerate(self.polylines):
                self.polylines[i] = [rotate_point(x, y) for x, y in polyline]

            # Recalculate bounds after rotation
            all_x = []
            all_y = []
            for circle in self.circles:
                all_x.append(circle['center'][0])
                all_y.append(circle['center'][1])
            for line in self.lines:
                all_x.extend([line['start'][0], line['end'][0]])
                all_y.extend([line['start'][1], line['end'][1]])
            for arc in self.arcs:
                all_x.append(arc['center'][0])
                all_y.append(arc['center'][1])
                radius = arc['radius']
                all_x.extend([arc['center'][0] - radius, arc['center'][0] + radius])
                all_y.extend([arc['center'][1] - radius, arc['center'][1] + radius])

            for polyline in self.polylines:
                for x, y in polyline:
                    all_x.append(x)
                    all_y.append(y)

            minX, maxX = min(all_x), max(all_x)
            minY, maxY = min(all_y), max(all_y)
        
        # Step 2: Translate based on origin corner
        # We want the selected corner to become (0, 0)
        if origin_corner == 'bottom-left':
            offsetX, offsetY = -minX, -minY
        elif origin_corner == 'bottom-right':
            offsetX, offsetY = -maxX, -minY
        elif origin_corner == 'top-left':
            offsetX, offsetY = -minX, -maxY
        elif origin_corner == 'top-right':
            offsetX, offsetY = -maxX, -maxY
        
        def translate_point(x, y):
            return x + offsetX, y + offsetY
        
        # Translate all entities
        for circle in self.circles:
            circle['center'] = translate_point(*circle['center'])
        
        for line in self.lines:
            line['start'] = translate_point(*line['start'])
            line['end'] = translate_point(*line['end'])
        
        for arc in self.arcs:
            arc['center'] = translate_point(*arc['center'])

        for i, polyline in enumerate(self.polylines):
            self.polylines[i] = [translate_point(x, y) for x, y in polyline]

        # Calculate new bounds
        all_x = []
        all_y = []
        for circle in self.circles:
            all_x.append(circle['center'][0])
            all_y.append(circle['center'][1])
        for line in self.lines:
            all_x.extend([line['start'][0], line['end'][0]])
            all_y.extend([line['start'][1], line['end'][1]])
        for polyline in self.polylines:
            for x, y in polyline:
                all_x.append(x)
                all_y.append(y)

        new_minX, new_maxX = min(all_x), max(all_x)
        new_minY, new_maxY = min(all_y), max(all_y)
        
        print(f"  Transformed bounds: X=[{new_minX:.3f}, {new_maxX:.3f}], Y=[{new_minY:.3f}, {new_maxY:.3f}]")
        print(f"  New origin (0,0) is at the {origin_corner} corner\n")
    
    def classify_holes(self):
        """Classify holes by diameter"""
        # Classify all circles as holes (apply size check)
        self.holes = []
        holes_skipped = 0

        for circle in self.circles:
            diameter = circle['diameter']
            center = circle['center']

            # Skip holes that are too small to mill with this tool
            if diameter < self.min_millable_hole:
                print(f"  ⚠️  Skipping hole at ({center[0]:.3f}, {center[1]:.3f}) - diameter {diameter:.3f}\" too small for {self.tool_diameter:.3f}\" tool")
                holes_skipped += 1
                continue

            # All millable holes use the same strategy (helical + spiral)
            self.holes.append({'center': center, 'diameter': diameter})
            print(f"  Hole (d={diameter:.3f}\") at ({center[0]:.3f}, {center[1]:.3f})")

        print(f"\nIdentified {len(self.holes)} millable holes")
        if holes_skipped > 0:
            print(f"  ⚠️  Skipped {holes_skipped} hole(s) too small for tool")

        # Sort holes to minimize travel time
        self._sort_holes()

    def _sort_holes(self):
        """
        Sort holes to minimize tool travel time.
        Sorts by X coordinate first, then by Y within each X group (zigzag pattern).
        """
        if len(self.holes) > 1:
            # Sort holes by X, then Y (holes now contains all millable holes)
            self.holes.sort(key=lambda h: (round(h['center'][0], 2), h['center'][1]))
            print(f"Sorted {len(self.holes)} holes for optimal travel")
    
    def identify_perimeter_and_pockets(self):
        """Identify the outer perimeter and any inner pockets"""
        if not self.polylines:
            self.perimeter = None
            self.pockets = []
            return
        
        # Convert to Shapely polygons
        polygons = []
        for points in self.polylines:
            try:
                poly = Polygon(points)
                if poly.is_valid:
                    polygons.append((poly, points))
            except:
                pass
        
        if not polygons:
            self.perimeter = None
            self.pockets = []
            return
        
        # Find the largest polygon (perimeter)
        polygons.sort(key=lambda x: x[0].area, reverse=True)
        self.perimeter = polygons[0][1]  # Get the original points
        self.pockets = [p[1] for p in polygons[1:]]

        print(f"\nIdentified perimeter and {len(self.pockets)} pockets")
    
    def generate_gcode(self, output_file: str):
        """Generate complete G-code file"""
        gcode = []

        # Load machine config
        config_path = os.path.join(os.path.dirname(__file__), 'machine_config.json')
        try:
            with open(config_path, 'r') as f:
                machine_config = json.load(f)
        except:
            # Default config if file not found
            machine_config = {
                'machine': {'name': 'CNC Router', 'controller': 'Generic', 'coolant': 'Air'},
                'team': {'number': 0, 'name': 'FRC Team'}
            }

        # Generate timestamp in Pacific time
        pacific_time = datetime.datetime.now(ZoneInfo("America/Los_Angeles"))
        timestamp = pacific_time.strftime("%Y-%m-%d %H:%M")

        # Determine operations present
        operations = []
        if self.holes:
            operations.append("Holes")
        if self.pockets:
            operations.append("Pockets")
        if self.perimeter:
            operations.append("Profile")
        operations_str = ", ".join(operations) if operations else "None"

        # Calculate helical entry angle
        helical_angle = f"~{int(self.ramp_angle)} deg"

        # Generate comprehensive header
        team = machine_config.get('team', {})
        machine = machine_config.get('machine', {})

        gcode.append(f"({team.get('name', 'FRC Team').upper()} - Team {team.get('number', '0000')})")
        gcode.append("(PenguinCAM CNC Post-Processor)")

        if hasattr(self, 'user_name'):
            gcode.append(f"(Generated by: {self.user_name} on {timestamp})")
        else:
            gcode.append(f"(Generated on: {timestamp})")
        gcode.append("")

        gcode.append(f"(Machine: {machine.get('name', 'CNC Router')})")
        gcode.append(f"(Controller: {machine.get('controller', 'Generic')})")
        gcode.append(f"(Units: {'Inches' if self.units == 'inch' else 'Millimeters'} - {'G20' if self.units == 'inch' else 'G21'})")
        gcode.append("(Coordinate system: G54)")
        gcode.append("(Plane: G17 - XY)")
        gcode.append("(Arc centers: Incremental - G91.1)")
        gcode.append("")

        material_info = f"{self.material_thickness}\""
        if hasattr(self, 'material_name'):
            material_info = f"{self.material_name} - {material_info} thick"
        else:
            material_info = f"{material_info} thick"
        gcode.append(f"(Material: {material_info})")
        gcode.append(f"(Tool: {self.tool_diameter}\" diam Flat End Mill)")
        gcode.append(f"(Spindle: {self.spindle_speed} RPM)")
        gcode.append(f"(Coolant: {machine.get('coolant', 'None')})")
        gcode.append("")

        gcode.append(f"(ZMIN: {self.cut_depth:.4f}\")")
        gcode.append(f"(Safe Z: {self.safe_height:.4f}\")")
        gcode.append(f"(Retract Z: {self.retract_height:.4f}\")")
        gcode.append("")

        gcode.append(f"(Operations: {operations_str})")
        gcode.append(f"(Helical entry angle: {helical_angle})")
        gcode.append("(No straight plunges)")
        gcode.append("")

        gcode.append("(Z-AXIS REFERENCE:)")
        gcode.append("(  Z=0 is at SACRIFICE BOARD surface)")
        gcode.append(f"(  Material top: Z={self.material_top:.4f}\")")
        gcode.append(f"(  Cuts {self.sacrifice_board_depth:.4f}\" into sacrifice board)")
        gcode.append("(  ** VERIFY Z-ZERO BEFORE RUNNING **)")
        gcode.append("")

        # Modal G-code setup (similar to Fusion 360)
        gcode.append("G90 G94 G91.1 G40 G49 G17")
        gcode.append("(G90=Absolute, G94=Feed/min, G91.1=Arc centers incremental [IJK relative to start point], G40=Cutter comp cancel, G49=Tool length comp cancel, G17=XY plane)")

        # Units
        if self.units == "inch":
            gcode.append("G20  ; Inches")
        else:
            gcode.append("G21  ; Millimeters")

        # Home Z axis (G28) - use G0 for rapid speed
        gcode.append("G0 G28 G91 Z0.  ; Home Z axis at rapid speed")
        gcode.append("G90  ; Back to absolute mode")
        gcode.append("")

        # Spindle on
        gcode.append(f"S{self.spindle_speed} M3  ; Spindle on at {self.spindle_speed} RPM")
        gcode.append("G4 P2  ; Wait 2 seconds for spindle to reach speed")
        gcode.append("")

        # Set work coordinate system
        gcode.append("G54  ; Use work coordinate system 1")
        gcode.append("")

        # Initial safe move to machine coordinate Z0 (stay high to avoid fixture collisions during XY moves)
        gcode.append("G53 G0 Z0.  ; Move to machine coordinate Z0 (safe clearance) - stay high for XY rapids")
        gcode.append("G0 X0 Y0  ; Rapid to work origin")
        gcode.append("")

        # Holes (all circular features - helical entry + spiral clearing)
        if self.holes:
            gcode.append("(===== HOLES =====)")
            for i, hole in enumerate(self.holes, 1):
                center = hole['center']
                diameter = hole['diameter']
                gcode.append(f"(Hole {i} - {diameter:.3f}\" diameter)")
                gcode.extend(self._generate_hole_gcode(center[0], center[1], diameter))
                gcode.append("")
        
        # Pockets
        if self.pockets:
            gcode.append("(===== POCKETS =====)")
            for i, pocket in enumerate(self.pockets, 1):
                gcode.append(f"(Pocket {i})")
                gcode.extend(self._generate_pocket_gcode(pocket))
                gcode.append("")
        
        # Perimeter with tabs
        if self.perimeter:
            gcode.append("(===== PERIMETER WITH TABS =====)")
            gcode.extend(self._generate_perimeter_gcode(self.perimeter))
            gcode.append("")
        
        # Footer
        gcode.append("(===== FINISH =====)")
        gcode.append(f"G0 Z{self.safe_height:.4f}  ; Move to safe height")
        gcode.append("G53 G0 Z0.  ; Move to machine coordinate Z0 (safe clearance)")
        gcode.append("M5  ; Spindle off")
        gcode.append("G53 G0 X0.5 Y23.5  ; Move gantry to back of machine for easy access")
        gcode.append("M30  ; Program end")
        gcode.append("")

        # Calculate estimated cycle time
        time_estimate = self._estimate_cycle_time(gcode)

        # Add cycle time to header (insert after the operations section)
        for i, line in enumerate(gcode):
            if line.startswith("(Operations:"):
                # Find where to insert (after "No straight plunges")
                insert_idx = i + 3  # After Operations, Helical angle, No straight plunges
                time_lines = [
                    "",
                    f"(Estimated cycle time: {self._format_time(time_estimate['total'])})",
                    f"(  Cutting: {self._format_time(time_estimate['cutting'])}, Rapids: {self._format_time(time_estimate['rapid'])}, Spindle: {self._format_time(time_estimate['dwell'])})",
                    "(  Note: Estimate does not include acceleration/deceleration)"
                ]
                for j, time_line in enumerate(time_lines):
                    gcode.insert(insert_idx + j, time_line)
                break

        # Write to file
        with open(output_file, 'w') as f:
            f.write('\n'.join(gcode))

        print(f"\nG-code written to {output_file}")
        print(f"Total lines: {len(gcode)}")
        print(f"\n⏱️  ESTIMATED_CYCLE_TIME: {time_estimate['total']:.1f} seconds ({self._format_time(time_estimate['total'])})")
        print(f"   Cutting: {self._format_time(time_estimate['cutting'])}, Rapids: {self._format_time(time_estimate['rapid'])}, Spindle: {self._format_time(time_estimate['dwell'])}")
    
    def _calculate_helical_passes(self, toolpath_radius: float, target_angle_deg: float = None, ramp_start_height: float = None) -> Tuple[int, float]:
        """
        Calculate number of helical passes needed for a safe plunge angle.

        Args:
            toolpath_radius: Radius of the circular toolpath
            target_angle_deg: Target plunge angle in degrees (default uses self.ramp_angle)
            ramp_start_height: Z height to start ramping from (default uses material_top + ramp_start_clearance)

        Returns:
            Tuple of (number_of_passes, depth_per_pass)
        """
        import math

        # Use material-specific ramp angle if not specified
        if target_angle_deg is None:
            target_angle_deg = self.ramp_angle

        # Use ramp start height if specified, otherwise use material_top + clearance
        if ramp_start_height is None:
            ramp_start_height = self.material_top + self.ramp_start_clearance

        # Total depth to cut (from ramp start height down to cut depth)
        total_depth = ramp_start_height - self.cut_depth

        # Circumference of one revolution
        circumference = 2 * math.pi * toolpath_radius

        # For target angle: depth_per_rev = circumference * tan(angle)
        target_depth_per_rev = circumference * math.tan(math.radians(target_angle_deg))

        # Number of passes needed
        num_passes = max(1, int(math.ceil(total_depth / target_depth_per_rev)))
        depth_per_pass = total_depth / num_passes

        return num_passes, depth_per_pass

    def _generate_hole_gcode(self, cx: float, cy: float, diameter: float) -> List[str]:
        """
        Generate G-code for a hole using helical entry + spiral-out strategy.
        Uses helical interpolation to safely enter, then spirals outward in multiple passes.

        Args:
            cx, cy: Hole center coordinates
            diameter: Hole diameter (from CAD)
        """
        gcode = []

        # Calculate target toolpath radius (hole radius minus tool radius for inside cut)
        hole_radius = diameter / 2
        final_toolpath_radius = hole_radius - self.tool_radius

        if final_toolpath_radius <= 0:
            gcode.append(f"(WARNING: Tool diameter {self.tool_diameter:.4f}\" is too large for {diameter:.4f}\" hole!)")
            return gcode

        # Strategy: Helical entry at small radius, then spiral outward
        # Each pass increases the radius by stepover percentage (material-specific)
        stepover = self.tool_diameter * self.stepover_percentage
        num_radial_passes = max(1, int(math.ceil(final_toolpath_radius / stepover)))

        # Calculate ramp start height (close to material surface)
        ramp_start_height = self.material_top + self.ramp_start_clearance

        # Calculate helical entry passes
        entry_radius = min(stepover, final_toolpath_radius)  # Use first stepover radius
        num_helical_passes, depth_per_pass = self._calculate_helical_passes(entry_radius, ramp_start_height=ramp_start_height)

        gcode.append(f"(Hole {diameter:.3f}\" dia: helical entry at {entry_radius:.4f}\" radius, then {num_radial_passes} radial passes)")

        # Position at edge of entry radius
        start_x = cx + entry_radius
        start_y = cy
        gcode.append(f"G1 X{start_x:.4f} Y{start_y:.4f} F{self.traverse_rate}  ; Position at entry radius")
        gcode.append(f"G1 Z{ramp_start_height:.4f} F{self.approach_rate}  ; Approach to ramp start height")

        # Helical entry in multiple passes using ramp feed rate
        gcode.append(f"(Helical entry: {num_helical_passes} passes at {self.ramp_angle}°, {depth_per_pass:.4f}\" per pass)")
        for pass_num in range(num_helical_passes):
            target_z = ramp_start_height - (pass_num + 1) * depth_per_pass
            gcode.append(f"G3 X{start_x:.4f} Y{start_y:.4f} I{-entry_radius:.4f} J0 Z{target_z:.4f} F{self.ramp_feed_rate}  ; Helical pass {pass_num + 1}/{num_helical_passes} CCW for climb milling")

        # Clean up pass at entry radius and final depth
        gcode.append(f"G3 X{start_x:.4f} Y{start_y:.4f} I{-entry_radius:.4f} J0 F{self.feed_rate}  ; Clean up pass at entry radius CCW for climb milling")

        # True Archimedean spiral outward from entry radius to final radius
        # Spiral equation: r = r_start + b*θ where b = stepover/(2π)
        radius_delta = final_toolpath_radius - entry_radius

        if radius_delta > 0.001:
            # Calculate spiral parameters
            spiral_constant = stepover / (2 * math.pi)
            total_angle = radius_delta / spiral_constant if spiral_constant > 0 else 0

            # Generate spiral points
            angle_increment = math.radians(10)  # 10 degrees per segment
            num_points = int(math.ceil(total_angle / angle_increment))

            gcode.append(f"(Archimedean spiral: {num_points} points from r={entry_radius:.4f}\" to r={final_toolpath_radius:.4f}\")")

            # Cut continuous spiral from entry_radius to final_toolpath_radius
            # Use positive angle for counter-clockwise spiral (climb milling on inside feature)
            for i in range(num_points):
                current_angle = i * angle_increment  # Positive for counter-clockwise
                current_radius = entry_radius + spiral_constant * current_angle

                # Convert polar coordinates to Cartesian
                x = cx + current_radius * math.cos(current_angle)
                y = cy + current_radius * math.sin(current_angle)

                gcode.append(f"G1 X{x:.4f} Y{y:.4f} F{self.feed_rate}")

        # Final cleanup pass at exact final radius
        final_x = cx + final_toolpath_radius
        final_y = cy
        gcode.append(f"(Final cleanup pass at exact radius)")
        gcode.append(f"G1 X{final_x:.4f} Y{final_y:.4f} F{self.feed_rate}  ; Move to final radius")
        gcode.append(f"G3 X{final_x:.4f} Y{final_y:.4f} I{-final_toolpath_radius:.4f} J0 F{self.feed_rate}  ; Cut final circle CCW for climb milling")

        # Retract
        gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract")

        return gcode

    def _estimate_cycle_time(self, gcode_lines: List[str]) -> dict:
        """
        Estimate total cycle time from G-code.
        Returns dict with breakdown of time components.
        """
        cutting_time = 0.0  # G1/G2/G3 moves
        rapid_time = 0.0    # G0 moves
        dwell_time = 0.0    # G4 pauses

        # Assume typical rapid speed (machine dependent)
        rapid_speed = 400.0  # IPM - conservative estimate

        current_x = 0.0
        current_y = 0.0
        current_z = 0.0
        current_feed = self.feed_rate

        for line in gcode_lines:
            # Remove comments
            line = re.sub(r'\(.*?\)', '', line).strip()
            line = re.sub(r';.*$', '', line).strip()

            if not line:
                continue

            # Parse G-code command
            if line.startswith('G0'):
                # Rapid move
                x, y, z = current_x, current_y, current_z
                if 'X' in line:
                    x = float(re.search(r'X([-\d.]+)', line).group(1))
                if 'Y' in line:
                    y = float(re.search(r'Y([-\d.]+)', line).group(1))
                if 'Z' in line:
                    z = float(re.search(r'Z([-\d.]+)', line).group(1))

                distance = math.sqrt((x - current_x)**2 + (y - current_y)**2 + (z - current_z)**2)
                rapid_time += distance / rapid_speed * 60  # Convert to seconds

                current_x, current_y, current_z = x, y, z

            elif line.startswith('G1'):
                # Linear cutting move
                x, y, z = current_x, current_y, current_z
                feed = current_feed

                if 'X' in line:
                    x = float(re.search(r'X([-\d.]+)', line).group(1))
                if 'Y' in line:
                    y = float(re.search(r'Y([-\d.]+)', line).group(1))
                if 'Z' in line:
                    z = float(re.search(r'Z([-\d.]+)', line).group(1))
                if 'F' in line:
                    feed = float(re.search(r'F([-\d.]+)', line).group(1))
                    current_feed = feed

                distance = math.sqrt((x - current_x)**2 + (y - current_y)**2 + (z - current_z)**2)
                cutting_time += distance / feed * 60  # Convert to seconds

                current_x, current_y, current_z = x, y, z

            elif line.startswith('G2') or line.startswith('G3'):
                # Arc move
                x, y, z = current_x, current_y, current_z
                feed = current_feed

                if 'X' in line:
                    x = float(re.search(r'X([-\d.]+)', line).group(1))
                if 'Y' in line:
                    y = float(re.search(r'Y([-\d.]+)', line).group(1))
                if 'Z' in line:
                    z = float(re.search(r'Z([-\d.]+)', line).group(1))
                if 'F' in line:
                    feed = float(re.search(r'F([-\d.]+)', line).group(1))
                    current_feed = feed

                # Get arc center offsets
                i = 0.0
                j = 0.0
                if 'I' in line:
                    i = float(re.search(r'I([-\d.]+)', line).group(1))
                if 'J' in line:
                    j = float(re.search(r'J([-\d.]+)', line).group(1))

                # Calculate arc length (approximate)
                center_x = current_x + i
                center_y = current_y + j
                radius = math.sqrt(i**2 + j**2)

                # Calculate angle swept
                start_angle = math.atan2(current_y - center_y, current_x - center_x)
                end_angle = math.atan2(y - center_y, x - center_x)
                angle = end_angle - start_angle

                # Handle full circles and direction (G2=CW, G3=CCW)
                if abs(angle) < 0.001:  # Full circle
                    angle = 2 * math.pi
                elif line.startswith('G2') and angle > 0:
                    angle -= 2 * math.pi
                elif line.startswith('G3') and angle < 0:
                    angle += 2 * math.pi

                arc_length = abs(angle * radius)

                # Add Z component if helical
                z_distance = abs(z - current_z)
                total_distance = math.sqrt(arc_length**2 + z_distance**2)

                cutting_time += total_distance / feed * 60  # Convert to seconds

                current_x, current_y, current_z = x, y, z

            elif line.startswith('G4'):
                # Dwell
                if 'P' in line:
                    dwell_seconds = float(re.search(r'P([-\d.]+)', line).group(1))
                    dwell_time += dwell_seconds

        total_time = cutting_time + rapid_time + dwell_time

        return {
            'total': total_time,
            'cutting': cutting_time,
            'rapid': rapid_time,
            'dwell': dwell_time
        }

    def _format_time(self, seconds: float) -> str:
        """Format seconds as human-readable time string"""
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            minutes = int(seconds // 60)
            secs = int(seconds % 60)
            return f"{minutes}m {secs}s"
        else:
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            return f"{hours}h {minutes}m"

    def _is_pocket_circular(self, pocket_points: List[Tuple[float, float]], tolerance: float = 0.1) -> bool:
        """
        Detect if a pocket is circular by checking if all vertices are equidistant from centroid.

        Args:
            pocket_points: List of (x, y) coordinates
            tolerance: Relative tolerance (0.1 = 10% variation allowed)

        Returns:
            True if pocket is circular, False otherwise
        """
        pocket_poly = Polygon(pocket_points)
        cx = pocket_poly.centroid.x
        cy = pocket_poly.centroid.y

        # Calculate distances from centroid to all vertices
        distances = []
        for x, y in pocket_points:
            dist = self._distance_2d((x, y), (cx, cy))
            distances.append(dist)

        if not distances:
            return False

        # Check if all distances are within tolerance of the average
        avg_dist = sum(distances) / len(distances)
        max_deviation = max(abs(d - avg_dist) for d in distances)
        relative_deviation = max_deviation / avg_dist if avg_dist > 0 else 0

        return relative_deviation < tolerance

    def _generate_pocket_gcode(self, pocket_points: List[Tuple[float, float]]) -> List[str]:
        """Generate G-code for a pocket with tool compensation (offset inward) and helical entry.
        Uses spiral clearing for circular pockets, contour-parallel for non-circular."""
        gcode = []

        # Create offset path (inward by tool radius)
        pocket_poly = Polygon(pocket_points)

        # Buffer inward (negative buffer)
        offset_poly = pocket_poly.buffer(-self.tool_radius)

        if offset_poly.is_empty or offset_poly.area < 0.001:
            gcode.append(f"(WARNING: Pocket too small for tool diameter {self.tool_diameter:.4f}\")")
            return gcode

        # Get the boundary of the offset polygon
        if hasattr(offset_poly, 'exterior'):
            offset_points = list(offset_poly.exterior.coords)[:-1]  # Remove duplicate last point
        else:
            gcode.append("(WARNING: Pocket offset resulted in invalid geometry)")
            return gcode

        # Use pocket centroid as entry position (center of pocket)
        entry_x = offset_poly.centroid.x
        entry_y = offset_poly.centroid.y

        # Detect if pocket is circular
        is_circular = self._is_pocket_circular(pocket_points)

        # Calculate helical entry parameters
        helix_radius = self.tool_diameter * 0.75  # Small helix for entry
        ramp_start_height = self.material_top + self.ramp_start_clearance
        num_helical_passes, depth_per_pass = self._calculate_helical_passes(helix_radius, ramp_start_height=ramp_start_height)

        gcode.append(f"(Pocket with helical entry at center: {num_helical_passes} passes at {self.ramp_angle}°)")

        # Position at pocket center
        gcode.append(f"G1 X{entry_x:.4f} Y{entry_y:.4f} F{self.traverse_rate}  ; Position at pocket center")
        gcode.append(f"G1 Z{ramp_start_height:.4f} F{self.approach_rate}  ; Approach to ramp start height")

        # Helical entry at center
        start_x = entry_x + helix_radius
        start_y = entry_y
        gcode.append(f"G1 X{start_x:.4f} Y{start_y:.4f} F{self.traverse_rate}  ; Move to helix start (above material)")

        for pass_num in range(num_helical_passes):
            target_z = ramp_start_height - (pass_num + 1) * depth_per_pass
            gcode.append(f"G3 X{start_x:.4f} Y{start_y:.4f} I{-helix_radius:.4f} J0 Z{target_z:.4f} F{self.ramp_feed_rate}  ; Helical pass {pass_num + 1}/{num_helical_passes} CCW for climb milling")

        # Return to center after helix
        gcode.append(f"G1 X{entry_x:.4f} Y{entry_y:.4f} F{self.feed_rate}  ; Return to pocket center")

        # Spiral outward from center to perimeter
        # Calculate maximum distance from center to any perimeter point
        max_radius = 0
        for point in offset_points:
            dist = self._distance_2d(point, (entry_x, entry_y))
            max_radius = max(max_radius, dist)

        # Calculate spiral passes (similar to hole clearing)
        stepover = self.tool_diameter * self.stepover_percentage
        num_passes = max(1, int(math.ceil(max_radius / stepover)))

        if is_circular:
            # CIRCULAR POCKET: Use efficient Archimedean spiral
            gcode.append(f"(Circular pocket detected - using Archimedean spiral clearing)")

            # Calculate inscribed radius (minimum distance from center to any EDGE)
            # This prevents circular spiral from cutting outside pocket sides
            from shapely.geometry import LineString, Point
            center_point = Point(entry_x, entry_y)

            min_edge_distance = float('inf')
            for i in range(len(offset_points)):
                # Create line segment for each edge
                p1 = offset_points[i]
                p2 = offset_points[(i + 1) % len(offset_points)]
                edge = LineString([p1, p2])

                # Calculate perpendicular distance from center to this edge
                edge_dist = center_point.distance(edge)
                min_edge_distance = min(min_edge_distance, edge_dist)

            # Use inscribed radius (not circumscribed radius to vertices!)
            max_radius = min_edge_distance

            # Archimedean spiral: r = b*θ where b = stepover/(2π)
            spiral_constant = stepover / (2 * math.pi)

            # Calculate total angle needed to reach max_radius
            if spiral_constant > 0:
                total_angle = max_radius / spiral_constant
            else:
                total_angle = 0

            # Generate spiral points
            angle_increment = math.radians(10)  # 10 degrees per segment
            num_points = int(math.ceil(total_angle / angle_increment))

            gcode.append(f"(Archimedean spiral: {num_points} points to radius {max_radius:.4f}\")")

            # Cut continuous spiral from center to max_radius
            # Use positive angle for counter-clockwise spiral (climb milling on inside feature)
            for i in range(num_points):
                current_angle = i * angle_increment  # Positive for counter-clockwise
                current_radius = spiral_constant * current_angle

                # Convert polar coordinates to Cartesian
                x = entry_x + current_radius * math.cos(current_angle)
                y = entry_y + current_radius * math.sin(current_angle)

                gcode.append(f"G1 X{x:.4f} Y{y:.4f} F{self.feed_rate}")

        else:
            # NON-CIRCULAR POCKET: Use contour-parallel offset clearing
            gcode.append(f"(Non-circular pocket detected - using contour-parallel clearing)")

            # Generate inward offsets from perimeter to center
            current_offset_distance = -self.tool_radius  # Start from tool-compensated perimeter
            contours = []

            # Calculate how many offset passes we need
            # Find the maximum inset we can do (when pocket becomes too small)
            test_offset = current_offset_distance
            while True:
                test_offset -= stepover
                test_poly = pocket_poly.buffer(test_offset)
                if test_poly.is_empty or test_poly.area < 0.001:
                    break
                if not hasattr(test_poly, 'exterior'):
                    break
                contours.append(test_poly)

            gcode.append(f"(Contour-parallel clearing: {len(contours)} offset passes)")

            # Cut contours from outside-in (perimeter to center)
            for idx, contour_poly in enumerate(reversed(contours)):
                if hasattr(contour_poly, 'exterior'):
                    contour_points = list(contour_poly.exterior.coords)[:-1]

                    gcode.append(f"(Contour pass {idx + 1}/{len(contours)})")

                    # Move to start of contour
                    gcode.append(f"G1 X{contour_points[0][0]:.4f} Y{contour_points[0][1]:.4f} F{self.feed_rate}")

                    # Cut the contour
                    for point in contour_points[1:]:
                        gcode.append(f"G1 X{point[0]:.4f} Y{point[1]:.4f} F{self.feed_rate}")

                    # Close the contour
                    gcode.append(f"G1 X{contour_points[0][0]:.4f} Y{contour_points[0][1]:.4f} F{self.feed_rate}")

                    # Return to center between passes for safety
                    gcode.append(f"G1 X{entry_x:.4f} Y{entry_y:.4f} F{self.feed_rate}")

        # Final pass - cut actual perimeter at exact size
        gcode.append(f"(Final pass: cut exact perimeter)")
        gcode.append(f"G1 X{offset_points[0][0]:.4f} Y{offset_points[0][1]:.4f} F{self.feed_rate}")
        for point in offset_points[1:]:
            gcode.append(f"G1 X{point[0]:.4f} Y{point[1]:.4f} F{self.feed_rate}")
        gcode.append(f"G1 X{offset_points[0][0]:.4f} Y{offset_points[0][1]:.4f} F{self.feed_rate}  ; Close pocket")

        # Retract
        gcode.append(f"G0 Z{self.safe_height:.4f}  ; Retract")

        return gcode
    
    def _generate_perimeter_gcode(self, perimeter_points: List[Tuple[float, float]]) -> List[str]:
        """Generate G-code for perimeter with tabs and tool compensation (offset outward)"""
        gcode = []

        # Create offset path (outward by tool radius)
        perimeter_poly = Polygon(perimeter_points)
        
        # Buffer outward (positive buffer) 
        offset_poly = perimeter_poly.buffer(self.tool_radius)
        
        if offset_poly.is_empty:
            gcode.append(f"(WARNING: Perimeter offset failed)")
            return gcode
        
        # Get the boundary of the offset polygon
        if hasattr(offset_poly, 'exterior'):
            offset_points = list(offset_poly.exterior.coords)[:-1]  # Remove duplicate last point
            # Reverse points for clockwise direction (climb milling on outside features)
            offset_points = offset_points[::-1]
        else:
            gcode.append("(WARNING: Perimeter offset resulted in invalid geometry)")
            return gcode
        
        # Calculate segment lengths
        segment_lengths = []
        for i in range(len(offset_points)):
            p1 = offset_points[i]
            p2 = offset_points[(i + 1) % len(offset_points)]
            length = self._distance_2d(p1, p2)
            segment_lengths.append(length)

        # Calculate total perimeter length
        perimeter_length = sum(segment_lengths)

        # Calculate ramp start height (close to material surface)
        ramp_start_height = self.material_top + self.ramp_start_clearance

        # Calculate ramp-in distance using material-specific ramp angle
        ramp_depth = ramp_start_height - self.cut_depth
        ramp_distance = ramp_depth / math.tan(math.radians(self.ramp_angle))
        gcode.append(f"(Ramp-in: {ramp_distance:.4f}\" at {self.ramp_angle}°)")

        # Calculate tab zones (start/end distances) - evenly spaced in cutting section (after ramp)
        # We cut from ramp_distance to perimeter_length, so tabs should only be in that range
        cutting_length = perimeter_length - ramp_distance
        tab_spacing = cutting_length / self.num_tabs
        tab_zones = []  # List of (start_dist, end_dist) tuples

        # Place tabs starting after the ramp, centered in each section
        half_tab_width = self.tab_width / 2
        for i in range(self.num_tabs):
            tab_center = ramp_distance + tab_spacing * (i + 0.5)
            tab_start = tab_center - half_tab_width
            tab_end = tab_center + half_tab_width
            tab_zones.append((tab_start, tab_end))

        gcode.append(f"(Tabs: {len(tab_zones)} evenly spaced in cutting section, {self.tab_width:.4f}\" wide)")

        # Move to start
        start = offset_points[0]
        gcode.append(f"G1 X{start[0]:.4f} Y{start[1]:.4f} F{self.traverse_rate}  ; Move to perimeter start")
        gcode.append(f"G1 Z{ramp_start_height:.4f} F{self.approach_rate}  ; Approach to ramp start height")

        # Ramp in along the perimeter path
        # Calculate points along perimeter for ramping
        ramp_points = []
        current_ramp_dist = 0
        current_z = ramp_start_height
        ramp_end_segment = 0  # Track which segment the ramp ends on

        for i in range(len(offset_points)):
            p1 = offset_points[i]
            p2 = offset_points[(i + 1) % len(offset_points)]
            seg_len = segment_lengths[i]

            if current_ramp_dist >= ramp_distance:
                break  # Ramp complete

            if current_ramp_dist + seg_len <= ramp_distance:
                # Entire segment is part of ramp
                z_at_end = ramp_start_height - (current_ramp_dist + seg_len) / ramp_distance * ramp_depth
                ramp_points.append((p2[0], p2[1], z_at_end))
                current_ramp_dist += seg_len
                ramp_end_segment = i + 1  # Ramp ends at the end of this segment
            else:
                # Partial segment - ramp ends partway through
                remaining_ramp = ramp_distance - current_ramp_dist
                t = remaining_ramp / seg_len
                final_x = p1[0] + t * (p2[0] - p1[0])
                final_y = p1[1] + t * (p2[1] - p1[1])
                ramp_points.append((final_x, final_y, self.cut_depth))
                current_ramp_dist = ramp_distance
                ramp_end_segment = i  # Ramp ends partway through this segment
                break

        # Execute ramp moves using ramp feed rate
        for i, (x, y, z) in enumerate(ramp_points):
            gcode.append(f"G1 X{x:.4f} Y{y:.4f} Z{z:.4f} F{self.ramp_feed_rate}  ; Ramp segment {i+1}")

        # Ensure we're at full depth
        if current_ramp_dist < ramp_distance:
            # Calculate remaining depth to descend
            if ramp_points:
                current_pos = ramp_points[-1]
                current_z = current_pos[2]
                remaining_depth = current_z - self.cut_depth

                if remaining_depth > 0.001:  # Only if significant depth remains
                    # Use small helical loop instead of straight plunge
                    helix_radius = self.tool_diameter * 0.75  # Small radius, safe for any geometry
                    helix_center_x = current_pos[0]
                    helix_center_y = current_pos[1]

                    # Calculate number of helical loops needed
                    circumference = 2 * math.pi * helix_radius
                    depth_per_loop = circumference * math.tan(math.radians(self.ramp_angle))
                    num_loops = max(1, int(math.ceil(remaining_depth / depth_per_loop)))
                    depth_per_loop_actual = remaining_depth / num_loops

                    gcode.append(f"(Perimeter too short - using helical finish: {num_loops} loop(s) at {self.ramp_angle}°)")

                    # Move to edge of helix radius
                    start_x = helix_center_x + helix_radius
                    start_y = helix_center_y
                    gcode.append(f"G1 X{start_x:.4f} Y{start_y:.4f} F{self.feed_rate}  ; Move to helix start")

                    # Perform helical loops
                    for loop_num in range(num_loops):
                        target_z = current_z - (loop_num + 1) * depth_per_loop_actual
                        gcode.append(f"G3 X{start_x:.4f} Y{start_y:.4f} I{-helix_radius:.4f} J0 Z{target_z:.4f} F{self.ramp_feed_rate}  ; Helical loop {loop_num + 1}/{num_loops} CCW for climb milling")

                    # Return to perimeter path
                    gcode.append(f"G1 X{helix_center_x:.4f} Y{helix_center_y:.4f} F{self.feed_rate}  ; Return to perimeter")

        gcode.append("")

        # Cut around perimeter with tabs, starting from where ramp ended
        # Use segment-centric approach: check each segment against tab zones
        current_distance = current_ramp_dist
        tab_z = self.cut_depth + self.tab_height
        tab_number = 0
        current_z = self.cut_depth  # Track current Z height to avoid unnecessary moves

        # Create perimeter points list starting from where ramp ended
        # Continue from ramp_end_segment to end, then wrap around to start
        remaining_points = offset_points[ramp_end_segment:] + offset_points[:ramp_end_segment]
        remaining_lengths = segment_lengths[ramp_end_segment:] + segment_lengths[:ramp_end_segment]

        # Helper function to process a segment with tab checking
        def process_segment(p1, p2, seg_start_dist, seg_length):
            nonlocal tab_number, current_z

            if seg_length == 0:
                return

            seg_end_dist = seg_start_dist + seg_length

            # Find all tab zones that intersect this segment
            intersecting_tabs = []
            for tab_idx, (tab_start, tab_end) in enumerate(tab_zones):
                # Check if tab zone overlaps with segment
                if tab_start < seg_end_dist and tab_end > seg_start_dist:
                    # Clamp to segment boundaries
                    overlap_start = max(tab_start, seg_start_dist)
                    overlap_end = min(tab_end, seg_end_dist)
                    intersecting_tabs.append((overlap_start, overlap_end, tab_idx))

            if not intersecting_tabs:
                # No tabs in this segment - ensure we're at cut depth, then cut normally
                if current_z != self.cut_depth:
                    gcode.append(f"G1 Z{self.cut_depth:.4f} F{self.plunge_rate}")
                    current_z = self.cut_depth
                gcode.append(f"G1 X{p2[0]:.4f} Y{p2[1]:.4f} F{self.feed_rate}")
                return

            # Segment has tabs - split it into subsegments
            # Sort intersecting tabs by start distance
            intersecting_tabs.sort(key=lambda x: x[0])

            # Build list of subsegments: [(start_dist, end_dist, is_tab), ...]
            subsegments = []
            current_pos = seg_start_dist

            for overlap_start, overlap_end, tab_idx in intersecting_tabs:
                # Add pre-tab segment if there's a gap
                if current_pos < overlap_start:
                    subsegments.append((current_pos, overlap_start, False, -1))

                # Add tab segment
                subsegments.append((overlap_start, overlap_end, True, tab_idx))
                current_pos = overlap_end

            # Add post-tab segment if there's remaining length
            if current_pos < seg_end_dist:
                subsegments.append((current_pos, seg_end_dist, False, -1))

            # Process each subsegment
            for sub_start, sub_end, is_tab, tab_idx in subsegments:
                # Calculate XY position at subsegment end
                t_end = (sub_end - seg_start_dist) / seg_length
                end_x = p1[0] + t_end * (p2[0] - p1[0])
                end_y = p1[1] + t_end * (p2[1] - p1[1])

                if is_tab:
                    # Calculate XY position at subsegment start
                    t_start = (sub_start - seg_start_dist) / seg_length
                    start_x = p1[0] + t_start * (p2[0] - p1[0])
                    start_y = p1[1] + t_start * (p2[1] - p1[1])

                    # Move to tab start in XY
                    gcode.append(f"G1 X{start_x:.4f} Y{start_y:.4f} F{self.feed_rate}")

                    # Raise Z only if not already at tab height
                    if current_z != tab_z:
                        tab_number += 1
                        gcode.append(f"G1 Z{tab_z:.4f} F{self.plunge_rate}  ; Tab {tab_number} start")
                        current_z = tab_z

                    # Move across tab (at tab height)
                    gcode.append(f"G1 X{end_x:.4f} Y{end_y:.4f} F{self.feed_rate}")
                else:
                    # Lower Z only if not already at cut depth
                    if current_z != self.cut_depth:
                        gcode.append(f"G1 Z{self.cut_depth:.4f} F{self.plunge_rate}  ; Tab end")
                        current_z = self.cut_depth

                    # Normal cutting move (at cut depth)
                    gcode.append(f"G1 X{end_x:.4f} Y{end_y:.4f} F{self.feed_rate}")

        # Process all segments from where ramp ended to closing
        for i in range(len(remaining_points) - 1):
            p1 = remaining_points[i]
            p2 = remaining_points[i + 1]
            seg_length = remaining_lengths[i]

            process_segment(p1, p2, current_distance, seg_length)
            current_distance += seg_length

        # Close the perimeter by returning to where ramp ended
        if ramp_points:
            ramp_end_x, ramp_end_y, _ = ramp_points[-1]
            last_point = remaining_points[-1]

            # Calculate closing segment
            closing_length = self._distance_2d((ramp_end_x, ramp_end_y), last_point)

            # Process closing segment
            process_segment(last_point, (ramp_end_x, ramp_end_y), current_distance, closing_length)

        # Retract
        gcode.append(f"G0 Z{self.safe_height:.4f}  ; Retract")

        return gcode

    def _adjust_y_coordinate(self, line: str, y_offset: float) -> str:
        """
        Adjust Y coordinate in a G-code line by adding offset.

        Handles formats: Y-0.1234, Y0.5678, Y-0.1234 (with other coords)

        Args:
            line: G-code line to modify
            y_offset: Offset to add to Y coordinate

        Returns:
            Modified G-code line with adjusted Y coordinate
        """
        def replace_y(match):
            y_val = float(match.group(1))
            new_y = y_val + y_offset
            return f'Y{new_y:.4f}'

        # Match Y followed by optional minus and digits
        return re.sub(r'Y(-?\d+\.?\d*)', replace_y, line)

    def _parse_tube_size(self, tube_size: str) -> tuple[float, float]:
        """
        Parse tube size string to width and height dimensions.

        Args:
            tube_size: Size string like '1x1', '2x1-standing', '2x1-flat', '1.5x1.5', '2x2'

        Returns:
            (width, height) tuple in inches
        """
        if tube_size == '1x1':
            return (1.0, 1.0)
        elif tube_size == '2x1-standing':
            return (1.0, 2.0)  # Standing: narrow width, tall height
        elif tube_size == '2x1-flat':
            return (2.0, 1.0)  # Flat: wide width, short height
        elif tube_size == '1.5x1.5':
            return (1.5, 1.5)
        elif tube_size == '2x2':
            return (2.0, 2.0)
        else:
            # Default to 1x1 if unknown
            return (1.0, 1.0)

    def _scale_tube_facing_toolpath(self, tube_width: float, tube_height: float) -> list[str]:
        """
        Scale the Fusion 360 reference toolpath (1x1 tube) to match actual tube dimensions.
        Also replaces feed rates with material-specific values.

        Args:
            tube_width: Target tube width in inches
            tube_height: Target tube height in inches

        Returns:
            List of scaled G-code lines
        """
        from tube_facing_toolpath import TUBE_FACING_TOOLPATH_1X1

        # Scale factors (reference is 1x1 tube)
        x_scale = tube_width / 1.0
        z_scale = tube_height / 1.0

        scaled_lines = []

        for line in TUBE_FACING_TOOLPATH_1X1.split('\n'):
            line = line.strip()
            if not line:
                continue

            # Parse and scale coordinates
            def scale_coord(match):
                axis = match.group(1)
                value = float(match.group(2))

                if axis == 'X' or axis == 'I':
                    # Scale X and I (X-axis arc offsets)
                    scaled = value * x_scale
                elif axis == 'Z' or axis == 'K':
                    # Scale Z and K (Z-axis arc offsets)
                    scaled = value * z_scale
                else:
                    # Y and J stay the same (depth into material)
                    scaled = value

                return f'{axis}{scaled:.4f}'

            # Replace all coordinate values
            scaled_line = re.sub(r'([XYZIJK])(-?\d+\.?\d*)', scale_coord, line)

            # Replace feed rates with material-specific values
            # F75 (plunge/ramp) -> use ramp_feed_rate
            # F100 (plunge) -> use plunge_rate
            # F24 (slow arc) -> use feed_rate * 0.5
            # No F code -> leave as-is
            if 'F75' in scaled_line:
                scaled_line = re.sub(r'F75\.?', f'F{self.ramp_feed_rate:.1f}', scaled_line)
            elif 'F100' in scaled_line:
                scaled_line = re.sub(r'F100\.?', f'F{self.plunge_rate:.1f}', scaled_line)
            elif 'F24' in scaled_line:
                scaled_line = re.sub(r'F24\.?', f'F{self.feed_rate * 0.5:.1f}', scaled_line)

            scaled_lines.append(scaled_line)

        return scaled_lines

    def _generate_tube_facing_toolpath(self, tube_width: float, tube_height: float,
                                       tool_radius: float, stepover: float,
                                       stepdown: float, facing_depth: float,
                                       finish_allowance: float) -> list[str]:
        """
        Generate complete tube facing toolpath by scaling Fusion 360 reference toolpath.

        The reference toolpath is from Fusion 360 for a 1x1 tube. We scale it to match
        the actual tube dimensions. Other parameters are unused but kept for API compatibility.

        Args:
            tube_width: Width of tube (X dimension) in inches
            tube_height: Height of tube (Z dimension) in inches
            tool_radius: Unused (toolpath has its own tool compensation)
            stepover: Unused
            stepdown: Unused
            facing_depth: Unused
            finish_allowance: Unused

        Returns:
            List of G-code lines for the facing operation
        """
        return self._scale_tube_facing_toolpath(tube_width, tube_height)

    def _generate_roughing_passes(self, *args, **kwargs):
        """Deprecated - kept for compatibility. Use _generate_tube_facing_toolpath instead."""
        return []

    def _generate_finishing_pass(self, *args, **kwargs):
        """Deprecated - kept for compatibility. Use _generate_tube_facing_toolpath instead."""
        return []


    def generate_tube_facing_gcode(self, output_file: str, tube_size: str = '1x1'):
        """
        Generate G-code for tube facing operation with parameterized tube dimensions.

        Strategy:
        - Roughing passes: Zigzag pocketing at multiple Z depths with helical ramping
        - Finishing pass: Profile around tube perimeter with proper lead-in/lead-out
        - Phase 1: Face first half (Y=-0.125 to Y=+0.125)
        - Pause for flip (M0)
        - Phase 2: Face second half (Y=-0.25 to Y=0)

        Args:
            output_file: Path to output G-code file
            tube_size: Size of tube ('1x1', '2x1-standing', '2x1-flat')
        """
        # Parse tube dimensions
        tube_width, tube_height = self._parse_tube_size(tube_size)

        # Calculate toolpath parameters
        tool_radius = self.tool_diameter / 2.0
        stepover = self.tool_diameter * 0.4  # 40% stepover for roughing
        stepdown = 0.05  # Conservative Z stepdown
        facing_depth = 0.25  # How much material to remove
        finish_allowance = 0.01  # Leave this much for finish pass

        # Generate complete facing toolpath for one half
        toolpath_lines = self._generate_tube_facing_toolpath(
            tube_width, tube_height, tool_radius, stepover,
            stepdown, facing_depth, finish_allowance
        )

        # Y offsets for each pass
        # The toolpath's finishing cut is at Y=-tool_radius, which
        # places the tube face at Y=0 after tool compensation.
        # Pass 1: Shift +0.125" so tube face ends at Y=+0.125"
        # Pass 2: Shift -0.125" so tube face ends at Y=-0.125"
        # After flip, total material removed = 0.250", both ends squared
        pass1_y_offset = 0.125
        pass2_y_offset = -0.125

        gcode = []

        # Generate timestamp in Pacific time
        pacific_time = datetime.datetime.now(ZoneInfo("America/Los_Angeles"))
        timestamp = pacific_time.strftime("%Y-%m-%d %H:%M")

        # === HEADER ===
        gcode.append('( PENGUINCAM TUBE FACING OPERATION )')
        gcode.append(f'( Generated: {timestamp} )')
        gcode.append(f'( Tube size: {tube_size} )')
        gcode.append(f'( Tool: {self.tool_diameter:.3f}" end mill )')
        gcode.append('( )')
        gcode.append('( SETUP INSTRUCTIONS: )')
        gcode.append('( 1. Mount tube in jig with end facing user )')
        gcode.append('( 2. Verify G55 is set to jig origin )')
        gcode.append('( 3. Z=0 is at bottom of tube (jig surface) )')
        gcode.append('( 4. Y=0 is at nominal end face of tube )')
        gcode.append('( )')

        # === INITIALIZATION ===
        gcode.append('')
        gcode.append('( === INITIALIZATION === )')
        gcode.append('G90 G94 G91.1 G40 G49 G17')
        gcode.append('G20')
        gcode.append('G0 G28 G91 Z0.  ; Home Z axis at rapid speed')
        gcode.append('G90  ; Back to absolute mode')
        gcode.append('')
        gcode.append('( Tool and spindle )')
        gcode.append('T1 M6')
        gcode.append(f'S{self.spindle_speed} M3')
        gcode.append('G4 P3.0')
        gcode.append('')
        gcode.append('G55  ; Use jig work coordinate system')
        gcode.append('')

        # === PHASE 1: FACE FIRST HALF ===
        gcode.append('( === PHASE 1: FACE FIRST HALF === )')
        gcode.append('( Face from Y=-0.125 to Y=+0.125 )')
        gcode.append('')
        gcode.append('G53 G0 Z0.  ; Move to machine Z0 - safe clearance')
        gcode.append('G0 X0 Y0  ; Rapid to work origin')
        gcode.append('')

        # Add toolpath with Pass 1 Y offset
        for line in toolpath_lines:
            line = line.strip()
            if line and not line.startswith('G52'):
                adjusted_line = self._adjust_y_coordinate(line, pass1_y_offset)
                gcode.append(adjusted_line)

        # === PAUSE FOR FLIP ===
        gcode.append('')
        gcode.append('( === PAUSE FOR TUBE FLIP === )')
        gcode.append('G53 G0 Z0.  ; Move to machine Z0 - safe clearance')
        gcode.append('G53 G0 X0.5 Y23.5  ; Park at back of machine')
        gcode.append('M5')
        gcode.append('G4 P5.0')
        gcode.append('')
        gcode.append('( *** OPERATOR ACTION REQUIRED *** )')
        gcode.append('( Flip tube 180 degrees end-for-end )')
        gcode.append('( Re-clamp tube in jig )')
        gcode.append('( Press CYCLE START to continue )')
        gcode.append('M0')
        gcode.append('')

        # === PHASE 2: FACE SECOND HALF ===
        gcode.append('( === PHASE 2: FACE SECOND HALF === )')
        gcode.append('( Face from Y=-0.250 to Y=-0.125 )')
        gcode.append('')
        gcode.append(f'S{self.spindle_speed} M3')
        gcode.append('G4 P3.0')
        gcode.append('')
        gcode.append('G53 G0 Z0.  ; Move to machine Z0 - safe clearance')
        gcode.append('G0 X0 Y0  ; Rapid to work origin')
        gcode.append('')

        # Add toolpath with Pass 2 Y offset
        for line in toolpath_lines:
            line = line.strip()
            if line and not line.startswith('G52'):
                adjusted_line = self._adjust_y_coordinate(line, pass2_y_offset)
                gcode.append(adjusted_line)

        # === END ===
        gcode.append('')
        gcode.append('( === PROGRAM END === )')
        gcode.append('G53 G0 Z0.  ; Move to machine Z0 (safe clearance)')
        gcode.append('G53 G0 X0.5 Y23.5  ; Park at back of machine')
        gcode.append('M5')
        gcode.append('M30')

        # Write to file
        with open(output_file, 'w') as f:
            f.write('\n'.join(gcode))

        print(f'OUTPUT_FILE:{output_file}')
        print(f'Tube facing G-code generated for {tube_size} tube')

        # Print stats for UI
        print(f"\nIdentified 0 millable holes and 0 pockets")
        print(f"Total lines: {len(gcode)}")

        # Estimate cycle time
        time_estimate = self._estimate_cycle_time(gcode)
        print(f"\n⏱️  ESTIMATED_CYCLE_TIME: {time_estimate['total']:.1f} seconds ({self._format_time(time_estimate['total'])})")

        print(f'\nSETUP:')
        print(f'  1. Mount tube in jig with end facing spindle')
        print(f'  2. Verify G55 is set to jig origin')
        print(f'  3. Z=0 is at bottom of tube (jig surface)')
        print(f'  4. Y=0 is at nominal end face of tube')
        print(f'\nOPERATION:')
        print(f'  Phase 1: Face first half of tube end')
        print(f'  -- Program pauses (M0) for tube flip --')
        print(f'  Phase 2: Face second half of tube end')

    def generate_tube_pattern_gcode(self, output_file: str, tube_height: float,
                                   square_end: bool, cut_to_length: bool,
                                   tube_width: float = None, tube_length: float = None):
        """
        Generate G-code for machining DXF pattern on both faces of a tube.

        The tube sits in a jig with the end facing the spindle. This method:
        1. Optionally squares the tube end (if square_end=True)
        2. Machines the DXF pattern on the first face
        3. Pauses (M0) for the operator to flip the tube 180° around Y-axis
        4. Machines the DXF pattern on the second face (Y-mirrored)
        5. Optionally machines tube to length (if cut_to_length=True - stub)

        The jig uses G55 work coordinate system with:
        - Origin at bottom-left corner of tube face
        - X-axis along tube width
        - Y-axis pointing away from spindle (into tube)
        - Z-axis along tube height (vertical)

        Args:
            output_file: Path to output G-code file
            tube_height: Height of tube in Z direction (inches)
            square_end: Whether to square the tube end before machining pattern
            cut_to_length: Whether to cut tube to length after pattern (stub)
            tube_width: Width of tube face (X dimension) in inches (optional, calculated from DXF if not provided)
            tube_length: Length of tube face (Y dimension) in inches (optional, for future use)
        """
        gcode = []

        # Generate timestamp
        pacific_time = datetime.datetime.now(ZoneInfo("America/Los_Angeles"))
        timestamp = pacific_time.strftime("%Y-%m-%d %H:%M")

        # === HEADER ===
        gcode.append('( PENGUINCAM TUBE PATTERN OPERATION )')
        gcode.append(f'( Generated: {timestamp} )')
        if hasattr(self, 'user_name') and self.user_name:
            gcode.append(f'( User: {self.user_name} )')
        gcode.append(f'( Tube height: {tube_height:.3f}" )')
        gcode.append(f'( Tool: {self.tool_diameter:.3f}" end mill )')
        gcode.append(f'( Material: {self.spindle_speed} RPM, {self.feed_rate:.1f} ipm )')
        gcode.append('( )')
        gcode.append('( SETUP INSTRUCTIONS: )')
        gcode.append('( 1. Mount tube in jig with end facing spindle )')
        gcode.append('( 2. Jig uses G55 work coordinate system [fixed position] )')
        gcode.append('( 3. G55 origin is at bottom-left corner of tube face )')
        gcode.append('( 4. X = tube width, Y = into tube, Z = tube height )')
        gcode.append('( )')

        # === INITIALIZATION ===
        gcode.append('')
        gcode.append('( === INITIALIZATION === )')
        gcode.append('G90 G94 G91.1 G40 G49 G17')
        gcode.append('G20')
        gcode.append('G0 G28 G91 Z0.  ; Home Z axis')
        gcode.append('G90  ; Back to absolute mode')
        gcode.append('')
        gcode.append('( Tool and spindle )')
        gcode.append('T1 M6')
        gcode.append(f'S{self.spindle_speed} M3')
        gcode.append('G4 P3.0')
        gcode.append('')
        gcode.append('G55  ; Use jig work coordinate system')
        gcode.append('')

        # Determine tube width for facing operations
        if tube_width is None:
            # Calculate from DXF geometry if not provided
            tube_width = 1.0  # Default
            if square_end:
                all_x_coords = []
                if hasattr(self, 'holes'):
                    for hole in self.holes:
                        all_x_coords.append(hole['center'][0])
                if hasattr(self, 'pockets'):
                    for pocket in self.pockets:
                        all_x_coords.extend([p[0] for p in pocket])
                if hasattr(self, 'perimeter') and self.perimeter:
                    all_x_coords.extend([p[0] for p in self.perimeter])

                if all_x_coords:
                    calculated_width = max(all_x_coords) - min(all_x_coords)
                    if calculated_width > 0.1:  # Only use if reasonable
                        tube_width = calculated_width

        # === PHASE 1: FIRST FACE (SQUARE + MACHINE PATTERN) ===
        gcode.append('( === PHASE 1: FIRST FACE === )')
        gcode.append('')

        # Square the end first (if requested)
        if square_end:
            gcode.append('( Square tube end )')
            tool_radius = self.tool_diameter / 2.0
            stepover = self.tool_diameter * 0.4
            stepdown = 0.05
            facing_depth = 0.25
            finish_allowance = 0.01

            facing_toolpath = self._generate_tube_facing_toolpath(
                tube_width, tube_height, tool_radius, stepover,
                stepdown, facing_depth, finish_allowance
            )

            # First side: face back to leave wall_thickness for second side
            y_offset_phase1 = self.material_thickness
            for line in facing_toolpath:
                adjusted_line = self._adjust_y_coordinate(line, y_offset_phase1)
                gcode.append(adjusted_line)
            gcode.append('')

        # Machine the pattern on this face
        gcode.append('( Machine pattern on first face )')
        gcode.append('( Machining holes and pockets only - perimeter is tube face )')
        z_offset = tube_height - self.material_thickness
        gcode.append(f'( Z offset: +{z_offset:.3f}" [tube_height - wall_thickness] )')
        # Y offset for first face: matches facing offset so holes align with face
        y_offset_first_face = self.material_thickness if square_end else 0.0
        gcode.append(f'( Y offset: +{y_offset_first_face:.3f}" [rough end will be milled back] )')
        gcode.append('')
        gcode.extend(self._generate_toolpath_gcode(skip_perimeter=True, z_offset=z_offset, y_offset=y_offset_first_face))

        # === CUT TO LENGTH - PHASE 1 ===
        if cut_to_length:
            gcode.append('')
            gcode.append('( === CUT TUBE TO LENGTH - PHASE 1 === )')
            cut_gcode = self._generate_cut_to_length(tube_width, tube_height, tube_length, phase=1)
            gcode.extend(cut_gcode)

        # === PAUSE FOR FLIP ===
        gcode.append('')
        gcode.append('( === PAUSE FOR TUBE FLIP === )')
        gcode.append('G53 G0 Z0.  ; Safe height')
        gcode.append('G53 G0 X0.5 Y23.5  ; Park at back')
        gcode.append('M5')
        gcode.append('G4 P5.0')
        gcode.append('')
        gcode.append('( *** OPERATOR ACTION REQUIRED *** )')
        gcode.append('( Flip tube 180 degrees around Y-axis )')
        gcode.append('( Holes will be machined on opposite face )')
        gcode.append('( Press CYCLE START to continue )')
        gcode.append('M0')
        gcode.append('')

        # === PHASE 2: SECOND FACE (SQUARE + MACHINE PATTERN) ===
        gcode.append('( === PHASE 2: SECOND FACE === )')
        gcode.append('')
        gcode.append(f'S{self.spindle_speed} M3')
        gcode.append('G4 P3.0')
        gcode.append('')

        # Square the end first (if requested)
        if square_end:
            gcode.append('( Square tube end )')
            tool_radius = self.tool_diameter / 2.0
            stepover = self.tool_diameter * 0.4
            stepdown = 0.05
            facing_depth = 0.25
            finish_allowance = 0.01

            facing_toolpath = self._generate_tube_facing_toolpath(
                tube_width, tube_height, tool_radius, stepover,
                stepdown, facing_depth, finish_allowance
            )

            # Second side: face to final depth
            y_offset_phase2 = 0.0
            for line in facing_toolpath:
                adjusted_line = self._adjust_y_coordinate(line, y_offset_phase2)
                gcode.append(adjusted_line)
            gcode.append('')

        # Machine the pattern on this face (X-mirrored, Y stays same)
        gcode.append('( Machine pattern on second face - X-mirrored )')
        gcode.append('( Pattern is X-mirrored [tube flipped end-for-end] so holes align opposite )')
        z_offset = tube_height - self.material_thickness
        gcode.append(f'( Z offset: +{z_offset:.3f}" [tube_height - wall_thickness] )')
        gcode.append('( Y coordinates: holes at Y=0, face milled back to expose them )')
        gcode.append('')

        # Mirror X coordinates around tube centerline (tube flipped end-for-end)
        mirrored_toolpath = self._generate_toolpath_gcode_mirrored_x(z_offset=z_offset, tube_width=tube_width)
        gcode.extend(mirrored_toolpath)

        # === CUT TO LENGTH - PHASE 2 ===
        if cut_to_length:
            gcode.append('')
            gcode.append('( === CUT TUBE TO LENGTH - PHASE 2 === )')
            cut_gcode = self._generate_cut_to_length(tube_width, tube_height, tube_length, phase=2)
            gcode.extend(cut_gcode)

        # === END ===
        gcode.append('')
        gcode.append('( === PROGRAM END === )')
        gcode.append('G53 G0 Z0.')
        gcode.append('G53 G0 X0.5 Y23.5')
        gcode.append('M5')
        gcode.append('M30')

        # Write to file
        with open(output_file, 'w') as f:
            f.write('\n'.join(gcode))

        print(f'OUTPUT_FILE:{output_file}')
        print(f'Tube pattern G-code generated')

        # Print stats for UI
        num_holes = len(self.holes) if hasattr(self, 'holes') else 0
        num_pockets = len(self.pockets) if hasattr(self, 'pockets') else 0
        print(f"\nIdentified {num_holes} millable holes and {num_pockets} pockets on each face")
        print(f"Total lines: {len(gcode)}")

        # Estimate cycle time
        time_estimate = self._estimate_cycle_time(gcode)
        print(f"\n⏱️  ESTIMATED_CYCLE_TIME: {time_estimate['total']:.1f} seconds ({self._format_time(time_estimate['total'])})")

        print(f'\nSETUP:')
        print(f'  1. Mount tube in jig with end facing spindle')
        print(f'  2. Verify G55 is set to jig origin')
        print(f'  3. Origin (0,0,0) = bottom-left corner of tube face')
        if square_end:
            print(f'\nOPERATIONS:')
            print(f'  Phase 0: Square tube end')
            print(f'  -- Flip tube end-for-end (M0) --')
            print(f'  Phase 0: Square opposite end')
            print(f'  Phase 1: Machine pattern on first face')
            print(f'  -- Flip tube 180° around Y-axis (M0) --')
            print(f'  Phase 2: Machine pattern on opposite face (mirrored)')
        else:
            print(f'\nOPERATIONS:')
            print(f'  Phase 1: Machine pattern on first face')
            print(f'  -- Flip tube 180° around Y-axis (M0) --')
            print(f'  Phase 2: Machine pattern on opposite face (mirrored)')
        if cut_to_length:
            print(f'  Cut to length: Not yet implemented')

    def _generate_toolpath_gcode(self, skip_perimeter: bool = False, z_offset: float = 0.0, y_offset: float = 0.0) -> list[str]:
        """
        Generate toolpath G-code for the current DXF geometry.

        Args:
            skip_perimeter: If True, skip perimeter cutting (useful for tube faces)
            z_offset: Offset to add to all Z coordinates (for tube mode, shifts to tube face height)
            y_offset: Offset to add to all Y coordinates (for tube first face, accounts for material removal)
        """
        toolpath = []

        # Generate toolpaths for holes
        if hasattr(self, 'holes') and self.holes:
            for hole in self.holes:
                toolpath.extend(self._generate_hole_gcode(
                    hole['center'][0],  # cx
                    hole['center'][1],  # cy
                    hole['diameter']    # diameter
                ))

        # Generate toolpaths for pockets
        if hasattr(self, 'pockets') and self.pockets:
            for pocket in self.pockets:
                toolpath.extend(self._generate_pocket_gcode(pocket))

        # Perimeter (only for standard mode, not tube faces)
        if not skip_perimeter and hasattr(self, 'perimeter') and self.perimeter:
            toolpath.extend(self._generate_perimeter_gcode(self.perimeter))

        # Apply offsets if needed (for tube mode)
        if z_offset != 0.0:
            toolpath = [self._offset_z_coordinate(line, z_offset) for line in toolpath]
        if y_offset != 0.0:
            toolpath = [self._offset_y_coordinate(line, y_offset) for line in toolpath]

        return toolpath

    def _generate_toolpath_gcode_mirrored_x(self, z_offset: float = 0.0, tube_width: float = 1.0) -> list[str]:
        """
        Generate toolpath G-code for mirrored features (second tube face).

        This is used for the second face of tube machining after flipping end-for-end.
        Instead of transforming generated toolpaths (which breaks safety logic),
        we mirror the feature geometry FIRST, then generate fresh toolpaths.

        This preserves all safety features:
        - Helical entry at center
        - Outward Archimedean spiral (gradual material removal)
        - Proper climb milling direction

        When flipping a tube 180° around Y-axis (end-for-end):
        - Feature X coordinates mirror around centerline: X_new = tube_width - X_old
        - Feature Y coordinates stay the same
        - Toolpaths are regenerated from mirrored geometry

        Args:
            z_offset: Offset to add to all Z coordinates (for tube mode)
            tube_width: Width of tube face for mirroring X around centerline
        """
        toolpath = []

        # Generate toolpaths for mirrored holes
        if hasattr(self, 'holes') and self.holes:
            for hole in self.holes:
                # Mirror the hole center around tube centerline
                original_cx = hole['center'][0]
                original_cy = hole['center'][1]
                mirrored_cx = tube_width - original_cx
                mirrored_cy = original_cy  # Y stays the same

                # Generate fresh toolpath for the mirrored hole
                # This preserves helical entry + outward spiral safety
                toolpath.extend(self._generate_hole_gcode(
                    mirrored_cx, mirrored_cy, hole['diameter']
                ))

        # Generate toolpaths for mirrored pockets
        if hasattr(self, 'pockets') and self.pockets:
            for pocket in self.pockets:
                # Mirror all pocket points around tube centerline
                mirrored_pocket = [(tube_width - x, y) for x, y in pocket]
                toolpath.extend(self._generate_pocket_gcode(mirrored_pocket))

        # Perimeter is not machined on tube faces (skip)

        # Apply Z offset if needed (for tube mode)
        if z_offset != 0.0:
            toolpath = [self._offset_z_coordinate(line, z_offset) for line in toolpath]

        return toolpath

    def _mirror_x_coordinate(self, line: str, tube_width: float) -> str:
        """
        Mirror X coordinate in a G-code line around tube centerline.

        When flipping tube end-for-end, X coordinates reflect around the centerline.
        Arc direction (G2/G3) stays the same because the physical cutting conditions
        are unchanged - the spindle is in the same position and tool rotation is the same.

        Transformations:
        - X_new = tube_width - X_old
        - I_new = -I_old (flip X arc offset)
        - J_new = J_old (Y arc offset unchanged)
        - G2/G3 unchanged (arc direction stays the same)

        Args:
            line: G-code line to modify
            tube_width: Width of tube face

        Returns:
            Modified G-code line with mirrored X coordinates
        """
        # Mirror X coordinates
        def replace_x(match):
            x_val = float(match.group(1))
            new_x = tube_width - x_val
            return f'X{new_x:.4f}'
        line = re.sub(r'X(-?\d+\.?\d*)', replace_x, line)

        # Flip I offset sign (X component of arc center)
        def replace_i(match):
            i_val = float(match.group(1))
            new_i = -i_val
            return f'I{new_i:.4f}'
        line = re.sub(r'I(-?\d+\.?\d*)', replace_i, line)

        return line

    def _offset_z_coordinate(self, line: str, z_offset: float) -> str:
        """
        Offset Z coordinate in a G-code line by adding z_offset.

        For standard mode: Z=0 at bottom of plate, toolpath cuts from Z=thickness (top) to Z=0 (bottom).
        For tube mode: Z=0 at bottom of lower face. Upper face bottom is at Z=tube_height-tube_wall_thickness.
        This method shifts Z coordinates by (tube_height - tube_wall_thickness) to position at upper face.

        Args:
            line: G-code line to modify
            z_offset: Offset to add to Z coordinate (typically tube_height - tube_wall_thickness)

        Returns:
            Modified G-code line with offset Z coordinate
        """
        def replace_z(match):
            z_val = float(match.group(1))
            new_z = z_val + z_offset
            return f'Z{new_z:.4f}'

        # Match Z followed by optional minus and digits
        return re.sub(r'Z(-?\d+\.?\d*)', replace_z, line)

    def _offset_y_coordinate(self, line: str, y_offset: float) -> str:
        """
        Offset Y coordinate in a G-code line by adding y_offset.

        For tube mode first face: Y offset accounts for material that will be removed during facing.
        If rough end will be milled back by 0.125", pattern must be positioned 0.125" deeper.

        Args:
            line: G-code line to modify
            y_offset: Offset to add to Y coordinate (typically wall_thickness for first face)

        Returns:
            Modified G-code line with offset Y coordinate
        """
        def replace_y(match):
            y_val = float(match.group(1))
            new_y = y_val + y_offset
            return f'Y{new_y:.4f}'

        return re.sub(r'Y(-?\d+\.?\d*)', replace_y, line)

    def _generate_cut_to_length(self, tube_width: float, tube_height: float,
                                 tube_length: float, phase: int) -> list[str]:
        """
        Generate G-code to cut tube to length.

        Cuts across the width of the tube at Y=tube_length (plus offset for phase 1).
        Makes multiple passes stepping down through the wall thickness, then trims
        the sides down to just past halfway.

        Args:
            tube_width: Width of tube (X dimension)
            tube_height: Height of tube (Z dimension)
            tube_length: Desired tube length (Y dimension)
            phase: 1 (before flip) or 2 (after flip)

        Returns:
            List of G-code lines
        """
        gcode = []

        # Calculate Y position for cut
        if phase == 1:
            # Phase 1: Cut at tube_length + facing offset
            y_cut = tube_length + self.material_thickness
            z_start = tube_height  # Top of tube (tube sits on sacrifice board at Z=0)
            gcode.append(f'( Cut to length at Y={y_cut:.4f}" [Phase 1: before flip] )')
        else:
            # Phase 2: Cut at tube_length
            y_cut = tube_length
            z_start = tube_height  # Top of tube
            gcode.append(f'( Cut to length at Y={y_cut:.4f}" [Phase 2: after flip] )')

        # Cut parameters
        plunge_clearance = 0.03  # Extra clearance to avoid plunging into stock with runout
        x_start = -(self.tool_diameter + plunge_clearance)
        x_end = tube_width + self.tool_diameter + plunge_clearance
        stepdown = 0.0625  # 1/16" per pass

        # Calculate ramp distance using material-specific ramp angle
        ramp_start_height = z_start + self.ramp_start_clearance

        gcode.append(f'( Cutting side-to-side from X={x_start:.4f}" to X={x_end:.4f}" )')
        gcode.append(f'( Using ramp entry at {self.ramp_angle}° angle )')
        gcode.append(f'( Stepdown: {stepdown}" per pass through wall thickness )')
        gcode.append('')

        # Position at start
        gcode.append(f'G0 Z{self.safe_height:.4f}  ; Retract')
        gcode.append(f'G0 X{x_start:.4f} Y{y_cut:.4f}  ; Position at cut start')

        # Cut through the wall thickness with ramping
        current_z = z_start
        pass_num = 1

        while current_z > (z_start - self.material_thickness - 0.01):  # Cut through wall + small margin
            target_z = max(current_z - stepdown, z_start - self.material_thickness - 0.01)
            ramp_depth = ramp_start_height - target_z
            ramp_distance = ramp_depth / math.tan(math.radians(self.ramp_angle))

            # Ensure ramp distance doesn't exceed cut width
            cut_width = x_end - x_start
            if ramp_distance > cut_width:
                # If ramp is too long, use multiple passes or steeper angle
                ramp_distance = cut_width * 0.9  # Use 90% of width for ramp

            gcode.append(f'( Pass {pass_num}: ramping to Z={target_z:.4f}" over {ramp_distance:.4f}" )')

            # Approach above ramp start
            gcode.append(f'G0 Z{ramp_start_height:.4f}  ; Approach to ramp start')

            # Calculate ramp end position
            x_ramp_end = x_start + ramp_distance

            # Ramp down while cutting
            gcode.append(f'G1 X{x_ramp_end:.4f} Z{target_z:.4f} F{self.ramp_feed_rate}  ; Ramp down')

            # Continue cutting to end at full depth
            gcode.append(f'G1 X{x_end:.4f} F{self.feed_rate}  ; Cut to end')

            # Finishing pass: cut back from right to left at full depth
            gcode.append(f'G1 X{x_start:.4f} F{self.feed_rate}  ; Finishing pass (right to left)')

            # Retract
            gcode.append(f'G0 Z{self.safe_height:.4f}  ; Retract')
            gcode.append(f'G0 X{x_start:.4f}  ; Return to start X')

            current_z = target_z
            pass_num += 1

        gcode.append('')

        # Trim sides down to just past halfway
        # Cut through wall thickness on left and right sides
        z_halfway = tube_height / 2.0 - 0.05  # Just past halfway
        gcode.append(f'( Trim sides down to Z={z_halfway:.4f}" [just past halfway] )')
        gcode.append(f'( Cut through wall thickness: {self.material_thickness:.4f}" )')

        # Trim left side (cut through wall from outside to inside)
        x_left_start = -(self.tool_diameter + plunge_clearance)
        x_left_end = self.material_thickness + self.tool_diameter + plunge_clearance
        gcode.append(f'( Trim left side: X from {x_left_start:.4f}" through wall to {x_left_end:.4f}" )')
        gcode.append(f'G0 Y{y_cut:.4f}')
        gcode.append(f'G0 X{x_left_start:.4f}')

        current_z = z_start
        while current_z > z_halfway:
            target_z = max(current_z - stepdown, z_halfway)
            gcode.append(f'G0 Z{target_z + 0.1:.4f}')
            gcode.append(f'G1 Z{target_z:.4f} F{self.plunge_rate}')
            gcode.append(f'G1 X{x_left_end:.4f} F{self.feed_rate}  ; Cut through left wall')
            gcode.append(f'G0 Z{self.safe_height:.4f}')
            gcode.append(f'G0 X{x_left_start:.4f}')
            current_z = target_z

        gcode.append('')

        # Trim right side (mirror of left side, offset by tube_width)
        # Start outside right wall and cut inward through wall thickness
        x_right_start = tube_width + self.tool_diameter + plunge_clearance
        x_right_end = tube_width - self.material_thickness - self.tool_diameter - plunge_clearance
        gcode.append(f'( Trim right side: X from {x_right_start:.4f}" through wall to {x_right_end:.4f}" )')
        gcode.append(f'G0 X{x_right_start:.4f}')

        current_z = z_start
        while current_z > z_halfway:
            target_z = max(current_z - stepdown, z_halfway)
            gcode.append(f'G0 Z{target_z + 0.1:.4f}')
            gcode.append(f'G1 Z{target_z:.4f} F{self.plunge_rate}')
            gcode.append(f'G1 X{x_right_end:.4f} F{self.feed_rate}  ; Cut through right wall')
            gcode.append(f'G0 Z{self.safe_height:.4f}')
            gcode.append(f'G0 X{x_right_start:.4f}')
            current_z = target_z

        gcode.append(f'G0 Z{self.safe_height:.4f}')
        gcode.append('')

        return gcode


def add_timestamp_to_filename(filename: str) -> str:
    """Add Pacific time timestamp to filename before extension."""
    pacific_time = datetime.datetime.now(ZoneInfo("America/Los_Angeles"))
    timestamp = pacific_time.strftime("%Y%m%d_%H%M%S")
    base_name = os.path.splitext(filename)[0]
    extension = os.path.splitext(filename)[1]
    return f"{base_name}_{timestamp}{extension}"


def main():
    parser = argparse.ArgumentParser(description='PenguinCAM - Team 6238 Post-Processor')
    parser.add_argument('input_dxf', nargs='?', help='Input DXF file from OnShape (not needed for tube-facing mode)')
    parser.add_argument('output_gcode', help='Output G-code file')
    parser.add_argument('--mode', type=str, default='standard',
                       choices=['standard', 'tube-facing', 'tube-pattern'],
                       help='Operation mode: standard (DXF processing), tube-facing (square tube ends), or tube-pattern (DXF pattern on tube faces)')
    parser.add_argument('--tube-size', type=str, default='1x1',
                       choices=['1x1', '2x1-standing', '2x1-flat'],
                       help='Tube size for tube-facing mode')
    parser.add_argument('--tube-height', type=float, default=1.0,
                       help='Tube Z-height in inches for tube-pattern mode (default: 1.0)')
    parser.add_argument('--tube-width', type=float,
                       help='Tube face width (X dimension) in inches for tube-pattern mode (optional, calculated from DXF if not provided)')
    parser.add_argument('--tube-length', type=float,
                       help='Tube face length (Y dimension) in inches for tube-pattern mode (optional, calculated from DXF if not provided)')
    parser.add_argument('--square-end', action='store_true',
                       help='Square the tube end before machining pattern (tube-pattern mode)')
    parser.add_argument('--cut-to-length', action='store_true',
                       help='Machine tube to length after pattern (tube-pattern mode - not yet implemented)')
    parser.add_argument('--material', type=str, default='plywood',
                       choices=['plywood', 'aluminum', 'polycarbonate'],
                       help='Material preset (default: plywood) - sets feeds, speeds, and ramp angles')
    parser.add_argument('--thickness', type=float, default=0.25,
                       help='Material thickness in inches (default: 0.25)')
    parser.add_argument('--tool-diameter', type=float, default=0.157,
                       help='Tool diameter in inches (default: 0.157" = 4mm)')
    parser.add_argument('--sacrifice-depth', type=float, default=0.02,
                       help='How far to cut into sacrifice board in inches (default: 0.02")')
    parser.add_argument('--units', choices=['inch', 'mm'], default='inch',
                       help='Units (default: inch)')
    parser.add_argument('--tabs', type=int, default=4,
                       help='Number of tabs on perimeter (default: 4)')
    parser.add_argument('--origin-corner', default='bottom-left',
                       choices=['bottom-left', 'bottom-right', 'top-left', 'top-right'],
                       help='Which corner should be origin (0,0) - default: bottom-left')
    parser.add_argument('--rotation', type=int, default=0,
                       choices=[0, 90, 180, 270],
                       help='Rotation angle in degrees clockwise (default: 0)')
    parser.add_argument('--user', type=str, default=None,
                       help='User name for G-code header (from Google OAuth)')

    # NEW: Cutting parameters
    parser.add_argument('--spindle-speed', type=int, default=18000,
                       help='Spindle speed in RPM (default: 18000)')
    parser.add_argument('--feed-rate', type=float, default=None,
                       help='Feed rate (default: 14 ipm or 365 mm/min depending on units)')
    parser.add_argument('--plunge-rate', type=float, default=None,
                       help='Plunge rate (default: 10 ipm or 339 mm/min depending on units)')
    
    args = parser.parse_args()

    # Mode branching
    if args.mode == 'tube-facing':
        # Tube facing mode - generate G-code for squaring tube ends
        if not args.output_gcode:
            parser.error("output_gcode is required for tube-facing mode")

        pp = FRCPostProcessor(args.thickness, args.tool_diameter)
        pp.apply_material_preset('aluminum')  # Tube facing is always aluminum

        # Add timestamp to output filename
        output_path = add_timestamp_to_filename(args.output_gcode)
        pp.generate_tube_facing_gcode(output_path, args.tube_size)

    elif args.mode == 'tube-pattern':
        # Tube pattern mode - machine DXF pattern on both tube faces
        if not args.input_dxf:
            parser.error("input_dxf is required for tube-pattern mode")
        if not args.output_gcode:
            parser.error("output_gcode is required for tube-pattern mode")

        # Create post-processor with tube WALL thickness (not height!)
        pp = FRCPostProcessor(material_thickness=args.thickness,
                              tool_diameter=args.tool_diameter,
                              units=args.units)

        # Store tube height for Z-offset calculations
        pp.tube_height = args.tube_height

        # Apply material preset and user parameters (shared logic)
        pp.apply_material_preset(args.material)
        if args.user:
            pp.user_name = args.user
        if args.spindle_speed != 18000:
            pp.spindle_speed = args.spindle_speed
        if args.feed_rate is not None:
            pp.feed_rate = args.feed_rate
        if args.plunge_rate is not None:
            pp.plunge_rate = args.plunge_rate

        # Load and process DXF (shared logic)
        pp.load_dxf(args.input_dxf)
        pp.transform_coordinates('bottom-left', args.rotation)  # Tube jig is always bottom-left
        pp.classify_holes()
        pp.identify_perimeter_and_pockets()

        # Debug: Check what was classified
        hole_count = len(pp.holes) if hasattr(pp, 'holes') else 0
        pocket_count = len(pp.pockets) if hasattr(pp, 'pockets') else 0
        has_perimeter = bool(pp.perimeter) if hasattr(pp, 'perimeter') else False
        print(f'DEBUG: Classified {hole_count} holes, {pocket_count} pockets, perimeter={has_perimeter}')

        # Add timestamp to output filename (shared logic)
        output_path = add_timestamp_to_filename(args.output_gcode)

        # Generate tube pattern G-code
        pp.generate_tube_pattern_gcode(output_path, args.tube_height,
                                       args.square_end, args.cut_to_length,
                                       args.tube_width, args.tube_length)

    else:
        # Standard mode - DXF processing
        if not args.input_dxf:
            parser.error("input_dxf is required for standard mode")

        # Create post-processor
        pp = FRCPostProcessor(material_thickness=args.thickness,
                              tool_diameter=args.tool_diameter,
                              units=args.units)

        # Apply material preset and user parameters (shared logic)
        pp.apply_material_preset(args.material)
        if args.user:
            pp.user_name = args.user
        if args.spindle_speed != 18000:
            pp.spindle_speed = args.spindle_speed
        if args.feed_rate is not None:
            pp.feed_rate = args.feed_rate
        if args.plunge_rate is not None:
            pp.plunge_rate = args.plunge_rate

        # Standard mode specific parameters
        pp.num_tabs = args.tabs
        pp.sacrifice_board_depth = args.sacrifice_depth
        pp.cut_depth = -pp.sacrifice_board_depth

        # Load and process DXF (shared logic)
        pp.load_dxf(args.input_dxf)
        pp.transform_coordinates(args.origin_corner, args.rotation)
        pp.classify_holes()
        pp.identify_perimeter_and_pockets()

        # Add timestamp to output filename (shared logic)
        output_path = add_timestamp_to_filename(args.output_gcode)

        pp.generate_gcode(output_path)

        # Print actual output path for GUI to parse (prefixed with OUTPUT_FILE:)
        print(f"OUTPUT_FILE:{output_path}")
        print(f"\nDone! G-code written to: {output_path}")
        print("Review the G-code file before running on your machine.")
        print(f"\nCUTTING PARAMETERS:")
        print(f"  Spindle speed: {pp.spindle_speed} RPM")
        print(f"  Feed rate: {pp.feed_rate:.1f} {args.units}/min")
        print(f"  Plunge rate: {pp.plunge_rate:.1f} {args.units}/min")
        print(f"\nZ-AXIS SETUP:")
        print(f"  ** Zero your Z-axis to the SACRIFICE BOARD surface **")
        print(f"  Material top will be at Z={pp.material_top:.4f}\"")
        print(f"  Cut depth: Z={pp.cut_depth:.4f}\" ({pp.sacrifice_board_depth:.4f}\" into sacrifice board)")
        print(f"  Safe height: Z={pp.safe_height:.4f}\"")
        print(f"\nTool compensation applied:")
        print(f"  Tool diameter: {pp.tool_diameter:.4f}\"")
        print(f"  Tool radius: {pp.tool_radius:.4f}\"")
        print(f"  Perimeter: offset OUTWARD by {pp.tool_radius:.4f}\"")
        print(f"  Pockets: offset INWARD by {pp.tool_radius:.4f}\"")
        print(f"  Holes: toolpath radius reduced by {pp.tool_radius:.4f}\" (holes < {pp.min_millable_hole:.3f}\" skipped)")


if __name__ == '__main__':
    main()
