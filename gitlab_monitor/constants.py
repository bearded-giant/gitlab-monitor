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

COMMIT_WINDOW_CYCLE = [1, 3, 7]
DEFAULT_COMMIT_WINDOW_DAYS = 1

PIPELINE_REFRESH_INTERVAL = _env_int('GLMON_PIPELINE_REFRESH_INTERVAL', 120)
# dashboard GraphQL runs ~600 DB queries per call server-side; keep MR lists off a fast pipeline cadence
MR_REFRESH_INTERVAL = _env_int('GLMON_MR_REFRESH_INTERVAL', 120)
JOB_REFRESH_INTERVAL = _env_int('GLMON_JOB_REFRESH_INTERVAL', 120)
LOG_META_REFRESH_INTERVAL = _env_int('GLMON_LOG_REFRESH_INTERVAL', 5)
LOG_TRACE_REFRESH_INTERVAL = _env_int('GLMON_TRACE_REFRESH_INTERVAL', 2)
LOG_FETCH_TIMEOUT = _env_int('GLMON_FETCH_TIMEOUT', 30)
IDLE_PAUSE_MINUTES = _env_int('GLMON_IDLE_PAUSE_MINUTES', 15)

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

MR_STATE_CYCLE = ['opened', 'merged', 'closed', 'failed']

MR_DASHBOARD_TABS = ['active', 'merged', 'list']
MR_DASHBOARD_PAGE = 20
# measured on gitlab 19.2: 3 lists x full node at first:20 = 223 of the 250 complexity cap
MR_DASHBOARD_BATCH = 3
MR_DASHBOARD_MERGED_DAYS = 14

_AUTHORED = 'authoredMergeRequests'
_REVIEWING = 'reviewRequestedMergeRequests'
_MINE = 'includeAssigned: true, '

# (id, title, tab, group, field, args), from gitlab's merge_request_dashboard_data
MR_DASHBOARD_LISTS = [
    ('returned_to_you', 'Returned to you', 'active', 1, _AUTHORED,
     _MINE + 'state: opened, reviewStates: [REVIEWED, REQUESTED_CHANGES]'),
    ('reviews_requested', 'Review requested', 'active', 1, _REVIEWING,
     'state: opened, reviewStates: [UNAPPROVED, UNREVIEWED, REVIEW_STARTED]'),
    # gitlab's or:{reviewerWildcard: NONE} is a no-op on its own; reviewerWildcardId filters
    ('assigned_to_you', 'Your merge requests', 'active', 1, _AUTHORED,
     _MINE + 'state: opened, reviewerWildcardId: NONE'),
    ('waiting_for_assignee', 'Waiting for author or assignee', 'active', 2, _REVIEWING,
     'state: opened, reviewStates: [REVIEWED, REQUESTED_CHANGES]'),
    ('waiting_for_approvals', 'Waiting for approvals', 'active', 2, _AUTHORED,
     _MINE + 'state: opened, reviewStates: [UNREVIEWED, UNAPPROVED, REVIEW_STARTED], '
     'not: {reviewStates: [REQUESTED_CHANGES, REVIEWED]}'),
    ('approved_by_you', 'Approved by you', 'active', 2, _REVIEWING,
     'state: opened, reviewState: APPROVED'),
    ('approved_by_others', 'Approved by others', 'active', 2, _AUTHORED,
     _MINE + 'state: opened, reviewState: APPROVED, '
     'not: {reviewStates: [REQUESTED_CHANGES, REVIEWED, UNREVIEWED, REVIEW_STARTED, UNAPPROVED]}'),
    ('merged_recently_reviews', 'Reviews', 'merged', 1, _REVIEWING,
     'state: merged, mergedAfter: $mergedAfter, sort: MERGED_AT_DESC'),
    ('merged_recently_assigned', 'Your merge requests', 'merged', 1, _AUTHORED,
     _MINE + 'state: merged, mergedAfter: $mergedAfter, sort: MERGED_AT_DESC'),
]
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
