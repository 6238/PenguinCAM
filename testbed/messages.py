"""The message log: one scenario's panel messages, in both directions (spec 6).

`<dir>/<scenario>.json` holds
{"scenario", "synthetic", "steps": [{"step", "events": [{"dir", "origin", "data", "t_ms"}]}]},
"dir" being "out" for a message the panel sent and "in" for one it received. The
checklist strip records them; the fake Onshape host answers from them.
"""
import json
from pathlib import Path

from testbed import settings
from testbed.adapters import current_recording_scrubber
from testbed.scrub import Scrubber


def load_message_log(name, directory=None) -> dict:
    """The message log of scenario `name`; `directory` defaults to settings.MESSAGE_DIR."""
    directory = Path(directory or settings.MESSAGE_DIR)
    return json.loads((directory / f'{name}.json').read_text())


def _scrubbed(value, scrubber):
    if isinstance(value, str):
        return scrubber.scrub_text(value)
    if isinstance(value, list):
        return [_scrubbed(item, scrubber) for item in value]
    if isinstance(value, dict):
        return {_scrubbed(key, scrubber): _scrubbed(item, scrubber) for key, item in value.items()}
    return value


def save_message_log(log, directory=None, scrubber=None) -> Path:
    """Write `log` with every string scrubbed and return the path.

    `directory` defaults to settings.fresh_message_dir(), where the drift report reads it. The
    scrubber defaults to the recording's, which knows the owner's values; without a
    recording in progress, one that still removes credentials and tokens.
    """
    directory = Path(directory or settings.fresh_message_dir())
    scrubber = scrubber or current_recording_scrubber() or Scrubber({})
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{log['scenario']}.json"
    path.write_text(json.dumps(_scrubbed(log, scrubber), indent=1, sort_keys=True) + '\n')
    return path


def message_log_segment(log, step) -> list:
    """The events recorded during `step`. A step that was never recorded is a KeyError."""
    for entry in log['steps']:
        if entry['step'] == step:
            return entry['events']
    raise KeyError(f'no step {step!r} in the message log of {log["scenario"]}')
