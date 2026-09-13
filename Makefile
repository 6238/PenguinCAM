.PHONY: install install-orca test test-quick

# Installs the Python environment and Orca Slicer (tools/orca, pinned in
# scripts/install-orca.sh). Orca is a core dependency: the server refuses to
# start without it.
install: install-orca
	@command -v uv >/dev/null 2>&1 || { echo "Installing uv..."; curl -LsSf https://astral.sh/uv/install.sh | sh; }
	@echo "Installing dependencies from requirements.txt..."
	uv pip install -r requirements.txt

install-orca:
	scripts/install-orca.sh

# Full run: everything in test-quick plus the real Orca slice. This is the
# gate before a change is done.
test: test-quick
	@echo ""
	@echo "Running Orca Slicer integration tests..."
	@uv run python -m unittest tests.orca_integration_test

# Everything except the slow slicer run; the tool for iteration.
test-quick:
	@echo "Running unit tests..."
	@uv run python -m unittest discover -s tests --buffer
	@echo ""
	@echo "Running system tests..."
	@uv run python gcode_test.py --quiet
