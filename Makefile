.PHONY: help install lint type-check test plugin-sync version-sync max-loc wheel-assets wheel-install diagrams plugin-sync-copy quality check act-dry act-ci

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

install: ## Install dependencies
	uv sync --all-extras

lint: ## Run ruff format + lint
	uv run ruff format --check .
	uv run ruff check .

type-check: ## Run mypy strict
	uv run mypy src

test: ## Run the test suite
	uv run pytest

max-loc: ## Enforce 777-line per-file cap
	python3 scripts/check_max_loc.py

plugin-sync: ## Verify plugins/ matches the packaged assets/ mirror
	python3 scripts/check_plugin_sync.py

version-sync: ## Verify VERSION matches every plugin manifest's version field
	python3 scripts/check_version_sync.py

wheel-assets: ## Build the wheel and verify bundled assets
	ci/verify_wheel_assets.sh

wheel-install: ## Install the wheel outside the checkout and smoke-test afriend
	ci/verify_wheel_install.sh

# Materialize the composite skills projection, including deletions.
plugin-sync-copy: ## Copy canonical skill projection into the plugin
	python3 scripts/check_plugin_sync.py --copy

# Requires plantuml (brew install plantuml) and graphviz. Renders both PNG
# (for README embedding) and SVG (scalable, text-selectable) from every
# .puml under docs/architecture/. The rendered files are committed because
# the README references them by absolute raw.githubusercontent URL.
diagrams: ## Re-render docs/architecture/*.puml to PNG + SVG
	plantuml -tpng docs/architecture/*.puml
	plantuml -tsvg docs/architecture/*.puml

quality: lint type-check max-loc plugin-sync version-sync wheel-assets wheel-install test ## Run all portable quality gates

check: quality ## Alias for quality

# Local CI via act (see .actrc). `env -u DOCKER_HOST` keeps a Colima/Docker
# Desktop DOCKER_HOST from conflicting with .actrc's daemon-socket setting.
act-dry: ## List CI jobs without running them (validates the workflow + .actrc)
	env -u DOCKER_HOST act --list

act-ci: ## Run the CI quality job locally via act (slow; pulls an image first run)
	# Privilege is limited to act's disposable job container. The Linux gate
	# deliberately starts bubblewrap inside Docker, which requires nested
	# namespace creation; --init preserves the process-reaping parity in .actrc.
	env -u DOCKER_HOST act -j quality --matrix python-version:3.13 --rm \
		--container-options '--init --privileged'
