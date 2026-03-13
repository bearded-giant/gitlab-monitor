#!/usr/bin/env python3
# Copyright 2024 BeardedGiant
# https://github.com/bearded-giant/gitlab-tools
# Licensed under Apache License 2.0

import sys
import subprocess
import gitlab
import webbrowser
from datetime import datetime
import re
import asyncio
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Static, Input, RichLog
from textual.containers import Container, Horizontal, ScrollableContainer
from textual.screen import Screen
from textual.binding import Binding
from rich.text import Text

from .config import Config


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
    'success':  ('bold white on #a6e3a1', ' success '),
    'failed':   ('bold white on #f38ba8', ' failed '),
    'running':  ('bold white on #f9e2af', ' running '),
    'pending':  ('bold white on #585b70', ' pending '),
    'skipped':  ('bold white on #45475a', ' skipped '),
    'canceled':  ('bold white on #585b70', ' canceled '),
    'created':  ('bold white on #45475a', ' created '),
    'manual':   ('bold white on #89b4fa', ' manual '),
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


# -- breadcrumb bar ----------------------------------------------------------

class Breadcrumb(Static):
    pass


class KeyBar(Static):
    pass


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
            'last_activity': p.last_activity_at,
        } for p in projects]

    def get_recent_pipelines(self, limit=50, ref=None, username=None):
        params = {'per_page': limit, 'order_by': 'id', 'sort': 'desc'}
        if ref:
            params['ref'] = ref
        if username:
            params['username'] = username
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

    KEY_MAP = {"q": "quit", "r": "refresh", "slash": "search"}

    DEBOUNCE_SECONDS = 0.4

    def __init__(self, api: GitLabAPI):
        super().__init__()
        self.api = api
        self.projects = []
        self._search_timer = None

    def compose(self) -> ComposeResult:
        yield Breadcrumb("  Projects", id="breadcrumb")
        yield Container(
            Input(placeholder="/  filter projects...", id="project-search"),
            id="filter-bar",
        )
        yield DataTable(id="project-table")
        yield KeyBar(
            "  q quit  /  filter  r refresh  enter select",
            id="keybar",
        )

    async def on_mount(self) -> None:
        table = self.query_one("#project-table", DataTable)
        table.add_columns("Project", "Description", "Last Activity")
        table.cursor_type = "row"
        await self.load_projects()
        table.focus()

    async def load_projects(self, search=None) -> None:
        self.projects = await asyncio.to_thread(self.api.get_projects, search)
        table = self.query_one("#project-table", DataTable)
        table.clear()
        for p in self.projects:
            age = format_age(p['last_activity'])
            desc = (p['description'] or '')[:40]
            table.add_row(
                Text(p['path'], style="bold"),
                Text(desc, style="dim"),
                Text(age, style="dim italic"),
            )

    def _schedule_search(self) -> None:
        if self._search_timer is not None:
            self._search_timer.stop()
        self._search_timer = self.set_timer(self.DEBOUNCE_SECONDS, self._do_search)

    async def _do_search(self) -> None:
        query = self.query_one("#project-search", Input).value.strip() or None
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

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if idx is not None and idx < len(self.projects):
            project = self.projects[idx]
            self.api.set_project(project['path'])
            self.app.push_screen(PipelineListScreen(self.api))


class PipelineListScreen(ScreenBase):

    KEY_MAP = {"q": "back", "r": "refresh", "slash": "search", "b": "browser", "y": "yank"}

    def __init__(self, api: GitLabAPI):
        super().__init__()
        self.api = api
        self.pipelines = []
        self.filtered_pipelines = []
        self._refresh_timer = None
        self._refreshing = False

    def compose(self) -> ComposeResult:
        name = self.api.project_name or "project"
        yield Breadcrumb(f"  Projects > [bold]{name}[/bold] > Pipelines", id="breadcrumb")
        yield Container(
            Input(placeholder="/  filter pipelines...", id="pipeline-filter"),
            id="filter-bar",
        )
        yield DataTable(id="pipeline-table")
        yield KeyBar(
            "  q back  /  filter  r refresh  b browser  y copy url  enter jobs",
            id="keybar",
        )

    async def on_mount(self) -> None:
        table = self.query_one("#pipeline-table", DataTable)
        table.add_columns("ID", "Status", "Branch", "SHA", "Age", "User")
        table.cursor_type = "row"
        await self.load_pipelines()
        table.focus()
        self._refresh_timer = self.set_interval(10, self._safe_refresh)

    async def load_pipelines(self) -> None:
        self.pipelines = await asyncio.to_thread(self.api.get_recent_pipelines)
        self._apply_filter()

    async def _safe_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            await self.load_pipelines()
        except Exception:
            pass
        finally:
            self._refreshing = False

    def on_unmount(self) -> None:
        if self._refresh_timer:
            self._refresh_timer.stop()

    def _apply_filter(self) -> None:
        query = self.query_one("#pipeline-filter", Input).value.strip().lower()
        if query:
            self.filtered_pipelines = [
                p for p in self.pipelines
                if query in p['ref'].lower()
                or query in p['status'].lower()
                or query in p['user'].lower()
                or query in str(p['id'])
                or query in p['sha'].lower()
            ]
        else:
            self.filtered_pipelines = self.pipelines
        self._update_table()

    def _update_table(self) -> None:
        table = self.query_one("#pipeline-table", DataTable)
        prev_row = table.cursor_row
        table.clear()
        for p in self.filtered_pipelines:
            age = format_age(p['created_at'])
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

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if idx is not None and idx < len(self.filtered_pipelines):
            pipeline = self.filtered_pipelines[idx]
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


class JobListScreen(ScreenBase):

    KEY_MAP = {"q": "back", "r": "refresh", "slash": "search", "b": "browser", "f": "failures", "y": "yank"}

    STAGE_ORDER = ['build', 'test', 'deploy', 'cleanup']

    def __init__(self, api: GitLabAPI, pipeline: dict):
        super().__init__()
        self.api = api
        self.pipeline = pipeline
        self.jobs = []
        self.filtered_jobs = []
        self._refresh_timer = None
        self._refreshing = False

    def compose(self) -> ComposeResult:
        name = self.api.project_name or "project"
        pid = self.pipeline['id']
        ref = self.pipeline['ref'][:20]
        yield Breadcrumb(
            f"  Projects > {name} > [bold]#{pid}[/bold] [dim]{ref}[/dim]",
            id="breadcrumb",
        )
        yield Container(
            Input(placeholder="/  filter jobs...", id="job-filter"),
            id="filter-bar",
        )
        yield DataTable(id="job-table")
        yield KeyBar(
            "  q back  /  filter  r refresh  b browser  f failures  y copy url  enter logs",
            id="keybar",
        )

    async def on_mount(self) -> None:
        table = self.query_one("#job-table", DataTable)
        table.add_columns("Stage", "Name", "Status", "Duration", "ID")
        table.cursor_type = "row"
        await self.load_jobs()
        table.focus()
        self._refresh_timer = self.set_interval(10, self._safe_refresh)

    async def load_jobs(self) -> None:
        self.jobs = await asyncio.to_thread(self.api.get_pipeline_jobs, self.pipeline['id'])
        order = self.STAGE_ORDER
        self.jobs.sort(key=lambda j: (
            order.index(j['stage']) if j['stage'] in order else len(order),
            j['name'],
        ))
        self._apply_filter()

    def _apply_filter(self) -> None:
        query = self.query_one("#job-filter", Input).value.strip().lower()
        if query:
            self.filtered_jobs = [
                j for j in self.jobs
                if query in j['name'].lower()
                or query in j['stage'].lower()
                or query in j['status'].lower()
                or query in str(j['id'])
            ]
        else:
            self.filtered_jobs = self.jobs
        self._update_table()

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
        try:
            await self.load_jobs()
        except Exception:
            pass
        finally:
            self._refreshing = False

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
        self._refreshing = False

    def compose(self) -> ComposeResult:
        name = self.job['name']
        jid = self.job['id']
        yield Breadcrumb(f"  Job [bold]#{jid}[/bold] [dim]{name}[/dim]", id="breadcrumb")
        yield Static("", id="job-info-bar")
        yield ScrollableContainer(
            RichLog(id="job-log", wrap=True, max_lines=MAX_LOG_LINES),
            id="log-container",
        )
        yield KeyBar(
            "  q back  r refresh  b browser  f failures only  y copy",
            id="keybar",
        )

    def _update_info_bar(self) -> None:
        bar = self.query_one("#job-info-bar", Static)
        status = self.job.get('status', '?')
        dur = format_duration(self.job.get('duration'))
        style, label = STATUS_STYLES.get(status, ('', f' {status} '))
        bar.update(f"  Status: [{style}]{label}[/]  Duration: [bold]{dur}[/bold]")

    async def on_mount(self) -> None:
        await self.load_trace()
        self._update_info_bar()
        if self.job.get('status') not in TERMINAL_STATUSES:
            self._refresh_timer = self.set_interval(5, self._auto_refresh)

    async def _auto_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            fresh = await asyncio.to_thread(self.api.get_job, self.job['id'])
            if fresh:
                self.job = fresh
            self._update_info_bar()
            await self._append_new_trace()
            if self.job.get('status') in TERMINAL_STATUSES and self._refresh_timer:
                self._refresh_timer.stop()
                self._refresh_timer = None
        except Exception:
            pass
        finally:
            self._refreshing = False

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

    def compose(self) -> ComposeResult:
        pid = self.pipeline['id']
        yield Breadcrumb(f"  Pipeline [bold]#{pid}[/bold] > Failed Jobs", id="breadcrumb")
        yield ScrollableContainer(
            RichLog(id="failures-log", wrap=True, max_lines=MAX_LOG_LINES),
            id="log-container",
        )
        yield KeyBar("  q back  y copy", id="keybar")

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

    #breadcrumb {
        dock: top;
        height: 1;
        background: #313244;
        color: #cdd6f4;
        padding: 0 0;
    }

    #filter-bar {
        dock: top;
        height: 3;
        background: #1e1e2e;
        padding: 0 1;
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

    #log-container {
        background: #11111b;
        padding: 1 2;
        height: 1fr;
    }

    RichLog {
        background: #11111b;
        color: #cdd6f4;
    }

    #info-container {
        dock: top;
        height: 2;
        background: #313244;
        padding: 0 2;
        color: #cdd6f4;
    }

    #job-info-bar {
        dock: top;
        height: 1;
        background: #181825;
        color: #cdd6f4;
        padding: 0 0;
    }

    #splash {
        width: 100%;
        height: 1fr;
        background: #1e1e2e;
        color: #89b4fa;
    }
    """

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.api = GitLabAPI(config)

    def action_quit(self) -> None:
        self.exit()

    async def on_mount(self) -> None:
        self.push_screen(LoadingScreen())
        # yield a frame so the splash paints before blocking API calls
        self.set_timer(0.1, self._finish_loading)

    async def _finish_loading(self) -> None:
        await asyncio.to_thread(self.api.connect_project)
        if self.api.project:
            self.switch_screen(PipelineListScreen(self.api))
        else:
            self.switch_screen(ProjectSelectScreen(self.api))


def main():
    config = Config()
    valid, message = config.validate()
    if not valid:
        print(f"Error: {message}", file=sys.stderr)
        print("\nRequired environment variables:")
        print("  export GITLAB_URL=https://gitlab.example.com")
        print("  export GITLAB_TOKEN=your_personal_access_token")
        print("\nOptional (skips project picker):")
        print("  export GITLAB_PROJECT=group/project")
        sys.exit(1)
    app = PipelineMonitor(config)
    app.run()


if __name__ == "__main__":
    main()
