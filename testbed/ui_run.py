"""The automated Onshape UI run: `python -m testbed record --ui` (spec 5.9).

Starts the development server in record mode on 6238, launches real Google Chrome headed
(under Xvfb in the container) with a profile kept in settings.state_dir()/chrome-profile,
signs in to cad.onshape.com unless the kept session still is, and drives each panel
scenario through the PenguinCAM-chondl-dev panel and the checklist strip.

It acts at human speed and never tries to get past a bot check: on a CAPTCHA, a challenge
or a verification step it saves a screenshot to settings.state_dir()/screenshots/ and
raises OnshapeCheckStopped, which the command line turns into exit 3. ONSHAPE_PASSWORD goes
only into Onshape's sign-in form; it is never logged or written anywhere.

A failed step ends only its scenario: the run saves a screenshot, discards that
scenario's partial recording, and goes on with the next. A scenario with a pick inside one
of Onshape's dialogs (owner_steps), which the run cannot make, is not opened at all and is
reported as needing the owner in an Onshape checkpoint. run_onshape_ui returns one
ScenarioOutcome per scenario.

Every Onshape page selector is in SELECTORS, so a change in Onshape's interface is a
one-line fix there.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote, quote_plus

from testbed import settings
from testbed.browser import RecordDevServer
from testbed.documents import pending_documents
from testbed.scenarios import PAGE_ASKS, SCENARIOS, STRIP_ACTIONS

UI_VARIABLES = ('ONSHAPE_USERNAME', 'ONSHAPE_PASSWORD', 'ONSHAPE_CLIENT_ID',
                'ONSHAPE_CLIENT_SECRET')
# Where testbed/scripts/install-chrome.sh unpacks Google Chrome.
CHROME_BINARY = Path.home() / '.local' / 'opt' / 'google-chrome' / 'opt' / 'google' / 'chrome' / 'chrome'
FALLBACK_CHROMIUM = '/usr/bin/chromium'
ONSHAPE_ORIGIN = 'https://cad.onshape.com'
SIGN_IN_URL = f'{ONSHAPE_ORIGIN}/signin'
PACE_MS = 1500                     # between actions: human speed, not a load on Onshape
STEP_TIMEOUT_MS = 30000
_PRODUCTION_CLIENT_ID_PREFIX = 'VKDK'
# A scenario's outcome in the run's report.
RECORDED, FAILED, NEEDS_OWNER = 'recorded', 'failed', 'needs the owner in an Onshape checkpoint'

SELECTORS = {
    # cad.onshape.com/signin: email first, then Continue, then the password (seen Fri 10-09;
    # the password step's two selectors are not yet seen on the live page).
    'email': 'input[name="username"]',
    'email_continue': 'button.continue-button',
    'password': 'input[type="password"]',
    'sign_in_submit': 'button[type="submit"], button:has-text("Sign in")',
    # Any page of a signed-in session: the account menu in the top bar.
    'signed_in': '[data-testid="account-menu"], .os-user-menu, #user-menu-button',
    # A document: the right panel's icon for the dev app, and the panel it opens.
    'panel_icon': '[title*="PenguinCAM-chondl-dev"], [aria-label*="PenguinCAM-chondl-dev"]',
    'panel_frame': f'iframe[src*="localhost:{settings.RECORD_PORT}"]',
    # The Onshape OAuth consent page the panel's popup shows.
    'oauth_allow': 'button:has-text("Allow"), button:has-text("Authorize")',
    # A part by name in the parts list or the feature tree; {name} is filled in.
    'part_in_list': '.os-parts-list [title="{name}"], .os-feature-tree [title="{name}"]',
    # Onshape's part dialog (Add from another document...): its close button. Guessed, not
    # yet seen on the live page.
    'dialog_close': '.os-select-item-dialog button[aria-label="Close"], '
                    '.modal-dialog button:has-text("Close")',
    # tb-inch's workspace units (spec 5.1: no API sets them): Document menu, Workspace
    # units, Length, Inch, OK. Guessed, not yet seen on the live page.
    'document_menu': '[data-testid="document-menu"], button[aria-label="Document menu"]',
    'workspace_units': 'text=Workspace units',
    'units_length': '[data-testid="units-length"], select[name="length"]',
    'units_inch': 'text=Inch',
    'units_ok': 'button:has-text("OK")',
    # Inside the panel (PenguinCAM's own pages, not Onshape's).
    'panel_connect': 'a[href*="/onshape/auth"], button:has-text("Connect")',
    'panel_mode_print': 'input[name="mode"][value="print"]',
    'panel_mode_2d': 'input[name="mode"][value="2d"]',
    'print_next': '#btn-next',
    'print_back': '#btn-back',
    'print_add_from_document': '#btn-add-from-document',
    'strip_scenario': '#testbed-scenario',
    'strip_action': '#testbed-action',
    'strip_next': '#testbed-next',
    'strip_done': '#testbed-done',
}

_BOT_CHECK_FRAMES = ('captcha', 'recaptcha', 'hcaptcha', 'challenges.cloudflare.com')
_BOT_CHECK_TEXT = re.compile(
    r"verify (that )?you('re| are) (a )?human|unusual activity|security check|"
    r'verify your email', re.IGNORECASE)


class OnshapeCheckStopped(Exception):
    """Onshape asked whether this is a person. The run stopped; the message has the reason
    and the screenshot's path. Exit code 3."""


class OnshapeStepFailed(Exception):
    """A step failed (a control not found, a page that did not load). The message names
    the step and the screenshot's path. Exit code 1."""


class UiRunRefused(RuntimeError):
    """The run will not start: the production app's client id, no documents.json, or
    test documents build-docs has not finished."""


@dataclass(frozen=True)
class ScenarioOutcome:
    name: str
    outcome: str            # RECORDED, FAILED or NEEDS_OWNER
    detail: str = ''        # why, for FAILED and NEEDS_OWNER (redacted)

    def line(self) -> str:
        return f'{self.name}: {self.outcome}' + (f' ({self.detail})' if self.detail else '')


def owner_steps(scenario) -> list:
    """The steps of `scenario` the run cannot do: picks inside Onshape's dialogs."""
    return [step for step in scenario.steps if step.in_dialog]


def default_ui_scenarios() -> list:
    """The panel scenarios in the order the run takes them when none are named: the print
    scenarios first, those it can finish ahead of those that need the owner, so a failure
    elsewhere never starves the print path."""
    panel = [name for name, scenario in SCENARIOS.items() if scenario.panel]
    return sorted(panel, key=lambda name: (not name.startswith('print-'),
                                           bool(owner_steps(SCENARIOS[name])), panel.index(name)))


def _fresh_paths(name) -> list:
    return [settings.fresh_cassette_dir() / f'{name}.json', settings.fresh_cassette_dir() / name,
            settings.fresh_message_dir() / f'{name}.json']


def _fresh_state(name) -> dict:
    """{path: mtime in ns} of the fresh recording files of `name` that exist."""
    state = {}
    for path in _fresh_paths(name):
        try:
            state[path] = path.stat().st_mtime_ns
        except FileNotFoundError:
            pass
    return state


def _discard_fresh(name, before):
    """Remove the fresh recording files of `name` that are new or changed since `before`
    (a _fresh_state): a scenario that stopped part way must not reach the drift report. The
    development server drops its in-memory buffer when the next scenario is chosen."""
    for path, mtime in _fresh_state(name).items():
        if before.get(path) == mtime:
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


class _PasswordFillFailed(Exception):
    """The password field could not be filled. Carries nothing of the original error,
    whose text (Playwright's call log) holds the value that was being filled."""

    def __str__(self):
        return 'the password field could not be filled'


def _say(text):
    print(f'ui run: {redact_password(text)}', file=sys.stderr)


def redact_password(text) -> str:
    """`text` with ONSHAPE_PASSWORD, plain, URL-encoded or JSON-escaped, replaced by ***.

    Both the raw value and the stripped one, which settings.required_environment() returns
    and the sign-in form is filled with.
    """
    text = str(text)
    raw = os.environ.get('ONSHAPE_PASSWORD', '')
    forms = set()
    for password in {raw, raw.strip()} - {''}:
        forms |= {password, quote(password, safe=''), quote_plus(password),
                  json.dumps(password)[1:-1]}
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, '***')
    return text


def _reason(exc) -> str:
    """The first line of an error, redacted: Playwright's call logs follow on later lines."""
    lines = str(exc).strip().splitlines()
    return redact_password(lines[0] if lines else type(exc).__name__)


def chrome_executable() -> Optional[str]:
    """Google Chrome as install-chrome.sh unpacked it, if it is there and starts; else None."""
    if not (CHROME_BINARY.is_file() and os.access(CHROME_BINARY, os.X_OK)):
        return None
    try:
        result = subprocess.run([str(CHROME_BINARY), '--headless=new', '--no-sandbox',
                                 '--version'], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return str(CHROME_BINARY) if result.returncode == 0 else None


def _profile_dir() -> Path:
    path = settings.state_dir() / 'chrome-profile'
    path.mkdir(parents=True, exist_ok=True)
    return path


def disable_password_saving(profile: Path) -> None:
    """Set the kept profile's preferences so Chrome never offers to save a password.

    The profile is kept between runs; a password saved in it would sit on disk in its
    Login Data. Merged into profile/Default/Preferences before every launch, keeping
    Chrome's other preferences.
    """
    path = Path(profile) / 'Default' / 'Preferences'
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        prefs = json.loads(path.read_text())
    except (OSError, ValueError):
        prefs = {}
    if not isinstance(prefs, dict):
        prefs = {}
    prefs['credentials_enable_service'] = False
    profile_prefs = prefs.get('profile')
    if not isinstance(profile_prefs, dict):
        profile_prefs = prefs['profile'] = {}
    profile_prefs['password_manager_enabled'] = False
    path.write_text(json.dumps(prefs))


def panel_url_file() -> Path:
    """Where the run records which panel URL the development app opens (spec 5.9)."""
    return settings.state_dir() / 'panel-url.txt'


def tb_inch_units_file() -> Path:
    """Present once tb-inch's workspace units are inches; the run then skips that step."""
    return settings.state_dir() / 'tb-inch-units.txt'


def screenshot(page, label) -> Path:
    """Save the page to state_dir()/screenshots/<UTC time>Z-<label>.png; return the path."""
    directory = settings.state_dir() / 'screenshots'
    directory.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[^a-z0-9-]+', '-', label.lower()).strip('-') or 'page'
    path = directory / f'{datetime.now(timezone.utc):%Y%m%d-%H%M%S}Z-{safe}.png'
    page.screenshot(path=str(path))
    return path


def _screenshot_or_reason(page, label) -> str:
    try:
        return str(screenshot(page, label))
    except Exception as exc:
        return f'none (the screenshot failed: {_reason(exc)})'


def ui_step(page, step, action, advice=''):
    """Run `action()`. If it fails, stop the run: screenshot, then OnshapeStepFailed naming
    `step` (and `advice`, what the owner can do). A bot check stop passes through.

    The new error is raised outside the `except`, so the original, whose text may hold
    a filled value, is not chained to it.
    """
    try:
        return action()
    except (OnshapeCheckStopped, OnshapeStepFailed):
        raise
    except Exception as exc:
        reason = _reason(exc)
    path = _screenshot_or_reason(page, step)
    raise OnshapeStepFailed(f'step {step!r} failed: {reason}; screenshot: {path}'
                            + (f'. {advice}' if advice else ''))


def _grant_local_network(context, page):
    """Let cad.onshape.com frame the panel on http://localhost (spec 5.4's two ways)."""
    try:
        context.grant_permissions(['local-network-access'], origin=ONSHAPE_ORIGIN)
        return 'playwright'
    except Exception:
        pass
    try:
        context.new_cdp_session(page).send('Browser.grantPermissions', {
            'origin': ONSHAPE_ORIGIN, 'permissions': ['localNetwork', 'loopbackNetwork']})
        return 'cdp'
    except Exception as exc:
        _say(f'could not grant the local network permission ({_reason(exc)}); the panel '
             'may not load')
        return None


def launch_persistent_chrome(playwright, headed=True, extra_args=()):
    """(context, executable): a persistent browser context on the kept profile.

    Google Chrome when it is installed and starts, else /usr/bin/chromium, which is logged.
    The sandbox is off for both: the container has no user namespaces, so neither starts
    with it, and the run visits only cad.onshape.com and the local development server.
    The profile's password manager is off (disable_password_saving).
    """
    disable_password_saving(_profile_dir())
    candidates = []
    chrome = chrome_executable()
    if chrome:
        candidates.append(chrome)
    else:
        _say(f'Google Chrome is not installed or does not start ({CHROME_BINARY}); '
             f'using {FALLBACK_CHROMIUM}')
    candidates.append(FALLBACK_CHROMIUM)
    last_error = None
    for executable in candidates:
        try:
            context = playwright.chromium.launch_persistent_context(
                str(_profile_dir()), executable_path=executable, headless=not headed,
                args=['--no-sandbox', *extra_args], viewport={'width': 1400, 'height': 900})
        except Exception as exc:
            last_error = _reason(exc)
            if executable != FALLBACK_CHROMIUM:
                _say(f'Google Chrome did not launch ({last_error}); using {FALLBACK_CHROMIUM}')
            continue
        _say(f'browser: {executable}')
        return context, executable
    raise RuntimeError(f'no browser could be launched: {last_error}')


def detect_onshape_bot_check(page) -> Optional[str]:
    """Why the page looks like Onshape asking whether this is a person, or None."""
    url = (getattr(page, 'url', '') or '').lower()
    if any(marker in url for marker in _BOT_CHECK_FRAMES):
        return f'a challenge page at {url.split("?")[0]}'
    sources = page.eval_on_selector_all(
        'iframe', 'frames => frames.map(f => f.getAttribute("src") || "")')
    for source in sources:
        lowered = (source or '').lower()
        if any(marker in lowered for marker in _BOT_CHECK_FRAMES):
            return f'a challenge frame from {source.split("?")[0]}'
    try:
        text = page.inner_text('body', timeout=5000)
    except Exception:
        text = ''
    match = _BOT_CHECK_TEXT.search(text or '')
    if match:
        return f'the page says "{match.group(0)}"'
    return None


def stop_on_onshape_check(page, where) -> None:
    """Raise OnshapeCheckStopped, after a screenshot, if the page shows a bot check."""
    reason = detect_onshape_bot_check(page)
    if reason:
        path = _screenshot_or_reason(page, f'onshape-check-{where}')
        raise OnshapeCheckStopped(f'{reason} ({where}); screenshot: {path}')


def _settle(page):
    try:
        page.wait_for_load_state('networkidle', timeout=STEP_TIMEOUT_MS)
    except Exception:
        pass                    # Onshape keeps sockets open; the pause below is the floor
    page.wait_for_timeout(PACE_MS)


def _fill_password(page, password):
    failed = False
    try:
        page.locator(SELECTORS['password']).first.fill(password)
    except Exception:
        failed = True
    if failed:                  # raised here, outside the except: nothing is chained
        raise _PasswordFillFailed()


def sign_in_to_onshape(page) -> None:
    """Fill Onshape's sign-in form with ONSHAPE_USERNAME and ONSHAPE_PASSWORD and submit.

    The page is already on the sign-in page. Neither value is logged, and a failed step
    stops with a screenshot and a message that cannot hold the password.
    """
    username, password = settings.required_environment('ONSHAPE_USERNAME', 'ONSHAPE_PASSWORD')

    def email():
        page.wait_for_selector(SELECTORS['email'], timeout=STEP_TIMEOUT_MS)
        stop_on_onshape_check(page, 'sign-in page')
        page.fill(SELECTORS['email'], username)
        page.wait_for_timeout(PACE_MS)

    def to_password():
        if not page.is_visible(SELECTORS['password']):    # the two-step form: email first
            page.click(SELECTORS['email_continue'])
            page.wait_for_selector(SELECTORS['password'], timeout=STEP_TIMEOUT_MS)
        stop_on_onshape_check(page, 'password page')

    def submit():
        page.wait_for_timeout(PACE_MS)
        page.click(SELECTORS['sign_in_submit'])

    ui_step(page, 'sign-in: email', email)
    ui_step(page, 'sign-in: continue to the password', to_password)
    ui_step(page, 'sign-in: password', lambda: _fill_password(page, password))
    ui_step(page, 'sign-in: submit', submit)
    _say('submitted the Onshape sign-in form')
    # The page navigates now, so the bot check's page script can fail mid-way.
    ui_step(page, 'sign-in: after submit',
            lambda: (_settle(page), stop_on_onshape_check(page, 'after sign-in')))


def _ensure_signed_in(page):
    def documents_page():
        page.goto(f'{ONSHAPE_ORIGIN}/documents')
        _settle(page)
        stop_on_onshape_check(page, 'documents page')
        return page.url.startswith(SIGN_IN_URL) or page.is_visible(SELECTORS['email'])

    if ui_step(page, 'documents page', documents_page):
        if not page.url.startswith(SIGN_IN_URL):
            ui_step(page, 'sign-in page', lambda: (page.goto(SIGN_IN_URL), _settle(page)))
        sign_in_to_onshape(page)
    else:
        _say('the kept Chrome profile is still signed in to Onshape')
    if page.url.startswith(SIGN_IN_URL):
        path = _screenshot_or_reason(page, 'sign-in-failed')
        raise OnshapeStepFailed(f"step 'sign-in' failed: still on the sign-in page after "
                                f'submitting; screenshot: {path}')


def _open_document(page, document, where):
    page.goto(f"{ONSHAPE_ORIGIN}/documents/{document['did']}/w/{document['wid']}"
              f"/e/{document['eid']}")
    _settle(page)
    stop_on_onshape_check(page, where)


def _open_workspace_units(page):
    for key in ('document_menu', 'workspace_units'):
        page.locator(SELECTORS[key]).first.click()
        page.wait_for_timeout(PACE_MS)


def _length_unit(page) -> str:
    """What the open Workspace units dialog shows for Length."""
    field = page.locator(SELECTORS['units_length']).first
    try:
        return field.input_value(timeout=STEP_TIMEOUT_MS)
    except Exception:
        return field.inner_text(timeout=STEP_TIMEOUT_MS)


def switch_tb_inch_units(page, document) -> bool:
    """The first run's extra (spec 5.1): set tb-inch's workspace units to inches.

    Done once: tb_inch_units_file() is written only after the dialog, opened again, reads
    inches. False when it was already done.
    """
    marker = tb_inch_units_file()
    if marker.is_file():
        return False
    advice = ("Switch tb-inch's units to inches by hand in Onshape (Document menu, "
              f'Workspace units, Length: Inch), then create {marker} so the run skips '
              'this step')

    def switch():
        _open_document(page, document, 'tb-inch units')
        _open_workspace_units(page)
        for key in ('units_length', 'units_inch', 'units_ok'):
            page.locator(SELECTORS[key]).first.click()
            page.wait_for_timeout(PACE_MS)

    def check():
        _open_workspace_units(page)
        unit = _length_unit(page)
        page.keyboard.press('Escape')               # closes the dialog unchanged
        page.wait_for_timeout(PACE_MS)
        if 'inch' not in unit.lower():
            raise RuntimeError(f'the Length unit reads {unit.strip()!r}, not inches')

    ui_step(page, 'tb-inch units', switch, advice)
    ui_step(page, 'tb-inch units: check', check, advice)
    marker.write_text('inch\n')
    _say("tb-inch's workspace units are now inches")
    return True


class _PanelScenario:
    """Drives one panel scenario in a real Onshape document (spec 5.9, steps 4 and 5)."""

    def __init__(self, page, scenario, document):
        self.page = page
        self.scenario = scenario
        self.document = document
        self.panel = page.frame_locator(SELECTORS['panel_frame'])

    def _step(self, step, action):
        return ui_step(self.page, f'{self.scenario.name}: {step}', action)

    def run(self):
        name = self.scenario.name
        self._step('open document', lambda: _open_document(self.page, self.document, name))
        for index, step in enumerate(self.scenario.steps):
            def act(step=step, index=index):
                if index and self._strip_ready():
                    self.panel.locator(SELECTORS['strip_next']).click()
                    self.page.wait_for_timeout(PACE_MS)
                self._do(step)
                self.page.wait_for_timeout(PACE_MS)
                stop_on_onshape_check(self.page, f'{name}-{step.name}')
            self._step(step.name, act)
        self._step('done', lambda: (self.panel.locator(SELECTORS['strip_done']).click(),
                                    _settle(self.page)))

    def _strip_ready(self):
        try:
            return self.panel.locator(SELECTORS['strip_scenario']).count() > 0
        except Exception:
            return False

    def _do(self, step):
        if step.name == 'open panel':
            self._open_panel()
        elif step.action == 'choose-print':
            self.panel.locator(SELECTORS['panel_mode_print']).check()
            strip = self.panel.locator(SELECTORS['strip_scenario'])
            strip.wait_for(timeout=STEP_TIMEOUT_MS)
            strip.select_option(self.scenario.name)
        elif step.action in PAGE_ASKS:          # hidden by the strip on the print page
            self.panel.locator(SELECTORS['strip_action']).evaluate(
                '(button, action) => window.PenguinCAMTestbed.act(action)', step.action)
        elif step.action == 'ask-unbounded':
            # Back on Setup first: the page stops its own request, so only the strip's is open.
            self.panel.locator(SELECTORS['print_back']).click()
            self.page.wait_for_timeout(PACE_MS)
            self.panel.locator(SELECTORS['strip_action']).click()
        elif step.action in STRIP_ACTIONS:
            self.panel.locator(SELECTORS['strip_action']).click()
        elif step.action == 'print-parts':
            self.panel.locator(SELECTORS['print_next']).click()
        elif step.action == 'print-dialog':
            self.panel.locator(SELECTORS['print_add_from_document']).click()
        elif step.action == 'back':
            self.panel.locator(SELECTORS['panel_mode_2d']).check()
        elif step.select:
            for number, part in enumerate(step.select):
                target = self.page.locator(SELECTORS['part_in_list'].format(name=part)).first
                target.click(modifiers=['Control'] if number else None)
                self.page.wait_for_timeout(PACE_MS)
        elif step.name == 'deselect':
            self.page.keyboard.press('Escape')
        elif step.name == 'dialog closed':
            self.page.locator(SELECTORS['dialog_close']).first.click()
        # Anything else (the team config banner) is only looked at.

    def _open_panel(self):
        if self.page.locator(SELECTORS['panel_frame']).count() == 0:
            self.page.locator(SELECTORS['panel_icon']).first.click()
        frame = self.page.locator(SELECTORS['panel_frame']).first
        frame.wait_for(timeout=STEP_TIMEOUT_MS)
        url = (frame.get_attribute('src') or '').split('?')[0]
        panel_url_file().write_text(url + '\n')
        _say(f'panel URL: {url} (recorded in {panel_url_file()})')
        _settle(self.page)
        connect = self.panel.locator(SELECTORS['panel_connect'])
        if connect.count():                      # the one place OAuth runs
            with self.page.expect_popup(timeout=STEP_TIMEOUT_MS) as popup_info:
                connect.first.click()
            popup = popup_info.value
            _settle(popup)
            stop_on_onshape_check(popup, 'oauth')
            allow = popup.locator(SELECTORS['oauth_allow'])
            if not popup.is_closed() and allow.count():
                allow.first.click()
            _settle(self.page)


def _run_scenario(page, name, documents) -> ScenarioOutcome:
    """One scenario's outcome. A bot check stop passes through and ends the run."""
    scenario = SCENARIOS[name]
    needs = owner_steps(scenario)
    if needs:
        detail = f"step {needs[0].name!r}: {needs[0].instruction}"
        _say(f'{name}: {NEEDS_OWNER}, {detail}')
        return ScenarioOutcome(name, NEEDS_OWNER, detail)
    before = _fresh_state(name)
    try:
        _PanelScenario(page, scenario, documents[scenario.document]).run()
    except OnshapeStepFailed as exc:
        detail = redact_password(exc)
    else:
        _say(f'{name}: done')
        return ScenarioOutcome(name, RECORDED)
    _discard_fresh(name, before)
    _say(f'{name} stopped: {detail}; its partial recording was discarded, going on')
    return ScenarioOutcome(name, FAILED, detail)


def _run(scenarios, documents, headed) -> list:
    from playwright.sync_api import sync_playwright

    outcomes = []
    with RecordDevServer() as server, sync_playwright() as playwright:
        _say(f'development server log: {server.log_path}')
        context, _executable = launch_persistent_chrome(playwright, headed)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(STEP_TIMEOUT_MS)
            _grant_local_network(context, page)
            _ensure_signed_in(page)
            if 'tb-inch' in documents:
                switch_tb_inch_units(page, documents['tb-inch'])
            for name in scenarios:
                try:
                    outcomes.append(_run_scenario(page, name, documents))
                except OnshapeCheckStopped:
                    done = ', '.join(o.line() for o in outcomes) or 'none'
                    _say(f'stopped during {name}; outcomes before it: {done}')
                    raise
        finally:
            context.close()
    return outcomes


def run_onshape_ui(scenarios, headed=True) -> list:
    """Run the panel scenarios in real Onshape; return one ScenarioOutcome per scenario.

    Needs ONSHAPE_USERNAME, ONSHAPE_PASSWORD and the dev app's ONSHAPE_CLIENT_ID and
    ONSHAPE_CLIENT_SECRET; refuses the production app's client id. Whatever stops the run,
    the error that leaves here has ONSHAPE_PASSWORD redacted and nothing chained to it.
    """
    # A panel URL left by an earlier run must not be reported as this run's.
    panel_url_file().unlink(missing_ok=True)
    values = dict(zip(UI_VARIABLES, settings.required_environment(*UI_VARIABLES)))
    if values['ONSHAPE_CLIENT_ID'].startswith(_PRODUCTION_CLIENT_ID_PREFIX):
        raise UiRunRefused('the automated Onshape UI run refuses the production Onshape '
                           "app: ONSHAPE_CLIENT_ID must be PenguinCAM-chondl-dev's")
    if not settings.DOCUMENTS_FILE.is_file():
        raise UiRunRefused(f'{settings.DOCUMENTS_FILE} does not exist; run build-docs first')
    documents = json.loads(settings.DOCUMENTS_FILE.read_text())
    pending = pending_documents(documents)
    if pending:
        listed = ', '.join(f'{name} ({why})' for name, why in sorted(pending.items()))
        raise UiRunRefused(f'documents.json has pending test documents: {listed}; finish '
                           'build-docs first (move each one into the test folder in Onshape, '
                           'then run build-docs again)')
    try:
        return _run(scenarios, documents, headed)
    except OnshapeCheckStopped as exc:
        stopped = OnshapeCheckStopped(redact_password(exc))
    except OnshapeStepFailed as exc:
        stopped = OnshapeStepFailed(redact_password(exc))
    except Exception as exc:
        stopped = OnshapeStepFailed(f'the UI run failed: {_reason(exc)}')
    raise stopped               # outside the except: the original is not chained
