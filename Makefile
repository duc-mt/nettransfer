.PHONY: help setup lint format test cov run selftest build check-release clean

PY ?= python3

help:  ## list the targets
	@grep -E "^[a-z-]+:.*##" $(MAKEFILE_LIST) | sed "s/:.*##/\t/"

setup:  ## install runtime + dev dependencies and the git hooks
	$(PY) -m pip install -r requirements-dev.txt
	pre-commit install

lint:  ## ruff lint + format check (what CI runs)
	ruff check .
	ruff format --check .

format:  ## apply formatting and safe lint fixes
	ruff check --fix .
	ruff format .

test:  ## run the test suite (headless Qt)
	QT_QPA_PLATFORM=offscreen $(PY) -m pytest -q

cov:  ## tests with coverage (CI fails below the threshold in pyproject.toml)
	QT_QPA_PLATFORM=offscreen $(PY) -m pytest -q --cov --cov-report=term-missing

run:  ## start the GUI from source
	$(PY) run.py

selftest:  ## build the whole GUI offscreen and exit (also what the packaged binary runs in CI)
	$(PY) run.py --selftest

build:  ## single-file executables for THIS operating system -> dist/release/
	$(PY) packaging/build.py

check-release:  ## version, tag and CHANGELOG consistency (TAG=v2.0.0 optional)
	$(PY) scripts/check_version.py $(TAG)

clean:  ## remove build output and caches
	rm -rf build dist .pytest_cache .ruff_cache .coverage coverage.xml
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
