.PHONY: help install lint type-check test plugin-sync version-sync max-loc diagrams plugin-sync-copy quality check

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

max-loc: ## Enforce 500-line per-file cap
	python3 scripts/check_max_loc.py

plugin-sync: ## Verify plugins/ matches the packaged assets/ mirror
	python3 scripts/check_plugin_sync.py

version-sync: ## Verify VERSION matches every plugin manifest's version field
	python3 scripts/check_version_sync.py

# Mirror assets/ -> plugins/.../skills/adversarial-friends/ byte-for-byte
# (including deletions). Manual convenience -- `plugin-sync` only verifies.
plugin-sync-copy: ## Copy assets/ -> the plugins/ mirror (manual)
	rsync -a --delete --exclude '__pycache__' --exclude '__init__.py' \
		src/adversarial_friends/assets/ \
		plugins/adversarial-friends/skills/adversarial-friends/

# Requires plantuml (brew install plantuml) and graphviz. Renders both PNG
# (for README embedding) and SVG (scalable, text-selectable) from every
# .puml under docs/architecture/. The rendered files are committed because
# the README references them by absolute raw.githubusercontent URL.
diagrams: ## Re-render docs/architecture/*.puml to PNG + SVG
	plantuml -tpng docs/architecture/*.puml
	plantuml -tsvg docs/architecture/*.puml

quality: lint type-check max-loc plugin-sync version-sync test ## Run all quality gates

check: quality ## Alias for quality
