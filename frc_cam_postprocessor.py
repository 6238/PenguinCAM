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
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict, Any

# Third-party
import ezdxf
from shapely import affinity
from shapely.geometry import Point, Polygon, LineString, LinearRing, MultiPolygon
from shapely.ops import unary_union, linemerge

# Local modules
from team_config import TeamConfig


@dataclass
class PostProcessorResult:
    """Result from post-processor operations"""
    success: bool
    gcode: Optional[str] = None
    filename: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
        return {
            'success': self.success,
            'gcode': self.gcode,
            'filename': self.filename,
            'errors': self.errors,
            'warnings': self.warnings,
            'stats': self.stats
        }


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
        'helix_radius_multiplier': 0.75, # Helix entry radius as fraction of tool radius
        'max_slotting_depth': 0.4,      # Maximum depth per pass for perimeter slotting (inches)
        'tab_width': 0.25,        # Tab width (inches)
        'tab_height': 0.15,        # Tab height (inches)
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
        'helix_radius_multiplier': 0.5,  # Helix entry radius as fraction of tool radius (conservative for aluminum)
        'max_slotting_depth': 0.2,      # Maximum depth per pass for perimeter slotting (inches)
        'tab_width': 0.25,        # Tab width (inches) - same as plywood
        'tab_height': 0.15,       # Tab height (inches) - same as plywood
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
        'helix_radius_multiplier': 0.75, # Helix entry radius as fraction of tool radius
        'max_slotting_depth': 0.25,     # Maximum depth per pass for perimeter slotting (inches)
        'tab_width': 0.25,        # Tab width (inches) - same as plywood
        'tab_height': 0.15,        # Tab height (inches) - same as plywood
        'description': 'Polycarbonate - same as plywood settings'
    }
}


class FRCPostProcessor:
    def __init__(self, material_thickness: float, tool_diameter: float, units: str = "inch",
                 config: Optional[TeamConfig] = None):
        """
        Initialize the post-processor

        Args:
            material_thickness: Thickness of material in inches
            tool_diameter: Diameter of cutting tool in inches (e.g., 4mm = 0.157")
            units: "inch" or "mm"
            config: Optional TeamConfig instance for team-specific settings.
                   If not provided, uses Team 6238 defaults.
        """
        # Use provided config or create default (Team 6238 defaults)
        if config is None:
            config = TeamConfig()
        self.config = config

        self.material_thickness = material_thickness
        self.tool_diameter = tool_diameter
        self.tool_radius = tool_diameter / 2
        self.units = units

        # Hole detection tolerance from config
        self.tolerance = config.hole_detection_tolerance

        # Minimum hole diameter that can be milled (must be > tool diameter for chip evacuation)
        # Holes smaller than this are skipped
        self.min_millable_hole = tool_diameter * config.min_millable_hole_multiplier

        # Multi-layer support
        self.layer_data = None  # Set by load_dxf for multi-layer DXFs

        # Z-axis reference: Z=0 is at BOTTOM (sacrifice board surface)
        # This allows zeroing to the sacrifice board instead of material top
        self.sacrifice_board_depth = config.sacrifice_board_depth  # How far to cut into sacrifice board (inches)
        self.clearance_height = config.clearance_height  # Clearance above material top for rapid moves (inches)

        # Calculated Z positions (Z=0 at sacrifice board)
        self.retract_height = material_thickness + self.clearance_height  # Retract above material for operations
        self.material_top = material_thickness  # Top surface of material
        self.cut_depth = -self.sacrifice_board_depth  # Cut slightly into sacrifice board

        # Cutting parameters (defaults - can be overridden by material presets)
        self.spindle_speed = 18000  # RPM
        self.feed_rate = 75.0 if units == "inch" else 1905  # Cutting feed rate (IPM or mm/min)
        self.ramp_feed_rate = 50.0 if units == "inch" else 1270  # Ramp feed rate (IPM or mm/min)
        self.plunge_rate = 35.0 if units == "inch" else 889  # Plunge feed rate (IPM or mm/min) for tab Z moves
        self.traverse_rate = 200.0 if units == "inch" else 5080  # Lateral moves above material (IPM or mm/min) - rapid moves
        self.approach_rate = 50.0 if units == "inch" else 1270  # Z approach to ramp start height (IPM or mm/min)
        self.ramp_angle = 20.0  # Ramp angle in degrees (for helical bores and perimeter ramps)
        self.ramp_start_clearance = 0.15 if units == "inch" else 3.8  # Clearance above material to start ramping
        self.stepover_percentage = 0.6  # Radial stepover as fraction of tool diameter (default 60%)

        # Tab parameters from config
        self.tabs_enabled = config.tabs_enabled  # Whether tabs are enabled
        self.tab_width = config.tab_width  # Width of tabs (inches)
        self.tab_height = config.tab_height  # How much material to leave in tab (inches)
        self.tab_spacing = config.tab_spacing  # Desired spacing between tabs (inches)

        # Fixturing preferences from config
        self.pause_before_perimeter = config.pause_before_perimeter  # Pause before perimeter for screw fixturing

        # Tube facing parameters
        self.tube_facing_offset = 0.0625  # Hole offset to align with faced surface at Y=+1/16" (inches)

        # Tube facing operation constants from config
        self.tube_facing_params = config.get_tube_facing_params()

        # Machine-specific constants from config
        self.machine_park_x = config.machine_park_x  # X position for machine park (machine coordinates)
        self.machine_park_y = config.machine_park_y  # Y position for machine park (machine coordinates)
        self.machine_park_z = config.machine_park_z  # Z position for safe clearance (machine coordinates)

        # Team information from config
        self.team_number = config.team_number  # FRC team number
        self.team_name = config.team_name  # FRC team name
        self.machine_name = config.machine_name  # Machine name
        self.machine_controller = config.machine_controller  # Controller type
        self.machine_coolant = config.machine_coolant  # Coolant type

        # Helix entry radius multiplier (applied to tool diameter)
        # Overridden by material presets
        self.helix_radius_multiplier = 0.75  # Default 75% of tool radius

        # Error tracking
        self.errors = []  # Collect validation errors during processing

    def apply_material_preset(self, material: str, machine_id: Optional[str] = None):
        """
        Apply a material preset to set feeds, speeds, and ramp angles.

        Args:
            material: Material name ('plywood', 'aluminum', 'polycarbonate', or custom)
            machine_id: Optional machine ID for machine-specific settings
        """
        # Get material preset from config (merges user config with Team 6238 defaults)
        preset = self.config.get_material_preset(material, machine_id)

        # Check if we got a valid preset (config returns empty dict for unknown materials)
        if not preset:
            print(f"Warning: Unknown material '{material}'. Using default plywood settings.")
            preset = self.config.get_material_preset('plywood', machine_id)

        self.material_name = preset.get('name', material.capitalize())  # Store material name for header

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

        # Max slotting depth (convert to mm if needed)
        if self.units == 'mm':
            self.max_slotting_depth = preset['max_slotting_depth'] * 25.4
        else:
            self.max_slotting_depth = preset['max_slotting_depth']

        # Tab sizes (convert to mm if needed)
        if self.units == 'mm':
            self.tab_width = preset['tab_width'] * 25.4
            self.tab_height = preset['tab_height'] * 25.4
        else:
            self.tab_width = preset['tab_width']
            self.tab_height = preset['tab_height']

        # Helix entry radius multiplier
        self.helix_radius_multiplier = preset['helix_radius_multiplier']

        # Peck drill depth (convert to mm if needed)
        if self.units == 'mm':
            self.peck_drill_depth = preset['peck_drill_depth'] * 25.4
        else:
            self.peck_drill_depth = preset['peck_drill_depth']

        print(f"\nApplied material preset: {preset.get('name', material.capitalize())}")
        if 'description' in preset:
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

    def _get_polygon_center(self, polygon) -> Tuple[float, float]:
        """
        Get approximate center of a Shapely polygon from its bounding box.

        Args:
            polygon: Shapely Polygon object

        Returns:
            (center_x, center_y) tuple
        """
        bounds = polygon.bounds  # (minx, miny, maxx, maxy)
        center_x = (bounds[0] + bounds[2]) / 2
        center_y = (bounds[1] + bounds[3]) / 2
        return center_x, center_y

    def _add_error(self, error_msg: str):
        """
        Add an error message to the error list and print it.

        Args:
            error_msg: Error message to add
        """
        print(f"  ❌ ERROR: {error_msg}")
        self.errors.append(error_msg)

    def _generate_pause_and_park_gcode(self, title: str, instructions: List[str]) -> List[str]:
        """
        Generate G-code for a safe pause-and-restart sequence with operator instructions.

        This standardized pause sequence:
        1. Moves to safe Z height (machine coordinates)
        2. Parks at machine park position
        3. Turns off air blast and spindle
        4. Displays operator instructions
        5. Pauses program (M0) waiting for operator to press CYCLE START
        6. Restarts spindle and air blast after CYCLE START
        7. Dwells for spindle spin-up

        Args:
            title: Title for the pause section (e.g., "PAUSE FOR TUBE FLIP")
            instructions: List of instruction lines to display to operator

        Returns:
            List of G-code lines for the complete pause-and-restart sequence
        """
        gcode = []
        gcode.append('')
        gcode.append(f'( === {title} === )')
        gcode.append(f'G53 G0 Z{self.machine_park_z:.4f}  ; Move to safe machine Z clearance')
        gcode.append(f'G53 G0 X{self.machine_park_x} Y{self.machine_park_y}  ; Park at back of machine')
        gcode.append('M9  ; Air blast off')
        gcode.append('M5  ; Spindle off')
        gcode.append('G4 P5.0  ; 5 second dwell')
        gcode.append('')
        gcode.append('( *** OPERATOR ACTION REQUIRED *** )')
        for instruction in instructions:
            gcode.append(f'( {instruction} )')
        gcode.append('( Press CYCLE START to continue )')
        gcode.append('M0  ; Program pause')
        gcode.append('')
        gcode.append('( === RESTART AFTER PAUSE === )')
        gcode.append('G90  ; Ensure absolute positioning mode')
        gcode.append(f'S{self.spindle_speed} M3  ; Spindle on')
        gcode.append('M7  ; Air blast on')
        gcode.append('G4 P3.0  ; 3 second spindle spin-up')
        gcode.append('')
        return gcode

    def _parse_layer_depth(self, layer_name: str) -> Optional[float]:
        """
        Parse Z depth from layer name (e.g., "Z_-0p250" -> -0.25, "Z_0p000" -> 0)
        Returns None if layer name doesn't match the expected format
        """
        match = re.match(r'^Z_(-?\d+)p(\d+)$', layer_name)
        if not match:
            return None

        is_negative = match.group(1).startswith('-')
        int_part = int(match.group(1))
        frac_part = int(match.group(2))
        frac_value = frac_part / (10 ** len(match.group(2)))

        if is_negative:
            return int_part - frac_value
        else:
            return int_part + frac_value

    def load_dxf(self, filename: str):
        """Load DXF file and extract geometry, organized by layer if multi-layer DXF"""
        print(f"Loading {filename}...")
        doc = ezdxf.readfile(filename)
        msp = doc.modelspace()

        # Check for multi-layer structure
        layers_with_depths = {}
        for layer in doc.layers:
            depth = self._parse_layer_depth(layer.dxf.name)
            if depth is not None:
                layers_with_depths[layer.dxf.name] = depth

        if layers_with_depths:
            print(f"Detected multi-layer DXF with {len(layers_with_depths)} depth layers")
            self._load_multilayer_dxf(doc, msp, layers_with_depths)
        else:
            print("Processing as single-layer DXF")
            self._load_singlelayer_dxf(msp)

    def _load_singlelayer_dxf(self, msp):
        """Load geometry from single-layer DXF (existing logic)"""
        self.layer_data = None  # Mark as single-layer

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
        # This is needed for Onshape exports which use individual entities
        lines = list(msp.query('LINE'))
        arcs = list(msp.query('ARC'))
        splines = list(msp.query('SPLINE'))

        # Also collect unclosed LWPOLYLINEs - they may be part of a perimeter that needs stitching
        unclosed_lwpolylines = []
        for entity in msp.query('LWPOLYLINE'):
            if not entity.closed and len(list(entity.get_points('xy'))) > 1:
                unclosed_lwpolylines.append(entity)

        if lines or arcs or splines or unclosed_lwpolylines:
            print(f"Found {len(lines)} lines, {len(arcs)} arcs, {len(splines)} splines, {len(unclosed_lwpolylines)} unclosed polylines - attempting to form closed paths...")
            closed_paths = self._chain_entities_to_paths(lines, arcs, splines, unclosed_lwpolylines)
            self.polylines.extend(closed_paths)
        
        print(f"Found {len(self.circles)} circles and {len(self.polylines)} closed paths")

    def _load_multilayer_dxf(self, doc, msp, layers_with_depths):
        """Load geometry from multi-layer DXF, organized by depth"""
        # Initialize geometry lists for transform_coordinates compatibility
        self.lines = []
        self.arcs = []
        self.splines = []

        # Store layer information for multi-pass processing
        self.layer_data = {}

        # Sort layers by depth (shallowest first, but we'll process deepest first except perimeter)
        sorted_layers = sorted(layers_with_depths.items(), key=lambda x: x[1], reverse=True)

        for layer_name, depth in sorted_layers:
            print(f"  Processing layer {layer_name} (Z={depth:.4f}\")")

            # Extract entities for this layer
            layer_circles = []
            layer_polylines = []

            # PRIORITY 1: Extract HATCH entities (solid regions from new format)
            hatch_count = 0
            for entity in msp.query('HATCH'):
                if entity.dxf.layer == layer_name:
                    try:
                        # Each HATCH has multiple boundary paths
                        for path in entity.paths:
                            if hasattr(path, 'vertices') and path.vertices:
                                # Polyline path
                                coords = [(v[0], v[1]) for v in path.vertices]
                                if len(coords) >= 3:
                                    layer_polylines.append(coords)
                                    hatch_count += 1
                    except Exception as e:
                        print(f"      Warning: Could not parse HATCH entity: {e}")

            if hatch_count > 0:
                print(f"    Extracted {hatch_count} regions from HATCH entities (solid format)")

            # FALLBACK: Extract circles and polylines (old stroke format)
            # Only process if no HATCH entities found
            if hatch_count == 0:
                # Extract circles from this layer
                for entity in msp.query('CIRCLE'):
                    if entity.dxf.layer == layer_name:
                        center = (entity.dxf.center.x, entity.dxf.center.y)
                        radius = entity.dxf.radius
                        layer_circles.append({'center': center, 'radius': radius, 'diameter': radius * 2})

                # Extract polylines from this layer (same logic as single-layer)
                for entity in msp.query('LWPOLYLINE'):
                    if entity.dxf.layer == layer_name:
                        points = [(p[0], p[1]) for p in entity.get_points('xy')]
                        if entity.closed and len(points) > 2:
                            layer_polylines.append(points)

                for entity in msp.query('POLYLINE'):
                    if entity.is_2d_polyline and entity.dxf.layer == layer_name:
                        points = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
                        if entity.is_closed and len(points) > 2:
                            layer_polylines.append(points)

            # Collect individual entities for path stitching
            lines = [e for e in msp.query('LINE') if e.dxf.layer == layer_name]
            arcs = [e for e in msp.query('ARC') if e.dxf.layer == layer_name]
            splines = [e for e in msp.query('SPLINE') if e.dxf.layer == layer_name]
            unclosed_lwpolylines = [e for e in msp.query('LWPOLYLINE')
                                   if e.dxf.layer == layer_name and not e.closed
                                   and len(list(e.get_points('xy'))) > 1]

            if lines or arcs or splines or unclosed_lwpolylines:
                closed_paths = self._chain_entities_to_paths(lines, arcs, splines, unclosed_lwpolylines)
                layer_polylines.extend(closed_paths)

            # Convert geometry to Shapely Polygons for unified representation
            polygons = self._convert_to_shapely_polygons(layer_circles, layer_polylines)

            self.layer_data[layer_name] = {
                'depth': depth,
                'polygons': polygons,
                # Keep old format temporarily for compatibility during migration
                'circles': layer_circles,
                'polylines': layer_polylines
            }

            print(f"    Found {len(layer_circles)} circles and {len(layer_polylines)} closed paths at this depth")
            print(f"    Converted to {len(polygons)} Shapely Polygon(s)")

        # For compatibility, set top-level circles/polylines to COPIES of the shallowest layer
        # (This allows classify_loops to work as-is for single-layer operations)
        # IMPORTANT: Use copy() to avoid double-transformation when transform_coordinates is called
        if self.layer_data:
            top_layer = sorted_layers[0][0]  # Shallowest layer
            self.circles = [circle.copy() for circle in self.layer_data[top_layer]['circles']]
            self.polylines = [polyline[:] for polyline in self.layer_data[top_layer]['polylines']]

    def _chain_entities_to_paths(self, lines, arcs, splines, unclosed_polylines=None):
        """
        Chain individual LINE, ARC, SPLINE, and unclosed LWPOLYLINE entities into closed paths.
        This handles DXF exports from Onshape and other CAD programs that don't use polylines.
        """
        if unclosed_polylines is None:
            unclosed_polylines = []

        # First, try the graph-based approach for exact geometry
        print("  Attempting to connect segments into exact paths...")
        exact_paths = self._connect_segments_graph_based(lines, arcs, splines, unclosed_polylines)
        if exact_paths:
            return exact_paths

        # Fallback: Convert all entities to linestrings and try merge
        print("  Falling back to linestring merge...")
        all_linestrings = []

        # Add unclosed LWPOLYLINE entities
        for lwpoly in unclosed_polylines:
            points = [(p[0], p[1]) for p in lwpoly.get_points('xy')]
            if len(points) >= 2:
                all_linestrings.append(LineString(points))

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
    
    def _connect_segments_graph_based(self, lines, arcs, splines, unclosed_polylines=None):
        """
        Build a connectivity graph and find closed cycles.
        This preserves exact geometry including curves.
        """
        if unclosed_polylines is None:
            unclosed_polylines = []

        # Build list of all segments with their endpoints
        segments = []

        # Add unclosed LWPOLYLINE entities (treat as multi-point path segment)
        for lwpoly in unclosed_polylines:
            points = [(p[0], p[1]) for p in lwpoly.get_points('xy')]
            if len(points) >= 2:
                segments.append({'type': 'polyline', 'points': points, 'start': points[0], 'end': points[-1]})

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
            start_key = self._round_point(seg['start'], 2)
            graph[start_key].append((idx, True))

            # Add connections for end point
            end_key = self._round_point(seg['end'], 2)
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
                end_key = self._round_point(current_end, 2)
                
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
            cx, cy = circle['center']
            r = circle.get('radius') or (circle.get('diameter', 0) / 2)
            # Include circle bounds (center ± radius)
            all_x.extend([cx - r, cx + r])
            all_y.extend([cy - r, cy + r])
        
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

        # Also collect coordinates from multi-layer geometry if present
        if self.layer_data:
            for layer_name, layer_info in self.layer_data.items():
                for circle in layer_info['circles']:
                    cx, cy = circle['center']
                    r = circle.get('radius') or (circle.get('diameter', 0) / 2)
                    # Include circle bounds (center ± radius)
                    all_x.extend([cx - r, cx + r])
                    all_y.extend([cy - r, cy + r])
                for polyline in layer_info['polylines']:
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

            # Also transform multi-layer geometry if present
            if self.layer_data:
                for layer_name, layer_info in self.layer_data.items():
                    # Rotate circles
                    for circle in layer_info['circles']:
                        circle['center'] = rotate_point(*circle['center'])

                    # Rotate polylines
                    for i, polyline in enumerate(layer_info['polylines']):
                        layer_info['polylines'][i] = [rotate_point(x, y) for x, y in polyline]

                    # Rotate Shapely Polygons
                    if 'polygons' in layer_info:
                        rotated_polygons = []
                        for poly in layer_info['polygons']:
                            # Rotate around center point
                            rotated = affinity.rotate(poly, rotation_angle, origin=(centerX, centerY), use_radians=False)
                            rotated_polygons.append(rotated)
                        layer_info['polygons'] = rotated_polygons

            # Recalculate bounds after rotation
            all_x = []
            all_y = []
            for circle in self.circles:
                cx, cy = circle['center']
                r = circle.get('radius') or (circle.get('diameter', 0) / 2)
                all_x.extend([cx - r, cx + r])
                all_y.extend([cy - r, cy + r])
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

            # Include multi-layer geometry in bounds calculation
            if self.layer_data:
                for layer_name, layer_info in self.layer_data.items():
                    for circle in layer_info['circles']:
                        cx, cy = circle['center']
                        r = circle.get('radius') or (circle.get('diameter', 0) / 2)
                        all_x.extend([cx - r, cx + r])
                        all_y.extend([cy - r, cy + r])
                    for polyline in layer_info['polylines']:
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

        # Also transform multi-layer geometry if present
        if self.layer_data:
            for layer_name, layer_info in self.layer_data.items():
                # Translate circles
                for circle in layer_info['circles']:
                    circle['center'] = translate_point(*circle['center'])

                # Translate polylines
                for i, polyline in enumerate(layer_info['polylines']):
                    layer_info['polylines'][i] = [translate_point(x, y) for x, y in polyline]

                # Translate Shapely Polygons
                if 'polygons' in layer_info:
                    translated_polygons = []
                    for poly in layer_info['polygons']:
                        translated = affinity.translate(poly, xoff=offsetX, yoff=offsetY)
                        translated_polygons.append(translated)
                    layer_info['polygons'] = translated_polygons

        # Calculate new bounds
        all_x = []
        all_y = []
        for circle in self.circles:
            cx, cy = circle['center']
            r = circle.get('radius') or (circle.get('diameter', 0) / 2)
            all_x.extend([cx - r, cx + r])
            all_y.extend([cy - r, cy + r])
        for line in self.lines:
            all_x.extend([line['start'][0], line['end'][0]])
            all_y.extend([line['start'][1], line['end'][1]])
        for polyline in self.polylines:
            for x, y in polyline:
                all_x.append(x)
                all_y.append(y)

        # Include multi-layer geometry in final bounds
        if self.layer_data:
            for layer_name, layer_info in self.layer_data.items():
                for circle in layer_info['circles']:
                    cx, cy = circle['center']
                    r = circle.get('radius') or (circle.get('diameter', 0) / 2)
                    all_x.extend([cx - r, cx + r])
                    all_y.extend([cy - r, cy + r])
                for polyline in layer_info['polylines']:
                    for x, y in polyline:
                        all_x.append(x)
                        all_y.append(y)

        new_minX, new_maxX = min(all_x), max(all_x)
        new_minY, new_maxY = min(all_y), max(all_y)

        print(f"  Transformed bounds: X=[{new_minX:.3f}, {new_maxX:.3f}], Y=[{new_minY:.3f}, {new_maxY:.3f}]")
        print(f"  New origin (0,0) is at the {origin_corner} corner\n")

        # Check if part fits within machine bounds
        part_width = new_maxX - new_minX
        part_height = new_maxY - new_minY
        machine_x_max = self.config.machine_x_max
        machine_y_max = self.config.machine_y_max

        if part_width > machine_x_max or part_height > machine_y_max:
            error_msg = (f"Part dimensions ({part_width:.2f}\" × {part_height:.2f}\") exceed machine bounds "
                        f"({machine_x_max:.1f}\" × {machine_y_max:.1f}\"). "
                        f"Try rotating 90° or reduce part size.")
            self._add_error(error_msg)
            print(f"  ❌ {error_msg}")
    
    def classify_holes(self):
        """Classify holes by diameter"""
        # Classify all circles as holes (apply size check)
        self.holes = []

        for circle in self.circles:
            diameter = circle['diameter']
            center = circle['center']

            # Check if hole is too small to mill with this tool
            if diameter < self.tool_diameter:
                error_msg = f"Hole at ({center[0]:.3f}, {center[1]:.3f}) has diameter {diameter:.3f}\" which is too small for {self.tool_diameter:.3f}\" tool"
                self._add_error(error_msg)
                continue

            # Determine machining strategy based on hole size
            if diameter < self.min_millable_hole:
                # Hole is larger than tool but too small to helical entry
                # Use peck drilling to get down, then spiral clear at bottom
                strategy = 'peck+spiral'
                self.holes.append({'center': center, 'diameter': diameter, 'needs_peck_drill': True})
                print(f"  Hole (d={diameter:.3f}\") at ({center[0]:.3f}, {center[1]:.3f}) - using peck drill + spiral")
            else:
                # Hole is large enough for helical entry
                strategy = 'helical+spiral'
                self.holes.append({'center': center, 'diameter': diameter, 'needs_peck_drill': False})
                print(f"  Hole (d={diameter:.3f}\") at ({center[0]:.3f}, {center[1]:.3f}) - using helical + spiral")

        print(f"\nIdentified {len(self.holes)} millable holes")
        if self.errors:
            print(f"  ❌ {len(self.errors)} error(s) found during hole classification")

        # Sort holes to minimize travel time
        self._sort_holes()

    def _optimize_route(self, items, item_type="items"):
        """
        Generic route optimization using nearest neighbor + 2-opt algorithm.

        This uses a two-phase approach:
        1. Nearest neighbor: Build initial route by always going to closest unvisited item
        2. 2-opt: Optimize route by eliminating crossed paths

        Args:
            items: List of dicts with 'center' key containing (x, y) coordinates
            item_type: String describing the item type for logging (e.g., "holes", "pockets")

        Returns:
            Tuple of (optimized_route, total_distance, num_iterations)
        """
        if len(items) <= 1:
            return items, 0.0, 0

        # Phase 1: Nearest Neighbor Algorithm
        # Start at origin (0, 0) and build route by always going to nearest unvisited item
        unvisited = items.copy()
        route = []
        current_pos = (0, 0)  # Start at origin

        while unvisited:
            # Find nearest unvisited item
            nearest_idx = 0
            nearest_dist = self._distance_2d(current_pos, unvisited[0]['center'])

            for i in range(1, len(unvisited)):
                dist = self._distance_2d(current_pos, unvisited[i]['center'])
                if dist < nearest_dist:
                    nearest_dist = dist
                    nearest_idx = i

            # Add nearest item to route and remove from unvisited
            nearest_item = unvisited.pop(nearest_idx)
            route.append(nearest_item)
            current_pos = nearest_item['center']

        # Phase 2: 2-opt Optimization
        # Try swapping edge pairs to reduce total distance
        improved = True
        max_iterations = 100
        iteration = 0

        while improved and iteration < max_iterations:
            improved = False
            iteration += 1

            for i in range(len(route) - 1):
                for j in range(i + 2, len(route)):
                    # 2-opt: reverse segment from i to j-1
                    # Changes edges: (i-1)→(i) and (j-1)→(j)
                    # Into edges: (i-1)→(j-1) and (i)→(j)

                    # Get the points before and after the segment to reverse
                    if i == 0:
                        point_before = (0, 0)  # Origin
                    else:
                        point_before = route[i - 1]['center']

                    if j < len(route):
                        point_after = route[j]['center']
                    else:
                        point_after = None  # No point after (j is at end)

                    point_i = route[i]['center']
                    point_j_minus_1 = route[j - 1]['center']

                    # Calculate distance before swap
                    dist_before = self._distance_2d(point_before, point_i)
                    if point_after is not None:
                        dist_before += self._distance_2d(point_j_minus_1, point_after)

                    # Calculate distance after swap (reversing segment i to j-1)
                    dist_after = self._distance_2d(point_before, point_j_minus_1)
                    if point_after is not None:
                        dist_after += self._distance_2d(point_i, point_after)

                    # If swap improves distance, do it
                    if dist_after < dist_before:
                        # Reverse the segment from i to j-1
                        route[i:j] = reversed(route[i:j])
                        improved = True

        # Calculate total travel distance
        total_dist = self._distance_2d((0, 0), route[0]['center'])
        for i in range(len(route) - 1):
            total_dist += self._distance_2d(route[i]['center'], route[i + 1]['center'])

        print(f"Optimized {len(route)} {item_type} - total travel: {total_dist:.2f}\" ({iteration} 2-opt iterations)")

        return route, total_dist, iteration

    def _sort_holes(self):
        """Sort holes to minimize tool travel time using nearest neighbor + 2-opt."""
        if len(self.holes) <= 1:
            return

        self.holes, _, _ = self._optimize_route(self.holes, "holes")

    def _sort_pockets(self):
        """
        Sort pockets to minimize tool travel time using nearest neighbor + 2-opt.

        Uses pocket centroids as the position for distance calculations.
        """
        if len(self.pockets) <= 1:
            return

        # Calculate centroid for each pocket (used for distance calculations)
        pocket_data = []
        for pocket_points in self.pockets:
            # Calculate centroid
            sum_x = sum(p[0] for p in pocket_points)
            sum_y = sum(p[1] for p in pocket_points)
            centroid = (sum_x / len(pocket_points), sum_y / len(pocket_points))
            pocket_data.append({'points': pocket_points, 'center': centroid})

        # Optimize route using generic algorithm
        optimized_route, _, _ = self._optimize_route(pocket_data, "pockets")

        # Extract optimized pocket points list
        self.pockets = [p['points'] for p in optimized_route]

    def identify_perimeter_and_pockets(self):
        """Identify the outer perimeter and any inner pockets"""
        # Collect all closed paths: polylines OR circles (converted to polylines)
        # Only use circles as perimeter candidates if there are NO polylines
        all_paths = []
        circle_to_path_map = {}  # Track which circles were converted to paths

        # Add existing polylines
        polyline_count = 0
        if self.polylines:
            all_paths.extend(self.polylines)
            polyline_count = len(self.polylines)

        # Convert circles to polylines ONLY if there are no existing polylines
        # (e.g., for a washer with just two concentric circles)
        if not self.polylines and hasattr(self, 'circles') and self.circles:
            for i, circle in enumerate(self.circles):
                try:
                    cx, cy = circle['center']
                    r = circle.get('radius') or (circle.get('diameter', 0) / 2)
                    if r <= 0:
                        continue
                    # Create polyline from circle (50 points)
                    points = []
                    for j in range(50):
                        angle = (j / 50) * 2 * math.pi
                        x = cx + r * math.cos(angle)
                        y = cy + r * math.sin(angle)
                        points.append((x, y))
                    path_idx = len(all_paths)
                    all_paths.append(points)
                    # Map path index to circle index
                    circle_to_path_map[path_idx] = i
                except (KeyError, TypeError):
                    # Skip circles with missing/invalid data
                    continue

        if not all_paths:
            self.perimeter = None
            self.pockets = []
            return

        # Convert to Shapely polygons, tracking path index
        polygons = []
        for path_idx, points in enumerate(all_paths):
            try:
                poly = Polygon(points)
                if poly.is_valid:
                    polygons.append((poly, points, path_idx))
            except:
                pass

        if not polygons:
            self.perimeter = None
            self.pockets = []
            return

        # Find the largest polygon (perimeter)
        polygons.sort(key=lambda x: x[0].area, reverse=True)
        candidate_perimeter = polygons[0][1]  # Get the original points
        candidate_poly = polygons[0][0]
        perimeter_path_idx = polygons[0][2]

        # Validate that the perimeter is reasonable
        # If we have holes, the perimeter should be significantly larger than the bounding box of holes
        if hasattr(self, 'circles') and self.circles:
            xs = [c['center'][0] for c in self.circles]
            ys = [c['center'][1] for c in self.circles]
            bbox_width = max(xs) - min(xs)
            bbox_height = max(ys) - min(ys)
            bbox_area = bbox_width * bbox_height

            # If the candidate perimeter is < 10% of the bounding box area, it's probably not the real perimeter
            perimeter_area = candidate_poly.area
            if perimeter_area < 0.1 * bbox_area:
                # Only report as error if we had actual polylines (not just converted circles)
                # If we only converted circles and the largest isn't big enough, silently skip perimeter
                if polyline_count > 0:
                    error_msg = f"Perimeter too small ({perimeter_area:.2f} sq in) compared to part bounding box ({bbox_area:.2f} sq in). DXF may be missing perimeter outline geometry."
                    print(f"\n❌ ERROR: {error_msg}")
                    self.errors.append(error_msg)
                self.perimeter = None
                self.pockets = []
                return

        self.perimeter = candidate_perimeter

        # For circles-as-perimeter (no polylines), don't treat other circles as pockets
        # They should remain as holes. Only actual polylines can be pockets.
        if polyline_count > 0:
            # Normal case: we have polylines, add remaining ones as pockets
            self.pockets = [p[1] for p in polygons[1:]]
        else:
            # Circles-only case (like a washer): no pockets, keep inner circles as holes
            self.pockets = []

        # Remove circles that were used as perimeter from self.circles
        circles_to_remove = set()

        # Check if perimeter came from a circle - if so, remove it
        if perimeter_path_idx in circle_to_path_map:
            circles_to_remove.add(circle_to_path_map[perimeter_path_idx])

        # If we created pockets from circles (only when polyline_count > 0), remove those too
        if polyline_count > 0:
            for poly, points, path_idx in polygons[1:]:
                if path_idx in circle_to_path_map:
                    circles_to_remove.add(circle_to_path_map[path_idx])

        # Remove circles in reverse order to avoid index issues
        if circles_to_remove:
            self.circles = [c for i, c in enumerate(self.circles) if i not in circles_to_remove]
            print(f"  Removed {len(circles_to_remove)} circle(s) that were identified as perimeter/pockets")

        print(f"\nIdentified perimeter and {len(self.pockets)} pockets")

        # Sort pockets to minimize travel time
        self._sort_pockets()
    
    def generate_gcode(self, suggested_filename: str = None, timestamp: str = None) -> PostProcessorResult:
        """
        Generate complete G-code for standard plate operations (single or multi-layer)

        Args:
            suggested_filename: Optional filename (without timestamp, will be added)

        Returns:
            PostProcessorResult with gcode string and stats
        """
        # Check for validation errors first
        if self.errors:
            print(f"\n❌ Cannot generate G-code: {len(self.errors)} validation error(s) found")
            for error in self.errors:
                print(f"   - {error}")
            return PostProcessorResult(
                success=False,
                errors=self.errors.copy()
            )

        # Multi-layer processing
        if self.layer_data:
            return self._generate_multilayer_gcode(suggested_filename, timestamp)

        # Use provided timestamp (from client's timezone) or generate one
        if not timestamp:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Generate header
        gcode = self._generate_gcode_header(timestamp, is_multilayer=False)
        warnings = []

        # Holes (all circular features - helical entry + spiral clearing, or contouring for large holes)
        if self.holes:
            gcode.append("(===== HOLES =====)")

            # Get pocket contouring threshold from config (applies to circular holes too)
            contour_threshold = self.config._get('machining', 'pockets', 'contour_threshold', default=50)

            # Check if this is a through-cut (to sacrifice board)
            # Only contour through-cuts; partial-depth features must be fully cleared
            is_through_cut = self.cut_depth <= 0  # At or below Z=0 means cutting into sacrifice board

            # Separate holes into contoured and cleared based on size
            contoured_holes = []
            cleared_holes = []

            for i, hole in enumerate(self.holes, 1):
                center = hole['center']
                diameter = hole['diameter']
                needs_peck = hole.get('needs_peck_drill', False)

                # Calculate hole area: π × r²
                hole_area = math.pi * (diameter / 2) ** 2

                # Calculate threshold area (same formula as pockets)
                threshold_area = (contour_threshold * self.tool_diameter**2 * self.stepover_percentage) if contour_threshold > 0 else float('inf')

                # Only contour if it's a through-cut AND exceeds size threshold
                if is_through_cut and hole_area > threshold_area:
                    contoured_holes.append((i, hole, hole_area))
                    gcode.append(f"(Hole {i} - {diameter:.3f}\" diameter, {hole_area:.3f} sq in > {threshold_area:.3f} sq in threshold - will contour through-cut)")
                else:
                    cleared_holes.append((i, hole, needs_peck))
                    strategy = "peck + spiral" if needs_peck else "helical + spiral"
                    reason = "(partial depth)" if not is_through_cut else ""
                    gcode.append(f"(Hole {i} - {diameter:.3f}\" diameter, {hole_area:.3f} sq in - {strategy} {reason})")

            # Process cleared holes first
            if cleared_holes:
                gcode.append("")
                gcode.append("(--- Cleared holes ---)")
                for i, hole, needs_peck in cleared_holes:
                    center = hole['center']
                    diameter = hole['diameter']
                    gcode.extend(self._generate_hole_gcode(center[0], center[1], diameter, needs_peck_drill=needs_peck))
                    gcode.append("")

            # Process contoured holes (with optional pause for fixturing)
            if contoured_holes:
                # Optional pause before contoured holes for teams using screw fixturing
                # Same logic as perimeter/pockets - any operation with tabs needs secure fixturing
                if self.pause_before_perimeter:
                    gcode.extend(self._generate_pause_and_park_gcode(
                        'PAUSE FOR FIXTURING',
                        [
                            'Cleared holes complete',
                            'Install screws through holes into sacrifice board',
                            'Fixture part securely before contouring large holes'
                        ]
                    ))

                gcode.append("")
                gcode.append("(--- Contoured holes (manual removal required) ---)")
                for i, hole, area in contoured_holes:
                    center = hole['center']
                    diameter = hole['diameter']

                    # Generate circular points for contouring
                    num_points = 50  # Tessellate circle into 50 segments
                    circle_points = []
                    for j in range(num_points):
                        angle = (j / num_points) * 2 * math.pi
                        x = center[0] + (diameter / 2) * math.cos(angle)
                        y = center[1] + (diameter / 2) * math.sin(angle)
                        circle_points.append((x, y))

                    gcode.append(f"(Hole {i} - {diameter:.3f}\" dia, {area:.3f} sq in - CONTOUR ONLY)")
                    gcode.extend(self._generate_pocket_contour_gcode(circle_points))
                    gcode.append("")
        
        # Pockets
        if self.pockets:
            gcode.append("(===== POCKETS =====)")

            # Get pocket contouring threshold from config
            contour_threshold = self.config._get('machining', 'pockets', 'contour_threshold', default=50)

            # Check if this is a through-cut (to sacrifice board)
            # Only contour through-cuts; partial-depth features must be fully cleared
            is_through_cut = self.cut_depth <= 0  # At or below Z=0 means cutting into sacrifice board

            # Separate pockets into contoured and fully cleared based on size
            contoured_pockets = []
            cleared_pockets = []

            for i, pocket in enumerate(self.pockets, 1):
                pocket_poly = Polygon(pocket)
                pocket_area = pocket_poly.area

                # Calculate threshold area: contour_threshold × tool_diameter² / stepover
                # Set contour_threshold to 0 to disable contouring entirely
                threshold_area = (contour_threshold * self.tool_diameter**2 * self.stepover_percentage) if contour_threshold > 0 else float('inf')

                # Only contour if it's a through-cut AND exceeds size threshold
                if is_through_cut and pocket_area > threshold_area:
                    contoured_pockets.append((i, pocket, pocket_area))
                    gcode.append(f"(Pocket {i}: {pocket_area:.3f} sq in > {threshold_area:.3f} sq in threshold - will contour through-cut)")
                else:
                    cleared_pockets.append((i, pocket, pocket_area))
                    reason = "- partial depth" if not is_through_cut else "- below threshold"
                    gcode.append(f"(Pocket {i}: {pocket_area:.3f} sq in - will fully clear {reason})")

            # Process fully cleared pockets first
            if cleared_pockets:
                gcode.append("")
                gcode.append("(--- Fully cleared pockets ---)")
                for i, pocket, area in cleared_pockets:
                    gcode.append(f"(Pocket {i} - {area:.3f} sq in)")
                    gcode.extend(self._generate_pocket_gcode(pocket))
                    gcode.append("")

            # Process contoured pockets (with optional pause for fixturing)
            if contoured_pockets:
                # Optional pause before pocket contours for teams using screw fixturing
                # Same logic as perimeter - any operation with tabs needs secure fixturing
                if self.pause_before_perimeter:
                    gcode.extend(self._generate_pause_and_park_gcode(
                        'PAUSE FOR FIXTURING',
                        [
                            'Cleared pockets complete',
                            'Install screws through holes into sacrifice board',
                            'Fixture part securely before contouring large pockets'
                        ]
                    ))

                gcode.append("")
                gcode.append("(--- Contoured pockets (manual removal required) ---)")
                for i, pocket, area in contoured_pockets:
                    gcode.append(f"(Pocket {i} - {area:.3f} sq in - CONTOUR ONLY)")
                    gcode.extend(self._generate_pocket_contour_gcode(pocket))
                    gcode.append("")
        
        # Perimeter (with optional pause for screw fixturing)
        if self.perimeter:
            # Optional pause before perimeter for teams using screw fixturing
            if self.pause_before_perimeter:
                gcode.extend(self._generate_pause_and_park_gcode(
                    'PAUSE FOR FIXTURING',
                    [
                        'Internal features complete',
                        'Install screws through holes into sacrifice board',
                        'Fixture part securely before perimeter cutting'
                    ]
                ))

            # Generate perimeter header (tabs may or may not be present depending on config)
            if self.tabs_enabled:
                gcode.append("(===== PERIMETER WITH TABS =====)")
            else:
                gcode.append("(===== PERIMETER (NO TABS) =====)")

            gcode.extend(self._generate_perimeter_gcode(self.perimeter))
            gcode.append("")

        # Footer
        gcode.extend(self._generate_gcode_footer())

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

        # Generate filename with timestamp
        base_name = suggested_filename if suggested_filename else "output"
        # Format timestamp for filename: YYYYMMDD_HHMMSS
        timestamp_for_file = timestamp.replace('-', '').replace(' ', '_').replace(':', '')
        filename = f"{base_name}_{timestamp_for_file}.nc"

        # Return result
        return PostProcessorResult(
            success=True,
            gcode='\n'.join(gcode),
            filename=filename,
            warnings=warnings,
            stats={
                'num_holes': len(self.holes) if hasattr(self, 'holes') else 0,
                'num_pockets': len(self.pockets) if hasattr(self, 'pockets') else 0,
                'has_perimeter': bool(self.perimeter) if hasattr(self, 'perimeter') else False,
                'total_lines': len(gcode),
                'cycle_time_seconds': time_estimate['total'],
                'cycle_time_display': self._format_time(time_estimate['total']),
                'cutting_time': self._format_time(time_estimate['cutting']),
                'rapid_time': self._format_time(time_estimate['rapid']),
                'dwell_time': self._format_time(time_estimate['dwell'])
            }
        )

    def _generate_gcode_header(self, timestamp: str = None, is_multilayer: bool = False) -> List[str]:
        """Generate common G-code header (comments + initialization)"""
        gcode = []

        # Use provided timestamp or generate one
        if not timestamp:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        timestamp_display = timestamp[:16]

        # Title
        gcode.append(f"({self.team_name.upper()} - Team {self.team_number})")
        if is_multilayer:
            gcode.append("(PenguinCAM CNC Post-Processor - MULTI-LAYER)")
        else:
            gcode.append("(PenguinCAM CNC Post-Processor)")

        if hasattr(self, 'user_name'):
            gcode.append(f"(Generated by: {self.user_name} on {timestamp_display})")
        else:
            gcode.append(f"(Generated on: {timestamp_display})")
        gcode.append("")

        # Machine info (sanitize to avoid nested parentheses in G-code comments)
        machine_name = self.machine_name.replace('(', '[').replace(')', ']')
        controller = self.machine_controller.replace('(', '[').replace(')', ']')

        gcode.append(f"(Machine: {machine_name})")
        gcode.append(f"(Controller: {controller})")

        if not is_multilayer:
            gcode.append(f"(Machine bounds: X={self.config.machine_x_max:.1f}\" Y={self.config.machine_y_max:.1f}\" Z={self.config.machine_z_max:.1f}\")")

        gcode.append(f"(Units: {'Inches' if self.units == 'inch' else 'Millimeters'}" +
                    (f" - {'G20' if self.units == 'inch' else 'G21'})" if not is_multilayer else ")"))

        if not is_multilayer:
            gcode.append("(Coordinate system: G54)")
            gcode.append("(Plane: G17 - XY)")
            gcode.append("(Arc centers: Incremental - G91.1)")

        gcode.append("")

        # Material and tool
        material_info = f"{self.material_thickness}\""
        if hasattr(self, 'material_name'):
            material_info = f"{self.material_name} - {material_info} thick"
        else:
            material_info = f"{material_info} thick"

        gcode.append(f"(Material: {material_info})")
        gcode.append(f"(Tool: {self.tool_diameter}\" diam Flat End Mill)")
        gcode.append(f"(Spindle: {self.spindle_speed} RPM)")

        if is_multilayer:
            if hasattr(self, 'layer_data'):
                gcode.append(f"(Layers: {len(self.layer_data)} depths)")
        else:
            gcode.append(f"(Coolant: {self.machine_coolant})")

        gcode.append("")

        if not is_multilayer:
            # Z-axis info (only for single-layer)
            gcode.append(f"(ZMIN: {self.cut_depth:.4f}\")")
            gcode.append(f"(Retract Z: {self.retract_height:.4f}\")")
            gcode.append("")

            # Operations
            operations = []
            if self.holes:
                operations.append("Holes")
            if self.pockets:
                operations.append("Pockets")
            if self.perimeter:
                operations.append("Profile")
            operations_str = ", ".join(operations) if operations else "None"

            helical_angle = f"~{int(self.ramp_angle)} deg"

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

        # Modal G-code setup
        gcode.append("G90 G94 G91.1 G40 G49 G17")

        if not is_multilayer:
            gcode.append("(G90=Absolute, G94=Feed/min, G91.1=Arc centers incremental - IJK relative to start point, G40=Cutter comp cancel, G49=Tool length comp cancel, G17=XY plane)")

        # Units
        if self.units == "inch":
            gcode.append("G20  ; Inches")
        else:
            gcode.append("G21  ; Millimeters")

        # Ensure absolute positioning mode
        gcode.append("G90  ; Absolute positioning mode")
        gcode.append("")

        # Spindle on
        gcode.append(f"S{self.spindle_speed} M3  ; Spindle on" + ("" if is_multilayer else f" at {self.spindle_speed} RPM"))
        gcode.append("M7  ; Air blast on")
        gcode.append("G4 P2  ; Wait" + (" for spindle" if is_multilayer else " 2 seconds for spindle to reach speed"))
        gcode.append("")

        # Set work coordinate system
        gcode.append("G54  ; " + ("Work coordinate system" if is_multilayer else "Use work coordinate system 1"))
        gcode.append("")

        # Initial positioning (stay high to avoid fixture collisions during XY moves)
        gcode.append(f"G53 G0 Z{self.machine_park_z:.4f}  ; " + ("Safe Z clearance" if is_multilayer else "Move to safe machine Z clearance"))
        gcode.append("G0 X0 Y0  ; " + ("Origin" if is_multilayer else "Rapid to work origin"))
        gcode.append("")

        return gcode

    def _generate_gcode_footer(self) -> List[str]:
        """Generate common G-code footer (safe moves + shutdown)"""
        gcode = []
        gcode.append("(===== FINISH =====)")
        gcode.append(f"G53 G0 Z{self.machine_park_z:.4f}  ; Move to safe machine Z clearance")
        gcode.append("M9  ; Air blast off")
        gcode.append("M5  ; Spindle off")
        gcode.append(f"G53 G0 X{self.machine_park_x} Y{self.machine_park_y}  ; Move gantry to back of machine for easy access")
        gcode.append("M30  ; Program end")
        gcode.append("")
        return gcode

    def _convert_to_shapely_polygons(self, circles, polylines):
        """
        Convert circles and polylines to Shapely Polygon objects.
        Handles HATCH entities (multiple boundaries = polygon with holes).
        Detects concentric circles and creates ring polygons.

        Args:
            circles: List of circle dicts with 'center' and 'radius'
            polylines: List of polyline coordinate lists

        Returns:
            List of Shapely Polygon objects (may have interior holes)
        """
        polygons = []

        # Detect concentric circles (same center, different radii)
        # These should become ring polygons (donut shapes with holes)
        used_circles = set()

        for i, circle1 in enumerate(circles):
            if i in used_circles:
                continue

            center1 = circle1['center']
            radius1 = circle1['radius']

            # Look for concentric circles
            concentric_group = [circle1]
            for j, circle2 in enumerate(circles):
                if i == j or j in used_circles:
                    continue

                center2 = circle2['center']
                radius2 = circle2['radius']

                # Check if centers are the same (within tolerance)
                dx = abs(center1[0] - center2[0])
                dy = abs(center1[1] - center2[1])
                if dx < 0.001 and dy < 0.001:
                    # Concentric!
                    concentric_group.append(circle2)
                    used_circles.add(j)

            used_circles.add(i)

            # Create polygon(s) from this group
            if len(concentric_group) == 1:
                # Single circle - simple filled polygon
                poly = Point(center1).buffer(radius1)
                polygons.append(poly)
            else:
                # Multiple concentric circles - create ring with holes
                # Sort by radius (largest first)
                concentric_group.sort(key=lambda c: c['radius'], reverse=True)

                # Outer boundary is the largest circle
                outer_circle = concentric_group[0]
                outer_poly = Point(outer_circle['center']).buffer(outer_circle['radius'])

                # Interior holes are the other circles
                holes = []
                for inner_circle in concentric_group[1:]:
                    inner_poly = Point(inner_circle['center']).buffer(inner_circle['radius'])
                    # Get exterior coords as hole
                    hole_coords = list(inner_poly.exterior.coords)
                    holes.append(hole_coords)

                # Create polygon with holes
                outer_coords = list(outer_poly.exterior.coords)
                ring_poly = Polygon(outer_coords, holes=holes)
                if ring_poly.is_valid:
                    polygons.append(ring_poly)
                    print(f"      Detected concentric circles: outer r={outer_circle['radius']:.3f}\", {len(holes)} inner hole(s)")

        # Convert polylines to polygons
        # Special handling: if we have exactly 2 polylines and one is inside the other,
        # create a polygon with a hole (e.g., from HATCH with island)
        if len(polylines) == 2:
            try:
                poly1 = Polygon(polylines[0])
                poly2 = Polygon(polylines[1])

                if poly1.is_valid and poly2.is_valid:
                    # Check if one contains the other
                    if poly1.contains(poly2):
                        # poly1 is outer, poly2 is inner hole
                        poly_with_hole = Polygon(polylines[0], holes=[polylines[1]])
                        if poly_with_hole.is_valid:
                            polygons.append(poly_with_hole)
                            return polygons
                    elif poly2.contains(poly1):
                        # poly2 is outer, poly1 is inner hole
                        poly_with_hole = Polygon(polylines[1], holes=[polylines[0]])
                        if poly_with_hole.is_valid:
                            polygons.append(poly_with_hole)
                            return polygons
            except:
                pass

        # Default: treat each polyline as separate polygon
        for polyline in polylines:
            if len(polyline) >= 3:
                try:
                    poly = Polygon(polyline)
                    if poly.is_valid:
                        polygons.append(poly)
                except:
                    pass

        return polygons

    def _geometries_to_shapely(self, circles, polylines):
        """Convert circles and polylines to shapely geometries"""
        geoms = []

        # Convert circles to polygons (buffered points)
        for circle in circles:
            center = circle['center']
            radius = circle['radius']
            geoms.append(Point(center).buffer(radius))

        # Convert polylines to polygons
        for polyline in polylines:
            if len(polyline) >= 3:
                try:
                    poly = Polygon(polyline)
                    if poly.is_valid:
                        geoms.append(poly)
                except:
                    pass

        if geoms:
            return unary_union(geoms)
        return None

    def _subtract_geometry(self, circles, polylines, cut_geometry):
        """
        Subtract geometry from circles and polylines.
        Used to remove areas that will be (or have been) cut by other operations.
        Returns new lists with subtracted geometry.
        """
        if cut_geometry is None or cut_geometry.is_empty:
            return circles, polylines

        new_circles = []
        new_polylines = []

        # Process circles
        for circle in circles:
            center = circle['center']
            radius = circle['radius']
            circle_geom = Point(center).buffer(radius)

            # Subtract already cut areas
            result = circle_geom.difference(cut_geometry)

            # If circle is completely covered by cut geometry, skip it
            if result.is_empty or result.area < 0.0001:
                print(f"    Circle at {center} fully removed by subtraction - skipping")
                continue

            # If circle remains mostly intact (>90% area), keep it as-is
            if result.area / circle_geom.area > 0.9:
                new_circles.append(circle)
            else:
                # Circle partially overlaps - convert remainder to polyline(s)
                print(f"    Circle at {center} partially overlaps - converting to polyline")
                if isinstance(result, Polygon):
                    coords = list(result.exterior.coords)[:-1]
                    if len(coords) >= 3:
                        new_polylines.append(coords)
                elif isinstance(result, MultiPolygon):
                    for poly in result.geoms:
                        coords = list(poly.exterior.coords)[:-1]
                        if len(coords) >= 3:
                            new_polylines.append(coords)

        # Process polylines
        for polyline in polylines:
            if len(polyline) < 3:
                continue

            try:
                poly = Polygon(polyline)
                if not poly.is_valid:
                    continue

                # Subtract already cut areas
                result = poly.difference(cut_geometry)

                # If completely covered by cut geometry, skip
                if result.is_empty or result.area < 0.0001:
                    print(f"    Polyline fully removed by subtraction - skipping")
                    continue

                # Extract remaining geometry
                if isinstance(result, Polygon):
                    coords = list(result.exterior.coords)[:-1]
                    if len(coords) >= 3:
                        new_polylines.append(coords)
                    # Also add holes as separate polylines
                    for interior in result.interiors:
                        coords = list(interior.coords)[:-1]
                        if len(coords) >= 3:
                            new_polylines.append(coords)
                elif isinstance(result, MultiPolygon):
                    for poly in result.geoms:
                        coords = list(poly.exterior.coords)[:-1]
                        if len(coords) >= 3:
                            new_polylines.append(coords)
                        for interior in poly.interiors:
                            coords = list(interior.coords)[:-1]
                            if len(coords) >= 3:
                                new_polylines.append(coords)
            except Exception as e:
                # If subtraction fails, keep original polyline
                print(f"    Warning: Could not subtract from polyline: {e}")
                new_polylines.append(polyline)

        print(f"    Before: {len(circles)} circles, {len(polylines)} polylines")
        print(f"    After:  {len(new_circles)} circles, {len(new_polylines)} polylines")

        return new_circles, new_polylines

    def _generate_multilayer_gcode(self, suggested_filename: str = None, timestamp: str = None) -> PostProcessorResult:
        """Generate G-code for multi-layer DXF (2.5D machining)"""
        print("\n" + "="*70)
        print("MULTI-LAYER PROCESSING")
        print("="*70)

        # Sort layers: deepest first
        sorted_layers = sorted(self.layer_data.items(), key=lambda x: x[1]['depth'])

        # Find the bottom face layer
        # NEW COORDINATE SYSTEM: Z=0 is sacrifice board surface
        # Bottom face should be at Z ≈ 0, top face at Z ≈ material_thickness
        expected_bottom_depth = 0.0
        tolerance = 0.01  # 0.01" tolerance for matching bottom face

        bottom_layer = None
        bottom_layer_name = None

        # Look for a layer at the expected bottom depth (Z ≈ 0)
        for layer_name, layer_info in self.layer_data.items():
            if abs(layer_info['depth'] - expected_bottom_depth) < tolerance:
                bottom_layer = (layer_name, layer_info)
                bottom_layer_name = layer_name
                print(f"✓ Found bottom face layer: {layer_name} at Z={layer_info['depth']:.4f}\" (expected {expected_bottom_depth:.4f}\")")
                break

        if not bottom_layer:
            # No bottom face found - use lowest Z layer for perimeter extraction only
            # ALL layers (including lowest) are treated as depth layers for pockets/holes
            lowest_layer = min(self.layer_data.items(), key=lambda x: x[1]['depth'])
            bottom_layer = lowest_layer
            bottom_layer_name = lowest_layer[0]
            print(f"⚠️  No bottom face at Z={expected_bottom_depth:.4f}\" found in DXF")
            print(f"   Using lowest layer {bottom_layer_name} at Z={lowest_layer[1]['depth']:.4f}\" for perimeter outline")
            print(f"   All layers (including lowest) will be processed as depth layers")

        # Separate layers: pocket layers (excluding bottom face and top surface)
        # - Bottom face layer (at Z ≈ 0): used for perimeter + through-holes/pockets
        # - Top surface layer (at Z ≈ material_thickness): reference geometry, not machined
        # - Middle layers (0 < Z < material_thickness): actual pockets/grooves to machine at specified depth
        # - If no true bottom face exists, ALL layers become depth layers
        has_true_bottom = abs(bottom_layer[1]['depth'] - expected_bottom_depth) < tolerance

        if has_true_bottom:
            # Exclude bottom face from depth layers
            # Process layers where 0 < Z < material_thickness (intermediate pockets)
            depth_layers = [
                item for item in sorted_layers
                if item[0] != bottom_layer_name and 0.01 < item[1]['depth'] < self.material_thickness - 0.01
            ]
        else:
            # Include all valid layers as depth layers
            # Process layers where 0 < Z < material_thickness
            depth_layers = [
                item for item in sorted_layers
                if 0.01 < item[1]['depth'] < self.material_thickness - 0.01
            ]

        print(f"\nProcessing order:")
        for i, (layer_name, layer_info) in enumerate(depth_layers, 1):
            if has_true_bottom or layer_name != bottom_layer_name:
                print(f"  {i}. {layer_name} (Z={layer_info['depth']:.4f}\") - pocket/groove at specified depth")

        # Report skipped layers
        for layer_name, layer_info in sorted_layers:
            # Skip if it's the bottom layer (handled separately) OR if it's at/above material thickness (top reference)
            if layer_name != bottom_layer_name and layer_info['depth'] >= self.material_thickness - 0.01:
                print(f"  → Skipping {layer_name} (Z={layer_info['depth']:.4f}\") - top surface reference geometry")

        if has_true_bottom:
            print(f"  {len(depth_layers) + 1}. {bottom_layer_name} (Z={bottom_layer[1]['depth']:.4f}\") - PERIMETER + through-holes/pockets (last)")
        else:
            print(f"  {len(depth_layers) + 1}. {bottom_layer_name} - PERIMETER OUTLINE ONLY (already processed pockets at specified depth)")

        # Generate timestamp if not provided
        if not timestamp:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Generate header
        gcode = self._generate_gcode_header(timestamp, is_multilayer=True)
        warnings = []

        # Track total features across all layers
        total_holes = 0
        total_pockets = 0

        # Process each depth layer (deepest to shallowest, excluding top/perimeter)
        for layer_name, layer_info in depth_layers:
            depth = layer_info['depth']
            print(f"\nGenerating toolpaths for {layer_name} at Z={depth:.4f}\"")

            gcode.append(f"(===== LAYER: {layer_name} | DEPTH: Z={depth:.4f}\" =====)")

            # === SHAPELY POLYGON APPROACH ===
            # Get Shapely Polygons for this layer
            current_polygons = layer_info['polygons']
            print(f"  Current layer: {len(current_polygons)} polygon(s)")

            # === PROPER 3D SLICING LOGIC ===
            # Find the next deeper layer
            next_deeper_layer = None
            next_deeper_depth = None
            for check_name, check_info in self.layer_data.items():
                check_depth = check_info['depth']
                if check_depth < depth - 0.001:  # Deeper than current
                    if next_deeper_depth is None or check_depth > next_deeper_depth:
                        next_deeper_layer = check_name
                        next_deeper_depth = check_depth

            # If no explicit deeper layer, check if bottom face exists and is deeper
            if has_true_bottom and bottom_layer[1]['depth'] < depth - 0.001:
                if next_deeper_depth is None or bottom_layer[1]['depth'] > next_deeper_depth:
                    next_deeper_layer = bottom_layer_name
                    next_deeper_depth = bottom_layer[1]['depth']

            # Get geometry at next deeper layer and perform slicing
            if next_deeper_layer:
                next_layer_info = self.layer_data[next_deeper_layer]
                deeper_polygons = next_layer_info['polygons']
                print(f"  Next deeper layer: {next_deeper_layer} at Z={next_deeper_depth:.4f}\"")
                print(f"    Deeper geometry: {len(deeper_polygons)} polygon(s)")

                # For bottom face: exclude perimeter from subtraction
                # The perimeter is the outermost boundary (part outline), not cleared material
                if next_deeper_layer == bottom_layer_name and has_true_bottom:
                    # Identify which polygon is the perimeter (largest area)
                    if deeper_polygons:
                        perimeter_polygon = max(deeper_polygons, key=lambda p: p.area)
                        # Exclude perimeter, keep only interior features (holes/pockets)
                        deeper_polygons_for_subtraction = [p for p in deeper_polygons if p != perimeter_polygon]
                        print(f"    Excluding perimeter from subtraction (area={perimeter_polygon.area:.3f} sq in)")
                        print(f"    Using {len(deeper_polygons_for_subtraction)} interior feature(s) for subtraction")
                    else:
                        deeper_polygons_for_subtraction = []
                else:
                    deeper_polygons_for_subtraction = deeper_polygons

                # SLICING: material_to_machine = current_solid - deeper_solid
                if not deeper_polygons_for_subtraction:
                    # No geometry to subtract
                    result = unary_union(current_polygons) if len(current_polygons) > 1 else (current_polygons[0] if current_polygons else None)
                else:
                    current_union = unary_union(current_polygons) if len(current_polygons) > 1 else current_polygons[0]
                    deeper_union = unary_union(deeper_polygons_for_subtraction) if len(deeper_polygons_for_subtraction) > 1 else deeper_polygons_for_subtraction[0]
                    result = current_union.difference(deeper_union)

                # Convert result back to list of polygons
                if result.is_empty:
                    sliced_polygons = []
                    print(f"  Result: No material to machine (fully covered by deeper layer)")
                elif isinstance(result, Polygon):
                    sliced_polygons = [result]
                    print(f"  Result: 1 polygon to machine")
                elif isinstance(result, MultiPolygon):
                    sliced_polygons = list(result.geoms)
                    print(f"  Result: {len(sliced_polygons)} polygons to machine")
                else:
                    # Other geometry types (unlikely but handle gracefully)
                    sliced_polygons = []
                    print(f"  Result: Unexpected geometry type {result.geom_type}")
            else:
                # No deeper layer - use all polygons as-is
                sliced_polygons = current_polygons
                print(f"  No deeper geometry to subtract - using all features as-is")

            # Convert Shapely Polygons back to circles/polylines for existing toolpath generation
            # Store Polygons with islands separately for special handling
            self.circles = []
            self.polylines = []
            self.pocket_polygons = []  # NEW: Store Polygon objects for island-aware machining

            for poly in sliced_polygons:
                if not poly.is_valid or poly.is_empty:
                    continue

                # Check if polygon has interior holes (islands)
                if len(poly.interiors) > 0:
                    # Store the complete Polygon object for island-aware machining
                    self.pocket_polygons.append(poly)
                    print(f"    Polygon with {len(poly.interiors)} interior hole(s) - will be machined as ring/pocket with islands")
                else:
                    # Simple polygon - extract as polyline
                    exterior_coords = list(poly.exterior.coords)[:-1]  # Remove duplicate last point
                    if len(exterior_coords) >= 3:
                        self.polylines.append(exterior_coords)

            print(f"  Converted to {len(self.circles)} circles, {len(self.polylines)} polylines, {len(self.pocket_polygons)} island-aware pockets")

            # Classify geometry (holes, pockets) - reuses existing methods
            self.classify_holes()

            # For depth layers: treat ALL polylines as pockets (no perimeter)
            self.perimeter = None
            self.pockets = self.polylines.copy() if self.polylines else []

            # NEW COORDINATE SYSTEM: DXF depths are already in machine coordinates
            # Z=0 is sacrifice board in both DXF and G-code
            # Pocket at DXF Z=0.050" → cut to machine Z=0.050"
            saved_cut_depth = self.cut_depth
            self.cut_depth = depth

            # Generate toolpaths at this depth
            # Apply same contouring logic as 2D mode
            contour_threshold = self.config._get('machining', 'pockets', 'contour_threshold', default=510)
            threshold_area = (contour_threshold * self.tool_diameter**2 * self.stepover_percentage) if contour_threshold > 0 else float('inf')

            # Check if this layer is a through-cut (at or below Z=0)
            # Only contour through-cuts; partial-depth layers must be fully cleared
            is_through_cut = self.cut_depth <= 0

            if self.holes:
                gcode.append(f"(Layer {layer_name}: {len(self.holes)} holes)")
                total_holes += len(self.holes)

                # Separate holes by size (only contour through-cuts)
                contoured_holes = []
                cleared_holes = []
                for hole in self.holes:
                    hole_area = math.pi * (hole['diameter'] / 2) ** 2
                    if is_through_cut and hole_area > threshold_area:
                        contoured_holes.append(hole)
                    else:
                        cleared_holes.append(hole)

                # Process cleared holes
                for hole in cleared_holes:
                    center = hole['center']
                    diameter = hole['diameter']
                    needs_peck = hole.get('needs_peck_drill', False)
                    gcode.extend(self._generate_hole_gcode(center[0], center[1], diameter, needs_peck_drill=needs_peck))

                # Process contoured holes
                for hole in contoured_holes:
                    center = hole['center']
                    diameter = hole['diameter']
                    # Generate circular points for contouring
                    num_points = 50
                    circle_points = []
                    for j in range(num_points):
                        angle = (j / num_points) * 2 * math.pi
                        x = center[0] + (diameter / 2) * math.cos(angle)
                        y = center[1] + (diameter / 2) * math.sin(angle)
                        circle_points.append((x, y))
                    gcode.append(f"(Large hole {diameter:.3f}\" dia - CONTOUR ONLY)")
                    gcode.extend(self._generate_pocket_contour_gcode(circle_points))

            if self.pockets:
                gcode.append(f"(Layer {layer_name}: {len(self.pockets)} pockets)")
                total_pockets += len(self.pockets)

                # Separate pockets by size (only contour through-cuts)
                contoured_pockets = []
                cleared_pockets = []
                for pocket in self.pockets:
                    pocket_poly = Polygon(pocket)
                    pocket_area = pocket_poly.area
                    if is_through_cut and pocket_area > threshold_area:
                        contoured_pockets.append(pocket)
                    else:
                        cleared_pockets.append(pocket)

                # Process cleared pockets
                for pocket in cleared_pockets:
                    gcode.extend(self._generate_pocket_gcode(pocket))

                # Process contoured pockets
                for pocket in contoured_pockets:
                    pocket_poly = Polygon(pocket)
                    gcode.append(f"(Large pocket {pocket_poly.area:.3f} sq in - CONTOUR ONLY)")
                    gcode.extend(self._generate_pocket_contour_gcode(pocket))

            # Process island-aware pockets (Polygons with interior holes)
            if hasattr(self, 'pocket_polygons') and self.pocket_polygons:
                gcode.append(f"(Layer {layer_name}: {len(self.pocket_polygons)} island-aware pockets)")
                total_pockets += len(self.pocket_polygons)

                for pocket_poly in self.pocket_polygons:
                    # For now, these are always cleared (no contouring for rings/grooves)
                    # In the future, could add size threshold check here too
                    gcode.append(f"(Ring/groove pocket with {len(pocket_poly.interiors)} islands)")
                    gcode.extend(self._generate_pocket_gcode_from_polygon(pocket_poly))

            # Restore original cut depth
            self.cut_depth = saved_cut_depth
            gcode.append("")

        # Process bottom face for perimeter
        layer_name, layer_info = bottom_layer
        depth = layer_info['depth']
        print(f"\nGenerating perimeter from {layer_name}")
        if has_true_bottom:
            print(f"  Bottom face is at Z={depth:.4f}\" - cutting perimeter through material")
        else:
            print(f"  Using deepest layer Z={depth:.4f}\" for perimeter outline only")

        gcode.append(f"(===== LAYER: {layer_name} | PERIMETER =====)")

        # Use bottom face geometry
        self.circles = layer_info['circles']
        self.polylines = layer_info['polylines']

        # Identify perimeter from bottom face (must come before classify_holes to remove perimeter circles)
        self.identify_perimeter_and_pockets()

        # Classify remaining circles as holes (after perimeter circles removed)
        self.classify_holes()

        # Generate holes and pockets ONLY if this is a true bottom face (through-cuts)
        # Otherwise they were already processed as depth layers
        if has_true_bottom:
            gcode.append(f"(Bottom face at Z={depth:.4f}\" - through-holes and through-pockets)")

            # Apply contouring logic to bottom face (same as depth layers)
            contour_threshold = self.config._get('machining', 'pockets', 'contour_threshold', default=510)
            threshold_area = (contour_threshold * self.tool_diameter**2 * self.stepover_percentage) if contour_threshold > 0 else float('inf')

            # Bottom face is always through-cut (Z=0)
            is_through_cut = True

            if self.holes:
                gcode.append("(===== HOLES =====)")
                total_holes += len(self.holes)

                # Separate holes by size (only contour through-cuts)
                contoured_holes = []
                cleared_holes = []
                for hole in self.holes:
                    hole_area = math.pi * (hole['diameter'] / 2) ** 2
                    if is_through_cut and hole_area > threshold_area:
                        contoured_holes.append(hole)
                    else:
                        cleared_holes.append(hole)

                # Process cleared holes
                for i, hole in enumerate(cleared_holes, 1):
                    center = hole['center']
                    diameter = hole['diameter']
                    needs_peck = hole.get('needs_peck_drill', False)
                    gcode.append(f"(Hole {i} - {diameter:.3f}\" diameter)")
                    gcode.extend(self._generate_hole_gcode(center[0], center[1], diameter, needs_peck_drill=needs_peck))
                    gcode.append("")

                # Process contoured holes
                for i, hole in enumerate(contoured_holes, 1):
                    center = hole['center']
                    diameter = hole['diameter']
                    # Generate circular points for contouring
                    num_points = 50
                    circle_points = []
                    for j in range(num_points):
                        angle = (j / num_points) * 2 * math.pi
                        x = center[0] + (diameter / 2) * math.cos(angle)
                        y = center[1] + (diameter / 2) * math.sin(angle)
                        circle_points.append((x, y))
                    gcode.append(f"(Hole {len(cleared_holes) + i} - {diameter:.3f}\" diameter - CONTOUR ONLY)")
                    gcode.extend(self._generate_pocket_contour_gcode(circle_points))
                    gcode.append("")

            if self.pockets:
                gcode.append("(===== POCKETS =====)")
                total_pockets += len(self.pockets)

                # Separate pockets by size (only contour through-cuts)
                contoured_pockets = []
                cleared_pockets = []
                for pocket in self.pockets:
                    pocket_poly = Polygon(pocket)
                    pocket_area = pocket_poly.area
                    if is_through_cut and pocket_area > threshold_area:
                        contoured_pockets.append(pocket)
                    else:
                        cleared_pockets.append(pocket)

                # Process cleared pockets
                for i, pocket in enumerate(cleared_pockets, 1):
                    gcode.append(f"(Pocket {i})")
                    gcode.extend(self._generate_pocket_gcode(pocket))
                    gcode.append("")

                # Process contoured pockets
                for i, pocket in enumerate(contoured_pockets, 1):
                    pocket_poly = Polygon(pocket)
                    gcode.append(f"(Pocket {len(cleared_pockets) + i} - {pocket_poly.area:.3f} sq in - CONTOUR ONLY)")
                    gcode.extend(self._generate_pocket_contour_gcode(pocket))
                    gcode.append("")
        else:
            gcode.append(f"(Perimeter outline from deepest layer - holes/pockets already cut at depth)")

        # Perimeter cut at full depth
        if self.perimeter:
            if self.pause_before_perimeter:
                gcode.extend(self._generate_pause_and_park_gcode(
                    'PAUSE FOR FIXTURING',
                    ['Internal features complete', 'Install fixturing', 'Secure part before perimeter']
                ))

            if self.tabs_enabled:
                gcode.append("(Perimeter with tabs)")
            else:
                gcode.append("(Perimeter - no tabs)")

            gcode.extend(self._generate_perimeter_gcode(self.perimeter))

        gcode.append("")

        # Footer
        gcode.extend(self._generate_gcode_footer())

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

        # Check for errors that occurred during generation
        if self.errors:
            return PostProcessorResult(
                success=False,
                errors=self.errors.copy()
            )

        # Generate filename
        base_name = suggested_filename if suggested_filename else "output"
        timestamp_for_file = timestamp.replace('-', '').replace(' ', '_').replace(':', '')
        filename = f"{base_name}_{timestamp_for_file}.nc"

        return PostProcessorResult(
            success=True,
            gcode='\n'.join(gcode),
            filename=filename,
            warnings=warnings,
            stats={
                'num_holes': total_holes,
                'num_pockets': total_pockets,
                'has_perimeter': bool(self.perimeter) if hasattr(self, 'perimeter') else False,
                'num_layers': len(self.layer_data),
                'total_lines': len(gcode),
                'cycle_time_seconds': time_estimate['total'],
                'cycle_time_display': self._format_time(time_estimate['total']),
                'cutting_time': self._format_time(time_estimate['cutting']),
                'rapid_time': self._format_time(time_estimate['rapid']),
                'dwell_time': self._format_time(time_estimate['dwell'])
            }
        )

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

    def _generate_peck_drill_and_spiral_gcode(self, cx: float, cy: float, diameter: float, final_toolpath_radius: float) -> List[str]:
        """
        Generate G-code for small holes using G83 peck drilling + spiral clearing.

        For holes that are larger than the tool but too small to helical entry into,
        we peck drill straight down to full depth, then do a single spiral clearing
        pass at the bottom to open the hole to the final diameter.

        Args:
            cx, cy: Hole center coordinates
            diameter: Hole diameter (from CAD)
            final_toolpath_radius: Target toolpath radius for spiral clearing
        """
        gcode = []

        # Peck drilling parameters (from material settings)
        peck_depth = self.peck_drill_depth
        retract_plane = self.material_top + 0.1  # 0.1" above stock for chip clearing (not full retract)
        final_depth = self.cut_depth  # Bottom of cut (negative value)

        gcode.append(f"(Peck drill at center, then spiral clear to {diameter:.3f}\" diameter)")

        # Rapid to hole center above material
        gcode.append(f"G0 X{cx:.4f} Y{cy:.4f}  ; Rapid to hole center")
        gcode.append(f"G0 Z{retract_plane:.4f}  ; Move to retract plane")

        # G83 peck drilling cycle
        # Format: G83 X__ Y__ Z__ R__ Q__ F__
        # Z = final depth (negative), R = retract plane, Q = peck depth, F = plunge rate
        gcode.append(f"G83 X{cx:.4f} Y{cy:.4f} Z{final_depth:.4f} R{retract_plane:.4f} Q{peck_depth:.4f} F{self.plunge_rate}  ; Peck drill to full depth")
        gcode.append("G80  ; Cancel canned cycle")

        # Now at bottom of hole, do spiral clearing pass to open to final diameter
        # Start from center and spiral outward
        stepover = self.tool_diameter * self.stepover_percentage

        # We're already at center at full depth, so start spiraling immediately
        gcode.append(f"(Spiral clear from center to r={final_toolpath_radius:.4f}\")")

        # Calculate spiral parameters for clearing from center to final radius
        spiral_constant = stepover / (2 * math.pi)
        total_angle = final_toolpath_radius / spiral_constant if spiral_constant > 0 else 0

        # Generate spiral points from center outward
        angle_increment = math.radians(10)  # 10 degrees per segment
        num_points = int(math.ceil(total_angle / angle_increment))

        # Spiral outward from center (r=0) to final radius
        for i in range(1, num_points + 1):  # Start at i=1 to avoid staying at center
            current_angle = i * angle_increment
            current_radius = spiral_constant * current_angle
            current_radius = min(current_radius, final_toolpath_radius)  # Don't exceed target

            # Convert polar to Cartesian
            x = cx + current_radius * math.cos(current_angle)
            y = cy + current_radius * math.sin(current_angle)

            gcode.append(f"G1 X{x:.4f} Y{y:.4f} F{self.feed_rate}")

        # Final cleanup pass at exact final radius (full circle)
        final_x = cx + final_toolpath_radius
        final_y = cy
        gcode.append(f"G1 X{final_x:.4f} Y{final_y:.4f} F{self.feed_rate}  ; Move to final radius")
        gcode.append(f"G3 X{final_x:.4f} Y{final_y:.4f} I{-final_toolpath_radius:.4f} J0 F{self.feed_rate}  ; Final cleanup circle CCW for climb milling")

        # Spring pass: repeat the final circle at zero stepover to relieve tool
        # deflection that left the hole slightly undersized.
        gcode.append(f"(Spring pass - compensate for tool deflection)")
        gcode.append(f"G3 X{final_x:.4f} Y{final_y:.4f} I{-final_toolpath_radius:.4f} J0 F{self.feed_rate}  ; Spring pass at final radius")

        # Retract
        gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract")

        return gcode

    def _generate_hole_gcode(self, cx: float, cy: float, diameter: float, needs_peck_drill: bool = False) -> List[str]:
        """
        Generate G-code for a hole using helical entry + spiral-out strategy,
        or peck drilling + spiral for small holes.

        Args:
            cx, cy: Hole center coordinates
            diameter: Hole diameter (from CAD)
            needs_peck_drill: If True, use G83 peck drilling instead of helical entry
        """
        gcode = []

        # Calculate target toolpath radius (hole radius minus tool radius for inside cut)
        hole_radius = diameter / 2
        final_toolpath_radius = hole_radius - self.tool_radius

        if final_toolpath_radius <= 0:
            gcode.append(f"(WARNING: Tool diameter {self.tool_diameter:.4f}\" is too large for {diameter:.4f}\" hole!)")
            return gcode

        # If hole is too small for helical entry, use peck drilling to get down
        if needs_peck_drill:
            return self._generate_peck_drill_and_spiral_gcode(cx, cy, diameter, final_toolpath_radius)

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
        gcode.append(f"(Helical entry: {num_helical_passes} passes at {self.ramp_angle} deg, {depth_per_pass:.4f}\" per pass)")
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

        # Spring pass: repeat the final circle at zero stepover to relieve tool
        # deflection that left the hole slightly undersized.
        gcode.append(f"(Spring pass - compensate for tool deflection)")
        gcode.append(f"G3 X{final_x:.4f} Y{final_y:.4f} I{-final_toolpath_radius:.4f} J0 F{self.feed_rate}  ; Spring pass at final radius")

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
            center_x, center_y = self._get_polygon_center(pocket_poly)
            error_msg = f"Pocket at approximately ({center_x:.3f}, {center_y:.3f}) is too small for {self.tool_diameter:.4f}\" tool - tool cannot fit inside with proper clearance"
            self._add_error(error_msg)
            return gcode

        # Get the boundary of the offset polygon
        if hasattr(offset_poly, 'exterior'):
            offset_points = list(offset_poly.exterior.coords)[:-1]  # Remove duplicate last point
        else:
            center_x, center_y = self._get_polygon_center(pocket_poly)
            error_msg = f"Pocket at approximately ({center_x:.3f}, {center_y:.3f}) resulted in invalid geometry after tool compensation"
            self._add_error(error_msg)
            return gcode

        # Use pocket centroid as entry position (center of pocket)
        entry_x = offset_poly.centroid.x
        entry_y = offset_poly.centroid.y

        # Calculate helical entry parameters
        helix_radius = self.tool_radius * self.helix_radius_multiplier  # Helix radius from material preset
        ramp_start_height = self.material_top + self.ramp_start_clearance
        num_helical_passes, depth_per_pass = self._calculate_helical_passes(helix_radius, ramp_start_height=ramp_start_height)

        gcode.append(f"(Pocket with helical entry at center: {num_helical_passes} passes at {self.ramp_angle} deg)")

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

        # Calculate stepover for pocket clearing
        stepover = self.tool_diameter * self.stepover_percentage

        # Always use contour-parallel clearing for reliable material removal
        # (Previous circular spiral optimization left material in slot-shaped pockets)
        gcode.append(f"(Pocket clearing - using contour-parallel stepover passes)")

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
            # For complex shapes, buffer can create MultiPolygon - handle both cases
            contours.append(test_poly)

        gcode.append(f"(Contour-parallel clearing: {len(contours)} offset passes)")

        # Cut contours from outside-in (perimeter to center)
        pass_number = 0
        for contour_geom in reversed(contours):
            # Handle both Polygon and MultiPolygon (complex shapes can split into multiple regions)
            polygons_to_cut = []
            if hasattr(contour_geom, 'exterior'):
                # Single Polygon
                polygons_to_cut.append(contour_geom)
            elif hasattr(contour_geom, 'geoms'):
                # MultiPolygon - cut all separate regions
                polygons_to_cut.extend(contour_geom.geoms)

            for poly in polygons_to_cut:
                if not hasattr(poly, 'exterior'):
                    continue

                contour_points = list(poly.exterior.coords)[:-1]
                if len(contour_points) < 3:
                    continue

                pass_number += 1
                gcode.append(f"(Contour pass {pass_number})")

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

        # Spring pass: re-trace the perimeter at zero stepover to relieve tool
        # deflection that left the pocket slightly undersized.
        gcode.append(f"(Spring pass - compensate for tool deflection)")
        for point in offset_points[1:]:
            gcode.append(f"G1 X{point[0]:.4f} Y{point[1]:.4f} F{self.feed_rate}")
        gcode.append(f"G1 X{offset_points[0][0]:.4f} Y{offset_points[0][1]:.4f} F{self.feed_rate}  ; Close spring pass")

        # Retract
        gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract")

        return gcode

    def _detect_circular_ring(self, polygon: Polygon) -> Optional[Tuple[float, float, float, float]]:
        """Check if a polygon with interiors is approximately a circular ring.

        Uses the isoperimetric quotient (4*pi*area/perimeter^2) to test circularity
        of both the exterior and each interior boundary, and verifies they share
        a common center.

        Args:
            polygon: Shapely Polygon, potentially with interior holes

        Returns:
            (center_x, center_y, outer_radius, inner_radius) if circular ring,
            or None if not a circular ring
        """
        if len(polygon.interiors) != 1:
            return None

        circularity_threshold = 0.95
        center_tolerance = 0.01  # inches

        # Check exterior circularity
        ext_area = Polygon(polygon.exterior).area
        ext_perimeter = polygon.exterior.length
        ext_circularity = (4 * math.pi * ext_area) / (ext_perimeter ** 2)
        if ext_circularity < circularity_threshold:
            return None

        # Check interior circularity
        interior = polygon.interiors[0]
        int_area = Polygon(interior.coords).area
        int_perimeter = interior.length
        int_circularity = (4 * math.pi * int_area) / (int_perimeter ** 2)
        if int_circularity < circularity_threshold:
            return None

        # Check shared center
        ext_centroid = Polygon(polygon.exterior).centroid
        int_centroid = Polygon(interior.coords).centroid
        if ext_centroid.distance(int_centroid) > center_tolerance:
            return None

        # Calculate radii from area (more accurate than perimeter for discretized circles)
        cx = ext_centroid.x
        cy = ext_centroid.y
        outer_radius = math.sqrt(ext_area / math.pi)
        inner_radius = math.sqrt(int_area / math.pi)

        return (cx, cy, outer_radius, inner_radius)

    def _generate_circular_ring_gcode(self, pocket_poly: Polygon, offset_poly: Polygon,
                                      cx: float, cy: float,
                                      outer_radius: float, inner_radius: float) -> List[str]:
        """Generate G-code for a circular ring/groove pocket using spiral clearing.

        Instead of contour-parallel passes (which cause full-width slotting on the
        first pass), this uses a ring-centered helical ramp at the entry radius,
        then Archimedean spirals outward and inward to clear the full ring width.
        Every pass only engages stepover-width of material.

        Args:
            pocket_poly: Original pocket polygon (before tool compensation)
            offset_poly: Tool-compensated polygon (buffered inward by tool_radius)
            cx, cy: Ring center coordinates
            outer_radius, inner_radius: Radii of the tool-compensated ring
        """
        gcode = []

        stepover = self.tool_diameter * self.stepover_percentage

        # Entry point: use representative_point which lands in the solid ring
        rep_point = offset_poly.representative_point()
        entry_x = rep_point.x
        entry_y = rep_point.y

        # Entry radius = distance from ring center to entry point
        entry_radius = math.sqrt((entry_x - cx) ** 2 + (entry_y - cy) ** 2)

        # Clamp entry radius to stay within the ring
        entry_radius = max(inner_radius, min(outer_radius, entry_radius))

        # Recalculate entry point on the ring center axis at entry_radius
        # Place at 0 degrees (positive X from center) for clean arc commands
        entry_x = cx + entry_radius
        entry_y = cy

        ramp_start_height = self.material_top + self.ramp_start_clearance
        num_helical_passes, depth_per_pass = self._calculate_helical_passes(
            entry_radius, ramp_start_height=ramp_start_height)

        gcode.append(f"(Circular ring spiral clearing: center ({cx:.4f}, {cy:.4f}), "
                     f"outer r={outer_radius:.4f}, inner r={inner_radius:.4f})")

        # Position at entry point on ring circumference
        gcode.append(f"G1 X{entry_x:.4f} Y{entry_y:.4f} F{self.traverse_rate}  ; Position at ring entry")
        gcode.append(f"G1 Z{ramp_start_height:.4f} F{self.approach_rate}  ; Approach to ramp start height")

        # Ring-centered helical ramp: helix around ring center at entry_radius
        # I,J are offsets from current position to arc center
        i_offset = cx - entry_x  # = -entry_radius (since entry_x = cx + entry_radius)
        j_offset = cy - entry_y  # = 0 (since entry_y = cy)

        gcode.append(f"(Helical ramp: {num_helical_passes} passes at entry radius {entry_radius:.4f})")
        for pass_num in range(num_helical_passes):
            target_z = ramp_start_height - (pass_num + 1) * depth_per_pass
            gcode.append(f"G3 X{entry_x:.4f} Y{entry_y:.4f} I{i_offset:.4f} J{j_offset:.4f} "
                         f"Z{target_z:.4f} F{self.ramp_feed_rate}  ; Helical pass {pass_num + 1}/{num_helical_passes}")

        # Cleanup circle at entry radius to ensure full ring at this radius is cleared
        gcode.append(f"G3 X{entry_x:.4f} Y{entry_y:.4f} I{i_offset:.4f} J{j_offset:.4f} "
                     f"F{self.feed_rate}  ; Cleanup pass at entry radius")

        # Archimedean spiral outward from entry_radius to outer_radius
        radius_delta_out = outer_radius - entry_radius
        if radius_delta_out > 0.001:
            spiral_constant = stepover / (2 * math.pi)
            total_angle = radius_delta_out / spiral_constant if spiral_constant > 0 else 0
            angle_increment = math.radians(10)
            num_points = int(math.ceil(total_angle / angle_increment))

            gcode.append(f"(Spiral outward: {num_points} points from r={entry_radius:.4f} to r={outer_radius:.4f})")
            for i in range(num_points):
                current_angle = i * angle_increment
                current_radius = entry_radius + spiral_constant * current_angle
                current_radius = min(current_radius, outer_radius)
                x = cx + current_radius * math.cos(current_angle)
                y = cy + current_radius * math.sin(current_angle)
                gcode.append(f"G1 X{x:.4f} Y{y:.4f} F{self.feed_rate}")

        # Cleanup circle at outer radius - G3/CCW for climb milling on inner groove wall
        outer_x = cx + outer_radius
        outer_y = cy
        gcode.append(f"G1 X{outer_x:.4f} Y{outer_y:.4f} F{self.feed_rate}  ; Move to outer radius")
        gcode.append(f"G3 X{outer_x:.4f} Y{outer_y:.4f} I{-outer_radius:.4f} J0 "
                     f"F{self.feed_rate}  ; Outer cleanup circle, CCW climb milling")

        # Spring pass on outer wall: re-trace at zero stepover to relieve tool deflection.
        gcode.append(f"(Spring pass - compensate for tool deflection)")
        gcode.append(f"G3 X{outer_x:.4f} Y{outer_y:.4f} I{-outer_radius:.4f} J0 "
                     f"F{self.feed_rate}  ; Outer wall spring pass")

        # Return to entry radius for inward spiral
        entry_on_ring_x = cx + entry_radius
        entry_on_ring_y = cy
        gcode.append(f"G1 X{entry_on_ring_x:.4f} Y{entry_on_ring_y:.4f} F{self.feed_rate}  ; Return to entry radius")

        # Archimedean spiral inward from entry_radius to inner_radius
        radius_delta_in = entry_radius - inner_radius
        if radius_delta_in > 0.001:
            spiral_constant = stepover / (2 * math.pi)
            total_angle = radius_delta_in / spiral_constant if spiral_constant > 0 else 0
            angle_increment = math.radians(10)
            num_points = int(math.ceil(total_angle / angle_increment))

            gcode.append(f"(Spiral inward: {num_points} points from r={entry_radius:.4f} to r={inner_radius:.4f})")
            for i in range(num_points):
                current_angle = i * angle_increment
                current_radius = entry_radius - spiral_constant * current_angle
                current_radius = max(current_radius, inner_radius)
                x = cx + current_radius * math.cos(current_angle)
                y = cy + current_radius * math.sin(current_angle)
                gcode.append(f"G1 X{x:.4f} Y{y:.4f} F{self.feed_rate}")

        # Cleanup circle at inner radius - G2/CW for climb milling on outer island wall
        inner_x = cx + inner_radius
        inner_y = cy
        gcode.append(f"G1 X{inner_x:.4f} Y{inner_y:.4f} F{self.feed_rate}  ; Move to inner radius")
        gcode.append(f"G2 X{inner_x:.4f} Y{inner_y:.4f} I{-inner_radius:.4f} J0 "
                     f"F{self.feed_rate}  ; Inner cleanup circle, CW climb milling")

        # Spring pass on inner wall: re-trace at zero stepover to relieve tool deflection.
        gcode.append(f"(Spring pass - compensate for tool deflection)")
        gcode.append(f"G2 X{inner_x:.4f} Y{inner_y:.4f} I{-inner_radius:.4f} J0 "
                     f"F{self.feed_rate}  ; Inner wall spring pass")

        # Retract
        gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract")

        return gcode

    def _generate_pocket_gcode_from_polygon(self, pocket_poly: Polygon) -> List[str]:
        """Generate G-code for a pocket from a Shapely Polygon (supports interior holes/islands).
        This is the island-aware version that respects Polygon interiors."""
        gcode = []

        # Validate input
        if not pocket_poly.is_valid or pocket_poly.is_empty:
            return gcode

        # Check groove width for polygons with interior holes (rings/grooves)
        if len(pocket_poly.interiors) > 0:
            min_groove_width = float('inf')
            for interior in pocket_poly.interiors:
                interior_ring = LinearRing(interior.coords)
                width = pocket_poly.exterior.distance(interior_ring)
                min_groove_width = min(min_groove_width, width)

            if min_groove_width < self.tool_diameter:
                center_x, center_y = pocket_poly.centroid.x, pocket_poly.centroid.y
                error_msg = (
                    f"Groove at approximately ({center_x:.3f}, {center_y:.3f}) "
                    f"is {min_groove_width:.4f}\" wide, which is too narrow for "
                    f"{self.tool_diameter:.4f}\" tool"
                )
                self._add_error(error_msg)
                return gcode

        # Buffer inward (negative buffer) for tool compensation
        # Key: Shapely automatically respects interior holes during buffer!
        offset_poly = pocket_poly.buffer(-self.tool_radius)

        if offset_poly.is_empty or offset_poly.area < 0.001:
            center_x, center_y = pocket_poly.centroid.x, pocket_poly.centroid.y
            error_msg = f"Pocket at approximately ({center_x:.3f}, {center_y:.3f}) is too small for {self.tool_diameter:.4f}\" tool - tool cannot fit inside with proper clearance"
            self._add_error(error_msg)
            return gcode

        # Check for circular ring - use spiral clearing instead of contour-parallel
        if isinstance(offset_poly, Polygon) and len(offset_poly.interiors) > 0:
            ring_params = self._detect_circular_ring(offset_poly)
            if ring_params is not None:
                cx, cy, outer_r, inner_r = ring_params
                return self._generate_circular_ring_gcode(
                    pocket_poly, offset_poly, cx, cy, outer_r, inner_r)

        # Find a good entry point within the machining area
        # CRITICAL: For rings/donuts, centroid is in the center hole (island)!
        # Use representative_point() which is guaranteed to be inside the solid geometry
        if hasattr(offset_poly, 'representative_point'):
            rep_point = offset_poly.representative_point()
            entry_x = rep_point.x
            entry_y = rep_point.y
        elif hasattr(offset_poly, 'geoms'):
            # MultiPolygon - use representative point of largest piece
            largest_poly = max(offset_poly.geoms, key=lambda p: p.area)
            rep_point = largest_poly.representative_point()
            entry_x = rep_point.x
            entry_y = rep_point.y
        else:
            # Fallback to centroid
            entry_x = offset_poly.centroid.x
            entry_y = offset_poly.centroid.y

        # Calculate helical entry parameters
        helix_radius = self.tool_radius * self.helix_radius_multiplier

        # Adapt helix radius to fit within available space
        # The max helix radius at the entry point is the distance from entry to nearest boundary
        max_helix_radius = offset_poly.boundary.distance(Point(entry_x, entry_y))
        if helix_radius > max_helix_radius * 0.9:  # 90% safety factor
            helix_radius = max(max_helix_radius * 0.9, self.tool_radius * 0.25)  # Floor at 25% of tool_radius

        ramp_start_height = self.material_top + self.ramp_start_clearance
        num_helical_passes, depth_per_pass = self._calculate_helical_passes(helix_radius, ramp_start_height=ramp_start_height)

        gcode.append(f"(Island-aware pocket with helical entry: {num_helical_passes} passes at {self.ramp_angle} deg)")

        # Position at entry point
        gcode.append(f"G1 X{entry_x:.4f} Y{entry_y:.4f} F{self.traverse_rate}  ; Position at entry point")
        gcode.append(f"G1 Z{ramp_start_height:.4f} F{self.approach_rate}  ; Approach to ramp start height")

        # Helical entry
        start_x = entry_x + helix_radius
        start_y = entry_y
        gcode.append(f"G1 X{start_x:.4f} Y{start_y:.4f} F{self.traverse_rate}  ; Move to helix start")

        for pass_num in range(num_helical_passes):
            target_z = ramp_start_height - (pass_num + 1) * depth_per_pass
            gcode.append(f"G3 X{start_x:.4f} Y{start_y:.4f} I{-helix_radius:.4f} J0 Z{target_z:.4f} F{self.ramp_feed_rate}  ; Helical pass {pass_num + 1}/{num_helical_passes}")

        # Return to entry point after helix
        gcode.append(f"G1 X{entry_x:.4f} Y{entry_y:.4f} F{self.feed_rate}  ; Return to entry point")

        # Calculate stepover for pocket clearing
        stepover = self.tool_diameter * self.stepover_percentage

        gcode.append(f"(Contour-parallel clearing with island avoidance)")

        # For ring polygons (with interior holes), buffer() on the whole ring shrinks
        # from both sides simultaneously, collapsing the ring in ~2 steps.
        # Instead, offset from the EXTERIOR ONLY and stop at the interior boundary.
        solid_exterior = Polygon(pocket_poly.exterior)

        # Build expanded interior (holes + tool_radius) as the no-go zone
        expanded_interiors = None
        if len(pocket_poly.interiors) > 0:
            interior_geoms = []
            for interior in pocket_poly.interiors:
                interior_poly = Polygon(interior.coords)
                interior_geoms.append(interior_poly.buffer(self.tool_radius))
            expanded_interiors = unary_union(interior_geoms)

        # Generate offset contours from exterior inward
        contours = []
        test_offset = -self.tool_radius
        while True:
            test_offset -= stepover
            offset_circle = solid_exterior.buffer(test_offset)
            if offset_circle.is_empty or offset_circle.area < 0.001:
                break

            # Subtract expanded interior to stay in machining area
            if expanded_interiors is not None:
                machining_portion = offset_circle.difference(expanded_interiors)
                if machining_portion.is_empty or machining_portion.area < 0.001:
                    break
                contours.append(machining_portion)
            else:
                contours.append(offset_circle)

        gcode.append(f"(Contour-parallel clearing: {len(contours)} offset passes)")

        # Cut contours from outside-in
        pass_number = 0
        for contour_geom in reversed(contours):
            # Handle both Polygon and MultiPolygon
            polygons_to_cut = []
            if isinstance(contour_geom, Polygon):
                polygons_to_cut.append(contour_geom)
            elif isinstance(contour_geom, MultiPolygon):
                polygons_to_cut.extend(contour_geom.geoms)

            for poly_to_cut in polygons_to_cut:
                if not hasattr(poly_to_cut, 'exterior'):
                    continue

                contour_coords = list(poly_to_cut.exterior.coords)
                if len(contour_coords) < 3:
                    continue

                pass_number += 1
                gcode.append(f"(Contour pass {pass_number})")

                gcode.append(f"G1 X{contour_coords[0][0]:.4f} Y{contour_coords[0][1]:.4f} F{self.feed_rate}")
                for point in contour_coords[1:]:
                    gcode.append(f"G1 X{point[0]:.4f} Y{point[1]:.4f} F{self.feed_rate}")

        # Final pass - trace tool-compensated boundary (exterior + interiors)
        exterior_coords = list(offset_poly.exterior.coords)[:-1]
        if len(exterior_coords) >= 3:
            pass_number += 1
            gcode.append(f"(Contour pass {pass_number} - final outer perimeter)")
            gcode.append(f"G1 X{exterior_coords[0][0]:.4f} Y{exterior_coords[0][1]:.4f} F{self.feed_rate}")
            for point in exterior_coords[1:]:
                gcode.append(f"G1 X{point[0]:.4f} Y{point[1]:.4f} F{self.feed_rate}")
            gcode.append(f"G1 X{exterior_coords[0][0]:.4f} Y{exterior_coords[0][1]:.4f} F{self.feed_rate}")

            # Spring pass: re-trace the exterior at zero stepover to relieve
            # tool deflection.
            gcode.append(f"(Spring pass - compensate for tool deflection)")
            for point in exterior_coords[1:]:
                gcode.append(f"G1 X{point[0]:.4f} Y{point[1]:.4f} F{self.feed_rate}")
            gcode.append(f"G1 X{exterior_coords[0][0]:.4f} Y{exterior_coords[0][1]:.4f} F{self.feed_rate}")

        # Also trace interior boundaries of the tool-compensated ring
        if hasattr(offset_poly, 'interiors'):
            for interior in offset_poly.interiors:
                interior_coords = list(interior.coords)[:-1]
                if len(interior_coords) >= 3:
                    pass_number += 1
                    gcode.append(f"(Contour pass {pass_number} - inner boundary)")
                    gcode.append(f"G1 X{interior_coords[0][0]:.4f} Y{interior_coords[0][1]:.4f} F{self.feed_rate}")
                    for point in interior_coords[1:]:
                        gcode.append(f"G1 X{point[0]:.4f} Y{point[1]:.4f} F{self.feed_rate}")
                    gcode.append(f"G1 X{interior_coords[0][0]:.4f} Y{interior_coords[0][1]:.4f} F{self.feed_rate}")

                    # Spring pass on this interior boundary.
                    gcode.append(f"(Spring pass - compensate for tool deflection)")
                    for point in interior_coords[1:]:
                        gcode.append(f"G1 X{point[0]:.4f} Y{point[1]:.4f} F{self.feed_rate}")
                    gcode.append(f"G1 X{interior_coords[0][0]:.4f} Y{interior_coords[0][1]:.4f} F{self.feed_rate}")

        # Retract
        gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract")

        return gcode

    def _generate_contour_gcode(self,
                               contour_points: List[Tuple[float, float]],
                               contour_type: str,
                               offset_direction: int,
                               clockwise: bool,
                               remove_tabs_at_end: bool) -> List[str]:
        """Generate G-code for contour cutting (perimeter or pocket contour) with tabs

        Shared logic for both perimeter and pocket contour operations.
        Supports multi-pass cutting for thick materials based on max_slotting_depth.
        Tabs are only cut on the final pass.

        Args:
            contour_points: List of (x, y) coordinates defining the contour
            contour_type: "perimeter" or "pocket" (for comments)
            offset_direction: +1 for outward offset (perimeter), -1 for inward (pocket)
            clockwise: True for CW (perimeter), False for CCW (pocket interior)
            remove_tabs_at_end: Whether to generate tab removal pass (False for pockets)
        """
        gcode = []

        # Calculate number of passes needed based on material thickness and max slotting depth
        # Total depth = from material top to cut depth (which is below Z=0)
        total_cut_depth = self.material_top - self.cut_depth  # e.g., 0.25 - (-0.02) = 0.27"
        num_passes = max(1, int(math.ceil(total_cut_depth / self.max_slotting_depth)))

        if num_passes > 1:
            actual_depth_per_pass = total_cut_depth / num_passes
            gcode.append(f"(Multi-pass {contour_type}: {num_passes} passes @ {actual_depth_per_pass:.3f}\" each, max {self.max_slotting_depth:.3f}\" per pass)")

        # Create offset path
        contour_poly = Polygon(contour_points)

        # Buffer by tool radius (positive for outward/perimeter, negative for inward/pocket)
        offset_distance = offset_direction * self.tool_radius
        offset_poly = contour_poly.buffer(offset_distance)

        if offset_poly.is_empty:
            center_x, center_y = self._get_polygon_center(contour_poly)
            error_msg = f"{contour_type.capitalize()} at approximately ({center_x:.3f}, {center_y:.3f}) failed offset operation - may have internal corners with radius smaller than {self.tool_diameter:.4f}\" tool can mill"
            self._add_error(error_msg)
            return gcode

        # Get the boundary of the offset polygon
        if hasattr(offset_poly, 'exterior'):
            offset_points = list(offset_poly.exterior.coords)[:-1]  # Remove duplicate last point
            # Set direction based on clockwise parameter
            # Default order from Shapely is CCW, so reverse for CW
            if clockwise:
                offset_points = offset_points[::-1]
        else:
            center_x, center_y = self._get_polygon_center(contour_poly)
            error_msg = f"{contour_type.capitalize()} at approximately ({center_x:.3f}, {center_y:.3f}) resulted in invalid geometry after tool compensation - may have internal corners too sharp for {self.tool_diameter:.4f}\" tool"
            self._add_error(error_msg)
            return gcode

        # Calculate segment lengths
        segment_lengths = []
        for i in range(len(offset_points)):
            p1 = offset_points[i]
            p2 = offset_points[(i + 1) % len(offset_points)]
            length = self._distance_2d(p1, p2)
            segment_lengths.append(length)

        # Calculate total contour length
        contour_length = sum(segment_lengths)

        # Will store tab positions for final removal pass (only populated on final pass)
        all_tab_positions = []

        # Calculate equal depth per pass for consistent tool loading
        depth_per_pass = total_cut_depth / num_passes

        # Multi-pass cutting loop
        for pass_num in range(1, num_passes + 1):
            is_final_pass = (pass_num == num_passes)

            # Calculate target depth for this pass (equal increments)
            if is_final_pass:
                # Final pass goes exactly to target depth to avoid rounding errors
                pass_cut_depth = self.cut_depth
            else:
                # Intermediate passes cut equal increments from material top
                pass_cut_depth = self.material_top - (pass_num * depth_per_pass)

            if num_passes > 1:
                gcode.append(f"")
                gcode.append(f"(===== PASS {pass_num}/{num_passes} - cutting to {pass_cut_depth:.3f}\" =====)")

            # Calculate ramp start height (close to material surface)
            ramp_start_height = self.material_top + self.ramp_start_clearance

            # Calculate ramp-in distance using material-specific ramp angle
            ramp_depth = ramp_start_height - pass_cut_depth
            ramp_distance = ramp_depth / math.tan(math.radians(self.ramp_angle))
            gcode.append(f"(Ramp-in: {ramp_distance:.4f}\" at {self.ramp_angle} deg)")

            # Calculate tab zones ONLY on final pass (if tabs are enabled)
            tab_zones = []  # List of (start_dist, end_dist) tuples
            if is_final_pass and self.tabs_enabled:
                # We cut from ramp_distance to contour_length, so tabs should only be in that range
                cutting_length = contour_length - ramp_distance

                # Calculate number of tabs based on desired spacing, with minimum of 3
                num_tabs = max(3, int(math.ceil(cutting_length / self.tab_spacing)))
                actual_tab_spacing = cutting_length / num_tabs

                # Place tabs starting after the ramp, centered in each section
                half_tab_width = self.tab_width / 2
                for i in range(num_tabs):
                    tab_center = ramp_distance + actual_tab_spacing * (i + 0.5)
                    tab_start = tab_center - half_tab_width
                    tab_end = tab_center + half_tab_width
                    tab_zones.append((tab_start, tab_end))

                gcode.append(f"(Tabs: {num_tabs} tabs - desired spacing: {self.tab_spacing:.2f}\", actual: {actual_tab_spacing:.2f}\" - width: {self.tab_width:.4f}\")")
            elif is_final_pass and not self.tabs_enabled:
                gcode.append(f"(Tabs disabled - perimeter will be cut through completely)")

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
                    ramp_points.append((final_x, final_y, pass_cut_depth))
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
                    remaining_depth = current_z - pass_cut_depth

                    if remaining_depth > 0.001:  # Only if significant depth remains
                        # Use small helical loop instead of straight plunge
                        helix_radius = self.tool_radius * self.helix_radius_multiplier  # Helix radius from material preset
                        helix_center_x = current_pos[0]
                        helix_center_y = current_pos[1]

                        # Calculate number of helical loops needed
                        circumference = 2 * math.pi * helix_radius
                        depth_per_loop = circumference * math.tan(math.radians(self.ramp_angle))
                        num_loops = max(1, int(math.ceil(remaining_depth / depth_per_loop)))
                        depth_per_loop_actual = remaining_depth / num_loops

                        gcode.append(f"(Perimeter too short - using helical finish: {num_loops} loops at {self.ramp_angle} deg)")

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

            # Cut around perimeter with tabs (on final pass only), starting from where ramp ended
            # Use segment-centric approach: check each segment against tab zones
            current_distance = current_ramp_dist
            tab_z = pass_cut_depth + self.tab_height
            tab_number = 0
            current_z = pass_cut_depth  # Track current Z height to avoid unnecessary moves

            # Store tab positions for the tab removal pass (only on final pass).
            # A single tab can straddle multiple contour segments — common on
            # curves, where circles are approximated as many short chords — so
            # we keep an ordered waypoint list per tab. The removal pass plunges
            # at the first waypoint and traces every piece in order; a tab that
            # lives entirely on one segment ends up with two waypoints, same as
            # the old straight-line behavior.
            tab_waypoints_by_idx = {}  # tab_idx -> [(x, y), (x, y), ...]

            # Create perimeter points list starting from where ramp ended
            # Continue from ramp_end_segment to end, then wrap around to start
            remaining_points = offset_points[ramp_end_segment:] + offset_points[:ramp_end_segment]
            remaining_lengths = segment_lengths[ramp_end_segment:] + segment_lengths[:ramp_end_segment]

            # Helper function to process a segment with tab checking
            def process_segment(p1, p2, seg_start_dist, seg_length):
                nonlocal tab_number, current_z, tab_waypoints_by_idx

                if seg_length == 0:
                    return

                seg_end_dist = seg_start_dist + seg_length

                # Find all tab zones that intersect this segment (only if tabs enabled for this pass)
                intersecting_tabs = []
                if is_final_pass:  # Only process tabs on final pass
                    for tab_idx, (tab_start, tab_end) in enumerate(tab_zones):
                        # Check if tab zone overlaps with segment
                        if tab_start < seg_end_dist and tab_end > seg_start_dist:
                            # Clamp to segment boundaries
                            overlap_start = max(tab_start, seg_start_dist)
                            overlap_end = min(tab_end, seg_end_dist)
                            intersecting_tabs.append((overlap_start, overlap_end, tab_idx))

                if not intersecting_tabs:
                    # No tabs in this segment - ensure we're at cut depth, then cut normally
                    if current_z != pass_cut_depth:
                        gcode.append(f"G1 Z{pass_cut_depth:.4f} F{self.plunge_rate}")
                        current_z = pass_cut_depth
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

                        # Record this sub-segment for the removal pass. Contiguous
                        # pieces of the same tab share an endpoint geometrically,
                        # so we only append the new endpoint on continuations.
                        if tab_idx not in tab_waypoints_by_idx:
                            tab_waypoints_by_idx[tab_idx] = [(start_x, start_y), (end_x, end_y)]
                        else:
                            tab_waypoints_by_idx[tab_idx].append((end_x, end_y))

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
                        if current_z != pass_cut_depth:
                            gcode.append(f"G1 Z{pass_cut_depth:.4f} F{self.plunge_rate}  ; Tab end")
                            current_z = pass_cut_depth

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

            # Store tab positions from final pass for removal
            if is_final_pass:
                all_tab_positions = sorted(tab_waypoints_by_idx.items(), key=lambda kv: kv[0])

            # Retract
            gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract")

        # ===== TAB REMOVAL PASS =====
        # Remove tabs in star pattern to gradually release the part (only if tabs were created and removal is enabled)
        # Note: Pocket contours NEVER remove tabs (center material remains for manual removal)
        if all_tab_positions and remove_tabs_at_end:
            gcode.append("")
            gcode.append("(===== TAB REMOVAL PASS =====)")
            gcode.append(f"(Removing {len(all_tab_positions)} tabs in star pattern)")

            # all_tab_positions is already sorted by tab_idx (see above).

            # Generate star pattern order: alternates between first and second half
            # For 4 tabs (0,1,2,3): order is 0,2,1,3
            # For 6 tabs (0,1,2,3,4,5): order is 0,3,1,4,2,5
            num_tabs = len(all_tab_positions)
            star_order = []
            half = num_tabs // 2
            for i in range(half):
                star_order.append(i)
                if i + half < num_tabs:
                    star_order.append(i + half)
            # Handle odd number of tabs
            if num_tabs % 2 == 1:
                star_order.append(num_tabs - 1)

            gcode.append(f"(Star pattern order: {', '.join(str(i+1) for i in star_order)})")
            gcode.append("")

            # Remove each tab in star order
            for removal_num, tab_order_idx in enumerate(star_order, 1):
                tab_idx, waypoints = all_tab_positions[tab_order_idx]
                start_x, start_y = waypoints[0]

                gcode.append(f"(Tab {tab_idx + 1} removal - #{removal_num} in sequence)")

                # Rapid to retract height (like moving between holes)
                gcode.append(f"G0 Z{self.retract_height:.4f}")

                # Rapid to position just before the tab (in the kerf)
                gcode.append(f"G0 X{start_x:.4f} Y{start_y:.4f}  ; Move to tab start (in kerf)")

                # Plunge to cut depth in empty kerf at approach rate (faster - no material)
                gcode.append(f"G1 Z{self.cut_depth:.4f} F{self.approach_rate}  ; Plunge in kerf")

                # Cut through each piece of the tab in contour order so curved
                # tabs (spanning multiple short chord segments) get fully removed.
                for ex, ey in waypoints[1:]:
                    gcode.append(f"G1 X{ex:.4f} Y{ey:.4f} F{self.feed_rate}  ; Cut through tab")

                # Retract after each tab
                gcode.append(f"G0 Z{self.retract_height:.4f}  ; Retract")
                gcode.append("")

        return gcode

    def _generate_perimeter_gcode(self, perimeter_points: List[Tuple[float, float]]) -> List[str]:
        """Generate G-code for perimeter with tabs and tool compensation (offset outward)

        Wrapper function that calls _generate_contour_gcode with perimeter-specific parameters.
        Supports multi-pass cutting for thick materials based on max_slotting_depth.
        Tabs are only cut on the final pass.
        """
        return self._generate_contour_gcode(
            contour_points=perimeter_points,
            contour_type="perimeter",
            offset_direction=+1,  # Outward offset
            clockwise=True,       # CW for climb milling on outside features
            remove_tabs_at_end=self.config.remove_tabs  # Config-based tab removal
        )

    def _generate_pocket_contour_gcode(self, pocket_points: List[Tuple[float, float]]) -> List[str]:
        """Generate G-code for pocket contour (outline only) with tabs

        Contours large pockets instead of fully clearing them to save machining time.
        The center material remains attached by tabs and must be manually removed.

        Args:
            pocket_points: List of (x, y) coordinates defining the pocket boundary

        Returns:
            List of G-code lines for pocket contouring operation
        """
        gcode = ["(WARNING: Interior pocket contour - center material requires manual removal)"]

        gcode.extend(self._generate_contour_gcode(
            contour_points=pocket_points,
            contour_type="pocket",
            offset_direction=-1,  # Inward offset
            clockwise=False,      # CCW for climb milling on inside features
            remove_tabs_at_end=False  # NEVER remove tabs on pockets - material stays in place
        ))

        return gcode

    def _offset_coordinate(self, line: str, axis: str, offset: float) -> str:
        """
        Offset a coordinate in a G-code line by adding an offset.

        Generic method for offsetting X, Y, or Z coordinates in G-code lines.

        Args:
            line: G-code line to modify
            axis: Coordinate axis to offset ('X', 'Y', or 'Z')
            offset: Offset to add to coordinate value

        Returns:
            Modified G-code line with offset coordinate
        """
        def replace_coord(match):
            coord_val = float(match.group(1))
            new_val = coord_val + offset
            return f'{axis}{new_val:.4f}'

        # Match axis letter followed by optional minus and digits
        return re.sub(rf'{axis}(-?\d+\.?\d*)', replace_coord, line)

    def _adjust_y_coordinate(self, line: str, y_offset: float) -> str:
        """
        Adjust Y coordinate in a G-code line by adding offset.
        Legacy method - wraps generic _offset_coordinate() for backwards compatibility.
        """
        return self._offset_coordinate(line, 'Y', y_offset)

    def _calculate_tube_operation_passes(self, tube_height: float) -> dict:
        """
        Calculate pass parameters for tube operations (facing, cutting).

        Common calculation for both tube facing and cut-to-length operations.
        Both use the same depth strategy: cut just over half the tube height,
        with multiple passes to respect flute length limits.

        Args:
            tube_height: Height of tube in inches (Z dimension)

        Returns:
            Dict with pass calculation results:
            - total_depth: Total depth to cut
            - wall_thickness: Wall thickness from material_thickness
            - num_roughing_passes: Number of roughing passes needed
            - roughing_depth_per_pass: Depth per roughing pass
            - num_finishing_passes: Number of finishing passes needed
            - finishing_depth_per_pass: Depth per finishing pass
        """
        # Cutting parameters
        total_depth = tube_height / 2 + self.tube_facing_params['depth_margin']  # Just over half the tube height
        wall_thickness = self.material_thickness  # Wall thickness of box tubing

        # Roughing: respects flute length limit (max per pass from params)
        # 1" tube (0.505"): 2 passes, 2" tube (1.005"): 4 passes
        max_roughing_depth = self.tube_facing_params['max_roughing_depth']
        num_roughing_passes = max(1, int(math.ceil(total_depth / max_roughing_depth)))
        roughing_depth_per_pass = total_depth / num_roughing_passes

        # Finishing: light stepover allows deeper passes (max per pass from params)
        # 1" tube (0.505"): 1 pass, 2" tube (1.005"): 2 passes
        max_finishing_depth = self.tube_facing_params['max_finishing_depth']
        num_finishing_passes = max(1, int(math.ceil(total_depth / max_finishing_depth)))
        finishing_depth_per_pass = total_depth / num_finishing_passes

        return {
            'total_depth': total_depth,
            'wall_thickness': wall_thickness,
            'num_roughing_passes': num_roughing_passes,
            'roughing_depth_per_pass': roughing_depth_per_pass,
            'num_finishing_passes': num_finishing_passes,
            'finishing_depth_per_pass': finishing_depth_per_pass
        }

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
        elif tube_size == '2x1' or tube_size == '2x1-flat':
            return (2.0, 1.0)  # Flat: wide width, short height (most common)
        elif tube_size == '2x1-standing':
            return (1.0, 2.0)  # Standing: narrow width, tall height
        elif tube_size == '1.5x1.5':
            return (1.5, 1.5)
        elif tube_size == '2x2':
            return (2.0, 2.0)
        else:
            # Default to 1x1 if unknown
            return (1.0, 1.0)

    def _generate_parametric_tube_facing(self, tube_width: float, tube_height: float,
                                          phase: int = 1) -> list[str]:
        """
        Generate tube facing toolpath - face the end of box tubing.

        Squares the end of box tubing with one vertical plunge and two
        horizontal passes (roughing + finishing).

        Coordinate system (tube lying horizontal, end facing spindle):
        - X: across tube width (cut direction)
        - Z: tube height (plunge direction, vertical)
        - Y: facing depth (material removal from tube end, negative = into tube)

        Phase 1 (first end):
        - Roughing tool edge at Y=+0.05"
        - Finishing tool edge at Y=+0.0625"

        Phase 2 (after flip):
        - Roughing tool edge at Y=-0.0125"
        - Finishing tool edge at Y=0"

        Args:
            tube_width: Tube width in inches (X dimension)
            tube_height: Tube height in inches (Z dimension, typically 1" or 2")
            phase: 1 for first end (with stepover), 2 for second end (no stepover)

        Returns:
            List of G-code lines for the facing operation
        """
        gcode = []
        tool_radius = self.tool_diameter / 2.0

        # Calculate pass parameters using shared helper
        passes = self._calculate_tube_operation_passes(tube_height)
        total_depth = passes['total_depth']
        wall_thickness = passes['wall_thickness']
        num_roughing_passes = passes['num_roughing_passes']
        roughing_depth_per_pass = passes['roughing_depth_per_pass']
        num_finishing_passes = passes['num_finishing_passes']
        finishing_depth_per_pass = passes['finishing_depth_per_pass']

        # Tool edge positions for each phase (these are the final face positions)
        if phase == 1:
            # Phase 1: Roughing and finishing positions from params
            roughing_tool_edge = self.tube_facing_params['roughing_tool_edge_p1']
            finishing_tool_edge = self.tube_facing_params['finishing_tool_edge_p1']
        else:
            # Phase 2: Roughing and finishing positions from params
            roughing_tool_edge = self.tube_facing_params['roughing_tool_edge_p2']
            finishing_tool_edge = self.tube_facing_params['finishing_tool_edge_p2']

        # Arc clearing parameters (needed to calculate roughing_y offset)
        arc_advance = self.tube_facing_params['arc_advance']  # How far each arc advances in X
        arc_radius = self.tube_facing_params['arc_radius']  # Arc radius
        half_advance = arc_advance / 2
        j_offset = math.sqrt(arc_radius**2 - half_advance**2)

        # Tool CENTER positions for tube facing:
        # - Coordinate system: +Y is INTO the tube (toward tube body)
        # - Kept material (tube body) is at +Y, tube face is at Y≈0
        # - Tool's +Y edge (toward tube body) defines the face position
        #
        # With positive J, G3 (CCW) arc goes through TOP of circle (max Y).
        # Arc center Y = roughing_y + j_offset
        # Top of circle Y = center_y + arc_radius = roughing_y + j_offset + arc_radius
        #
        # At arc CHORD (start/end): tool center Y = roughing_y
        # At arc PEAK (top of circle): tool center Y = roughing_y + j_offset + arc_radius
        #
        # The PEAK is where the tool cuts deepest into the tube (maximum +Y edge).
        # Roughing should never exceed roughing_tool_edge, so we set PEAK at that limit.
        #
        # For roughing +Y edge at PEAK to equal roughing_tool_edge:
        #   (roughing_y + j_offset + arc_radius) + tool_radius = roughing_tool_edge
        #   roughing_y = roughing_tool_edge - tool_radius - j_offset - arc_radius
        roughing_y = roughing_tool_edge - tool_radius - j_offset - arc_radius
        finishing_y = finishing_tool_edge - tool_radius

        # X positions (tool edge 0.05" from material edge)
        clearance = tool_radius + 0.05
        start_x = tube_width + clearance  # Far side
        end_x = -clearance  # Near side

        # Z positions
        z_top = tube_height  # Top of tube
        z_safe = tube_height + 0.25  # Safe height above tube
        z_final = z_top - total_depth  # Final depth (just over half height)

        chord_face = roughing_y + tool_radius  # Face position at chord (start/end of arc)
        gcode.append(f'( Tube facing: {tube_width:.2f}" wide x {tube_height:.2f}" tall )')
        gcode.append(f'( Tool: {self.tool_diameter:.3f}" )')
        gcode.append(f'( Total depth: {total_depth:.3f}" )')
        gcode.append(f'( Roughing: {num_roughing_passes} passes of {roughing_depth_per_pass:.3f}" each, +Y edge at Y={roughing_tool_edge:.4f}" )')
        gcode.append(f'( Finishing: {num_finishing_passes} passes of {finishing_depth_per_pass:.3f}" each, +Y edge at Y={finishing_tool_edge:.4f}" )')

        # === ROUGHING PASSES ===
        arc_feed = self.feed_rate

        gcode.append('( === ROUGHING PASSES === )')
        gcode.append(f'( {num_roughing_passes} depth passes with arc clearing )')

        # Calculate wall boundaries for subsequent passes (box tubing is hollow)
        # Back wall (far side): from start_x to inner edge
        back_wall_inner_x = tube_width - wall_thickness - clearance
        # Front wall (near side): from inner edge to end_x
        front_wall_inner_x = wall_thickness + clearance

        for pass_num in range(num_roughing_passes):
            z_cut = z_top - (pass_num + 1) * roughing_depth_per_pass

            if pass_num == 0:
                # First pass: full arc pattern across entire width
                gcode.append(f'( Roughing pass {pass_num + 1}/{num_roughing_passes} to Z={z_cut:.3f}" - full width )')

                # Position at start
                gcode.append(f'G0 X{start_x:.4f} Y{roughing_y:.4f}')
                gcode.append(f'G0 Z{z_safe:.4f}')

                # Plunge to cut depth
                gcode.append(f'G0 Z{z_cut:.4f}')  # Rapid plunge (in air)

                # Arc clearing pattern across tube width
                gcode.append(f'G1 F{arc_feed}')
                current_x = start_x
                while current_x > end_x + arc_advance:
                    next_x = current_x - arc_advance
                    gcode.append(f'G3 X{next_x:.4f} Y{roughing_y:.4f} I{-half_advance:.4f} J{j_offset:.4f}')
                    current_x = next_x

                # Final linear move to end position if needed
                if current_x > end_x:
                    gcode.append(f'G1 X{end_x:.4f}')

                # Retract after this pass
                gcode.append(f'G0 Z{z_safe:.4f}')
            else:
                # Subsequent passes: cut walls only, rapid across hollow middle
                gcode.append(f'( Roughing pass {pass_num + 1}/{num_roughing_passes} to Z={z_cut:.3f}" - walls only )')

                # Position at start (back wall)
                gcode.append(f'G0 X{start_x:.4f} Y{roughing_y:.4f}')
                gcode.append(f'G0 Z{z_safe:.4f}')

                # Plunge to cut depth
                gcode.append(f'G0 Z{z_cut:.4f}')  # Rapid plunge (in air)

                # Arc clearing through back wall only
                gcode.append(f'G1 F{arc_feed}')
                current_x = start_x
                while current_x > back_wall_inner_x + arc_advance:
                    next_x = current_x - arc_advance
                    gcode.append(f'G3 X{next_x:.4f} Y{roughing_y:.4f} I{-half_advance:.4f} J{j_offset:.4f}')
                    current_x = next_x

                # Finish back wall
                if current_x > back_wall_inner_x:
                    gcode.append(f'G1 X{back_wall_inner_x:.4f}')

                # Retract, rapid across hollow middle
                gcode.append(f'G0 Z{z_safe:.4f}')
                gcode.append(f'G0 X{front_wall_inner_x:.4f}')

                # Plunge inside (material already removed on pass 1)
                gcode.append(f'G0 Z{z_cut:.4f}')  # Rapid plunge (in air)

                # Arc clearing through front wall
                gcode.append(f'G1 F{arc_feed}')
                current_x = front_wall_inner_x
                while current_x > end_x + arc_advance:
                    next_x = current_x - arc_advance
                    gcode.append(f'G3 X{next_x:.4f} Y{roughing_y:.4f} I{-half_advance:.4f} J{j_offset:.4f}')
                    current_x = next_x

                # Final linear move to end position if needed
                if current_x > end_x:
                    gcode.append(f'G1 X{end_x:.4f}')

                # Retract after this pass
                gcode.append(f'G0 Z{z_safe:.4f}')

        gcode.append(f'( Roughing complete: {num_roughing_passes} passes )')

        # === FINISHING PASSES ===
        stepover = finishing_tool_edge - roughing_tool_edge
        gcode.append('( === FINISHING PASSES === )')
        gcode.append(f'( {num_finishing_passes} depth passes, stepover {stepover:.4f}" )')

        for pass_num in range(num_finishing_passes):
            z_cut = z_top - (pass_num + 1) * finishing_depth_per_pass

            if pass_num == 0:
                # First pass: full cut across entire width
                gcode.append(f'( Finishing pass {pass_num + 1}/{num_finishing_passes} to Z={z_cut:.3f}" - full width )')

                # Position for finishing
                gcode.append(f'G0 X{start_x:.4f} Y{finishing_y:.4f}')

                # Plunge to cut depth
                gcode.append(f'G0 Z{z_cut:.4f}')  # Rapid plunge (in air)

                # Single horizontal cut across
                gcode.append(f'G1 X{end_x:.4f} F{self.feed_rate}')

                # Retract
                gcode.append(f'G0 Z{z_safe:.4f}')
            else:
                # Subsequent passes: cut walls only, rapid across hollow middle
                gcode.append(f'( Finishing pass {pass_num + 1}/{num_finishing_passes} to Z={z_cut:.3f}" - walls only )')

                # Position at start (back wall)
                gcode.append(f'G0 X{start_x:.4f} Y{finishing_y:.4f}')

                # Plunge to cut depth
                gcode.append(f'G0 Z{z_cut:.4f}')  # Rapid plunge (in air)

                # Cut through back wall only
                gcode.append(f'G1 X{back_wall_inner_x:.4f} F{self.feed_rate}')

                # Retract, rapid across hollow middle
                gcode.append(f'G0 Z{z_safe:.4f}')
                gcode.append(f'G0 X{front_wall_inner_x:.4f}')

                # Plunge inside (material already removed on pass 1)
                gcode.append(f'G0 Z{z_cut:.4f}')  # Rapid plunge (in air)

                # Cut through front wall
                gcode.append(f'G1 X{end_x:.4f} F{self.feed_rate}')

                # Retract
                gcode.append(f'G0 Z{z_safe:.4f}')

        return gcode

    def _generate_tube_facing_toolpath(self, tube_width: float, tube_height: float,
                                       tool_radius: float, stepover: float,
                                       stepdown: float, facing_depth: float,
                                       finish_allowance: float, phase: int = 1) -> list[str]:
        """
        Generate complete tube facing toolpath using parametric side-entry approach.

        This method generates toolpaths from scratch for any tube size using
        side-entry (plunge outside tube, arc into material) and contour clearing.
        The approach allows for 0.55" deep facing in a single pass per Z level.

        Args:
            tube_width: Width of tube (X dimension) in inches
            tube_height: Height of tube (Z dimension) in inches
            tool_radius: Unused (calculated internally)
            stepover: Unused (uses stepover_percentage)
            stepdown: Unused (single pass per Z level)
            facing_depth: Unused (hardcoded to 0.55")
            finish_allowance: Unused
            phase: 1 for first end (with stepover), 2 for second end (no stepover)

        Returns:
            List of G-code lines for the facing operation
        """
        return self._generate_parametric_tube_facing(tube_width, tube_height, phase)

    def generate_tube_facing_gcode(self, tube_size: str = '1x1', suggested_filename: str = None, timestamp: str = None) -> PostProcessorResult:
        """
        Generate G-code for tube facing operation with parameterized tube dimensions.

        Strategy:
        - Roughing passes: Zigzag pocketing at multiple Z depths with helical ramping
        - Finishing pass: Profile around tube perimeter with proper lead-in/lead-out
        - Phase 1: Face first half (Y=-0.125 to Y=+0.125)
        - Pause for flip (M0)
        - Phase 2: Face second half (Y=-0.25 to Y=0)

        Args:
            tube_size: Size of tube ('1x1', '2x1-standing', '2x1-flat')
            suggested_filename: Optional filename (without timestamp, will be added)

        Returns:
            PostProcessorResult with gcode string and stats
        """
        # Parse tube dimensions
        tube_width, tube_height = self._parse_tube_size(tube_size)

        # Calculate toolpath parameters
        tool_radius = self.tool_diameter / 2.0
        stepover = self.tool_diameter * 0.4  # 40% stepover for roughing
        stepdown = 0.05  # Conservative Z stepdown
        facing_depth = 0.25  # How much material to remove
        finish_allowance = 0.01  # Leave this much for finish pass

        # Generate separate toolpaths for each phase
        # Phase 1: Roughing and finishing at different Y depths (stepover)
        # Phase 2: Roughing and finishing at same Y depth (no stepover)
        phase1_toolpath = self._generate_tube_facing_toolpath(
            tube_width, tube_height, tool_radius, stepover,
            stepdown, facing_depth, finish_allowance, phase=1
        )
        phase2_toolpath = self._generate_tube_facing_toolpath(
            tube_width, tube_height, tool_radius, stepover,
            stepdown, facing_depth, finish_allowance, phase=2
        )

        # Tool edge positions are now directly specified in the toolpath generation
        # Phase 1: Roughing at +0.05", Finishing at +0.0625"
        # Phase 2: Roughing at -0.0125", Finishing at 0"
        # No Y offset needed - positions are absolute
        pass1_y_offset = 0
        pass2_y_offset = 0

        gcode = []

        # Use provided timestamp (from client's timezone) or generate one
        if not timestamp:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Format for G-code header (just date and time, no seconds)
        timestamp_display = timestamp[:16]  # YYYY-MM-DD HH:MM

        # === HEADER ===
        gcode.append('( PENGUINCAM TUBE FACING OPERATION )')
        gcode.append(f'( Generated: {timestamp_display} )')
        gcode.append(f'( Tube size: {tube_size} )')
        gcode.append(f'( Tool: {self.tool_diameter:.3f}" end mill )')
        gcode.append('( )')
        gcode.append('( SETUP INSTRUCTIONS: )')
        gcode.append('( 1. Mount tube in jig with end facing user )')
        gcode.append('( 2. Verify G55 is set to jig origin )')
        gcode.append('( 3. Z=0 is at bottom of tube [jig surface] )')
        gcode.append('( 4. Y=0 is at nominal end face of tube )')
        gcode.append('( )')

        # === INITIALIZATION ===
        gcode.append('')
        gcode.append('( === INITIALIZATION === )')
        gcode.append('G90 G94 G91.1 G40 G49 G17')
        gcode.append('G20')
        gcode.append('G90  ; Absolute positioning mode')
        gcode.append('')
        gcode.append('( Tool and spindle )')
        gcode.append('T1 M6')
        gcode.append(f'S{self.spindle_speed} M3')
        gcode.append('M7  ; Air blast on')
        gcode.append('G4 P3.0')
        gcode.append('')
        gcode.append('G55  ; Use jig work coordinate system')
        gcode.append('')

        # === PHASE 1: FACE FIRST HALF ===
        gcode.append('( === PHASE 1: FACE FIRST HALF === )')
        gcode.append('( Face from Y=-0.125 to Y=+0.125 )')
        gcode.append('')
        gcode.append(f'G53 G0 Z{self.machine_park_z:.4f}  ; Move to safe machine Z clearance')
        gcode.append('G0 X0 Y0  ; Rapid to work origin')
        gcode.append('')

        # Add Phase 1 toolpath with Pass 1 Y offset
        for line in phase1_toolpath:
            line = line.strip()
            if line and not line.startswith('G52'):
                adjusted_line = self._adjust_y_coordinate(line, pass1_y_offset)
                gcode.append(adjusted_line)

        # === PAUSE FOR FLIP ===
        gcode.extend(self._generate_pause_and_park_gcode(
            'PAUSE FOR TUBE FLIP',
            [
                'Flip tube 180 degrees end-for-end',
                'Re-clamp tube in jig'
            ]
        ))

        # === PHASE 2: FACE SECOND HALF ===
        gcode.append('( === PHASE 2: FACE SECOND HALF === )')
        gcode.append('( Face from Y=-0.250 to Y=-0.125 )')
        gcode.append('')
        gcode.append(f'G53 G0 Z{self.machine_park_z:.4f}  ; Move to safe machine Z clearance')
        gcode.append('G0 X0 Y0  ; Rapid to work origin')
        gcode.append('')

        # Add Phase 2 toolpath with Pass 2 Y offset (no stepover - same Y for roughing/finishing)
        for line in phase2_toolpath:
            line = line.strip()
            if line and not line.startswith('G52'):
                adjusted_line = self._adjust_y_coordinate(line, pass2_y_offset)
                gcode.append(adjusted_line)

        # === END ===
        gcode.append('')
        gcode.append('( === PROGRAM END === )')
        gcode.append(f'G53 G0 Z{self.machine_park_z:.4f}  ; Move to safe machine Z clearance')
        gcode.append(f'G53 G0 X{self.machine_park_x} Y{self.machine_park_y}  ; Park at back of machine')
        gcode.append('M9  ; Air blast off')
        gcode.append('M5')
        gcode.append('G54  ; Reset to standard work coordinate system')
        gcode.append('M30')

        # Estimate cycle time
        time_estimate = self._estimate_cycle_time(gcode)

        # Generate filename with timestamp
        base_name = suggested_filename if suggested_filename else "tube_facing"
        # Format timestamp for filename: YYYYMMDD_HHMMSS
        timestamp_for_file = timestamp.replace('-', '').replace(' ', '_').replace(':', '')
        filename = f"{base_name}_{timestamp_for_file}.nc"

        # Return result
        return PostProcessorResult(
            success=True,
            gcode='\n'.join(gcode),
            filename=filename,
            warnings=[],
            stats={
                'operation': 'tube_facing',
                'tube_size': tube_size,
                'tube_width': tube_width,
                'tube_height': tube_height,
                'num_holes': 0,
                'num_pockets': 0,
                'has_perimeter': False,
                'total_lines': len(gcode),
                'cycle_time_seconds': time_estimate['total'],
                'cycle_time_display': self._format_time(time_estimate['total']),
                'cutting_time': self._format_time(time_estimate['cutting']),
                'rapid_time': self._format_time(time_estimate['rapid']),
                'dwell_time': self._format_time(time_estimate['dwell']),
                'setup_instructions': [
                    'Mount tube in jig with end facing spindle',
                    'Verify G55 is set to jig origin',
                    'Z=0 is at bottom of tube (jig surface)',
                    'Y=0 is at nominal end face of tube'
                ],
                'operation_notes': [
                    'Phase 1: Face first half of tube end',
                    'Program pauses (M0) for tube flip',
                    'Phase 2: Face second half of tube end'
                ]
            }
        )

    def generate_tube_pattern_gcode(self, tube_height: float,
                                   square_end: bool, cut_to_length: bool,
                                   tube_width: float = None, tube_length: float = None,
                                   suggested_filename: str = None, timestamp: str = None) -> PostProcessorResult:
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
            tube_height: Height of tube in Z direction (inches)
            square_end: Whether to square the tube end before machining pattern
            cut_to_length: Whether to cut tube to length after pattern (stub)
            tube_width: Width of tube face (X dimension) in inches (optional, calculated from DXF if not provided)
            tube_length: Length of tube face (Y dimension) in inches (optional, for future use)
            suggested_filename: Optional filename (without timestamp, will be added)

        Returns:
            PostProcessorResult with gcode string and stats
        """
        # Check for validation errors first
        if self.errors:
            print(f"\n❌ Cannot generate G-code: {len(self.errors)} validation error(s) found")
            for error in self.errors:
                print(f"   - {error}")
            return PostProcessorResult(
                success=False,
                errors=self.errors.copy()
            )

        gcode = []

        # Use provided timestamp (from client's timezone) or generate one
        if not timestamp:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Format for G-code header (just date and time, no seconds)
        timestamp_display = timestamp[:16]  # YYYY-MM-DD HH:MM

        # === HEADER ===
        gcode.append('( PENGUINCAM TUBE PATTERN OPERATION )')
        gcode.append(f'( Generated: {timestamp_display} )')
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
        gcode.append('G90  ; Absolute positioning mode')
        gcode.append('')
        gcode.append('( Tool and spindle )')
        gcode.append('T1 M6')
        gcode.append(f'S{self.spindle_speed} M3')
        gcode.append('M7  ; Air blast on')
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

            # Facing toolpath Y coordinates are already absolute (calculated in _generate_parametric_tube_facing)
            # No additional offset needed - the face positions are set by roughing_tool_edge/finishing_tool_edge
            for line in facing_toolpath:
                gcode.append(line)
            gcode.append('')

        # Machine the pattern on this face
        gcode.append('( Machine pattern on first face )')
        gcode.append('( Machining holes and pockets only - perimeter is tube face )')
        z_offset = tube_height - self.material_thickness
        gcode.append(f'( Z offset: +{z_offset:.3f}" [tube_height - wall_thickness] )')
        # Y offset for first face: matches facing offset so holes align with face
        y_offset_first_face = self.tube_facing_offset if square_end else 0.0
        gcode.append(f'( Y offset: +{y_offset_first_face:.3f}" [rough end will be milled back] )')
        gcode.append('')
        gcode.extend(self._generate_toolpath_gcode(skip_perimeter=True, z_offset=z_offset, y_offset=y_offset_first_face))

        # === CUT TO LENGTH - PHASE 1 ===
        if cut_to_length:
            gcode.append('')
            gcode.append('( === CUT TUBE TO LENGTH - PHASE 1 === )')
            cut_gcode = self._generate_cut_to_length(tube_width, tube_height, tube_length, phase=1, square_end=square_end)
            gcode.extend(cut_gcode)

        # === PAUSE FOR FLIP ===
        gcode.extend(self._generate_pause_and_park_gcode(
            'PAUSE FOR TUBE FLIP',
            [
                'Flip tube 180 degrees around Y-axis',
                'Holes will be machined on opposite face'
            ]
        ))

        # === PHASE 2: SECOND FACE (SQUARE + MACHINE PATTERN) ===
        gcode.append('( === PHASE 2: SECOND FACE === )')
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
                stepdown, facing_depth, finish_allowance, phase=2
            )

            # Facing toolpath Y coordinates are already absolute (calculated in _generate_parametric_tube_facing)
            # No additional offset needed - the face positions are set by roughing_tool_edge/finishing_tool_edge
            for line in facing_toolpath:
                gcode.append(line)
            gcode.append('')

        # Machine the pattern on this face (X-mirrored, Y offset for facing alignment)
        gcode.append('( Machine pattern on second face - X-mirrored )')
        gcode.append('( Pattern is X-mirrored [tube flipped end-for-end] so holes align opposite )')
        z_offset = tube_height - self.material_thickness
        gcode.append(f'( Z offset: +{z_offset:.3f}" [tube_height - wall_thickness] )')
        # Y offset: 0 for Phase 2 - work zero is re-established after flip, face is at Y=0"
        y_offset_phase2 = 0.0
        gcode.append(f'( Y offset: {y_offset_phase2:.4f}" [face at Y=0, no offset needed] )')
        gcode.append('')

        # Mirror X coordinates around tube centerline (tube flipped end-for-end)
        mirrored_toolpath = self._generate_toolpath_gcode_mirrored_x(
            z_offset=z_offset, tube_width=tube_width, y_offset=y_offset_phase2
        )
        gcode.extend(mirrored_toolpath)

        # === CUT TO LENGTH - PHASE 2 ===
        if cut_to_length:
            gcode.append('')
            gcode.append('( === CUT TUBE TO LENGTH - PHASE 2 === )')
            cut_gcode = self._generate_cut_to_length(tube_width, tube_height, tube_length, phase=2, square_end=square_end)
            gcode.extend(cut_gcode)

        # === END ===
        gcode.append('')
        gcode.append('( === PROGRAM END === )')
        gcode.append(f'G53 G0 Z{self.machine_park_z:.4f}')
        gcode.append(f'G53 G0 X{self.machine_park_x} Y{self.machine_park_y}')
        gcode.append('M9  ; Air blast off')
        gcode.append('M5')
        gcode.append('G54  ; Reset to standard work coordinate system')
        gcode.append('M30')

        # Estimate cycle time
        time_estimate = self._estimate_cycle_time(gcode)

        # Collect stats
        num_holes = len(self.holes) if hasattr(self, 'holes') else 0
        num_pockets = len(self.pockets) if hasattr(self, 'pockets') else 0

        # Generate filename with timestamp
        base_name = suggested_filename if suggested_filename else "tube_pattern"
        # Format timestamp for filename: YYYYMMDD_HHMMSS
        timestamp_for_file = timestamp.replace('-', '').replace(' ', '_').replace(':', '')
        filename = f"{base_name}_{timestamp_for_file}.nc"

        # Build operation notes based on configuration
        operation_notes = []
        if square_end:
            operation_notes.extend([
                'Phase 0: Square tube end',
                'Flip tube end-for-end (M0)',
                'Phase 0: Square opposite end',
                'Phase 1: Machine pattern on first face',
                'Flip tube 180° around Y-axis (M0)',
                'Phase 2: Machine pattern on opposite face (mirrored)'
            ])
        else:
            operation_notes.extend([
                'Phase 1: Machine pattern on first face',
                'Flip tube 180° around Y-axis (M0)',
                'Phase 2: Machine pattern on opposite face (mirrored)'
            ])
        if cut_to_length:
            operation_notes.append(f'Cut to length: Y={tube_length}" (each phase)')

        # Return result
        return PostProcessorResult(
            success=True,
            gcode='\n'.join(gcode),
            filename=filename,
            warnings=[],
            stats={
                'operation': 'tube_pattern',
                'tube_height': tube_height,
                'tube_width': tube_width,
                'tube_length': tube_length,
                'square_end': square_end,
                'cut_to_length': cut_to_length,
                'num_holes': num_holes,
                'num_pockets': num_pockets,
                'num_holes_per_face': num_holes,
                'num_pockets_per_face': num_pockets,
                'has_perimeter': False,
                'total_lines': len(gcode),
                'cycle_time_seconds': time_estimate['total'],
                'cycle_time_display': self._format_time(time_estimate['total']),
                'cutting_time': self._format_time(time_estimate['cutting']),
                'rapid_time': self._format_time(time_estimate['rapid']),
                'dwell_time': self._format_time(time_estimate['dwell']),
                'setup_instructions': [
                    'Mount tube in jig with end facing spindle',
                    'Verify G55 is set to jig origin',
                    'Origin (0,0,0) = bottom-left corner of tube face'
                ],
                'operation_notes': operation_notes
            }
        )

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
                    hole['diameter'],   # diameter
                    needs_peck_drill=hole.get('needs_peck_drill', False)
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

    def _generate_toolpath_gcode_mirrored_x(self, z_offset: float = 0.0, tube_width: float = 1.0,
                                            y_offset: float = 0.0) -> list[str]:
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
        - Feature Y coordinates get offset to account for facing: Y_new = Y_old + y_offset
        - Toolpaths are regenerated from mirrored geometry

        Args:
            z_offset: Offset to add to all Z coordinates (for tube mode)
            tube_width: Width of tube face for mirroring X around centerline
            y_offset: Offset to add to all Y coordinates (for tube facing alignment)
        """
        toolpath = []

        # Generate toolpaths for mirrored holes
        if hasattr(self, 'holes') and self.holes:
            for hole in self.holes:
                # Mirror the hole center around tube centerline
                original_cx = hole['center'][0]
                original_cy = hole['center'][1]
                mirrored_cx = tube_width - original_cx
                mirrored_cy = original_cy + y_offset  # Apply Y offset for facing alignment

                # Generate fresh toolpath for the mirrored hole
                # This preserves helical entry + outward spiral safety
                toolpath.extend(self._generate_hole_gcode(
                    mirrored_cx, mirrored_cy, hole['diameter'],
                    needs_peck_drill=hole.get('needs_peck_drill', False)
                ))

        # Generate toolpaths for mirrored pockets
        if hasattr(self, 'pockets') and self.pockets:
            for pocket in self.pockets:
                # Mirror all pocket points around tube centerline and apply Y offset
                mirrored_pocket = [(tube_width - x, y + y_offset) for x, y in pocket]
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

        Legacy method - wraps generic _offset_coordinate() for backwards compatibility.
        """
        return self._offset_coordinate(line, 'Z', z_offset)

    def _offset_y_coordinate(self, line: str, y_offset: float) -> str:
        """
        Offset Y coordinate in a G-code line by adding y_offset.

        For tube mode first face: Y offset accounts for material that will be removed during facing.
        If rough end will be milled back by 0.125", pattern must be positioned 0.125" deeper.

        Legacy method - wraps generic _offset_coordinate() for backwards compatibility.
        """
        return self._offset_coordinate(line, 'Y', y_offset)

    def _generate_cut_to_length(self, tube_width: float, tube_height: float,
                                 tube_length: float, phase: int, square_end: bool) -> list[str]:
        """
        Generate G-code to cut tube to length using arc clearing pattern.

        Uses the same technique as tube facing:
        - Arc clearing pattern for roughing (reduces chip load)
        - Straight finishing pass
        - Single plunge to just over half tube height
        - 1.5x tool diameter clearance outside tube
        - Phase-specific Y offsets for alignment

        Coordinate system:
        - X: across tube width (cut direction)
        - Z: tube height (plunge direction, vertical)
        - Y: along tube length (cut position)

        Args:
            tube_width: Width of tube (X dimension)
            tube_height: Height of tube (Z dimension)
            tube_length: Desired tube length (Y dimension)
            phase: 1 (before flip) or 2 (after flip)
            square_end: Whether the tube end was squared (affects Phase 1 offset)

        Returns:
            List of G-code lines
        """
        gcode = []
        tool_radius = self.tool_diameter / 2.0

        # Calculate pass parameters using shared helper
        passes = self._calculate_tube_operation_passes(tube_height)
        total_depth = passes['total_depth']
        wall_thickness = passes['wall_thickness']
        num_roughing_passes = passes['num_roughing_passes']
        roughing_depth_per_pass = passes['roughing_depth_per_pass']
        num_finishing_passes = passes['num_finishing_passes']
        finishing_depth_per_pass = passes['finishing_depth_per_pass']

        # Y offset for cut position
        # Phase 1: Add tube_facing_offset ONLY if square_end=True (material removed from front)
        # Phase 2: No offset (coordinate system reset after flip)
        if phase == 1:
            # Phase 1: Cut at tube_length + optional facing offset + tool radius compensation
            if square_end:
                y_cut = tube_length + self.tube_facing_offset + self.tool_radius
            else:
                y_cut = tube_length + self.tool_radius
            z_start = tube_height  # Top of tube (tube sits on sacrifice board at Z=0)
            gcode.append(f'( Cut to length at Y={y_cut:.4f}" [Phase 1: before flip] )')
        else:
            # Phase 2: Cut at tube_length + tool radius compensation (no facing offset)
            y_cut = tube_length + self.tool_radius
            z_start = tube_height  # Top of tube
            gcode.append(f'( Cut to length at Y={y_cut:.4f}" [Phase 2: after flip] )')

        # For cut to length, the tool's -Y edge defines the kept part boundary
        # (opposite of tube facing where +Y edge defines the face)
        # Roughing leaves 0.0125" for finishing pass
        finish_stock = 0.0125  # Material left for finishing

        # Arc clearing parameters (same as tube facing)
        arc_advance = 0.04  # How far each arc advances in X
        arc_radius = 0.05  # Arc radius
        half_advance = arc_advance / 2
        j_offset = math.sqrt(arc_radius**2 - half_advance**2)

        # Tool CENTER positions for cut to length:
        # - The kept part is at Y < y_cut, waste is at Y > y_cut
        # - Tool's -Y edge (toward kept part) defines the cut boundary
        #
        # With positive J, G3 (CCW) arc goes through TOP of circle (max Y, into waste).
        # Arc center Y = roughing_y + j_offset
        # Top of circle Y = center_y + arc_radius = roughing_y + j_offset + arc_radius
        #
        # At arc CHORD (start/end): tool center Y = roughing_y, tool -Y edge = roughing_y - tool_radius
        # At arc PEAK (top of circle): tool center Y = roughing_y + j_offset + arc_radius (in waste)
        #
        # The CHORD is where tool -Y edge is closest to kept part (the limit for roughing).
        # For roughing to leave finish_stock, the -Y edge at chord should be at finish_stock from kept edge:
        #   roughing_y - tool_radius = (tube_length + finish_stock)
        #   roughing_y = tube_length + finish_stock + tool_radius
        # Since y_cut already equals tube_length + tool_radius:
        #   roughing_y = y_cut + finish_stock
        roughing_y = y_cut + finish_stock
        finishing_y = y_cut  # y_cut is already the tool center position

        # Calculate peak position for comments
        peak_y = roughing_y + j_offset + arc_radius  # Tool center at peak
        peak_minus_edge = peak_y - tool_radius  # Tool -Y edge at peak (in waste)

        # X positions (tool edge 0.05" from material edge)
        clearance = tool_radius + 0.05
        start_x = tube_width + clearance  # Far side
        end_x = -clearance  # Near side

        # Z positions
        z_top = tube_height  # Top of tube
        z_safe = tube_height + 0.25  # Safe height above tube
        z_final = z_top - total_depth  # Final depth (just over half height)

        gcode.append(f'( Tube width: {tube_width:.2f}" x height: {tube_height:.2f}" )')
        gcode.append(f'( Tool: {self.tool_diameter:.3f}" )')
        gcode.append(f'( Total depth: {total_depth:.3f}" )')
        gcode.append(f'( Roughing: {num_roughing_passes} passes of {roughing_depth_per_pass:.3f}" each, leaves {finish_stock:.4f}" for finishing )')
        gcode.append(f'( Finishing: {num_finishing_passes} passes of {finishing_depth_per_pass:.3f}" each, -Y edge at Y={y_cut:.4f}" )')
        gcode.append('')

        # === ROUGHING PASSES ===
        # Use arc clearing pattern to reduce chip load
        arc_feed = self.feed_rate  # Full feed rate

        gcode.append('( === ROUGHING PASSES === )')
        gcode.append(f'( {num_roughing_passes} depth passes with arc clearing )')

        # Calculate wall boundaries for subsequent passes (box tubing is hollow)
        # Back wall (far side): from start_x to inner edge
        back_wall_inner_x = tube_width - wall_thickness - clearance
        # Front wall (near side): from inner edge to end_x
        front_wall_inner_x = wall_thickness + clearance

        for pass_num in range(num_roughing_passes):
            z_cut = z_top - (pass_num + 1) * roughing_depth_per_pass

            if pass_num == 0:
                # First pass: full arc pattern across entire width
                gcode.append(f'( Roughing pass {pass_num + 1}/{num_roughing_passes} to Z={z_cut:.3f}" - full width )')

                # Position at start (combine X Y for cleaner G-code)
                gcode.append(f'G0 X{start_x:.4f} Y{roughing_y:.4f}')
                gcode.append(f'G0 Z{z_safe:.4f}')

                # Plunge to cut depth
                gcode.append(f'G0 Z{z_cut:.4f}')  # Rapid plunge (in air)

                # Arc clearing pattern across tube width
                gcode.append(f'G1 F{arc_feed}')
                current_x = start_x
                while current_x > end_x + arc_advance:
                    next_x = current_x - arc_advance
                    gcode.append(f'G3 X{next_x:.4f} Y{roughing_y:.4f} I{-half_advance:.4f} J{j_offset:.4f}')
                    current_x = next_x

                # Final linear move to end position if needed
                if current_x > end_x:
                    gcode.append(f'G1 X{end_x:.4f}')

                # Retract after this pass
                gcode.append(f'G0 Z{z_safe:.4f}')
            else:
                # Subsequent passes: cut walls only, rapid across hollow middle
                gcode.append(f'( Roughing pass {pass_num + 1}/{num_roughing_passes} to Z={z_cut:.3f}" - walls only )')

                # Position at start (back wall)
                gcode.append(f'G0 X{start_x:.4f} Y{roughing_y:.4f}')
                gcode.append(f'G0 Z{z_safe:.4f}')

                # Plunge to cut depth
                gcode.append(f'G0 Z{z_cut:.4f}')  # Rapid plunge (in air)

                # Arc clearing through back wall only
                gcode.append(f'G1 F{arc_feed}')
                current_x = start_x
                while current_x > back_wall_inner_x + arc_advance:
                    next_x = current_x - arc_advance
                    gcode.append(f'G3 X{next_x:.4f} Y{roughing_y:.4f} I{-half_advance:.4f} J{j_offset:.4f}')
                    current_x = next_x

                # Finish back wall
                if current_x > back_wall_inner_x:
                    gcode.append(f'G1 X{back_wall_inner_x:.4f}')

                # Retract, rapid across hollow middle
                gcode.append(f'G0 Z{z_safe:.4f}')
                gcode.append(f'G0 X{front_wall_inner_x:.4f}')

                # Plunge inside (material already removed on pass 1)
                gcode.append(f'G0 Z{z_cut:.4f}')  # Rapid plunge (in air)

                # Arc clearing through front wall
                gcode.append(f'G1 F{arc_feed}')
                current_x = front_wall_inner_x
                while current_x > end_x + arc_advance:
                    next_x = current_x - arc_advance
                    gcode.append(f'G3 X{next_x:.4f} Y{roughing_y:.4f} I{-half_advance:.4f} J{j_offset:.4f}')
                    current_x = next_x

                # Final linear move to end position if needed
                if current_x > end_x:
                    gcode.append(f'G1 X{end_x:.4f}')

                # Retract after this pass
                gcode.append(f'G0 Z{z_safe:.4f}')

        gcode.append(f'( Roughing complete: {num_roughing_passes} passes )')

        # === FINISHING PASSES ===
        gcode.append('( === FINISHING PASSES === )')
        gcode.append(f'( {num_finishing_passes} depth passes, removes {finish_stock:.4f}" )')

        for pass_num in range(num_finishing_passes):
            z_cut = z_top - (pass_num + 1) * finishing_depth_per_pass

            if pass_num == 0:
                # First pass: full cut across entire width
                gcode.append(f'( Finishing pass {pass_num + 1}/{num_finishing_passes} to Z={z_cut:.3f}" - full width )')

                # Position for finishing
                gcode.append(f'G0 X{start_x:.4f} Y{finishing_y:.4f}')

                # Plunge to cut depth
                gcode.append(f'G0 Z{z_cut:.4f}')  # Rapid plunge (in air)

                # Single horizontal cut across
                gcode.append(f'G1 X{end_x:.4f} F{self.feed_rate}')

                # Retract
                gcode.append(f'G0 Z{z_safe:.4f}')
            else:
                # Subsequent passes: cut walls only, rapid across hollow middle
                gcode.append(f'( Finishing pass {pass_num + 1}/{num_finishing_passes} to Z={z_cut:.3f}" - walls only )')

                # Position at start (back wall)
                gcode.append(f'G0 X{start_x:.4f} Y{finishing_y:.4f}')

                # Plunge to cut depth
                gcode.append(f'G0 Z{z_cut:.4f}')  # Rapid plunge (in air)

                # Cut through back wall only
                gcode.append(f'G1 X{back_wall_inner_x:.4f} F{self.feed_rate}')

                # Retract, rapid across hollow middle
                gcode.append(f'G0 Z{z_safe:.4f}')
                gcode.append(f'G0 X{front_wall_inner_x:.4f}')

                # Plunge inside (material already removed on pass 1)
                gcode.append(f'G0 Z{z_cut:.4f}')  # Rapid plunge (in air)

                # Cut through front wall
                gcode.append(f'G1 X{end_x:.4f} F{self.feed_rate}')

                # Retract
                gcode.append(f'G0 Z{z_safe:.4f}')

        return gcode


def add_timestamp_to_filename(filename: str) -> str:
    """Add timestamp to filename before extension."""
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = os.path.splitext(filename)[0]
    extension = os.path.splitext(filename)[1]
    return f"{base_name}_{timestamp}{extension}"


def main():
    parser = argparse.ArgumentParser(description='PenguinCAM - Team 6238 Post-Processor')
    parser.add_argument('input_dxf', nargs='?', help='Input DXF file from Onshape (not needed for tube-facing mode)')
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
                       help='Machine tube to length after pattern (tube-pattern mode)')
    parser.add_argument('--material', type=str, default='plywood',
                       help='Material preset (default: plywood). Built-in: plywood, aluminum, polycarbonate. Custom materials from config also supported.')
    parser.add_argument('--thickness', type=float, default=0.25,
                       help='Material thickness in inches (default: 0.25)')
    parser.add_argument('--tool-diameter', type=float, default=0.157,
                       help='Tool diameter in inches (default: 0.157" = 4mm)')
    parser.add_argument('--sacrifice-depth', type=float, default=0.02,
                       help='How far to cut into sacrifice board in inches (default: 0.02")')
    parser.add_argument('--units', choices=['inch', 'mm'], default='inch',
                       help='Units (default: inch)')
    parser.add_argument('--tab-spacing', type=float, default=6.0,
                       help='Desired spacing between tabs in inches (default: 6.0, minimum 3 tabs)')
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

        # Call API to generate G-code
        base_name = os.path.splitext(os.path.basename(args.output_gcode))[0]
        result = pp.generate_tube_facing_gcode(tube_size=args.tube_size, suggested_filename=base_name)

        if not result.success:
            print(f"ERROR: Failed to generate G-code")
            for error in result.errors:
                print(f"  - {error}")
            sys.exit(1)

        # Write G-code to file
        output_path = os.path.join(os.path.dirname(args.output_gcode) or '.', result.filename)
        with open(output_path, 'w') as f:
            f.write(result.gcode)

        # Print output for CLI
        print(f'OUTPUT_FILE:{output_path}')
        print(f'Tube facing G-code generated for {args.tube_size} tube')
        print(f"\nIdentified 0 millable holes and 0 pockets")
        print(f"Total lines: {result.stats['total_lines']}")
        print(f"\n⏱️  ESTIMATED_CYCLE_TIME: {result.stats['cycle_time_seconds']:.1f} seconds ({result.stats['cycle_time_display']})")
        print(f'\nSETUP:')
        for instruction in result.stats['setup_instructions']:
            print(f'  {instruction}')
        print(f'\nOPERATION:')
        for note in result.stats['operation_notes']:
            print(f'  {note}')

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

        # Call API to generate G-code
        base_name = os.path.splitext(os.path.basename(args.output_gcode))[0]
        result = pp.generate_tube_pattern_gcode(
            tube_height=args.tube_height,
            square_end=args.square_end,
            cut_to_length=args.cut_to_length,
            tube_width=args.tube_width,
            tube_length=args.tube_length,
            suggested_filename=base_name
        )

        if not result.success:
            print(f"ERROR: Failed to generate G-code")
            for error in result.errors:
                print(f"  - {error}")
            sys.exit(1)

        # Write G-code to file
        output_path = os.path.join(os.path.dirname(args.output_gcode) or '.', result.filename)
        with open(output_path, 'w') as f:
            f.write(result.gcode)

        # Print output for CLI
        print(f'OUTPUT_FILE:{output_path}')
        print(f'Tube pattern G-code generated')
        print(f"\nIdentified {result.stats['num_holes_per_face']} millable holes and {result.stats['num_pockets_per_face']} pockets on each face")
        print(f"Total lines: {result.stats['total_lines']}")
        print(f"\n⏱️  ESTIMATED_CYCLE_TIME: {result.stats['cycle_time_seconds']:.1f} seconds ({result.stats['cycle_time_display']})")
        print(f'\nSETUP:')
        for instruction in result.stats['setup_instructions']:
            print(f'  {instruction}')
        print(f'\nOPERATIONS:')
        for note in result.stats['operation_notes']:
            print(f'  {note}')

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
        pp.tab_spacing = args.tab_spacing
        pp.sacrifice_board_depth = args.sacrifice_depth
        pp.cut_depth = -pp.sacrifice_board_depth

        # Load and process DXF (shared logic)
        pp.load_dxf(args.input_dxf)
        pp.transform_coordinates(args.origin_corner, args.rotation)
        pp.classify_holes()
        pp.identify_perimeter_and_pockets()

        # Call API to generate G-code
        base_name = os.path.splitext(os.path.basename(args.output_gcode))[0]
        result = pp.generate_gcode(suggested_filename=base_name)

        if not result.success:
            print(f"ERROR: Failed to generate G-code")
            for error in result.errors:
                print(f"  - {error}")
            sys.exit(1)

        # Write G-code to file
        output_path = os.path.join(os.path.dirname(args.output_gcode) or '.', result.filename)
        with open(output_path, 'w') as f:
            f.write(result.gcode)

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
        print(f"  Retract height: Z={pp.retract_height:.4f}\"")
        print(f"\nTool compensation applied:")
        print(f"  Tool diameter: {pp.tool_diameter:.4f}\"")
        print(f"  Tool radius: {pp.tool_radius:.4f}\"")
        print(f"  Perimeter: offset OUTWARD by {pp.tool_radius:.4f}\"")
        print(f"  Pockets: offset INWARD by {pp.tool_radius:.4f}\"")
        print(f"  Holes: toolpath radius reduced by {pp.tool_radius:.4f}\" (holes < {pp.min_millable_hole:.3f}\" skipped)")


if __name__ == '__main__':
    main()
