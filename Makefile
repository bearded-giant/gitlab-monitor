.PHONY: help install install-dev uninstall dev test clean version

PKG := gitlab-monitor

help:
	@echo "gitlab-monitor (glmon) - make targets"
	@echo ""
	@echo "  make install      pipx install . --force                       (release version from pyproject.toml)"
	@echo "  make install-dev  pipx install with .dev0+<sha>[.dirty] suffix (baked dev build)"
	@echo "  make dev          pipx install -e . --force                    (editable, auto-reflects source)"
	@echo "  make uninstall    pipx uninstall $(PKG)"
	@echo "  make test         run the pytest suite via uv (pytest not a project dep)"
	@echo "  make version      print the dev version that install-dev would use"
	@echo "  make clean        remove build artifacts"

# Compute dev version: <base>.dev0+<sha>[.dirty]
# Examples: 1.4.1.dev0+a1b2c3d            (clean tree)
#           1.4.1.dev0+a1b2c3d.dirty      (uncommitted changes)
DEV_BASE = $(shell grep -E '^version = ' pyproject.toml | head -1 | sed -E 's/version = "(.*)"/\1/')
DEV_SHA  = $(shell git rev-parse --short=7 HEAD 2>/dev/null || echo nogit)
DEV_DIRTY = $(shell git diff --quiet HEAD 2>/dev/null || echo .dirty)
DEV_VERSION = $(DEV_BASE).dev0+$(DEV_SHA)$(DEV_DIRTY)

version:
	@echo "$(DEV_VERSION)"

install:
	-pipx uninstall $(PKG)
	pipx install . --force

install-dev:
	@echo "Installing $(PKG) $(DEV_VERSION)"
	@cp pyproject.toml pyproject.toml.bak
	@sed -E -i.tmp 's/^version = .*/version = "$(DEV_VERSION)"/' pyproject.toml
	@rm -f pyproject.toml.tmp
	-pipx uninstall $(PKG)
	@pipx install . --force; \
	  rc=$$?; \
	  mv pyproject.toml.bak pyproject.toml; \
	  exit $$rc

dev:
	pipx install -e . --force

uninstall:
	pipx uninstall $(PKG)

test:
	uv sync
	uv run --with pytest pytest -q test_*.py

clean:
	rm -rf build dist *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete
