# Copyright 2024 BeardedGiant
# https://github.com/bearded-giant/gitlab-tools
# Licensed under Apache License 2.0

import re
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


def format_activity_strip(counts, width):
    if not counts or width is None or width < 5:
        return Text("")
    # counts: newest-first list of {date, day_offset, count}
    segments = []
    for entry in counts:
        offset = entry.get('day_offset', 0)
        n = entry.get('count', 0)
        label = "T" if offset == 0 else f"-{offset}"
        segments.append((label, n))
    sep = " │ "
    while segments:
        t = Text()
        first = True
        for label, n in segments:
            if not first:
                t.append(sep, style="#585b70")
            first = False
            style = "bold #cdd6f4" if n > 0 else "#6c7086"
            t.append(f"{label}:", style="#6c7086")
            t.append(str(n), style=style)
        if t.cell_len <= width:
            return t
        segments.pop()  # drop oldest day, retry
    return Text("")


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


def _pipeline_status_with_id(status, pipeline_id):
    if not status and not pipeline_id:
        return Text("—", style="dim")
    out = Text()
    if status:
        out.append_text(status_badge(status))
    if pipeline_id:
        if status:
            out.append(" ")
        out.append(f"#{pipeline_id}", style="dim")
    return out


def _branch_pair_text(source, target, max_each=20):
    src = (source or '').strip()
    tgt = (target or '').strip()
    if not src and not tgt:
        return Text("—", style="dim")
    if len(src) > max_each:
        src = src[: max_each - 1] + "…"
    if len(tgt) > max_each:
        tgt = tgt[: max_each - 1] + "…"
    out = Text()
    out.append(src, style="#cdd6f4")
    out.append(" → ", style="dim")
    out.append(tgt, style="dim #a6adc8")
    return out


def _is_mr_approved(m):
    if not m or m.get('state') != 'opened':
        return False
    if m.get('head_pipeline_status') != 'success':
        return False
    approved = m.get('approvals_count', 0) or 0
    required = m.get('approvals_required', 0) or 0
    if required > 0:
        return approved >= required
    return approved > 0


def _mr_state_badge(state, m=None):
    if m is not None and _is_mr_approved(m):
        return Text(' APPROVED ', style='bold #181825 on #a6e3a1')
    styles = {
        'opened': ('bold #a6e3a1', ' open '),
        'merged': ('bold #89b4fa', ' merged '),
        'closed': ('bold #f38ba8', ' closed '),
        'locked': ('bold #6c7086', ' locked '),
    }
    style, label = styles.get(state, ('', f' {state} '))
    return Text(label, style=style)


def _auto_merge_badge(m=None):
    if m is not None and m.get('merge_when_pipeline_succeeds'):
        return Text('true', style='bold #f9e2af')
    return Text('false', style='dim #6c7086')


_BADGE_STYLES = {
    'success': 'bold #a6e3a1',
    'warning': 'bold #f9e2af',
    'neutral': 'bold #a6adc8',
    'info': 'bold #89b4fa',
}


def _returned_badge(m, me):
    states = set(m.get('review_states', {}).values())
    if 'REQUESTED_CHANGES' in states:
        return 'Changes requested', 'warning'
    if 'REVIEWED' in states:
        return 'Reviewer commented', 'warning'
    return None


def _requested_badge(m, me):
    mine = m.get('review_states', {}).get(me)
    if mine == 'REVIEW_STARTED':
        return 'Review started', 'warning'
    if mine in ('UNREVIEWED', 'UNAPPROVED'):
        return 'Requested', 'warning'
    return None


def _assigned_badge(m, me):
    if m.get('draft'):
        return 'Draft', 'warning'
    if not [r for r in m.get('reviewers', []) if (r or '').lower() != 'gitlabduo']:
        return 'Reviewers needed', 'warning'
    return None


def _waiting_badge(m, me):
    mine = m.get('review_states', {}).get(me)
    if mine == 'REQUESTED_CHANGES':
        return 'You requested changes', 'neutral'
    if mine == 'REVIEWED':
        return 'You commented', 'neutral'
    return None


def _approval_badge(m, me):
    left = m.get('approvals_left')
    not_approved = [s for s in m.get('review_states', {}).values() if s != 'APPROVED']
    if (left or 0) > 0 or (m.get('approvals_required') == 0 and not_approved):
        n = left or len(not_approved)
        return f"{n} approval{'s' if n != 1 else ''} required", 'neutral'
    if me in m.get('approved_by', []) or left == 0:
        return 'Approved', 'neutral'
    return 'Waiting for approval', 'neutral'


_DASHBOARD_BADGES = {
    'returned_to_you': _returned_badge,
    'reviews_requested': _requested_badge,
    'assigned_to_you': _assigned_badge,
    'waiting_for_assignee': _waiting_badge,
    'waiting_for_approvals': _approval_badge,
    'approved_by_you': _approval_badge,
    'approved_by_others': _approval_badge,
}


def _dashboard_badge(list_id, m, me):
    if m.get('state') == 'merged':
        label = ('Merged', 'info')
    elif m.get('ready'):
        label = ('Ready to merge', 'success')
    else:
        method = _DASHBOARD_BADGES.get(list_id)
        label = method(m, me) if method else None
    if not label:
        return Text('')
    return Text(label[0], style=_BADGE_STYLES[label[1]])


_REVIEW_GLYPHS = {
    'APPROVED': ('✓', '#a6e3a1'),
    'REQUESTED_CHANGES': ('✗', '#f38ba8'),
    'REVIEWED': ('✎', '#f9e2af'),
    'REVIEW_STARTED': ('…', '#f9e2af'),
}


def _reviewers_text(m, limit=2):
    names = m.get('reviewers') or []
    if not names:
        return Text('—', style='dim')
    states = m.get('review_states') or {}
    # acted-on reviewers first: a 9-reviewer MR should show who approved or blocked
    shown = sorted(names, key=lambda u: states.get(u) not in _REVIEW_GLYPHS)[:limit]
    t = Text()
    for i, u in enumerate(shown):
        glyph, color = _REVIEW_GLYPHS.get(states.get(u), ('·', '#6c7086'))
        if i:
            t.append(' ')
        t.append(u, style='#cdd6f4')
        t.append(glyph, style=f'bold {color}')
    if len(names) > limit:
        t.append(f' +{len(names) - limit}', style='dim')
    return t


def _ratio_text(done, total, empty='—'):
    if not total:
        return Text(empty, style='dim')
    return Text(f"{done}/{total}", style='bold #a6e3a1' if done >= total else 'bold #f9e2af')


def _mr_state_color(state, m=None):
    if m is not None and _is_mr_approved(m):
        return '#a6e3a1'
    colors = {
        'opened': '#a6e3a1',
        'merged': '#89b4fa',
        'closed': '#f38ba8',
        'locked': '#6c7086',
    }
    return colors.get(state, '#cdd6f4')


_VER_RE = re.compile(r'^(?P<prefix>[A-Za-z]*)(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?')


def next_minor_version(latest):
    # suggest the next minor (bump minor, zero patch); fall back to latest verbatim if unparseable
    if not latest:
        return ''
    m = _VER_RE.match(latest.strip())
    if not m:
        return latest
    return f"{m.group('prefix')}{int(m.group('major'))}.{int(m.group('minor')) + 1}.0"


def next_patch_version(latest):
    # suggest the next patch (bump patch, or .1 when latest has no patch); fall back verbatim if unparseable
    if not latest:
        return ''
    m = _VER_RE.match(latest.strip())
    if not m:
        return latest
    patch = int(m.group('patch')) if m.group('patch') else 0
    return f"{m.group('prefix')}{int(m.group('major'))}.{int(m.group('minor'))}.{patch + 1}"


_FENCE_RE = re.compile(r'^ {0,3}(`{3,}|~{3,})([^\n]*)\n(.*?)^ {0,3}\1[`~]*[ \t]*$', re.M | re.S)
_DETAILS_RE = re.compile(r'<details\b[^>]*>\s*(?:<summary\b[^>]*>(.*?)</summary>)?|</details\s*>', re.I | re.S)
_STRIP_TAGS_RE = re.compile(r'</?(?:p|div|span|sub|sup|small|center|kbd|ins|u)\b[^>]*>', re.I)


def _keep_fence(m, store):
    info, code = m.group(2).strip(), m.group(3)
    if info.startswith('suggestion'):
        # gitlab suggestion blocks carry only the replacement lines
        store.append(('diff', ''.join(f"+{line}\n" for line in code.rstrip('\n').split('\n'))))
    elif info in ('diff', 'patch'):
        store.append(('diff', code))
    else:
        store.append(m.group(0))
    return f"\x00{len(store) - 1}\x00"


def _keep(m, store):
    store.append(m.group(0))
    return f"\x00{len(store) - 1}\x00"


def _restore(text, store):
    def put(m):
        v = store[int(m.group(1))]
        return v if isinstance(v, str) else v[1]
    return re.sub(r'\x00(\d+)\x00', put, text)


def _html_link(m):
    href, inner = m.group(1).strip(), m.group(2).strip()
    if not inner:
        return ''
    return inner if href in ('', '#') else f"[{inner}]({href}) "


def _html_to_md(text):
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
    text = re.sub(r'<img\b[^>]*?\balt="([^"]*)"[^>]*>', lambda m: f"`{m.group(1)}`" if m.group(1) else '', text, flags=re.I)
    text = re.sub(r'<img\b[^>]*>', '', text, flags=re.I)
    text = re.sub(r'<a\b[^>]*?\bhref="([^"]*)"[^>]*>(.*?)</a>', _html_link, text, flags=re.I | re.S)
    text = re.sub(r'<h([1-6])\b[^>]*>(.*?)</h\1>',
                  lambda m: f"\n\n{'#' * int(m.group(1))} {' '.join(m.group(2).split())}\n\n", text, flags=re.I | re.S)
    text = re.sub(r'<br\s*/?>', '  \n', text, flags=re.I)
    text = re.sub(r'<hr\s*/?>', '\n\n---\n\n', text, flags=re.I)
    text = re.sub(r'</?(?:b|strong)>', '**', text, flags=re.I)
    text = re.sub(r'</?(?:i|em)>', '*', text, flags=re.I)
    text = re.sub(r'</?code>', '`', text, flags=re.I)
    text = _STRIP_TAGS_RE.sub('', text)
    return re.sub(r'\n{3,}', '\n\n', text)


def _plain_title(text, store):
    return ' '.join(_restore(_html_to_md(text), store).replace('#', ' ').replace('*', '').replace('`', '').split())


def _append_md(out, text):
    text = text.strip()
    if text:
        out.append(('md', text))


def diff_text(code):
    out = Text()
    for i, line in enumerate(code.rstrip('\n').split('\n')):
        if i:
            out.append("\n")
        if line.startswith(('+++', '---')):
            style = "bold #cdd6f4"
        elif line.startswith('+'):
            style = "#a6e3a1"
        elif line.startswith('-'):
            style = "#f38ba8"
        elif line.startswith('@@'):
            style = "#89dceb"
        else:
            style = "#a6adc8"
        out.append(line, style=style)
    return out


def _finish_segments(nodes, store):
    out = []
    for node in nodes:
        if node[0] == 'md':
            text = _html_to_md(node[1])
            pos = 0
            for m in re.finditer(r'\x00(\d+)\x00', text):
                v = store[int(m.group(1))]
                if isinstance(v, tuple):
                    _append_md(out, _restore(text[pos:m.start()], store))
                    out.append(v)
                    pos = m.end()
            _append_md(out, _restore(text[pos:], store))
        else:
            children = _finish_segments(node[2], store)
            if children:
                out.append(('details', _plain_title(node[1], store) or 'Details', children))
    return out


def markdown_segments(body):
    store = []
    text = _FENCE_RE.sub(lambda m: _keep_fence(m, store), body or '')
    text = re.sub(r'`[^`\n]+`', lambda m: _keep(m, store), text)
    root = []
    stack = [root]
    pos = 0
    for m in _DETAILS_RE.finditer(text):
        stack[-1].append(('md', text[pos:m.start()]))
        pos = m.end()
        if m.group(0).startswith('</'):
            if len(stack) > 1:
                stack.pop()
            continue
        node = ('details', m.group(1) or '', [])
        stack[-1].append(node)
        stack.append(node[2])
    stack[-1].append(('md', text[pos:]))
    return _finish_segments(root, store)


_GT_SCORE_RE = re.compile(r'greptile_confidence_score:(\d+)|Confidence Score:\s*(\d+)\s*/\s*5', re.I)
_GT_PRIORITY_RE = re.compile(r'<img\b[^>]*\balt="(P\d)"', re.I)
_GT_BADGE_LINK_RE = re.compile(r'<a\b[^>]*>\s*<img\b[^>]*>\s*</a>', re.I)
# prompt-to-fix blocks repeat the finding; mermaid diagrams can't render in a terminal
_GT_DROP_RE = re.compile(
    r'<details\b[^>]*>(?:(?!</details>).)*?(?:Prompt To Fix|```mermaid)(?:(?!</details>).)*</details\s*>',
    re.S | re.I,
)
_GT_TITLE_RE = re.compile(r'\*\*(.+?)\*\*')
_GT_URL_RE = re.compile(r'href="([^"?]*/-/pull/\d+)')


def format_local(iso_str):
    if not iso_str:
        return ""
    return datetime.fromisoformat(iso_str.replace('Z', '+00:00')).astimezone().strftime('%Y-%m-%d %H:%M')


def _is_greptile(d):
    notes = d.get('notes') or []
    return bool(notes) and 'greptile' in (notes[0].get('author') or '').lower()


def is_greptile_summary(d):
    return _is_greptile(d) and 'greptile_summary' in (d['notes'][0].get('body') or '')


def greptile_review(discussions):
    summary = None
    by_sha = {}
    for d in discussions:
        if not _is_greptile(d):
            continue
        note = d['notes'][0]
        body = note.get('body') or ''
        if is_greptile_summary(d):
            m = _GT_SCORE_RE.search(body)
            url = _GT_URL_RE.search(body)
            segments = []
            for seg in markdown_segments(_GT_DROP_RE.sub('', re.sub(r'<h2\b.*?</h2>', '', body, flags=re.S | re.I))):
                segments.extend(seg[2] if seg[0] == 'details' and seg[1] == 'Summary' else [seg])
            summary = {
                'score': int(m.group(1) or m.group(2)) if m else None,
                'updated_at': note.get('updated_at') or note.get('created_at') or '',
                'segments': segments,
                'url': url.group(1) if url else '',
            }
        elif d.get('resolvable'):
            p = _GT_PRIORITY_RE.search(body)
            body = _GT_DROP_RE.sub('', _GT_BADGE_LINK_RE.sub('', body)).strip()
            t = _GT_TITLE_RE.match(body)
            by_sha.setdefault(d.get('head_sha') or '', []).append({
                'priority': p.group(1) if p else '',
                'title': t.group(1) if t else '',
                'segments': markdown_segments(body[t.end():] if t else body),
                'unresolved': bool(d.get('unresolved')),
                'created_at': note.get('created_at') or '',
                'path': d.get('path') or '',
                'line': d.get('line'),
            })
    if summary is None and not by_sha:
        return None
    # shortcut: review # = order of reviewed head shas that drew findings, a clean review in between shifts later numbers; read greptile's review list if that bites
    reviews = sorted(by_sha.values(), key=lambda fs: min(f['created_at'] for f in fs))
    return {**(summary or {'score': None, 'updated_at': '', 'segments': [], 'url': ''}), 'reviews': reviews}
