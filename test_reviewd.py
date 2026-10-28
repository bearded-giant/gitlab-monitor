import json
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gitlab_monitor import reviewd
from gitlab_monitor.config import ReviewFiles
from gitlab_monitor.reviewd import Lock, build_prompt, build_queue, claude_argv, parse_claude_output


def _mr(iid, sha, draft=False, minutes_ago=60):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return {'project_path': 'g/p', 'iid': iid, 'sha': sha, 'draft': draft, 'updated_at': ts,
            'title': f'mr {iid}', 'web_url': f'https://gl/g/p/-/merge_requests/{iid}', 'author': 'a'}


def test_review_files_round_trip():
    rf = ReviewFiles(Path(tempfile.mkdtemp()))
    meta = {'mr': 'https://gl/x', 'sha': 'abc', 'previous_shas': [], 'title': 'T: with colon', 'cost_usd': 1.25,
            'approved_by_me': False, 'status': 'unread'}
    path = rf.write('g/p', 7, meta, '# body\n\nkey: value looks like yaml but is body\n')
    got = rf.get('g/p', 7)
    assert got['sha'] == 'abc' and got['cost_usd'] == 1.25 and got['previous_shas'] == [] and got['approved_by_me'] is False
    assert rf.set_status('g/p', 7, 'read') and rf.get('g/p', 7)['status'] == 'read'
    assert rf.update_meta('g/p', 7, approved_by_me=True) and rf.get('g/p', 7)['approved_by_me'] is True
    same = rf.write('g/p', 7, {**meta, 'sha': 'def', 'previous_shas': ['abc'], 'title': 'renamed'}, 'v2')
    assert same == path
    assert rf.get('g/p', 7)['previous_shas'] == ['abc']
    _m, body = ReviewFiles.parse(path)
    assert body == 'v2\n'


def test_review_files_ignores_probe_and_junk():
    root = Path(tempfile.mkdtemp())
    (root / '_probe').mkdir()
    (root / '_probe' / 'x.md').write_text('---\nproject: "g/p"\niid: 1\n---\nx')
    (root / 'nofm.md').write_text('no frontmatter')
    assert ReviewFiles(root).index() == {}


def test_build_queue_reasons_and_skips():
    now = datetime.now(timezone.utc)
    mrs = [_mr(1, 'a'), _mr(2, 'b2'), _mr(3, 'c2'), _mr(4, 'd', draft=True), _mr(5, 'e', minutes_ago=2), _mr(6, 'f')]
    index = {'g/p:2': {'sha': 'b1', 'approved_by_me': False}, 'g/p:3': {'sha': 'c1', 'approved_by_me': True}, 'g/p:6': {'sha': 'f'}}
    got = [(m['iid'], r) for m, r in build_queue(mrs, index, now, include_drafts=False, settle_minutes=10)]
    assert got == [(1, 'new'), (2, 'sha moved'), (3, 'approved+moved')], got
    with_drafts = [m['iid'] for m, _ in build_queue(mrs, index, now, include_drafts=True, settle_minutes=0)]
    assert with_drafts == [1, 2, 3, 4, 5], with_drafts


def test_prompt_and_argv_never_post():
    p = build_prompt('https://gl/g/p/-/merge_requests/9', 'oldsha')
    # grill runs as a forked skill: --append-system-prompt stops at the parent, its arguments reach the fork
    assert p.splitlines() == ['/grill https://gl/g/p/-/merge_requests/9',
                              'Previously reviewed at oldsha. Call out what changed since.',
                              reviewd.NO_WRITE_PROMPT]
    assert build_prompt('https://x').splitlines()[-1] == reviewd.NO_WRITE_PROMPT
    argv = claude_argv(p, 'opus')
    assert '--post' not in ' '.join(argv) and '--append-system-prompt' in argv and '--output-format' in argv
    try:
        claude_argv('/grill https://x --post', 'opus')
    except ValueError:
        pass
    else:
        raise AssertionError('--post must be refused')


def test_lock_blocked_caller_leaves_holder_lock():
    root = tempfile.mkdtemp()
    with Lock(root, 'g/p:1') as held:
        assert held is not None
        with Lock(root, 'g/p:1') as blocked:
            assert blocked is None
        # the blocked caller must not delete the holder's lockfile on exit
        assert held.path.exists()
    assert not held.path.exists()


def test_parse_claude_output_array_and_object():
    arr = [{'type': 'system'}, {'type': 'assistant'}, {'type': 'result', 'result': 'ok', 'total_cost_usd': 1.2}]
    assert parse_claude_output(json.dumps(arr))['result'] == 'ok'
    assert parse_claude_output(json.dumps(arr[-1]))['total_cost_usd'] == 1.2


def test_run_review_records_models_used():
    # a skill can pin its own model (grill forks to sonnet), so --model is not what ran
    out = json.dumps([{'type': 'result', 'result': 'r', 'total_cost_usd': 0.5, 'session_id': 's', 'num_turns': 1,
                       'modelUsage': {'claude-sonnet-5-5': {'costUSD': 0.5}}}])
    real = reviewd.subprocess.run
    reviewd.subprocess.run = lambda argv, **_: subprocess.CompletedProcess(argv, 0, stdout=out, stderr='')
    try:
        res = reviewd.run_review(_mr(1, 'abc'), None, 'opus', '.')
    finally:
        reviewd.subprocess.run = real
    assert res['models'] == ['claude-sonnet-5-5']


def test_refresh_approvals_one_request_per_tick():
    # launchd ticks every 5 min, 24/7: a per-MR approvals lookup ran 15 req/tick against shared GitLab
    rf = ReviewFiles(Path(tempfile.mkdtemp()))
    for iid, approved in ((1, False), (2, True), (3, False)):
        rf.write('g/p', iid, {'sha': f's{iid}', 'approved_by_me': approved, 'status': 'read'}, 'b')
    rf.write('g/p', 4, {'sha': 'old', 'approved_by_me': False, 'status': 'read'}, 'b')
    mrs = [_mr(1, 's1'), _mr(2, 's2'), _mr(3, 's3'), _mr(4, 'new'), _mr(5, 's5')]
    calls = []

    class Api:
        def get_review_requests(self, **kw):
            calls.append(kw)
            return [_mr(1, 's1'), _mr(4, 'new')]

        def get_mr_approvals_summary(self, *a):
            raise AssertionError('per-MR approvals lookup on a tick')

    config = type('C', (), {'review_files': rf})()
    reviewd.refresh_approvals(config, Api(), mrs)
    assert calls == [{'approved_by_me': True}], calls
    got = {i: rf.get('g/p', i)['approved_by_me'] for i in (1, 2, 3, 4)}
    assert got == {1: True, 2: False, 3: False, 4: False}, got

    calls.clear()
    reviewd.refresh_approvals(config, Api(), [_mr(5, 's5')])
    assert calls == []


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_'):
            fn()
            print('ok', name)


def test_parse_mr_ref_handles_both_hosts():
    from gitlab_monitor.reviewd import parse_mr_ref
    assert parse_mr_ref('https://gitlab.example.com/g/sub/p/-/merge_requests/12/diffs#note_1') == ('g/sub/p', 12)
    assert parse_mr_ref('https://github.com/o/r/pull/7/files') == ('gh:o/r', 7)
    assert parse_mr_ref('g/p!3') == ('g/p', 3)
    assert parse_mr_ref('gh:o/r#4') == ('gh:o/r', 4)
