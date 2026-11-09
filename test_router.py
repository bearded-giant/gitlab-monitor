import pytest

from gitlab_monitor.router import MultiAPI


class _P:
    def __init__(self, name, url):
        self.name = name
        self.url = url
        self.project = None
        self.project_name = None
        self.calls = []

    def set_project(self, key):
        self.project = {'key': key}
        self.project_name = key

    def host_label(self, key=None):
        return (self.name, self.url)

    def project_url(self, key):
        return f"{self.url}/{key.replace('gh:', '')}"

    def supports(self, action, key=None):
        return self.name != 'GitHub' or action != 'revert_tag_commit'

    def get_my_merge_requests(self, state='opened', limit=50):
        self.calls.append(('mrs', state, limit))
        return [{'iid': 1, 'updated_at': '2' if self.name == 'GitLab' else '3', 'project_path': self.url}]

    def get_projects(self, search=None, per_page=50):
        if self.name == 'GitHub':
            raise RuntimeError('boom')
        return [{'path': 'g/p', 'last_activity': '1'}]

    def get_projects_by_paths(self, paths):
        self.calls.append(('paths', tuple(paths)))
        return [{'path': p} for p in paths]

    def get_pipeline_jobs(self, pid):
        return [self.name]

    def get_merge_request(self, key, iid):
        return {'from': self.name, 'key': key, 'iid': iid}

    def get_my_activity_counts(self, days=7, force=False):
        return [{'date': 'd', 'day_offset': 0, 'count': 2}]

    def get_mr_dashboard_lists(self, full, counts=()):
        return {'reviews_requested': {'count': 1, 'mrs': [{'iid': 9, 'updated_at': self.url}]}}


class _Config:
    gitlab_enabled = True
    github_enabled = True
    project_path = None


def _router():
    return MultiAPI(_Config(), providers={'gitlab': _P('GitLab', 'https://gl'), 'github': _P('GitHub', 'https://gh')})


def test_dispatch_by_key_and_implicit_project_memory():
    r = _router()
    assert r.get_merge_request('gh:o/r', 2)['from'] == 'GitHub'
    assert r.get_merge_request('g/p', 2)['from'] == 'GitLab'
    r.set_project('gh:o/r')
    assert r.project_name == 'gh:o/r' and r.get_pipeline_jobs(1) == ['GitHub']
    r.set_project('g/p')
    assert r.get_pipeline_jobs(1) == ['GitLab']
    assert r.supports('revert_tag_commit', 'g/p') and not r.supports('revert_tag_commit', 'gh:o/r')


def test_fanout_merges_and_isolates_failures():
    r = _router()
    rows = r.get_my_merge_requests('opened', 10)
    assert [m['project_path'] for m in rows] == ['https://gh', 'https://gl']
    assert r.get_projects() == [{'path': 'g/p', 'last_activity': '1'}]
    assert r.get_my_activity_counts()[0]['count'] == 4
    dash = r.get_mr_dashboard_lists([('reviews_requested',)])
    assert dash['reviews_requested']['count'] == 2 and len(dash['reviews_requested']['mrs']) == 2


def test_fanout_reports_failed_provider():
    r = _router()
    seen = []
    r.on_error = lambda name, err: seen.append((name, str(err)))
    assert r.get_projects() == [{'path': 'g/p', 'last_activity': '1'}]
    assert seen == [('github', 'boom')]
    r.config = type('NoHosts', (), {'gitlab_enabled': False, 'github_enabled': False})()
    assert r.fresh().on_error is r.on_error


def test_paths_isolate_and_report_failed_provider():
    r = _router()
    seen = []
    r.on_error = lambda name, err: seen.append(name)

    def dead(paths):
        raise RuntimeError('401')
    r.providers['gitlab'].get_projects_by_paths = dead
    assert [m['path'] for m in r.get_projects_by_paths(['gh:o/r', 'g/p'])] == ['gh:o/r']
    assert seen == ['gitlab']
    single = MultiAPI(_Config(), providers={'gitlab': r.providers['gitlab']})
    with pytest.raises(RuntimeError, match='401'):
        single.get_projects_by_paths(['g/p'])


def test_paths_split_per_provider_keep_order():
    r = _router()
    out = r.get_projects_by_paths(['gh:o/r', 'g/p', 'gh:o/s'])
    assert [m['path'] for m in out] == ['gh:o/r', 'g/p', 'gh:o/s']
    assert r.providers['github'].calls == [('paths', ('gh:o/r', 'gh:o/s'))]


def test_host_label_and_missing_provider():
    r = _router()
    assert r.host_label() == ('Hosts', 'gl + gh')
    assert r.host_label('gh:o/r') == ('GitHub', 'https://gh')
    r.set_project('g/p')
    assert r.host_label() == ('Hosts', 'gl + gh') and r.host_label('g/p') == ('GitLab', 'https://gl')
    assert r.project_url('gh:o/r') == 'https://gh/o/r'
    single = MultiAPI(_Config(), providers={'gitlab': _P('GitLab', 'https://gl')})
    assert single.host_label() == ('GitLab', 'https://gl')
    with pytest.raises(RuntimeError, match='github is not configured'):
        single.get_merge_request('gh:o/r', 1)


def test_check_hosts_reports_each_failing_host():
    import gitlab
    import requests

    from gitlab_monitor.router import host_problem

    r = _router()
    seen = []
    r.on_error = lambda name, err: seen.append((name, host_problem(err)))

    def dead():
        raise gitlab.exceptions.GitlabAuthenticationError('invalid_token', 401)

    def offline():
        raise requests.ConnectTimeout('timed out')
    r.providers['gitlab'].ping = dead
    r.providers['github'].ping = offline
    r.check_hosts()
    assert seen == [('gitlab', 'token rejected (expired or revoked)'), ('github', 'unreachable (network, VPN or DNS)')]

    seen.clear()
    single = MultiAPI(_Config(), providers={'github': r.providers['github']})
    single.on_error = r.on_error
    single.check_hosts()
    assert seen == [('github', 'unreachable (network, VPN or DNS)')]

    resp = type('R', (), {'status_code': 401})()
    assert host_problem(requests.HTTPError(response=resp)) == 'token rejected (expired or revoked)'
    assert host_problem(RuntimeError('boom')) == 'RuntimeError: boom'
