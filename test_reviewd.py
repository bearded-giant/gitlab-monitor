import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gitlab_monitor.config import ReviewFiles
from gitlab_monitor.reviewd import build_prompt, build_queue, claude_argv, parse_claude_output


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
    assert p.splitlines() == ['/kai:review-adversarial https://gl/g/p/-/merge_requests/9',
                              'Previously reviewed at oldsha. Call out what changed since.']
    argv = claude_argv(p, 'opus')
    assert '--post' not in ' '.join(argv) and '--append-system-prompt' in argv and '--output-format' in argv
    try:
        claude_argv('/kai:review-adversarial https://x --post', 'opus')
    except ValueError:
        pass
    else:
        raise AssertionError('--post must be refused')


def test_parse_claude_output_array_and_object():
    arr = [{'type': 'system'}, {'type': 'assistant'}, {'type': 'result', 'result': 'ok', 'total_cost_usd': 1.2}]
    assert parse_claude_output(json.dumps(arr))['result'] == 'ok'
    assert parse_claude_output(json.dumps(arr[-1]))['total_cost_usd'] == 1.2


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_'):
            fn()
            print('ok', name)
