.PHONY: venv install test run bench tui tui-run clean

VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

# Create an isolated virtualenv and install the project (with dev deps) into it.
# Keeps coding-agent's pinned deps (e.g. httpx>=0.27) out of any shared/global env.
venv:
	python3 -m venv $(VENV)
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -e ".[dev]"

# Alias
install: venv

# Run the test suite inside the venv.
test:
	$(PY) -m pytest -q

# Launch the interactive agent (needs API key env vars; see README).
run:
	$(VENV)/bin/coding-agent

# Run the benchmark suite (needs API key env vars).
bench:
	$(PY) benchmarks/benchmark.py

# Build the full-screen Rust TUI (needs cargo).
tui:
	cd tui && cargo build --release

# Build + launch the Rust TUI, pointing it at this venv's python.
tui-run: tui
	CODING_AGENT_PYTHON=$(VENV)/bin/python CODING_AGENT_DIR=. ./tui/target/release/coding-agent-tui

clean:
	rm -rf $(VENV) .pytest_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
	cd tui 2>/dev/null && cargo clean 2>/dev/null || true
