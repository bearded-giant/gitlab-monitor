# Copyright 2024 BeardedGiant
# https://github.com/bearded-giant/gitlab-tools
# Licensed under Apache License 2.0

try:
    from importlib.metadata import version as _pkg_version, PackageNotFoundError

    try:
        __version__ = _pkg_version("gitlab-monitor")
    except PackageNotFoundError:
        __version__ = "0.0.0+unknown"
except Exception:
    __version__ = "0.0.0+unknown"
