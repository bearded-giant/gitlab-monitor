# Copyright 2024 BeardedGiant
# https://github.com/bearded-giant/gitlab-tools
# Licensed under Apache License 2.0

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .api import GitLabAPI
from .config import Config
from .reviewd import Lock, _key, _log, _parse_ts, claude_argv, parse_claude_output, resolve_mr

STATE_DIR = Path.home() / '.cache' / 'glmon'
STATE_FILE = STATE_DIR / 'jirad-state.json'
SUMMARY_MODEL = 'haiku'
SUMMARY_PROMPT = (
    "Summarize the purpose of this merge request for a Jira ticket in at most 3 short bullet points. "
    "Plain language a non-engineer can read: no code, identifiers, file paths, routes, or implementation detail. "
    "Output only the bullets, one per line, each starting with '- '.\n\nTitle: {title}\n\n{description}"
)
IN_PROGRESS, IN_REVIEW, DONE, WONT_FIX = 'In Progress', 'In Review', 'Done', "Won't fix"
SYNC_OWNED = {s.lower() for s in (IN_PROGRESS, IN_REVIEW, WONT_FIX)}
# no head pipeline means the project runs no MR CI, so nothing is left to go green
GREEN = {'success', None}
DRAFT_RE = re.compile(r'^\s*(?:draft:|\[draft\]|\(draft\))\s*', re.IGNORECASE)


def find_key(project, *texts):
    pat = re.compile(rf'\b{re.escape(project)}-(\d+)\b', re.IGNORECASE)
    for text in texts:
        for m in pat.finditer(text or ''):
            if int(m.group(1)):
                return f"{project.upper()}-{int(m.group(1))}"
    return None


def jira_summary(title, project):
    t = DRAFT_RE.sub('', title, count=1)
    t = re.sub(rf'\s*[\[(]?\b{re.escape(project)}-\d+\b[\])]?', '', t, flags=re.IGNORECASE)
    return t.strip().lstrip(':').strip() or title.strip()


def retitle(title, key, project):
    placeholder = re.compile(rf'\b{re.escape(project)}-0+\b', re.IGNORECASE)
    if placeholder.search(title):
        return placeholder.sub(key, title, count=1)
    draft = DRAFT_RE.match(title)
    return f"{draft.group(0) if draft else ''}[{key}] {jira_summary(title, project)}"


def desired_status(mr):
    if mr['state'] == 'merged':
        return DONE
    if mr['state'] == 'closed':
        return WONT_FIX
    if mr['state'] != 'opened':
        return None
    if not mr.get('draft') and mr.get('head_pipeline_status') in GREEN:
        return IN_REVIEW
    return IN_PROGRESS


def summarize(text, limit=3):
    m = re.search(r'^#+\s*description\s*$(.*?)(?=^#+\s|\Z)', text or '', re.MULTILINE | re.DOTALL | re.IGNORECASE)
    body = re.sub(r'```.*?```', '', m.group(1) if m else (text or ''), flags=re.DOTALL)
    out = []
    for raw in body.splitlines():
        line = re.sub(r'^\s*(?:[-*+]|\d+[.)])\s+', '', raw).replace('`', '').replace('**', '').strip()
        if line and not line.startswith(('#', '<!--', '|', '>')):
            out.append(line[:200])
        if len(out) == limit:
            break
    return out


def claude_describe(mr):
    prompt = SUMMARY_PROMPT.format(title=mr['title'], description=(mr.get('description') or '')[:6000])
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(claude_argv(prompt, SUMMARY_MODEL), cwd=str(STATE_DIR),
                              capture_output=True, text=True, timeout=180, check=False)
        lines = summarize(parse_claude_output(proc.stdout).get('result')) if proc.returncode == 0 else []
        if lines:
            return lines
        _log(f"summary via claude empty (exit {proc.returncode}); using the description's first line")
    except Exception as e:
        _log(f"summary via claude failed: {e}; using the description's first line")
    return summarize(mr.get('description'), limit=1)


describe_mr = claude_describe


def excluded(project_path, namespaces):
    return any(project_path == ns.rstrip('/') or project_path.startswith(ns.rstrip('/') + '/') for ns in namespaces)


def can_move(issue, target):
    if issue['status'].lower() == target.lower():
        return False
    return issue['category'] == 'new' or issue['status'].lower() in SYNC_OWNED


class Jira:

    def __init__(self, base, email, token):
        self.base = base.rstrip('/')
        self.auth = 'Basic ' + base64.b64encode(f"{email}:{token}".encode()).decode()

    def _req(self, method, path, body=None):
        req = urllib.request.Request(
            f"{self.base}/rest/api/2/{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': self.auth, 'Accept': 'application/json', 'Content-Type': 'application/json'},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"jira {method} {path}: {e.code} {e.read()[:300]!r}") from None
        return json.loads(raw) if raw else {}

    def myself(self):
        return self._req('GET', 'myself')['accountId']

    def get_issue(self, key):
        status = self._req('GET', f'issue/{key}?fields=status,labels,summary')['fields']
        return {'status': status['status']['name'], 'category': status['status']['statusCategory']['key'],
                'labels': status.get('labels') or [], 'summary': status.get('summary') or ''}

    def create_issue(self, project, issue_type, summary, lines, label, account_id):
        fields = {
            'project': {'key': project},
            'issuetype': {'name': issue_type},
            'summary': summary[:255],
            'description': '\n'.join(f"* {line}" for line in lines),
            'labels': [label],
        }
        if account_id:
            fields['assignee'] = {'accountId': account_id}
        return self._req('POST', 'issue', {'fields': fields})['key']

    def remote_link(self, key, url, title):
        self._req('POST', f'issue/{key}/remotelink', {'globalId': url, 'object': {'url': url, 'title': title}})

    def transition(self, key, status):
        options = self._req('GET', f'issue/{key}/transitions')['transitions']
        match = next((t for t in options if t['to']['name'].lower() == status.lower()), None)
        if not match:
            raise RuntimeError(f"{key}: no transition to {status!r}")
        self._req('POST', f'issue/{key}/transitions', {'transition': {'id': match['id']}})


def load_state(path):
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, path)


def sync_one(mr, api, jira, cfg, state, dry_run=False, summary_lines=None):
    project, tracked = cfg['project'], state.setdefault('mrs', {})
    mkey = _key(mr)
    seen = tracked.get(mkey) or {}
    key = find_key(project, mr['title'], mr.get('source_branch')) or seen.get('key')
    if not key:
        if mr['state'] != 'opened':
            return None
        summary = jira_summary(mr['title'], project)
        if dry_run:
            _log(f"would create {project} {cfg['issue_type']} for {mkey}: {summary}")
            return 'create'
        lines = summary_lines or describe_mr(mr)
        key = jira.create_issue(project, cfg['issue_type'], summary, lines, cfg['label'], state.get('account_id'))
        tracked[mkey] = seen = {'key': key, 'status': None}
        _log(f"created {key} for {mkey}")
        try:
            jira.remote_link(key, mr['web_url'], f"{mr['project_path']}!{mr['iid']}")
        except Exception as e:
            _log(f"remote link {key} failed: {e}")
    if mr['state'] == 'opened' and not find_key(project, mr['title']):
        new_title = retitle(mr['title'], key, project)
        if dry_run:
            _log(f"would retitle {mkey}: {new_title}")
        else:
            api.set_mr_title(mr['project_path'], mr['iid'], new_title)
            _log(f"retitled {mkey}: {new_title}")
    desired = desired_status(mr)
    if desired is None or seen == {'key': key, 'status': desired}:
        return None
    issue = jira.get_issue(key)
    if cfg['label'] not in issue['labels']:
        tracked[mkey] = {'key': key, 'status': desired}
        return None
    if can_move(issue, desired):
        if dry_run:
            _log(f"would move {key} {issue['status']} -> {desired} ({mkey})")
            return 'transition'
        jira.transition(key, desired)
        _log(f"moved {key} {issue['status']} -> {desired} ({mkey})")
    tracked[mkey] = {'key': key, 'status': desired}
    return desired


def tick(api, jira, cfg, state, dry_run=False, save=None):
    mrs = api.get_my_merge_requests(state='all', limit=100, days=int(cfg['lookback_days']), created_after=cfg['since'],
                                    get_all=True)
    mrs = [m for m in mrs if not excluded(m['project_path'], cfg['exclude_namespaces'])]
    _log(f"mine={len(mrs)} since={cfg['since']}")
    for mr in mrs:
        try:
            if mr['state'] == 'opened' and not mr.get('draft'):
                mr = api.get_merge_request_basic(mr['project_path'], mr['iid'])
            sync_one(mr, api, jira, cfg, state, dry_run=dry_run)
        except Exception as e:
            _log(f"FAILED {_key(mr)}: {e}")
        if save and not dry_run:
            save()
    return 0


def remote_project_path(url):
    m = re.match(r'^(?:\w+://)?(?:[^@/]+@)?([^:/]+)(?::\d+)?[:/](.+?)(?:\.git)?/?$', (url or '').strip())
    return (m.group(1), m.group(2)) if m else (None, None)


def git_project(host, cwd=None):
    def git(*args):
        return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, check=False).stdout.strip()
    branch = git('branch', '--show-current')
    for name in git('remote').split():
        remote_host, path = remote_project_path(git('remote', 'get-url', name))
        if remote_host == host:
            return path, branch
    return None, branch


def lookup_lines(cfg, project_path, branch, mr, key, issue):
    if excluded(project_path, cfg['exclude_namespaces']):
        return [f"{project_path} is in jira.exclude_namespaces: no ticket by design"]
    ref = f"{project_path}!{mr['iid']}" if mr else project_path
    if not key:
        if not mr:
            return [f"no MR for branch {branch} in {project_path}: the ticket is created when the MR opens"]
        if mr['state'] != 'opened':
            return [f"no ticket: {ref} is {mr['state']} and jirad never creates one for a closed MR"]
        if not cfg['enabled']:
            return [f"no ticket yet for {ref}: jirad is disabled (glmon-jirad --enable)"]
        if cfg.get('since') and mr.get('created_at') and _parse_ts(mr['created_at']) < _parse_ts(cfg['since']):
            return [f"no ticket for {ref}: it predates jira.since; glmon-jirad --mr {ref} creates one"]
        return [f"no ticket yet for {ref}: the next jirad tick creates it"]
    if not issue or 'error' in issue:
        return [f"{key}: jira lookup failed: {(issue or {}).get('error')}"]
    lines = [f"{key}  {issue['status']}  {issue.get('summary', '')[:70]}  {cfg['base'].rstrip('/')}/browse/{key}"]
    if not mr:
        return lines + [f"no MR for branch {branch} yet"]
    desired = desired_status(mr)
    if cfg['label'] not in issue['labels']:
        verdict = 'not created by jirad, so it is left alone'
    elif desired is None or issue['status'].lower() == desired.lower():
        verdict = 'in sync'
    elif can_move(issue, desired):
        verdict = 'drift: the next jirad tick moves it' if cfg['enabled'] else 'drift: jirad is disabled'
    else:
        verdict = f"held at {issue['status']} by hand, jirad leaves it"
    state = mr['state'] + (' draft' if mr.get('draft') else '')
    lines.append(f"{ref}  {state}  pipeline {mr.get('head_pipeline_status') or 'none'}  "
                 f"expects {desired or 'no change'}: {verdict}")
    return lines


def run_lookup(config, cfg, ref):
    api = GitLabAPI(config)
    if ref.startswith('http') or '!' in ref:
        try:
            mr = resolve_mr(api, ref)
        except ValueError:
            print(f"bad MR ref {ref!r}: use a URL or group/project!iid")
            return 1
        project_path, branch = mr['project_path'], mr['source_branch']
    else:
        host = urlparse(config.gitlab_url).hostname
        project_path, branch = git_project(host)
        if not project_path:
            print(f"not a checkout with a {host} remote: no ticket by design")
            return 1
        branch = ref or branch
        if not branch:
            print("detached HEAD: pass a branch, MR URL, or project!iid")
            return 1
        mr = None if excluded(project_path, cfg['exclude_namespaces']) else api.find_mr_by_branch(project_path, branch)
    key = None
    if not excluded(project_path, cfg['exclude_namespaces']):
        tracked = (load_state(STATE_FILE).get('mrs') or {}).get(_key(mr)) if mr else None
        key = find_key(cfg['project'], mr['title'] if mr else '', branch) or (tracked or {}).get('key')
    issue = None
    if key:
        try:
            issue = Jira(cfg['base'], cfg['email'], config.jira_token).get_issue(key)
        except Exception as e:
            issue = {'error': str(e)}
    print('\n'.join(lookup_lines(cfg, project_path, branch, mr, key, issue)))
    return 0


def acquire(root, wait=0, poll=2):
    deadline = time.monotonic() + wait
    while True:
        lock = Lock(root, 'jirad')
        if lock.__enter__():
            return lock
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


def token_source(config):
    # launchd sees only the file, so an env-only token reads as set here but missing under the job
    file_ok = (config.config_dir / 'jira_token').exists()
    env_ok = bool(os.environ.get('JIRA_TOKEN') or os.environ.get('JIRA_API_TOKEN'))
    return {(True, True): 'file+env', (True, False): 'file', (False, True): 'env-only(launchd-needs-file)'}.get(
        (file_ok, env_ok), 'missing')


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def main(argv=None):
    ap = argparse.ArgumentParser(prog='glmon-jirad', description='keep one Jira ticket per merge request you author (one tick per run)')
    ap.add_argument('--once', action='store_true', help='run one tick (default)')
    ap.add_argument('--dry-run', action='store_true', help='print what would change, write nothing; works while disabled')
    ap.add_argument('--mr', metavar='URL|project!iid', help='sync one MR now, even if it predates jira.since')
    ap.add_argument('--summary-file', metavar='PATH', help='with --mr: bullets for a new ticket description')
    ap.add_argument('--enable', action='store_true', help='turn sync on; only MRs created from now on get tickets')
    ap.add_argument('--disable', action='store_true', help='turn sync off; --mr is off too')
    ap.add_argument('--status', action='store_true')
    ap.add_argument('--lookup', nargs='?', const='', metavar='BRANCH|URL|project!iid',
                    help='read-only: ticket and status for a branch (default: current checkout) or MR; works while disabled')
    args = ap.parse_args(argv)

    config = Config()
    cfg = config.jira
    if args.enable or args.disable:
        fields: dict = {'enabled': bool(args.enable)}
        if args.enable and not cfg.get('since'):
            fields['since'] = _now_iso()
        config.set_jira(**fields)
        _log(f"jira sync {'enabled' if args.enable else 'disabled'} ({config.config_file})")
        return 0
    if args.status:
        print(f"enabled={cfg['enabled']} project={cfg['project']} since={cfg['since']} "
              f"exclude={cfg['exclude_namespaces']} token={token_source(config)} "
              f"tracked={len(load_state(STATE_FILE).get('mrs', {}))}")
        return 0
    if args.lookup is not None:
        missing = [k for k in ('base', 'email', 'project') if not cfg.get(k)] + ([] if config.jira_token else ['token'])
        ok, msg = config.validate()
        if missing or not ok:
            _log(f"jira config missing: {', '.join(missing)}" if missing else msg)
            return 2
        return run_lookup(config, cfg, args.lookup)
    if not cfg['enabled'] and not args.dry_run:
        _log("jira sync disabled (jira.enabled=false); nothing to do")
        return 0
    if not cfg.get('since'):
        cfg = {**cfg, 'since': _now_iso()}
        if not args.dry_run:
            config.set_jira(since=cfg['since'])
    missing = [k for k in ('base', 'email', 'project') if not cfg.get(k)] + ([] if config.jira_token else ['token'])
    if missing:
        _log(f"jira config missing: {', '.join(missing)}")
        return 2
    ok, msg = config.validate()
    if not ok:
        _log(msg)
        return 2

    api = GitLabAPI(config)
    jira = Jira(cfg['base'], cfg['email'], config.jira_token)
    # an explicit --mr (ship-it) waits out a running tick so its own summary bullets are used
    lock = acquire(STATE_DIR, wait=90 if args.mr else 0)
    if lock is None:
        _log("another jirad run holds the lock; the next tick picks this MR up")
        return 0
    try:
        state = load_state(STATE_FILE)
        if not state.get('account_id'):
            state['account_id'] = jira.myself()
        if args.mr:
            mr = resolve_mr(api, args.mr)
            if excluded(mr['project_path'], cfg['exclude_namespaces']):
                _log(f"skip {_key(mr)}: excluded namespace")
                return 0
            lines = summarize(Path(args.summary_file).read_text()) if args.summary_file else None
            result = sync_one(mr, api, jira, cfg, state, dry_run=args.dry_run, summary_lines=lines)
            key = (state.get('mrs', {}).get(_key(mr)) or {}).get('key')
            outcome = {None: 'no change', 'create': 'would create', 'transition': 'would move'}.get(result, f"at {result}")
            _log(f"{_key(mr)}: ticket {key or 'none'}, {outcome}")
        else:
            tick(api, jira, cfg, state, dry_run=args.dry_run, save=lambda: save_state(STATE_FILE, state))
        if not args.dry_run:
            save_state(STATE_FILE, state)
    finally:
        lock.__exit__(None, None, None)
    return 0


if __name__ == '__main__':
    sys.exit(main())
