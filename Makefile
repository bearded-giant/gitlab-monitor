.PHONY: help install reinstall uninstall dev build clean

PKG := gitlab-monitor

help:
	@echo "gitlab-monitor (glmon) - make targets"
	@echo ""
	@echo "  make install     pipx install . --force"
	@echo "  make reinstall   pipx uninstall + install --force"
	@echo "  make dev         pipx install -e . --force (editable)"
	@echo "  make uninstall   pipx uninstall $(PKG)"
	@echo "  make build       python -m build (sdist + wheel)"
	@echo "  make clean       remove build artifacts"

install:
	pipx install . --force

reinstall:
	-pipx uninstall $(PKG)
	pipx install . --force

dev:
	pipx install -e . --force

uninstall:
	pipx uninstall $(PKG)

build:
	python -m build

clean:
	rm -rf build dist *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete
