# Makefile for flowjet (FlowJet) — unified agent CLI + server
UV_RUN ?= uv run
UV_INDEX_URL ?= https://pypi.org/simple
HOST ?= 0.0.0.0
PORT ?= 8618
export UV_INDEX_URL

.PHONY: sync sync-dev format format-check lint lint-fix autofix \
	test test-unit test-integration test-server test-http test-acp test-coverage \
	run serve examples examples-sdk examples-modes examples-http examples-acp examples-e2e \
	build publish clean help

help:
	@echo "flowjet (FlowJet / fj) — agent CLI + server"
	@echo ""
	@echo "Setup:"
	@echo "  make sync            - Sync dependencies (server extras)"
	@echo "  make sync-dev        - Sync with dev + server extras"
	@echo ""
	@echo "Code quality:"
	@echo "  make format          - Format with ruff"
	@echo "  make format-check    - Check formatting (CI)"
	@echo "  make lint            - Lint with ruff"
	@echo "  make lint-fix        - Auto-fix lint issues"
	@echo "  make autofix         - Format + auto-fix lint issues"
	@echo ""
	@echo "Testing:"
	@echo "  make test            - Run unit + integration + server tests"
	@echo "  make test-unit       - Run unit tests"
	@echo "  make test-integration - Run integration tests"
	@echo "  make test-server     - Run server + HTTP + ACP tests"
	@echo "  make test-http       - Run HTTP integration tests"
	@echo "  make test-acp        - Run ACP integration tests"
	@echo "  make test-coverage   - Tests with coverage report"
	@echo ""
	@echo "Server:"
	@echo "  make run             - Start the server (HOST=0.0.0.0 PORT=8618)"
	@echo "  make serve           - Alias for 'make run'"
	@echo ""
	@echo "Examples (start server first with 'make run'):"
	@echo "  make examples        - List available example commands"
	@echo "  make examples-sdk    - OpenAI SDK end-to-end"
	@echo "  make examples-modes  - Ask vs Agent interaction modes"
	@echo "  make examples-http   - Raw HTTP end-to-end"
	@echo "  make examples-acp    - ACP WebSocket end-to-end"
	@echo "  make examples-e2e    - Run SDK + modes + HTTP + ACP"
	@echo ""
	@echo "Packaging:"
	@echo "  make build           - Build dist/ (wheel + sdist)"
	@echo "  make publish         - Build and publish to PyPI"
	@echo "  make clean           - Remove build artifacts and caches"
	@echo ""
	@echo "Options (override with VAR=value):"
	@echo "  HOST                 - Server bind address (default: 0.0.0.0)"
	@echo "  PORT                 - Server port (default: 8618)"
	@echo "  UV_RUN               - Python runner (default: uv run)"

sync:
	uv sync --extra server

sync-dev:
	uv sync --extra dev --extra server

format:
	$(UV_RUN) ruff format src/ tests/

format-check:
	$(UV_RUN) ruff format --check src/ tests/

lint:
	$(UV_RUN) ruff check src/ tests/

lint-fix:
	$(UV_RUN) ruff check --fix src/ tests/

autofix: format lint-fix

test-unit:
	$(UV_RUN) python -m pytest tests/unit/ -q

test-integration:
	$(UV_RUN) python -m pytest tests/integration/ -q -m integration

test-server:
	$(UV_RUN) python -m pytest tests/unit/server/ tests/integration/http/ tests/integration/acp/ -q

test-http:
	$(UV_RUN) python -m pytest tests/integration/http/ -q

test-acp:
	$(UV_RUN) python -m pytest tests/integration/acp/ -q

test: test-unit test-integration test-server

test-coverage:
	$(UV_RUN) python -m pytest tests/unit/ tests/integration/ \
		--cov=flowjet --cov-report=term-missing --cov-report=xml

# --- server ---------------------------------------------------------------

run serve:
	FLOWJET_HOST=$(HOST) FLOWJET_PORT=$(PORT) $(UV_RUN) flowjet-server

examples:
	@echo "Start the server in another terminal:  make sync-dev && make run"
	@echo "Then:"
	@echo "  make examples-sdk     # OpenAI SDK end-to-end"
	@echo "  make examples-modes   # Ask vs Agent interaction modes (real nano)"
	@echo "  make examples-http    # Raw HTTP end-to-end"
	@echo "  make examples-acp     # ACP WebSocket end-to-end"
	@echo "  make examples-e2e     # Run SDK + HTTP + ACP"
	@echo "See examples/README.md"

examples-sdk:
	$(UV_RUN) python examples/e2e_openai_sdk.py

examples-modes:
	$(UV_RUN) python examples/e2e_ask_agent_modes.py

examples-http:
	$(UV_RUN) python examples/e2e_http_api.py

examples-acp:
	$(UV_RUN) python examples/e2e_acp_websocket.py

examples-e2e: examples-sdk examples-modes examples-http examples-acp

build:
	rm -rf dist/
	uv build

publish: build
	uv publish

clean:
	rm -rf dist/ build/ .pytest_cache/ .ruff_cache/ .mypy_cache/ htmlcov/ coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
