UV := $(HOME)/.local/bin/uv

.PHONY: install serve test lint clean

## Install uv and project dependencies
install: $(UV)
	cd backend && $(UV) sync --frozen

$(UV):
	curl -LsSf https://astral.sh/uv/install.sh | sh

## Start the app locally (backend serves frontend on port 8000)
serve: install
	cd backend && $(UV) run uvicorn server:app --reload --host 0.0.0.0 --port 8000

## Run backend tests
test: install
	cd backend && $(UV) run pytest

## Run all linters
lint: install
	cd backend && $(UV) run ruff check .
	cd frontend && npx eslint .

## Remove caches and virtual environment
clean:
	rm -rf backend/.venv backend/__pycache__ backend/cache/*
