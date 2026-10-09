"""The automated Onshape UI run: `python -m testbed record --ui` (spec 5.9).

Starts the development server in record mode on 6238, launches real Google Chrome headed
(under Xvfb in the container) with a profile kept in settings.state_dir()/chrome-profile,
signs in to cad.onshape.com unless the kept session still is, and drives each panel
scenario through the PenguinCAM-chondl-dev panel and the checklist strip.

It acts at human speed and never tries to get past a bot check: on a CAPTCHA, a challenge
or a verification step it saves a screenshot to settings.state_dir()/screenshots/ and
raises OnshapeCheckStopped, which the command line turns into exit 3. ONSHAPE_PASSWORD goes
only into Onshape's sign-in form; it is never logged or written anywhere.

Every Onshape page selector is in SELECTORS, so a change in Onshape's interface is a
one-line fix there.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from testbed import settings
from testbed.browser import RecordDevServer
from testbed.scenarios import SCENARIOS

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
# The strip's buttons; the other actions are done in the panel or in Onshape.
_STRIP_ACTIONS = ('ask-part', 'ask-two-parts', 'dialog-part', 'dialog-parts', 'close-dialog')

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
    # Inside the panel (PenguinCAM's own pages, not Onshape's).
    'panel_connect': 'a[href*="/onshape/auth"], button:has-text("Connect")',
    'panel_mode_print': 'input[name="mode"][value="print"]',
    'panel_mode_2d': 'input[name="mode"][value="2d"]',
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
    """A step could not find its control in Onshape's interface; a screenshot was saved."""


def _say(text):
    print(f'ui run: {text}', file=sys.stderr)


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


def screenshot(page, label) -> Path:
    """Save the page to state_dir()/screenshots/<time>-<label>.png; return the path."""
    directory = settings.state_dir() / 'screenshots'
    directory.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[^a-z0-9-]+', '-', label.lower()).strip('-') or 'page'
    path = directory / f"{datetime.now():%Y%m%d-%H%M%S}-{safe}.png"
    page.screenshot(path=str(path))
    return path


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
        _say(f'could not grant the local network permission ({exc}); the panel may not load')
        return None


def launch_persistent_chrome(playwright, headed=True):
    """(context, executable): a persistent browser context on the kept profile.

    Google Chrome when it is installed and starts, else /usr/bin/chromium, which is logged.
    Chrome's sandbox is off: the container has no user namespaces, and the run visits only
    cad.onshape.com and the local development server.
    """
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
                args=['--no-sandbox'], viewport={'width': 1400, 'height': 900})
        except Exception as exc:
            last_error = exc
            if executable != FALLBACK_CHROMIUM:
                _say(f'Google Chrome did not launch ({str(exc).splitlines()[0]}); '
                     f'using {FALLBACK_CHROMIUM}')
            continue
        _say(f'browser: {executable}')
        return context, executable
    raise RuntimeError(f'no browser could be launched: {last_error}')


def detect_onshape_bot_check(page) -> Optional[str]:
    """Why the page looks like Onshape asking whether this is a person, or None."""
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
        path = screenshot(page, f'onshape-check-{where}')
        raise OnshapeCheckStopped(f'{reason} ({where}); screenshot: {path}')


def _settle(page):
    try:
        page.wait_for_load_state('networkidle', timeout=STEP_TIMEOUT_MS)
    except Exception:
        pass                    # Onshape keeps sockets open; the pause below is the floor
    page.wait_for_timeout(PACE_MS)


def sign_in_to_onshape(page) -> None:
    """Fill Onshape's sign-in form with ONSHAPE_USERNAME and ONSHAPE_PASSWORD and submit.

    The page is already on the sign-in page. Neither value is logged.
    """
    username, password = settings.required_environment('ONSHAPE_USERNAME', 'ONSHAPE_PASSWORD')
    page.wait_for_selector(SELECTORS['email'], timeout=STEP_TIMEOUT_MS)
    stop_on_onshape_check(page, 'sign-in page')
    page.fill(SELECTORS['email'], username)
    page.wait_for_timeout(PACE_MS)
    if not page.is_visible(SELECTORS['password']):        # the two-step form: email first
        page.click(SELECTORS['email_continue'])
        page.wait_for_selector(SELECTORS['password'], timeout=STEP_TIMEOUT_MS)
    page.fill(SELECTORS['password'], password)
    page.wait_for_timeout(PACE_MS)
    page.click(SELECTORS['sign_in_submit'])
    _say('submitted the Onshape sign-in form')
    _settle(page)
    stop_on_onshape_check(page, 'after sign-in')


def _ensure_signed_in(page):
    page.goto(f'{ONSHAPE_ORIGIN}/documents')
    _settle(page)
    stop_on_onshape_check(page, 'documents page')
    if page.url.startswith(SIGN_IN_URL) or page.is_visible(SELECTORS['email']):
        if not page.url.startswith(SIGN_IN_URL):
            page.goto(SIGN_IN_URL)
            _settle(page)
        sign_in_to_onshape(page)
    else:
        _say('the kept Chrome profile is still signed in to Onshape')
    if page.url.startswith(SIGN_IN_URL):
        path = screenshot(page, 'sign-in-failed')
        raise OnshapeStepFailed(f'still on the sign-in page after signing in; screenshot: {path}')


class _PanelScenario:
    """Drives one panel scenario in a real Onshape document (spec 5.9, steps 4 and 5)."""

    def __init__(self, page, scenario, document):
        self.page = page
        self.scenario = scenario
        self.document = document
        self.panel = page.frame_locator(SELECTORS['panel_frame'])

    def run(self):
        doc = self.document
        self.step = 'open document'
        self.page.goto(f"{ONSHAPE_ORIGIN}/documents/{doc['did']}/w/{doc['wid']}/e/{doc['eid']}")
        _settle(self.page)
        stop_on_onshape_check(self.page, self.scenario.name)
        for index, step in enumerate(self.scenario.steps):
            self.step = step.name
            if index and self._strip_ready():
                self.panel.locator(SELECTORS['strip_next']).click()
                self.page.wait_for_timeout(PACE_MS)
            self._do(step)
            self.page.wait_for_timeout(PACE_MS)
            stop_on_onshape_check(self.page, f'{self.scenario.name}-{step.name}')
        self.step = 'done'
        self.panel.locator(SELECTORS['strip_done']).click()
        _settle(self.page)

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
        elif step.action in _STRIP_ACTIONS:
            self.panel.locator(SELECTORS['strip_action']).click()
        elif step.action == 'back':
            self.panel.locator(SELECTORS['panel_mode_2d']).check()
        elif step.select:
            for number, name in enumerate(step.select):
                target = self.page.locator(SELECTORS['part_in_list'].format(name=name)).first
                target.click(modifiers=['Control'] if number else None)
                self.page.wait_for_timeout(PACE_MS)
        elif step.name == 'deselect':
            self.page.keyboard.press('Escape')
        # Anything else (the team config banner) is only looked at.

    def _open_panel(self):
        if self.page.locator(SELECTORS['panel_frame']).count() == 0:
            self.page.locator(SELECTORS['panel_icon']).first.click()
        frame = self.page.locator(SELECTORS['panel_frame']).first
        frame.wait_for(timeout=STEP_TIMEOUT_MS)
        _say(f"panel URL: {(frame.get_attribute('src') or '').split('?')[0]}")
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


def run_onshape_ui(scenarios, headed=True) -> Path:
    """Run the panel scenarios in real Onshape; return the fresh message log directory.

    Needs ONSHAPE_USERNAME, ONSHAPE_PASSWORD and the dev app's ONSHAPE_CLIENT_ID and
    ONSHAPE_CLIENT_SECRET; refuses the production app's client id.
    """
    values = dict(zip(UI_VARIABLES, settings.required_environment(*UI_VARIABLES)))
    if values['ONSHAPE_CLIENT_ID'].startswith(_PRODUCTION_CLIENT_ID_PREFIX):
        raise RuntimeError('the automated Onshape UI run refuses the production Onshape '
                           'app: ONSHAPE_CLIENT_ID must be PenguinCAM-chondl-dev\'s')
    if not settings.DOCUMENTS_FILE.is_file():
        raise RuntimeError(f'{settings.DOCUMENTS_FILE} does not exist; run build-docs first')
    documents = json.loads(settings.DOCUMENTS_FILE.read_text())
    from playwright.sync_api import sync_playwright

    with RecordDevServer() as server, sync_playwright() as playwright:
        _say(f'development server log: {server.log_path}')
        context, _executable = launch_persistent_chrome(playwright, headed)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(STEP_TIMEOUT_MS)
            _grant_local_network(context, page)
            _ensure_signed_in(page)
            for name in scenarios:
                scenario = SCENARIOS[name]
                driver = _PanelScenario(page, scenario, documents[scenario.document])
                try:
                    driver.run()
                except (OnshapeCheckStopped, OnshapeStepFailed):
                    raise
                except Exception as exc:
                    path = screenshot(page, f'{name}-{driver.step}')
                    raise OnshapeStepFailed(f'{name}: step {driver.step!r} failed: '
                                            f'{str(exc).splitlines()[0]}; screenshot: {path}'
                                            ) from None
                _say(f'{name}: done')
        finally:
            context.close()
    return settings.fresh_message_dir()
