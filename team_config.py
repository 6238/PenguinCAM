"""
Team Configuration Management for PenguinCAM

Handles loading and managing team-specific settings from YAML config files
stored in Onshape documents. Falls back to Team 6238 defaults if config
is missing or incomplete.
"""

import yaml
from typing import Optional, Dict, Any


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
        'park_position': {'x': 0.5, 'y': 0.5},
        'standard_work_offset': 'G54',
        'tube_jig_work_offset': 'G55',
        'coolant': 'Air'
    },
    'machining': {
        'z_reference': {
            'sacrifice_board_depth': 0.008,
            'safe_height': 1.5,
            'clearance_height': 0.5
        },
        'tabs': {
            'enabled': True,
            'width': 0.25,
            'height': 0.1,
            'spacing': 6.0
        },
        'fixturing': {
            'pause_before_perimeter': False
        },
        'holes': {
            'detection_tolerance': 0.02,
            'min_millable_multiplier': 1.2
        },
        'default_tool': {
            'diameter': 0.157  # 4mm end mill
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


class TeamConfig:
    """
    Manages team-specific configuration for PenguinCAM.

    Config is loaded from a YAML file stored in the team's Onshape documents
    named "PenguinCAM-config.yaml". Falls back to Team 6238 defaults for any
    missing values.
    """

    def __init__(self, config_data: Optional[Dict[str, Any]] = None):
        """
        Initialize team config from YAML data.

        Args:
            config_data: Parsed YAML config dict, or None for defaults
        """
        if config_data is None:
            config_data = {}

        # Normalize to v2 structure internally for consistent API
        self._data = self._normalize_to_v2(config_data)

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

            # Ensure machine has a name
            if 'name' not in machine_config:
                machine_config['name'] = 'Default Machine'

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

        For v2 configs, looks inside the default machine config.

        Args:
            *keys: Path to nested value (e.g., 'machine', 'park_position', 'x')
            default: Optional override default (otherwise uses TEAM_6238_DEFAULTS)

        Returns:
            Value from config, or from TEAM_6238_DEFAULTS, or provided default
        """
        # Get the default machine config (handles both v1 wrapped and v2 native)
        machine_config = self.get_machine_config(None)

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
        """Get default machine ID"""
        return self._data.get('default_machine', 'default')

    def get_machine_config(self, machine_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Get config for a specific machine.

        Args:
            machine_id: Machine ID, or None for default machine

        Returns:
            Machine configuration dict
        """
        if machine_id is None:
            machine_id = self.default_machine_id

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
    def machine_coolant(self) -> str:
        """Machine coolant type (Air, Flood, Mist, None)"""
        return self._get('machine', 'coolant')

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
    def safe_height(self) -> float:
        """Safe height for rapid moves (inches)"""
        return self._get('machining', 'z_reference', 'safe_height')

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

    @property
    def default_tool_diameter(self) -> float:
        """Default tool diameter (inches) - used as UI default"""
        return self._get('machining', 'default_tool', 'diameter')

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
            machine_id: Machine ID, or None for default machine

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
            machine_id: Machine ID, or None for default machine

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
            machine_id: Machine ID, or None for default machine

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
        folder_value = self._get('integrations', 'google_drive', 'folder_id')

        if not folder_value:
            return None

        # If it's a full URL, extract the ID
        if 'drive.google.com' in folder_value:
            # Format: https://drive.google.com/drive/folders/FOLDER_ID
            # or: https://drive.google.com/drive/u/0/folders/FOLDER_ID
            parts = folder_value.split('/folders/')
            if len(parts) == 2:
                # Remove any query parameters or trailing slashes
                folder_id = parts[1].split('?')[0].rstrip('/')
                return folder_id

        # Otherwise assume it's already just the ID
        return folder_value

    # ========================================================================
    # Helpers
    # ========================================================================

    def to_dict(self, machine_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Return config as a dictionary for JSON serialization.

        Args:
            machine_id: Machine ID, or None for default machine

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

        return {
            'team_number': self.team_number,
            'team_name': self.team_name,
            'machine_name': get_machine_setting('machine', 'name'),
            'machine_controller': get_machine_setting('machine', 'controller'),
            'machine_x_max': get_machine_setting('machine', 'dimensions', 'x_max'),
            'machine_y_max': get_machine_setting('machine', 'dimensions', 'y_max'),
            'machine_z_max': get_machine_setting('machine', 'dimensions', 'z_max'),
            'google_drive_enabled': get_machine_setting('integrations', 'google_drive', 'enabled'),
            'google_drive_folder_id': get_machine_setting('integrations', 'google_drive', 'folder_id'),
            'default_tool_diameter': get_machine_setting('default_tool', 'diameter'),
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
        return f"TeamConfig(team={self.team_number}, name='{self.team_name}')"


# =============================================================================
# YAML TEMPLATE
# =============================================================================

CONFIG_TEMPLATE = """# PenguinCAM Team Configuration
# This file defines machine-specific settings and machining preferences
#
# All values are optional - any missing values will use Team 6238 defaults.
# You only need to specify values you want to override.

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

  # Machine work envelope (inches)
  dimensions:
    x_max: 48.0
    y_max: 96.0
    z_max: 8.0

  # Park position (machine coordinates for safe part access)
  park_position:
    x: 0.5      # X position when parking (machine coords)
    y: 23.5     # Y position when parking (machine coords)
    # NOTE: This is machine-specific! Adjust for your CNC's safe position.

  # Coordinate systems
  standard_work_offset: "G54"       # Work offset for standard operations
  tube_jig_work_offset: "G55"       # Work offset for tube jig operations

  coolant: "Air"                    # Air, Flood, Mist, None

# =============================================================================
# GENERAL MACHINING PREFERENCES
# =============================================================================
machining:
  # Z-axis reference system
  z_reference:
    sacrifice_board_depth: 0.008    # How far to cut into sacrifice board (inches)
    safe_height: 1.5                # Safe height for rapid moves (inches)
    clearance_height: 0.5           # Clearance above material for rapid moves (inches)

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
    diameter: 0.157                 # 4mm end mill (inches)
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
    description: "Aluminum box tubing - 18K RPM, 55 IPM cutting, 4° ramp"

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
