.PHONY: install install-orca test test-quick test-daemon daemon-tarball testbed-replay testbed-record testbed-local

# Installs the Python environment and Orca Slicer (tools/orca, pinned in
# print/scripts/install-orca.sh). Orca is a core dependency: the server refuses to
# start without it.
install: install-orca
	@command -v uv >/dev/null 2>&1 || { echo "Installing uv..."; curl -LsSf https://astral.sh/uv/install.sh | sh; }
	@echo "Installing dependencies from requirements.txt..."
	uv pip install -r requirements.txt

install-orca:
	print/scripts/install-orca.sh

# Full run: everything in test-quick plus the real Orca slice. This is the
# gate before a change is done.
test: test-quick
	@echo ""
	@echo "Running Orca Slicer integration tests..."
	@uv run python -m unittest print.tests.orca_integration_test
	@uv run python -m unittest testbed.tests.slice_fixture_box

# Everything except the slow slicer run; the tool for iteration.
test-quick: test-daemon
	@echo "Running unit tests..."
	@PENGUINCAM_STRICT_GCODE=1 uv run python -m unittest discover -s tests -t . --buffer
	@echo ""
	@echo "Running print unit tests..."
	@uv run python -m unittest discover -s print/tests -t . --buffer
	@echo ""
	@echo "Running test bed unit tests..."
	@uv run python -m unittest discover -s testbed/tests -t . --buffer
	@echo ""
	@echo "Running system tests..."
	@PENGUINCAM_STRICT_GCODE=1 uv run python gcode_test.py --quiet

# The Pi daemon's own suite. Its requirements go into the development venv only -- they are
# NOT in the repository's requirements.txt and must never reach the Docker image.
test-daemon:
	@echo "Running printer daemon tests..."
	@uv run python -c "import bambulabs_api, tomli_w, requests" >/dev/null 2>&1 || \
		uv pip install -r print/printer_daemon/requirements.txt
	@uv run python -m unittest discover -s print/printer_daemon/tests -t print --buffer

# The daemon alone, as one archive, for running it on a laptop against a development
# server. Staged through a directory rather than tar's --transform, so the target works
# with BSD tar as well as GNU tar. dist/ is git-ignored.
DAEMON_TARBALL ?= dist/penguincam-printer-daemon.tar.gz
daemon-tarball:
	@rm -rf dist/penguincam-printer-daemon
	@mkdir -p dist/penguincam-printer-daemon
	@cp -R print/printer_daemon dist/penguincam-printer-daemon/
	@find dist/penguincam-printer-daemon -name __pycache__ -type d -prune -exec rm -rf {} +
	@tar -C dist -czf $(DAEMON_TARBALL) penguincam-printer-daemon
	@rm -rf dist/penguincam-printer-daemon
	@echo "wrote $(DAEMON_TARBALL)"

# The browser scenarios in replay mode: a replay development server on 6240 and a test
# browser on the fake Onshape host, under Xvfb. No live Onshape call. Playwright comes
# from testbed/requirements.txt, never requirements.txt; it drives the system Chromium
# (TESTBED_CHROMIUM, default /usr/bin/chromium) and never downloads a browser.
testbed-replay:
	@uv run python -c "import playwright" >/dev/null 2>&1 || uv pip install -r testbed/requirements.txt
	@xvfb-run -a uv run python -m unittest discover -s testbed/tests -p 'browser_*.py' -t . -v

# The live API check against real Onshape: records part-export with ONSHAPE_ACCESS_KEY and
# ONSHAPE_SECRET_KEY, writes every call to the call ledger, then writes the drift report.
# It checks the budget first and is never part of make test.
testbed-record:
	@uv run python -m testbed record

# Local mode: the development server on 6238 with a stand-in Onshape Part Studio at
# http://localhost:6238/testbed/local, made of uploaded STL files. No Onshape account, no
# credentials and no call to Onshape. Runs in the foreground until stopped.
testbed-local:
	PORT=6238 EMBED_COOKIES=1 PENGUINCAM_TESTBED=local uv run python frc_cam_gui_app.py
