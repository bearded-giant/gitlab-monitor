import tempfile
from pathlib import Path

import gitlab_monitor.jirad as jirad
from gitlab_monitor.jirad import (
    DONE, IN_PROGRESS, IN_REVIEW, WONT_FIX, can_move, desired_status, excluded, find_key,
    jira_summary, load_state, retitle, save_state, summarize, sync_one, tick,
)

# tests never shell out to claude
jirad.describe_mr = lambda mr: summarize(mr.get('description'))

CFG = {'project': 'PE', 'issue_type': 'Story', 'label': 'mr-sync', 'exclude_namespaces': ['bryan.grimes', 'ai-tooling'],
       'lookback_days': 14, 'since': '2026-01-01T00:00:00Z'}


def _mr(iid=1, title='feat: add thing', state='opened', draft=False, pipeline='success', branch='add-thing',
        path='engineering/app', description=''):
    return {'project_path': path, 'iid': iid, 'title': title, 'state': state, 'draft': draft,
            'head_pipeline_status': pipeline, 'source_branch': branch, 'description': description,
            'web_url': f'https://gl/{path}/-/merge_requests/{iid}'}


class FakeApi:
    def __init__(self, mrs=()):
        self.mrs = list(mrs)
        self.titles = []

    def set_mr_title(self, path, iid, title):
        self.titles.append((path, iid, title))

    def get_my_merge_requests(self, **kw):
        self.list_kwargs = kw
        return self.mrs

    def get_merge_request_basic(self, path, iid):
        return next(m for m in self.mrs if m['project_path'] == path and m['iid'] == iid)


class FakeJira:
    def __init__(self, issues=None):
        self.issues = issues or {}
        self.created, self.links, self.moves = [], [], []

    def create_issue(self, project, issue_type, summary, lines, label, account_id):
        key = f"{project}-{100 + len(self.created)}"
        self.created.append({'key': key, 'summary': summary, 'lines': lines, 'label': label, 'assignee': account_id})
        self.issues[key] = {'status': 'Backlog', 'category': 'new', 'labels': [label]}
        return key

    def remote_link(self, key, url, title):
        self.links.append((key, url, title))

    def get_issue(self, key):
        return dict(self.issues[key])

    def transition(self, key, status):
        self.moves.append((key, status))
        self.issues[key]['status'] = status


def test_find_key_ignores_placeholder_and_reads_branch():
    assert find_key('PE', '[PE-0000] feat: x') is None
    assert find_key('PE', '[PE-0000] feat: x', 'pe-42-fix') == 'PE-42'
    assert find_key('PE', 'Draft: [pe-7] y') == 'PE-7'
    assert find_key('PE', 'type-42 recipe', 'ope-12') is None


def test_summary_and_retitle_keep_draft_prefix_and_drop_placeholder():
    assert jira_summary('Draft: [PE-0000] feat: x', 'PE') == 'feat: x'
    assert retitle('[PE-0000] feat: x', 'PE-5', 'PE') == '[PE-5] feat: x'
    assert retitle('Draft: feat: x', 'PE-5', 'PE') == 'Draft: [PE-5] feat: x'
    assert retitle('feat: x', 'PE-5', 'PE') == '[PE-5] feat: x'


def test_scope_placeholder_is_replaced_in_place():
    assert jira_summary('chore(PE-0000): add greptile review config', 'PE') == 'chore: add greptile review config'
    assert jira_summary('PE-0000: fix it', 'PE') == 'fix it'
    assert retitle('chore(PE-0000): add config', 'PE-5', 'PE') == 'chore(PE-5): add config'
    assert retitle('Draft: feat(PE-0000): y', 'PE-5', 'PE') == 'Draft: feat(PE-5): y'


def test_desired_status_table():
    assert desired_status(_mr(state='merged')) == DONE
    assert desired_status(_mr(state='closed')) == WONT_FIX
    assert desired_status(_mr(state='locked')) is None
    assert desired_status(_mr(draft=True)) == IN_PROGRESS
    assert desired_status(_mr(pipeline='failed')) == IN_PROGRESS
    assert desired_status(_mr(pipeline='running')) == IN_PROGRESS
    assert desired_status(_mr(pipeline='success')) == IN_REVIEW
    assert desired_status(_mr(pipeline=None)) == IN_REVIEW


def test_summarize_prefers_description_section_and_drops_code():
    body = ("## Description\n- adds a cache for `foo()`\n```py\nx = 1\n```\n* second point\n3. third\n- fourth\n"
            "## How to QA\n- not this\n")
    assert summarize(body) == ['adds a cache for foo()', 'second point', 'third']
    assert summarize('plain line\n\n| table |\n# header\nsecond') == ['plain line', 'second']
    assert summarize('') == []
    assert summarize('- **Route:** a bold label') == ['Route: a bold label']


def test_summary_prompt_forbids_code_and_falls_back():
    calls = []

    def boom(argv, **kw):
        calls.append(argv)
        raise OSError('no claude')
    real, jirad.subprocess.run = jirad.subprocess.run, boom
    try:
        lines = jirad.claude_describe(_mr(description='## Description\nfirst why line\n- **Route:** /x\n'))
    finally:
        jirad.subprocess.run = real
    assert lines == ['first why line']
    prompt = calls[0][calls[0].index('-p') + 1]
    assert 'no code' in prompt and 'first why line' in prompt and '--model' in calls[0]


def test_excluded_matches_namespace_prefix_only():
    ns = CFG['exclude_namespaces']
    assert excluded('bryan.grimes/claude-code-config', ns)
    assert excluded('ai-tooling/investigator-agent', ns)
    assert not excluded('ai-tooling-extra/x', ns)
    assert not excluded('engineering/customcheckout', ns)


def test_can_move_respects_human_statuses():
    assert can_move({'status': 'Backlog', 'category': 'new'}, IN_PROGRESS)
    assert can_move({'status': "Won't fix", 'category': 'done'}, IN_PROGRESS)
    assert not can_move({'status': 'In QA', 'category': 'indeterminate'}, DONE)
    assert not can_move({'status': 'Done', 'category': 'done'}, IN_PROGRESS)
    assert not can_move({'status': 'In Review', 'category': 'indeterminate'}, IN_REVIEW)


def test_open_mr_without_key_creates_ticket_retitles_and_moves():
    api, jira, state = FakeApi(), FakeJira(), {'account_id': 'acc'}
    mr = _mr(title='[PE-0000] feat: add thing', description='## Description\n- why it matters\n')
    assert sync_one(mr, api, jira, CFG, state) == IN_REVIEW
    assert jira.created == [{'key': 'PE-100', 'summary': 'feat: add thing', 'lines': ['why it matters'],
                             'label': 'mr-sync', 'assignee': 'acc'}]
    assert jira.links == [('PE-100', mr['web_url'], 'engineering/app!1')]
    assert api.titles == [('engineering/app', 1, '[PE-100] feat: add thing')]
    assert jira.moves == [('PE-100', IN_REVIEW)]
    assert state['mrs']['engineering/app:1'] == {'key': 'PE-100', 'status': IN_REVIEW}


def test_summary_lines_override_description():
    api, jira = FakeApi(), FakeJira()
    sync_one(_mr(draft=True), api, jira, CFG, {}, summary_lines=['from ship-it'])
    assert jira.created[0]['lines'] == ['from ship-it']
    assert jira.moves == [('PE-100', IN_PROGRESS)]


def test_closed_mr_without_key_never_creates():
    api, jira = FakeApi(), FakeJira()
    assert sync_one(_mr(state='closed'), api, jira, CFG, {}) is None
    assert jira.created == [] and api.titles == []


def test_foreign_ticket_is_never_moved():
    jira = FakeJira({'PE-9': {'status': 'In QA', 'category': 'indeterminate', 'labels': []}})
    state = {}
    assert sync_one(_mr(title='[PE-9] fix', state='merged'), FakeApi(), jira, CFG, state) is None
    assert jira.moves == [] and state['mrs']['engineering/app:1'] == {'key': 'PE-9', 'status': DONE}


def test_unchanged_state_skips_jira_entirely():
    class NoCalls(FakeJira):
        def get_issue(self, key):
            raise AssertionError('jira must not be called when state is unchanged')
    state = {'mrs': {'engineering/app:1': {'key': 'PE-3', 'status': DONE}}}
    assert sync_one(_mr(title='[PE-3] x', state='merged'), FakeApi(), NoCalls(), CFG, state) is None


def test_key_from_state_retitles_instead_of_creating_twice():
    api, jira = FakeApi(), FakeJira({'PE-100': {'status': 'Backlog', 'category': 'new', 'labels': ['mr-sync']}})
    state = {'mrs': {'engineering/app:1': {'key': 'PE-100', 'status': None}}}
    sync_one(_mr(), api, jira, CFG, state)
    assert jira.created == []
    assert api.titles == [('engineering/app', 1, '[PE-100] feat: add thing')]


def test_merge_moves_owned_ticket_to_done_and_close_to_wont_fix():
    jira = FakeJira({'PE-1': {'status': 'In Review', 'category': 'indeterminate', 'labels': ['mr-sync']},
                     'PE-2': {'status': 'In Progress', 'category': 'indeterminate', 'labels': ['mr-sync']}})
    sync_one(_mr(iid=1, title='[PE-1] a', state='merged'), FakeApi(), jira, CFG, {})
    sync_one(_mr(iid=2, title='[PE-2] b', state='closed'), FakeApi(), jira, CFG, {})
    assert jira.moves == [('PE-1', DONE), ('PE-2', WONT_FIX)]


def test_dry_run_writes_nothing():
    api, jira, state = FakeApi(), FakeJira({'PE-1': {'status': 'Backlog', 'category': 'new', 'labels': ['mr-sync']}}), {}
    assert sync_one(_mr(), api, jira, CFG, state, dry_run=True) == 'create'
    assert sync_one(_mr(iid=2, title='[PE-1] x'), api, jira, CFG, state, dry_run=True) == 'transition'
    assert jira.created == [] and jira.moves == [] and api.titles == [] and 'engineering/app:2' not in state['mrs']


def test_tick_skips_excluded_namespaces_and_passes_since():
    mrs = [_mr(iid=1, path='ai-tooling/investigator-agent'), _mr(iid=2, path='bryan.grimes/x'), _mr(iid=3)]
    api, jira = FakeApi(mrs), FakeJira()
    tick(api, jira, CFG, {})
    assert [c['summary'] for c in jira.created] == ['feat: add thing']
    assert api.titles == [('engineering/app', 3, '[PE-100] feat: add thing')]
    assert api.list_kwargs['created_after'] == CFG['since'] and api.list_kwargs['state'] == 'all'


def test_remote_project_path_forms():
    rp = jirad.remote_project_path
    assert rp('git@gitlab.rechargeapps.net:identity/recharge-auth.git') == ('gitlab.rechargeapps.net', 'identity/recharge-auth')
    assert rp('https://gitlab.rechargeapps.net/engineering/frontend/frontend.git') == ('gitlab.rechargeapps.net', 'engineering/frontend/frontend')
    assert rp('ssh://git@gitlab.rechargeapps.net:2222/g/p.git') == ('gitlab.rechargeapps.net', 'g/p')
    assert rp('git@github.com:bearded-giant/gitlab-monitor.git') == ('github.com', 'bearded-giant/gitlab-monitor')
    assert rp('') == (None, None)


def test_git_project_picks_remote_on_gitlab_host():
    import subprocess
    repo = tempfile.mkdtemp()
    git = lambda *a: subprocess.run(['git', *a], cwd=repo, capture_output=True, check=True)  # noqa: E731
    git('init', '-q', '-b', 'feat/x')
    git('remote', 'add', 'origin', 'git@github.com:me/thing.git')
    git('remote', 'add', 'gitlab', 'git@gitlab.example.com:team/thing.git')
    assert jirad.git_project('gitlab.example.com', cwd=repo) == ('team/thing', 'feat/x')
    assert jirad.git_project('other.host', cwd=repo) == (None, 'feat/x')


LCFG = {**CFG, 'enabled': True, 'base': 'https://acme.atlassian.net/'}


def _issue(status, labels=('mr-sync',), category='indeterminate'):
    return {'status': status, 'category': category, 'labels': list(labels), 'summary': 'feat: add thing'}


def test_lookup_lines_ticket_states():
    ll = jirad.lookup_lines
    got = ll(LCFG, 'engineering/app', 'add-thing', _mr(), 'PE-5', _issue('In Review'))
    assert got[0] == 'PE-5  In Review  feat: add thing  https://acme.atlassian.net/browse/PE-5'
    assert got[1] == 'engineering/app!1  opened  pipeline success  expects In Review: in sync'
    assert ll(LCFG, 'engineering/app', 'b', _mr(pipeline='failed'), 'PE-5', _issue('In Review'))[1].endswith(
        'expects In Progress: drift: the next jirad tick moves it')
    assert ll(LCFG, 'engineering/app', 'b', _mr(state='merged'), 'PE-5', _issue('In QA'))[1].endswith(
        'held at In QA by hand, jirad leaves it')
    assert ll(LCFG, 'engineering/app', 'b', _mr(), 'PE-5', _issue('Backlog', labels=()))[1].endswith(
        'not created by jirad, so it is left alone')
    assert ll(LCFG, 'engineering/app', 'b', None, 'PE-5', _issue('Backlog'))[1] == 'no MR for branch b yet'
    assert ll(LCFG, 'engineering/app', 'b', _mr(), 'PE-5', {'error': '404'}) == ['PE-5: jira lookup failed: 404']


def test_lookup_lines_without_ticket():
    ll = jirad.lookup_lines
    assert ll(LCFG, 'ai-tooling/x', 'b', None, None, None) == ['ai-tooling/x is in jira.exclude_namespaces: no ticket by design']
    assert 'created when the MR opens' in ll(LCFG, 'engineering/app', 'b', None, None, None)[0]
    assert 'merged' in ll(LCFG, 'engineering/app', 'b', _mr(state='merged'), None, None)[0]
    assert 'disabled' in ll({**LCFG, 'enabled': False}, 'engineering/app', 'b', _mr(), None, None)[0]
    old = {**_mr(), 'created_at': '2025-12-31T00:00:00.000Z'}
    assert 'glmon-jirad --mr engineering/app!1' in ll(LCFG, 'engineering/app', 'b', old, None, None)[0]
    new = {**_mr(), 'created_at': '2026-02-01T00:00:00.000Z'}
    assert 'next jirad tick' in ll(LCFG, 'engineering/app', 'b', new, None, None)[0]


def test_acquire_waits_for_running_tick_then_gives_up():
    root = tempfile.mkdtemp()
    with jirad.Lock(root, 'jirad') as held:
        assert held is not None
        assert jirad.acquire(root, wait=0) is None
        assert jirad.acquire(root, wait=0.3, poll=0.1) is None
    lock = jirad.acquire(root, wait=0)
    assert lock is not None and lock.path.exists()
    lock.__exit__(None, None, None)
    assert not lock.path.exists()


def test_token_source_flags_env_only():
    import os
    class C:
        config_dir = Path(tempfile.mkdtemp())
    saved = {k: os.environ.pop(k, None) for k in ('JIRA_TOKEN', 'JIRA_API_TOKEN')}
    try:
        assert jirad.token_source(C) == 'missing'
        os.environ['JIRA_API_TOKEN'] = 'x'
        assert jirad.token_source(C) == 'env-only(launchd-needs-file)'
        (C.config_dir / 'jira_token').write_text('x')
        assert jirad.token_source(C) == 'file+env'
        del os.environ['JIRA_API_TOKEN']
        assert jirad.token_source(C) == 'file'
    finally:
        os.environ.pop('JIRA_API_TOKEN', None)
        os.environ.update({k: v for k, v in saved.items() if v is not None})


def test_state_round_trip_and_corrupt_file():
    path = Path(tempfile.mkdtemp()) / 'state.json'
    assert load_state(path) == {}
    save_state(path, {'mrs': {'a:1': {'key': 'PE-1', 'status': DONE}}})
    assert load_state(path)['mrs']['a:1']['key'] == 'PE-1'
    path.write_text('not json')
    assert load_state(path) == {}


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_'):
            fn()
            print('ok', name)
