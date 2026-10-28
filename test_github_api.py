import json
from types import SimpleNamespace

import pytest

from gitlab_monitor.api import GitLabAPI
from gitlab_monitor.github_api import (
    GitHubAPI, Unsupported, _status, github_key, is_github_key, repo_of, GH_DASHBOARD_QUERIES,
)


class _Resp:
    def __init__(self, data=None, status=200, content=None, headers=None):
        self.status_code = status
        self._data = data
        self.content = content if content is not None else (json.dumps(data).encode() if data is not None else b'')
        self.headers = headers or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(response=self)


class _Session:
    def __init__(self, routes=None, graphql=None):
        self.headers = {}
        self.routes = routes or {}
        self.graphql = graphql or (lambda q, v: {})
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        path = url.replace('https://api.github.com', '')
        if (method, path) in self.routes:
            hit = self.routes[(method, path)]
        elif path in self.routes:
            hit = self.routes[path]
        else:
            return _Resp({}, 404)
        if callable(hit):
            hit = hit(kw)
        return _Resp(hit) if not isinstance(hit, _Resp) else hit

    def post(self, url, json=None, **kw):
        self.calls.append(('POST', url, {'json': json}))
        return _Resp({'data': self.graphql(json['query'], json.get('variables') or {})})

    def get(self, url, **kw):
        self.calls.append(('GET', url, kw))
        hit = self.routes.get(('LOG', kw.get('headers', {}).get('Range', '')))
        return hit if hit is not None else _Resp({}, 404)


class _Store:
    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k)

    def set(self, k, v):
        self.d[k] = v


class _Config:
    gitlab_url = 'https://gitlab.example.com'
    gitlab_token = 'x'
    github_token = 't'
    github_url = None
    github_api_url = None
    project_path = None
    project_users = _Store()


def _api(routes=None, graphql=None):
    return GitHubAPI(_Config(), session=_Session(routes, graphql))


def test_status_map():
    cases = [('queued', None, 'pending'), ('in_progress', None, 'running'), ('completed', 'success', 'success'),
             ('completed', 'failure', 'failed'), ('completed', 'cancelled', 'canceled'),
             ('completed', 'skipped', 'skipped'), ('completed', 'timed_out', 'failed'),
             ('completed', 'action_required', 'manual')]
    for status, conclusion, want in cases:
        assert _status(status, conclusion) == want, (status, conclusion)


def test_key_helpers():
    assert github_key('Owner/Repo') == 'gh:Owner/Repo'
    assert is_github_key('gh:a/b') and not is_github_key('a/b') and not is_github_key(None)
    assert repo_of('gh:a/b') == 'a/b' and repo_of('g/p') == 'g/p'


_RUN = {'id': 1, 'status': 'completed', 'conclusion': 'success', 'head_branch': 'main', 'head_sha': 'abcdef1234567890',
        'created_at': '2026-10-28T18:15:24Z', 'updated_at': '2026-10-28T18:17:45Z', 'actor': {'login': 'me'},
        'html_url': 'https://github.com/o/r/actions/runs/1', 'name': 'ci', 'event': 'pull_request',
        'run_started_at': '2026-10-28T18:15:24Z'}
_JOB = {'id': 9, 'name': 'test', 'status': 'in_progress', 'conclusion': None, 'started_at': '2026-10-28T18:15:27Z',
        'completed_at': None, 'html_url': 'https://github.com/o/r/actions/runs/1/job/9',
        'steps': [{'number': 1, 'name': 'Set up job', 'status': 'completed', 'conclusion': 'success'},
                  {'number': 2, 'name': 'Run tests', 'status': 'in_progress', 'conclusion': None}]}


def test_run_and_job_dicts_match_gitlab_keys():
    api = _api({'/repos/o/r/actions/runs': {'workflow_runs': [_RUN]}, '/repos/o/r/actions/runs/1/jobs': {'jobs': [_JOB]},
                '/repos/o/r': {'id': 5, 'full_name': 'o/r', 'name': 'r', 'default_branch': 'main'}})
    api.set_project('gh:o/r')
    gl_keys = set(GitLabAPI._pipeline_to_dict(SimpleNamespace(id=1, status='success', ref='x', sha='abc', created_at='',
                                                               updated_at='', user=None, web_url='')))
    runs = api.get_recent_pipelines(limit=5)
    assert runs[0]['status'] == 'success' and runs[0]['sha'] == 'abcdef12' and runs[0]['project_path'] == 'gh:o/r'
    assert gl_keys <= set(runs[0])
    jobs = api.get_pipeline_jobs(1)
    assert {'id', 'name', 'status', 'stage', 'duration', 'started_at', 'finished_at', 'web_url'} <= set(jobs[0])
    assert jobs[0]['stage'] == '' and jobs[0]['status'] == 'running'
    assert [s['status'] for s in jobs[0]['steps']] == ['success', 'running']


_PR = {'id': 77, 'number': 2, 'title': 'ci: gate', 'body': 'b', 'state': 'open', 'draft': False,
       'mergeable_state': 'dirty', 'head': {'ref': 'ci-gate', 'sha': 'f294c2690000', 'repo': {'full_name': 'o/r'}},
       'base': {'ref': 'main', 'repo': {'full_name': 'o/r', 'id': 5}}, 'created_at': 'c', 'updated_at': 'u',
       'merged_at': None, 'closed_at': None, 'user': {'login': 'me'}, 'requested_reviewers': [{'login': 'rev'}],
       'assignees': [], 'html_url': 'https://github.com/o/r/pull/2', 'comments': 1, 'review_comments': 2,
       'auto_merge': None, 'merge_commit_sha': None}


def _gl_mr_keys():
    mr = SimpleNamespace(id=1, iid=1, project_id=1, title='t', state='opened', source_branch='a', target_branch='b',
                         created_at='', updated_at='', web_url='', references={'full': 'g/p!1'})
    return set(GitLabAPI._mr_to_dict(mr))


def test_rest_pr_dict_matches_gitlab_keys():
    api = _api()
    d = api._pr_to_dict(_PR)
    assert set(d) == _gl_mr_keys()
    assert d['iid'] == 2 and d['project_path'] == 'gh:o/r' and d['has_conflicts'] and d['state'] == 'opened'
    assert d['reviewers'] == ['rev'] and d['user_notes_count'] == 3


_NODE = {
    'id': 'PR_x', 'number': 2, 'title': 'ci: gate', 'body': '', 'isDraft': False, 'state': 'OPEN',
    'url': 'https://github.com/o/r/pull/2', 'createdAt': 'c', 'updatedAt': 'u', 'mergedAt': None, 'closedAt': None,
    'repository': {'nameWithOwner': 'o/r', 'databaseId': 5}, 'author': {'login': 'me'},
    'headRefName': 'ci-gate', 'baseRefName': 'main', 'headRefOid': 'f294c269', 'mergeable': 'MERGEABLE',
    'reviewDecision': 'REVIEW_REQUIRED', 'additions': 18, 'deletions': 7, 'changedFiles': 3,
    'autoMergeRequest': None, 'mergeCommit': None, 'assignees': {'nodes': []},
    'reviewRequests': {'nodes': [{'requestedReviewer': {'login': 'rev'}}]},
    'latestReviews': {'nodes': [{'author': {'login': 'greptile-apps'}, 'state': 'COMMENTED'},
                                {'author': {'login': 'me'}, 'state': 'APPROVED'}]},
    'reviewThreads': {'totalCount': 2, 'nodes': [{'isResolved': False}, {'isResolved': True}]},
    'comments': {'totalCount': 4},
    'commits': {'totalCount': 3, 'nodes': [{'commit': {'statusCheckRollup': {'state': 'PENDING', 'contexts': {'nodes': [
        {'status': 'IN_PROGRESS', 'conclusion': None, 'checkSuite': {'workflowRun': {'databaseId': 40743720377,
                                                                                     'url': 'https://github.com/o/r/actions/runs/40743720377'}}}]}}}}]},
}


def test_gql_pr_dict_superset_of_both_gitlab_shapes():
    api = _api()
    api._username_cache = 'me'
    d = api._gql_pr_to_dict(_NODE)
    gl = GitLabAPI(_Config())
    gql_keys = set(gl._gql_mr_to_dict({'iid': '1', 'project': {}, 'reviewers': {'nodes': []}, 'approvedBy': {'nodes': []},
                                       'mergeabilityChecks': [], 'headPipeline': None}))
    assert _gl_mr_keys() <= set(d) and gql_keys <= set(d)
    assert d['head_pipeline_status'] == 'running' and d['head_pipeline_id'] == 40743720377
    assert d['threads_resolvable'] == 2 and d['threads_resolved'] == 1 and not d['blocking_discussions_resolved']
    assert d['approvals_count'] == 1 and d['approvals_required'] == 1 and d['user_has_approved']
    assert d['reviewers'] == ['rev', 'greptile-apps', 'me'] and d['commits_count'] == 3


def test_search_paginates_when_get_all():
    pages = iter([
        {'search': {'issueCount': 3, 'pageInfo': {'hasNextPage': True, 'endCursor': 'c1'}, 'nodes': [_NODE, _NODE]}},
        {'search': {'issueCount': 3, 'pageInfo': {'hasNextPage': False, 'endCursor': None}, 'nodes': [_NODE]}},
    ])
    api = _api(graphql=lambda q, v: next(pages))
    api._username_cache = 'me'
    assert len(api.get_my_merge_requests(state='all', limit=100, days=7, get_all=True)) == 3
    first_query = api.session.calls[0][2]['json']['variables']['q']
    assert 'author:@me' in first_query and 'updated:>=' in first_query and 'is:open' not in first_query


def test_discussions_merge_threads_reviews_comments():
    pr = {'reviewThreads': {'nodes': [
        {'id': 'T1', 'isResolved': False, 'path': 'a.py', 'line': 3,
         'comments': {'nodes': [{'id': 'C1', 'author': {'login': 'greptile-apps'}, 'body': 'nit', 'createdAt': '2', 'updatedAt': '2'}]}},
        {'id': 'T2', 'isResolved': True, 'path': 'b.py', 'line': 1,
         'comments': {'nodes': [{'id': 'C2', 'author': {'login': 'rev'}, 'body': 'ok', 'createdAt': '3', 'updatedAt': '3'}]}},
    ]}, 'reviews': {'nodes': [
        {'id': 'R1', 'author': {'login': 'greptile-apps'}, 'body': 'summary', 'state': 'COMMENTED', 'createdAt': '1', 'updatedAt': '1'},
        {'id': 'R2', 'author': {'login': 'rev'}, 'body': '', 'state': 'APPROVED', 'createdAt': '4', 'updatedAt': '4'},
    ]}, 'comments': {'nodes': [{'id': 'I1', 'author': {'login': 'me'}, 'body': 'hi', 'createdAt': '5', 'updatedAt': '5'}]}}
    api = _api(graphql=lambda q, v: {'repository': {'pullRequest': pr}})
    disc = api.get_mr_discussions('gh:o/r', 2)
    assert [d['id'] for d in disc] == ['R1', 'T1', 'T2', 'I1']
    assert disc[1]['unresolved'] and disc[1]['path'] == 'a.py' and disc[1]['notes'][0]['resolvable']
    assert not disc[2]['unresolved'] and disc[2]['notes'][0]['resolved']
    assert api.get_mr_unresolved_count('gh:o/r', 2) == 1


def test_job_trace_range_full_body_and_partial():
    body = b'0123456789'
    api = _api({('LOG', ''): _Resp(content=body), ('LOG', 'bytes=4-'): _Resp(status=206, content=body[4:],
                                                                             headers={'Content-Range': 'bytes 4-9/10'})})
    api.project_name = 'gh:o/r'
    assert api.get_job_trace_range(9, 4) == (body[4:], 10)
    assert api.get_job_trace(9) == '0123456789'
    api2 = _api({('LOG', ''): _Resp(status=404)})
    api2.project_name = 'gh:o/r'
    assert api2.get_job_trace_range(9, 0) == (b'', 0) and api2.get_job_trace(9) == ''


def test_auto_merge_refused_when_repo_disallows():
    api = _api({'/repos/o/r': {'full_name': 'o/r', 'allow_auto_merge': False}},
               graphql=lambda q, v: {'repository': {'pullRequest': _NODE}})
    assert not api.supports('auto_merge', 'gh:o/r') and api.supports('merge')
    assert not api.supports('revert_tag_commit')
    with pytest.raises(RuntimeError, match='auto-merge disabled'):
        api.set_merge_when_pipeline_succeeds('gh:o/r', 2, True)
    with pytest.raises(Unsupported):
        api.revert_tag_commit('gh:o/r', 'v1', 'abc')


def test_dashboard_lists_use_known_ids_only():
    seen = {}

    def gql(q, v):
        seen['q'] = q
        return {'reviews_requested': {'issueCount': 2, 'nodes': [_NODE]}, 'approved_by_you': {'issueCount': 1}}
    api = _api(graphql=gql)
    api._username_cache = 'me'
    full = [('reviews_requested', 'Review requested', 'active', 1, 'x', 'y'), ('bogus', 'Bogus', 'active', 1, 'x', 'y')]
    counts = [('approved_by_you', 'Approved by you', 'active', 2, 'x', 'y')]
    out = api.get_mr_dashboard_lists(full, counts)
    assert out['reviews_requested']['count'] == 2 and out['reviews_requested']['mrs'][0]['iid'] == 2
    assert out['bogus'] == {'count': 0, 'mrs': []} and out['approved_by_you'] == {'count': 1, 'mrs': []}
    assert GH_DASHBOARD_QUERIES['reviews_requested'] in seen['q'] and 'bogus' not in seen['q']


def test_find_mr_by_branch_and_writes():
    api = _api({'/repos/o/r/pulls': [_PR], ('PATCH', '/repos/o/r/pulls/2'): {}, ('PATCH', '/repos/o/r/issues/2'): {},
                ('POST', '/repos/o/r/pulls/2/reviews'): {}, ('PUT', '/repos/o/r/pulls/2/merge'): {'merged': True},
                ('GET', '/repos/o/r/pulls/2'): _PR, ('DELETE', '/repos/o/r/git/refs/heads/ci-gate'): {}})
    found = api.find_mr_by_branch('gh:o/r', 'ci-gate')
    assert found['iid'] == 2 and api.session.calls[-1][2]['params']['head'] == 'o:ci-gate'
    assert api.set_mr_title('gh:o/r', 2, 'new') and api.set_mr_assignee('gh:o/r', 2, 'me')
    assert api.approve_merge_request('gh:o/r', 2) and api.merge_mr('gh:o/r', 2)
    assert [c[0] for c in api.session.calls][-3:] == ['GET', 'PUT', 'DELETE']


def test_tag_deploys_from_deployments_api():
    nodes = [{'databaseId': 1, 'environment': 'preview/x', 'state': 'INACTIVE', 'ref': {'name': 'x'}, 'commitOid': 'aaaaaaaa1',
              'createdAt': '1', 'updatedAt': '1', 'creator': {'login': 'me'}, 'latestStatus': {'logUrl': 'https://l'}},
             {'databaseId': 2, 'environment': 'preview/x', 'state': 'IN_PROGRESS', 'ref': {'name': 'x'}, 'commitOid': 'bbbbbbbb2',
              'createdAt': '2', 'updatedAt': '2', 'creator': {'login': 'vercel[bot]'}, 'latestStatus': None}]
    api = _api(graphql=lambda q, v: {'repository': {'deployments': {'nodes': nodes}}})
    rows = api.list_tag_pipelines('gh:o/r', username='me')
    assert [r['status'] for r in rows] == ['running', 'success'] and rows[0]['user'] == 'vercel[bot]'
    assert rows[0]['web_url'].endswith('/o/r/deployments') and rows[1]['web_url'] == 'https://l'


def test_paged_keeps_page_size_stable_past_100():
    def page(kw):
        p, n = kw['params']['page'], kw['params']['per_page']
        lo = (p - 1) * n
        return list(range(lo, min(lo + n, 250)))
    api = _api({'/x': page})
    assert api._paged('/x', 250) == list(range(250))
