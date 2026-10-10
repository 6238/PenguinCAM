# Onshape test bed

Developer guide for the test bed in `testbed/`: recordings of real Onshape traffic, fakes
that replay them, and tools that check whether real Onshape still behaves the way the
recordings say. It covers the print path's Onshape side only (parts, selections and 3D
exports). The CNC path (faces, DXF, 2D and 2.5D) is not tested here.

Every live Onshape call made with API keys or through a private OAuth app counts against
the account's annual API allowance, and the Education plans allow only a few thousand calls
a year ([API limits](https://onshape-public.github.io/docs/auth/limits/)). So the test bed
works from recordings and spends live calls deliberately, through a counted budget.

## 1. Terms

Each term means one thing everywhere in this guide and in the code.

| Term | Meaning |
|---|---|
| **test bed** | Everything in `testbed/`: test documents, recordings, fakes, scenarios and tools. |
| **development server** | PenguinCAM's Flask server started locally: on port 6238 in record mode, on port 6240 when a browser scenario starts one in replay mode. |
| **panel** | PenguinCAM inside Onshape's right panel: `/onshape-panel` first, and the print wizard (`/print`) once 3D Printing is chosen. |
| **test folder** | The Onshape folder the test documents live in, named by `TESTBED_FOLDER_ID`. |
| **test documents** | The Onshape documents the test bed builds in the test folder (section 6). |
| **scenario** | A named, scripted run, for example `part-select`. |
| **record mode** | Scenarios run against real Onshape and their traffic is saved. |
| **replay mode** | Scenarios run against the fakes, served from saved traffic. |
| **cassette** | The saved API traffic of one scenario: one file of request and response pairs. |
| **message log** | The saved panel messages of one scenario, in both directions. |
| **recorder** | The transport adapter that saves API traffic in record mode. |
| **replay adapter** | The transport adapter that answers API calls from cassettes, inside the process, in tests and in the development server alike. |
| **current scenario** | The one scenario a process is recording or replaying at a time. |
| **fake Onshape host** | The page at `https://cad-testbed.onshape.com` that embeds the panel the way Onshape does and plays Onshape's side of the panel messages from message logs. |
| **development sign-in** | Signing the panel in with the API keys instead of OAuth, in replay mode only. |
| **live API check** | Running the API side of every scenario in record mode with the API keys, unattended. |
| **automated Onshape UI run** | Playwright in real Chrome signing in to Onshape and running the scenarios in the panel inside real Onshape, in record mode. |
| **Onshape checkpoint** | A person running the same scenarios by hand in Chrome while PenguinCAM records; the fallback for the automated Onshape UI run. |
| **checklist strip** | The bar at the top of the print page, shown whenever the test bed is on, that offers the scenarios and each scenario's steps. |
| **drift report** | The differences between a fresh recording and the stored one. |
| **call ledger** | The local record of every live call the test bed made, used to enforce the budget. |

## 2. Switching the test bed on

One environment variable, `PENGUINCAM_TESTBED`, set to `replay` or `record`. It is read
when `frc_cam_gui_app` is imported, through `testbed/settings.py`, which every test bed
setting goes through and which imports only the standard library.

- **Unset** (production, and ordinary development): no test bed route, adapter, template
  flag or script is loaded. The server behaves exactly as it does without the test bed.
- **`replay`**: every Onshape request, on any host, is answered by the replay adapter from
  the current scenario's cassette. Nothing leaves the machine. An unmatched request gets
  HTTP 599 with a body starting `not in the cassette: ` that names the method, host and
  path. `/testbed/sign-in` (the development sign-in) exists only in this mode.
- **`record`**: every Onshape request goes to real Onshape through the recorder, which
  scrubs it and counts it in the call ledger.

The app refuses to import with `PENGUINCAM_TESTBED` set when the server looks deployed
(`FLASK_ENV=production`, `RAILWAY_ENVIRONMENT` or `VERCEL` set). In record mode it also
refuses without `ONSHAPE_CLIENT_ID` and `ONSHAPE_CLIENT_SECRET`, or with the production
app's client id, so a recording never runs through the production app.

The `/testbed/*` routes are exempt from the app's rate limiter: they exist only on a
development server with the test bed on, and browser runs call them in bursts.

## 3. Makefile targets and the command line

| Target | What it does | Live calls |
|---|---|---|
| `make test-quick`, `make test` | Include the test bed's unit tests (`testbed/tests/test_*.py`). `make test` also slices `tb-box`'s exported mesh (the fixture until `part-export` is recorded) with Orca. | none |
| `make testbed-replay` | Installs `testbed/requirements.txt` into the development environment if Playwright is missing, then runs the browser tests (`testbed/tests/browser_*.py`) under `xvfb-run`: the harness self-tests, every browser scenario in replay mode, and the offline launch of the Chromium the automated Onshape UI run drives. | none |
| `make testbed-record` | The live API check: `python -m testbed record`. | yes, counted |

`make testbed-replay` must pass before a change to the print path's Onshape code is done.
A browser scenario with no recording yet is skipped with "needs a recording", never passed.

Playwright is pinned in `testbed/requirements.txt` (`playwright==1.63.0`). It is a
development dependency only: it is never in `requirements.txt` or the Docker image, and the
test bed never runs `playwright install` or downloads a browser.

The command line is `uv run python -m testbed <command>`:

| Command | What it does |
|---|---|
| `build-docs [--rebuild NAME]` | Builds the test documents that are missing from the test folder (live, counted). `--rebuild NAME` deletes and rebuilds one. |
| `record [SCENARIO...]` | The live API check for the scenarios without a panel part, then the drift report. |
| `record --ui [SCENARIO...]` | The automated Onshape UI run for the scenarios with a panel part, then the drift report. |
| `drift` | Compares every fresh recording in `testbed/.fresh/` with the stored one and writes `testbed/.fresh/drift-report.md`. |
| `accept SCENARIO...` | Scans every fresh file of the scenario for secrets and identity leaks (the scan the clean test runs) and, only if none is found, moves the fresh cassette, its binary files and its message log over the stored ones. A hit refuses the scenario, moves nothing and names each `file:line` and kind, never the value. Commit them with a message that says why the recording changed. |
| `ledger` | The call ledger's counted calls in the current cycle, the budget, and its last ten lines. |

Exit codes: 0 on success; 1 for any other failure; 2 for a missing environment variable,
which the message names; 3 when the automated Onshape UI run was stopped by an Onshape
check; 4 when the budget would be passed, a ledger line is unreadable, or Onshape answered
402.

Every live command ends by printing `counted calls: <n>, remaining budget: <m>`, whatever
stopped it.

## 4. Environment variables

Credentials are read only from the environment. A command that needs a missing one stops
and names it; none is ever written to a file or a log.

| Variable | Used by | Meaning |
|---|---|---|
| `PENGUINCAM_TESTBED` | the app | `replay` or `record`; unset means off (section 2). |
| `ONSHAPE_ACCESS_KEY`, `ONSHAPE_SECRET_KEY` | `build-docs`, `record` | The API key pair. Replay mode needs neither. |
| `ONSHAPE_CLIENT_ID`, `ONSHAPE_CLIENT_SECRET` | record mode, `record --ui` | A private development OAuth app whose redirect URL is `http://localhost:6238/onshape/oauth/callback`. Never the production app. |
| `ONSHAPE_USERNAME`, `ONSHAPE_PASSWORD` | `record --ui` | The Onshape sign-in. Only the Chromium the automated Onshape UI run drives gets them; the development server the automated Onshape UI run starts does not. |
| `TESTBED_FOLDER_ID` | `build-docs` | The test folder's id: the last segment of the folder's URL in Onshape. |
| `TESTBED_BUDGET` | every live command | Counted calls the test bed may spend per cycle. Default 250. |
| `TESTBED_CYCLE_START` | every live command | The allowance's cycle start, `YYYY-MM-DD`. Default: a rolling year ending today. |
| `TESTBED_RUN_CAP` | every live command | Counted calls one command may spend. Default 150. |
| `TESTBED_CHROMIUM` | `make testbed-replay` | The Chromium the browser scenarios drive. Default `/usr/bin/chromium`. |
| `TESTBED_CASSETTE_DIR`, `TESTBED_MESSAGE_DIR` | replay development server | Other recordings to replay; the harness self-tests point them at synthetic fixtures. |
| `TESTBED_SYNTHETIC_SCENARIOS` | replay development server | A JSON file naming the scenario copies (each `synthetic-<name>`) a self-test runs on its synthetic fixtures. Set only by the self-tests; the server rejects any name without the `synthetic-` prefix. |
| `TESTBED_ONLINE` | `browser_chrome.py` | `1` runs the one browser test that loads Onshape's sign-in page (it never fills the form). |
| `XDG_STATE_HOME` | everything | The state directory is `$XDG_STATE_HOME/penguincam-testbed`, default `~/.local/state/penguincam-testbed`. |

The state directory, outside the repository and outside any Docker build context, holds
the call ledger, the development servers' logs (`logs/`), the automated Onshape UI run's Chrome profile
(`chrome-profile/`) and its screenshots (`screenshots/`).

## 5. The budget and the call ledger

The call ledger is `ledger.jsonl` in the state directory. It has one line per live
exchange, with each redirect hop and each retry attempt separately: time (UTC), scenario,
method, path, status, kind (`response` or `retry`), and whether Onshape counts it. Onshape
counts a 2xx or 3xx response; a retry attempt line is kept but not counted, because the
response that ends the retry chain has its own line. It covers the API keys and the
development app alike.

- **Before it starts**, every live command adds its estimate to the calls counted since
  the cycle start. It refuses if that would pass `TESTBED_BUDGET`, or if the estimate alone
  would pass `TESTBED_RUN_CAP`. A scenario's estimate is its last recording's counted calls
  (plus the two account lookups below), or the estimate in its definition before its first
  recording.
- **A bad ledger line** makes the command refuse, naming the line, rather than undercount.
- **A 402** (the annual allowance is used up) latches in the process: no further live call
  is sent, the scenario's partial recording is discarded, and the command exits 4. There
  is no retry.
- **429s** are retried by the client's existing retry adapter, which honours
  `Retry-After`, and each attempt is in the ledger.
- **The ledger is per machine.** Onshape's own usage page (My Account, Developer) stays the
  authority; calls made on another machine, or by ordinary use of the development app, are
  not in this ledger. That is why the budget is a share of the allowance with headroom.

At the start of each recording the recorder fetches `/users/sessioninfo` and `/companies`
once (the account lookups, `OWNER_LOOKUPS` in `testbed/adapters.py`), to learn which names
and ids to replace with stand-ins. They are counted too.

## 6. Test documents

The test folder is created once by hand in Onshape: the public API has no endpoint that
creates a folder. Its id goes in `TESTBED_FOLDER_ID`. `build-docs` then builds:

| Document | Contents | Why |
|---|---|---|
| `tb-box` | One 40 x 30 x 10 mm box, with a saved version | The plain case; `part-export` also exports it from the version. |
| `tb-two-parts` | A box and a cylinder in one Part Studio | Choosing among several parts; a curved surface in the mesh. |
| `tb-inch` | A 2 x 1 x 0.5 in block, workspace units inches | Unit conversion. |
| `tb-sideways` | A tall thin part modelled lying on its side | Orientation on the plate. |
| `tb-oversized` | A 400 x 50 x 10 mm slab | Larger than the printer's plate. |
| `tb-assembly` | An assembly placing two instances of `tb-box` | Parts reached through an assembly. |
| `tb-configured` | A 20 x 20 mm spacer whose list input Size sets its depth: Small 10 mm, Large 20 mm | A configured Part Studio: the print path passes the configuration through, or refuses a click it cannot configure. `part-export` leaves it out. |

- **Ids.** The document, workspace, element and part ids go into `testbed/documents.json`,
  keyed by the `tb-` name, which the scenarios read.
- **Rules for writing.** Before any write, the builder checks that the target is a test
  document: it is listed in the test folder, its name starts with `tb-`, and its
  description is exactly `created by PenguinCAM test bed`. It never deletes a document
  without all three.
- **Re-running is safe.** The builder lists the folder, keeps the documents that exist and
  builds only what is missing. `--rebuild tb-box` makes a new version, so rebuild
  `tb-assembly` next; the command says so.
- **A document created outside the folder.** If `POST /documents` with the folder as
  `parentId` does not place the new document in the folder, the builder writes nothing to
  it. Its `documents.json` entry is marked `pending: not in the test folder`, and
  `build-docs` prints its id and exits 1. Move it into the test folder in Onshape and run
  `build-docs` again, which builds it. `record --ui` refuses to start while any entry is
  pending.
- **Units.** No API endpoint sets a document's units. The first automated Onshape UI run
  switches `tb-inch`'s workspace units to inches in Onshape's interface, opens the dialog
  again to check that they read inches, and only then writes `tb-inch-units.txt` in the
  state directory so later runs skip the step. If it cannot, it stops with a screenshot
  and says how to do it by hand.
- **Cost.** A full build is about 48 counted calls (`BUILD_DOCS_ESTIMATE` in
  `testbed/documents.py`, which `build-docs` checks against the budget before it starts).

## 7. Recording, scrubbing and replay

The recorder is installed in `OnshapeClient.__init__` when the test bed is on, because the
server builds a new client for every request. It saves each exchange (each redirect hop
separately) and writes the scenario's cassette to `testbed/.fresh/cassettes/` when the
scenario ends: Done in the checklist strip, or the end of a command-line run. Binary bodies
(exported STL files) are stored as files next to the cassette.

The repository is public, so everything is scrubbed before it reaches disk:

- **Removed:** `Authorization` headers in bearer and Basic form, cookies, OAuth tokens, API
  keys, and the whole query of any exchange whose host is not `*.onshape.com` (signed
  storage URLs).
- **Replaced with stand-ins, in requests and responses alike:** the account's user id
  (`000000000000000000000001`), name (`Test Bed Owner`, also first and last name) and email
  (`testbed@example.invalid`); company and classroom ids (`0000000000000000000000c1`,
  `…c2`, …) and their names (`Test Bed Classroom 1`, `Test Bed Classroom 2`, …). The
  stand-ins are constants in `testbed/scrub.py`.
- **Replaced with a fixture:** the body of every `/blobelements/` download, which is how
  the team config file is read; the fixture is `testbed/fixtures/team-config-fixture.yaml`.
- **Checked:** `test_committed_recordings_are_clean` fails `make test-quick` if a committed
  cassette or message log holds anything shaped like a credential, a token, an email
  address, a signed URL, or a user id or name that is not a stand-in. It names the file and
  line.

The replay adapter matches a request on method, host, full path, and query and body with
volatile fields removed. A recorded exchange is answered once; when a request's answers are
used up the last one is repeated, so a reload still gets an answer. Polls of an
asynchronous translation replay strictly in recorded order, and a poll past the recorded
ones gets the 599.

The panel's messages are recorded by the checklist strip on the print page: it records
every message it sends and, before any of the panel's own filters, every message the panel
receives, and posts them to the development server, which saves the message log to
`testbed/.fresh/messages/`. The CNC page is not touched.

The print page does its own Onshape messaging (`print/static/print_onshape.js`), so the
strip defers to it: it sends no `applicationInit` of its own and hides its ask and dialog
buttons. The page's own messages are recorded on their way out through a stand-in for
`window.parent` (the HTML standard makes that property replaceable), so the fake Onshape
host can match the page's requests when the scenario is replayed. A received message is
recorded inside the page's own handler, which listens before the strip loads, so it stays
ahead of the request the page sends in answer to it. The page's `applicationInit` goes out
before the strip loads, so a print page message log never holds it; replay is unaffected,
since the replayed page's is not recorded either.

## 8. The automated Onshape UI run and the Onshape checkpoint

`record --ui` runs the scenarios with a panel part inside real Onshape:

1. It starts the development server in record mode on 6238 with `EMBED_COOKIES=1`. Its log
   is `logs/record-devserver.log` in the state directory.
2. It launches Google Chrome headed (under Xvfb on a machine without a display) on the
   profile kept in `chrome-profile/`, so Onshape sees a returning browser and the session
   survives between runs. The profile's password manager is turned off before every
   launch, so Chrome never offers to save the password. It grants `cad.onshape.com` the
   local network permission (section 10).
3. It signs in with `ONSHAPE_USERNAME` and `ONSHAPE_PASSWORD`, unless the kept session is
   still signed in.
4. It switches `tb-inch`'s units on the first run (section 6), then, for each scenario,
   opens the test document, opens the development app's panel, connects through OAuth in
   the panel's popup when asked, chooses 3D Printing, and works through the checklist
   strip. Selections are made in Onshape's parts list or select dialog, never in the 3D
   view.
5. It presses Done, writes the drift report, and prints the panel URL the development app
   opened (also kept in `panel-url.txt`) and the counted calls.

Rules for the run:

- **Pace.** It waits for each page to settle and acts at human speed.
- **Bot checks.** On a CAPTCHA, a challenge page, an unexpected verification step, or any
  other sign that Onshape is asking whether this is a person, it stops, saves a screenshot
  and exits 3. It never tries to solve, bypass or hide from such a check.
- **A failed step** stops the run with a screenshot and a message naming the step; exit 1.
  Every Onshape page selector is in `SELECTORS` in `testbed/ui_run.py`, so a change in
  Onshape's interface is a one-line fix there.
- **Secrets.** The password goes only into Onshape's sign-in form. Every message the run
  prints is redacted, error text from Playwright is cut to its first line, and nothing is
  chained to the error that leaves the run. Never set Playwright's `DEBUG=pw:api` for this
  run: Playwright would then print filled values itself. Screenshots stay in the state
  directory, never in the repository, because they can show the account's name and email.

**Google Chrome.** `testbed/scripts/install-chrome.sh` installs Google Chrome for ARM64
Linux without root, by unpacking Google's `.deb` into `~/.local/opt/google-chrome/`, and
lists any missing libraries. If Chrome is missing or does not start, the run falls back to
`/usr/bin/chromium` and says so. Both run with the sandbox off, which suits a machine
without user namespaces; the run visits only `cad.onshape.com` and the development server.

**The Onshape checkpoint** does the same job by hand, when a bot check stops the
automated Onshape UI run or someone wants to watch the panel. With the development server
started in record mode, the person opens the test document the checklist strip names in
Chrome, opens the development app's panel, connects, chooses 3D Printing, works through
the checklist strip's steps, and presses Done. Then `python -m testbed drift` writes the
drift report. The recordings are the same as from an automated Onshape UI run.

## 9. The drift report

`drift` (and the end of `record` and `record --ui`) compares each fresh recording with the
stored one, scenario by scenario:

- **API traffic:** the same requests in the same order, the same statuses, and response
  bodies of the same structure (keys and value types). Volatile values (ids, timestamps,
  microversions) compare by type only. Exported meshes compare by bounding box and triangle
  count. Calls that only the OAuth sign-in makes are ignored when they appear in one
  recording only.
- **Message logs:** the same message names, fields and value types.

Each difference says whether the request differs (PenguinCAM changed) or only the response
does (Onshape changed). A clean report means the stored recordings still describe real
Onshape. A fresh recording replaces the stored one only through `accept`.

## 10. The fake Onshape host

Three checks stand between a test page and the panel, and the test bed passes each of them
without loosening any:

- **The panel's message listener** drops messages whose origin is not `*.onshape.com` on
  the default port (`static/source_onshape.js`).
- **The panel's `Content-Security-Policy`** allows framing only by `https://*.onshape.com`,
  on the CNC panel and the print page alike.
- **Chrome's Local Network Access check** (since Chrome 142) blocks a public `https` page
  from framing `http://localhost` until the user grants a permission.

So the fake Onshape host is served to the test browser at
`https://cad-testbed.onshape.com`, always on the default port. The test browser answers
that host's requests itself through Playwright request routing; the name does not resolve
in DNS, so a request that escaped the routing would fail rather than reach anything. Every
other `*.onshape.com` request is refused inside the browser. The browser grants the fake
origin Chrome's local network permission, under Playwright's name `local-network-access`
(Playwright 1.58 and later), with a fallback that grants Chrome's own `localNetwork` and
`loopbackNetwork` through the browser protocol. Without the grant, the frame does not load.

Experiments on an aarch64 Linux machine (Chromium 151; Playwright 1.58 and 1.63) confirmed
that this gives the page a real `https://cad-testbed.onshape.com` origin, passes the panel's
`frame-ancestors` check, carries messages both ways, and keeps the panel's
`SameSite=None; Secure` session cookie across reloads. Because the production checks stay
exactly as they are, a scenario that passes on the fake Onshape host has passed the real
checks.

The fake Onshape host embeds `/onshape-panel` with the query parameters Onshape sends,
lets the panel navigate on to `/print` in the same iframe, and answers the panel's messages
from the scenario's message log, matching on the message's name and keys, not on its
`messageId`. Its answers come only from message logs recorded in real Onshape, so a browser
scenario has nothing to replay until an automated Onshape UI run or an Onshape checkpoint
has recorded it.

The browser scenarios drive `/usr/bin/chromium` by default. Set `TESTBED_CHROMIUM` to use
another Chromium or Chrome binary.

## 11. Adding a scenario

1. **Define it** in `testbed/scenarios/__init__.py`: add a `Scenario(name, document,
   panel, steps, estimate)` to `SCENARIOS`. The name is lowercase with hyphens; it names the
   scenario's cassette and message log files, so renaming a scenario renames both in the
   same commit. `document` is a test document's `tb-` name. `estimate` is the counted calls
   a first recording may make, for the budget.
2. **Write its steps.** Each `Step` has a name, the instruction the checklist strip shows,
   and optionally an action (one of `ACTIONS`): the strip's button, or for `print-parts` and
   `print-dialog` the print page's Next and Add from another document… A selection step is
   named `select part <name>` or `select parts <a>, <b>` (use `_select`), because the fake
   Onshape host finds a selection's answer by the step's name. A step with
   `recorded_only=True` is recorded for reference and skipped in replay.
3. **For a scenario with a panel part**, nothing else is needed: `browser_scenarios.py`
   adds a test for every panel scenario, which is skipped until the scenario is recorded.
   Record it with `record --ui <name>` (or an Onshape checkpoint), read the drift report,
   and `accept <name>`.
4. **For an API-only scenario**, add its calls to `run_scenario_api` in the same module,
   through the client's existing `_make_api_request` (or, as `print-export` does, through
   `print/onshape_parts.py`). Record it with `record <name>` and
   `accept <name>`.
5. **Commit** the cassette, any binary files next to it, and the message log. The scrub
   test checks them before the commit can pass `make test-quick`.

## 12. Where the files live

| Path | What it holds |
|---|---|
| `testbed/settings.py` | The switch, the import guards and every test bed setting. |
| `testbed/__main__.py` | The command line. |
| `testbed/documents.py` | The test documents builder. |
| `testbed/scrub.py`, `testbed/cassette.py` | The recording format, scrubbing and the stand-ins. |
| `testbed/adapters.py` | The recorder, the replay adapter and the current scenario. |
| `testbed/ledger.py` | The call ledger and the budget. |
| `testbed/flask_hooks.py`, `testbed/messages.py` | The `/testbed/*` routes and the message log format. |
| `testbed/panel/` | The checklist strip and its message recording script. |
| `testbed/host/` | The fake Onshape host's page and script. |
| `testbed/browser.py` | The Playwright harness and the development servers it starts. |
| `testbed/scenarios/` | Scenario definitions and the part export runner. |
| `testbed/mesh.py`, `testbed/drift.py` | Exported mesh checks and the drift report. |
| `testbed/ui_run.py` | The automated Onshape UI run. |
| `testbed/scripts/install-chrome.sh` | Installs Google Chrome without root. |
| `testbed/cassettes/`, `testbed/messages/` | The committed recordings. |
| `testbed/documents.json` | The test documents' ids. |
| `testbed/.fresh/` | Fresh recordings and the drift report; git-ignored. |
| `testbed/tests/` | Unit tests (`test_*.py`), browser tests (`browser_*.py`) and fixtures. |

Outside `testbed/`, all inert unless `PENGUINCAM_TESTBED` is set: `OnshapeClient.__init__`,
`get_client` and `update_session_tokens` in `onshape_integration.py`; the import guards, the
route registration and `/onshape/status` in `frc_cam_gui_app.py`; and one conditional
`<script>` tag in `print/templates/print_wizard.html`.
