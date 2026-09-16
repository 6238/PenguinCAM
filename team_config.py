"""
Team Configuration Management for PenguinCAM

Handles loading and managing team-specific settings from YAML config files
stored in Onshape documents. Falls back to Team 6238 defaults if config
is missing or incomplete.
"""

import copy
import re
import yaml
from typing import Optional, Dict, Any


# =============================================================================
# LENGTH PARSING (units in config values)
# =============================================================================
# PenguinCAM is inch-native, but config values may carry a unit so metric and SAE
# tooling can be expressed naturally (e.g. "4mm", '0.25"', "1/8", "3 cm"). Numbers are
# treated as inches. Mirrors the client-side parser in static/wizard.js.

_LENGTH_TO_INCH = {
    '': 1.0, 'in': 1.0, 'inch': 1.0, 'inches': 1.0, '"': 1.0,
    'mm': 1 / 25.4, 'millimeter': 1 / 25.4, 'millimeters': 1 / 25.4,
    'cm': 1 / 2.54, 'centimeter': 1 / 2.54, 'centimeters': 1 / 2.54,
    'm': 1 / 0.0254, 'meter': 1 / 0.0254, 'meters': 1 / 0.0254,
    'ft': 12.0, 'foot': 12.0, 'feet': 12.0, "'": 12.0,
    'yd': 36.0, 'yard': 36.0, 'yards': 36.0,
}
_FRACTION_RE = re.compile(r'^([+-]?\d+)\s*/\s*(\d+)\s*(.*)$')
_DECIMAL_RE = re.compile(r'^([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(.*)$')

# Default tool when a config doesn't specify one: 4mm end mill (in inches).
DEFAULT_TOOL_DIAMETER_IN = 4.0 / 25.4

# Leaf keys whose values are lengths and may therefore be given as unit strings. Only
# these are converted during config normalization, so material names, work offsets, feed
# rates, angles, and ratios are never misinterpreted. 'diameter' is intentionally absent:
# the default_tool_diameter properties parse it directly so the raw text stays available
# for display.
LENGTH_KEYS = frozenset({
    'x_max', 'y_max', 'z_max',                                   # machine.dimensions
    'x', 'y', 'z',                                               # machine.park_position
    'sacrifice_board_depth', 'clearance_height', 'safe_height',  # machining.z_reference
    'width', 'height', 'spacing',                               # machining.tabs
    'depth_margin', 'max_roughing_depth', 'max_finishing_depth',  # tube_facing
    'roughing_tool_edge', 'finishing_tool_edge', 'arc_advance', 'arc_radius',
    'ramp_start_clearance', 'max_slotting_depth', 'peck_drill_depth',  # materials
    'tab_width', 'tab_height',
})


def parse_length(value):
    """Parse a possibly-unit-bearing length into inches, or None if unparseable.

    Numbers pass through as inches. Strings may use mm/cm/m/in/"/ft/'/yd, plain numbers,
    or fractions (e.g. "1/8"); no unit means inches. Negative values are allowed because
    some config offsets (park Z, tube-facing edges) are legitimately negative.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lower()
    if not s:
        return None
    m = _FRACTION_RE.match(s)
    if m:
        denom = float(m.group(2))
        if denom == 0:
            return None
        number = float(m.group(1)) / denom
        unit = m.group(3).strip()
    else:
        m = _DECIMAL_RE.match(s)
        if not m:
            return None
        number = float(m.group(1))
        unit = m.group(2).strip()
    factor = _LENGTH_TO_INCH.get(unit)
    if factor is None:
        return None
    return number * factor


def _normalize_lengths(node):
    """Recursively convert unit-string length values (on LENGTH_KEYS) to inch floats,
    in place. Numbers and non-length strings are left untouched."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                _normalize_lengths(value)
            elif isinstance(value, str) and key in LENGTH_KEYS:
                parsed = parse_length(value)
                if parsed is not None:
                    node[key] = parsed
    elif isinstance(node, list):
        for item in node:
            _normalize_lengths(item)


# =============================================================================
# TEAM 6238 DEFAULTS
# These are used as fallbacks when config values are missing
# =============================================================================

TEAM_6238_DEFAULTS = {
    'team': {
        'number': 6238,
        'name': 'Popcorn Penguins'
    },
    'machine': {
        'name': 'Generic CNC Router',
        'manufacturer': 'Generic',
        'controller': 'Generic',
        'dimensions': {'x_max': 24.0, 'y_max': 24.0, 'z_max': 8.0},
        # park_position, coolant, and tube_work_coordinate_system are intentionally NOT
        # defaulted: they are opt-in. A config with no park_position emits portable G54-only
        # output (no G53); no coolant -> no M7/M8/M9; no tube_work_coordinate_system -> tube
        # ops use G54 (operator zeros it per tube), instead of a fixed jig WCS. Machines that
        # want these (e.g. Mach4 routers with a fixed jig) add them to their config. Keeps
        # output compatible with GRBL/WinCNC.
    },
    'machining': {
        'z_reference': {
            'sacrifice_board_depth': 0.008,
            'clearance_height': 0.5
        },
        'tabs': {
            'enabled': True,
            'width': 0.25,
            'height': 0.1,
            'spacing': 6.0,
            'remove_tabs': True
        },
        'fixturing': {
            'pause_before_perimeter': False
        },
        'holes': {
            'detection_tolerance': 0.02,
            'min_millable_multiplier': 1.2
        },
        'pockets': {
            # Contour threshold: feature area in multiples of tool cross-section (at 100% stepover)
            # Applies to both irregular pockets AND large circular holes (through-cuts only)
            # IMPORTANT: Only applies to through-cuts (Z <= 0); partial-depth features always cleared
            # Formula: threshold_area = contour_threshold × tool_diameter² × stepover_percentage
            # Actual threshold scales directly with stepover (tighter stepover = lower area threshold)
            # Default: 510 (aluminum: ~2.0" dia, plywood: ~3.2" dia, polycarbonate: ~3.0" dia)
            # Set to 0 to disable (always fully clear all features)
            'contour_threshold': 510
        },
        'default_tool': {
            'diameter': '4mm'  # default when unspecified; parsed to inches for machining
        }
    },
    'tube_facing': {
        'depth_margin': 0.005,
        'max_roughing_depth': 0.3,
        'max_finishing_depth': 0.51,
        'phase_1': {
            'roughing_tool_edge': 0.05,
            'finishing_tool_edge': 0.0625
        },
        'phase_2': {
            'roughing_tool_edge': -0.0125,
            'finishing_tool_edge': 0.0
        },
        'arc_advance': 0.04,
        'arc_radius': 0.05
    },
    'materials': {
        'plywood': {
            'name': 'Plywood',
            'spindle_speed': 18000,
            'feed_rate': 75.0,
            'ramp_feed_rate': 50.0,
            'plunge_rate': 35.0,
            'traverse_rate': 200.0,
            'approach_rate': 50.0,
            'ramp_angle': 20.0,
            'ramp_start_clearance': 0.150,
            'stepover_percentage': 0.65,
            'helix_radius_multiplier': 0.75,
            'max_slotting_depth': 0.4,
            'peck_drill_depth': 0.05,
            'corner_min_feed_scale': 0.7,   # softer/heat-limited: keep feed up to preserve chip load
            'tab_width': 0.25,
            'tab_height': 0.15
        },
        'aluminum': {
            'name': 'Aluminum',
            'spindle_speed': 18000,
            'feed_rate': 55.0,
            'ramp_feed_rate': 35.0,
            'plunge_rate': 15.0,
            'traverse_rate': 200.0,
            'approach_rate': 35.0,
            'ramp_angle': 4.0,
            'ramp_start_clearance': 0.050,
            'stepover_percentage': 0.25,
            'helix_radius_multiplier': 0.5,
            'max_slotting_depth': 0.2,
            'peck_drill_depth': 0.05,
            'corner_min_feed_scale': 0.4,   # force-limited: aggressive corner slowdown to protect the tool
            'tab_width': 0.25,
            'tab_height': 0.15
        },
        'polycarbonate': {
            'name': 'Polycarbonate',
            'spindle_speed': 18000,
            'feed_rate': 75.0,
            'ramp_feed_rate': 50.0,
            'plunge_rate': 20.0,
            'traverse_rate': 200.0,
            'approach_rate': 50.0,
            'ramp_angle': 20.0,
            'ramp_start_clearance': 0.100,
            'stepover_percentage': 0.55,
            'helix_radius_multiplier': 0.75,
            'max_slotting_depth': 0.25,
            'peck_drill_depth': 0.05,
            'corner_min_feed_scale': 0.7,   # softer/heat-limited: keep feed up to preserve chip load
            'tab_width': 0.25,
            'tab_height': 0.15
        }
    },
    'integrations': {
        'google_drive': {
            'enabled': False,
            'folder_id': None
        }
    }
}


def extract_drive_folder_id(folder_value: Optional[str]) -> Optional[str]:
    """
    Normalize a configured Google Drive folder value to a bare folder ID.

    Teams are told to paste the folder's Drive URL, but the upload path needs the
    bare ID (it is used directly as a `parents` entry). Both the
    TeamConfig.google_drive_folder_id property and to_dict() go through here so the
    session dict can never carry an un-normalized URL.
    """
    if not folder_value:
        return None

    # Format: https://drive.google.com/drive/folders/FOLDER_ID
    # or:     https://drive.google.com/drive/u/0/folders/FOLDER_ID
    if 'drive.google.com' in folder_value:
        parts = folder_value.split('/folders/')
        if len(parts) == 2:
            # Remove any query parameters or trailing slashes
            return parts[1].split('?')[0].rstrip('/')

    # Otherwise assume it's already just the ID
    return folder_value


class TeamConfig:
    """
    Manages team-specific configuration for PenguinCAM.

    Config is loaded from a YAML file stored in the team's Onshape documents
    named "PenguinCAM-config.yaml". Falls back to Team 6238 defaults for any
    missing values.
    """

    def __init__(self, config_data: Optional[Dict[str, Any]] = None,
                 machine_id: Optional[str] = None):
        """
        Initialize team config from YAML data.

        Args:
            config_data: Parsed YAML config dict, or None for defaults
            machine_id: Machine this config reads from; None means the config's default
                machine. See for_machine() - callers that know which machine a job is for
                should bind rather than pass machine_id to individual accessors.
        """
        if config_data is None:
            config_data = {}

        # Normalize to v2 structure internally for consistent API. Deep-copy first so we
        # never mutate the caller's dict (e.g. the session config), then convert any
        # unit-string length values (e.g. "4mm") to inch floats so all downstream readers
        # see plain numbers.
        self._data = copy.deepcopy(self._normalize_to_v2(config_data))
        _normalize_lengths(self._data)

        # Every setting resolves through _get() -> get_machine_config(), so this one field
        # is what makes a multi-machine (v2) config actually apply the SELECTED machine's
        # settings. Before it existed, _get() hardcoded the default machine and only
        # materials honored the selection, so a second machine's tabs/pockets/z_reference/
        # park/coolant blocks were silently ignored.
        self._active_machine_id = machine_id

    def _normalize_to_v2(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert any config version to v2 structure internally.

        v1: Top-level machine/materials/integrations
        v2: machines -> machine_id -> machine/materials/integrations

        Args:
            data: Raw config data

        Returns:
            Normalized v2 structure
        """
        version = data.get('version', 1)

        if version == 1:
            # Wrap v1 config as single machine named 'default'
            # Copy all keys except 'version' into the default machine
            machine_config = {}
            for key, value in data.items():
                if key != 'version':
                    machine_config[key] = value

            # Ensure the wrapped machine has a display name. In a v1 config the
            # name lives nested under `machine.name`, not at the top level, so
            # promote it (falling back to a generic label only if truly absent).
            if 'name' not in machine_config:
                nested_name = machine_config.get('machine', {}).get('name')
                machine_config['name'] = nested_name or 'Default Machine'

            return {
                'version': 2,
                'default_machine': 'default',
                'machines': {
                    'default': machine_config
                }
            }

        elif version == 2:
            # Already v2, use as-is
            return data

        else:
            raise ValueError(f"Unsupported config version: {version}")

    def _get(self, *keys, default=None):
        """
        Safely get nested dict value with fallback to Team 6238 defaults.

        For v2 configs, checks root level first (for 'team'), then the ACTIVE machine's
        config (see for_machine; the default machine when nothing is bound).

        Args:
            *keys: Path to nested value (e.g., 'machine', 'park_position', 'x')
            default: Optional override default (otherwise uses TEAM_6238_DEFAULTS)

        Returns:
            Value from config, or from TEAM_6238_DEFAULTS, or provided default
        """
        # Special case: 'team' is at root level in v2 configs, not in machine config
        if keys and keys[0] == 'team':
            value = self._data
            for key in keys:
                if isinstance(value, dict):
                    value = value.get(key)
                    if value is None:
                        break
                else:
                    value = None
                    break

            if value is not None:
                return value

        # Get the active machine's config (handles both v1 wrapped and v2 native)
        machine_config = self.get_machine_config()

        # Try to get from machine config
        value = machine_config
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
                if value is None:
                    break
            else:
                value = None
                break

        # If found in machine config, return it
        if value is not None:
            return value

        # Fall back to Team 6238 defaults
        default_value = TEAM_6238_DEFAULTS
        for key in keys:
            if isinstance(default_value, dict):
                default_value = default_value.get(key)
                if default_value is None:
                    break
            else:
                default_value = None
                break

        # Return default_value from TEAM_6238_DEFAULTS, or provided default
        return default_value if default_value is not None else default

    # ========================================================================
    # Machine Management (v2 Config Support)
    # ========================================================================

    def get_available_machines(self) -> Dict[str, Dict[str, Any]]:
        """
        Get all available machines from config.

        Returns:
            Dictionary mapping machine_id to machine info (name, machine config, etc.)
        """
        return self._data.get('machines', {})

    @property
    def default_machine_id(self) -> str:
        """Get default machine ID.

        A v2 config whose `default_machine` is missing or names a machine that isn't
        defined falls back to the FIRST machine in the file. Without that fallback the id
        resolves to a nonexistent key and every setting silently reverts to the built-in
        Team 6238 defaults, which looks like "my config is being ignored"."""
        machines = self._data.get('machines', {})
        declared = self._data.get('default_machine')
        if declared in machines:
            return declared
        if machines:
            return next(iter(machines))
        return declared or 'default'

    @property
    def active_machine_id(self) -> str:
        """Machine this config currently reads from: the bound machine (see for_machine),
        or the default machine when nothing is bound or the bound id is unknown."""
        machine_id = self._active_machine_id
        if machine_id is not None and machine_id in self._data.get('machines', {}):
            return machine_id
        return self.default_machine_id

    def for_machine(self, machine_id: Optional[str]) -> 'TeamConfig':
        """Return this config bound to `machine_id`, so every setting - not just materials
        - comes from that machine.

        Returns self when `machine_id` is None or already active. Otherwise a shallow copy
        that SHARES the normalized (read-only) config data, so binding per request is cheap
        and can never mutate an instance held elsewhere (e.g. one built from the session).
        An unknown id falls back to the default machine, matching get_machine_config.
        """
        if machine_id is None or machine_id == self._active_machine_id:
            return self
        bound = copy.copy(self)
        bound._active_machine_id = machine_id
        return bound

    def get_machine_config(self, machine_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Get config for a specific machine.

        Args:
            machine_id: Machine ID, or None for the active machine (the bound machine if
                for_machine() was used, else the default machine)

        Returns:
            Machine configuration dict
        """
        if machine_id is None:
            machine_id = self._active_machine_id or self.default_machine_id

        machines = self._data.get('machines', {})
        return machines.get(machine_id, machines.get(self.default_machine_id, {}))

    # ========================================================================
    # Team Information
    # ========================================================================

    @property
    def team_number(self) -> int:
        """FRC team number"""
        return self._get('team', 'number')

    @property
    def team_name(self) -> str:
        """FRC team name"""
        return self._get('team', 'name')

    # ========================================================================
    # Machine Configuration
    # ========================================================================

    @property
    def machine_name(self) -> str:
        """Machine model name"""
        return self._get('machine', 'name')

    @property
    def machine_manufacturer(self) -> str:
        """Machine manufacturer"""
        return self._get('machine', 'manufacturer')

    @property
    def machine_controller(self) -> str:
        """Machine controller type (Mach3, Mach4, LinuxCNC, etc.)"""
        return self._get('machine', 'controller')

    @property
    def machine_park_x(self) -> float:
        """Machine park X position (machine coordinates)"""
        return self._get('machine', 'park_position', 'x')

    @property
    def machine_park_y(self) -> float:
        """Machine park Y position (machine coordinates)"""
        return self._get('machine', 'park_position', 'y')

    @property
    def machine_park_z(self) -> float:
        """Machine park Z position (machine coordinates, safe clearance)"""
        return self._get('machine', 'park_position', 'z')

    @property
    def park_position(self):
        """Optional machine-coordinate park as (x, y, z), or None if not configured.

        Only when this is set does the post-processor emit the G53 machine-coordinate
        park (raise Z, move gantry to a fixed spot). Absent -> no G53 anywhere, so the
        output stays portable across controllers (GRBL/Easel/WinCNC)."""
        x = self._get('machine', 'park_position', 'x')
        y = self._get('machine', 'park_position', 'y')
        z = self._get('machine', 'park_position', 'z')
        if x is None or y is None or z is None:
            return None
        return (x, y, z)

    @property
    def safe_clearance_height(self):
        """Configured "clear everything" height over Z=0 (sacrifice board) for the G54
        bed-crossing moves (first-cut approach, end retract, multi-part rapids, tube-flip
        pause). Set above the tallest fixture. None -> fall back to material_thickness +
        clearance (just above the stock). From z_reference.safe_height."""
        return self._get('machining', 'z_reference', 'safe_height', default=None)

    @property
    def machine_coolant(self) -> str:
        """Machine coolant type (Air, Flood, Mist) or None. Opt-in: no value -> no coolant
        M-codes are emitted (portable across controllers)."""
        return self._get('machine', 'coolant')

    @property
    def tube_work_coordinate_system(self) -> str:
        """Work coordinate system the tube jig is zeroed in. Defaults to 'G54' (portable:
        the operator zeros G54 to the tube for each job, exactly like flat work). Teams with
        a permanently-fixtured jig can set an alternate FIXED WCS (e.g. 'G55') so the jig
        zero persists in its own coordinate system while G54 stays their per-stock flat zero.
        Only G54-G59 (the bank supported by GRBL/Mach/WinCNC) are honored; anything else
        (including an unset G59.x sub-system) falls back to 'G54'."""
        value = self._get('machine', 'tube_work_coordinate_system')
        if value is None:
            return 'G54'
        normalized = str(value).strip().upper()
        return normalized if normalized in {'G54', 'G55', 'G56', 'G57', 'G58', 'G59'} else 'G54'

    @property
    def machine_x_max(self) -> float:
        """Machine maximum X travel (inches)"""
        return self._get('machine', 'dimensions', 'x_max')

    @property
    def machine_y_max(self) -> float:
        """Machine maximum Y travel (inches)"""
        return self._get('machine', 'dimensions', 'y_max')

    @property
    def machine_z_max(self) -> float:
        """Machine maximum Z travel (inches)"""
        return self._get('machine', 'dimensions', 'z_max')

    # ========================================================================
    # General Machining Preferences
    # ========================================================================

    @property
    def sacrifice_board_depth(self) -> float:
        """How far to cut into sacrifice board (inches)"""
        return self._get('machining', 'z_reference', 'sacrifice_board_depth')

    @property
    def clearance_height(self) -> float:
        """Clearance above material for rapid moves (inches)"""
        return self._get('machining', 'z_reference', 'clearance_height')

    @property
    def tab_width(self) -> float:
        """Default tab width (inches)"""
        return self._get('machining', 'tabs', 'width')

    @property
    def tab_height(self) -> float:
        """Default tab height (inches)"""
        return self._get('machining', 'tabs', 'height')

    @property
    def tab_spacing(self) -> float:
        """Default desired tab spacing (inches)"""
        return self._get('machining', 'tabs', 'spacing')

    @property
    def tabs_enabled(self) -> bool:
        """Whether tabs are enabled for perimeter cutting"""
        return self._get('machining', 'tabs', 'enabled')

    @property
    def remove_tabs(self) -> bool:
        """Whether to automatically remove tabs at end of job"""
        return self._get('machining', 'tabs', 'remove_tabs')

    @property
    def pause_before_perimeter(self) -> bool:
        """Whether to pause before cutting perimeter (for screw fixturing)"""
        return self._get('machining', 'fixturing', 'pause_before_perimeter')

    @property
    def hole_detection_tolerance(self) -> float:
        """Tolerance for detecting circular holes (inches)"""
        return self._get('machining', 'holes', 'detection_tolerance')

    @property
    def min_millable_hole_multiplier(self) -> float:
        """Minimum hole diameter as multiple of tool diameter"""
        return self._get('machining', 'holes', 'min_millable_multiplier')

    def _raw_default_tool_diameter(self, machine_id: Optional[str] = None):
        """Raw default-tool diameter (may be a unit string). Checked at the machine top
        level (where the config docs/template put `default_tool`) and then nested under
        `machining` (the defaults location), falling back to the 6238 default."""
        machine_config = self.get_machine_config(machine_id)
        for path in (('default_tool', 'diameter'), ('machining', 'default_tool', 'diameter')):
            value = machine_config
            for key in path:
                value = value.get(key) if isinstance(value, dict) else None
                if value is None:
                    break
            if value is not None:
                return value
        return TEAM_6238_DEFAULTS['machining']['default_tool']['diameter']

    @property
    def default_tool_diameter(self) -> float:
        """Default tool diameter in inches - used as UI default. Accepts a unit string
        (e.g. "4mm") or a number; falls back to 4mm if missing or unparseable."""
        parsed = parse_length(self._raw_default_tool_diameter())
        return parsed if parsed and parsed > 0 else DEFAULT_TOOL_DIAMETER_IN

    @property
    def default_tool_diameter_text(self) -> str:
        """Default tool diameter as the raw display text (e.g. "4mm"), so the UI can show
        it verbatim instead of a translated inch value. Bare numbers get an inch mark."""
        raw = self._raw_default_tool_diameter()
        return raw if isinstance(raw, str) else f'{raw}"'

    # ========================================================================
    # Tube Facing Parameters
    # ========================================================================

    def get_tube_facing_params(self) -> Dict[str, Any]:
        """Get all tube facing parameters as a dict"""
        return {
            'depth_margin': self._get('tube_facing', 'depth_margin'),
            'max_roughing_depth': self._get('tube_facing', 'max_roughing_depth'),
            'max_finishing_depth': self._get('tube_facing', 'max_finishing_depth'),
            'roughing_tool_edge_p1': self._get('tube_facing', 'phase_1', 'roughing_tool_edge'),
            'finishing_tool_edge_p1': self._get('tube_facing', 'phase_1', 'finishing_tool_edge'),
            'roughing_tool_edge_p2': self._get('tube_facing', 'phase_2', 'roughing_tool_edge'),
            'finishing_tool_edge_p2': self._get('tube_facing', 'phase_2', 'finishing_tool_edge'),
            'arc_advance': self._get('tube_facing', 'arc_advance'),
            'arc_radius': self._get('tube_facing', 'arc_radius')
        }

    # ========================================================================
    # Material Presets
    # ========================================================================

    def get_available_materials(self, machine_id: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """
        Get all available materials for a specific machine.

        Args:
            machine_id: Machine ID, or None for the active machine (see for_machine)

        Returns:
            Dictionary mapping material ID to material info (with 'name' and other params)
        """
        machine_config = self.get_machine_config(machine_id)

        # Start with Team 6238 defaults
        materials = dict(TEAM_6238_DEFAULTS['materials'])

        # Add/override with machine-specific materials
        machine_materials = machine_config.get('materials', {})
        for material_id, material_data in machine_materials.items():
            # Get complete material preset (with fallback)
            materials[material_id] = self.get_material_preset(material_id, machine_id)

        return materials

    def is_material_complete(self, material: str, machine_id: Optional[str] = None) -> bool:
        """
        Check if a material has all required parameters defined.

        Args:
            material: Material name
            machine_id: Machine ID, or None for the active machine (see for_machine)

        Returns:
            True if material has all required params, False if using fallback
        """
        # Required parameters for a complete material definition
        required_params = {
            'name', 'spindle_speed', 'feed_rate', 'ramp_feed_rate', 'plunge_rate',
            'traverse_rate', 'approach_rate', 'ramp_angle', 'ramp_start_clearance',
            'stepover_percentage', 'helix_radius_multiplier', 'max_slotting_depth',
            'tab_width', 'tab_height'
        }

        # Check if material exists in defaults
        if material in TEAM_6238_DEFAULTS['materials']:
            return True

        # Check if machine config has all required parameters
        machine_config = self.get_machine_config(machine_id)
        machine_material = machine_config.get('materials', {}).get(material, {})
        return required_params.issubset(machine_material.keys())

    def get_material_preset(self, material: str, machine_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Get material preset parameters for a specific machine with fallback to Team 6238 defaults.

        Args:
            material: Material name ('plywood', 'aluminum', 'polycarbonate', or custom)
            machine_id: Machine ID, or None for the active machine (see for_machine)

        Returns:
            Dictionary of material parameters (always complete, uses plywood fallback)
        """
        machine_config = self.get_machine_config(machine_id)

        # Get machine-specific material config
        machine_material = machine_config.get('materials', {}).get(material, {})

        # Get Team 6238 default for this material
        default_preset = TEAM_6238_DEFAULTS['materials'].get(material, {})

        # If no default found, use plywood as universal fallback
        if not default_preset:
            default_preset = TEAM_6238_DEFAULTS['materials']['plywood'].copy()
            # Use custom name if provided, otherwise capitalize the material ID
            if 'name' not in machine_material:
                machine_material = {**machine_material, 'name': material.replace('_', ' ').title()}

        # Merge: defaults → machine overrides
        return {**default_preset, **machine_material}

    # ========================================================================
    # Integration Settings
    # ========================================================================

    @property
    def google_drive_enabled(self) -> bool:
        """Whether Google Drive integration is enabled for this team"""
        return self._get('integrations', 'google_drive', 'enabled')

    @property
    def google_drive_folder_id(self) -> Optional[str]:
        """
        Google Drive folder ID for uploading G-code.
        Accepts either a folder ID or a full Drive URL, returns just the ID.
        """
        return extract_drive_folder_id(self._get('integrations', 'google_drive', 'folder_id'))

    # ========================================================================
    # Helpers
    # ========================================================================

    def to_dict(self, machine_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Return config as a dictionary for JSON serialization.

        Args:
            machine_id: Machine ID, or None for the active machine (see for_machine)

        Returns:
            Dictionary with machine-specific settings
        """
        machine_config = self.get_machine_config(machine_id)

        # Helper to get machine-specific setting with fallback
        def get_machine_setting(*keys, default=None):
            value = machine_config
            for key in keys:
                if isinstance(value, dict):
                    value = value.get(key)
                    if value is None:
                        break
                else:
                    value = None
                    break
            # Fallback to TEAM_6238_DEFAULTS if not in machine config
            if value is None:
                fallback = TEAM_6238_DEFAULTS
                for key in keys:
                    if isinstance(fallback, dict):
                        fallback = fallback.get(key)
                        if fallback is None:
                            break
                    else:
                        fallback = None
                        break
                value = fallback if fallback is not None else default
            return value

        # Tool diameter for this machine: parse to inches, and keep the raw text for the
        # UI to show verbatim (e.g. "4mm"). Falls back to 4mm via TEAM_6238_DEFAULTS.
        raw_tool = self._raw_default_tool_diameter(machine_id)
        tool_in = parse_length(raw_tool)
        return {
            'team_number': self.team_number,
            'team_name': self.team_name,
            'machine_name': get_machine_setting('machine', 'name'),
            'machine_controller': get_machine_setting('machine', 'controller'),
            'machine_x_max': get_machine_setting('machine', 'dimensions', 'x_max'),
            'machine_y_max': get_machine_setting('machine', 'dimensions', 'y_max'),
            'machine_z_max': get_machine_setting('machine', 'dimensions', 'z_max'),
            'google_drive_enabled': get_machine_setting('integrations', 'google_drive', 'enabled'),
            'google_drive_folder_id': extract_drive_folder_id(
                get_machine_setting('integrations', 'google_drive', 'folder_id')),
            'default_tool_diameter': tool_in if (tool_in and tool_in > 0) else DEFAULT_TOOL_DIAMETER_IN,
            'default_tool_diameter_text': raw_tool if isinstance(raw_tool, str) else f'{raw_tool}"',
        }

    @classmethod
    def from_yaml(cls, yaml_content: str) -> 'TeamConfig':
        """
        Create TeamConfig from YAML string.

        Args:
            yaml_content: YAML content as string

        Returns:
            TeamConfig instance (falls back to Team 6238 defaults on parse error)
        """
        try:
            data = yaml.safe_load(yaml_content)
            return cls(data)
        except yaml.YAMLError as e:
            print(f"⚠️  Error parsing team config YAML: {e}")
            print("   Using Team 6238 defaults")
            return cls()

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> 'TeamConfig':
        """
        Create TeamConfig from dictionary (e.g., from session storage).

        Args:
            config_dict: Configuration dictionary

        Returns:
            TeamConfig instance
        """
        return cls(config_dict)

    def __repr__(self):
        # Machine id included so the request logs show WHICH machine's settings a job
        # actually ran with (the multi-machine failure mode is invisible otherwise).
        return (f"TeamConfig(team={self.team_number}, name='{self.team_name}', "
                f"machine='{self.active_machine_id}')")


# =============================================================================
# YAML TEMPLATE
# =============================================================================

CONFIG_TEMPLATE = """# PenguinCAM Team Configuration
# This file defines machine-specific settings and machining preferences
#
# All values are optional - any missing values will use Team 6238 defaults.
# You only need to specify values you want to override.
#
# UNITS: Dimensions default to inches, but you may add a unit to any dimension
# value (quote it so YAML reads it as text): e.g. diameter: "4mm", x_max: "600mm",
# width: "0.25in", or a fraction like "1/8". Supported units: in/", mm, cm, m, ft/', yd.
# A plain number (no quotes/unit) is inches. Non-dimension values (feed rates, RPM,
# angles, ratios) are always plain numbers.

# =============================================================================
# TEAM INFORMATION
# =============================================================================
team:
  number: 6238
  name: "Popcorn Penguins"

# =============================================================================
# MACHINE & CONTROLLER
# =============================================================================
machine:
  name: "Avid CNC Pro4896"           # Your machine model
  manufacturer: "Avid CNC"
  controller: "Mach4"                 # Mach3, Mach4, LinuxCNC, etc.

  # Machine work envelope. Plain numbers are inches; add a unit to use metric,
  # e.g. x_max: "1200mm" (quote unit values). See the UNITS note at the top.
  dimensions:
    x_max: 48.0
    y_max: 96.0
    z_max: 8.0

  # OPTIONAL end-of-program park in MACHINE coordinates (moves the gantry out of the way
  # for part access). This is the ONLY thing that puts G53 in the output, so OMIT this
  # whole block on controllers that don't support G53 (e.g. GRBL/Easel). Machine-specific.
  park_position:
    x: 0.5      # X position when parking (machine coords)
    y: 23.5     # Y position when parking (machine coords)
    z: -0.5     # safe machine Z (machine coords) to raise to before parking

  # OPTIONAL work coordinate system for TUBE operations. Default (omit this line): tube
  # jobs use G54, so the operator zeros G54 to each tube just like flat work - fully
  # portable. If you have a PERMANENTLY-FIXTURED tube jig, set an alternate fixed WCS
  # (G54-G59) so the jig zero persists in its own system while G54 stays your per-stock
  # flat zero. That fixed WCS must be pre-set in your controller (e.g. G10 L2 P2 for G55);
  # an unset WCS defaults to machine zero and will cut in the wrong place.
  tube_work_coordinate_system: "G55"

  # OPTIONAL coolant. If set (Air/Mist -> M7, Flood -> M8, with M9 off), coolant M-codes
  # are emitted. OMIT this line (or use None) on controllers without coolant M-codes
  # (stock GRBL rejects M7 unless compiled in). Air keeps an air-blast on/off.
  coolant: "Air"

# =============================================================================
# GENERAL MACHINING PREFERENCES
# =============================================================================
machining:
  # Z-axis reference system
  z_reference:
    sacrifice_board_depth: 0.008    # How far to cut into sacrifice board (inches)
    clearance_height: 0.5           # Clearance above material for rapid moves (inches)
    # OPTIONAL: "clear everything" height above Z=0 (sacrifice board), in work
    # coordinates (G54). Used only for the moves that cross the machine bed: the
    # rapid to the first cut, the end retract, multi-part rapids, and the tube-flip
    # pause. Set this ABOVE YOUR TALLEST CLAMP/FIXTURE so those moves clear it.
    # If omitted, those moves fall back to material_thickness + clearance_height
    # (just above the stock) -- fine for edge clamps, but it will NOT clear tall
    # fixturing. Mid-part retracts always use material_thickness + clearance_height
    # regardless, since they stay over the stock.
    # safe_height: 2.0

  # Tab parameters (for perimeter operations)
  tabs:
    width: 0.25                     # Tab width (inches)
    height: 0.1                     # How much material to leave in tab (inches)
    spacing: 6.0                    # Desired spacing between tabs (inches)
    # Note: Actual spacing may be closer to ensure minimum 3 tabs

  # Hole detection and processing
  holes:
    detection_tolerance: 0.02       # Tolerance for detecting circular holes (inches)
    min_millable_multiplier: 1.2    # Minimum hole diameter as multiple of tool diameter
    # Note: Holes smaller than tool_diameter * 1.2 are skipped

  # Tool parameters (defaults - can be overridden per job)
  default_tool:
    diameter: "4mm"                 # e.g. "4mm", "1/8", or a plain number in inches (0.157)
    # Note: This sets the default in the UI, but user can override for each job

# =============================================================================
# TUBE FACING OPERATION PARAMETERS
# =============================================================================
tube_facing:
  # Depth calculations
  depth_margin: 0.005               # Extra depth beyond half tube height (inches)

  # Multi-pass depth limits
  max_roughing_depth: 0.3           # Maximum depth per roughing pass (inches)
  max_finishing_depth: 0.51         # Maximum depth per finishing pass (inches)

  # Tool edge positions for two-pass flip strategy
  # These are the Y positions where the tool leaves the final face
  phase_1:
    roughing_tool_edge: 0.05        # Phase 1 roughing position (inches)
    finishing_tool_edge: 0.0625     # Phase 1 finishing position (inches)

  phase_2:
    roughing_tool_edge: -0.0125     # Phase 2 roughing position (inches)
    finishing_tool_edge: 0.0        # Phase 2 finishing position (inches)

  # Arc clearing parameters
  arc_advance: 0.04                 # Arc advance distance in X (inches)
  arc_radius: 0.05                  # Arc radius for clearing moves (inches)

# =============================================================================
# MATERIAL-SPECIFIC SETTINGS
# =============================================================================
# You can override any or all materials. Only specify values you want to change.
# Any missing values will use Team 6238 defaults for that material.

materials:
  plywood:
    name: "Plywood"
    description: "Standard plywood settings - 18K RPM, 75 IPM cutting"

    # Speeds and feeds
    spindle_speed: 18000            # RPM
    feed_rate: 75.0                 # Cutting feed rate (IPM)
    ramp_feed_rate: 50.0            # Ramp feed rate (IPM)
    plunge_rate: 35.0               # Plunge feed rate for tab Z moves (IPM)
    traverse_rate: 200.0            # Lateral moves above material (IPM)
    approach_rate: 50.0             # Z approach to ramp start (IPM)

    # Toolpath parameters
    ramp_angle: 20.0                # Ramp angle in degrees
    ramp_start_clearance: 0.150     # Clearance above material to start ramping (inches)
    stepover_percentage: 0.65       # Radial stepover as fraction of tool diameter
    helix_radius_multiplier: 0.75   # Helix entry radius as fraction of tool radius

    # Multi-pass parameters
    max_slotting_depth: 0.4         # Maximum depth per pass for perimeter slotting (inches)

    # Tab parameters (can override defaults)
    tab_width: 0.25
    tab_height: 0.15

  aluminum:
    name: "Aluminum"
    description: "Aluminum box tubing - 18K RPM, 55 IPM cutting, 4 deg ramp"

    # Speeds and feeds
    spindle_speed: 18000
    feed_rate: 55.0
    ramp_feed_rate: 35.0
    plunge_rate: 15.0               # Slower for aluminum
    traverse_rate: 200.0
    approach_rate: 35.0

    # Toolpath parameters
    ramp_angle: 4.0                 # Shallow ramp for aluminum
    ramp_start_clearance: 0.050
    stepover_percentage: 0.25       # Conservative for aluminum
    helix_radius_multiplier: 0.5    # Conservative helix entry for aluminum

    # Multi-pass parameters
    max_slotting_depth: 0.2         # Shallower passes for aluminum

    # OPTIONAL: feed floor at sharp pocket corners, as a fraction of feed_rate. At a sharp
    # corner the cutter wraps two edges and engagement spikes, so we ease the feed down (the
    # toolpath itself is unchanged). Lower = more protection. Aluminum is force-limited, so an
    # aggressive 0.4 is good; softer/heat-limited materials (plywood, polycarbonate) use ~0.7
    # to keep the feed up and preserve chip load (avoid rubbing/melting). Omit for the default.
    corner_min_feed_scale: 0.4

    # Tab parameters
    tab_width: 0.25
    tab_height: 0.15

  polycarbonate:
    name: "Polycarbonate"
    description: "Polycarbonate - same as plywood settings"

    # Speeds and feeds
    spindle_speed: 18000
    feed_rate: 75.0
    ramp_feed_rate: 50.0
    plunge_rate: 20.0
    traverse_rate: 200.0
    approach_rate: 50.0

    # Toolpath parameters
    ramp_angle: 20.0
    ramp_start_clearance: 0.100
    stepover_percentage: 0.55       # Moderate for polycarbonate
    helix_radius_multiplier: 0.75

    # Multi-pass parameters
    max_slotting_depth: 0.25

    # Tab parameters
    tab_width: 0.25
    tab_height: 0.15

# =============================================================================
# GOOGLE DRIVE INTEGRATION (optional)
# =============================================================================
integrations:
  google_drive:
    enabled: true
    folder_id: "https://drive.google.com/drive/folders/YOUR_FOLDER_ID"
    # To get your folder URL:
    # 1. Open Google Drive in your browser
    # 2. Navigate to: Shared drives → Your Team → CNC → G-code
    # 3. Copy the full URL from your browser (looks like above)
    # 4. Paste it here (you can paste either the full URL or just the folder ID)

# =============================================================================
# UI CUSTOMIZATION (optional - for future use)
# =============================================================================
ui:
  theme: "default"
  # Future: team logo, colors, branding
  # logo_url: "https://your-team-website.com/logo.png"
  # primary_color: "#FF6B35"
"""
