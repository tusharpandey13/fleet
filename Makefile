.PHONY: deps deps-online test test-py lint clean

# Install dev deps offline from committed wheelhouse (no network, no registry token).
deps:
	pip install --no-index --find-links vendor/wheels -r requirements-dev.txt

# Refresh the vendored wheelhouse from the Artifactory mirror (needs valid pip.conf token).
deps-online:
	pip download -d vendor/wheels --only-binary=:all: -r requirements-dev.txt

# bats spawns real git commits; disable GPG signing so tests never hang on pinentry.
BATS_GIT_ENV = GIT_CONFIG_COUNT=2 \
	GIT_CONFIG_KEY_0=commit.gpgsign GIT_CONFIG_VALUE_0=false \
	GIT_CONFIG_KEY_1=tag.gpgsign GIT_CONFIG_VALUE_1=false

test: deps
	python -m pytest tests/ -v
	$(BATS_GIT_ENV) bats tests/*.bats </dev/null

# Python-only gate (skips bats; useful where bats-core not installed).
test-py: deps
	python -m pytest tests/ -v

lint:
	@echo "Lint target: ruff/flake8 for Python, shellcheck for Bash (TBD)"

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache

help:
	@echo "Available targets:"
	@echo "  deps        - Install dev deps offline from vendor/wheels"
	@echo "  deps-online - Refresh vendor/wheels from Artifactory mirror"
	@echo "  test        - deps + pytest + bats"
	@echo "  test-py     - deps + pytest only (no bats)"
	@echo "  lint        - Run linters (TBD)"
	@echo "  clean       - Remove bytecode and caches"
