"""The browser harness: a replay development server, a test browser and the fake Onshape host.

The test browser serves https://cad-testbed.onshape.com itself through Playwright request
routing (spec 5.4): the host page from testbed/host/, and the scenarios' message logs. That
origin is granted Chrome's local network permission so it may frame the panel from
http://localhost. Every other *.onshape.com request is refused inside the browser, so
nothing reaches Onshape.

Playwright comes from testbed/requirements.txt and drives the system Chromium; it never
downloads a browser.
"""
import os
import re
import signal
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from testbed import settings
from testbed.settings import FAKE_HOST_ORIGIN, REPLAY_PORT

REPO = settings.TESTBED_DIR.parent
HOST_DIR = settings.TESTBED_DIR / 'host'
LOCAL_NETWORK_PERMISSIONS = ['localNetwork', 'loopbackNetwork']    # the CDP fallback's names
SERVER_START_TIMEOUT_S = 120
_SCENARIO_NAME = re.compile(r'[a-z0-9][a-z0-9-]*')
# Any Onshape host but the fake one; refused before the network.
_OTHER_ONSHAPE = re.compile(r'^[a-z]+://([^/?#]*\.)?onshape\.com(:\d+)?([/?#]|$)')
# Variables that would point the server at other recordings, or make it a deployed one,
# and the Onshape sign-in credentials, which only the UI run's browser needs.
_SERVER_DROPPED = ('TESTBED_CASSETTE_DIR', 'TESTBED_MESSAGE_DIR', 'FLASK_ENV',
                   'RAILWAY_ENVIRONMENT', 'VERCEL', 'BASE_URL', 'ONSHAPE_USERNAME',
                   'ONSHAPE_PASSWORD')


def _answers(url):
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status
    except Exception:
        return None


class ReplayDevServer:
    """The development server in replay mode on `port`, for as long as the `with` lasts.

    `cassette_dir` and `message_dir`, when given, replace testbed/cassettes and
    testbed/messages (the self-tests' synthetic fixtures). Its output goes to
    settings.state_dir()/logs/replay-devserver.log.
    """

    MODE = 'replay'

    def __init__(self, port=REPLAY_PORT, cassette_dir=None, message_dir=None):
        self.port = port
        self.cassette_dir = cassette_dir
        self.message_dir = message_dir
        self.url = f'http://localhost:{port}'
        self.log_path = settings.state_dir() / 'logs' / f'{self.MODE}-devserver.log'
        self.process = None

    def __enter__(self):
        if _answers(f'http://127.0.0.1:{self.port}/') is not None:
            raise RuntimeError(f'something already answers on port {self.port}; '
                               'stop it before running the browser scenarios')
        env = {k: v for k, v in os.environ.items() if k not in _SERVER_DROPPED}
        env.update(PORT=str(self.port), PENGUINCAM_TESTBED=self.MODE, EMBED_COOKIES='1')
        if self.cassette_dir:
            env['TESTBED_CASSETTE_DIR'] = str(self.cassette_dir)
        if self.message_dir:
            env['TESTBED_MESSAGE_DIR'] = str(self.message_dir)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, 'wb') as log:
            self.process = subprocess.Popen(
                ['uv', 'run', 'python', 'frc_cam_gui_app.py'], cwd=REPO, env=env,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + SERVER_START_TIMEOUT_S
        while _answers(f'http://127.0.0.1:{self.port}/') != 200:
            if self.process.poll() is not None or time.monotonic() > deadline:
                self.__exit__(None, None, None)
                tail = self.log_path.read_text(errors='replace')[-2000:]
                raise RuntimeError(f'the {self.MODE} development server did not answer on port '
                                   f'{self.port}; its log ({self.log_path}) ends:\n{tail}')
            time.sleep(0.5)
        return self

    def __exit__(self, *exc):
        # Flask's reloader runs a parent and a child; the session holds both and uv.
        if self.process is None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(10)
        except ProcessLookupError:
            pass
        self.process = None


class RecordDevServer(ReplayDevServer):
    """The development server in record mode on 6238, for the automated Onshape UI run.

    Started as the workspace instructions describe: EMBED_COOKIES=1, the code's default
    BASE_URL, and the dev app's ONSHAPE_CLIENT_ID and ONSHAPE_CLIENT_SECRET from this
    environment. Its output goes to settings.state_dir()/logs/record-devserver.log.
    """

    MODE = 'record'

    def __init__(self, port=settings.RECORD_PORT):
        super().__init__(port)


def _fulfill_text(route, status, body, content_type='text/plain; charset=utf-8'):
    route.fulfill(status=status, content_type=content_type, body=body)


def serve_fake_host(route, message_dir=None):
    """Answer a request for the fake Onshape host; anything else it is asked for is a 404.

    /documents/** is the host page, /_testbed/host.js its script and
    /_testbed/messages/<scenario>.json a message log from `message_dir` (default
    settings.MESSAGE_DIR).
    """
    path = urlsplit(route.request.url).path
    if path.startswith('/documents/'):
        return _fulfill_text(route, 200, (HOST_DIR / 'index.html').read_text(),
                             'text/html; charset=utf-8')
    if path == '/_testbed/host.js':
        return _fulfill_text(route, 200, (HOST_DIR / 'host.js').read_text(),
                             'text/javascript; charset=utf-8')
    prefix = '/_testbed/messages/'
    if path.startswith(prefix) and path.endswith('.json'):
        name = path[len(prefix):-len('.json')]
        log = Path(message_dir or settings.MESSAGE_DIR) / f'{name}.json'
        if _SCENARIO_NAME.fullmatch(name) and log.is_file():
            return _fulfill_text(route, 200, log.read_text(), 'application/json')
        return _fulfill_text(route, 404, f'no message log for scenario {name!r}')
    return _fulfill_text(route, 404, f'not served by the fake Onshape host: {path}')


def grant_local_network(browser, context) -> str:
    """Let the fake Onshape host frame http://localhost; return which way it was granted.

    Playwright 1.58 and later call the permission `local-network-access`. Failing that,
    Chrome's own names are granted through the browser protocol, which needs the
    context's id, read from a page's target.
    """
    try:
        context.grant_permissions(['local-network-access'], origin=FAKE_HOST_ORIGIN)
        return 'playwright'
    except Exception:
        page = context.new_page()
        try:
            info = context.new_cdp_session(page).send('Target.getTargetInfo')
            browser.new_browser_cdp_session().send('Browser.grantPermissions', {
                'origin': FAKE_HOST_ORIGIN, 'permissions': LOCAL_NETWORK_PERMISSIONS,
                'browserContextId': info['targetInfo']['browserContextId']})
        finally:
            page.close()
        return 'cdp'


def launch_test_browser(headed=False, executable=None, message_dir=None):
    """(playwright, browser, context): Chromium with the fake Onshape host routed.

    The caller closes the browser and stops playwright. `message_dir` is where the fake
    Onshape host reads message logs (default settings.MESSAGE_DIR).
    """
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.launch(
            executable_path=executable or settings.chromium_executable(), headless=not headed)
        context = browser.new_context()
        grant_local_network(browser, context)
        # Routes run newest first, so the fake host's route is tried before the refusal.
        context.route(_OTHER_ONSHAPE, lambda route: route.abort('blockedbyclient'))
        context.route(FAKE_HOST_ORIGIN + '/**',
                      lambda route: serve_fake_host(route, message_dir))
    except BaseException:
        playwright.stop()
        raise
    return playwright, browser, context


def host_url(document: dict, scenario: str, port=REPLAY_PORT, version_id=None) -> str:
    """The fake Onshape host's page for `document` ({did, wid, eid}), answering from the
    message log of `scenario`. With `version_id`, the panel opens on that version."""
    where = f'v/{version_id}' if version_id else f"w/{document['wid']}"
    query = urlencode({'scenario': scenario, 'port': port})
    return f"{FAKE_HOST_ORIGIN}/documents/{document['did']}/{where}/e/{document['eid']}?{query}"


def sign_in_for_replay(context, port=REPLAY_PORT, scenario=None) -> None:
    """Sign the browser in through /testbed/sign-in and make `scenario` current.

    Done from a page on the server's own origin, so the session cookie lands in the
    browser's cookie jar exactly as a sign-in in the panel would leave it.
    """
    page = context.new_page()
    try:
        page.goto(f'http://localhost:{port}/testbed/scenarios')
        result = page.evaluate("""async (scenario) => {
            const signIn = await fetch('/testbed/sign-in', {credentials: 'same-origin'});
            if (signIn.status !== 204) return 'sign-in answered ' + signIn.status;
            if (!scenario) return null;
            const chosen = await fetch('/testbed/scenario', {
                method: 'POST', credentials: 'same-origin',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: scenario})});
            return chosen.ok ? null : 'choosing ' + scenario + ' answered ' + chosen.status;
        }""", scenario)
    finally:
        page.close()
    if result:
        raise RuntimeError(f'sign_in_for_replay: {result}')
