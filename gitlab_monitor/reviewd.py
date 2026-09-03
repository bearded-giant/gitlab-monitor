# Copyright 2024 BeardedGiant
# https://github.com/bearded-giant/gitlab-tools
# Licensed under Apache License 2.0

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .api import GitLabAPI
from .config import Config

NO_WRITE_PROMPT = "Never post, comment, approve, or otherwise write to GitLab. Output the review only."
CLAUDE_TIMEOUT = 1800
REASON_TITLES = {
    'new': ('New review', 'Submarine'),
    'sha moved': ('UPDATED, re-reviewed', 'Ping'),
    'approved+moved': ('APPROVED MR CHANGED', 'Basso'),
}


def _log(msg):
    print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def _key(mr):
    return f"{mr['project_path']}:{mr['iid']}"


def _parse_ts(ts):
    return datetime.fromisoformat(str(ts).replace('Z', '+00:00'))


def build_queue(mrs, index, now, include_drafts=False, settle_minutes=10):
    queue = []
    for mr in mrs:
        if mr.get('draft') and not include_drafts:
            continue
        if now - _parse_ts(mr['updated_at']) < timedelta(minutes=settle_minutes):
            continue
        entry = index.get(_key(mr))
        if not entry:
            queue.append((mr, 'new'))
        elif entry.get('sha') and entry['sha'] != mr.get('sha'):
            queue.append((mr, 'approved+moved' if entry.get('approved_by_me') else 'sha moved'))
    return queue


def build_prompt(web_url, prev_sha=None):
    prompt = f"/kai:review-adversarial {web_url}"
    if prev_sha:
        prompt += f"\nPreviously reviewed at {prev_sha}. Call out what changed since."
    return prompt


def claude_argv(prompt, model):
    if '--post' in prompt:
        raise ValueError('refusing to run a review that would post to GitLab')
    exe = shutil.which('claude') or os.path.expanduser('~/.local/bin/claude')
    return [exe, '-p', prompt, '--model', model, '--output-format', 'json',
            '--append-system-prompt', NO_WRITE_PROMPT]


def parse_claude_output(stdout):
    arr = json.loads(stdout)
    if isinstance(arr, dict):
        arr = [arr]
    return next(m for m in arr if m.get('type') == 'result')


def run_review(mr, prev_sha, model, cwd):
    argv = claude_argv(build_prompt(mr['web_url'], prev_sha), model)
    started = datetime.now(timezone.utc)
    proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=CLAUDE_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr[-400:]}")
    res = parse_claude_output(proc.stdout)
    if res.get('is_error'):
        raise RuntimeError(f"claude result is_error: {str(res.get('result'))[:400]}")
    return {
        'result': res.get('result') or '',
        'cost_usd': round(float(res.get('total_cost_usd') or 0), 4),
        'session_id': res.get('session_id'),
        'num_turns': res.get('num_turns'),
        'reviewed_at': started.isoformat(timespec='seconds'),
    }


def notify(reason, mr):
    title, sound = REASON_TITLES.get(reason, REASON_TITLES['new'])
    title = f"{title}: {mr['project_path'].rsplit('/', 1)[-1]}!{mr['iid']}"
    message = mr['title'][:120]
    tn = shutil.which('terminal-notifier')
    try:
        if tn:
            subprocess.run([tn, '-title', title, '-message', message, '-sound', sound,
                            '-open', mr['web_url'], '-group', f"glmon-{_key(mr)}"],
                           capture_output=True, timeout=4)
            return
        safe_t = title.replace('"', "'")
        safe_m = message.replace('"', "'")
        subprocess.run(['osascript', '-e',
                        f'display notification "{safe_m}" with title "{safe_t}" sound name "{sound}"'],
                       capture_output=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        pass


class Lock:
    def __init__(self, root, key):
        self.path = Path(root) / '_locks' / (key.replace('/', '-').replace(':', '-') + '.lock')

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                os.kill(int(self.path.read_text().strip() or 0), 0)
                return None
            except (OSError, ValueError):
                pass
        self.path.write_text(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


def review_one(config, api, mr, reason):
    files = config.review_files
    prev = files.get(mr['project_path'], mr['iid']) or {}
    with Lock(files.root, _key(mr)) as lock:
        if lock is None:
            _log(f"skip {_key(mr)}: review already running")
            return None
        approvals = api.get_mr_approvals_summary(mr['project_path'], mr['iid']) or {}
        if reason == 'new' and approvals.get('user_has_approved'):
            # ponytail: already approved before any review existed; record the sha so a later push still fires approved+moved, spend nothing
            path = files.write(mr['project_path'], mr['iid'], {
                'mr': mr['web_url'], 'sha': mr.get('sha'), 'previous_shas': [], 'title': mr['title'],
                'author': mr.get('author'), 'reviewed_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                'reason': 'approved-baseline', 'model': None, 'cost_usd': 0, 'session_id': None, 'num_turns': 0,
                'approved_by_me': True, 'status': 'read',
            }, f"# {mr['title']}\n\n{mr['web_url']}\n\nApproved by you before auto-review existed. No review was run; this file only pins the sha so a later push is caught.\n")
            _log(f"baseline {_key(mr)} (already approved) sha={str(mr.get('sha'))[:8]}")
            return path
        _log(f"review {_key(mr)} ({reason}) sha={str(mr.get('sha'))[:8]}")
        res = run_review(mr, prev.get('sha') if reason != 'new' else None, config.reviews['model'], files.root)
        previous = list(prev.get('previous_shas') or [])
        if prev.get('sha') and prev['sha'] != mr.get('sha'):
            previous.append(prev['sha'])
        meta = {
            'mr': mr['web_url'],
            'sha': mr.get('sha'),
            'previous_shas': previous,
            'title': mr['title'],
            'author': mr.get('author'),
            'reviewed_at': res['reviewed_at'],
            'reason': reason,
            'model': config.reviews['model'],
            'cost_usd': res['cost_usd'],
            'session_id': res['session_id'],
            'num_turns': res['num_turns'],
            'approved_by_me': bool(approvals.get('user_has_approved')),
            'status': 'unread',
        }
        body = f"# {mr['title']}\n\n{mr['web_url']}\n\n{res['result']}\n"
        path = files.write(mr['project_path'], mr['iid'], meta, body)
        _log(f"wrote {path} cost=${res['cost_usd']} turns={res['num_turns']}")
        notify(reason, mr)
        return path


def refresh_approvals(config, api, mrs):
    files = config.review_files
    index = files.index()
    for mr in mrs:
        entry = index.get(_key(mr))
        if not entry or entry.get('sha') != mr.get('sha'):
            continue
        summ = api.get_mr_approvals_summary(mr['project_path'], mr['iid']) or {}
        approved = bool(summ.get('user_has_approved'))
        if approved != bool(entry.get('approved_by_me')):
            files.update_meta(mr['project_path'], mr['iid'], approved_by_me=approved)
            _log(f"approved_by_me {_key(mr)} -> {approved}")


def tick(config, api, dry_run=False, limit=None):
    reviews = config.reviews
    mrs = api.get_review_requests()
    queue = build_queue(mrs, config.review_files.index(), datetime.now(timezone.utc),
                        include_drafts=bool(reviews.get('include_drafts')),
                        settle_minutes=int(reviews.get('settle_minutes') or 0))
    _log(f"assigned={len(mrs)} queued={len(queue)}")
    for mr, reason in queue:
        print(f"  {reason:15} {_key(mr)} {str(mr.get('sha'))[:8]} {mr['title'][:70]}")
    if dry_run:
        return 0
    refresh_approvals(config, api, mrs)
    done = 0
    for mr, reason in queue:
        if limit is not None and done >= limit:
            break
        try:
            if review_one(config, api, mr, reason):
                done += 1
        except Exception as e:
            _log(f"FAILED {_key(mr)}: {e}")
    return 0


def resolve_mr(api, ref):
    if ref.startswith('http'):
        path, iid = ref.split('/-/merge_requests/')
        project_path = path.split('://', 1)[1].split('/', 1)[1]
        iid = int(iid.strip('/').split('/')[0].split('#')[0].split('?')[0])
    else:
        project_path, iid = ref.rsplit('!', 1)
        iid = int(iid)
    detail = api.get_merge_request(project_path, iid)
    if not detail:
        raise SystemExit(f"MR not found: {ref}")
    detail.setdefault('project_path', project_path)
    return detail


def main(argv=None):
    ap = argparse.ArgumentParser(prog='glmon-reviewd', description='glmon assigned-review poller (one tick per run)')
    ap.add_argument('--once', action='store_true', help='run one tick (default)')
    ap.add_argument('--dry-run', action='store_true', help='print the queue and exit')
    ap.add_argument('--limit', type=int, default=None, help='max reviews this tick')
    ap.add_argument('--mr', metavar='URL|project!iid', help='review one MR now, ignoring enabled/draft/settle/sha checks')
    ap.add_argument('--enable', action='store_true', help='turn auto-review on (reviews.enabled)')
    ap.add_argument('--disable', action='store_true', help='turn auto-review off; --mr still works')
    ap.add_argument('--status', action='store_true')
    args = ap.parse_args(argv)

    config = Config()
    if args.enable or args.disable:
        config.set_reviews_enabled(bool(args.enable))
        _log(f"auto-review {'enabled' if args.enable else 'disabled'} ({config.config_file})")
        return 0
    if args.status:
        n = len(config.review_files.index())
        print(f"enabled={config.reviews_enabled} dir={config.reviews_dir} files={n} model={config.reviews['model']} "
              f"settle_minutes={config.reviews['settle_minutes']} include_drafts={config.reviews['include_drafts']}")
        return 0

    ok, msg = config.validate()
    if not ok:
        _log(msg)
        return 2
    api = GitLabAPI(config)
    config.reviews_dir.mkdir(parents=True, exist_ok=True)

    if args.mr:
        mr = resolve_mr(api, args.mr)
        prev = config.review_files.get(mr['project_path'], mr['iid'])
        reason = 'new' if not prev else ('approved+moved' if prev.get('approved_by_me') and prev.get('sha') != mr.get('sha') else 'sha moved')
        return 0 if review_one(config, api, mr, reason) else 1

    if not config.reviews_enabled:
        _log("auto-review disabled (reviews.enabled=false); nothing to do")
        return 0
    return tick(config, api, dry_run=args.dry_run, limit=args.limit)


if __name__ == '__main__':
    sys.exit(main())
