#!/usr/bin/env python3
# Copyright 2024 BeardedGiant
# https://github.com/bearded-giant/gitlab-tools
# Licensed under Apache License 2.0

import sys
import argparse
import subprocess
import webbrowser
from datetime import datetime
import asyncio
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Static, Input, RichLog, TextArea, Label, Markdown
from textual.containers import Container, Horizontal, ScrollableContainer
from textual.suggester import SuggestFromList
from textual.screen import Screen, ModalScreen
from textual.binding import Binding
from rich.text import Text

from .config import Config
from . import __version__
from .constants import (
    PIPELINE_AGE_CYCLE,
    DEFAULT_PIPELINE_AGE_DAYS,
    PIPELINE_REFRESH_INTERVAL,
    JOB_REFRESH_INTERVAL,
    LOG_META_REFRESH_INTERVAL,
    LOG_TRACE_REFRESH_INTERVAL,
    LOG_FETCH_TIMEOUT,
    STATUS_STYLES,
    TERMINAL_STATUSES,
    MAX_LOG_LINES,
    INFO_PANE_WIDTH,
    KEY_COLS,
    MR_STATE_CYCLE,
    MERGED_WINDOW_CYCLE,
    DEFAULT_MERGED_WINDOW_DAYS,
    LOGO,
    REPO_URL,
)
from .formatting import (
    _dbg,
    copy_to_clipboard,
    status_badge,
    format_age,
    format_duration,
    _info_lines,
    _key_lines,
    _key_lines_from_rows,
    _header_layout,
    _format_header,
    _breadcrumb_text,
    _auto_refresh_indicator,
    _loading_indicator,
    _status_line,
    _pipeline_status_color,
    _pipeline_status_with_id,
    _branch_pair_text,
    _mr_state_badge,
    _mr_state_color,
)
from .api import GitLabAPI







# -- status styling ----------------------------------------------------------





# -- k9s-style header --------------------------------------------------------





class K9sHeader(Static):

    def __init__(self, info_pairs, keys, **kw):
        super().__init__(**kw)
        self._info_pairs = info_pairs
        self._keys = keys
        self._available_width = None
        self.update(_format_header(info_pairs, keys, self._available_width))

    def _rerender(self) -> None:
        self.update(_format_header(self._info_pairs, self._keys, self._available_width))

    def set_info(self, pairs) -> None:
        self._info_pairs = pairs
        self._rerender()

    def set_keys(self, keys) -> None:
        self._keys = keys
        self._rerender()

    def on_resize(self, event) -> None:
        try:
            new_width = event.size.width
        except Exception:
            return
        if new_width != self._available_width:
            self._available_width = new_width
            self._rerender()


class KeyBar(Static):
    pass




class StatusBar(Horizontal):
    def __init__(self, left=None, right=None, **kw):
        super().__init__(**kw)
        self._initial_left = left or ""
        self._initial_right = right or ""
        self._initial_loading = ""
        v = Text()
        v.append("  ", style="#585b70")
        v.append(f"v{__version__}", style="#6c7086")
        self._version_text = v

    def compose(self) -> ComposeResult:
        yield Static(self._initial_left, id="status-left")
        yield Static(self._initial_loading, id="status-loading")
        yield Static(self._initial_right, id="status-right")
        yield Static(self._version_text, id="status-version")

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

    def set_loading(self, text) -> None:
        try:
            self.query_one("#status-loading", Static).update(text)
        except Exception:
            self._initial_loading = text




class ScreenBase(Screen):
    """base screen that routes single-char keys only when input is not focused"""

    # subclasses define: KEY_MAP = {"q": "back", "r": "refresh", ...}
    KEY_MAP: dict[str, str] = {}

    # when set, _refresh_status implementations show this as the right-side loading text
    _user_loading_label: str | None = None

    async def _show_loading(self, label: str) -> None:
        self._user_loading_label = label
        sb_found = False
        exc = ""
        try:
            sb = self.query_one("#statusbar", StatusBar)
            sb_found = True
            sb.set_text(_loading_indicator(label))
            sb.refresh()
        except Exception as e:
            exc = f"{type(e).__name__}:{e}"
        _dbg(f"_show_loading screen={type(self).__name__} label={label!r} sb_found={sb_found} exc={exc!r}")
        await asyncio.sleep(0)

    def _clear_loading(self) -> None:
        self._user_loading_label = None
        _dbg(f"_clear_loading screen={type(self).__name__}")
        try:
            self._refresh_status()
        except Exception:
            pass

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



# -- screens -----------------------------------------------------------------



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
            f"\n[dim #6c7086]v{__version__}[/]"
            "\n\n\n[#a6adc8]loading projects...[/]"
        )


class ProjectSelectScreen(ScreenBase):

    KEY_MAP = {"q": "quit", "r": "refresh", "slash": "search", "s": "star", "a": "toggle_all", "c": "clear_filter", "m": "open_modules", "tab": "open_modules", "g": "goto_mr", "p": "my_pipelines"}

    DEBOUNCE_SECONDS = 0.4

    def __init__(self, api: GitLabAPI, favorites):
        super().__init__()
        self.api = api
        self.favorites = favorites
        self.projects = []
        self.mode = "fav" if favorites.list() else "all"
        self._search_timer = None
        self._loading = False

    def compose(self) -> ComposeResult:
        yield K9sHeader(self._info_pairs(), self._keys(), id="header")
        yield Static(self._breadcrumb(), id="breadcrumb", classes="breadcrumb")
        yield Container(
            Input(placeholder="/  filter projects...", id="project-search"),
            id="filter-bar",
        )
        yield DataTable(id="project-table")
        yield StatusBar(self._status_text(), id="statusbar")

    def _breadcrumb(self):
        return _breadcrumb_text(["Projects"])

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
            sb = self.query_one("#statusbar", StatusBar)
            sb.set_text(_loading_indicator(self._user_loading_label) if self._user_loading_label else self._status_text())
            label = self._user_loading_label or ("loading projects..." if self._loading else None)
            if label:
                sb.set_right(_loading_indicator(label))
            else:
                sb.set_right("")
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
        toggle_label = "show all" if self.mode == "fav" else "favorites only"
        return [
            [("tab", "modules"),  ("/", "filter"),     ("enter", "select")],
            [("r", "refresh"),    ("s", "star"),       ("a", toggle_label)],
            [("p", "my pipelines"),("g", "goto MR"),   ("c", "clear")],
            [("q", "quit")],
        ]

    def _refresh_breadcrumb(self) -> None:
        self.query_one("#header", K9sHeader).set_info(self._info_pairs())

    async def on_mount(self) -> None:
        table = self.query_one("#project-table", DataTable)
        table.add_columns("", "Project", "Description", "Last Activity")
        table.cursor_type = "row"
        # show loading immediately, defer actual fetch until after first render
        self._user_loading_label = "loading projects..."
        try:
            sb = self.query_one("#statusbar", StatusBar)
            sb.set_text(_loading_indicator(self._user_loading_label))
        except Exception:
            pass
        self.call_after_refresh(lambda: asyncio.create_task(self._initial_load()))

    async def _initial_load(self) -> None:
        try:
            await self.load_projects()
        finally:
            self._clear_loading()
        try:
            self.query_one("#project-table", DataTable).focus()
        except Exception:
            pass
        try:
            last = self.api.config.get_last_view() or {}
            if last.get('type') != 'pipelines':
                self.api.config.save_last_view('pipelines')
        except Exception:
            pass

    async def load_projects(self, search=None) -> None:
        self._loading = True
        self._refresh_status()
        try:
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
        finally:
            self._loading = False
            self._refresh_status()

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
        if self.mode == "all" and not self.favorites.list():
            self.notify("No favorites starred yet (press 's' to star)", timeout=3)
            return
        self.mode = "all" if self.mode == "fav" else "fav"
        # clear search when toggling
        self.query_one("#project-search", Input).value = ""
        self._refresh_keys()
        label = "favorites" if self.mode == "fav" else "all projects"
        self._user_loading_label = f"loading {label}..."
        try:
            await self.load_projects()
        finally:
            self._clear_loading()
        self.notify(f"Showing: {label}", timeout=2)

    def _refresh_keys(self) -> None:
        try:
            self.query_one("#header", K9sHeader).set_keys(self._keys())
        except Exception:
            pass

    async def action_clear_filter(self) -> None:
        inp = self.query_one("#project-search", Input)
        if not inp.value:
            return
        inp.value = ""
        if self._search_timer is not None:
            self._search_timer.stop()
        await self._do_search()

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if idx is not None and idx < len(self.projects):
            project = self.projects[idx]
            self.api.set_project(project['path'])
            age = getattr(self.app, 'default_age_days', DEFAULT_PIPELINE_AGE_DAYS)
            initial_filter = getattr(self.app, 'default_branch_filter', '') or ''
            self.app.push_screen(PipelineListScreen(self.api, age_days=age, initial_filter=initial_filter))

    async def action_quit(self) -> None:
        self.app.exit()

    async def action_open_modules(self) -> None:
        self.app.open_modules()

    async def action_my_pipelines(self) -> None:
        age = getattr(self.app, 'default_age_days', DEFAULT_PIPELINE_AGE_DAYS)
        self.app.switch_screen(MyPipelineListScreen(self.api, age_days=age))

    async def action_goto_mr(self) -> None:
        def _after(result):
            if result:
                project_path, iid = result
                asyncio.ensure_future(self._open_mr(project_path, iid))
        self.app.push_screen(MRPickerModal(recents=self.api.config.recent_projects), _after)

    async def _open_mr(self, project_path: str, iid: int) -> None:
        self.app.push_screen(MergeRequestDetailScreen(self.api, project_path, iid))


class PipelineListScreen(ScreenBase):

    KEY_MAP = {"q": "back", "r": "refresh", "slash": "search", "b": "browser", "y": "yank", "t": "toggle_age", "x": "cancel", "s": "toggle_status", "f": "failed", "c": "clear_filter", "M": "project_mrs", "P": "my_pipelines"}

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
            [("/", "filter"),    ("enter", "jobs"),  ("r", "refresh")],
            [("b", "browser"),   ("y", "copy url"),  ("c", "clear")],
            [("t", "age"),       ("s", status_label),("f", failed_label)],
            [("M", "MRs"),       ("P", "my pipes"),  ("x", "cancel")],
            [("q", "back")],
        ]

    def compose(self) -> ComposeResult:
        yield K9sHeader(self._info_pairs(), self._keys(), id="header")
        yield Static(self._breadcrumb(), id="breadcrumb", classes="breadcrumb")
        yield Container(
            Input(value=self.initial_filter, placeholder="/  filter pipelines...", id="pipeline-filter"),
            id="filter-bar",
        )
        yield DataTable(id="pipeline-table")
        yield StatusBar(self._status_text(), id="statusbar")

    def _breadcrumb(self):
        return _breadcrumb_text([self.api.project_name or "Project", "Pipelines"])

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
            sb.set_text(_loading_indicator(self._user_loading_label) if self._user_loading_label else self._status_text())
            sb.set_right(_auto_refresh_indicator(
                self.REFRESH_INTERVAL,
                active=self._refresh_timer is not None,
                refreshing=self._refreshing,
                loading_label=self._user_loading_label,
            ))
        except Exception:
            pass

    REFRESH_INTERVAL = PIPELINE_REFRESH_INTERVAL

    async def on_mount(self) -> None:
        table = self.query_one("#pipeline-table", DataTable)
        table.add_columns("ID", "Status", "Branch", "SHA", "Age", "User")
        table.cursor_type = "row"
        self._user_loading_label = "loading pipelines..."
        try:
            sb = self.query_one("#statusbar", StatusBar)
            sb.set_text(_loading_indicator(self._user_loading_label))
        except Exception:
            pass
        self.call_after_refresh(lambda: asyncio.create_task(self._initial_load()))

    async def _initial_load(self) -> None:
        try:
            await self.load_pipelines()
        finally:
            self._clear_loading()
        try:
            self.query_one("#pipeline-table", DataTable).focus()
        except Exception:
            pass
        self._refresh_timer = self.set_interval(self.REFRESH_INTERVAL, self._safe_refresh)
        self._refresh_status()
        if self.api.project_name:
            self.api.config.save_last_view('pipelines', project=self.api.project_name)
            self.api.config.recent_projects.remember(self.api.project_name)

    def _refresh_breadcrumb(self) -> None:
        self.query_one("#header", K9sHeader).set_info(self._info_pairs())

    async def load_pipelines(self, full_bridge_scan=True) -> None:
        owns_refresh_flag = not self._refreshing
        if owns_refresh_flag:
            self._refreshing = True
            self._refresh_status()
        try:
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
        finally:
            if owns_refresh_flag:
                self._refreshing = False
                self._refresh_status()

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
                if query in (p.get('ref') or '').lower()
                or query in (p.get('status') or '').lower()
                or query in (p.get('user') or '').lower()
                or query in str(p['id'])
                or query in (p.get('sha') or '').lower()
                or query in (p.get('_bridge_name') or '').lower()
                or query in (p.get('_ds_project_path') or '').lower()
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
        if self._refreshing:
            return
        await self._show_loading("loading pipelines...")
        try:
            await self.load_pipelines()
        finally:
            self._clear_loading()

    async def action_search(self) -> None:
        self.query_one("#pipeline-filter", Input).focus()

    async def action_clear_filter(self) -> None:
        inp = self.query_one("#pipeline-filter", Input)
        cleared_text = bool(inp.value)
        cleared_status = self.status_filter is not None
        if not cleared_text and not cleared_status:
            return
        inp.value = ""
        self.status_filter = None
        self._apply_filter()
        self._refresh_header()

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

    async def action_my_pipelines(self) -> None:
        self.app.push_screen(MyPipelineListScreen(self.api, age_days=self.age_days))


class JobListScreen(ScreenBase):

    KEY_MAP = {"q": "back", "r": "refresh", "slash": "search", "b": "browser", "f": "failures", "s": "toggle_status", "y": "yank", "c": "clear_filter"}

    STAGE_ORDER = ['build', 'test', 'deploy', 'cleanup']

    STATUS_CYCLE = [None, 'failed', 'running', 'pending']

    def __init__(self, api: GitLabAPI, pipeline: dict):
        super().__init__()
        self.api = api
        self.pipeline = pipeline
        self.jobs = []
        self.bridges = []
        self.rows = []
        self.filtered_jobs = []
        self.status_filter = None
        self._refresh_timer = None
        self._refreshing = False

    def _info_pairs(self):
        job_count = len(getattr(self, 'jobs', []) or [])
        bridge_count = len(getattr(self, 'bridges', []) or [])
        total = job_count + bridge_count
        jobs_label = str(total)
        if bridge_count:
            jobs_label = f"{total} ({job_count} jobs + {bridge_count} bridges)"
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
            [("/", "filter"),   ("enter", "logs"),  ("r", "refresh")],
            [("b", "browser"),  ("y", "copy url"),  ("c", "clear")],
            [("s", status_label),("f", "failures")],
            [("q", "back")],
        ]

    def compose(self) -> ComposeResult:
        yield K9sHeader(self._info_pairs(), self._keys(), id="header")
        yield Static(self._breadcrumb(), id="breadcrumb", classes="breadcrumb")
        yield Container(
            Input(placeholder="/  filter jobs...", id="job-filter"),
            id="filter-bar",
        )
        yield DataTable(id="job-table")
        yield StatusBar(self._status_text(), id="statusbar")

    def _breadcrumb(self):
        return _breadcrumb_text([
            self.api.project_name or "Project",
            f"Pipeline {self.pipeline['id']}",
            "Jobs",
        ])

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
            sb.set_text(_loading_indicator(self._user_loading_label) if self._user_loading_label else self._status_text())
            sb.set_right(_auto_refresh_indicator(
                self.REFRESH_INTERVAL,
                active=self._refresh_timer is not None,
                refreshing=self._refreshing,
                loading_label=self._user_loading_label,
            ))
        except Exception:
            pass

    REFRESH_INTERVAL = JOB_REFRESH_INTERVAL

    async def on_mount(self) -> None:
        table = self.query_one("#job-table", DataTable)
        table.add_columns("Stage", "Name", "Status", "Duration", "ID")
        table.cursor_type = "row"
        self._user_loading_label = "loading jobs..."
        try:
            sb = self.query_one("#statusbar", StatusBar)
            sb.set_text(_loading_indicator(self._user_loading_label))
        except Exception:
            pass
        self.call_after_refresh(lambda: asyncio.create_task(self._initial_load()))

    async def _initial_load(self) -> None:
        try:
            await self.load_jobs()
        finally:
            self._clear_loading()
        try:
            self.query_one("#job-table", DataTable).focus()
        except Exception:
            pass
        self._refresh_timer = self.set_interval(self.REFRESH_INTERVAL, self._safe_refresh)
        self._refresh_status()

    async def load_jobs(self) -> None:
        owns_refresh_flag = not self._refreshing
        if owns_refresh_flag:
            self._refreshing = True
            self._refresh_status()
        try:
            jobs, detail, bridges = await asyncio.gather(
                asyncio.to_thread(self.api.get_pipeline_jobs, self.pipeline['id']),
                asyncio.to_thread(self.api.get_pipeline_detail, self.pipeline['id']),
                asyncio.to_thread(self.api.get_pipeline_bridges, self.pipeline['id']),
            )
            self.jobs = jobs
            self.bridges = bridges or []
            if detail:
                self.pipeline.update(detail)
            order = self.STAGE_ORDER

            def _stage_key(stage):
                return order.index(stage) if stage in order else len(order)

            self.jobs.sort(key=lambda j: (_stage_key(j['stage']), j['name']))
            self.rows = []
            for j in self.jobs:
                self.rows.append({'_kind': 'job', **j})
            for b in self.bridges:
                stage = b.get('_bridge_stage') or ''
                self.rows.append({
                    '_kind': 'bridge',
                    'id': b.get('_bridge_id') or b.get('id'),
                    'name': b.get('_bridge_name') or '',
                    'status': b.get('_bridge_status') or b.get('status') or 'unknown',
                    'stage': stage,
                    'duration': b.get('_bridge_duration'),
                    'started_at': b.get('_bridge_started_at'),
                    'finished_at': b.get('_bridge_finished_at'),
                    'web_url': b.get('_bridge_web_url') or b.get('web_url'),
                    '_downstream': b,
                })
            self.rows.sort(key=lambda r: (_stage_key(r.get('stage') or ''), r.get('name') or ''))
            self._apply_filter()
            try:
                self.query_one("#header", K9sHeader).set_info(self._info_pairs())
            except Exception:
                pass
        finally:
            if owns_refresh_flag:
                self._refreshing = False
                self._refresh_status()

    def _apply_filter(self) -> None:
        query = self.query_one("#job-filter", Input).value.strip().lower()
        rows = self.rows
        if self.status_filter:
            rows = [r for r in rows if r.get('status') == self.status_filter]
        if query:
            self.filtered_jobs = [
                r for r in rows
                if query in (r.get('name') or '').lower()
                or query in (r.get('stage') or '').lower()
                or query in (r.get('status') or '').lower()
                or query in str(r.get('id') or '')
            ]
        else:
            self.filtered_jobs = rows
        self._update_table()
        self._refresh_status()

    def _update_table(self) -> None:
        table = self.query_one("#job-table", DataTable)
        prev_row = table.cursor_row
        table.clear()
        for row in self.filtered_jobs:
            failed = row.get('status') == 'failed'
            is_bridge = row.get('_kind') == 'bridge'
            name_style = "bold red" if failed else ("bold #b4befe" if is_bridge else "")
            stage_style = "red" if failed else ("#b4befe" if is_bridge else "dim")
            dur = format_duration(row.get('duration'))
            if is_bridge:
                ds = row.get('_downstream') or {}
                ds_id = ds.get('id')
                ds_path = ds.get('_ds_project_path')
                bridge_name = row.get('name') or ''
                if ds_path and ds_id:
                    suffix = f"  → {ds_path} #{ds_id}"
                elif ds_path:
                    suffix = f"  → {ds_path}"
                elif ds_id:
                    suffix = f"  → pipeline #{ds_id}"
                else:
                    suffix = ""
                name_text = Text()
                name_text.append("↳ ", style="#b4befe")
                name_text.append(bridge_name[:40], style=name_style)
                name_text.append(suffix, style="dim #b4befe")
            else:
                name_text = Text((row.get('name') or '')[:50], style=name_style)
            table.add_row(
                Text(row.get('stage') or '', style=stage_style),
                name_text,
                status_badge(row.get('status') or 'unknown'),
                Text(dur, style="red" if failed else "dim"),
                Text(str(row.get('id') or ''), style="dim"),
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
        if self._refreshing:
            return
        await self._show_loading("loading jobs...")
        try:
            await self.load_jobs()
        finally:
            self._clear_loading()

    async def action_search(self) -> None:
        self.query_one("#job-filter", Input).focus()

    async def action_clear_filter(self) -> None:
        inp = self.query_one("#job-filter", Input)
        cleared_text = bool(inp.value)
        cleared_status = self.status_filter is not None
        if not cleared_text and not cleared_status:
            return
        inp.value = ""
        self.status_filter = None
        self._apply_filter()
        try:
            header = self.query_one("#header", K9sHeader)
            header.set_keys(self._keys())
            header.set_info(self._info_pairs())
        except Exception:
            pass

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if idx is None or idx >= len(self.filtered_jobs):
            return
        row = self.filtered_jobs[idx]
        if row.get('_kind') == 'bridge':
            ds = row.get('_downstream') or {}
            ds_path = ds.get('_ds_project_path')
            if ds_path:
                ds_api = GitLabAPI(self.api.config)
                try:
                    ds_api.set_project(ds_path)
                except Exception:
                    self.notify(f"Cannot access {ds_path}", severity="error", timeout=3)
                    return
                self.app.push_screen(JobListScreen(ds_api, ds))
            else:
                self.app.push_screen(JobListScreen(self.api, ds))
            return
        self.app.push_screen(JobDetailScreen(self.api, row))

    async def action_browser(self) -> None:
        table = self.query_one("#job-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self.filtered_jobs):
            url = self.filtered_jobs[table.cursor_row].get('web_url')
            if url:
                webbrowser.open(url)

    async def action_failures(self) -> None:
        failed = [r for r in self.rows if r.get('status') == 'failed']
        if not failed:
            self.notify("No failed jobs in this pipeline", timeout=2)
            return
        if all(r.get('_kind') == 'bridge' for r in failed):
            self.status_filter = 'failed'
            self._apply_filter()
            try:
                header = self.query_one("#header", K9sHeader)
                header.set_keys(self._keys())
                header.set_info(self._info_pairs())
            except Exception:
                pass
            self.notify("Failures are in child pipelines — press <enter> to drill in", timeout=3)
            return
        failed_jobs = [r for r in failed if r.get('_kind') == 'job']
        self.app.push_screen(FailedJobsScreen(self.api, self.pipeline, failed_jobs))

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
            url = j.get('web_url')
            if url and copy_to_clipboard(url):
                label = "bridge" if j.get('_kind') == 'bridge' else "job"
                self.notify(f"Copied {label} #{j.get('id')} URL", timeout=2)


class JobDetailScreen(ScreenBase):

    KEY_MAP = {"q": "back", "r": "refresh", "b": "browser", "f": "failures", "y": "yank"}

    def __init__(self, api: GitLabAPI, job: dict):
        super().__init__()
        self.api = api
        self.job = job
        self.trace = ""
        self.trace_bytes_read = 0
        self._line_buffer = ""
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
            [("r", "refresh"),  ("b", "browser"),       ("y", "copy")],
            [("f", "failures only")],
            [("q", "back")],
        ]

    def compose(self) -> ComposeResult:
        yield K9sHeader(self._info_pairs(), self._keys(), id="header")
        yield Static(self._breadcrumb(), id="breadcrumb", classes="breadcrumb")
        yield RichLog(id="job-log", wrap=True, max_lines=MAX_LOG_LINES)
        yield StatusBar(self._status_text(), id="statusbar")

    def _breadcrumb(self):
        return _breadcrumb_text([
            self.api.project_name or "Project",
            f"Pipeline {self.job.get('pipeline_id') or '?'}",
            f"Job: {self.job.get('name') or '?'}",
        ])

    def _status_text(self):
        dur = format_duration(self.job.get('duration'))
        return _status_line([("duration", dur)])

    def _refresh_status(self) -> None:
        try:
            sb = self.query_one("#statusbar", StatusBar)
            sb.set_text(_loading_indicator(self._user_loading_label) if self._user_loading_label else self._status_text())
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
        t.append(" · tail ", style="#6c7086")
        t.append(f"{self.TRACE_INTERVAL}s", style="bold #a6e3a1")
        return t

    def _update_info_bar(self) -> None:
        try:
            self.query_one("#header", K9sHeader).set_info(self._info_pairs())
        except Exception:
            pass
        self._refresh_status()

    async def on_mount(self) -> None:
        self._user_loading_label = "loading job log..."
        try:
            sb = self.query_one("#statusbar", StatusBar)
            sb.set_text(_loading_indicator(self._user_loading_label))
        except Exception:
            pass
        self.call_after_refresh(lambda: asyncio.create_task(self._initial_load()))

    async def _initial_load(self) -> None:
        try:
            await self.load_trace()
        finally:
            self._clear_loading()
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
            self._flush_line_buffer()
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
        new_bytes, total = await asyncio.to_thread(
            self.api.get_job_trace_range, self.job['id'], self.trace_bytes_read
        )
        if not new_bytes:
            return
        self.trace_bytes_read = total
        chunk = new_bytes.decode("utf-8", errors="replace")
        self.trace += chunk
        buf = self._line_buffer + chunk
        parts = buf.split('\n')
        self._line_buffer = parts[-1]
        log = self.query_one("#job-log", RichLog)
        for line in parts[:-1]:
            self._write_trace_line(log, line)

    def _flush_line_buffer(self) -> None:
        if not self._line_buffer:
            return
        try:
            log = self.query_one("#job-log", RichLog)
            self._write_trace_line(log, self._line_buffer)
        except Exception:
            pass
        self._line_buffer = ""

    async def load_trace(self) -> None:
        log = self.query_one("#job-log", RichLog)
        log.clear()
        self.trace = ""
        self.trace_bytes_read = 0
        self._line_buffer = ""

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

        await self._append_new_trace()
        if self.job.get('status') in TERMINAL_STATUSES:
            self._flush_line_buffer()

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
        return [("y", "copy"), ("q", "back")]

    def compose(self) -> ComposeResult:
        yield K9sHeader(self._info_pairs(), self._keys(), id="header")
        yield Static(self._breadcrumb(), id="breadcrumb", classes="breadcrumb")
        yield RichLog(id="failures-log", wrap=True, max_lines=MAX_LOG_LINES)
        yield StatusBar(
            _status_line([f"{len(self.failed_jobs)} failed jobs", ("pipeline", f"#{self.pipeline['id']}")]),
            id="statusbar",
        )

    def _breadcrumb(self):
        return _breadcrumb_text([
            self.api.project_name or "Project",
            f"Pipeline {self.pipeline['id']}",
            "Failed jobs",
        ])


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




class MyMergeRequestsScreen(ScreenBase):

    KEY_MAP = {"q": "back", "r": "refresh", "slash": "search", "b": "browser", "y": "yank", "g": "goto", "s": "toggle_state", "m": "open_modules", "tab": "open_modules", "e": "export", "n": "note", "p": "pipelines", "w": "toggle_window"}

    REFRESH_INTERVAL = PIPELINE_REFRESH_INTERVAL

    def __init__(self, api: GitLabAPI):
        super().__init__()
        self.api = api
        self.mrs = []
        self.filtered_mrs = []
        self.state = 'opened'
        self.window_days = DEFAULT_MERGED_WINDOW_DAYS
        self._refresh_timer = None
        self._refreshing = False
        self._unresolved_cache = {}
        self._related_cache = {}
        self._row_to_mr = []
        self._unresolved_col_key = None

    def _is_windowed_state(self):
        return self.state in ('merged', 'closed', 'all')

    def _info_pairs(self):
        total = len(self.mrs)
        shown = len(self.filtered_mrs)
        count = f"{shown}/{total}" if shown != total else str(total)
        pairs = [
            ("GitLab", self.api.config.gitlab_url),
            ("Scope", "created_by_me"),
            ("State", self.state),
            ("MRs", count),
        ]
        if self._is_windowed_state():
            pairs.append(("Window", f"{self.window_days}d"))
        return pairs

    def _keys(self):
        window_slot = ("w", f"window:{self.window_days}d") if self._is_windowed_state() else None
        return [
            [("tab", "modules"),  ("/", "filter"),              ("enter", "view")],
            [("r", "refresh"),    ("b", "browser"),             ("y", "copy url")],
            [("p", "pipelines"),  ("s", f"state:{self.state}"), ("g", "goto MR")],
            [("e", "export md"),  ("n", "note"),                window_slot],
            [("q", "quit")],
        ]

    def compose(self) -> ComposeResult:
        yield K9sHeader(self._info_pairs(), self._keys(), id="header")
        yield Static(self._breadcrumb(), id="breadcrumb", classes="breadcrumb")
        yield Container(
            Input(placeholder="/  filter MRs...", id="mr-filter"),
            id="filter-bar",
        )
        yield DataTable(id="mr-table")
        yield StatusBar(self._status_text(), id="statusbar")

    def _breadcrumb(self):
        return _breadcrumb_text(["Merge Requests"])

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
            sb.set_text(_loading_indicator(self._user_loading_label) if self._user_loading_label else self._status_text())
            sb.set_right(_auto_refresh_indicator(
                self.REFRESH_INTERVAL,
                active=self._refresh_timer is not None,
                refreshing=self._refreshing,
                loading_label=self._user_loading_label,
            ))
        except Exception:
            pass

    async def on_mount(self) -> None:
        table = self.query_one("#mr-table", DataTable)
        col_keys = table.add_columns("●", "IID", "Title", "Branch", "MR", "Pipeline", "Threads", "Age")
        try:
            self._unresolved_col_key = col_keys[6]
        except Exception:
            self._unresolved_col_key = None
        table.cursor_type = "row"
        # set loading text + schedule the actual load AFTER first render so user sees the loading state
        self._user_loading_label = "loading MRs..."
        try:
            sb = self.query_one("#statusbar", StatusBar)
            sb.set_text(_loading_indicator(self._user_loading_label))
        except Exception:
            pass
        self.call_after_refresh(lambda: asyncio.create_task(self._initial_load()))

    def _set_unresolved_col_label(self, label: str) -> None:
        if self._unresolved_col_key is None:
            return
        try:
            table = self.query_one("#mr-table", DataTable)
            col = table.columns.get(self._unresolved_col_key)
            if col is not None:
                col.label = Text(label)
                table.refresh()
        except Exception:
            pass

    async def _initial_load(self) -> None:
        try:
            await self.load_mrs()
        finally:
            self._clear_loading()
        try:
            self.query_one("#mr-table", DataTable).focus()
        except Exception:
            pass
        self._refresh_timer = self.set_interval(self.REFRESH_INTERVAL, self._safe_refresh)
        self._refresh_status()
        self.api.config.save_last_view('my_mrs')

    async def load_mrs(self) -> None:
        days = self.window_days if self._is_windowed_state() else None
        self.mrs = await asyncio.to_thread(self.api.get_my_merge_requests, self.state, 50, days)
        if self.state == 'opened' and self.mrs:
            await asyncio.gather(
                self._backfill_head_pipelines(self.mrs),
                self._fetch_unresolved_counts(self.mrs),
            )
        elif self._is_windowed_state() and self.mrs:
            await self._enrich_merged_mrs()
        self._set_unresolved_col_label("Related" if self._is_windowed_state() else "Threads")
        self._apply_filter()
        try:
            self.query_one("#header", K9sHeader).set_info(self._info_pairs())
            self.query_one("#header", K9sHeader).set_keys(self._keys())
        except Exception:
            pass

    async def _backfill_head_pipelines(self, mrs) -> None:
        todo = [m for m in mrs if m.get('project_path') and m.get('iid') is not None and not m.get('head_pipeline_status')]
        if not todo:
            return
        results = await asyncio.gather(*(
            asyncio.to_thread(self.api.get_mr_pipelines, m['project_path'], m['iid'])
            for m in todo
        ), return_exceptions=True)
        for mr, r in zip(todo, results):
            if isinstance(r, list) and r:
                mr['head_pipeline_status'] = r[0].get('status') or mr.get('head_pipeline_status')
                mr['head_pipeline_id'] = r[0].get('id') or mr.get('head_pipeline_id')
                mr['head_pipeline_web_url'] = r[0].get('web_url') or mr.get('head_pipeline_web_url')

    async def _fetch_unresolved_counts(self, mrs) -> None:
        todo = [(m['project_path'], m['iid']) for m in mrs if m.get('project_path')]
        if not todo:
            return
        results = await asyncio.gather(*(
            asyncio.to_thread(self.api.get_mr_unresolved_count, p, i) for p, i in todo
        ), return_exceptions=True)
        for (p, i), r in zip(todo, results):
            if isinstance(r, int):
                self._unresolved_cache[(p, i)] = r

    async def _enrich_merged_mrs(self) -> None:
        related_todo = []
        for m in self.mrs:
            proj = m.get('project_path') or ''
            iid = m.get('iid')
            if not proj or iid is None:
                continue
            if m.get('state') == 'merged':
                key = (proj, iid)
                if key in self._related_cache:
                    continue
                target = m.get('target_branch') or ''
                merged_at = m.get('merged_at') or ''
                if target and merged_at:
                    related_todo.append((proj, iid, target, merged_at))

        await self._backfill_head_pipelines(self.mrs)

        if related_todo:
            results = await asyncio.gather(*(
                asyncio.to_thread(self.api.list_pipelines_for_ref_since, p, ref, since)
                for p, _, ref, since in related_todo
            ), return_exceptions=True)
            for (p, i, _, _), r in zip(related_todo, results):
                self._related_cache[(p, i)] = r if isinstance(r, list) else []

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
        self._row_to_mr = []
        groups = {}
        order = []
        for m in self.filtered_mrs:
            key = m['project_path'] or ''
            if key not in groups:
                groups[key] = []
                order.append(key)
        for m in self.filtered_mrs:
            groups[m['project_path'] or ''].append(m)
        order.sort(key=lambda p: (p.rsplit('/', 1)[-1] or '').lower())
        blank = Text("")
        for i, proj_path in enumerate(order):
            repo = (proj_path.rsplit('/', 1)[-1] if proj_path else 'UNKNOWN').upper()
            header = Text(repo, style="bold #89b4fa")
            table.add_row(blank, header, blank, blank, blank, blank, blank, blank)
            self._row_to_mr.append(None)
            for m in groups[proj_path]:
                iid = Text(f"!{m['iid']}", style="bold #89b4fa")
                title = m['title'] or ''
                if m['draft']:
                    title = f"[draft] {title}"
                title_t = Text(title[:50], style="bold #cdd6f4")
                branch_t = _branch_pair_text(m.get('source_branch'), m.get('target_branch'))
                related_or_unresolved_t = self._render_related_or_unresolved(m)
                age = format_age(m['updated_at'] or m['created_at'])
                has_note = self.api.config.mr_notes.has(m['project_path'] or '', m['iid'])
                note_t = Text("●", style="bold #f9e2af") if has_note else blank
                table.add_row(
                    note_t,
                    iid,
                    title_t,
                    branch_t,
                    _mr_state_badge(m['state']),
                    _pipeline_status_with_id(m['head_pipeline_status'], m.get('head_pipeline_id')),
                    related_or_unresolved_t,
                    Text(age, style="dim italic"),
                )
                self._row_to_mr.append(m)
            if i < len(order) - 1:
                table.add_row(blank, blank, blank, blank, blank, blank, blank, blank)
                self._row_to_mr.append(None)
        if prev is not None and self._row_to_mr:
            target = min(prev, len(self._row_to_mr) - 1)
            while target < len(self._row_to_mr) and self._row_to_mr[target] is None:
                target += 1
            if target >= len(self._row_to_mr):
                target = next((i for i, x in enumerate(self._row_to_mr) if x is not None), 0)
            table.move_cursor(row=target)

    def _render_related_or_unresolved(self, m) -> Text:
        key = (m.get('project_path') or '', m.get('iid'))
        if self._is_windowed_state():
            related = self._related_cache.get(key)
            if related is None:
                return Text("—", style="dim")
            count = len(related)
            if count == 0:
                return Text("0", style="dim")
            active = sum(1 for p in related if p.get('status') not in TERMINAL_STATUSES)
            if active:
                return Text(f"{count} ({active}*)", style="bold #f9e2af")
            return Text(str(count), style="bold #89b4fa")
        unresolved = self._unresolved_cache.get(key)
        if unresolved is None:
            return Text("—", style="dim")
        if unresolved == 0:
            return Text("0", style="dim #a6e3a1")
        return Text(str(unresolved), style="bold #f38ba8")

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "mr-filter":
            self._apply_filter()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "mr-filter":
            self._apply_filter()
            self.query_one("#mr-table", DataTable).focus()

    async def action_back(self) -> None:
        self.app.exit()

    async def action_open_modules(self) -> None:
        self.app.open_modules()

    async def action_pipelines(self) -> None:
        table = self.query_one("#mr-table", DataTable)
        m = self._mr_at_row(table.cursor_row)
        if m is None:
            return
        proj = m.get('project_path') or ''
        if not proj:
            self.notify("MR has no project path", severity="warning", timeout=2)
            return
        ds_api = GitLabAPI(self.api.config)
        try:
            ds_api.set_project(proj)
        except Exception:
            self.notify(f"Cannot access {proj}", severity="error", timeout=3)
            return
        self.app.push_screen(MRPipelineListScreen(ds_api, proj, m))

    async def action_toggle_window(self) -> None:
        if not self._is_windowed_state():
            return
        try:
            idx = MERGED_WINDOW_CYCLE.index(self.window_days)
        except ValueError:
            idx = -1
        self.window_days = MERGED_WINDOW_CYCLE[(idx + 1) % len(MERGED_WINDOW_CYCLE)]
        self._related_cache.clear()
        self._unresolved_cache.clear()
        await self._show_loading(f"loading {self.window_days}d MRs...")
        try:
            await self.load_mrs()
        finally:
            self._clear_loading()

    async def action_refresh(self) -> None:
        if self._refreshing:
            return
        self._unresolved_cache.clear()
        self._related_cache.clear()
        await self._show_loading("loading MRs...")
        try:
            await self.load_mrs()
        finally:
            self._clear_loading()

    async def action_search(self) -> None:
        self.query_one("#mr-filter", Input).focus()

    async def action_toggle_state(self) -> None:
        try:
            idx = MR_STATE_CYCLE.index(self.state)
        except ValueError:
            idx = -1
        self.state = MR_STATE_CYCLE[(idx + 1) % len(MR_STATE_CYCLE)]
        self._unresolved_cache.clear()
        self._related_cache.clear()
        await self._show_loading("loading MRs...")
        try:
            await self.load_mrs()
        finally:
            self._clear_loading()

    async def action_goto(self) -> None:
        def _after(result):
            if result:
                project_path, iid = result
                asyncio.ensure_future(self._open_mr(project_path, iid))
        self.app.push_screen(MRPickerModal(recents=self.api.config.recent_projects), _after)

    async def _open_mr(self, project_path: str, iid: int) -> None:
        self.app.push_screen(MergeRequestDetailScreen(self.api, project_path, iid))

    def _mr_at_row(self, idx):
        if idx is None or idx < 0 or idx >= len(self._row_to_mr):
            return None
        return self._row_to_mr[idx]

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        m = self._mr_at_row(event.cursor_row)
        if m is not None:
            self.app.push_screen(MergeRequestDetailScreen(self.api, m['project_path'], m['iid']))

    async def action_browser(self) -> None:
        table = self.query_one("#mr-table", DataTable)
        m = self._mr_at_row(table.cursor_row)
        if m is not None:
            webbrowser.open(m['web_url'])

    async def action_yank(self) -> None:
        table = self.query_one("#mr-table", DataTable)
        m = self._mr_at_row(table.cursor_row)
        if m is not None and copy_to_clipboard(m['web_url']):
            self.notify(f"Copied MR !{m['iid']} URL", timeout=2)

    async def action_note(self) -> None:
        table = self.query_one("#mr-table", DataTable)
        m = self._mr_at_row(table.cursor_row)
        if m is None:
            return
        proj = m['project_path'] or ''
        if not proj:
            self.notify("MR has no project path — cannot save note", severity="warning", timeout=2)
            return
        iid = m['iid']
        repo = proj.rsplit('/', 1)[-1] if proj else 'unknown'
        existing = self.api.config.mr_notes.get(proj, iid) or ""

        def _after(result):
            if result is None:
                return
            action, text = result
            if action == "delete":
                if self.api.config.mr_notes.delete(proj, iid):
                    self.notify(f"Note deleted for !{iid}", timeout=2)
                    self._update_table()
                return
            if action == "save":
                if not text:
                    if self.api.config.mr_notes.delete(proj, iid):
                        self.notify(f"Empty note removed for !{iid}", timeout=2)
                        self._update_table()
                    return
                self.api.config.mr_notes.set(proj, iid, text)
                self.notify(f"Note saved for !{iid}", timeout=2)
                self._update_table()

        self.app.push_screen(MRNoteModal(repo, iid, initial=existing), _after)

    async def action_export(self) -> None:
        if not self.filtered_mrs:
            self.notify("Nothing to export", severity="warning", timeout=2)
            return
        initial = self.api.config.export_dir
        def _after(result):
            if result is None:
                return
            target_dir = os.path.expanduser(result.strip())
            if not target_dir:
                self.notify("Export cancelled (empty dir)", severity="warning", timeout=2)
                return
            try:
                os.makedirs(target_dir, exist_ok=True)
            except Exception as e:
                self.notify(f"Cannot create dir: {e}", severity="error", timeout=4)
                return
            try:
                path = self._export_markdown(target_dir)
            except Exception as e:
                self.notify(f"Export failed: {e}", severity="error", timeout=4)
                return
            try:
                self.api.config.set_export_dir(target_dir)
            except Exception:
                pass
            self.notify(f"Exported {len(self.filtered_mrs)} MRs → {path}", timeout=4)
        self.app.push_screen(
            PathInputModal(
                title="Export MRs to directory",
                placeholder="/path/to/dir",
                initial=initial,
            ),
            _after,
        )

    def _export_markdown(self, target_dir: str) -> str:
        groups = {}
        order = []
        for m in self.filtered_mrs:
            key = m['project_path'] or ''
            if key not in groups:
                groups[key] = []
                order.append(key)
        for m in self.filtered_mrs:
            groups[m['project_path'] or ''].append(m)
        order.sort(key=lambda p: (p.rsplit('/', 1)[-1] or '').lower())

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        title_ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [
            f"# My Merge Requests",
            "",
            f"- generated: {title_ts}",
            f"- state: {self.state}",
            f"- count: {len(self.filtered_mrs)}",
            f"- gitlab: {self.api.config.gitlab_url}",
            "",
        ]
        for proj_path in order:
            repo = (proj_path.rsplit('/', 1)[-1] if proj_path else 'UNKNOWN').upper()
            lines.append(f"## {repo}")
            lines.append("")
            lines.append(f"_project: `{proj_path or 'unknown'}`_")
            lines.append("")
            for m in groups[proj_path]:
                title = m['title'] or ''
                if m['draft']:
                    title = f"[draft] {title}"
                unresolved = self._unresolved_cache.get((m['project_path'], m['iid']))
                age = format_age(m['updated_at'] or m['created_at'])
                parts = [
                    f"**[!{m['iid']}]({m['web_url']})**",
                    title,
                ]
                meta = [f"state: {m['state']}"]
                if m.get('head_pipeline_status'):
                    meta.append(f"pipeline: {m['head_pipeline_status']}")
                if unresolved is not None:
                    meta.append(f"unresolved: {unresolved}")
                meta.append(f"age: {age}")
                if m.get('source_branch') and m.get('target_branch'):
                    meta.append(f"branch: `{m['source_branch']}` → `{m['target_branch']}`")
                lines.append(f"- {' — '.join(parts)}  ")
                lines.append(f"  _{' · '.join(meta)}_")
                note = self.api.config.mr_notes.get(m['project_path'] or '', m['iid'])
                if note:
                    for note_line in note.splitlines() or [""]:
                        lines.append(f"  > {note_line}")
            lines.append("")

        path = os.path.join(target_dir, f"mrs-{self.state}-{ts}.md")
        with open(path, 'w') as f:
            f.write("\n".join(lines))
        return path


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
            [("/", "filter"),   ("enter", "view"),         ("r", "refresh")],
            [("b", "browser"),  ("y", "copy url"),         ("s", f"state:{self.state}")],
            [("q", "back")],
        ]

    def compose(self) -> ComposeResult:
        yield K9sHeader(self._info_pairs(), self._keys(), id="header")
        yield Static(self._breadcrumb(), id="breadcrumb", classes="breadcrumb")
        yield Container(
            Input(placeholder="/  filter MRs...", id="proj-mr-filter"),
            id="filter-bar",
        )
        yield DataTable(id="proj-mr-table")
        yield StatusBar(self._status_text(), id="statusbar")

    def _breadcrumb(self):
        return _breadcrumb_text([self.project_path or "Project", "Merge Requests"])

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
            sb.set_text(_loading_indicator(self._user_loading_label) if self._user_loading_label else self._status_text())
            sb.set_right(_auto_refresh_indicator(
                self.REFRESH_INTERVAL,
                active=self._refresh_timer is not None,
                refreshing=self._refreshing,
                loading_label=self._user_loading_label,
            ))
        except Exception:
            pass

    async def on_mount(self) -> None:
        table = self.query_one("#proj-mr-table", DataTable)
        table.add_columns("IID", "Title", "MR", "Pipeline", "Author", "Age")
        table.cursor_type = "row"
        self._user_loading_label = "loading MRs..."
        try:
            sb = self.query_one("#statusbar", StatusBar)
            sb.set_text(_loading_indicator(self._user_loading_label))
        except Exception:
            pass
        self.call_after_refresh(lambda: asyncio.create_task(self._initial_load()))

    async def _initial_load(self) -> None:
        try:
            await self.load_mrs()
        finally:
            self._clear_loading()
        try:
            self.query_one("#proj-mr-table", DataTable).focus()
        except Exception:
            pass
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
                _pipeline_status_with_id(m['head_pipeline_status'], m.get('head_pipeline_id')),
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
        if self._refreshing:
            return
        await self._show_loading("loading MRs...")
        try:
            await self.load_mrs()
        finally:
            self._clear_loading()

    async def action_search(self) -> None:
        self.query_one("#proj-mr-filter", Input).focus()

    async def action_toggle_state(self) -> None:
        try:
            idx = self.STATE_CYCLE.index(self.state)
        except ValueError:
            idx = -1
        self.state = self.STATE_CYCLE[(idx + 1) % len(self.STATE_CYCLE)]
        await self._show_loading("loading MRs...")
        try:
            await self.load_mrs()
        finally:
            self._clear_loading()

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
        "k": "commits",
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
            [("r", "refresh"),    ("b", "browser"),    ("y", "copy url")],
            [("p", "pipelines"),  ("k", "commits"),    ("g", "goto MR")],
            [("a", "approve"),    ("c", "comment"),    ("x", "close")],
            [("f", resolved_label),("t", auto_label)],
            [("q", "back")],
        ]

    def compose(self) -> ComposeResult:
        yield K9sHeader(self._info_pairs(), self._keys(), id="header")
        yield Static(self._breadcrumb(), id="breadcrumb", classes="breadcrumb")
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

    def _breadcrumb(self):
        return _breadcrumb_text([self.project_path or "Project", f"MR !{self.iid}"])

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
            sb.set_text(_loading_indicator(self._user_loading_label) if self._user_loading_label else self._status_text())
            sb.set_right(_auto_refresh_indicator(
                self.REFRESH_INTERVAL,
                active=self._refresh_timer is not None,
                refreshing=self._refreshing,
                loading_label=self._user_loading_label,
            ))
        except Exception:
            pass

    async def on_mount(self) -> None:
        if self.project_path:
            self.api.config.recent_projects.remember(self.project_path)
        self._user_loading_label = "loading MR..."
        try:
            sb = self.query_one("#statusbar", StatusBar)
            sb.set_text(_loading_indicator(self._user_loading_label))
        except Exception:
            pass
        self.call_after_refresh(lambda: asyncio.create_task(self._initial_load()))

    async def _initial_load(self) -> None:
        try:
            await self.load_mr()
        finally:
            self._clear_loading()
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
        if self._refreshing:
            return
        await self._show_loading("loading MR...")
        try:
            await self.load_mr()
        finally:
            self._clear_loading()

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

    async def action_commits(self) -> None:
        if not self.mr:
            return
        self.app.push_screen(MRCommitListScreen(self.api, self.project_path, self.mr))

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
        self.related = []
        self._row_pipelines = []
        self._refresh_timer = None
        self._refreshing = False

    def _info_pairs(self):
        counts = str(len(self.pipelines))
        if self.related:
            counts = f"{len(self.pipelines)} + {len(self.related)} related"
        return [
            ("GitLab", self.api.config.gitlab_url),
            ("Project", self.project_path),
            ("MR", f"!{self.mr['iid']}  {(self.mr.get('title') or '')[:40]}"),
            ("Source", self.mr.get('source_branch') or ''),
            ("Pipelines", counts),
        ]

    def _keys(self):
        return [
            [("enter", "jobs"), ("r", "refresh"),  ("b", "browser")],
            [("y", "copy url")],
            [("q", "back")],
        ]

    def compose(self) -> ComposeResult:
        yield K9sHeader(self._info_pairs(), self._keys(), id="header")
        yield Static(self._breadcrumb(), id="breadcrumb", classes="breadcrumb")
        yield DataTable(id="mr-pipeline-table")
        yield StatusBar(self._status_text(), id="statusbar")

    def _breadcrumb(self):
        return _breadcrumb_text([
            self.project_path or "Project",
            f"MR !{self.mr['iid']}",
            "Pipelines",
        ])

    def _status_text(self):
        parts = [f"{len(self.pipelines)} pipelines", ("MR", f"!{self.mr['iid']}")]
        if self.related:
            parts.insert(1, ("related", str(len(self.related))))
        return _status_line(parts)

    def _refresh_status(self) -> None:
        try:
            sb = self.query_one("#statusbar", StatusBar)
            sb.set_text(_loading_indicator(self._user_loading_label) if self._user_loading_label else self._status_text())
            sb.set_right(_auto_refresh_indicator(
                self.REFRESH_INTERVAL,
                active=self._refresh_timer is not None,
                refreshing=self._refreshing,
                loading_label=self._user_loading_label,
            ))
        except Exception:
            pass

    async def on_mount(self) -> None:
        table = self.query_one("#mr-pipeline-table", DataTable)
        table.add_columns("Source", "ID", "Status", "Ref", "SHA", "Last Run")
        table.cursor_type = "row"
        self._user_loading_label = "loading pipelines..."
        try:
            sb = self.query_one("#statusbar", StatusBar)
            sb.set_text(_loading_indicator(self._user_loading_label))
        except Exception:
            pass
        self.call_after_refresh(lambda: asyncio.create_task(self._initial_load()))

    async def _initial_load(self) -> None:
        try:
            await self.load_pipelines()
        finally:
            self._clear_loading()
        try:
            self.query_one("#mr-pipeline-table", DataTable).focus()
        except Exception:
            pass
        active = any(p['status'] not in TERMINAL_STATUSES for p in (self.pipelines + self.related))
        if active:
            self._refresh_timer = self.set_interval(self.REFRESH_INTERVAL, self._safe_refresh)
        self._refresh_status()

    async def load_pipelines(self) -> None:
        merged_at = self.mr.get('merged_at') or ''
        target_branch = self.mr.get('target_branch') or ''
        if merged_at and target_branch:
            mr_pipes, related = await asyncio.gather(
                asyncio.to_thread(self.api.get_mr_pipelines, self.project_path, self.mr['iid']),
                asyncio.to_thread(self.api.list_pipelines_for_ref_since, self.project_path, target_branch, merged_at),
            )
            self.pipelines = mr_pipes or []
            mr_ids = {p['id'] for p in self.pipelines}
            self.related = [p for p in (related or []) if p.get('id') not in mr_ids]
        else:
            self.pipelines = await asyncio.to_thread(self.api.get_mr_pipelines, self.project_path, self.mr['iid'])
            self.related = []
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
        self._row_pipelines = []
        for i, p in enumerate(self.pipelines):
            id_text = Text(str(p['id']), style="bold #89b4fa" if i == 0 else "bold")
            ref_text = Text((p['ref'] or '')[:40], style="cyan")
            table.add_row(
                Text("MR", style="bold #89b4fa"),
                id_text,
                status_badge(p['status']),
                ref_text,
                Text(p['sha'], style="dim"),
                Text(format_age(p['updated_at'] or p['created_at']), style="dim italic"),
            )
            self._row_pipelines.append(p)
        for p in self.related:
            ref_text = Text((p['ref'] or '')[:40], style="cyan")
            table.add_row(
                Text("related", style="bold #f9e2af"),
                Text(str(p['id']), style="#cdd6f4"),
                status_badge(p['status']),
                ref_text,
                Text(p['sha'], style="dim"),
                Text(format_age(p['updated_at'] or p['created_at']), style="dim italic"),
            )
            self._row_pipelines.append(p)
        total = len(self._row_pipelines)
        if prev is not None and total:
            table.move_cursor(row=min(prev, total - 1))
        self._refresh_status()

    async def action_back(self) -> None:
        self.app.pop_screen()

    async def action_refresh(self) -> None:
        if self._refreshing:
            return
        await self._show_loading("loading pipelines...")
        try:
            await self.load_pipelines()
        finally:
            self._clear_loading()

    async def action_browser(self) -> None:
        table = self.query_one("#mr-pipeline-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self._row_pipelines):
            url = self._row_pipelines[table.cursor_row].get('web_url')
            if url:
                webbrowser.open(url)

    async def action_yank(self) -> None:
        table = self.query_one("#mr-pipeline-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self._row_pipelines):
            url = self._row_pipelines[table.cursor_row].get('web_url')
            if url and copy_to_clipboard(url):
                self.notify("Copied URL", timeout=2)

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if idx is None or idx >= len(self._row_pipelines):
            return
        pipeline = self._row_pipelines[idx]
        ds_api = GitLabAPI(self.api.config)
        try:
            ds_api.set_project(self.project_path)
        except Exception:
            self.notify(f"Cannot access {self.project_path}", severity="error", timeout=3)
            return
        self.app.push_screen(JobListScreen(ds_api, pipeline))


class MyPipelineListScreen(ScreenBase):

    KEY_MAP = {"q": "back", "r": "refresh", "b": "browser", "y": "yank", "t": "toggle_age", "slash": "search", "c": "clear_filter", "tab": "open_modules", "a": "pick_project"}

    REFRESH_INTERVAL = PIPELINE_REFRESH_INTERVAL

    def __init__(self, api: GitLabAPI, age_days=DEFAULT_PIPELINE_AGE_DAYS):
        super().__init__()
        self.api = api
        self.age_days = age_days
        self.pipelines = []
        self.filtered_pipelines = []
        self.username = ''
        self._row_to_pipeline = []
        self._refresh_timer = None
        self._refreshing = False

    def _age_label(self) -> str:
        return f"last {self.age_days}d" if self.age_days is not None else "all"

    def _favorites(self):
        return self.api.config.favorites.list()

    def _info_pairs(self):
        favs = self._favorites()
        return [
            ("GitLab", self.api.config.gitlab_url),
            ("User", self.username or "—"),
            ("Favorites", str(len(favs))),
            ("Scope", self._age_label()),
            ("Pipelines", str(len(self.pipelines))),
        ]

    def _keys(self):
        return [
            [("tab", "modules"), ("/", "filter"),   ("enter", "jobs")],
            [("r", "refresh"),   ("b", "browser"),  ("y", "copy url")],
            [("a", "pick project"), ("t", "age"),   ("c", "clear")],
            [("q", "back")],
        ]

    def compose(self) -> ComposeResult:
        yield K9sHeader(self._info_pairs(), self._keys(), id="header")
        yield Static(self._breadcrumb(), id="breadcrumb", classes="breadcrumb")
        yield Container(
            Input(placeholder="/  filter pipelines...", id="my-pipe-filter"),
            id="filter-bar",
        )
        yield DataTable(id="my-pipe-table")
        yield StatusBar(self._status_text(), id="statusbar")

    def _breadcrumb(self):
        return _breadcrumb_text(["My Pipelines"])

    def _status_text(self):
        total = len(self.pipelines)
        shown = len(self.filtered_pipelines)
        count = f"{shown}/{total} pipelines" if shown != total else f"{total} pipelines"
        parts = [count, ("scope", self._age_label())]
        if not self._favorites():
            parts.append("no favorites starred — press 's' on Projects to star")
        return _status_line(parts)

    def _refresh_status(self) -> None:
        try:
            sb = self.query_one("#statusbar", StatusBar)
            sb.set_text(_loading_indicator(self._user_loading_label) if self._user_loading_label else self._status_text())
            sb.set_right(_auto_refresh_indicator(
                self.REFRESH_INTERVAL,
                active=self._refresh_timer is not None,
                refreshing=self._refreshing,
                loading_label=self._user_loading_label,
            ))
        except Exception:
            pass

    async def on_mount(self) -> None:
        table = self.query_one("#my-pipe-table", DataTable)
        table.add_columns("Project", "ID", "Status", "Branch", "SHA", "Age")
        table.cursor_type = "row"
        self._user_loading_label = "loading my pipelines..."
        try:
            sb = self.query_one("#statusbar", StatusBar)
            sb.set_text(_loading_indicator(self._user_loading_label))
        except Exception:
            pass
        self.call_after_refresh(lambda: asyncio.create_task(self._initial_load()))

    async def _initial_load(self) -> None:
        try:
            await self.load_pipelines()
        finally:
            self._clear_loading()
        try:
            self.query_one("#my-pipe-table", DataTable).focus()
        except Exception:
            pass
        if any(p['status'] not in TERMINAL_STATUSES for p in self.pipelines):
            self._refresh_timer = self.set_interval(self.REFRESH_INTERVAL, self._safe_refresh)
        self._refresh_status()
        self.api.config.save_last_view('my_pipelines')

    async def load_pipelines(self) -> None:
        self.username = await asyncio.to_thread(self.api.current_username)
        favs = self._favorites()
        if not self.username or not favs:
            self.pipelines = []
            self._apply_filter()
            try:
                self.query_one("#header", K9sHeader).set_info(self._info_pairs())
            except Exception:
                pass
            return
        results = await asyncio.gather(*(
            asyncio.to_thread(
                self.api.list_my_pipelines_for_project,
                proj, self.username, 25, self.age_days,
            ) for proj in favs
        ), return_exceptions=True)
        all_pipes = []
        for r in results:
            if isinstance(r, list):
                all_pipes.extend(r)
        all_pipes.sort(key=lambda p: p.get('updated_at') or p.get('created_at') or '', reverse=True)
        self.pipelines = all_pipes
        self._apply_filter()
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

    def _apply_filter(self) -> None:
        try:
            q = self.query_one("#my-pipe-filter", Input).value.strip().lower()
        except Exception:
            q = ""
        if not q:
            self.filtered_pipelines = list(self.pipelines)
        else:
            self.filtered_pipelines = [
                p for p in self.pipelines
                if q in (p.get('project_path') or '').lower()
                or q in (p.get('ref') or '').lower()
                or q in (p.get('status') or '').lower()
                or q in str(p.get('id') or '')
                or q in (p.get('sha') or '').lower()
            ]
        self._update_table()
        self._refresh_status()

    def _update_table(self) -> None:
        table = self.query_one("#my-pipe-table", DataTable)
        prev = table.cursor_row
        table.clear()
        self._row_to_pipeline = []
        groups = {}
        order = []
        for p in self.filtered_pipelines:
            proj = p.get('project_path') or ''
            if proj not in groups:
                groups[proj] = []
                order.append(proj)
        for p in self.filtered_pipelines:
            groups[p.get('project_path') or ''].append(p)
        blank = Text("")
        for proj in order:
            repo = (proj.rsplit('/', 1)[-1] if proj else 'UNKNOWN').upper()
            header = Text(repo, style="bold #89b4fa")
            table.add_row(header, blank, blank, blank, blank, blank)
            self._row_to_pipeline.append(None)
            for p in groups[proj]:
                age = format_age(p.get('updated_at') or p.get('created_at') or '')
                table.add_row(
                    Text("  " + proj, style="dim"),
                    Text(str(p['id']), style="bold"),
                    status_badge(p['status']),
                    Text((p.get('ref') or '')[:35], style="cyan"),
                    Text(p['sha'], style="dim"),
                    Text(age, style="dim italic"),
                )
                self._row_to_pipeline.append(p)
        if prev is not None and self._row_to_pipeline:
            target = min(prev, len(self._row_to_pipeline) - 1)
            while target < len(self._row_to_pipeline) and self._row_to_pipeline[target] is None:
                target += 1
            if target >= len(self._row_to_pipeline):
                target = next((i for i, x in enumerate(self._row_to_pipeline) if x is not None), 0)
            table.move_cursor(row=target)

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "my-pipe-filter":
            self._apply_filter()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "my-pipe-filter":
            self._apply_filter()
            self.query_one("#my-pipe-table", DataTable).focus()

    def _pipeline_at_row(self, idx):
        if idx is None or idx < 0 or idx >= len(self._row_to_pipeline):
            return None
        return self._row_to_pipeline[idx]

    async def action_back(self) -> None:
        self.app.exit()

    async def action_open_modules(self) -> None:
        self.app.open_modules()

    async def action_pick_project(self) -> None:
        self.app.push_screen(ProjectSelectScreen(self.api, self.api.config.favorites))

    async def action_refresh(self) -> None:
        if self._refreshing:
            return
        await self._show_loading("loading my pipelines...")
        try:
            await self.load_pipelines()
        finally:
            self._clear_loading()

    async def action_search(self) -> None:
        self.query_one("#my-pipe-filter", Input).focus()

    async def action_clear_filter(self) -> None:
        inp = self.query_one("#my-pipe-filter", Input)
        if not inp.value:
            return
        inp.value = ""
        self._apply_filter()

    async def action_toggle_age(self) -> None:
        try:
            idx = PIPELINE_AGE_CYCLE.index(self.age_days)
        except ValueError:
            idx = -1
        self.age_days = PIPELINE_AGE_CYCLE[(idx + 1) % len(PIPELINE_AGE_CYCLE)]
        await self._show_loading("loading my pipelines...")
        try:
            await self.load_pipelines()
        finally:
            self._clear_loading()

    async def action_browser(self) -> None:
        table = self.query_one("#my-pipe-table", DataTable)
        p = self._pipeline_at_row(table.cursor_row)
        if p and p.get('web_url'):
            webbrowser.open(p['web_url'])

    async def action_yank(self) -> None:
        table = self.query_one("#my-pipe-table", DataTable)
        p = self._pipeline_at_row(table.cursor_row)
        if p and p.get('web_url') and copy_to_clipboard(p['web_url']):
            self.notify(f"Copied pipeline #{p['id']} URL", timeout=2)

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        p = self._pipeline_at_row(event.cursor_row)
        if p is None:
            return
        proj = p.get('project_path') or ''
        if not proj:
            return
        ds_api = GitLabAPI(self.api.config)
        try:
            ds_api.set_project(proj)
        except Exception:
            self.notify(f"Cannot access {proj}", severity="error", timeout=3)
            return
        self.app.push_screen(JobListScreen(ds_api, p))


class MRCommitListScreen(ScreenBase):

    KEY_MAP = {"q": "back", "r": "refresh", "b": "browser", "y": "yank"}

    def __init__(self, api: GitLabAPI, project_path: str, mr: dict):
        super().__init__()
        self.api = api
        self.project_path = project_path
        self.mr = mr
        self.commits = []
        self._refreshing = False

    def _info_pairs(self):
        return [
            ("GitLab", self.api.config.gitlab_url),
            ("Project", self.project_path),
            ("MR", f"!{self.mr['iid']}  {(self.mr.get('title') or '')[:40]}"),
            ("Source", self.mr.get('source_branch') or ''),
            ("Commits", str(len(self.commits))),
        ]

    def _keys(self):
        return [
            [("r", "refresh"), ("b", "browser"), ("y", "copy sha")],
            [("q", "back")],
        ]

    def compose(self) -> ComposeResult:
        yield K9sHeader(self._info_pairs(), self._keys(), id="header")
        yield Static(self._breadcrumb(), id="breadcrumb", classes="breadcrumb")
        yield DataTable(id="mr-commit-table")
        yield StatusBar(self._status_text(), id="statusbar")

    def _breadcrumb(self):
        return _breadcrumb_text([
            self.project_path or "Project",
            f"MR !{self.mr['iid']}",
            "Commits",
        ])

    def _status_text(self):
        return _status_line([f"{len(self.commits)} commits", ("MR", f"!{self.mr['iid']}")])

    def _refresh_status(self) -> None:
        try:
            sb = self.query_one("#statusbar", StatusBar)
            sb.set_text(_loading_indicator(self._user_loading_label) if self._user_loading_label else self._status_text())
            if self._user_loading_label:
                sb.set_right(_loading_indicator(self._user_loading_label))
            else:
                sb.set_right("")
        except Exception:
            pass

    async def on_mount(self) -> None:
        table = self.query_one("#mr-commit-table", DataTable)
        table.add_columns("SHA", "Title", "Created", "Author", "Pipeline")
        table.cursor_type = "row"
        self._user_loading_label = "loading commits..."
        try:
            sb = self.query_one("#statusbar", StatusBar)
            sb.set_text(_loading_indicator(self._user_loading_label))
        except Exception:
            pass
        self.call_after_refresh(lambda: asyncio.create_task(self._initial_load()))

    async def _initial_load(self) -> None:
        try:
            await self.load_commits()
        finally:
            self._clear_loading()
        try:
            self.query_one("#mr-commit-table", DataTable).focus()
        except Exception:
            pass
        self._refresh_status()

    async def load_commits(self) -> None:
        commits = await asyncio.to_thread(self.api.get_mr_commits, self.project_path, self.mr['iid'])
        if commits:
            statuses = await asyncio.gather(*(
                asyncio.to_thread(self.api.get_commit_pipeline_status, self.project_path, c['sha']) for c in commits
            ), return_exceptions=True)
            for c, s in zip(commits, statuses):
                c['pipeline_status'] = s if isinstance(s, str) else ''
        self.commits = commits
        self._update_table()
        try:
            self.query_one("#header", K9sHeader).set_info(self._info_pairs())
        except Exception:
            pass

    def _update_table(self) -> None:
        table = self.query_one("#mr-commit-table", DataTable)
        prev = table.cursor_row
        table.clear()
        for c in self.commits:
            title = (c.get('title') or '')[:70]
            status = c.get('pipeline_status') or ''
            pipeline_cell = status_badge(status) if status else Text('—', style="dim")
            table.add_row(
                Text(c['short_sha'], style="bold #89b4fa"),
                Text(title),
                Text(format_age(c.get('created_at') or ''), style="dim italic"),
                Text(c.get('author_name') or 'unknown', style="dim"),
                pipeline_cell,
            )
        if prev is not None and self.commits:
            table.move_cursor(row=min(prev, len(self.commits) - 1))
        self._refresh_status()

    async def action_back(self) -> None:
        self.app.pop_screen()

    async def action_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        await self._show_loading("loading commits...")
        try:
            await self.load_commits()
        finally:
            self._refreshing = False
            self._clear_loading()

    async def action_browser(self) -> None:
        table = self.query_one("#mr-commit-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self.commits):
            url = self.commits[table.cursor_row].get('web_url')
            if url:
                webbrowser.open(url)

    async def action_yank(self) -> None:
        table = self.query_one("#mr-commit-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self.commits):
            sha = self.commits[table.cursor_row].get('sha') or ''
            if sha and copy_to_clipboard(sha):
                self.notify(f"Copied {sha[:8]}", timeout=2)

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        await self.action_browser()


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


class PathInputModal(ModalScreen[str | None]):
    """Single-line path input. Enter submits, esc cancels."""

    def __init__(self, title: str, placeholder: str = "", initial: str = ""):
        super().__init__()
        self.title_text = title
        self.placeholder = placeholder
        self.initial = initial

    def compose(self) -> ComposeResult:
        header = Text()
        header.append(self.title_text, style="bold #cdd6f4")
        header.append("\n")
        header.append("enter submit · esc cancel", style="dim #a6adc8")
        yield Container(
            Static(header, id="path-input-header"),
            Input(value=self.initial, placeholder=self.placeholder, id="path-input"),
            id="path-input-box",
        )

    def on_mount(self) -> None:
        try:
            inp = self.query_one("#path-input", Input)
            inp.focus()
            inp.cursor_position = len(inp.value)
        except Exception:
            pass

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.prevent_default()
            event.stop()
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "path-input":
            text = (event.value or "").strip()
            self.dismiss(text if text else None)


class MRNoteModal(ModalScreen[tuple[str, str] | None]):
    """Per-MR local note editor.

    Returns ('save', text) on ctrl+s, ('delete', '') on ctrl+d, None on esc.
    """

    def __init__(self, project_repo: str, mr_iid: int, initial: str = ""):
        super().__init__()
        self.project_repo = project_repo
        self.mr_iid = mr_iid
        self.initial = initial

    def compose(self) -> ComposeResult:
        header = Text()
        header.append(f"Note · {self.project_repo} !{self.mr_iid}", style="bold #cdd6f4")
        header.append("\n")
        hints = "ctrl+s save · esc cancel"
        if self.initial:
            hints = "ctrl+s save · ctrl+d delete · esc cancel"
        header.append(hints, style="dim #a6adc8")
        ta = TextArea(self.initial, id="mr-note-area")
        ta.show_line_numbers = False
        yield Container(
            Static(header, id="mr-note-header"),
            ta,
            id="mr-note-box",
        )

    def on_mount(self) -> None:
        try:
            ta = self.query_one("#mr-note-area", TextArea)
            ta.focus()
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
                text = self.query_one("#mr-note-area", TextArea).text
            except Exception:
                text = ""
            self.dismiss(("save", text.strip()))
        elif event.key == "ctrl+d":
            event.prevent_default()
            event.stop()
            self.dismiss(("delete", ""))


class AboutModal(ModalScreen[None]):
    """About modal showing version, credit, links. g/b open URLs, esc closes."""

    GITHUB_URL = "https://github.com/bearded-giant/gitlab-tools"
    SITE_URL = "https://beardedgiantllc.com"

    def compose(self) -> ComposeResult:
        body = Text()
        body.append("GitLab Monitor (glmon)\n", style="bold #cdd6f4")
        body.append("\n")
        body.append("version ", style="#6c7086")
        body.append(f"{__version__}\n", style="bold #cdd6f4")
        body.append("\n")
        body.append("by ", style="#6c7086")
        body.append("Bearded Giant LLC\n", style="bold #d97706")
        body.append("\n")
        body.append(f"{self.GITHUB_URL}\n", style="#89b4fa underline")
        body.append(f"{self.SITE_URL}\n", style="#89b4fa underline")
        body.append("\n")
        body.append("g ", style="bold #f9e2af")
        body.append("github  ·  ", style="#6c7086")
        body.append("b ", style="bold #f9e2af")
        body.append("website  ·  ", style="#6c7086")
        body.append("esc ", style="bold #f9e2af")
        body.append("close", style="#6c7086")
        yield Container(
            Static(body, id="about-body"),
            id="about-box",
        )

    def on_key(self, event) -> None:
        if event.key == "escape" or event.key == "question_mark":
            event.prevent_default()
            event.stop()
            self.dismiss(None)
        elif event.key == "g":
            event.prevent_default()
            event.stop()
            webbrowser.open(self.GITHUB_URL)
        elif event.key == "b":
            event.prevent_default()
            event.stop()
            webbrowser.open(self.SITE_URL)


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


MODULE_PROJECTS = "projects"
MODULE_MRS = "mrs"
MODULE_PIPELINES = "pipelines"


class ModuleModal(ModalScreen[str | None]):
    """Module switcher. Returns selected module id or None on cancel."""

    MODULES = [
        (MODULE_PROJECTS,  "1", "Projects",  "favorites + jump to project pipelines"),
        (MODULE_MRS,       "2", "MRs",       "my merge requests"),
        (MODULE_PIPELINES, "3", "Pipelines", "my pipelines (across favorites)"),
    ]

    BINDINGS = [
        Binding("1", "pick_index('0')", show=False),
        Binding("2", "pick_index('1')", show=False),
        Binding("3", "pick_index('2')", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("escape", "cancel", show=False),
        Binding("q", "cancel", show=False),
        Binding("tab", "cancel", show=False),
    ]

    def compose(self) -> ComposeResult:
        from textual.widgets import OptionList
        from textual.widgets.option_list import Option

        title = Text()
        title.append("Modules", style="bold #cdd6f4")
        footer = Text()
        footer.append("↑↓/jk move · enter select · 1-3 jump · esc/q/tab close", style="dim #6c7086")
        options = []
        for mod_id, num, name, desc in self.MODULES:
            row = Text()
            row.append(f"<{num}>  ", style="bold #89b4fa")
            row.append(f"{name:<10}", style="bold #cdd6f4")
            row.append(f"  {desc}", style="#a6adc8")
            options.append(Option(row, id=mod_id))
        yield Container(
            Static(title, id="modules-title"),
            OptionList(*options, id="modules-list"),
            Static(footer, id="modules-footer"),
            id="modules-box",
        )

    def on_mount(self) -> None:
        try:
            from textual.widgets import OptionList
            ol = self.query_one("#modules-list", OptionList)
            ol.highlighted = 0
            ol.focus()
        except Exception:
            pass

    def _list(self):
        from textual.widgets import OptionList
        return self.query_one("#modules-list", OptionList)

    def action_cursor_up(self) -> None:
        try:
            ol = self._list()
            n = ol.option_count
            ol.highlighted = ((ol.highlighted or 0) - 1) % n
        except Exception:
            pass

    def action_cursor_down(self) -> None:
        try:
            ol = self._list()
            n = ol.option_count
            ol.highlighted = ((ol.highlighted or 0) + 1) % n
        except Exception:
            pass

    def action_pick_index(self, idx: str) -> None:
        try:
            i = int(idx)
        except ValueError:
            return
        if 0 <= i < len(self.MODULES):
            self.dismiss(self.MODULES[i][0])

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_option_list_option_selected(self, event) -> None:
        opt_id = getattr(event.option, "id", None)
        if opt_id:
            self.dismiss(opt_id)


# -- app ---------------------------------------------------------------------

class PipelineMonitor(App):

    TITLE = "glmon"

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        Binding("question_mark", "about", "About", show=False, priority=True),
    ]

    async def action_about(self) -> None:
        if isinstance(self.screen, AboutModal):
            return
        for s in self.screen_stack:
            if isinstance(s, AboutModal):
                return
        self.push_screen(AboutModal())

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

    .breadcrumb {
        height: 1;
        padding: 0 2;
        background: #1e1e2e;
        color: #89b4fa;
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

    #statusbar #status-loading {
        width: 30;
        background: #313244;
        color: #f9e2af;
        content-align: right middle;
        padding: 0 2 0 0;
    }

    #statusbar #status-right {
        width: auto;
        background: #313244;
        color: #a6adc8;
        content-align: right middle;
    }

    #statusbar #status-version {
        width: auto;
        background: #313244;
        color: #6c7086;
        content-align: right middle;
        padding: 0 0 0 0;
    }

    AboutModal {
        align: center middle;
        background: #1e1e2e 70%;
    }

    #about-box {
        width: 60;
        height: auto;
        background: #313244;
        border: tall #d97706;
        padding: 1 3;
    }

    #about-body {
        width: 100%;
        height: auto;
        content-align: center middle;
        color: #cdd6f4;
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

    ModuleModal {
        align: center middle;
        background: #1e1e2e 70%;
    }

    #modules-box {
        width: 70;
        height: auto;
        background: #313244;
        border: tall #89b4fa;
        padding: 1 2;
    }

    #modules-title {
        width: 100%;
        height: 1;
        color: #cdd6f4;
        margin-bottom: 1;
    }

    #modules-list {
        width: 100%;
        height: auto;
        background: #313244;
        color: #cdd6f4;
    }

    #modules-list > .option-list--option-highlighted {
        background: #45475a;
        color: #f9e2af;
    }

    #modules-footer {
        width: 100%;
        height: 1;
        color: #6c7086;
        margin-top: 1;
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

    MRNoteModal {
        align: center middle;
        background: #1e1e2e 60%;
    }

    #mr-note-box {
        width: 80;
        height: 20;
        background: #313244;
        border: tall #f9e2af;
        padding: 1 2;
    }

    #mr-note-header {
        width: 100%;
        height: auto;
        color: #cdd6f4;
        margin-bottom: 1;
    }

    #mr-note-area {
        width: 100%;
        height: 1fr;
        background: #1e1e2e;
        color: #cdd6f4;
    }

    PathInputModal {
        align: center middle;
        background: #1e1e2e 60%;
    }

    #path-input-box {
        width: 80;
        height: auto;
        background: #313244;
        border: tall #89b4fa;
        padding: 1 2;
    }

    #path-input-header {
        width: 100%;
        height: auto;
        color: #cdd6f4;
        margin-bottom: 1;
    }

    #path-input {
        width: 100%;
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

    def open_modules(self) -> None:
        for s in self.screen_stack:
            if isinstance(s, ModuleModal):
                return

        def _after(result):
            if not result:
                return
            if result == MODULE_PROJECTS:
                self.switch_screen(ProjectSelectScreen(self.api, self.config.favorites))
            elif result == MODULE_MRS:
                self.switch_screen(MyMergeRequestsScreen(self.api))
            elif result == MODULE_PIPELINES:
                self.switch_screen(MyPipelineListScreen(self.api, age_days=self.default_age_days))

        self.push_screen(ModuleModal(), _after)

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

        def _default_home():
            if self.config.favorites.list():
                return MyPipelineListScreen(self.api, age_days=self.default_age_days)
            return ProjectSelectScreen(self.api, self.config.favorites)

        # explicit -p / GITLAB_PROJECT wins over resume
        if self.explicit_project and self.api.project:
            self.switch_screen(ProjectSelectScreen(self.api, self.config.favorites))
            self.push_screen(PipelineListScreen(
                self.api,
                age_days=self.default_age_days,
                initial_filter=self.default_branch_filter,
            ))
            return
        if not self.resume:
            self.switch_screen(_default_home())
            return
        last = self.config.get_last_view()
        if not last:
            if self.api.project:
                self.switch_screen(ProjectSelectScreen(self.api, self.config.favorites))
                self.push_screen(PipelineListScreen(
                    self.api,
                    age_days=self.default_age_days,
                    initial_filter=self.default_branch_filter,
                ))
                return
            self.switch_screen(_default_home())
            return
        view_type = last.get('type')
        try:
            if view_type == 'my_mrs':
                self.switch_screen(MyMergeRequestsScreen(self.api))
            elif view_type == 'my_pipelines':
                self.switch_screen(MyPipelineListScreen(self.api, age_days=self.default_age_days))
            elif view_type == 'pipelines':
                proj = last.get('project')
                if proj:
                    try:
                        await asyncio.to_thread(self.api.set_project, proj)
                        self.switch_screen(ProjectSelectScreen(self.api, self.config.favorites))
                        self.push_screen(PipelineListScreen(
                            self.api,
                            age_days=self.default_age_days,
                            initial_filter=self.default_branch_filter,
                        ))
                        return
                    except Exception:
                        self.notify(f"Could not resume project {proj}", severity="warning", timeout=3)
                self.switch_screen(_default_home())
            else:
                self.switch_screen(_default_home())
        except Exception:
            self.switch_screen(_default_home())


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
    _dbg(f"=== glmon start version={__version__} argv={sys.argv[1:]!r} ===")
    parser = argparse.ArgumentParser(prog="glmon", description="GitLab pipeline monitor TUI")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
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
