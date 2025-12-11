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
        self.clearance_height = 0.1  # Clearance above material top (inches)
        
        # Calculated Z positions (Z=0 at sacrifice board)
        self.safe_height = material_thickness + self.clearance_height  # Above material
        self.material_top = material_thickness  # Top surface of material
        self.cut_depth = -self.sacrifice_board_depth  # Cut slightly into sacrifice board
        
        # Cutting parameters (you can adjust these)
        self.spindle_speed = 24000
        self.feed_rate = 14.0 if units == "inch" else 365  # inches per minute
        self.plunge_rate = 10.0 if units =="inch" else 339 # inches per minute
        
        # Tab parameters
        self.tab_width = 0.25  # Width of tabs (inches)
        self.tab_height = 0.03  # How much material to leave in tab (inches)
        self.num_tabs = 4  # Number of tabs around perimeter
        
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
        
        # Calculate new bounds
        all_x = []
        all_y = []
        for circle in self.circles:
            all_x.append(circle['center'][0])
            all_y.append(circle['center'][1])
        for line in self.lines:
            all_x.extend([line['start'][0], line['end'][0]])
            all_y.extend([line['start'][1], line['end'][1]])
        
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
        gcode = []
        
        # Header
        gcode.append("(PenguinCAM - Team 6238)")
        gcode.append(f"(Material thickness: {self.material_thickness}\")")
        gcode.append(f"(Tool diameter: {self.tool_diameter}\" = {self.tool_diameter * 25.4:.2f}mm)")
        gcode.append(f"(Tool compensation: Perimeter +{self.tool_radius:.4f}\", Pockets -{self.tool_radius:.4f}\")")
        gcode.append("")
        gcode.append("(Z-AXIS COORDINATE SYSTEM:)")
        gcode.append(f"(  Z=0 is at SACRIFICE BOARD (bottom))")
        gcode.append(f"(  Material top is at Z={self.material_top:.4f}\")")
        gcode.append(f"(  Cut depth: Z={self.cut_depth:.4f}\" ({self.sacrifice_board_depth:.4f}\" into sacrifice board))")
        gcode.append(f"(  Safe height: Z={self.safe_height:.4f}\")")
        gcode.append("(  ** Zero your Z-axis to the sacrifice board surface **)")
        gcode.append("")
        gcode.append("(Generated by frc_cam_postprocessor.py)")
        gcode.append("")
        
        # Setup
        if self.units == "inch":
            gcode.append("G20  ; Inches")
        else:
            gcode.append("G21  ; Millimeters")
        
        gcode.append("G90  ; Absolute positioning")
        gcode.append("G17  ; XY plane")
        gcode.append(f"F{self.feed_rate}  ; Set feed rate")
        gcode.append("")
        
        # Spindle on
<<<<<<< HEAD
        gcode.append(f"M3 S{self.spindle_speed}  ; Spindle on at {self.spindle_speed} RPM")
=======
        gcode.append(f"M3 {self.spindle_speed}  ; Spindle on at {self.spindle_speed} RPM")
>>>>>>> 6aa4b64 (Material chooser)
        gcode.append("G4 P2  ; Wait 2 seconds for spindle to reach speed")
        gcode.append("")
        
        # Move to safe height
        gcode.append(f"G0 Z{self.safe_height:.4f}  ; Move to safe height")
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
                        gcode.append(f"G0 Z{self.safe_height:.4f}")
                        gcode.append(f"G1 Z{self.cut_depth:.4f} F{self.plunge_rate}  ; Plunge")
                        gcode.append(f"G1 Z{self.safe_height:.4f} F{self.plunge_rate}  ; Retract")
                    else:
                        # Position at edge of toolpath
                        start_x = x + toolpath_radius
                        start_y = y
                        gcode.append(f"G0 X{start_x:.4f} Y{start_y:.4f}  ; Position at hole edge (compensated)")
                        gcode.append(f"G0 Z{self.safe_height:.4f}")
                        gcode.append(f"G1 Z{self.material_top:.4f} F{self.plunge_rate}  ; Rapid to surface")
                        gcode.append(f"G2 X{start_x:.4f} Y{start_y:.4f} I{-toolpath_radius:.4f} J0 Z{self.cut_depth:.4f} F{self.plunge_rate}  ; Helical plunge")
                        gcode.append(f"G2 X{start_x:.4f} Y{start_y:.4f} I{-toolpath_radius:.4f} J0 F{self.feed_rate}  ; Clean up pass")
                        gcode.append(f"G0 Z{self.safe_height:.4f}  ; Retract")
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
        gcode.append("M5  ; Spindle off")
        gcode.append("G0 X0 Y0  ; Return to origin")
        gcode.append("M30  ; Program end")
        
        # Write to file
        with open(output_file, 'w') as f:
            f.write('\n'.join(gcode))
        
        print(f"\nG-code written to {output_file}")
        print(f"Total lines: {len(gcode)}")
    
    def _generate_bearing_hole_gcode(self, cx: float, cy: float) -> List[str]:
        """Generate G-code for a bearing hole using helical interpolation with tool compensation"""
        gcode = []
        
        # Calculate toolpath radius (hole radius minus tool radius for inside cut)
        hole_radius = self.bearing_hole_diameter / 2
        toolpath_radius = hole_radius - self.tool_radius
        
        if toolpath_radius <= 0:
            gcode.append(f"(WARNING: Tool diameter {self.tool_diameter:.4f}\" is too large for {self.bearing_hole_diameter:.4f}\" hole!)")
            return gcode
        
        # Position at edge of toolpath
        start_x = cx + toolpath_radius
        start_y = cy
        
        gcode.append(f"G0 X{start_x:.4f} Y{start_y:.4f}  ; Position at hole edge (compensated)")
        gcode.append(f"G0 Z{self.safe_height:.4f}")
        
        # Helical plunge using G2 (clockwise circular interpolation)
        gcode.append(f"G1 Z{self.material_top:.4f} F{self.plunge_rate}  ; Rapid to material surface")
        gcode.append(f"G2 X{start_x:.4f} Y{start_y:.4f} I{-toolpath_radius:.4f} J0 Z{self.cut_depth:.4f} F{self.plunge_rate}  ; Helical plunge")
        
        # Clean up pass at depth
        gcode.append(f"G2 X{start_x:.4f} Y{start_y:.4f} I{-toolpath_radius:.4f} J0 F{self.feed_rate}  ; Clean up pass")
        
        # Retract
        gcode.append(f"G0 Z{self.safe_height:.4f}  ; Retract")
        
        return gcode
    
    def _generate_pocket_gcode(self, pocket_points: List[Tuple[float, float]]) -> List[str]:
        """Generate G-code for a pocket with tool compensation (offset inward)"""
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
        
        # Move to start
        start = offset_points[0]
        gcode.append(f"G0 X{start[0]:.4f} Y{start[1]:.4f}  ; Move to pocket start (compensated)")
        gcode.append(f"G0 Z{self.safe_height:.4f}")
        gcode.append(f"G1 Z{self.cut_depth:.4f} F{self.plunge_rate}  ; Plunge")
        
        # Cut around pocket boundary (offset path)
        for point in offset_points[1:]:
            gcode.append(f"G1 X{point[0]:.4f} Y{point[1]:.4f} F{self.feed_rate}")
        
        # Close the loop
        gcode.append(f"G1 X{start[0]:.4f} Y{start[1]:.4f} F{self.feed_rate}  ; Close loop")
        
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
        
        # Calculate segment lengths and identify straight vs curved sections
        segment_lengths = []
        segment_angles = []
        is_straight = []
        
        for i in range(len(offset_points)):
            p1 = offset_points[i]
            p2 = offset_points[(i + 1) % len(offset_points)]
            p0 = offset_points[(i - 1) % len(offset_points)]
            
            # Segment length
            length = math.sqrt((p2[0] - p1[0])**2 + (p2[1] - p1[1])**2)
            segment_lengths.append(length)
            
            # Calculate angle change to detect curves
            # Vector from p0 to p1
            v1 = (p1[0] - p0[0], p1[1] - p0[1])
            v1_len = math.sqrt(v1[0]**2 + v1[1]**2)
            
            # Vector from p1 to p2
            v2 = (p2[0] - p1[0], p2[1] - p1[1])
            v2_len = math.sqrt(v2[0]**2 + v2[1]**2)
            
            if v1_len > 0.001 and v2_len > 0.001:
                # Normalize vectors
                v1_norm = (v1[0] / v1_len, v1[1] / v1_len)
                v2_norm = (v2[0] / v2_len, v2[1] / v2_len)
                
                # Dot product to get angle
                dot_product = v1_norm[0] * v2_norm[0] + v1_norm[1] * v2_norm[1]
                dot_product = max(-1.0, min(1.0, dot_product))  # Clamp to [-1, 1]
                angle = math.acos(dot_product)
                segment_angles.append(angle)
                
                # If angle change is small (< 5 degrees), consider it straight
                angle_threshold = math.radians(5)  # 5 degrees
                is_straight.append(angle < angle_threshold)
            else:
                segment_angles.append(0)
                is_straight.append(True)  # Very short segments treated as straight
        
        # Calculate total perimeter length
        perimeter_length = sum(segment_lengths)
        
        # Find straight sections and their lengths
        straight_sections = []
        current_section_start = 0
        current_section_length = 0
        in_straight_section = False
        
        for i in range(len(offset_points)):
            if is_straight[i]:
                if not in_straight_section:
                    # Start of new straight section
                    current_section_start = sum(segment_lengths[:i])
                    current_section_length = 0
                    in_straight_section = True
                current_section_length += segment_lengths[i]
            else:
                if in_straight_section:
                    # End of straight section
                    straight_sections.append({
                        'start': current_section_start,
                        'length': current_section_length,
                        'end': current_section_start + current_section_length
                    })
                    in_straight_section = False
        
        # Close the last section if needed
        if in_straight_section:
            straight_sections.append({
                'start': current_section_start,
                'length': current_section_length,
                'end': current_section_start + current_section_length
            })
        
        # Calculate tab positions - distribute evenly among straight sections
        tab_positions = []
        
        if len(straight_sections) == 0:
            # No straight sections found - fall back to evenly spaced tabs
            gcode.append("(WARNING: No straight sections found for tabs - using evenly spaced tabs)")
            tab_spacing = perimeter_length / self.num_tabs
            tab_positions = [i * tab_spacing for i in range(self.num_tabs)]
        else:
            # Place tabs in straight sections
            # Distribute tabs proportionally based on straight section lengths
            total_straight_length = sum(s['length'] for s in straight_sections)
            
            # How many tabs per straight section?
            tabs_placed = 0
            for section in straight_sections:
                # Proportional number of tabs for this section
                section_tabs = max(1, round(self.num_tabs * section['length'] / total_straight_length))
                
                # Don't exceed total tabs
                if tabs_placed + section_tabs > self.num_tabs:
                    section_tabs = self.num_tabs - tabs_placed
                
                if section_tabs > 0 and section['length'] > self.tab_width * 2:
                    # Place tabs evenly within this section
                    if section_tabs == 1:
                        # Single tab in center of section
                        tab_positions.append(section['start'] + section['length'] / 2)
                    else:
                        # Multiple tabs evenly spaced
                        tab_spacing = section['length'] / (section_tabs + 1)
                        for i in range(1, section_tabs + 1):
                            tab_positions.append(section['start'] + i * tab_spacing)
                    
                    tabs_placed += section_tabs
                
                if tabs_placed >= self.num_tabs:
                    break
            
            # If we didn't place enough tabs, add more to longest straight section
            if tabs_placed < self.num_tabs and straight_sections:
                longest_section = max(straight_sections, key=lambda s: s['length'])
                remaining_tabs = self.num_tabs - tabs_placed
                for i in range(remaining_tabs):
                    # Spread remaining tabs in longest section
                    position = longest_section['start'] + (i + 1) * longest_section['length'] / (remaining_tabs + 1)
                    tab_positions.append(position)
        
        gcode.append(f"(Tabs placed: {len(tab_positions)} on straight sections)")
        
        # Move to start
        start = offset_points[0]
        gcode.append(f"G0 X{start[0]:.4f} Y{start[1]:.4f}  ; Move to perimeter start (compensated)")
        gcode.append(f"G0 Z{self.safe_height:.4f}")
        gcode.append(f"G1 Z{self.cut_depth:.4f} F{self.plunge_rate}  ; Plunge to cut depth")
        
        # Cut around perimeter with tabs
        current_distance = 0
        tab_index = 0
        
        for i, point in enumerate(offset_points[1:] + [offset_points[0]], 1):
            segment_start_dist = current_distance
            segment_end_dist = current_distance + segment_lengths[i - 1]
            
            # Check if any tabs are in this segment
            while tab_index < len(tab_positions) and tab_positions[tab_index] < segment_end_dist:
                tab_dist = tab_positions[tab_index]
                
                if tab_dist >= segment_start_dist and segment_lengths[i - 1] > 0:
                    # Calculate tab position along segment
                    t = (tab_dist - segment_start_dist) / segment_lengths[i - 1]
                    prev_point = offset_points[i - 1]
                    
                    # Tab start
                    tab_start_x = prev_point[0] + t * (point[0] - prev_point[0]) - self.tab_width / 2 * (point[0] - prev_point[0]) / segment_lengths[i - 1]
                    tab_start_y = prev_point[1] + t * (point[1] - prev_point[1]) - self.tab_width / 2 * (point[1] - prev_point[1]) / segment_lengths[i - 1]
                    
                    # Move to tab start
                    gcode.append(f"G1 X{tab_start_x:.4f} Y{tab_start_y:.4f} F{self.feed_rate}")
                    
                    # Raise for tab (leave tab_height of material)
                    tab_z = self.cut_depth + self.tab_height
                    gcode.append(f"G1 Z{tab_z:.4f} F{self.plunge_rate}  ; Tab {tab_index + 1}")
                    
                    # Tab end
                    tab_end_x = prev_point[0] + t * (point[0] - prev_point[0]) + self.tab_width / 2 * (point[0] - prev_point[0]) / segment_lengths[i - 1]
                    tab_end_y = prev_point[1] + t * (point[1] - prev_point[1]) + self.tab_width / 2 * (point[1] - prev_point[1]) / segment_lengths[i - 1]
                    
                    # Move across tab
                    gcode.append(f"G1 X{tab_end_x:.4f} Y{tab_end_y:.4f} F{self.feed_rate}")
                    
                    # Lower back to cut depth
                    gcode.append(f"G1 Z{self.cut_depth:.4f} F{self.plunge_rate}")
                
                tab_index += 1
            
            # Continue to next point
            gcode.append(f"G1 X{point[0]:.4f} Y{point[1]:.4f} F{self.feed_rate}")
            current_distance = segment_end_dist
        
        # Retract
        gcode.append(f"G0 Z{self.safe_height:.4f}  ; Retract")
        
        return gcode

def main():
    parser = argparse.ArgumentParser(description='PenguinCAM - Team 6238 Post-Processor')
    parser.add_argument('input_dxf', help='Input DXF file from OnShape')
    parser.add_argument('output_gcode', help='Output G-code file')
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
    
    # NEW: Cutting parameters
    parser.add_argument('--spindle-speed', type=int, default=24000,
                       help='Spindle speed in RPM (default: 24000)')
    parser.add_argument('--feed-rate', type=float, default=None,
                       help='Feed rate (default: 14 ipm or 365 mm/min depending on units)')
    parser.add_argument('--plunge-rate', type=float, default=None,
                       help='Plunge rate (default: 10 ipm or 339 mm/min depending on units)')
    
    args = parser.parse_args()
    
    # Create post-processor
    pp = FRCPostProcessor(material_thickness=args.thickness, 
                          tool_diameter=args.tool_diameter,
                          units=args.units)
    pp.num_tabs = args.tabs
    pp.drill_screw_holes = args.drill_screws
    pp.sacrifice_board_depth = args.sacrifice_depth
    
    # Set cutting parameters
    pp.spindle_speed = args.spindle_speed
    pp.feed_rate = args.feed_rate
    pp.plunge_rate = args.plunge_rate
    
    # Recalculate Z positions with user-specified sacrifice depth
    pp.cut_depth = -pp.sacrifice_board_depth
    
    # Process file
    pp.load_dxf(args.input_dxf)
    
    # Apply origin and rotation transformation BEFORE processing
    if args.origin_corner != 'bottom-left' or args.rotation != 0:
        pp.transform_coordinates(args.origin_corner, args.rotation)
    
    pp.classify_holes()
    pp.identify_perimeter_and_pockets()
    pp.generate_gcode(args.output_gcode)
    
    print("\nDone! Review the G-code file before running on your machine.")
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
