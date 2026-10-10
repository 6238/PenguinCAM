"""The browser scenarios in replay mode (spec 5.7 and 11); run by make testbed-replay.

Each scenario with a panel part opens its test document on the fake Onshape host, against a
replay development server, and is driven through its steps as the owner drove it while
recording: the panel's 3D Printing radio for choose-print, the checklist strip's button for
a step's action (its action itself for the buttons the strip hides on the print page), the
print page's Next and Add from another document... for print-parts and print-dialog, and
the fake Onshape host's selectPart/selectParts/deselect/dialogClosed for what the owner did
in Onshape. Steps recorded for reference only are skipped and left out of the comparison. The strip's recorded events must then equal the scenario's message
log in message names, keys and value types.

A scenario without a recording is skipped with "needs a recording", never passed. The
self-test runs part-select against the synthetic fixtures.
"""
import json
import tempfile
import time
import unittest

from testbed import settings
from testbed.browser import ReplayDevServer, host_url, launch_test_browser, sign_in_for_replay
from testbed.messages import load_message_log
from testbed.scenarios import SCENARIOS, scenario_is_recorded
from testbed.tests.browser_host import (SYNTHETIC_DOCUMENT, panel_frame,
                                        stage_synthetic_fixtures, wait_for)

STRIP_ACTIONS = ('ask-part', 'ask-two-parts', 'dialog-part', 'dialog-parts', 'close-dialog',
                 'ask-unbounded')
# Hidden by the strip while the print page asks Onshape itself (testbed_panel.js).
PAGE_ASKS = ('ask-part', 'ask-two-parts', 'dialog-part', 'dialog-parts', 'close-dialog')
QUIET_S = 0.5            # a step is over once no message has arrived for this long


def _json_type(value):
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, (int, float)):
        return 'number'
    if isinstance(value, str):
        return 'string'
    if isinstance(value, list):
        return 'array'
    if isinstance(value, dict):
        return 'object'
    return 'null'


def event_shape(event) -> tuple:
    """What a replayed event must share with the recorded one: direction, message name,
    and each key with its value's JSON type."""
    data = event.get('data')
    if not isinstance(data, dict):
        return (event['dir'], None, _json_type(data))
    return (event['dir'], data.get('messageName'),
            tuple(sorted((key, _json_type(value)) for key, value in data.items())))


def shape_differences(log, strip_events) -> list:
    """Each step whose strip events differ in shape from the message log's, as
    (step, recorded shapes, replayed shapes). `strip_events` maps a step to its events."""
    differences = []
    steps = [entry['step'] for entry in log['steps']]
    steps += [step for step in strip_events if step not in steps]
    recorded = {entry['step']: entry['events'] for entry in log['steps']}
    for step in steps:
        want = [event_shape(e) for e in recorded.get(step, [])]
        got = [event_shape(e) for e in strip_events.get(step, [])]
        if want != got:
            differences.append((step, want, got))
    return differences


class ScenarioDriver:
    """Drives one scenario through a page on the fake Onshape host."""

    def __init__(self, page, scenario):
        self.page = page
        self.scenario = scenario
        self.names = [step.name for step in scenario.steps]
        self.frame = None              # the panel's frame once it shows the print page
        self.strip_events = {}         # step -> events the strip recorded

    def run(self):
        self.frame = None
        panel_frame(self.page)
        for index, step in enumerate(self.scenario.steps):
            if step.recorded_only:
                continue
            self.page.evaluate('name => window.__testbed.setStep(name)', step.name)
            if step.action == 'choose-print':
                self._choose_print()
            if self.frame is not None:
                self._advance_strip_to(index)
            if step.action in PAGE_ASKS:
                self.frame.evaluate('a => window.PenguinCAMTestbed.act(a)', step.action)
            elif step.action in STRIP_ACTIONS:
                self.frame.click('#testbed-action')
            elif step.action == 'print-parts':
                self.frame.click('#btn-next')
            elif step.action == 'print-dialog':
                self.frame.click('#btn-add-from-document')
            elif step.action == 'back':
                self._collect()
                self.frame.check('input[name="mode"][value="2d"]')
                self.frame = None
                panel_frame(self.page)
            elif len(step.select) == 1:
                self.page.evaluate('name => window.__testbed.selectPart(name)', step.select[0])
            elif step.select:
                self.page.evaluate('names => window.__testbed.selectParts(names)', step.select)
            elif step.name == 'deselect':
                self.page.evaluate('window.__testbed.deselect()')
            elif step.name == 'dialog closed':
                self.page.evaluate('window.__testbed.dialogClosed()')
            self._wait_quiet()
        if self.frame is not None:
            self._collect()
        return self.strip_events

    def _choose_print(self):
        panel = panel_frame(self.page)
        panel.check('input[name="mode"][value="print"]')
        self.frame = panel_frame(self.page, '/print')
        wait_for(self.page, lambda: self.frame.evaluate(
            "!!document.querySelector('#testbed-scenario option[value]:not([value=\"\"])')"),
            'the checklist strip and its scenarios')
        # Chosen in the strip as the owner does, which starts its message log afresh.
        self.frame.select_option('#testbed-scenario', self.scenario.name)
        wait_for(self.page, lambda: self.frame.evaluate(
            '([scenario, step]) => window.PenguinCAMTestbed.currentScenario() === scenario && '
            'window.PenguinCAMTestbed.currentStep() === step && '
            'window.PenguinCAMTestbed.recorded.length === 0', [self.scenario.name, self.names[0]]),
            f'the strip to start {self.scenario.name}')

    def _strip_index(self):
        return self.names.index(self.frame.evaluate('window.PenguinCAMTestbed.currentStep()'))

    def _advance_strip_to(self, index):
        while self._strip_index() < index:
            before = self._strip_index()
            self.frame.click('#testbed-next')
            wait_for(self.page, lambda: self._strip_index() != before,
                     f'the strip to move on from {self.names[before]}')

    def _counts(self):
        recorded = self.frame.evaluate('window.PenguinCAMTestbed.recorded.length') \
            if self.frame is not None else 0
        return (self.page.evaluate('window.__testbed.log.length + window.__testbed.sent.length'),
                recorded)

    def _wait_quiet(self):
        last, since = None, time.monotonic()
        while time.monotonic() - since < QUIET_S:
            try:
                counts = self._counts()
            except Exception:              # the frame is between two pages
                counts = None
            if counts is None or counts != last:
                last, since = counts, time.monotonic()
            self.page.wait_for_timeout(100)

    def _collect(self):
        for entry in self.frame.evaluate('window.PenguinCAMTestbed.recorded'):
            self.strip_events.setdefault(entry['step'], []).append(entry['event'])


def replayed_steps(log, scenario):
    """The message log without the steps recorded for reference only."""
    skipped = {step.name for step in scenario.steps if step.recorded_only}
    return dict(log, steps=[entry for entry in log['steps'] if entry['step'] not in skipped])


def run_browser_scenario(name, document, cassette_dir=None, message_dir=None) -> tuple:
    """Run scenario `name` on `document` in replay mode: (message log, strip events).

    The message log comes back without the steps replay skips."""
    scenario = SCENARIOS[name]
    log = replayed_steps(load_message_log(name, message_dir), scenario)
    with ReplayDevServer(cassette_dir=cassette_dir, message_dir=message_dir):
        playwright, browser, context = launch_test_browser(message_dir=message_dir)
        try:
            sign_in_for_replay(context, settings.REPLAY_PORT, name)
            page = context.new_page()
            page.goto(host_url(document, name))
            wait_for(page, lambda: page.evaluate('window.__testbed.ready'),
                     f'the fake Onshape host to load the message log of {name}')
            return log, ScenarioDriver(page, scenario).run()
        finally:
            browser.close()
            playwright.stop()


def _format(differences):
    return '\n'.join(f'step {step!r}:\n  recorded {json.dumps(want)}\n  replayed {json.dumps(got)}'
                     for step, want, got in differences)


class BrowserScenarioTest(unittest.TestCase):
    """One test per scenario with a panel part, added below."""

    def check_scenario(self, name):
        if not scenario_is_recorded(name):
            self.skipTest('needs a recording')
        try:
            documents = json.loads(settings.DOCUMENTS_FILE.read_text())
        except FileNotFoundError:
            self.fail(f'{name} is recorded but {settings.DOCUMENTS_FILE.name} is missing; '
                      'run build-docs')
        log, strip_events = run_browser_scenario(name, documents[SCENARIOS[name].document])
        differences = shape_differences(log, strip_events)
        self.assertEqual(differences, [], f'{name} replayed differently:\n' + _format(differences))


def _scenario_test(name):
    def test(self):
        self.check_scenario(name)
    test.__name__ = f"test_{name.replace('-', '_')}"
    test.__doc__ = f'The {name} scenario replays as recorded.'
    return test


for _name, _scenario in SCENARIOS.items():
    if _scenario.panel:
        setattr(BrowserScenarioTest, f"test_{_name.replace('-', '_')}", _scenario_test(_name))


class BrowserScenarioSelfTest(unittest.TestCase):
    def test_part_select_replays_the_synthetic_message_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            cassettes, messages = stage_synthetic_fixtures(tmp)
            log, strip_events = run_browser_scenario('part-select', SYNTHETIC_DOCUMENT,
                                                     cassettes, messages)
        self.assertEqual(shape_differences(log, strip_events), [],
                         _format(shape_differences(log, strip_events)))
        # Every recorded event came back, and the selection answered the live request.
        self.assertEqual(sum(len(e) for e in strip_events.values()),
                         sum(len(entry['events']) for entry in log['steps']))
        selection = strip_events['select part Cylinder'][0]['data']
        self.assertEqual(selection['messageId'], 'testbed-1')
        self.assertEqual(selection['selections'][0]['selectionId'], 'JHD')
        self.assertEqual(strip_events['select part Box'][0]['data']['messageName'], 'SELECTION')


class ShapeComparisonTest(unittest.TestCase):
    """The comparison itself, without a browser."""

    LOG = {'scenario': 's', 'steps': [{'step': 'a', 'events': [
        {'dir': 'out', 'data': {'messageName': 'requestSelection', 'messageId': 'testbed-3',
                                'requiredSelectionCount': 1}}]}]}

    def test_same_names_keys_and_types_match_despite_values(self):
        replayed = {'a': [{'dir': 'out', 'data': {'messageName': 'requestSelection',
                                                  'messageId': 'testbed-1',
                                                  'requiredSelectionCount': 2}}]}
        self.assertEqual(shape_differences(self.LOG, replayed), [])

    def test_a_changed_type_missing_key_or_extra_event_differs(self):
        for replayed in (
                {'a': [{'dir': 'out', 'data': {'messageName': 'requestSelection',
                                               'messageId': 'testbed-1',
                                               'requiredSelectionCount': '1'}}]},
                {'a': [{'dir': 'out', 'data': {'messageName': 'requestSelection',
                                               'messageId': 'testbed-1'}}]},
                {'a': self.LOG['steps'][0]['events'] * 2},
                {'a': self.LOG['steps'][0]['events'], 'b': self.LOG['steps'][0]['events']},
                {}):
            with self.subTest(replayed=replayed):
                self.assertNotEqual(shape_differences(self.LOG, replayed), [])


if __name__ == '__main__':
    unittest.main()
