"""The one convention for a browser self-test on synthetic fixtures (spec 8.2).

A self-test never replays a synthetic log through a real scenario. It runs a synthetic-...
copy of the scenario (testbed/scenarios synthetic_copy), stages the fixtures under the
copy's name, tells only its own replay server about the copy, and skips itself once the
real scenario it copies is recorded, so it can never keep passing on invented data after
the recording lands. Shared by browser_host, browser_scenarios and browser_print.
"""
import shutil
import unittest
from pathlib import Path

from testbed.scenarios import SYNTHETIC_PREFIX, scenario_is_recorded

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
# The ids the synthetic panel fixtures were written with.
SYNTHETIC_DOCUMENT = {'did': '0000000000000000000000d1', 'wid': '0000000000000000000000a1',
                      'eid': '0000000000000000000000e1'}
# Per scenario copy: (cassette fixture, message log fixture), by file stem.
_FIXTURE_STEMS = {
    'synthetic-part-select': ('synthetic-panel', 'synthetic-part-select'),
    'synthetic-print-configured-part': ('synthetic-print-configured-part',
                                        'synthetic-print-configured-part'),
}


def stage_self_test_fixtures(directory, scenarios) -> tuple:
    """(cassette dir, message dir) under `directory`, holding each synthetic-... copy's
    fixtures under the copy's name (a cassette's binaries too)."""
    cassettes, messages = Path(directory) / 'cassettes', Path(directory) / 'messages'
    cassettes.mkdir()
    messages.mkdir()
    for scenario in scenarios:
        if not scenario.name.startswith(SYNTHETIC_PREFIX):
            raise ValueError(f'{scenario.name!r} is not a synthetic- scenario copy')
        cassette, message_log = _FIXTURE_STEMS[scenario.name]
        shutil.copy(FIXTURES / 'cassettes' / f'{cassette}.json', cassettes / f'{scenario.name}.json')
        binaries = FIXTURES / 'cassettes' / cassette
        if binaries.is_dir():
            shutil.copytree(binaries, cassettes / scenario.name)
        shutil.copy(FIXTURES / 'messages' / f'{message_log}.json', messages / f'{scenario.name}.json')
    return cassettes, messages


def skip_once_recorded(scenario):
    """Skip the running test, or a whole class from setUpClass, if the real scenario that
    `scenario` (a synthetic-... copy) stands in for is recorded."""
    real = scenario.name[len(SYNTHETIC_PREFIX):]
    if scenario_is_recorded(real):
        raise unittest.SkipTest(f'{real} is recorded: this self-test on synthetic input is retired; '
                                'delete it with the synthetic fixtures')
