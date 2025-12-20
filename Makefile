.PHONY: install test

install:
	@command -v uv >/dev/null 2>&1 || { echo "Installing uv..."; curl -LsSf https://astral.sh/uv/install.sh | sh; }
	uv sync --all-extras

test:
	@echo "Running unit tests..."
	@uv run python -m unittest discover -s tests --buffer
	@echo ""
	@echo "Running system tests..."
	@uv run python gcode_test.py --quiet
