.PHONY: install install-orca test test-quick test-daemon daemon-tarball

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

# Everything except the slow slicer run; the tool for iteration.
test-quick: test-daemon
	@echo "Running unit tests..."
	@uv run python -m unittest discover -s tests --buffer
	@echo ""
	@echo "Running print unit tests..."
	@uv run python -m unittest discover -s print/tests -t . --buffer
	@echo ""
	@echo "Running system tests..."
	@uv run python gcode_test.py --quiet

# The Pi daemon's own suite. Its requirements go into the development venv only -- they are
# NOT in the repository's requirements.txt and must never reach the Docker image.
test-daemon:
	@echo "Running printer daemon tests..."
	@uv run python -c "import bambulabs_api, tomli_w, requests" >/dev/null 2>&1 || \
		uv pip install -r print/printer_daemon/requirements.txt
	@uv run python -m unittest discover -s print/printer_daemon/tests -t print --buffer

# The daemon alone, as one archive, for running it on a laptop against a development
# server (docs/PRINTER_DEV_TESTING.md). Staged through a directory rather than tar's
# --transform, so the same target works with the Mac's bsdtar. dist/ is git-ignored.
DAEMON_TARBALL ?= dist/penguincam-printer-daemon.tar.gz
daemon-tarball:
	@rm -rf dist/penguincam-printer-daemon
	@mkdir -p dist/penguincam-printer-daemon/docs
	@cp -R print/printer_daemon dist/penguincam-printer-daemon/
	@cp docs/PRINTER_DEV_TESTING.md dist/penguincam-printer-daemon/docs/
	@find dist/penguincam-printer-daemon -name __pycache__ -type d -prune -exec rm -rf {} +
	@tar -C dist -czf $(DAEMON_TARBALL) penguincam-printer-daemon
	@rm -rf dist/penguincam-printer-daemon
	@echo "wrote $(DAEMON_TARBALL)"
