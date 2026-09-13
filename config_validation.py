"""Validation and sanitization for user-supplied PenguinCAM team configs.

The anonymous upload flow lets a team point PenguinCAM at a YAML config hosted at a
CORS-friendly URL (fetched client-side by the browser, then POSTed to us). That YAML is
UNTRUSTED input, so before it is allowed to become a TeamConfig we:

  * cap its size (cheap billion-laughs / abuse guard) before parsing;
  * parse with ``yaml.safe_load`` (no arbitrary object construction) and require a mapping;
  * sanitize every string leaf to plain ASCII with no parentheses -- config strings flow
    into G-code COMMENTS, and CNC controllers choke on nested parens / unicode (see the
    G-code rules in CLAUDE.md). This is the injection control, and it is required
    regardless of how the file was fetched;
  * range-check the known numeric feed/speed/dimension/depth keys so a hostile or
    fat-fingered config cannot emit a physically dangerous toolpath.

Unlike ``TeamConfig.from_yaml`` (which silently falls back to defaults on error -- correct
for a background Onshape fetch), this raises ``ConfigValidationError`` with a user-facing
message, so the interactive "load config" UI can tell the team exactly what is wrong.
"""

import math

import yaml

from printer_relay import normalize_code
from team_config import LENGTH_KEYS, parse_length


# Generous ceiling: a real config is a few KB. Anything larger is abuse or a YAML bomb.
MAX_CONFIG_BYTES = 256 * 1024


class ConfigValidationError(Exception):
    """Raised when a user-supplied config is unsafe or malformed. ``message`` is safe to
    show to the end user (it never echoes back attacker-controlled content verbatim)."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


# Numeric leaf keys that are NOT lengths (feeds, speeds, angles, ratios, counts) mapped to
# their inclusive [min, max] sane range. A value outside the range is rejected rather than
# clamped, so the team notices and fixes their config instead of silently getting something
# else. Keys absent here are left to TeamConfig's own fallbacks.
NUMERIC_BOUNDS = {
    'spindle_speed': (1.0, 100000.0),          # RPM
    'feed_rate': (0.1, 10000.0),               # IPM
    'ramp_feed_rate': (0.1, 10000.0),
    'plunge_rate': (0.1, 10000.0),
    'traverse_rate': (0.1, 10000.0),
    'approach_rate': (0.1, 10000.0),
    'ramp_angle': (0.1, 90.0),                 # degrees
    'stepover_percentage': (0.01, 1.0),        # fraction of tool diameter
    'helix_radius_multiplier': (0.01, 2.0),    # fraction of tool radius
    'corner_min_feed_scale': (0.01, 1.0),      # fraction of feed_rate
    'contour_threshold': (0.0, 1000000.0),
    'detection_tolerance': (0.0, 1.0),         # inches
    'min_millable_multiplier': (0.1, 100.0),
    'number': (0.0, 1000000.0),                # team number
}

# Length values may legitimately be negative (park Z, tube-facing tool edges), so only the
# magnitude is bounded. Values may be plain numbers OR unit strings ("4mm", "1/8", '0.25"');
# both are validated through parse_length.
MAX_LENGTH_INCHES = 10000.0


def _sanitize_string(value, path, warnings):
    """Reduce a string to printable ASCII with no parentheses (G-code comment safety).

    Parens become spaces (they would nest inside G-code comments); other non-ASCII or
    control characters are dropped. Records a warning when the value was altered so the UI
    can surface it. Never raises -- string content is sanitized, not rejected."""
    cleaned = ''.join(
        ' ' if c in '()' else c
        for c in value
        if (32 <= ord(c) < 127) or c in '()'
    )
    cleaned = cleaned.strip()
    if cleaned != value.strip():
        warnings.append(f"Adjusted text at {path} to be CNC-safe (removed parentheses or non-ASCII characters).")
    return cleaned


def _validate_number(value, lo, hi, path):
    """Ensure a numeric leaf is a finite number within [lo, hi]. Raises otherwise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigValidationError(f"Value at {path} must be a number.")
    if not math.isfinite(value):
        raise ConfigValidationError(f"Value at {path} must be a finite number.")
    if not (lo <= value <= hi):
        raise ConfigValidationError(f"Value at {path} ({value}) is outside the allowed range {lo} to {hi}.")


def _validate_length(value, path):
    """Ensure a length leaf (number or unit string) parses and is within magnitude bounds."""
    if isinstance(value, bool):
        raise ConfigValidationError(f"Value at {path} must be a length, not a boolean.")
    parsed = parse_length(value)
    if parsed is None:
        raise ConfigValidationError(f"Value at {path} is not a valid length (e.g. 0.25, \"4mm\", \"1/8\").")
    if not math.isfinite(parsed) or abs(parsed) > MAX_LENGTH_INCHES:
        raise ConfigValidationError(f"Length at {path} is unreasonably large.")


def _check_leaf(key, value, child_path):
    """Run the numeric/length range check for a leaf, if the key is bounded. Raises
    ConfigValidationError on a bad value; a no-op for unbounded keys."""
    if key in LENGTH_KEYS:
        _validate_length(value, child_path)
    elif isinstance(value, (int, float)) and not isinstance(value, bool) and key in NUMERIC_BOUNDS:
        lo, hi = NUMERIC_BOUNDS[key]
        _validate_number(value, lo, hi, child_path)


def _walk(node, path, warnings, strict):
    """Recursively validate numbers/lengths and sanitize strings, in place.

    In ``strict`` mode a bad numeric/length value raises. In lenient mode it is instead
    dropped from the config (so TeamConfig falls back to that key's default) and a warning is
    recorded -- this keeps a background Onshape page render from hard-failing on a typo.
    String sanitization runs in both modes; it never rejects."""
    if isinstance(node, dict):
        drop = []
        for key, value in list(node.items()):
            child_path = f"{path}.{key}" if path else str(key)
            if isinstance(value, (dict, list)):
                _walk(value, child_path, warnings, strict)
            elif isinstance(value, str):
                if key in LENGTH_KEYS:
                    try:
                        _validate_length(value, child_path)
                    except ConfigValidationError:
                        if strict:
                            raise
                        drop.append(key)
                        warnings.append(f"Ignored invalid length at {child_path}; using the default instead.")
                        continue
                node[key] = _sanitize_string(value, child_path, warnings)
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                try:
                    _check_leaf(key, value, child_path)
                except ConfigValidationError:
                    if strict:
                        raise
                    drop.append(key)
                    warnings.append(f"Ignored out-of-range value at {child_path}; using the default instead.")
            # bools / None pass through untouched (flags, opt-outs)
        for key in drop:
            del node[key]
    elif isinstance(node, list):
        for i, value in enumerate(node):
            child_path = f"{path}[{i}]"
            if isinstance(value, (dict, list)):
                _walk(value, child_path, warnings, strict)
            elif isinstance(value, str):
                node[i] = _sanitize_string(value, child_path, warnings)


def validate_and_sanitize_config(yaml_text, strict=True):
    """Parse, validate, and sanitize an untrusted team-config YAML string.

    Applies to EVERY config source (the anonymous upload flow and the Onshape classroom
    fetch alike) -- crappy or hostile YAML can come from either.

    ``strict`` selects the error policy:

      * ``strict=True`` (interactive upload): any structural problem or out-of-range value
        raises ``ConfigValidationError`` with a user-facing ``message``, so the team can fix
        their file. Returns ``(config_dict, warnings)`` on success.
      * ``strict=False`` (background Onshape render): never raises for content problems --
        a structural failure returns ``(None, warnings)`` so the caller falls back to full
        defaults, and an individual out-of-range value is dropped (that key reverts to its
        default). Keeps a bad config from taking down the page.

    String sanitization (ASCII, no parens -- G-code comment safety) runs in both modes.
    ``warnings`` is a list of human-readable notes about anything that was adjusted."""
    def _fail(message):
        if strict:
            raise ConfigValidationError(message)
        return None, [message]

    if yaml_text is None:
        return _fail("No configuration content was provided.")
    if not isinstance(yaml_text, str):
        return _fail("Configuration must be text.")
    if len(yaml_text.encode('utf-8')) > MAX_CONFIG_BYTES:
        return _fail(f"Configuration file is too large (limit {MAX_CONFIG_BYTES // 1024} KB).")

    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        # yaml's error messages can be long; keep the first line for the user.
        detail = str(e).splitlines()[0] if str(e) else "invalid YAML"
        return _fail(f"Could not parse the configuration file: {detail}")

    if data is None:
        return _fail("The configuration file is empty.")
    if not isinstance(data, dict):
        return _fail("The configuration file must be a set of settings (a YAML mapping), not a list or single value.")

    version = data.get('version', 1)
    if version not in (1, 2):
        return _fail(f"Unsupported configuration version: {version}. Supported versions are 1 and 2.")

    if version == 2:
        machines = data.get('machines')
        if not isinstance(machines, dict) or not machines:
            return _fail("A version 2 configuration needs a 'machines:' section with at "
                         "least one machine defined under it.")

    warnings = []
    _walk(data, '', warnings, strict)

    # Runs AFTER _walk so the comparison uses the sanitized `default_machine` value (machine
    # ids are dict keys and aren't sanitized, so an id containing parentheses would otherwise
    # stop matching its own key here). A missing or dangling `default_machine` is the
    # difference between "my settings apply" and "every setting silently reverts to the
    # built-in defaults", so repair it to the first machine listed and say so.
    if version == 2:
        machines = data['machines']
        default_machine = data.get('default_machine')
        if default_machine not in machines:
            first = next(iter(machines))
            if default_machine is None:
                warnings.append(f"No 'default_machine' was set; using '{first}'.")
            else:
                warnings.append(f"'default_machine' refers to an undefined machine "
                                f"'{default_machine}'; using '{first}' instead.")
            data['default_machine'] = first

    # Runs AFTER _walk for the same reason the default_machine repair does: it must see the
    # sanitized value. The pairing code is the printer relay's whole identity, so normalise
    # it once here rather than at every read site. (The string sanitizer never touches a
    # code -- it only strips parentheses and non-ASCII, and the alphabet has neither.)
    printing = data.get('printing')
    if printing is not None:
        if not isinstance(printing, dict):
            if strict:
                raise ConfigValidationError(
                    "The 'printing:' section must be a set of settings, not a single value.")
            del data['printing']
            warnings.append("Ignored the 'printing' section: it must be a set of settings.")
        elif 'pairing_code' in printing:
            raw_code = printing['pairing_code']
            if raw_code is None or (isinstance(raw_code, str) and not raw_code.strip()):
                # An explicit `pairing_code:` with nothing after it (YAML null) or an empty
                # string is how a mentor retires a Pi -- it means "no printer paired", not a
                # mistake -- so drop the key silently in both modes.
                del printing['pairing_code']
            else:
                code = normalize_code(raw_code)
                if code is None:
                    if strict:
                        raise ConfigValidationError(
                            "printing.pairing_code must be the 8-character code shown by "
                            "the Pi.")
                    del printing['pairing_code']
                    warnings.append("Ignored printing.pairing_code: it is not the 8-character "
                                    "code shown by the Pi; no printer will be paired.")
                else:
                    printing['pairing_code'] = code

    return data, warnings
