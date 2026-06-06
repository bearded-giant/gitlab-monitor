#!/usr/bin/env python3
# Copyright 2024 BeardedGiant
# https://github.com/bearded-giant/gitlab-tools
# Licensed under Apache License 2.0

import os
import sys
import argparse
import subprocess
import gitlab
import webbrowser
from datetime import datetime, timedelta, timezone
import re
import asyncio
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Static, Input, RichLog, TextArea, Label, Markdown
from textual.containers import Container, Horizontal, ScrollableContainer
from textual.suggester import SuggestFromList
from textual.screen import Screen, ModalScreen
from textual.binding import Binding
from rich.text import Text

from .config import Config


# pipeline age filter cycle: 3d -> 7d -> 30d -> all (None)
PIPELINE_AGE_CYCLE = [3, 7, 30, None]
DEFAULT_PIPELINE_AGE_DAYS = 3


def _env_int(name, default):
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


PIPELINE_REFRESH_INTERVAL = _env_int('GLMON_PIPELINE_REFRESH_INTERVAL', 10)
JOB_REFRESH_INTERVAL = _env_int('GLMON_JOB_REFRESH_INTERVAL', 10)
LOG_META_REFRESH_INTERVAL = _env_int('GLMON_LOG_REFRESH_INTERVAL', 5)
LOG_TRACE_REFRESH_INTERVAL = _env_int('GLMON_TRACE_REFRESH_INTERVAL', 20)
LOG_FETCH_TIMEOUT = _env_int('GLMON_FETCH_TIMEOUT', 30)


def copy_to_clipboard(text):
    try:
        subprocess.run(['pbcopy'], input=text.encode(), check=True)
        return True
    except Exception:
        try:
            subprocess.run(['xclip', '-selection', 'clipboard'], input=text.encode(), check=True)
            return True
        except Exception:
            return False


# -- status styling ----------------------------------------------------------

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


def status_badge(status):
    style, label = STATUS_STYLES.get(status, ('', f' {status} '))
    return Text(label, style=style)


def format_duration(seconds):
    if seconds is None:
        return "-"
    m, s = divmod(int(seconds), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h{m}m"
    return f"{m}m{s}s"


def format_age(iso_str):
    if not iso_str:
        return ""
    dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
    now = datetime.now(dt.tzinfo)
    diff = (now - dt).total_seconds()
    if diff < 60:
        return "just now"
    elif diff < 3600:
        return f"{int(diff / 60)}m ago"
    elif diff < 86400:
        return f"{int(diff / 3600)}h ago"
    elif diff < 604800:
        return f"{int(diff / 86400)}d ago"
    else:
        return dt.strftime('%m-%d')


# -- k9s-style header --------------------------------------------------------

INFO_PANE_WIDTH = 56
KEY_COLS = 3


def _info_lines(pairs, max_value_width=None):
    if not pairs:
        return []
    label_w = max(len(l) for l, _ in pairs)
    lines = []
    for label, value in pairs:
        t = Text()
        t.append(f"{label}:".rjust(label_w + 1), style="bold #f9e2af")
        t.append(" ", style="bold #f9e2af")
        sval = str(value)
        if max_value_width is not None and len(sval) > max_value_width:
            sval = sval[:max(0, max_value_width - 1)] + "…"
        t.append(sval, style="#cdd6f4")
        lines.append(t)
    return lines


def _key_lines(keys, cols=KEY_COLS):
    if not keys:
        return []
    rows_per_col = (len(keys) + cols - 1) // cols
    columns = [keys[i * rows_per_col:(i + 1) * rows_per_col] for i in range(cols)]
    col_widths = []
    for col in columns:
        if not col:
            col_widths.append((0, 0))
            continue
        max_k = max(len(f"<{k}>") for k, _ in col)
        max_a = max(len(a) for _, a in col)
        col_widths.append((max_k, max_a))
    rows = max((len(c) for c in columns), default=0)
    lines = []
    for r in range(rows):
        line = Text()
        for ci, col in enumerate(columns):
            kw, aw = col_widths[ci]
            if r < len(col):
                k, a = col[r]
                line.append(f"<{k}>".ljust(kw), style="bold #89b4fa")
                line.append(" ")
                line.append(a.ljust(aw + 3), style="#cdd6f4")
            else:
                line.append(" " * (kw + 1 + aw + 3))
        lines.append(line)
    return lines


def _format_header(info_pairs, keys, info_width=INFO_PANE_WIDTH):
    # leave room for label + ": " (~12 chars at most)
    info = _info_lines(info_pairs, max_value_width=max(10, info_width - 12))
    krows = _key_lines(keys)
    rows = max(len(info), len(krows))
    out = Text()
    for i in range(rows):
        if i > 0:
            out.append("\n")
        if i < len(info):
            left = info[i]
            out.append(left)
            pad = max(1, info_width - left.cell_len)
            out.append(" " * pad)
        else:
            out.append(" " * info_width)
        if i < len(krows):
            out.append(krows[i])
    return out


class K9sHeader(Static):

    def __init__(self, info_pairs, keys, **kw):
        super().__init__(_format_header(info_pairs, keys), **kw)
        self._info_pairs = info_pairs
        self._keys = keys

    def set_info(self, pairs) -> None:
        self._info_pairs = pairs
        self.update(_format_header(pairs, self._keys))

    def set_keys(self, keys) -> None:
        self._keys = keys
        self.update(_format_header(self._info_pairs, keys))


class KeyBar(Static):
    pass


class StatusBar(Horizontal):
    def __init__(self, left=None, right=None, **kw):
        super().__init__(**kw)
        self._initial_left = left or ""
        self._initial_right = right or ""

    def compose(self) -> ComposeResult:
        yield Static(self._initial_left, id="status-left")
        yield Static(self._initial_right, id="status-right")

    def set_text(self, text) -> None:
        try:
            self.query_one("#status-left", Static).update(text)
        except Exception:
            self._initial_left = text

    def set_right(self, text) -> None:
        try:
            self.query_one("#status-right", Static).update(text)
        except Exception:
            self._initial_right = text


def _auto_refresh_indicator(interval_seconds, active=True, refreshing=False):
    """Build right-side status text showing auto-refresh state."""
    t = Text()
    if not active or interval_seconds is None:
        t.append("auto-refresh: ", style="#6c7086")
        t.append("off", style="bold #f38ba8")
        return t
    if refreshing:
        t.append("⟳ ", style="bold #f9e2af")
        t.append("refreshing", style="bold #f9e2af")
    else:
        t.append("↻ ", style="#a6e3a1")
        t.append("auto-refresh: ", style="#6c7086")
        t.append(f"{interval_seconds}s", style="bold #a6e3a1")
    return t


def _status_line(parts):
    """Render footer line. parts: list of (label, value) or strings."""
    t = Text()
    sep = Text("  |  ", style="#585b70")
    first = True
    for p in parts:
        if not p:
            continue
        if not first:
            t.append_text(sep)
        first = False
        if isinstance(p, tuple):
            label, value = p
            t.append(f"{label}: ", style="#6c7086")
            t.append(str(value), style="bold #cdd6f4")
        else:
            t.append(str(p), style="#cdd6f4")
    return t


class ScreenBase(Screen):
    """base screen that routes single-char keys only when input is not focused"""

    # subclasses define: KEY_MAP = {"q": "back", "r": "refresh", ...}
    KEY_MAP: dict[str, str] = {}

    def _input_focused(self) -> bool:
        try:
            for inp in self.query(Input):
                if inp.has_focus:
                    return True
        except Exception:
            pass
        return False

    def on_key(self, event) -> None:
        if self._input_focused():
            if event.key in ("down", "escape"):
                event.prevent_default()
                event.stop()
                for dt in self.query(DataTable):
                    dt.focus()
                    break
            return

        action_name = self.KEY_MAP.get(event.key)
        if action_name:
            event.prevent_default()
            event.stop()
            method = getattr(self, f"action_{action_name}", None)
            if method:
                result = method()
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)


# -- api layer ---------------------------------------------------------------

class GitLabAPI:

    def __init__(self, config: Config):
        self.config = config
        self.gl = gitlab.Gitlab(config.gitlab_url, private_token=config.gitlab_token)
        self.project = None
        self.project_name = None

    def connect_project(self):
        if self.config.project_path:
            self.project = self.gl.projects.get(self.config.project_path)
            self.project_name = self.config.project_path

    def set_project(self, project_path: str):
        self.project = self.gl.projects.get(project_path)
        self.project_name = project_path

    @staticmethod
    def _project_activity(p):
        # GitLab's last_activity_at lags + skips pipeline events; take max with updated_at
        a = getattr(p, 'last_activity_at', None) or ''
        u = getattr(p, 'updated_at', None) or ''
        return max(a, u) or None

    def get_projects(self, search=None, per_page=50):
        params = {'per_page': per_page, 'order_by': 'last_activity_at', 'sort': 'desc', 'membership': True}
        if search:
            params['search'] = search
        projects = self.gl.projects.list(**params)
        return [{
            'id': p.id,
            'path': p.path_with_namespace,
            'name': p.name,
            'description': p.description or '',
            'last_activity': self._project_activity(p),
        } for p in projects]

    def get_project_meta(self, project_path):
        try:
            p = self.gl.projects.get(project_path)
            return {
                'id': p.id,
                'path': p.path_with_namespace,
                'name': p.name,
                'description': p.description or '',
                'last_activity': self._project_activity(p),
            }
        except Exception:
            return None

    def get_projects_by_paths(self, paths):
        results = []
        for path in paths:
            meta = self.get_project_meta(path)
            if meta:
                results.append(meta)
        return results

    def get_recent_pipelines(self, limit=50, ref=None, username=None, days=None):
        params = {'per_page': limit, 'order_by': 'id', 'sort': 'desc'}
        if ref:
            params['ref'] = ref
        if username:
            params['username'] = username
        if days is not None:
            since = datetime.now(timezone.utc) - timedelta(days=days)
            params['updated_after'] = since.isoformat().replace('+00:00', 'Z')
        pipelines = self.project.pipelines.list(**params)
        results = []
        for p in pipelines:
            results.append({
                'id': p.id,
                'status': p.status,
                'ref': p.ref,
                'sha': p.sha[:8],
                'created_at': p.created_at,
                'updated_at': p.updated_at,
                'user': getattr(p, 'user', {}).get('username', 'unknown') if hasattr(p, 'user') and p.user else 'unknown',
                'web_url': p.web_url,
            })
        return results

    def _project_path_from_url(self, web_url):
        # extract project path from pipeline web_url like
        # https://gitlab.example.com/group/subgroup/project/-/pipelines/123
        try:
            base = self.config.gitlab_url.rstrip('/')
            path = web_url.replace(base, '').strip('/')
            # path: group/subgroup/project/-/pipelines/123
            idx = path.find('/-/')
            if idx > 0:
                return path[:idx]
        except Exception:
            pass
        return None

    def get_pipeline_bridges(self, pipeline_id):
        try:
            pipeline = self.project.pipelines.get(pipeline_id)
            bridges = pipeline.bridges.list(all=True)
            results = []
            for b in bridges:
                dp = getattr(b, 'downstream_pipeline', None)
                if not dp:
                    continue
                web_url = dp.get('web_url', '')
                ds_project_path = self._project_path_from_url(web_url)
                is_same_project = (ds_project_path == self.project_name) if ds_project_path else True
                results.append({
                    'id': dp.get('id'),
                    'status': dp.get('status', 'unknown'),
                    'ref': dp.get('ref', ''),
                    'sha': (dp.get('sha') or '')[:8],
                    'created_at': dp.get('created_at', ''),
                    'updated_at': dp.get('updated_at', ''),
                    'web_url': web_url,
                    'user': 'unknown',
                    '_ds_project_path': ds_project_path if not is_same_project else None,
                    '_is_downstream': True,
                    '_parent_id': pipeline_id,
                    '_bridge_name': b.name,
                })
            return results
        except Exception:
            return []

    def get_pipeline_detail(self, pipeline_id):
        try:
            p = self.project.pipelines.get(pipeline_id)
            user = getattr(p, 'user', None) or {}
            return {
                'duration': getattr(p, 'duration', None),
                'queued_duration': getattr(p, 'queued_duration', None),
                'started_at': getattr(p, 'started_at', None),
                'finished_at': getattr(p, 'finished_at', None),
                'source': getattr(p, 'source', None),
                'coverage': getattr(p, 'coverage', None),
                'user': user.get('username') if isinstance(user, dict) else getattr(user, 'username', None),
            }
        except Exception:
            return {}

    def cancel_pipeline(self, pipeline_id):
        pipeline = self.project.pipelines.get(pipeline_id)
        pipeline.cancel()
        return pipeline.status

    def get_pipeline_jobs(self, pipeline_id):
        try:
            pipeline = self.project.pipelines.get(pipeline_id)
            jobs = pipeline.jobs.list(all=True)
            return [{
                'id': job.id,
                'name': job.name,
                'status': job.status,
                'stage': job.stage,
                'duration': job.duration,
                'started_at': job.started_at,
                'finished_at': job.finished_at,
                'web_url': job.web_url,
            } for job in jobs]
        except Exception:
            return []

    def get_job(self, job_id):
        try:
            job = self.project.jobs.get(job_id)
            return {
                'id': job.id,
                'name': job.name,
                'status': job.status,
                'stage': job.stage,
                'duration': job.duration,
                'started_at': job.started_at,
                'finished_at': job.finished_at,
                'web_url': job.web_url,
            }
        except Exception:
            return None

    def get_job_trace(self, job_id):
        try:
            job = self.project.jobs.get(job_id)
            trace = job.trace()
            if isinstance(trace, bytes):
                trace = trace.decode("utf-8", errors="replace")
            return trace
        except Exception as e:
            return f"Error fetching trace: {e}"

    def get_job_failures(self, job_id):
        trace = self.get_job_trace(job_id)
        failures = []
        summary_pattern = re.compile(
            r"=+\s*short test summary info\s*=+\n(.*?)(?=^=+|\Z)",
            re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )
        summary_match = summary_pattern.search(trace)
        if summary_match:
            for line in summary_match.group(1).strip().split('\n'):
                if 'FAILED' in line:
                    failures.append(line.strip())
        if not failures:
            for line in trace.split('\n'):
                if any(kw in line.lower() for kw in ['error:', 'failed:', 'exception:']):
                    failures.append(line.strip())
                    if len(failures) > 20:
                        break
        return failures

    # -- merge requests ------------------------------------------------------

    @staticmethod
    def _mr_project_path_from_refs(mr):
        # `references.full` looks like "group/sub/project!123"
        try:
            refs = getattr(mr, 'references', None) or {}
            full = refs.get('full') if isinstance(refs, dict) else None
            if full and '!' in full:
                return full.split('!', 1)[0]
        except Exception:
            pass
        return None

    @staticmethod
    def _mr_to_dict(mr, project_path=None):
        author = getattr(mr, 'author', None) or {}
        head_pipeline = getattr(mr, 'head_pipeline', None) or {}
        path = project_path or GitLabAPI._mr_project_path_from_refs(mr)
        return {
            'id': mr.id,
            'iid': mr.iid,
            'project_id': mr.project_id,
            'project_path': path or '',
            'title': mr.title,
            'description': getattr(mr, 'description', '') or '',
            'state': mr.state,
            'draft': bool(getattr(mr, 'draft', False) or getattr(mr, 'work_in_progress', False)),
            'merge_status': getattr(mr, 'merge_status', None) or getattr(mr, 'detailed_merge_status', None),
            'source_branch': mr.source_branch,
            'target_branch': mr.target_branch,
            'created_at': mr.created_at,
            'updated_at': mr.updated_at,
            'merged_at': getattr(mr, 'merged_at', None),
            'closed_at': getattr(mr, 'closed_at', None),
            'author': author.get('username') if isinstance(author, dict) else getattr(author, 'username', 'unknown'),
            'web_url': mr.web_url,
            'user_notes_count': getattr(mr, 'user_notes_count', 0) or 0,
            'upvotes': getattr(mr, 'upvotes', 0) or 0,
            'downvotes': getattr(mr, 'downvotes', 0) or 0,
            'head_pipeline_status': head_pipeline.get('status') if isinstance(head_pipeline, dict) else getattr(head_pipeline, 'status', None),
            'head_pipeline_id': head_pipeline.get('id') if isinstance(head_pipeline, dict) else getattr(head_pipeline, 'id', None),
            'head_pipeline_web_url': head_pipeline.get('web_url') if isinstance(head_pipeline, dict) else getattr(head_pipeline, 'web_url', None),
            'has_conflicts': getattr(mr, 'has_conflicts', False),
            'blocking_discussions_resolved': getattr(mr, 'blocking_discussions_resolved', True),
        }

    def get_my_merge_requests(self, state='opened', limit=50):
        try:
            mrs = self.gl.mergerequests.list(
                scope='created_by_me',
                state=state,
                per_page=limit,
                order_by='updated_at',
                sort='desc',
            )
            return [self._mr_to_dict(mr) for mr in mrs]
        except Exception:
            return []

    def get_project_merge_requests(self, project_path, state='merged', limit=50):
        try:
            project = self.gl.projects.get(project_path)
            mrs = project.mergerequests.list(
                state=state,
                per_page=limit,
                order_by='updated_at',
                sort='desc',
            )
            return [self._mr_to_dict(mr, project_path=project_path) for mr in mrs]
        except Exception:
            return []

    def get_merge_request(self, project_path, iid):
        try:
            project = self.gl.projects.get(project_path)
            mr = project.mergerequests.get(iid)
            data = self._mr_to_dict(mr, project_path=project_path)
            # diff stats
            try:
                changes = mr.changes()
                data['changes_count'] = len(changes.get('changes', [])) if isinstance(changes, dict) else 0
            except Exception:
                data['changes_count'] = 0
            # commits count
            try:
                commits = mr.commits()
                data['commits_count'] = sum(1 for _ in commits)
            except Exception:
                data['commits_count'] = 0
            # approvals
            try:
                approvals = mr.approvals.get()
                approved_by = getattr(approvals, 'approved_by', None) or []
                data['approvals_count'] = len(approved_by)
                data['approved_by'] = [
                    (a.get('user') or {}).get('username', '') if isinstance(a, dict) else ''
                    for a in approved_by
                ]
                data['approvals_required'] = getattr(approvals, 'approvals_required', 0) or 0
            except Exception:
                data['approvals_count'] = 0
                data['approved_by'] = []
                data['approvals_required'] = 0
            return data
        except Exception:
            return None

    def get_mr_approval_state(self, project_path, iid):
        try:
            project = self.gl.projects.get(project_path)
            mr = project.mergerequests.get(iid)
            state = mr.approval_state.get()
            rules_raw = getattr(state, 'rules', None) or []
            rules = []
            for r in rules_raw:
                d = r if isinstance(r, dict) else (r.attributes if hasattr(r, 'attributes') else dict(r))
                approved_by = d.get('approved_by') or []
                approved_names = [
                    (a.get('username') if isinstance(a, dict) else getattr(a, 'username', '')) or ''
                    for a in approved_by
                ]
                rules.append({
                    'id': d.get('id'),
                    'name': d.get('name') or '',
                    'rule_type': d.get('rule_type') or '',
                    'approvals_required': d.get('approvals_required', 0) or 0,
                    'approved': bool(d.get('approved', False)),
                    'approved_by': [n for n in approved_names if n],
                })
            return rules
        except Exception:
            return []

    def get_mr_pipelines(self, project_path, iid):
        try:
            project = self.gl.projects.get(project_path)
            mr = project.mergerequests.get(iid)
            pipelines = mr.pipelines.list(all=False, per_page=50)
            results = []
            for p in pipelines:
                pd = p.attributes if hasattr(p, 'attributes') else {}
                results.append({
                    'id': pd.get('id') or getattr(p, 'id', None),
                    'status': pd.get('status') or getattr(p, 'status', 'unknown'),
                    'ref': pd.get('ref') or getattr(p, 'ref', ''),
                    'sha': (pd.get('sha') or getattr(p, 'sha', '') or '')[:8],
                    'created_at': pd.get('created_at') or getattr(p, 'created_at', ''),
                    'updated_at': pd.get('updated_at') or getattr(p, 'updated_at', ''),
                    'web_url': pd.get('web_url') or getattr(p, 'web_url', ''),
                    'user': 'unknown',
                })
            return results
        except Exception:
            return []

    @staticmethod
    def _discussion_unresolved(disc_dict):
        notes = disc_dict.get('notes', []) if isinstance(disc_dict, dict) else []
        return any(n.get('resolvable') and not n.get('resolved') for n in notes if isinstance(n, dict))

    def get_mr_discussions(self, project_path, iid):
        try:
            project = self.gl.projects.get(project_path)
            mr = project.mergerequests.get(iid)
            discussions = mr.discussions.list(all=True)
            results = []
            for d in discussions:
                dd = d.attributes if hasattr(d, 'attributes') else dict(d)
                notes = dd.get('notes', []) or []
                if not notes:
                    continue
                first = notes[0]
                results.append({
                    'id': dd.get('id'),
                    'unresolved': self._discussion_unresolved(dd),
                    'resolvable': any(n.get('resolvable') for n in notes),
                    'notes': [
                        {
                            'id': n.get('id'),
                            'author': (n.get('author') or {}).get('username', 'unknown'),
                            'body': n.get('body') or '',
                            'created_at': n.get('created_at', ''),
                            'system': bool(n.get('system')),
                            'resolved': bool(n.get('resolved')),
                            'resolvable': bool(n.get('resolvable')),
                        }
                        for n in notes
                    ],
                    'first_author': (first.get('author') or {}).get('username', 'unknown'),
                    'first_created_at': first.get('created_at', ''),
                    'system': bool(first.get('system')),
                })
            return results
        except Exception:
            return []

    def get_mr_unresolved_count(self, project_path, iid):
        discussions = self.get_mr_discussions(project_path, iid)
        return sum(1 for d in discussions if d.get('unresolved'))

    def approve_merge_request(self, project_path, iid):
        project = self.gl.projects.get(project_path)
        mr = project.mergerequests.get(iid)
        mr.approve()
        return True

    def close_merge_request(self, project_path, iid):
        project = self.gl.projects.get(project_path)
        mr = project.mergerequests.get(iid)
        mr.state_event = 'close'
        mr.save()
        return True

    def create_mr_note(self, project_path, iid, body):
        project = self.gl.projects.get(project_path)
        mr = project.mergerequests.get(iid)
        return mr.notes.create({'body': body})


# -- screens -----------------------------------------------------------------

LOGO = r"""
       _
  __ _| |_ __ ___   ___  _ __
 / _` | | '_ ` _ \ / _ \| '_ \
| (_| | | | | | | | (_) | | | |
 \__, |_|_| |_| |_|\___/|_| |_|
 |___/
"""


REPO_URL = "https://github.com/bearded-giant/gitlab-monitor"


class LoadingScreen(Screen):

    def compose(self) -> ComposeResult:
        yield Static("", id="splash")
        yield KeyBar("  connecting to gitlab...", id="keybar")

    def on_mount(self) -> None:
        splash = self.query_one("#splash", Static)
        logo = LOGO.rstrip('\n')
        splash.update(
            f"[bold #89b4fa]{logo}[/]\n"
            f"\n[dim #a6adc8]built by Bearded Giant[/]  [dim #585b70]{REPO_URL}[/]"
            "\n\n\n[#a6adc8]loading projects...[/]"
        )


class ProjectSelectScreen(ScreenBase):

    KEY_MAP = {"q": "quit", "r": "refresh", "slash": "search", "s": "star", "a": "toggle_all", "m": "my_mrs", "g": "goto_mr"}

    DEBOUNCE_SECONDS = 0.4

    def __init__(self, api: GitLabAPI, favorites):
        super().__init__()
        self.api = api
        self.favorites = favorites
        self.projects = []
        self.mode = "fav" if favorites.list() else "all"
        self._search_timer = None

    def compose(self) -> ComposeResult:
        yield K9sHeader(self._info_pairs(), self._keys(), id="header")
        yield Container(
            Input(placeholder="/  filter projects...", id="project-search"),
            id="filter-bar",
        )
        yield DataTable(id="project-table")
        yield StatusBar(self._status_text(), id="statusbar")

    def _status_text(self):
        total = len(getattr(self, 'projects', []) or [])
        text_filter = ""
        try:
            text_filter = self.query_one("#project-search", Input).value.strip()
        except Exception:
            pass
        mode = "favorites" if self.mode == "fav" else "all"
        parts = [f"{total} projects", ("view", mode)]
        if text_filter:
            parts.append(("filter", text_filter))
        return _status_line(parts)

    def _refresh_status(self) -> None:
        try:
            self.query_one("#statusbar", StatusBar).set_text(self._status_text())
        except Exception:
            pass

    def _info_pairs(self):
        mode_label = "Favorites only" if self.mode == "fav" else "All projects"
        return [
            ("GitLab", self.api.config.gitlab_url),
            ("View", mode_label),
            ("Favorites", str(len(self.favorites.list()))),
        ]

    def _keys(self):
        return [
            ("q", "quit"),
            ("/", "filter"),
            ("r", "refresh"),
            ("s", "star"),
            ("a", "toggle all"),
            ("m", "my MRs"),
            ("g", "goto MR"),
            ("enter", "select"),
        ]

    def _refresh_breadcrumb(self) -> None:
        self.query_one("#header", K9sHeader).set_info(self._info_pairs())

    async def on_mount(self) -> None:
        table = self.query_one("#project-table", DataTable)
        table.add_columns("", "Project", "Description", "Last Activity")
        table.cursor_type = "row"
        await self.load_projects()
        table.focus()

    async def load_projects(self, search=None) -> None:
        if self.mode == "fav" and not search:
            paths = self.favorites.list()
            if paths:
                self.projects = await asyncio.to_thread(self.api.get_projects_by_paths, paths)
            else:
                # no favorites yet, fall back to all
                self.mode = "all"
                self.projects = await asyncio.to_thread(self.api.get_projects, None)
        else:
            self.projects = await asyncio.to_thread(self.api.get_projects, search)
        # sort by most recent activity desc (ISO 8601 strings sort lexically)
        self.projects.sort(key=lambda p: p.get('last_activity') or '', reverse=True)
        self._render_table()
        self._refresh_breadcrumb()

    def _render_table(self) -> None:
        table = self.query_one("#project-table", DataTable)
        table.clear()
        for p in self.projects:
            age = format_age(p['last_activity'])
            desc = (p['description'] or '')[:40]
            star = "*" if self.favorites.has(p['path']) else " "
            table.add_row(
                Text(star, style="bold #f9e2af"),
                Text(p['path'], style="bold"),
                Text(desc, style="dim"),
                Text(age, style="dim italic"),
            )
        self._refresh_status()

    def _schedule_search(self) -> None:
        if self._search_timer is not None:
            self._search_timer.stop()
        self._search_timer = self.set_timer(self.DEBOUNCE_SECONDS, self._do_search)

    async def _do_search(self) -> None:
        query = self.query_one("#project-search", Input).value.strip() or None
        if query and self.mode == "fav":
            # local filter over favorites
            paths = self.favorites.list()
            all_favs = await asyncio.to_thread(self.api.get_projects_by_paths, paths)
            q = query.lower()
            self.projects = [p for p in all_favs if q in p['path'].lower() or q in (p['name'] or '').lower()]
            self.projects.sort(key=lambda p: p.get('last_activity') or '', reverse=True)
            self._render_table()
            return
        await self.load_projects(search=query)

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "project-search":
            self._schedule_search()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "project-search":
            if self._search_timer is not None:
                self._search_timer.stop()
            await self._do_search()
            self.query_one("#project-table", DataTable).focus()

    async def action_search(self) -> None:
        self.query_one("#project-search", Input).focus()

    async def action_refresh(self) -> None:
        query = self.query_one("#project-search", Input).value.strip() or None
        await self.load_projects(search=query)

    async def action_star(self) -> None:
        table = self.query_one("#project-table", DataTable)
        idx = table.cursor_row
        if idx is None or idx >= len(self.projects):
            return
        path = self.projects[idx]['path']
        starred = self.favorites.toggle(path)
        self.notify(f"{'Starred' if starred else 'Unstarred'} {path}", timeout=2)
        # in favorites mode, drop unstarred row; otherwise just rerender
        if self.mode == "fav" and not starred:
            self.projects.pop(idx)
            self._render_table()
            if self.projects:
                table.move_cursor(row=min(idx, len(self.projects) - 1))
        else:
            self._render_table()
            table.move_cursor(row=idx)
        self._refresh_breadcrumb()

    async def action_toggle_all(self) -> None:
        self.mode = "all" if self.mode == "fav" else "fav"
        # clear search when toggling
        self.query_one("#project-search", Input).value = ""
        await self.load_projects()

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if idx is not None and idx < len(self.projects):
            project = self.projects[idx]
            self.api.set_project(project['path'])
            age = getattr(self.app, 'default_age_days', DEFAULT_PIPELINE_AGE_DAYS)
            initial_filter = getattr(self.app, 'default_branch_filter', '') or ''
            self.app.push_screen(PipelineListScreen(self.api, age_days=age, initial_filter=initial_filter))

    async def action_quit(self) -> None:
        # quitting from ProjectSelect = user wants to reset their "home" view
        try:
            self.api.config.clear_last_view()
        except Exception:
            pass
        self.app.exit()

    async def action_my_mrs(self) -> None:
        self.app.push_screen(MyMergeRequestsScreen(self.api))

    async def action_goto_mr(self) -> None:
        def _after(result):
            if result:
                project_path, iid = result
                asyncio.ensure_future(self._open_mr(project_path, iid))
        self.app.push_screen(MRPickerModal(recents=self.api.config.recent_projects), _after)

    async def _open_mr(self, project_path: str, iid: int) -> None:
        self.app.push_screen(MergeRequestDetailScreen(self.api, project_path, iid))


class PipelineListScreen(ScreenBase):

    KEY_MAP = {"q": "back", "r": "refresh", "slash": "search", "b": "browser", "y": "yank", "t": "toggle_age", "x": "cancel", "s": "toggle_status", "f": "failed", "M": "project_mrs"}

    STATUS_CYCLE = [None, 'running', 'pending']

    def __init__(self, api: GitLabAPI, age_days=DEFAULT_PIPELINE_AGE_DAYS, initial_filter: str = ""):
        super().__init__()
        self.api = api
        self.pipelines = []
        self.filtered_pipelines = []
        self.age_days = age_days
        self.initial_filter = initial_filter or ""
        self.status_filter = None
        self._refresh_timer = None
        self._refreshing = False
        self._bridges_cache = {}

    def _age_label(self) -> str:
        return f"last {self.age_days}d" if self.age_days is not None else "all"

    def _info_pairs(self):
        total = len(getattr(self, 'pipelines', []) or [])
        pipelines_label = str(total)
        if self.status_filter:
            pipelines_label = f"{len(self.filtered_pipelines)}/{total}  ({self.status_filter})"
        return [
            ("GitLab", self.api.config.gitlab_url),
            ("Project", self.api.project_name or "project"),
            ("Scope", self._age_label()),
            ("Pipelines", pipelines_label),
        ]

    def _keys(self):
        status_label = f"status:{self.status_filter}" if self.status_filter else "status"
        failed_label = "failed*" if self.status_filter == 'failed' else "failed"
        return [
            ("q", "back"),
            ("/", "filter"),
            ("r", "refresh"),
            ("t", "age"),
            ("s", status_label),
            ("f", failed_label),
            ("M", "MRs"),
            ("b", "browser"),
            ("y", "copy url"),
            ("x", "cancel"),
            ("enter", "jobs"),
        ]

    def compose(self) -> ComposeResult:
        yield K9sHeader(self._info_pairs(), self._keys(), id="header")
        yield Container(
            Input(value=self.initial_filter, placeholder="/  filter pipelines...", id="pipeline-filter"),
            id="filter-bar",
        )
        yield DataTable(id="pipeline-table")
        yield StatusBar(self._status_text(), id="statusbar")

    def _status_text(self):
        total = len(getattr(self, 'pipelines', []) or [])
        shown = len(getattr(self, 'filtered_pipelines', []) or [])
        text_filter = ""
        try:
            text_filter = self.query_one("#pipeline-filter", Input).value.strip()
        except Exception:
            pass
        count = f"{shown}/{total} pipelines" if shown != total else f"{total} pipelines"
        parts = [count, ("scope", self._age_label())]
        if self.status_filter:
            parts.append(("status", self.status_filter))
        if text_filter:
            parts.append(("filter", text_filter))
        return _status_line(parts)

    def _refresh_status(self) -> None:
        try:
            sb = self.query_one("#statusbar", StatusBar)
            sb.set_text(self._status_text())
            sb.set_right(_auto_refresh_indicator(
                self.REFRESH_INTERVAL,
                active=self._refresh_timer is not None,
                refreshing=self._refreshing,
            ))
        except Exception:
            pass

    REFRESH_INTERVAL = PIPELINE_REFRESH_INTERVAL

    async def on_mount(self) -> None:
        table = self.query_one("#pipeline-table", DataTable)
        table.add_columns("ID", "Status", "Branch", "SHA", "Age", "User")
        table.cursor_type = "row"
        await self.load_pipelines()
        table.focus()
        self._refresh_timer = self.set_interval(self.REFRESH_INTERVAL, self._safe_refresh)
        self._refresh_status()
        if self.api.project_name:
            self.api.config.save_last_view('pipelines', project=self.api.project_name)
            self.api.config.recent_projects.remember(self.api.project_name)

    def _refresh_breadcrumb(self) -> None:
        self.query_one("#header", K9sHeader).set_info(self._info_pairs())

    async def load_pipelines(self, full_bridge_scan=True) -> None:
        raw = await asyncio.to_thread(self.api.get_recent_pipelines, 50, None, None, self.age_days)
        active_ids = set(p['id'] for p in raw if p['status'] not in TERMINAL_STATUSES)
        ids_to_check = set(active_ids)
        if full_bridge_scan:
            for p in raw[:10]:
                ids_to_check.add(p['id'])
            self._bridges_cache = {}
        else:
            # re-check parents that had active downstream last time
            for pid, ds_list in self._bridges_cache.items():
                if any(d['status'] not in TERMINAL_STATUSES for d in ds_list):
                    ids_to_check.add(pid)
        if ids_to_check:
            check_list = list(ids_to_check)
            bridge_results = await asyncio.gather(*(
                asyncio.to_thread(self.api.get_pipeline_bridges, pid) for pid in check_list
            ))
            for pid, bridges in zip(check_list, bridge_results):
                if bridges:
                    self._bridges_cache[pid] = bridges
        self.pipelines = []
        for p in raw:
            self.pipelines.append(p)
            for ds in self._bridges_cache.get(p['id'], []):
                self.pipelines.append(ds)
        self._apply_filter()
        self._refresh_breadcrumb()

    async def _safe_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        self._refresh_status()
        try:
            await self.load_pipelines(full_bridge_scan=False)
        except Exception:
            pass
        finally:
            self._refreshing = False
            self._refresh_status()

    def on_unmount(self) -> None:
        if self._refresh_timer:
            self._refresh_timer.stop()

    def _apply_filter(self) -> None:
        query = self.query_one("#pipeline-filter", Input).value.strip().lower()
        pipelines = self.pipelines
        if self.status_filter:
            pipelines = [p for p in pipelines if p['status'] == self.status_filter]
        if query:
            self.filtered_pipelines = [
                p for p in pipelines
                if query in p['ref'].lower()
                or query in p['status'].lower()
                or query in p['user'].lower()
                or query in str(p['id'])
                or query in p['sha'].lower()
                or query in p.get('_bridge_name', '').lower()
                or query in p.get('_ds_project_path', '').lower()
            ]
        else:
            self.filtered_pipelines = pipelines
        self._update_table()
        self._refresh_status()

    def _update_table(self) -> None:
        table = self.query_one("#pipeline-table", DataTable)
        prev_row = table.cursor_row
        table.clear()
        for p in self.filtered_pipelines:
            is_ds = p.get('_is_downstream', False)
            age = format_age(p['created_at'])
            if is_ds:
                bridge = p.get('_bridge_name', '')
                ds_path = p.get('_ds_project_path', '')
                label = bridge or ds_path or ''
                if ds_path and bridge:
                    label = f"{bridge} ({ds_path})"
                ref_text = Text()
                ref_text.append("  └ ", style="dim #585b70")
                ref_text.append(label[:35] if label else p['ref'][:30], style="#b4befe")
                id_text = Text()
                id_text.append(f"#{p['id']}", style="#b4befe dim")
                table.add_row(
                    id_text,
                    status_badge(p['status']),
                    ref_text,
                    Text(p['sha'], style="dim"),
                    Text(age, style="dim italic"),
                    Text("", style="dim"),
                )
            else:
                ref = p['ref'][:30]
                table.add_row(
                    Text(str(p['id']), style="bold"),
                    status_badge(p['status']),
                    Text(ref, style="cyan"),
                    Text(p['sha'], style="dim"),
                    Text(age, style="dim italic"),
                    Text(p['user'], style="dim"),
                )
        if prev_row is not None and self.filtered_pipelines:
            table.move_cursor(row=min(prev_row, len(self.filtered_pipelines) - 1))

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "pipeline-filter":
            self._apply_filter()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "pipeline-filter":
            self._apply_filter()
            self.query_one("#pipeline-table", DataTable).focus()

    async def action_back(self) -> None:
        self.app.pop_screen()

    async def action_refresh(self) -> None:
        await self.load_pipelines()

    async def action_search(self) -> None:
        self.query_one("#pipeline-filter", Input).focus()

    async def action_toggle_age(self) -> None:
        try:
            idx = PIPELINE_AGE_CYCLE.index(self.age_days)
        except ValueError:
            idx = -1
        self.age_days = PIPELINE_AGE_CYCLE[(idx + 1) % len(PIPELINE_AGE_CYCLE)]
        self._refresh_breadcrumb()
        self.notify(f"Showing pipelines: {self._age_label()}", timeout=2)
        await self.load_pipelines()

    async def action_toggle_status(self) -> None:
        try:
            idx = self.STATUS_CYCLE.index(self.status_filter)
        except ValueError:
            idx = -1
        self.status_filter = self.STATUS_CYCLE[(idx + 1) % len(self.STATUS_CYCLE)]
        self._apply_filter()
        self._refresh_header()

    async def action_failed(self) -> None:
        self.status_filter = None if self.status_filter == 'failed' else 'failed'
        self._apply_filter()
        self._refresh_header()

    def _refresh_header(self) -> None:
        try:
            header = self.query_one("#header", K9sHeader)
            header.set_keys(self._keys())
            header.set_info(self._info_pairs())
        except Exception:
            pass

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if idx is not None and idx < len(self.filtered_pipelines):
            pipeline = self.filtered_pipelines[idx]
            ds_path = pipeline.get('_ds_project_path')
            if pipeline.get('_is_downstream') and ds_path:
                ds_api = GitLabAPI(self.api.config)
                try:
                    ds_api.set_project(ds_path)
                except Exception:
                    self.notify(f"Cannot access {ds_path}", severity="error", timeout=3)
                    return
                self.app.push_screen(JobListScreen(ds_api, pipeline))
                return
            self.app.push_screen(JobListScreen(self.api, pipeline))

    async def action_browser(self) -> None:
        table = self.query_one("#pipeline-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self.filtered_pipelines):
            webbrowser.open(self.filtered_pipelines[table.cursor_row]['web_url'])

    async def action_yank(self) -> None:
        table = self.query_one("#pipeline-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self.filtered_pipelines):
            p = self.filtered_pipelines[table.cursor_row]
            if copy_to_clipboard(p['web_url']):
                self.notify(f"Copied pipeline #{p['id']} URL", timeout=2)

    async def action_cancel(self) -> None:
        table = self.query_one("#pipeline-table", DataTable)
        if table.cursor_row is None or table.cursor_row >= len(self.filtered_pipelines):
            return
        p = self.filtered_pipelines[table.cursor_row]
        if p['status'] in TERMINAL_STATUSES:
            self.notify(f"Pipeline #{p['id']} already {p['status']}", timeout=2)
            return

        def _after(confirmed):
            if confirmed:
                asyncio.ensure_future(self._do_cancel(p))

        modal = ConfirmModal(
            f"Cancel pipeline #{p['id']}?",
            detail=f"{p['ref'][:40]}  ({p['status']})",
        )
        self.app.push_screen(modal, _after)

    async def _do_cancel(self, pipeline) -> None:
        try:
            await asyncio.to_thread(self.api.cancel_pipeline, pipeline['id'])
            self.notify(f"Cancelled pipeline #{pipeline['id']}", timeout=2)
            await self.load_pipelines()
        except Exception as e:
            self.notify(f"Cancel failed: {e}", severity="error", timeout=3)

    async def action_project_mrs(self) -> None:
        if self.api.project_name:
            self.app.push_screen(ProjectMergeRequestsScreen(self.api, self.api.project_name))


class JobListScreen(ScreenBase):

    KEY_MAP = {"q": "back", "r": "refresh", "slash": "search", "b": "browser", "f": "failures", "s": "toggle_status", "y": "yank"}

    STAGE_ORDER = ['build', 'test', 'deploy', 'cleanup']

    STATUS_CYCLE = [None, 'running', 'pending']

    def __init__(self, api: GitLabAPI, pipeline: dict):
        super().__init__()
        self.api = api
        self.pipeline = pipeline
        self.jobs = []
        self.filtered_jobs = []
        self.status_filter = None
        self._refresh_timer = None
        self._refreshing = False

    def _info_pairs(self):
        jobs_label = str(len(getattr(self, 'jobs', []) or []))
        if self.status_filter:
            jobs_label = f"{len(self.filtered_jobs)}/{jobs_label}  ({self.status_filter})"
        p = self.pipeline
        pairs = [
            ("GitLab", self.api.config.gitlab_url),
            ("Project", self.api.project_name or "project"),
            ("Pipeline", f"#{p['id']}  {p['status']}"),
            ("Branch", p['ref'][:40]),
            ("Jobs", jobs_label),
        ]
        dur = p.get('duration')
        if dur is not None:
            qd = p.get('queued_duration')
            label = format_duration(dur)
            if qd:
                label = f"{label}  (queued {format_duration(qd)})"
            pairs.append(("Duration", label))
        started = p.get('started_at')
        if started:
            pairs.append(("Started", format_age(started)))
        user = p.get('user')
        if user:
            pairs.append(("User", user))
        source = p.get('source')
        if source:
            pairs.append(("Source", source))
        coverage = p.get('coverage')
        if coverage is not None:
            pairs.append(("Coverage", f"{coverage}%"))
        return pairs

    def _keys(self):
        status_label = f"status:{self.status_filter}" if self.status_filter else "status"
        return [
            ("q", "back"),
            ("/", "filter"),
            ("r", "refresh"),
            ("b", "browser"),
            ("f", "failures"),
            ("s", status_label),
            ("y", "copy url"),
            ("enter", "logs"),
        ]

    def compose(self) -> ComposeResult:
        yield K9sHeader(self._info_pairs(), self._keys(), id="header")
        yield Container(
            Input(placeholder="/  filter jobs...", id="job-filter"),
            id="filter-bar",
        )
        yield DataTable(id="job-table")
        yield StatusBar(self._status_text(), id="statusbar")

    def _status_text(self):
        total = len(getattr(self, 'jobs', []) or [])
        shown = len(getattr(self, 'filtered_jobs', []) or [])
        text_filter = ""
        try:
            text_filter = self.query_one("#job-filter", Input).value.strip()
        except Exception:
            pass
        count = f"{shown}/{total} jobs" if shown != total else f"{total} jobs"
        parts = [count]
        if self.status_filter:
            parts.append(("status", self.status_filter))
        if text_filter:
            parts.append(("filter", text_filter))
        return _status_line(parts)

    def _refresh_status(self) -> None:
        try:
            sb = self.query_one("#statusbar", StatusBar)
            sb.set_text(self._status_text())
            sb.set_right(_auto_refresh_indicator(
                self.REFRESH_INTERVAL,
                active=self._refresh_timer is not None,
                refreshing=self._refreshing,
            ))
        except Exception:
            pass

    REFRESH_INTERVAL = JOB_REFRESH_INTERVAL

    async def on_mount(self) -> None:
        table = self.query_one("#job-table", DataTable)
        table.add_columns("Stage", "Name", "Status", "Duration", "ID")
        table.cursor_type = "row"
        await self.load_jobs()
        table.focus()
        self._refresh_timer = self.set_interval(self.REFRESH_INTERVAL, self._safe_refresh)
        self._refresh_status()

    async def load_jobs(self) -> None:
        jobs, detail = await asyncio.gather(
            asyncio.to_thread(self.api.get_pipeline_jobs, self.pipeline['id']),
            asyncio.to_thread(self.api.get_pipeline_detail, self.pipeline['id']),
        )
        self.jobs = jobs
        if detail:
            self.pipeline.update(detail)
        order = self.STAGE_ORDER
        self.jobs.sort(key=lambda j: (
            order.index(j['stage']) if j['stage'] in order else len(order),
            j['name'],
        ))
        self._apply_filter()
        try:
            self.query_one("#header", K9sHeader).set_info(self._info_pairs())
        except Exception:
            pass

    def _apply_filter(self) -> None:
        query = self.query_one("#job-filter", Input).value.strip().lower()
        jobs = self.jobs
        if self.status_filter:
            jobs = [j for j in jobs if j['status'] == self.status_filter]
        if query:
            self.filtered_jobs = [
                j for j in jobs
                if query in j['name'].lower()
                or query in j['stage'].lower()
                or query in j['status'].lower()
                or query in str(j['id'])
            ]
        else:
            self.filtered_jobs = jobs
        self._update_table()
        self._refresh_status()

    def _update_table(self) -> None:
        table = self.query_one("#job-table", DataTable)
        prev_row = table.cursor_row
        table.clear()
        for job in self.filtered_jobs:
            failed = job['status'] == 'failed'
            name_style = "bold red" if failed else ""
            stage_style = "red" if failed else "dim"
            dur = format_duration(job['duration'])
            table.add_row(
                Text(job['stage'], style=stage_style),
                Text(job['name'][:50], style=name_style),
                status_badge(job['status']),
                Text(dur, style="red" if failed else "dim"),
                Text(str(job['id']), style="dim"),
            )
        if prev_row is not None and self.filtered_jobs:
            table.move_cursor(row=min(prev_row, len(self.filtered_jobs) - 1))

    async def _safe_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        self._refresh_status()
        try:
            await self.load_jobs()
        except Exception:
            pass
        finally:
            self._refreshing = False
            self._refresh_status()

    def on_unmount(self) -> None:
        if self._refresh_timer:
            self._refresh_timer.stop()

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "job-filter":
            self._apply_filter()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "job-filter":
            self._apply_filter()
            self.query_one("#job-table", DataTable).focus()

    async def action_back(self) -> None:
        self.app.pop_screen()

    async def action_refresh(self) -> None:
        await self.load_jobs()

    async def action_search(self) -> None:
        self.query_one("#job-filter", Input).focus()

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if idx is not None and idx < len(self.filtered_jobs):
            self.app.push_screen(JobDetailScreen(self.api, self.filtered_jobs[idx]))

    async def action_browser(self) -> None:
        table = self.query_one("#job-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self.filtered_jobs):
            webbrowser.open(self.filtered_jobs[table.cursor_row]['web_url'])

    async def action_failures(self) -> None:
        failed = [j for j in self.jobs if j['status'] == 'failed']
        if failed:
            self.app.push_screen(FailedJobsScreen(self.api, self.pipeline, failed))

    async def action_toggle_status(self) -> None:
        idx = self.STATUS_CYCLE.index(self.status_filter)
        self.status_filter = self.STATUS_CYCLE[(idx + 1) % len(self.STATUS_CYCLE)]
        self._apply_filter()
        try:
            header = self.query_one("#header", K9sHeader)
            header.set_keys(self._keys())
            header.set_info(self._info_pairs())
        except Exception:
            pass

    async def action_yank(self) -> None:
        table = self.query_one("#job-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self.filtered_jobs):
            j = self.filtered_jobs[table.cursor_row]
            if copy_to_clipboard(j['web_url']):
                self.notify(f"Copied job #{j['id']} URL", timeout=2)


class JobDetailScreen(ScreenBase):

    KEY_MAP = {"q": "back", "r": "refresh", "b": "browser", "f": "failures", "y": "yank"}

    def __init__(self, api: GitLabAPI, job: dict):
        super().__init__()
        self.api = api
        self.job = job
        self.trace = ""
        self.trace_lines_written = 0
        self.failures = []
        self._refresh_timer = None
        self._trace_timer = None
        self._refreshing_meta = False
        self._refreshing_trace = False
        self._final_trace_done = False

    REFRESH_INTERVAL = LOG_META_REFRESH_INTERVAL
    TRACE_INTERVAL = LOG_TRACE_REFRESH_INTERVAL
    FETCH_TIMEOUT = LOG_FETCH_TIMEOUT

    def _info_pairs(self):
        status = self.job.get('status', '?')
        return [
            ("GitLab", self.api.config.gitlab_url),
            ("Project", self.api.project_name or "project"),
            ("Job", f"#{self.job['id']}  {self.job['name']}"),
            ("Status", status),
        ]

    def _keys(self):
        return [
            ("q", "back"),
            ("r", "refresh"),
            ("b", "browser"),
            ("f", "failures only"),
            ("y", "copy"),
        ]

    def compose(self) -> ComposeResult:
        yield K9sHeader(self._info_pairs(), self._keys(), id="header")
        yield RichLog(id="job-log", wrap=True, max_lines=MAX_LOG_LINES)
        yield StatusBar(self._status_text(), id="statusbar")

    def _status_text(self):
        dur = format_duration(self.job.get('duration'))
        return _status_line([("duration", dur)])

    def _refresh_status(self) -> None:
        try:
            sb = self.query_one("#statusbar", StatusBar)
            sb.set_text(self._status_text())
            sb.set_right(self._build_refresh_indicator())
        except Exception:
            pass

    def _build_refresh_indicator(self):
        active = self._refresh_timer is not None
        refreshing = self._refreshing_meta or self._refreshing_trace
        t = Text()
        if not active:
            t.append("auto-refresh: ", style="#6c7086")
            t.append("off", style="bold #f38ba8")
            return t
        if refreshing:
            t.append("⟳ ", style="bold #f9e2af")
            t.append("refreshing", style="bold #f9e2af")
            return t
        t.append("↻ ", style="#a6e3a1")
        t.append("status ", style="#6c7086")
        t.append(f"{self.REFRESH_INTERVAL}s", style="bold #a6e3a1")
        t.append(" · log ", style="#6c7086")
        t.append(f"{self.TRACE_INTERVAL}s", style="bold #a6e3a1")
        return t

    def _update_info_bar(self) -> None:
        try:
            self.query_one("#header", K9sHeader).set_info(self._info_pairs())
        except Exception:
            pass
        self._refresh_status()

    async def on_mount(self) -> None:
        await self.load_trace()
        if self.job.get('status') not in TERMINAL_STATUSES:
            self._refresh_timer = self.set_interval(self.REFRESH_INTERVAL, self._refresh_meta)
            self._trace_timer = self.set_interval(self.TRACE_INTERVAL, self._refresh_trace)
        self._update_info_bar()

    def _stop_timers_if_terminal(self) -> None:
        if self.job.get('status') not in TERMINAL_STATUSES:
            return
        if self._refresh_timer:
            self._refresh_timer.stop()
            self._refresh_timer = None
        if self._trace_timer:
            self._trace_timer.stop()
            self._trace_timer = None

    async def _refresh_meta(self) -> None:
        if self._refreshing_meta:
            return
        self._refreshing_meta = True
        self._refresh_status()
        try:
            try:
                fresh = await asyncio.wait_for(
                    asyncio.to_thread(self.api.get_job, self.job['id']),
                    timeout=self.FETCH_TIMEOUT,
                )
                if fresh:
                    self.job = fresh
            except Exception:
                pass
            self._update_info_bar()
            # job just went terminal — fetch final trace once before stopping timers
            if (
                self.job.get('status') in TERMINAL_STATUSES
                and not self._final_trace_done
            ):
                self._final_trace_done = True
                await self._final_trace_fetch()
            self._stop_timers_if_terminal()
        finally:
            self._refreshing_meta = False
            self._refresh_status()

    async def _final_trace_fetch(self) -> None:
        # wait briefly for any in-flight trace refresh to clear so we don't
        # double-write or skip; bound the wait to FETCH_TIMEOUT
        deadline_iters = max(1, int(self.FETCH_TIMEOUT * 10))
        for _ in range(deadline_iters):
            if not self._refreshing_trace:
                break
            await asyncio.sleep(0.1)
        self._refreshing_trace = True
        self._refresh_status()
        try:
            try:
                await asyncio.wait_for(self._append_new_trace(), timeout=self.FETCH_TIMEOUT)
            except Exception:
                pass
        finally:
            self._refreshing_trace = False

    async def _refresh_trace(self) -> None:
        if self._refreshing_trace:
            return
        self._refreshing_trace = True
        self._refresh_status()
        try:
            try:
                await asyncio.wait_for(self._append_new_trace(), timeout=self.FETCH_TIMEOUT)
            except Exception:
                pass
        finally:
            self._refreshing_trace = False
            self._refresh_status()

    def _write_trace_line(self, log, line) -> None:
        if any(kw in line.lower() for kw in ['error', 'failed', 'exception']):
            log.write(Text(line, style="#f38ba8"))
        else:
            log.write(line)

    async def _append_new_trace(self) -> None:
        new_trace = await asyncio.to_thread(self.api.get_job_trace, self.job['id'])
        if new_trace == self.trace:
            return
        log = self.query_one("#job-log", RichLog)
        new_lines = new_trace.split('\n')
        # append only lines beyond what we already wrote
        for line in new_lines[self.trace_lines_written:]:
            self._write_trace_line(log, line)
        self.trace = new_trace
        self.trace_lines_written = len(new_lines)

    async def load_trace(self) -> None:
        log = self.query_one("#job-log", RichLog)
        log.clear()
        self.trace = await asyncio.to_thread(self.api.get_job_trace, self.job['id'])
        self.trace_lines_written = 0

        if self.job['status'] == 'failed':
            self.failures = await asyncio.to_thread(self.api.get_job_failures, self.job['id'])
            if self.failures:
                log.write(Text(" FAILURE SUMMARY ", style="bold white on #f38ba8"))
                log.write("")
                for f in self.failures:
                    log.write(Text(f"  {f}", style="#f38ba8"))
                log.write("")
                log.write(Text(
                    " " + "-" * 78 + " ",
                    style="dim",
                ))
                log.write("")

        lines = self.trace.split('\n')
        for line in lines:
            self._write_trace_line(log, line)
        self.trace_lines_written = len(lines)

    def on_unmount(self) -> None:
        if self._refresh_timer:
            self._refresh_timer.stop()
        if self._trace_timer:
            self._trace_timer.stop()

    async def action_back(self) -> None:
        self.app.pop_screen()

    async def action_refresh(self) -> None:
        fresh = await asyncio.to_thread(self.api.get_job, self.job['id'])
        if fresh:
            self.job = fresh
        self._update_info_bar()
        await self.load_trace()

    async def action_browser(self) -> None:
        webbrowser.open(self.job['web_url'])

    async def action_failures(self) -> None:
        log = self.query_one("#job-log", RichLog)
        log.clear()
        if self.failures:
            log.write(Text(" FAILURES ONLY ", style="bold white on #f38ba8"))
            log.write("")
            for f in self.failures:
                log.write(Text(f"  {f}", style="#f38ba8"))
        else:
            log.write(Text("  No failures detected", style="dim"))

    async def action_yank(self) -> None:
        text = self.trace if self.trace else "\n".join(self.failures)
        if copy_to_clipboard(text):
            self.notify("Copied to clipboard", timeout=2)
        else:
            self.notify("Copy failed", severity="error", timeout=2)


class FailedJobsScreen(ScreenBase):

    KEY_MAP = {"q": "back", "y": "yank"}

    def __init__(self, api: GitLabAPI, pipeline: dict, failed_jobs: list):
        super().__init__()
        self.api = api
        self.pipeline = pipeline
        self.failed_jobs = failed_jobs

    def _info_pairs(self):
        return [
            ("GitLab", self.api.config.gitlab_url),
            ("Project", self.api.project_name or "project"),
            ("Pipeline", f"#{self.pipeline['id']}"),
            ("Failures", str(len(self.failed_jobs))),
        ]

    def _keys(self):
        return [("q", "back"), ("y", "copy")]

    def compose(self) -> ComposeResult:
        yield K9sHeader(self._info_pairs(), self._keys(), id="header")
        yield RichLog(id="failures-log", wrap=True, max_lines=MAX_LOG_LINES)
        yield StatusBar(
            _status_line([f"{len(self.failed_jobs)} failed jobs", ("pipeline", f"#{self.pipeline['id']}")]),
            id="statusbar",
        )

    async def on_mount(self) -> None:
        await self.load_failures()

    async def load_failures(self) -> None:
        log = self.query_one("#failures-log", RichLog)
        log.clear()
        for job in self.failed_jobs:
            dur = format_duration(job['duration'])
            log.write(Text(
                f" {job['name']} ",
                style="bold white on #f38ba8",
            ))
            log.write(Text(
                f"  stage: {job['stage']}  duration: {dur}  id: {job['id']}",
                style="dim",
            ))
            log.write("")
            failures = await asyncio.to_thread(self.api.get_job_failures, job['id'])
            if failures:
                for f in failures[:10]:
                    log.write(Text(f"  {f}", style="#f38ba8"))
            else:
                log.write(Text("  no specific failures extracted", style="dim"))
            log.write("")
            log.write("")

    async def action_back(self) -> None:
        self.app.pop_screen()

    async def action_yank(self) -> None:
        lines = []
        for job in self.failed_jobs:
            lines.append(f"{job['name']} (stage: {job['stage']}, id: {job['id']})")
            failures = await asyncio.to_thread(self.api.get_job_failures, job['id'])
            for f in failures[:10]:
                lines.append(f"  {f}")
            lines.append("")
        if copy_to_clipboard("\n".join(lines)):
            self.notify("Copied to clipboard", timeout=2)
        else:
            self.notify("Copy failed", severity="error", timeout=2)


# -- merge request screens ---------------------------------------------------

MR_STATE_CYCLE = ['opened', 'merged', 'closed', 'all']


def _mr_state_badge(state):
    styles = {
        'opened': ('bold #a6e3a1', ' open '),
        'merged': ('bold #89b4fa', ' merged '),
        'closed': ('bold #f38ba8', ' closed '),
        'locked': ('bold #6c7086', ' locked '),
    }
    style, label = styles.get(state, ('', f' {state} '))
    return Text(label, style=style)


def _mr_state_color(state):
    colors = {
        'opened': '#a6e3a1',
        'merged': '#89b4fa',
        'closed': '#f38ba8',
        'locked': '#6c7086',
    }
    return colors.get(state, '#cdd6f4')


def _pipeline_status_color(status):
    if not status:
        return '#6c7086'
    colors = {
        'success': '#a6e3a1',
        'failed': '#f38ba8',
        'running': '#f9e2af',
        'pending': '#a6adc8',
        'canceled': '#a6adc8',
        'skipped': '#6c7086',
        'manual': '#89b4fa',
        'created': '#6c7086',
    }
    return colors.get(status, '#cdd6f4')


def _pipeline_status_text(status):
    if not status:
        return Text("—", style="dim")
    return status_badge(status)


class MyMergeRequestsScreen(ScreenBase):

    KEY_MAP = {"q": "back", "r": "refresh", "slash": "search", "b": "browser", "y": "yank", "g": "goto", "s": "toggle_state"}

    REFRESH_INTERVAL = PIPELINE_REFRESH_INTERVAL

    def __init__(self, api: GitLabAPI):
        super().__init__()
        self.api = api
        self.mrs = []
        self.filtered_mrs = []
        self.state = 'opened'
        self._refresh_timer = None
        self._refreshing = False
        self._unresolved_cache = {}

    def _info_pairs(self):
        total = len(self.mrs)
        shown = len(self.filtered_mrs)
        count = f"{shown}/{total}" if shown != total else str(total)
        return [
            ("GitLab", self.api.config.gitlab_url),
            ("Scope", "created_by_me"),
            ("State", self.state),
            ("MRs", count),
        ]

    def _keys(self):
        return [
            ("q", "back"),
            ("/", "filter"),
            ("r", "refresh"),
            ("s", f"state:{self.state}"),
            ("g", "goto MR"),
            ("b", "browser"),
            ("y", "copy url"),
            ("enter", "view"),
        ]

    def compose(self) -> ComposeResult:
        yield K9sHeader(self._info_pairs(), self._keys(), id="header")
        yield Container(
            Input(placeholder="/  filter MRs...", id="mr-filter"),
            id="filter-bar",
        )
        yield DataTable(id="mr-table")
        yield StatusBar(self._status_text(), id="statusbar")

    def _status_text(self):
        total = len(self.mrs)
        shown = len(self.filtered_mrs)
        text_filter = ""
        try:
            text_filter = self.query_one("#mr-filter", Input).value.strip()
        except Exception:
            pass
        count = f"{shown}/{total} MRs" if shown != total else f"{total} MRs"
        parts = [count, ("state", self.state)]
        if text_filter:
            parts.append(("filter", text_filter))
        return _status_line(parts)

    def _refresh_status(self) -> None:
        try:
            sb = self.query_one("#statusbar", StatusBar)
            sb.set_text(self._status_text())
            sb.set_right(_auto_refresh_indicator(
                self.REFRESH_INTERVAL,
                active=self._refresh_timer is not None,
                refreshing=self._refreshing,
            ))
        except Exception:
            pass

    async def on_mount(self) -> None:
        table = self.query_one("#mr-table", DataTable)
        table.add_columns("IID", "Project", "Title", "MR", "Pipeline", "Unresolved", "Age")
        table.cursor_type = "row"
        await self.load_mrs()
        table.focus()
        self._refresh_timer = self.set_interval(self.REFRESH_INTERVAL, self._safe_refresh)
        self._refresh_status()
        self.api.config.save_last_view('my_mrs')

    async def load_mrs(self) -> None:
        self.mrs = await asyncio.to_thread(self.api.get_my_merge_requests, self.state)
        # Fetch unresolved counts in parallel for opened MRs only
        if self.state == 'opened' and self.mrs:
            todo = [(m['project_path'], m['iid']) for m in self.mrs if m['project_path']]
            results = await asyncio.gather(*(
                asyncio.to_thread(self.api.get_mr_unresolved_count, p, i) for p, i in todo
            ), return_exceptions=True)
            for (p, i), r in zip(todo, results):
                if isinstance(r, int):
                    self._unresolved_cache[(p, i)] = r
        self._apply_filter()
        try:
            self.query_one("#header", K9sHeader).set_info(self._info_pairs())
            self.query_one("#header", K9sHeader).set_keys(self._keys())
        except Exception:
            pass

    async def _safe_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        self._refresh_status()
        try:
            await self.load_mrs()
        except Exception:
            pass
        finally:
            self._refreshing = False
            self._refresh_status()

    def on_unmount(self) -> None:
        if self._refresh_timer:
            self._refresh_timer.stop()

    def _apply_filter(self) -> None:
        try:
            q = self.query_one("#mr-filter", Input).value.strip().lower()
        except Exception:
            q = ""
        if not q:
            self.filtered_mrs = list(self.mrs)
        else:
            self.filtered_mrs = [
                m for m in self.mrs
                if q in (m['title'] or '').lower()
                or q in (m['project_path'] or '').lower()
                or q in (m['author'] or '').lower()
                or q in str(m['iid'])
                or q in (m['source_branch'] or '').lower()
                or q in (m['target_branch'] or '').lower()
            ]
        self._update_table()
        self._refresh_status()

    def _update_table(self) -> None:
        table = self.query_one("#mr-table", DataTable)
        prev = table.cursor_row
        table.clear()
        for m in self.filtered_mrs:
            iid = Text(f"!{m['iid']}", style="bold #89b4fa")
            proj = Text((m['project_path'] or '')[:32], style="cyan")
            title = m['title'] or ''
            if m['draft']:
                title = f"[draft] {title}"
            title_t = Text(title[:50], style="bold #cdd6f4")
            unresolved = self._unresolved_cache.get((m['project_path'], m['iid']))
            if unresolved is None:
                unresolved_t = Text("—", style="dim")
            elif unresolved == 0:
                unresolved_t = Text("0", style="dim #a6e3a1")
            else:
                unresolved_t = Text(str(unresolved), style="bold #f38ba8")
            age = format_age(m['updated_at'] or m['created_at'])
            table.add_row(
                iid,
                proj,
                title_t,
                _mr_state_badge(m['state']),
                _pipeline_status_text(m['head_pipeline_status']),
                unresolved_t,
                Text(age, style="dim italic"),
            )
        if prev is not None and self.filtered_mrs:
            table.move_cursor(row=min(prev, len(self.filtered_mrs) - 1))

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "mr-filter":
            self._apply_filter()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "mr-filter":
            self._apply_filter()
            self.query_one("#mr-table", DataTable).focus()

    async def action_back(self) -> None:
        self.app.pop_screen()

    async def action_refresh(self) -> None:
        self._unresolved_cache.clear()
        await self.load_mrs()

    async def action_search(self) -> None:
        self.query_one("#mr-filter", Input).focus()

    async def action_toggle_state(self) -> None:
        try:
            idx = MR_STATE_CYCLE.index(self.state)
        except ValueError:
            idx = -1
        self.state = MR_STATE_CYCLE[(idx + 1) % len(MR_STATE_CYCLE)]
        self._unresolved_cache.clear()
        await self.load_mrs()

    async def action_goto(self) -> None:
        def _after(result):
            if result:
                project_path, iid = result
                asyncio.ensure_future(self._open_mr(project_path, iid))
        self.app.push_screen(MRPickerModal(recents=self.api.config.recent_projects), _after)

    async def _open_mr(self, project_path: str, iid: int) -> None:
        self.app.push_screen(MergeRequestDetailScreen(self.api, project_path, iid))

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if idx is not None and idx < len(self.filtered_mrs):
            m = self.filtered_mrs[idx]
            self.app.push_screen(MergeRequestDetailScreen(self.api, m['project_path'], m['iid']))

    async def action_browser(self) -> None:
        table = self.query_one("#mr-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self.filtered_mrs):
            webbrowser.open(self.filtered_mrs[table.cursor_row]['web_url'])

    async def action_yank(self) -> None:
        table = self.query_one("#mr-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self.filtered_mrs):
            m = self.filtered_mrs[table.cursor_row]
            if copy_to_clipboard(m['web_url']):
                self.notify(f"Copied MR !{m['iid']} URL", timeout=2)


class ProjectMergeRequestsScreen(ScreenBase):

    KEY_MAP = {"q": "back", "r": "refresh", "slash": "search", "b": "browser", "y": "yank", "s": "toggle_state"}

    REFRESH_INTERVAL = PIPELINE_REFRESH_INTERVAL
    STATE_CYCLE = ['merged', 'closed', 'opened', 'all']

    def __init__(self, api: GitLabAPI, project_path: str):
        super().__init__()
        self.api = api
        self.project_path = project_path
        self.mrs = []
        self.filtered_mrs = []
        self.state = 'merged'
        self._refresh_timer = None
        self._refreshing = False

    def _info_pairs(self):
        total = len(self.mrs)
        shown = len(self.filtered_mrs)
        count = f"{shown}/{total}" if shown != total else str(total)
        return [
            ("GitLab", self.api.config.gitlab_url),
            ("Project", self.project_path),
            ("State", self.state),
            ("MRs", count),
        ]

    def _keys(self):
        return [
            ("q", "back"),
            ("/", "filter"),
            ("r", "refresh"),
            ("s", f"state:{self.state}"),
            ("b", "browser"),
            ("y", "copy url"),
            ("enter", "view"),
        ]

    def compose(self) -> ComposeResult:
        yield K9sHeader(self._info_pairs(), self._keys(), id="header")
        yield Container(
            Input(placeholder="/  filter MRs...", id="proj-mr-filter"),
            id="filter-bar",
        )
        yield DataTable(id="proj-mr-table")
        yield StatusBar(self._status_text(), id="statusbar")

    def _status_text(self):
        total = len(self.mrs)
        shown = len(self.filtered_mrs)
        text_filter = ""
        try:
            text_filter = self.query_one("#proj-mr-filter", Input).value.strip()
        except Exception:
            pass
        count = f"{shown}/{total} MRs" if shown != total else f"{total} MRs"
        parts = [count, ("state", self.state)]
        if text_filter:
            parts.append(("filter", text_filter))
        return _status_line(parts)

    def _refresh_status(self) -> None:
        try:
            sb = self.query_one("#statusbar", StatusBar)
            sb.set_text(self._status_text())
            sb.set_right(_auto_refresh_indicator(
                self.REFRESH_INTERVAL,
                active=self._refresh_timer is not None,
                refreshing=self._refreshing,
            ))
        except Exception:
            pass

    async def on_mount(self) -> None:
        table = self.query_one("#proj-mr-table", DataTable)
        table.add_columns("IID", "Title", "MR", "Pipeline", "Author", "Age")
        table.cursor_type = "row"
        await self.load_mrs()
        table.focus()
        self._refresh_timer = self.set_interval(self.REFRESH_INTERVAL, self._safe_refresh)
        self._refresh_status()

    async def load_mrs(self) -> None:
        self.mrs = await asyncio.to_thread(self.api.get_project_merge_requests, self.project_path, self.state)
        self._apply_filter()
        try:
            self.query_one("#header", K9sHeader).set_info(self._info_pairs())
            self.query_one("#header", K9sHeader).set_keys(self._keys())
        except Exception:
            pass

    async def _safe_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        self._refresh_status()
        try:
            await self.load_mrs()
        except Exception:
            pass
        finally:
            self._refreshing = False
            self._refresh_status()

    def on_unmount(self) -> None:
        if self._refresh_timer:
            self._refresh_timer.stop()

    def _apply_filter(self) -> None:
        try:
            q = self.query_one("#proj-mr-filter", Input).value.strip().lower()
        except Exception:
            q = ""
        if not q:
            self.filtered_mrs = list(self.mrs)
        else:
            self.filtered_mrs = [
                m for m in self.mrs
                if q in (m['title'] or '').lower()
                or q in (m['author'] or '').lower()
                or q in str(m['iid'])
                or q in (m['source_branch'] or '').lower()
            ]
        self._update_table()
        self._refresh_status()

    def _update_table(self) -> None:
        table = self.query_one("#proj-mr-table", DataTable)
        prev = table.cursor_row
        table.clear()
        for m in self.filtered_mrs:
            iid = Text(f"!{m['iid']}", style="bold #89b4fa")
            title = m['title'] or ''
            if m['draft']:
                title = f"[draft] {title}"
            ref_age = m.get('merged_at') or m.get('closed_at') or m['updated_at']
            table.add_row(
                iid,
                Text(title[:60], style="bold #cdd6f4"),
                _mr_state_badge(m['state']),
                _pipeline_status_text(m['head_pipeline_status']),
                Text(m['author'] or '', style="dim"),
                Text(format_age(ref_age), style="dim italic"),
            )
        if prev is not None and self.filtered_mrs:
            table.move_cursor(row=min(prev, len(self.filtered_mrs) - 1))

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "proj-mr-filter":
            self._apply_filter()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "proj-mr-filter":
            self._apply_filter()
            self.query_one("#proj-mr-table", DataTable).focus()

    async def action_back(self) -> None:
        self.app.pop_screen()

    async def action_refresh(self) -> None:
        await self.load_mrs()

    async def action_search(self) -> None:
        self.query_one("#proj-mr-filter", Input).focus()

    async def action_toggle_state(self) -> None:
        try:
            idx = self.STATE_CYCLE.index(self.state)
        except ValueError:
            idx = -1
        self.state = self.STATE_CYCLE[(idx + 1) % len(self.STATE_CYCLE)]
        await self.load_mrs()

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if idx is not None and idx < len(self.filtered_mrs):
            m = self.filtered_mrs[idx]
            self.app.push_screen(MergeRequestDetailScreen(self.api, self.project_path, m['iid']))

    async def action_browser(self) -> None:
        table = self.query_one("#proj-mr-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self.filtered_mrs):
            webbrowser.open(self.filtered_mrs[table.cursor_row]['web_url'])

    async def action_yank(self) -> None:
        table = self.query_one("#proj-mr-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self.filtered_mrs):
            m = self.filtered_mrs[table.cursor_row]
            if copy_to_clipboard(m['web_url']):
                self.notify(f"Copied MR !{m['iid']} URL", timeout=2)


class MergeRequestDetailScreen(ScreenBase):

    KEY_MAP = {
        "q": "back",
        "r": "refresh",
        "b": "browser",
        "y": "yank",
        "p": "pipelines",
        "a": "approve",
        "x": "close",
        "c": "comment",
        "g": "goto",
        "f": "toggle_resolved",
        "t": "toggle_auto",
    }

    REFRESH_INTERVAL = PIPELINE_REFRESH_INTERVAL

    def __init__(self, api: GitLabAPI, project_path: str, iid: int):
        super().__init__()
        self.api = api
        self.project_path = project_path
        self.iid = iid
        self.mr = None
        self.discussions = []
        self.approval_rules = []
        self.show_resolved = False
        self._refresh_timer = None
        self._refreshing = False

    def _info_pairs(self):
        if not self.mr:
            return [
                ("GitLab", self.api.config.gitlab_url),
                ("Project", self.project_path),
                ("MR", f"!{self.iid}"),
                ("Status", "loading..."),
            ]
        m = self.mr
        title = (m['title'] or '')[:60]
        pipeline = m['head_pipeline_status'] or '—'
        return [
            ("GitLab", self.api.config.gitlab_url),
            ("Project", self.project_path),
            ("MR", f"!{m['iid']}  {title}"),
            ("State", m['state']),
            ("Pipeline", pipeline),
            ("Author", m['author'] or 'unknown'),
            ("Created", format_age(m['created_at'])),
        ]

    def _keys(self):
        resolved_label = "hide resolved" if self.show_resolved else "show resolved"
        auto_label = "auto off" if self._refresh_timer is not None else "auto on"
        return [
            ("q", "back"),
            ("r", "refresh"),
            ("p", "pipelines"),
            ("a", "approve"),
            ("x", "close"),
            ("c", "comment"),
            ("f", resolved_label),
            ("t", auto_label),
            ("g", "goto MR"),
            ("b", "browser"),
            ("y", "copy url"),
        ]

    def compose(self) -> ComposeResult:
        yield K9sHeader(self._info_pairs(), self._keys(), id="header")
        yield ScrollableContainer(
            Horizontal(
                Static("", id="mr-col-left", classes="mr-col"),
                Static("", id="mr-col-right", classes="mr-col"),
                id="mr-cols",
            ),
            Static("", id="mr-approvals"),
            Static("", id="mr-desc-heading", classes="mr-section"),
            Markdown("", id="mr-desc"),
            Static("", id="mr-disc-heading", classes="mr-section"),
            Static("", id="mr-disc"),
            id="mr-body-scroll",
        )
        yield StatusBar(self._status_text(), id="statusbar")

    def _status_text(self):
        if not self.mr:
            return _status_line(["loading..."])
        m = self.mr
        unresolved = sum(1 for d in self.discussions if d.get('unresolved'))
        parts = [
            ("commits", m.get('commits_count', 0)),
            ("pipelines", m.get('head_pipeline_id') and "1+" or "0"),
            ("changes", m.get('changes_count', 0)),
            ("approvals", f"{m.get('approvals_count', 0)}/{m.get('approvals_required', 0) or '-'}"),
            ("unresolved", unresolved),
        ]
        return _status_line(parts)

    def _refresh_status(self) -> None:
        try:
            sb = self.query_one("#statusbar", StatusBar)
            sb.set_text(self._status_text())
            sb.set_right(_auto_refresh_indicator(
                self.REFRESH_INTERVAL,
                active=self._refresh_timer is not None,
                refreshing=self._refreshing,
            ))
        except Exception:
            pass

    async def on_mount(self) -> None:
        if self.project_path:
            self.api.config.recent_projects.remember(self.project_path)
        await self.load_mr()
        if self.mr and self.mr.get('state') == 'opened':
            self._refresh_timer = self.set_interval(self.REFRESH_INTERVAL, self._safe_refresh)
        self._refresh_status()

    def on_unmount(self) -> None:
        if self._refresh_timer:
            self._refresh_timer.stop()

    async def load_mr(self) -> None:
        mr, discussions, approval_rules = await asyncio.gather(
            asyncio.to_thread(self.api.get_merge_request, self.project_path, self.iid),
            asyncio.to_thread(self.api.get_mr_discussions, self.project_path, self.iid),
            asyncio.to_thread(self.api.get_mr_approval_state, self.project_path, self.iid),
        )
        self.mr = mr
        self.discussions = discussions or []
        self.approval_rules = approval_rules or []
        try:
            self.query_one("#header", K9sHeader).set_info(self._info_pairs())
            self.query_one("#header", K9sHeader).set_keys(self._keys())
        except Exception:
            pass
        self._render_body()
        self._refresh_status()

    async def _safe_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        self._refresh_status()
        try:
            await self.load_mr()
        except Exception:
            pass
        finally:
            self._refreshing = False
            self._refresh_status()

    def _build_left_col(self) -> Text:
        m = self.mr
        rows = [
            ("ID", Text(f"!{m['iid']}  (id {m['id']})", style="#cdd6f4")),
            ("Title", Text(m['title'] or '', style="bold #cdd6f4")),
            ("Author", Text(m['author'] or 'unknown', style="#cdd6f4")),
            ("Source", Text(m['source_branch'], style="cyan")),
            ("Target", Text(m['target_branch'], style="cyan")),
        ]
        return self._format_kv_rows(rows)

    def _build_right_col(self) -> Text:
        m = self.mr
        state_t = Text(m['state'], style=f"bold {_mr_state_color(m['state'])}")
        pipeline_status = m.get('head_pipeline_status')
        pipeline_id = m.get('head_pipeline_id')
        pipeline_t = Text()
        if pipeline_status:
            pipeline_t.append(pipeline_status, style=f"bold {_pipeline_status_color(pipeline_status)}")
            if pipeline_id:
                pipeline_t.append(f"  -  {pipeline_id}", style="#a6adc8")
        else:
            pipeline_t.append("—", style="dim")
        created_t = Text(f"{m['created_at']}  ({format_age(m['created_at'])})", style="#cdd6f4")
        rows = [
            ("State", state_t),
            ("Pipeline", pipeline_t),
            ("Created", created_t),
        ]
        if m.get('merged_at'):
            rows.append(("Merged", Text(f"{m['merged_at']}  ({format_age(m['merged_at'])})", style="#cdd6f4")))
        elif m.get('closed_at') and m['state'] == 'closed':
            rows.append(("Closed", Text(f"{m['closed_at']}  ({format_age(m['closed_at'])})", style="#cdd6f4")))
        if m.get('has_conflicts'):
            rows.append(("Conflicts", Text("yes", style="bold #f38ba8")))
        return self._format_kv_rows(rows)

    @staticmethod
    def _format_kv_rows(rows) -> Text:
        if not rows:
            return Text("")
        label_w = max(len(label) for label, _ in rows)
        out = Text()
        for i, (label, value_text) in enumerate(rows):
            if i > 0:
                out.append("\n")
            out.append(f"{label.rjust(label_w)}: ", style="bold #f9e2af")
            out.append_text(value_text if isinstance(value_text, Text) else Text(str(value_text)))
        return out

    def _build_approvals(self) -> Text:
        m = self.mr
        approved = m.get('approvals_count', 0) or 0
        required = m.get('approvals_required', 0) or 0
        names = m.get('approved_by') or []
        out = Text()
        if not (required or approved or self.approval_rules):
            return out
        out.append("Approvals: ", style="bold #f9e2af")
        out.append(f"{approved}/{required if required else '-'}", style="bold #cdd6f4")
        if names:
            out.append("   ")
            out.append(" | ".join(names), style="bold #a6e3a1")
        for rule in self.approval_rules:
            if rule.get('rule_type') != 'code_owner':
                continue
            rr = rule.get('approvals_required', 0) or 0
            ra = rule.get('approved_by') or []
            if rr == 0 and not ra:
                continue
            out.append("\n   CODEOWNER ", style="bold #f9e2af")
            out.append(f"{len(ra)}/{rr}", style="bold #cdd6f4")
            if ra:
                out.append("   ")
                out.append(" | ".join(ra), style="bold #a6e3a1")
            else:
                out.append("   ")
                out.append("(needs approval)", style="bold #f38ba8")
            name = rule.get('name') or ''
            if name and name.lower() not in ('code owners', 'codeowners'):
                out.append("   ")
                out.append(f"[{name}]", style="dim #a6adc8")
        return out

    def _build_discussions(self) -> Text:
        visible = self.discussions if self.show_resolved else [
            d for d in self.discussions if d.get('unresolved') or not d.get('resolvable')
        ]
        non_system = [d for d in visible if not d.get('system')]
        out = Text()
        if not non_system:
            out.append("(no discussions)", style="dim italic #6c7086")
            return out
        for i, d in enumerate(non_system):
            if i > 0:
                out.append("\n\n")
            marker_style = "bold #f38ba8" if d.get('unresolved') else (
                "bold #a6e3a1" if d.get('resolvable') else "bold #89b4fa"
            )
            marker_label = "●" if d.get('unresolved') else ("✓" if d.get('resolvable') else "·")
            out.append(f"{marker_label} ", style=marker_style)
            out.append(d.get('first_author') or 'unknown', style="bold #cdd6f4")
            out.append("  ")
            out.append(format_age(d.get('first_created_at') or ''), style="dim italic #a6adc8")
            if d.get('resolvable'):
                out.append("  ")
                out.append(
                    "unresolved" if d.get('unresolved') else "resolved",
                    style="bold #f38ba8" if d.get('unresolved') else "dim #a6e3a1",
                )
            for note in d.get('notes', []):
                if note.get('system'):
                    continue
                body = (note.get('body') or '').strip()
                if not body:
                    continue
                out.append("\n")
                for j, line in enumerate(body.split('\n')):
                    if j > 0:
                        out.append("\n")
                    out.append(f"    {line}", style="#cdd6f4")
        return out

    def _disc_heading_text(self) -> Text:
        visible = self.discussions if self.show_resolved else [
            d for d in self.discussions if d.get('unresolved') or not d.get('resolvable')
        ]
        non_system = [d for d in visible if not d.get('system')]
        unresolved = sum(1 for d in self.discussions if d.get('unresolved'))
        total = len(self.discussions)
        t = Text()
        t.append(" DISCUSSIONS ", style="bold white on #89b4fa")
        t.append(f"  ({len(non_system)} shown · {unresolved} unresolved · {total} total)", style="dim #a6adc8")
        return t

    def _render_body(self) -> None:
        if not self.mr:
            try:
                self.query_one("#mr-col-left", Static).update(Text("MR not found", style="bold #f38ba8"))
                self.query_one("#mr-col-right", Static).update("")
                self.query_one("#mr-approvals", Static).update("")
                self.query_one("#mr-desc-heading", Static).update("")
                self.query_one("#mr-desc", Markdown).update("")
                self.query_one("#mr-disc-heading", Static).update("")
                self.query_one("#mr-disc", Static).update("")
            except Exception:
                pass
            return
        try:
            self.query_one("#mr-col-left", Static).update(self._build_left_col())
            self.query_one("#mr-col-right", Static).update(self._build_right_col())
            self.query_one("#mr-approvals", Static).update(self._build_approvals())
            self.query_one("#mr-desc-heading", Static).update(Text(" DESCRIPTION ", style="bold white on #89b4fa"))
            desc = (self.mr.get('description') or '').strip()
            self.query_one("#mr-desc", Markdown).update(desc if desc else "*(no description)*")
            self.query_one("#mr-disc-heading", Static).update(self._disc_heading_text())
            self.query_one("#mr-disc", Static).update(self._build_discussions())
        except Exception:
            pass

    async def action_back(self) -> None:
        self.app.pop_screen()

    async def action_refresh(self) -> None:
        await self.load_mr()

    async def action_browser(self) -> None:
        if self.mr:
            webbrowser.open(self.mr['web_url'])

    async def action_yank(self) -> None:
        if self.mr and copy_to_clipboard(self.mr['web_url']):
            self.notify(f"Copied MR !{self.mr['iid']} URL", timeout=2)

    async def action_pipelines(self) -> None:
        if not self.mr:
            return
        self.app.push_screen(MRPipelineListScreen(self.api, self.project_path, self.mr))

    async def action_toggle_resolved(self) -> None:
        self.show_resolved = not self.show_resolved
        try:
            self.query_one("#header", K9sHeader).set_keys(self._keys())
        except Exception:
            pass
        self._render_body()

    async def action_toggle_auto(self) -> None:
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
            self._refresh_timer = None
            self.notify("Auto-refresh off", timeout=2)
        else:
            self._refresh_timer = self.set_interval(self.REFRESH_INTERVAL, self._safe_refresh)
            self.notify(f"Auto-refresh on ({self.REFRESH_INTERVAL}s)", timeout=2)
        try:
            self.query_one("#header", K9sHeader).set_keys(self._keys())
        except Exception:
            pass
        self._refresh_status()

    async def action_goto(self) -> None:
        def _after(result):
            if result:
                project_path, iid = result
                asyncio.ensure_future(self._open_mr(project_path, iid))
        self.app.push_screen(MRPickerModal(default_project=self.project_path, recents=self.api.config.recent_projects), _after)

    async def _open_mr(self, project_path: str, iid: int) -> None:
        self.app.push_screen(MergeRequestDetailScreen(self.api, project_path, iid))

    async def action_approve(self) -> None:
        if not self.mr:
            return
        if self.mr['state'] != 'opened':
            self.notify(f"Cannot approve — MR is {self.mr['state']}", severity="warning", timeout=3)
            return

        def _after(confirmed):
            if confirmed:
                asyncio.ensure_future(self._do_approve())

        modal = ConfirmModal(
            f"Approve MR !{self.mr['iid']}?",
            detail=(self.mr['title'] or '')[:60],
        )
        self.app.push_screen(modal, _after)

    async def _do_approve(self) -> None:
        try:
            await asyncio.to_thread(self.api.approve_merge_request, self.project_path, self.iid)
            self.notify(f"Approved MR !{self.iid}", timeout=2)
            await self.load_mr()
        except Exception as e:
            self.notify(f"Approve failed: {e}", severity="error", timeout=4)

    async def action_close(self) -> None:
        if not self.mr:
            return
        if self.mr['state'] != 'opened':
            self.notify(f"Cannot close — MR is {self.mr['state']}", severity="warning", timeout=3)
            return

        def _after(confirmed):
            if confirmed:
                asyncio.ensure_future(self._do_close())

        modal = ConfirmModal(
            f"Close MR !{self.mr['iid']}?",
            detail=(self.mr['title'] or '')[:60],
        )
        self.app.push_screen(modal, _after)

    async def _do_close(self) -> None:
        try:
            await asyncio.to_thread(self.api.close_merge_request, self.project_path, self.iid)
            self.notify(f"Closed MR !{self.iid}", timeout=2)
            await self.load_mr()
        except Exception as e:
            self.notify(f"Close failed: {e}", severity="error", timeout=4)

    async def action_comment(self) -> None:
        if not self.mr:
            return

        def _after(body):
            if body:
                asyncio.ensure_future(self._do_comment(body))

        modal = TextInputModal(
            f"Comment on MR !{self.iid}",
            placeholder="Type your comment...",
        )
        self.app.push_screen(modal, _after)

    async def _do_comment(self, body: str) -> None:
        try:
            await asyncio.to_thread(self.api.create_mr_note, self.project_path, self.iid, body)
            self.notify("Comment posted", timeout=2)
            await self.load_mr()
        except Exception as e:
            self.notify(f"Comment failed: {e}", severity="error", timeout=4)


class MRPipelineListScreen(ScreenBase):

    KEY_MAP = {"q": "back", "r": "refresh", "b": "browser", "y": "yank"}

    REFRESH_INTERVAL = PIPELINE_REFRESH_INTERVAL

    def __init__(self, api: GitLabAPI, project_path: str, mr: dict):
        super().__init__()
        self.api = api
        self.project_path = project_path
        self.mr = mr
        self.pipelines = []
        self._refresh_timer = None
        self._refreshing = False

    def _info_pairs(self):
        return [
            ("GitLab", self.api.config.gitlab_url),
            ("Project", self.project_path),
            ("MR", f"!{self.mr['iid']}  {(self.mr.get('title') or '')[:40]}"),
            ("Source", self.mr.get('source_branch') or ''),
            ("Pipelines", str(len(self.pipelines))),
        ]

    def _keys(self):
        return [
            ("q", "back"),
            ("r", "refresh"),
            ("b", "browser"),
            ("y", "copy url"),
            ("enter", "jobs"),
        ]

    def compose(self) -> ComposeResult:
        yield K9sHeader(self._info_pairs(), self._keys(), id="header")
        yield DataTable(id="mr-pipeline-table")
        yield StatusBar(self._status_text(), id="statusbar")

    def _status_text(self):
        return _status_line([f"{len(self.pipelines)} pipelines", ("MR", f"!{self.mr['iid']}")])

    def _refresh_status(self) -> None:
        try:
            sb = self.query_one("#statusbar", StatusBar)
            sb.set_text(self._status_text())
            sb.set_right(_auto_refresh_indicator(
                self.REFRESH_INTERVAL,
                active=self._refresh_timer is not None,
                refreshing=self._refreshing,
            ))
        except Exception:
            pass

    async def on_mount(self) -> None:
        table = self.query_one("#mr-pipeline-table", DataTable)
        table.add_columns("ID", "Status", "Ref", "SHA", "Last Run")
        table.cursor_type = "row"
        await self.load_pipelines()
        table.focus()
        # auto-refresh only if any pipeline is active
        if any(p['status'] not in TERMINAL_STATUSES for p in self.pipelines):
            self._refresh_timer = self.set_interval(self.REFRESH_INTERVAL, self._safe_refresh)
        self._refresh_status()

    async def load_pipelines(self) -> None:
        self.pipelines = await asyncio.to_thread(self.api.get_mr_pipelines, self.project_path, self.mr['iid'])
        # head pipeline first (most recent) — gitlab returns desc by default
        self._update_table()
        try:
            self.query_one("#header", K9sHeader).set_info(self._info_pairs())
        except Exception:
            pass

    async def _safe_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        self._refresh_status()
        try:
            await self.load_pipelines()
        except Exception:
            pass
        finally:
            self._refreshing = False
            self._refresh_status()

    def on_unmount(self) -> None:
        if self._refresh_timer:
            self._refresh_timer.stop()

    def _update_table(self) -> None:
        table = self.query_one("#mr-pipeline-table", DataTable)
        prev = table.cursor_row
        table.clear()
        for i, p in enumerate(self.pipelines):
            id_text = Text(str(p['id']), style="bold #89b4fa" if i == 0 else "bold")
            ref_text = Text((p['ref'] or '')[:40], style="cyan")
            table.add_row(
                id_text,
                status_badge(p['status']),
                ref_text,
                Text(p['sha'], style="dim"),
                Text(format_age(p['updated_at'] or p['created_at']), style="dim italic"),
            )
        if prev is not None and self.pipelines:
            table.move_cursor(row=min(prev, len(self.pipelines) - 1))
        self._refresh_status()

    async def action_back(self) -> None:
        self.app.pop_screen()

    async def action_refresh(self) -> None:
        await self.load_pipelines()

    async def action_browser(self) -> None:
        table = self.query_one("#mr-pipeline-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self.pipelines):
            url = self.pipelines[table.cursor_row].get('web_url')
            if url:
                webbrowser.open(url)

    async def action_yank(self) -> None:
        table = self.query_one("#mr-pipeline-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self.pipelines):
            url = self.pipelines[table.cursor_row].get('web_url')
            if url and copy_to_clipboard(url):
                self.notify("Copied URL", timeout=2)

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if idx is None or idx >= len(self.pipelines):
            return
        pipeline = self.pipelines[idx]
        # Ensure api is bound to the MR's project so JobListScreen calls work
        ds_api = GitLabAPI(self.api.config)
        try:
            ds_api.set_project(self.project_path)
        except Exception:
            self.notify(f"Cannot access {self.project_path}", severity="error", timeout=3)
            return
        self.app.push_screen(JobListScreen(ds_api, pipeline))


# -- modals ------------------------------------------------------------------

class ConfirmModal(ModalScreen[bool]):
    """y/N confirmation modal. Default N — only y/Y returns True."""

    def __init__(self, message: str, detail: str = ""):
        super().__init__()
        self.message = message
        self.detail = detail

    def compose(self) -> ComposeResult:
        body = Text()
        body.append(self.message, style="bold #cdd6f4")
        if self.detail:
            body.append("\n")
            body.append(self.detail, style="#a6adc8")
        body.append("\n\n")
        body.append("[y/", style="#a6adc8")
        body.append("N", style="bold #f9e2af")
        body.append("]", style="#a6adc8")
        yield Container(Static(body, id="confirm-text"), id="confirm-box")

    def on_key(self, event) -> None:
        event.prevent_default()
        event.stop()
        self.dismiss(event.key.lower() == "y")


class TextInputModal(ModalScreen[str | None]):
    """Multi-line input modal. Returns text on ctrl+s, None on escape."""

    def __init__(self, title: str, placeholder: str = "", initial: str = ""):
        super().__init__()
        self.title_text = title
        self.placeholder = placeholder
        self.initial = initial

    def compose(self) -> ComposeResult:
        header = Text()
        header.append(self.title_text, style="bold #cdd6f4")
        header.append("\n")
        header.append("ctrl+s submit · esc cancel", style="dim #a6adc8")
        ta = TextArea(self.initial, id="text-input-area")
        ta.show_line_numbers = False
        yield Container(
            Static(header, id="text-input-header"),
            ta,
            id="text-input-box",
        )

    def on_mount(self) -> None:
        try:
            self.query_one("#text-input-area", TextArea).focus()
        except Exception:
            pass

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.prevent_default()
            event.stop()
            self.dismiss(None)
        elif event.key == "ctrl+s":
            event.prevent_default()
            event.stop()
            try:
                text = self.query_one("#text-input-area", TextArea).text
            except Exception:
                text = ""
            text = text.strip()
            self.dismiss(text if text else None)


class MRPickerModal(ModalScreen[tuple[str, int] | None]):
    """Pick MR by project path + ID. Ghost-text autocomplete from recents. Returns (project_path, iid) on submit."""

    def __init__(self, default_project: str = "", recents=None):
        super().__init__()
        self.default_project = default_project
        self.recents_store = recents
        self.all_recents = list(recents.list()) if recents else []

    def compose(self) -> ComposeResult:
        header = Text()
        header.append("Open MR by ID", style="bold #cdd6f4")
        header.append("\n")
        header.append("→/tab accept suggestion · ctrl+d remove current from recents · esc cancel",
                      style="dim #a6adc8")
        suggester = SuggestFromList(self.all_recents, case_sensitive=False) if self.all_recents else None
        yield Container(
            Static(header, id="picker-header"),
            Label("Project", classes="picker-label"),
            Input(
                value=self.default_project,
                placeholder="group/project",
                id="picker-project",
                suggester=suggester,
            ),
            Label("MR ID", classes="picker-label"),
            Input(placeholder="123", id="picker-iid"),
            Static("", id="picker-error"),
            id="picker-box",
        )

    def on_mount(self) -> None:
        try:
            target = "#picker-iid" if self.default_project else "#picker-project"
            self.query_one(target, Input).focus()
        except Exception:
            pass

    def _submit(self) -> None:
        try:
            proj = self.query_one("#picker-project", Input).value.strip()
            iid_str = self.query_one("#picker-iid", Input).value.strip()
        except Exception:
            return
        if not proj or not iid_str:
            self._set_error("project and id required")
            return
        try:
            iid = int(iid_str)
        except ValueError:
            self._set_error("id must be integer")
            return
        if self.recents_store:
            self.recents_store.remember(proj)
        self.dismiss((proj, iid))

    def _set_error(self, msg: str) -> None:
        try:
            self.query_one("#picker-error", Static).update(Text(msg, style="bold #f38ba8"))
        except Exception:
            pass

    def _remove_current_recent(self) -> None:
        try:
            current = self.query_one("#picker-project", Input).value.strip()
        except Exception:
            return
        if not current:
            return
        match = next((p for p in self.all_recents if p.lower() == current.lower()), None)
        if not match:
            self.notify(f"'{current}' not in recents", severity="warning", timeout=2)
            return
        if self.recents_store:
            self.recents_store.remove(match)
        self.all_recents.remove(match)
        # rebuild suggester w/ updated list
        try:
            inp = self.query_one("#picker-project", Input)
            inp.suggester = SuggestFromList(self.all_recents, case_sensitive=False) if self.all_recents else None
            inp.value = ""
        except Exception:
            pass
        self.notify(f"Removed {match} from recents", timeout=2)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "picker-project":
            try:
                self.query_one("#picker-iid", Input).focus()
            except Exception:
                pass
        elif event.input.id == "picker-iid":
            self._submit()

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.prevent_default()
            event.stop()
            self.dismiss(None)
        elif event.key == "ctrl+d":
            event.prevent_default()
            event.stop()
            self._remove_current_recent()


# -- app ---------------------------------------------------------------------

class PipelineMonitor(App):

    TITLE = "glmon"

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
    ]

    CSS = """
    Screen {
        background: #1e1e2e;
    }

    K9sHeader {
        height: auto;
        background: #181825;
        padding: 1 2;
        margin-bottom: 1;
        color: #cdd6f4;
    }

    #filter-bar {
        height: 3;
        background: #1e1e2e;
        padding: 0 1;
        margin-bottom: 1;
    }

    #filter-bar Input {
        width: 100%;
        background: #313244;
        border: none;
        color: #cdd6f4;
    }

    #filter-bar Input:focus {
        border: tall #89b4fa;
    }

    #keybar {
        dock: bottom;
        height: 1;
        background: #313244;
        color: #a6adc8;
        padding: 0 0;
    }

    #statusbar {
        dock: bottom;
        height: 1;
        background: #313244;
        color: #a6adc8;
        padding: 0 1;
    }

    #statusbar #status-left {
        width: 1fr;
        background: #313244;
        color: #a6adc8;
    }

    #statusbar #status-right {
        width: auto;
        background: #313244;
        color: #a6adc8;
        content-align: right middle;
    }

    DataTable {
        background: #1e1e2e;
        height: 1fr;
    }

    DataTable > .datatable--header {
        background: #181825;
        color: #a6adc8;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: #45475a;
        color: #cdd6f4;
    }

    RichLog {
        background: #11111b;
        color: #cdd6f4;
        padding: 1 2;
        height: 1fr;
        scrollbar-size: 1 1;
    }

    #info-container {
        dock: top;
        height: 2;
        background: #313244;
        padding: 0 2;
        color: #cdd6f4;
    }

    #splash {
        width: 100%;
        height: 1fr;
        background: #1e1e2e;
        color: #89b4fa;
    }

    ConfirmModal {
        align: center middle;
        background: #1e1e2e 60%;
    }

    #confirm-box {
        width: 60;
        height: auto;
        background: #313244;
        border: tall #f9e2af;
        padding: 1 2;
    }

    #confirm-text {
        width: 100%;
        height: auto;
        color: #cdd6f4;
    }

    TextInputModal {
        align: center middle;
        background: #1e1e2e 60%;
    }

    #text-input-box {
        width: 80;
        height: 24;
        background: #313244;
        border: tall #89b4fa;
        padding: 1 2;
    }

    #text-input-header {
        width: 100%;
        height: auto;
        color: #cdd6f4;
        margin-bottom: 1;
    }

    #text-input-area {
        width: 100%;
        height: 1fr;
        background: #1e1e2e;
        color: #cdd6f4;
    }

    MRPickerModal {
        align: center middle;
        background: #1e1e2e 60%;
    }

    #picker-box {
        width: 70;
        height: auto;
        background: #313244;
        border: tall #89b4fa;
        padding: 1 2;
    }

    #picker-header {
        width: 100%;
        height: auto;
        color: #cdd6f4;
        margin-bottom: 1;
    }

    .picker-label {
        color: #f9e2af;
        text-style: bold;
        margin-top: 1;
    }

    #picker-project, #picker-iid {
        width: 100%;
        background: #1e1e2e;
        color: #cdd6f4;
        border: tall #45475a;
    }

    #picker-project:focus, #picker-iid:focus {
        border: tall #89b4fa;
    }

    #picker-error {
        width: 100%;
        height: auto;
        color: #f38ba8;
        margin-top: 1;
    }

    #mr-body-scroll {
        background: #1e1e2e;
        color: #cdd6f4;
        padding: 1 2;
        height: 1fr;
        scrollbar-size: 1 1;
    }

    #mr-cols {
        width: 100%;
        height: auto;
        background: #1e1e2e;
        margin-bottom: 1;
    }

    .mr-col {
        width: 1fr;
        height: auto;
        background: #1e1e2e;
        color: #cdd6f4;
        padding: 0 2 0 0;
    }

    #mr-approvals {
        width: 100%;
        height: auto;
        background: #181825;
        color: #cdd6f4;
        padding: 1 2;
        margin: 1 0;
    }

    .mr-section {
        width: 100%;
        height: auto;
        background: #1e1e2e;
        color: #cdd6f4;
        margin-top: 1;
    }

    #mr-desc {
        width: 100%;
        height: auto;
        background: #1e1e2e;
        color: #cdd6f4;
        margin-bottom: 1;
    }

    #mr-desc MarkdownH1, #mr-desc MarkdownH2, #mr-desc MarkdownH3 {
        background: #1e1e2e;
        color: #89b4fa;
    }

    #mr-desc MarkdownFence, #mr-desc MarkdownCode {
        background: #181825;
        color: #f9e2af;
    }

    #mr-disc {
        width: 100%;
        height: auto;
        background: #1e1e2e;
        color: #cdd6f4;
        padding: 1 0;
    }
    """

    def __init__(self, config: Config, default_age_days=DEFAULT_PIPELINE_AGE_DAYS, default_branch_filter: str = "", resume: bool = True, explicit_project: bool = False):
        super().__init__()
        self.config = config
        self.api = GitLabAPI(config)
        self.default_age_days = default_age_days
        self.default_branch_filter = default_branch_filter or ""
        self.resume = resume
        self.explicit_project = explicit_project

    def action_quit(self) -> None:
        self.exit()

    async def on_mount(self) -> None:
        self.push_screen(LoadingScreen())
        # yield a frame so the splash paints before blocking API calls
        self.set_timer(0.1, self._finish_loading)

    async def _finish_loading(self) -> None:
        try:
            await asyncio.to_thread(self.api.connect_project)
        except Exception:
            self.switch_screen(ProjectSelectScreen(self.api, self.config.favorites))
            self.notify(f"Project not found: {self.config.project_path}", severity="error", timeout=5)
            return
        self.switch_screen(ProjectSelectScreen(self.api, self.config.favorites))
        # explicit -p / GITLAB_PROJECT wins over resume
        if self.explicit_project and self.api.project:
            self.push_screen(PipelineListScreen(
                self.api,
                age_days=self.default_age_days,
                initial_filter=self.default_branch_filter,
            ))
            return
        if not self.resume:
            return
        last = self.config.get_last_view()
        if not last:
            # legacy fallback: auto-jump to default project if GITLAB_PROJECT set
            if self.api.project:
                self.push_screen(PipelineListScreen(
                    self.api,
                    age_days=self.default_age_days,
                    initial_filter=self.default_branch_filter,
                ))
            return
        view_type = last.get('type')
        try:
            if view_type == 'my_mrs':
                self.push_screen(MyMergeRequestsScreen(self.api))
            elif view_type == 'pipelines':
                proj = last.get('project')
                if proj:
                    try:
                        await asyncio.to_thread(self.api.set_project, proj)
                        self.push_screen(PipelineListScreen(
                            self.api,
                            age_days=self.default_age_days,
                            initial_filter=self.default_branch_filter,
                        ))
                    except Exception:
                        self.notify(f"Could not resume project {proj}", severity="warning", timeout=3)
        except Exception:
            pass


def _detect_cwd_branch() -> str:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""


def main():
    parser = argparse.ArgumentParser(prog="glmon", description="GitLab pipeline monitor TUI")
    parser.add_argument("-p", "--project", help="Project path (group/project) to jump into directly")
    parser.add_argument("--days", type=int, default=DEFAULT_PIPELINE_AGE_DAYS,
                        help=f"Default pipeline age window in days (default {DEFAULT_PIPELINE_AGE_DAYS})")
    parser.add_argument("-b", "--branch",
                        help="Pre-fill pipeline filter with this branch (clearable in the UI)")
    parser.add_argument("-B", "--cwd-branch", action="store_true",
                        help="Pre-fill pipeline filter with current git branch from CWD")
    parser.add_argument("--no-resume", action="store_true",
                        help="Skip restoring last view (MR list / pipelines)")
    args = parser.parse_args()

    branch_filter = ""
    if args.branch:
        branch_filter = args.branch.strip()
    elif args.cwd_branch:
        branch_filter = _detect_cwd_branch()
        if not branch_filter:
            print("Warning: --cwd-branch set but no git branch detected in CWD", file=sys.stderr)

    config = Config()
    if args.project:
        config._config['project_path'] = args.project

    valid, message = config.validate()
    if not valid:
        print(f"Error: {message}", file=sys.stderr)
        print("\nRequired environment variables:")
        print("  export GITLAB_URL=https://gitlab.example.com")
        print("  export GITLAB_TOKEN=your_personal_access_token")
        print("\nOptional (skips project picker):")
        print("  export GITLAB_PROJECT=group/project")
        print("  --project group/project")
        sys.exit(1)

    app = PipelineMonitor(
        config,
        default_age_days=args.days,
        default_branch_filter=branch_filter,
        resume=not args.no_resume,
        explicit_project=bool(args.project),
    )
    app.run()


if __name__ == "__main__":
    main()
