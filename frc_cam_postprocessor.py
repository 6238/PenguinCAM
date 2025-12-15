#!/usr/bin/env python3
"""
PenguinCAM - FRC Team 6238 CAM Post-Processor
Generates G-code from DXF files with predefined operations for:
- #10 screw holes
- 1.125" bearing holes
- Pockets
- Perimeter with tabs
"""

import ezdxf
from shapely.geometry import Point, Polygon, LineString, MultiPolygon
from shapely.ops import unary_union
import math
from typing import List, Tuple
import argparse


# Material presets based on team 6238 feeds/speeds document
MATERIAL_PRESETS = {
    'plywood': {
        'name': 'Plywood',
        'feed_rate': 75.0,        # Cutting feed rate (IPM)
        'ramp_feed_rate': 50.0,   # Ramp feed rate (IPM)
        'plunge_rate': 20.0,      # Plunge feed rate (IPM) - matches Fusion 360
        'spindle_speed': 18000,   # RPM
        'ramp_angle': 20.0,       # Ramp angle in degrees
        'ramp_start_clearance': 0.150,  # Clearance above material to start ramping (inches)
        'stepover_percentage': 0.65,    # Radial stepover as fraction of tool diameter (65% for plywood)
        'description': 'Standard plywood settings - 18K RPM, 75 IPM cutting'
    },
    'aluminum': {
        'name': 'Aluminum',
        'feed_rate': 55.0,        # Cutting feed rate (IPM)
        'ramp_feed_rate': 35.0,   # Ramp feed rate (IPM)
        'plunge_rate': 35.0,      # Plunge feed rate (IPM) - using ramp rate
        'spindle_speed': 18000,   # RPM
        'ramp_angle': 4.0,        # Ramp angle in degrees
        'ramp_start_clearance': 0.050,  # Clearance above material to start ramping (inches)
        'stepover_percentage': 0.45,    # Radial stepover as fraction of tool diameter (45% conservative for aluminum)
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
        
        # Hole diameters
        self.screw_hole_diameter = 0.19  # #10 screw
        self.bearing_hole_diameter = 1.125  # Bearing hole
        
        # Hole cutting strategy
        self.drill_screw_holes = True  # True = center drill, False = mill out
        
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
        self.plunge_rate = 20.0 if units == "inch" else 508  # Plunge feed rate (IPM or mm/min) - matches Fusion 360
        self.ramp_angle = 20.0  # Ramp angle in degrees (for helical bores and perimeter ramps)
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
        from shapely.geometry import LineString, Point, Polygon
        from shapely.ops import linemerge, unary_union
        import math
        
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
        from collections import defaultdict
        import math
        
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
            return math.sqrt((p1[0] - p2[0])**2 + (p1[1] - p2[1])**2) < tol
        
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
        import math
        
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
        import math
        
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
        self.screw_holes = []
        self.bearing_holes = []
        self.other_holes = []

        for circle in self.circles:
            diameter = circle['diameter']
            center = circle['center']

            # Check if it's a screw hole
            if abs(diameter - self.screw_hole_diameter) < self.tolerance:
                self.screw_holes.append(center)
                print(f"  Screw hole at ({center[0]:.3f}, {center[1]:.3f})")
            # Check if it's a bearing hole
            elif abs(diameter - self.bearing_hole_diameter) < self.tolerance:
                self.bearing_holes.append(center)
                print(f"  Bearing hole at ({center[0]:.3f}, {center[1]:.3f})")
            else:
                self.other_holes.append({'center': center, 'diameter': diameter})
                print(f"  Unknown hole (d={diameter:.3f}) at ({center[0]:.3f}, {center[1]:.3f})")

        print(f"\nClassified: {len(self.screw_holes)} screw holes, "
              f"{len(self.bearing_holes)} bearing holes, "
              f"{len(self.other_holes)} other holes")

        # Sort holes to minimize travel time
        self._sort_holes()

    def _sort_holes(self):
        """
        Sort holes to minimize tool travel time.
        Sorts by X coordinate first, then by Y within each X group (zigzag pattern).
        """
        if len(self.screw_holes) > 1:
            # Sort screw holes by X, then Y
            self.screw_holes.sort(key=lambda p: (round(p[0], 2), p[1]))
            print(f"Sorted {len(self.screw_holes)} screw holes for optimal travel")

        if len(self.bearing_holes) > 1:
            # Sort bearing holes by X, then Y
            self.bearing_holes.sort(key=lambda p: (round(p[0], 2), p[1]))
            print(f"Sorted {len(self.bearing_holes)} bearing holes for optimal travel")
    
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
        
        # Add "other holes" (non-screw, non-bearing circles) as circular pockets
        # These need to be milled out
        for hole in self.other_holes:
            center = hole['center']
            diameter = hole['diameter']
            radius = diameter / 2.0
            
            # Create a circular pocket with 32 points
            num_points = 32
            circle_points = []
            for i in range(num_points):
                angle = 2 * math.pi * i / num_points
                x = center[0] + radius * math.cos(angle)
                y = center[1] + radius * math.sin(angle)
                circle_points.append((x, y))
            
            # Close the circle
            circle_points.append(circle_points[0])
            
            # Add to pockets
            self.pockets.append(circle_points)
        
        print(f"\nIdentified perimeter and {len(self.pockets)} pockets")
        if len(self.other_holes) > 0:
            print(f"  (includes {len(self.other_holes)} circular pockets from non-standard holes)")
    
    def generate_gcode(self, output_file: str):
        """Generate complete G-code file"""
        import datetime
        import json
        import os

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
        from zoneinfo import ZoneInfo
        pacific_time = datetime.datetime.now(ZoneInfo("America/Los_Angeles"))
        timestamp = pacific_time.strftime("%Y-%m-%d %H:%M")

        # Determine operations present
        operations = []
        if self.screw_holes:
            operations.append("Screw Holes")
        if self.bearing_holes:
            operations.append("Bearing Bores")
        if self.pockets:
            operations.append("Pockets")
        if self.perimeter:
            operations.append("Profile")
        operations_str = ", ".join(operations) if operations else "None"

        # Calculate helical entry angle
        import math
        helical_angle = f"~{int(self.ramp_angle)}°"

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
        gcode.append(f"(Tool: Ø{self.tool_diameter}\" Flat End Mill)")
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
        gcode.append("")

        # Screw holes
        if self.screw_holes:
            gcode.append("(===== SCREW HOLES =====)")
            if self.drill_screw_holes:
                gcode.append("(Strategy: Center drill - tool positioned at hole center)")
                for i, (x, y) in enumerate(self.screw_holes, 1):
                    gcode.append(f"(Screw hole {i})")
                    gcode.append(f"G0 X{x:.4f} Y{y:.4f}  ; Position over hole center")
                    gcode.append(f"G0 Z{self.safe_height:.4f}")
                    gcode.append(f"G1 Z{self.cut_depth:.4f} F{self.plunge_rate}  ; Plunge")
                    gcode.append(f"G1 Z{self.safe_height:.4f} F{self.plunge_rate}  ; Retract")
                    gcode.append("")
            else:
                gcode.append("(Strategy: Mill out - helical interpolation with tool compensation)")
                for i, (x, y) in enumerate(self.screw_holes, 1):
                    gcode.append(f"(Screw hole {i})")
                    # Calculate toolpath radius (hole radius minus tool radius)
                    hole_radius = self.screw_hole_diameter / 2
                    toolpath_radius = hole_radius - self.tool_radius

                    if toolpath_radius <= 0:
                        gcode.append(f"(WARNING: Tool too large to mill {self.screw_hole_diameter:.4f}\" hole - switching to center drill)")
                        gcode.append(f"G0 X{x:.4f} Y{y:.4f}  ; Position over hole center")
                        gcode.append(f"G0 Z{self.retract_height:.4f}")
                        gcode.append(f"G1 Z{self.cut_depth:.4f} F{self.plunge_rate}  ; Plunge")
                        gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract")
                    else:
                        # Calculate ramp start height (close to material surface)
                        ramp_start_height = self.material_top + self.ramp_start_clearance

                        # Calculate helical passes using material-specific ramp angle
                        num_passes, depth_per_pass = self._calculate_helical_passes(toolpath_radius, ramp_start_height=ramp_start_height)

                        # Position at edge of toolpath
                        start_x = x + toolpath_radius
                        start_y = y
                        gcode.append(f"(Helical bore: {num_passes} passes at {self.ramp_angle}°, {depth_per_pass:.4f}\" per pass)")
                        gcode.append(f"G0 X{start_x:.4f} Y{start_y:.4f}  ; Position at hole edge")
                        gcode.append(f"G0 Z{ramp_start_height:.4f}  ; Rapid to ramp start height")

                        # Helical plunge in multiple passes using ramp feed rate
                        for pass_num in range(num_passes):
                            target_z = ramp_start_height - (pass_num + 1) * depth_per_pass
                            gcode.append(f"G2 X{start_x:.4f} Y{start_y:.4f} I{-toolpath_radius:.4f} J0 Z{target_z:.4f} F{self.ramp_feed_rate}  ; Helical pass {pass_num + 1}/{num_passes}")

                        # Clean up pass at final depth
                        gcode.append(f"G2 X{start_x:.4f} Y{start_y:.4f} I{-toolpath_radius:.4f} J0 F{self.feed_rate}  ; Clean up pass")

                        # Retract
                        gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract")
                    gcode.append("")
        
        # Bearing holes (spiral out from center)
        if self.bearing_holes:
            gcode.append("(===== BEARING HOLES =====)")
            for i, (x, y) in enumerate(self.bearing_holes, 1):
                gcode.append(f"(Bearing hole {i})")
                gcode.extend(self._generate_bearing_hole_gcode(x, y))
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

    def _generate_bearing_hole_gcode(self, cx: float, cy: float) -> List[str]:
        """
        Generate G-code for a bearing hole using helical entry + spiral-out strategy.
        Uses helical interpolation to safely enter, then spirals outward in multiple passes.
        """
        gcode = []

        # Calculate target toolpath radius (hole radius minus tool radius for inside cut)
        hole_radius = self.bearing_hole_diameter / 2
        final_toolpath_radius = hole_radius - self.tool_radius

        if final_toolpath_radius <= 0:
            gcode.append(f"(WARNING: Tool diameter {self.tool_diameter:.4f}\" is too large for {self.bearing_hole_diameter:.4f}\" hole!)")
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

        gcode.append(f"(Bearing hole: helical entry at {entry_radius:.4f}\" radius, then {num_radial_passes} radial passes)")

        # Position at edge of entry radius
        start_x = cx + entry_radius
        start_y = cy
        gcode.append(f"G0 X{start_x:.4f} Y{start_y:.4f}  ; Position at entry radius")
        gcode.append(f"G0 Z{ramp_start_height:.4f}  ; Rapid to ramp start height")

        # Helical entry in multiple passes using ramp feed rate
        gcode.append(f"(Helical entry: {num_helical_passes} passes at {self.ramp_angle}°, {depth_per_pass:.4f}\" per pass)")
        for pass_num in range(num_helical_passes):
            target_z = ramp_start_height - (pass_num + 1) * depth_per_pass
            gcode.append(f"G2 X{start_x:.4f} Y{start_y:.4f} I{-entry_radius:.4f} J0 Z{target_z:.4f} F{self.ramp_feed_rate}  ; Helical pass {pass_num + 1}/{num_helical_passes}")

        # Clean up pass at entry radius and final depth
        gcode.append(f"G2 X{start_x:.4f} Y{start_y:.4f} I{-entry_radius:.4f} J0 F{self.feed_rate}  ; Clean up pass at entry radius")

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
            # Use negative angle for clockwise spiral (climb milling on inside feature)
            for i in range(num_points):
                current_angle = -(i * angle_increment)  # Negative for clockwise
                current_radius = entry_radius + spiral_constant * abs(current_angle)

                # Convert polar coordinates to Cartesian
                x = cx + current_radius * math.cos(current_angle)
                y = cy + current_radius * math.sin(current_angle)

                gcode.append(f"G1 X{x:.4f} Y{y:.4f} F{self.feed_rate}")

        # Final cleanup pass at exact final radius
        final_x = cx + final_toolpath_radius
        final_y = cy
        gcode.append(f"(Final cleanup pass at exact radius)")
        gcode.append(f"G1 X{final_x:.4f} Y{final_y:.4f} F{self.feed_rate}  ; Move to final radius")
        gcode.append(f"G2 X{final_x:.4f} Y{final_y:.4f} I{-final_toolpath_radius:.4f} J0 F{self.feed_rate}  ; Cut final circle")

        # Retract
        gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract")

        return gcode

    def _estimate_cycle_time(self, gcode_lines: List[str]) -> dict:
        """
        Estimate total cycle time from G-code.
        Returns dict with breakdown of time components.
        """
        import re
        import math

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
        from shapely.geometry import Polygon
        pocket_poly = Polygon(pocket_points)
        cx = pocket_poly.centroid.x
        cy = pocket_poly.centroid.y

        # Calculate distances from centroid to all vertices
        distances = []
        for x, y in pocket_points:
            dist = math.sqrt((x - cx)**2 + (y - cy)**2)
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
        from shapely.geometry import Polygon
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
        gcode.append(f"G0 X{entry_x:.4f} Y{entry_y:.4f}  ; Position at pocket center")
        gcode.append(f"G0 Z{ramp_start_height:.4f}  ; Rapid to ramp start height")

        # Helical entry at center
        start_x = entry_x + helix_radius
        start_y = entry_y
        gcode.append(f"G1 X{start_x:.4f} Y{start_y:.4f} F{self.feed_rate}  ; Move to helix start")

        for pass_num in range(num_helical_passes):
            target_z = ramp_start_height - (pass_num + 1) * depth_per_pass
            gcode.append(f"G2 X{start_x:.4f} Y{start_y:.4f} I{-helix_radius:.4f} J0 Z{target_z:.4f} F{self.ramp_feed_rate}  ; Helical pass {pass_num + 1}/{num_helical_passes}")

        # Return to center after helix
        gcode.append(f"G1 X{entry_x:.4f} Y{entry_y:.4f} F{self.feed_rate}  ; Return to pocket center")

        # Spiral outward from center to perimeter
        # Calculate maximum distance from center to any perimeter point
        max_radius = 0
        for point in offset_points:
            dist = math.sqrt((point[0] - entry_x)**2 + (point[1] - entry_y)**2)
            max_radius = max(max_radius, dist)

        # Calculate spiral passes (similar to bearing holes)
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
            # Use negative angle for clockwise spiral (climb milling on inside feature)
            for i in range(num_points):
                current_angle = -(i * angle_increment)  # Negative for clockwise
                current_radius = spiral_constant * abs(current_angle)

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
        from shapely.geometry import Polygon
        perimeter_poly = Polygon(perimeter_points)
        
        # Buffer outward (positive buffer) 
        offset_poly = perimeter_poly.buffer(self.tool_radius)
        
        if offset_poly.is_empty:
            gcode.append(f"(WARNING: Perimeter offset failed)")
            return gcode
        
        # Get the boundary of the offset polygon
        if hasattr(offset_poly, 'exterior'):
            offset_points = list(offset_poly.exterior.coords)[:-1]  # Remove duplicate last point
        else:
            gcode.append("(WARNING: Perimeter offset resulted in invalid geometry)")
            return gcode
        
        # Calculate segment lengths
        segment_lengths = []
        for i in range(len(offset_points)):
            p1 = offset_points[i]
            p2 = offset_points[(i + 1) % len(offset_points)]
            length = math.sqrt((p2[0] - p1[0])**2 + (p2[1] - p1[1])**2)
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
        gcode.append(f"G0 X{start[0]:.4f} Y{start[1]:.4f}  ; Move to perimeter start")
        gcode.append(f"G0 Z{ramp_start_height:.4f}  ; Rapid to ramp start height")

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
                        gcode.append(f"G2 X{start_x:.4f} Y{start_y:.4f} I{-helix_radius:.4f} J0 Z{target_z:.4f} F{self.ramp_feed_rate}  ; Helical loop {loop_num + 1}/{num_loops}")

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
            closing_length = math.sqrt((ramp_end_x - last_point[0])**2 + (ramp_end_y - last_point[1])**2)

            # Process closing segment
            process_segment(last_point, (ramp_end_x, ramp_end_y), current_distance, closing_length)

        # Retract
        gcode.append(f"G0 Z{self.safe_height:.4f}  ; Retract")
        
        return gcode

def main():
    parser = argparse.ArgumentParser(description='PenguinCAM - Team 6238 Post-Processor')
    parser.add_argument('input_dxf', help='Input DXF file from OnShape')
    parser.add_argument('output_gcode', help='Output G-code file')
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
    parser.add_argument('--drill-screws', action='store_true',
                       help='Center drill screw holes instead of milling (faster)')
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
    
    # Create post-processor
    pp = FRCPostProcessor(material_thickness=args.thickness,
                          tool_diameter=args.tool_diameter,
                          units=args.units)

    # Apply material preset (sets feeds, speeds, ramp angles)
    pp.apply_material_preset(args.material)

    # Override with command-line parameters if specified
    pp.num_tabs = args.tabs
    pp.drill_screw_holes = args.drill_screws
    pp.sacrifice_board_depth = args.sacrifice_depth

    # Store user name if provided
    if args.user:
        pp.user_name = args.user

    # Set cutting parameters (override preset if specified)
    if args.spindle_speed != 18000:  # Only override if user changed from default
        pp.spindle_speed = args.spindle_speed
    if args.feed_rate is not None:
        pp.feed_rate = args.feed_rate
    if args.plunge_rate is not None:
        pp.plunge_rate = args.plunge_rate
    
    # Recalculate Z positions with user-specified sacrifice depth
    pp.cut_depth = -pp.sacrifice_board_depth
    
    # Process file
    pp.load_dxf(args.input_dxf)

    # Apply origin and rotation transformation BEFORE processing
    # ALWAYS transform to ensure selected corner is at (0,0)
    pp.transform_coordinates(args.origin_corner, args.rotation)

    pp.classify_holes()
    pp.identify_perimeter_and_pockets()

    # Add timestamp to output filename (Pacific time)
    import datetime
    import os
    from zoneinfo import ZoneInfo
    pacific_time = datetime.datetime.now(ZoneInfo("America/Los_Angeles"))
    timestamp = pacific_time.strftime("%Y%m%d_%H%M%S")
    base_name = os.path.splitext(args.output_gcode)[0]
    extension = os.path.splitext(args.output_gcode)[1]
    output_path = f"{base_name}_{timestamp}{extension}"

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
    print(f"  Bearing holes: toolpath radius reduced by {pp.tool_radius:.4f}\"")
    if pp.drill_screw_holes:
        print(f"  Screw holes: center drilled (no compensation)")
    else:
        print(f"  Screw holes: milled with compensation")


if __name__ == '__main__':
    main()
