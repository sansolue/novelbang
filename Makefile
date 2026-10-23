# Common tasks. `make` on its own lists them.
# Needs uv (https://docs.astral.sh/uv/). Works with the GNU Make 3.81 that comes with macOS.

SHELL := /bin/bash
.DEFAULT_GOAL := help

VERSION_NOW = $(shell uv run --no-project python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
DEV_WORKSPACE ?= /tmp/novelbang-dev
PORT ?= 8765

.PHONY: help setup run dev desktop test app icon log version bump release clean

help: ## List the tasks
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{ printf "  make %-9s %s\n", $$1, $$2 }'
	@echo
	@echo "  Options: make run ARGS='~/Writing --port 9000'   make bump VERSION=0.2.0"

setup: ## Install everything: the app, the desktop window, tests and build tools
	uv sync --extra desktop --group build

run: ## Start NovelBang in the browser (ARGS are passed to start.sh)
	./start.sh $(ARGS)

dev: ## Run on a scratch folder with debug logging, no browser (PORT=8765)
	NOVELBANG_DEBUG=1 NOVELBANG_NO_OLLAMA=1 uv run novelbang $(DEV_WORKSPACE) --no-browser --port $(PORT)

desktop: ## Run the desktop app from source
	uv run --extra desktop novelbang-desktop

test: ## Run the tests
	uv run pytest -q

app: ## Build the desktop app for this computer into dist/
ifeq ($(OS),Windows_NT)
	pwsh packaging/build-windows.ps1
else ifeq ($(shell uname -s),Darwin)
	packaging/build-macos.sh
else
	@echo "The desktop app builds on macOS and Windows only" >&2; exit 1
endif

icon: ## Redraw packaging/icon.png
	uv run --group build python packaging/make_icon.py

log: ## Follow the debug log
	@log=$$(uv run python -c 'from novelbang.logs import log_path; print(log_path())'); \
	  echo "$$log"; touch "$$log" 2>/dev/null; tail -n 40 -f "$$log"

version: ## Show the current version
	@echo $(VERSION_NOW)

bump: ## Set a new version and commit it: make bump VERSION=0.2.0
	@[[ "$(VERSION)" =~ ^[0-9]+(\.[0-9]+)*((a|b|rc|dev)[0-9]+)?$$ ]] || { echo "Usage: make bump VERSION=0.2.0" >&2; exit 1; }
	@git diff --quiet && git diff --cached --quiet || { echo "Commit or stash your changes first" >&2; exit 1; }
	uv run --no-project python -c 'import re, sys; from pathlib import Path; v = sys.argv[1]; \
	  [Path(p).write_text(re.sub(pat, rep, Path(p).read_text(), count=1, flags=re.M)) for p, pat, rep in ( \
	    ("pyproject.toml", r"^version = \".*\"", f"version = \"{v}\""), \
	    ("src/novelbang/__init__.py", r"^__version__ = \".*\"", f"__version__ = \"{v}\""))]' "$(VERSION)"
	uv lock -q
	git commit -q -m "Release $(VERSION)" pyproject.toml src/novelbang/__init__.py uv.lock
	@echo "Version $(VERSION) committed. 'make release' tags it and starts the release build."

release: test ## Tag the current version and push it, which builds and publishes the release
	@git diff --quiet && git diff --cached --quiet || { echo "Commit or stash your changes first" >&2; exit 1; }
	@[[ "$$(git branch --show-current)" == master ]] || { echo "Release from master" >&2; exit 1; }
	@! git rev-parse -q --verify "refs/tags/v$(VERSION_NOW)" >/dev/null || { echo "v$(VERSION_NOW) is already tagged: 'make bump VERSION=…' first" >&2; exit 1; }
	@read -r -p "Tag v$(VERSION_NOW), push master and the tag, and publish the release? [y/N] " ok && [[ "$$ok" == [yY] ]]
	git push origin master
	git tag -a "v$(VERSION_NOW)" -m "NovelBang $(VERSION_NOW)"
	git push origin "v$(VERSION_NOW)"
	@echo "Release build started: https://github.com/sansolue/novelbang/actions"

clean: ## Remove build output and caches
	rm -rf build dist .pytest_cache
	find . -name __pycache__ -type d -prune -not -path './.venv/*' -exec rm -rf {} +
