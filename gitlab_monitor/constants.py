# Copyright 2024 BeardedGiant
# https://github.com/bearded-giant/gitlab-tools
# Licensed under Apache License 2.0

import os
from . import __version__


_DEBUG_LOG_ENABLED = ".dev" in __version__ and os.environ.get("GLMON_DEBUG_LOG", "").lower() != "off"
_DEBUG_LOG_PATH = os.environ.get("GLMON_DEBUG_LOG") or "/tmp/glmon-debug.log"


def _env_int(name, default):
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


PIPELINE_AGE_CYCLE = [3, 7, 30, None]
DEFAULT_PIPELINE_AGE_DAYS = 3

PIPELINE_REFRESH_INTERVAL = _env_int('GLMON_PIPELINE_REFRESH_INTERVAL', 10)
JOB_REFRESH_INTERVAL = _env_int('GLMON_JOB_REFRESH_INTERVAL', 10)
LOG_META_REFRESH_INTERVAL = _env_int('GLMON_LOG_REFRESH_INTERVAL', 5)
LOG_TRACE_REFRESH_INTERVAL = _env_int('GLMON_TRACE_REFRESH_INTERVAL', 20)
LOG_FETCH_TIMEOUT = _env_int('GLMON_FETCH_TIMEOUT', 30)

STATUS_STYLES = {
    'success':  ('bold #a6e3a1', ' success '),
    'failed':   ('bold #f38ba8', ' failed '),
    'running':  ('bold #f9e2af', ' running '),
    'pending':  ('bold #a6adc8', ' pending '),
    'skipped':  ('bold #6c7086', ' skipped '),
    'canceled': ('bold #a6adc8', ' canceled '),
    'created':  ('bold #6c7086', ' created '),
    'manual':   ('bold #89b4fa', ' manual '),
}

TERMINAL_STATUSES = frozenset({'success', 'failed', 'canceled', 'skipped', 'manual'})
MAX_LOG_LINES = 5000

INFO_PANE_WIDTH = 56
KEY_COLS = 3

MR_STATE_CYCLE = ['opened', 'merged', 'closed', 'all']
MERGED_WINDOW_CYCLE = [1, 7, 10]
DEFAULT_MERGED_WINDOW_DAYS = 7

LOGO = r"""
       _
  __ _| |_ __ ___   ___  _ __
 / _` | | '_ ` _ \ / _ \| '_ \
| (_| | | | | | | | (_) | | | |
 \__, |_|_| |_| |_|\___/|_| |_|
 |___/
"""

REPO_URL = "https://github.com/bearded-giant/gitlab-monitor"
