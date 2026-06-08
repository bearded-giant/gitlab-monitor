# Copyright 2024 BeardedGiant
# https://github.com/bearded-giant/gitlab-tools
# Licensed under Apache License 2.0

import subprocess
from datetime import datetime
from rich.text import Text

from .constants import (
    STATUS_STYLES,
    INFO_PANE_WIDTH,
    KEY_COLS,
    _DEBUG_LOG_ENABLED,
    _DEBUG_LOG_PATH,
)


def _dbg(msg: str) -> None:
    if not _DEBUG_LOG_ENABLED:
        return
    try:
        with open(_DEBUG_LOG_PATH, "a") as f:
            f.write(f"{datetime.now().isoformat()} {msg}\n")
    except Exception:
        pass


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
    if isinstance(keys[0], list):
        return _key_lines_from_rows(keys, cols)
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


def _key_lines_from_rows(rows, cols):
    n_cols = min(cols, max((len(r) for r in rows), default=0))
    col_widths = []
    for ci in range(n_cols):
        max_k = 0
        max_a = 0
        for r in rows:
            entry = r[ci] if ci < len(r) else None
            if entry is None:
                continue
            k, a = entry
            max_k = max(max_k, len(f"<{k}>"))
            max_a = max(max_a, len(a))
        col_widths.append((max_k, max_a))
    lines = []
    for r in rows:
        line = Text()
        for ci in range(n_cols):
            kw, aw = col_widths[ci]
            entry = r[ci] if ci < len(r) else None
            if entry is not None:
                k, a = entry
                line.append(f"<{k}>".ljust(kw), style="bold #89b4fa")
                line.append(" ")
                line.append(a.ljust(aw + 3), style="#cdd6f4")
            else:
                line.append(" " * (kw + 1 + aw + 3))
        lines.append(line)
    return lines


def _header_layout(available_width):
    if available_width is None or available_width >= 130:
        return INFO_PANE_WIDTH, KEY_COLS, False
    if available_width >= 100:
        return 50, 2, False
    if available_width >= 75:
        return 42, 1, False
    return max(20, available_width - 2), 2, True


def _format_header(info_pairs, keys, available_width=None):
    info_width, cols, stack = _header_layout(available_width)
    info = _info_lines(info_pairs, max_value_width=max(10, info_width - 12))
    krows = _key_lines(keys, cols=cols)
    out = Text()
    if stack:
        for i, line in enumerate(info):
            if i > 0:
                out.append("\n")
            out.append(line)
        for line in krows:
            out.append("\n")
            out.append(line)
        return out
    rows = max(len(info), len(krows))
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


def _breadcrumb_text(parts):
    t = Text()
    sep = Text("  ›  ", style="#585b70")
    first = True
    for p in parts:
        if p is None or p == "":
            continue
        if not first:
            t.append_text(sep)
        first = False
        if isinstance(p, tuple):
            text, style = p
            t.append(text, style=style)
        else:
            t.append(str(p), style="bold #89b4fa")
    return t


def _auto_refresh_indicator(interval_seconds, active=True, refreshing=False, loading_label=None):
    t = Text()
    if refreshing:
        t.append("⟳ ", style="bold #f9e2af")
        t.append("refreshing", style="bold #f9e2af")
        return t
    if not active or interval_seconds is None:
        t.append("auto-refresh: ", style="#6c7086")
        t.append("off", style="bold #f38ba8")
        return t
    t.append("↻ ", style="#a6e3a1")
    t.append("auto-refresh: ", style="#6c7086")
    t.append(f"{interval_seconds}s", style="bold #a6e3a1")
    return t


def _loading_indicator(label="loading"):
    t = Text()
    t.append("⟳ ", style="bold #f9e2af")
    t.append(label, style="bold #f9e2af")
    return t


def _status_line(parts):
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
