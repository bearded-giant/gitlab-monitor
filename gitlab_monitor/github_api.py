# Copyright 2024 BeardedGiant
# https://github.com/bearded-giant/gitlab-tools
# Licensed under Apache License 2.0

import time
from datetime import datetime, timedelta, timezone

import requests

from .api import parse_failures, ACTIVITY_TTL, ACTIVITY_WINDOW_DAYS, ACTIVITY_PAGE_CAP
from .constants import LOG_FETCH_TIMEOUT, MR_DASHBOARD_PAGE, MR_DASHBOARD_MERGED_DAYS

GH_PREFIX = 'gh:'
GH_API = 'https://api.github.com'
GH_WEB = 'https://github.com'


def is_github_key(key):
    return bool(key) and str(key).startswith(GH_PREFIX)


def repo_of(key):
    return str(key)[len(GH_PREFIX):] if is_github_key(key) else str(key or '')


def github_key(owner_repo):
    return GH_PREFIX + owner_repo.strip('/')


class Unsupported(RuntimeError):
    pass


_RUN_STATUS = {'queued': 'pending', 'waiting': 'pending', 'requested': 'pending', 'pending': 'pending',
               'in_progress': 'running'}
_CONCLUSION = {'success': 'success', 'failure': 'failed', 'cancelled': 'canceled', 'skipped': 'skipped',
               'timed_out': 'failed', 'action_required': 'manual', 'neutral': 'success', 'stale': 'canceled',
               'startup_failure': 'failed'}
_ROLLUP = {'SUCCESS': 'success', 'FAILURE': 'failed', 'ERROR': 'failed', 'PENDING': 'running', 'EXPECTED': 'pending'}
_DEPLOY = {'ACTIVE': 'success', 'SUCCESS': 'success', 'INACTIVE': 'success', 'PENDING': 'pending',
           'QUEUED': 'pending', 'WAITING': 'pending', 'IN_PROGRESS': 'running', 'FAILURE': 'failed',
           'ERROR': 'failed', 'DESTROYED': 'canceled', 'ABANDONED': 'canceled'}

_SEARCH_STATE = {'opened': 'is:open', 'merged': 'is:merged', 'closed': 'is:closed is:unmerged', 'all': ''}
_PR_STATES = {'opened': ['OPEN'], 'merged': ['MERGED'], 'closed': ['CLOSED'], 'all': ['OPEN', 'MERGED', 'CLOSED']}

# same ids as constants.MR_DASHBOARD_LISTS so the dashboard merges both hosts per list
GH_DASHBOARD_QUERIES = {
    'returned_to_you': 'is:pr is:open author:@me review:changes_requested',
    'reviews_requested': 'is:pr is:open review-requested:@me',
    'assigned_to_you': 'is:pr is:open author:@me review:none',
    'waiting_for_assignee': 'is:pr is:open reviewed-by:@me review:changes_requested',
    'waiting_for_approvals': 'is:pr is:open author:@me review:required',
    'approved_by_you': 'is:pr is:open reviewed-by:@me review:approved',
    'approved_by_others': 'is:pr is:open author:@me review:approved',
    'merged_recently_reviews': 'is:pr is:merged reviewed-by:@me merged:>={merged_after}',
    'merged_recently_assigned': 'is:pr is:merged author:@me merged:>={merged_after}',
}

_PR_NODE = '''
fragment PR on PullRequest {
  id number title body isDraft state url createdAt updatedAt mergedAt closedAt
  repository { nameWithOwner databaseId }
  author { login }
  headRefName baseRefName headRefOid mergeable reviewDecision
  additions deletions changedFiles
  autoMergeRequest { enabledAt }
  mergeCommit { oid }
  assignees(first: 10) { nodes { login } }
  reviewRequests(first: 10) { nodes { requestedReviewer { ... on User { login } ... on Team { name } } } }
  latestReviews(first: 20) { nodes { author { login } state } }
  reviewThreads(first: 100) { totalCount nodes { isResolved } }
  comments { totalCount }
  commits(last: 1) { totalCount nodes { commit { statusCheckRollup { state contexts(first: 50) { nodes {
    ... on CheckRun { status conclusion checkSuite { workflowRun { databaseId url } } }
  } } } } } }
}
'''


def _status(status, conclusion):
    if status == 'completed':
        return _CONCLUSION.get(conclusion or '', 'unknown')
    return _RUN_STATUS.get(status or '', 'unknown')


def _iso(ts):
    return (ts or '').replace('+00:00', 'Z')


def _date_days_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%d')


def _seconds(start, end):
    if not start:
        return None
    try:
        a = datetime.fromisoformat(start.replace('Z', '+00:00'))
        b = datetime.fromisoformat(end.replace('Z', '+00:00')) if end else datetime.now(timezone.utc)
        return (b - a).total_seconds()
    except ValueError:
        return None


class GitHubAPI:

    def __init__(self, config, session=None):
        self.config = config
        self.api_url = (getattr(config, 'github_api_url', None) or GH_API).rstrip('/')
        self.web_url = (getattr(config, 'github_url', None) or GH_WEB).rstrip('/')
        self.session = session or requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {config.github_token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        })
        self.project = None
        self.project_name = None
        self._username_cache = None
        self._user_id_cache = None
        self._activity_cache = None
        self._meta = {}

    # -- transport -----------------------------------------------------------

    def _req(self, method, path, **kw):
        kw.setdefault('timeout', 15)
        resp = self.session.request(method, f'{self.api_url}{path}', **kw)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def _get(self, path, **params):
        return self._req('GET', path, params=params)

    def _paged(self, path, limit, key=None, **params):
        out = []
        page = 1
        while len(out) < limit:
            params.update(per_page=min(100, limit - len(out)), page=page)
            data = self._get(path, **params)
            items = data.get(key) or [] if key else data
            out.extend(items)
            if len(items) < params['per_page']:
                break
            page += 1
        return out[:limit]

    def _graphql(self, query, variables=None):
        resp = self.session.post(f'{self.api_url}/graphql', json={'query': query, 'variables': variables or {}},
                                 timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get('errors') and not payload.get('data'):
            raise RuntimeError(payload['errors'][0].get('message', 'GraphQL error'))
        return payload.get('data') or {}

    def _repo(self, key):
        return repo_of(key or self.project_name)

    def _owner_name(self, key):
        owner, _, name = self._repo(key).partition('/')
        return owner, name

    def _repo_meta(self, key):
        repo = self._repo(key)
        if repo not in self._meta:
            self._meta[repo] = self._get(f'/repos/{repo}')
        return self._meta[repo]

    # -- identity / projects -------------------------------------------------

    def current_username(self):
        if self._username_cache is None:
            try:
                me = self._get('/user')
                self._username_cache = me.get('login') or ''
                self._user_id_cache = me.get('id')
            except Exception:
                return ''
        return self._username_cache

    def current_user_id(self):
        if self._user_id_cache is None:
            self.current_username()
        return self._user_id_cache

    def connect_project(self):
        if is_github_key(self.config.project_path):
            self.set_project(self.config.project_path)

    def set_project(self, project_path):
        self.project = self._repo_meta(project_path)
        self.project_name = project_path

    def supports(self, action, project_path=None):
        if action in ('revert_tag_commit', 'bridges', 'job_trace_live'):
            return False
        if action == 'auto_merge':
            try:
                return bool(self._repo_meta(project_path).get('allow_auto_merge'))
            except Exception:
                return False
        return True

    def host_label(self, project_path=None):
        return ('GitHub', self.web_url.replace('https://', ''))

    def project_url(self, project_path):
        return f'{self.web_url}/{self._repo(project_path)}'

    def mr_url(self, project_path, iid):
        return f'{self.project_url(project_path)}/pull/{iid}'

    def pipeline_url(self, project_path, pipeline_id):
        return f'{self.project_url(project_path)}/actions/runs/{pipeline_id}'

    def commit_url(self, project_path, sha):
        return f'{self.project_url(project_path)}/commit/{sha}'

    def tag_url(self, project_path, name):
        return f'{self.project_url(project_path)}/releases/tag/{name}'

    def dashboard_url(self, merged=False):
        q = 'is:pr+is:merged+author:@me' if merged else 'is:pr+is:open+author:@me'
        return f'{self.web_url}/pulls?q={q}'

    @staticmethod
    def _repo_to_dict(r):
        return {
            'id': r.get('id'),
            'path': github_key(r.get('full_name') or ''),
            'name': r.get('name') or '',
            'description': r.get('description') or '',
            'last_activity': max(r.get('pushed_at') or '', r.get('updated_at') or '') or None,
        }

    def get_projects(self, search=None, per_page=50):
        repos = self._paged('/user/repos', per_page, affiliation='owner,collaborator,organization_member',
                            sort='pushed', direction='desc')
        rows = [self._repo_to_dict(r) for r in repos]
        if search:
            s = search.lower()
            rows = [r for r in rows if s in r['path'].lower() or s in r['description'].lower()]
        return rows

    def get_project_meta(self, project_path):
        try:
            return self._repo_to_dict(self._repo_meta(project_path))
        except Exception:
            return None

    def get_projects_by_paths(self, paths):
        return [m for m in (self.get_project_meta(p) for p in paths if is_github_key(p)) if m]

    def get_default_branch(self, project_path):
        try:
            return self._repo_meta(project_path).get('default_branch') or ''
        except Exception:
            return ''

    # -- workflow runs as pipelines -----------------------------------------

    def _run_to_dict(self, run, project_path=None):
        key = project_path or self.project_name or github_key((run.get('repository') or {}).get('full_name') or '')
        return {
            'id': run.get('id'),
            'status': _status(run.get('status'), run.get('conclusion')),
            'ref': run.get('head_branch') or '',
            'sha': (run.get('head_sha') or '')[:8],
            'created_at': run.get('created_at') or '',
            'updated_at': run.get('updated_at') or '',
            'user': (run.get('actor') or {}).get('login') or 'unknown',
            'web_url': run.get('html_url') or '',
            'project_path': key,
            'name': run.get('name') or '',
        }

    def _runs(self, key, limit, **params):
        runs = self._paged(f'/repos/{self._repo(key)}/actions/runs', limit, key='workflow_runs', **params)
        return [self._run_to_dict(r, project_path=key) for r in runs]

    def get_recent_pipelines(self, limit=50, ref=None, username=None, days=None):
        params = {}
        if ref:
            params['branch'] = ref
        if username:
            params['actor'] = username
        if days is not None:
            params['created'] = f'>={_date_days_ago(days)}'
        return self._runs(self.project_name, limit, **params)

    def list_pipelines_for_ref_since(self, project_path, ref, since_iso, limit=20):
        if not (project_path and ref):
            return []
        params = {'branch': ref}
        if since_iso:
            params['created'] = f'>={since_iso[:10]}'
        try:
            return self._runs(project_path, limit, **params)
        except Exception:
            return []

    def list_my_pipelines_for_project(self, project_path, username, limit=25, days=None):
        if not (project_path and username):
            return []
        params = {'actor': username}
        if days is not None:
            params['created'] = f'>={_date_days_ago(days)}'
        try:
            return self._runs(project_path, limit, **params)
        except Exception:
            return []

    def list_recent_pipelines(self, project_path, ref=None, since_days=None, limit=20):
        params = {}
        ref = ref or self.get_default_branch(project_path)
        if ref:
            params['branch'] = ref
        if since_days is not None:
            params['created'] = f'>={_date_days_ago(since_days)}'
        try:
            return self._runs(project_path, limit, **params)
        except Exception:
            return []

    def list_tag_pipelines(self, project_path, limit=20, username=None):
        # deployments, not tag runs: that is what vercel and the ci workflow write for these repos
        owner, name = self._owner_name(project_path)
        try:
            data = self._graphql(
                'query($o:String!,$n:String!,$l:Int!){repository(owner:$o,name:$n){deployments(last:$l){nodes{'
                'databaseId environment state ref{name} commitOid createdAt updatedAt creator{login} '
                'latestStatus{logUrl environmentUrl}}}}}',
                {'o': owner, 'n': name, 'l': min(limit, 100)},
            )
        except Exception:
            return []
        nodes = ((data.get('repository') or {}).get('deployments') or {}).get('nodes') or []
        out = []
        for d in reversed(nodes):
            st = d.get('latestStatus') or {}
            out.append({
                'id': d.get('databaseId'),
                'status': _DEPLOY.get(d.get('state') or '', 'unknown'),
                'ref': d.get('environment') or (d.get('ref') or {}).get('name') or '',
                'sha': (d.get('commitOid') or '')[:8],
                'created_at': d.get('createdAt') or '',
                'updated_at': d.get('updatedAt') or '',
                'user': (d.get('creator') or {}).get('login') or 'unknown',
                'web_url': st.get('logUrl') or st.get('environmentUrl')
                or f'{self.web_url}/{self._repo(project_path)}/deployments',
                'project_path': project_path,
            })
        return out

    def get_pipeline_bridges(self, pipeline_id):
        return []

    def get_pipeline_detail(self, pipeline_id):
        try:
            run = self._get(f'/repos/{self._repo(None)}/actions/runs/{pipeline_id}')
        except Exception:
            return {}
        started = run.get('run_started_at')
        done = run.get('updated_at') if run.get('status') == 'completed' else None
        return {
            'duration': _seconds(started, done),
            'queued_duration': _seconds(run.get('created_at'), started) if started else None,
            'started_at': started,
            'finished_at': done,
            'source': run.get('event'),
            'coverage': None,
            'user': (run.get('actor') or {}).get('login'),
        }

    def cancel_pipeline(self, pipeline_id):
        self._req('POST', f'/repos/{self._repo(None)}/actions/runs/{pipeline_id}/cancel')
        return 'canceled'

    @staticmethod
    def _job_to_dict(job):
        return {
            'id': job.get('id'),
            'name': job.get('name') or '',
            'status': _status(job.get('status'), job.get('conclusion')),
            'stage': '',
            'duration': _seconds(job.get('started_at'), job.get('completed_at')),
            'started_at': job.get('started_at'),
            'finished_at': job.get('completed_at'),
            'web_url': job.get('html_url') or '',
            'steps': [{
                'number': s.get('number'),
                'name': s.get('name') or '',
                'status': _status(s.get('status'), s.get('conclusion')),
                'started_at': s.get('started_at'),
                'finished_at': s.get('completed_at'),
            } for s in job.get('steps') or []],
        }

    def get_pipeline_jobs(self, pipeline_id):
        try:
            jobs = self._paged(f'/repos/{self._repo(None)}/actions/runs/{pipeline_id}/jobs', 100, key='jobs')
            return [self._job_to_dict(j) for j in jobs]
        except Exception:
            return []

    def get_job(self, job_id):
        try:
            return self._job_to_dict(self._get(f'/repos/{self._repo(None)}/actions/jobs/{job_id}'))
        except Exception:
            return None

    def _log_response(self, job_id, offset=0):
        headers = {'Range': f'bytes={int(offset)}-'} if offset else {}
        return self.session.get(f'{self.api_url}/repos/{self._repo(None)}/actions/jobs/{int(job_id)}/logs',
                                headers=headers, timeout=LOG_FETCH_TIMEOUT, allow_redirects=True)

    def get_job_trace(self, job_id):
        try:
            resp = self._log_response(job_id)
            if resp.status_code != 200:
                return ''
            return resp.content.decode('utf-8', errors='replace')
        except Exception as e:
            return f"Error fetching trace: {e}"

    def get_job_trace_range(self, job_id, offset):
        try:
            resp = self._log_response(job_id, offset)
            if resp.status_code == 206:
                total = offset + len(resp.content)
                cr = resp.headers.get('Content-Range') or ''
                if '/' in cr:
                    try:
                        total = int(cr.rsplit('/', 1)[1])
                    except ValueError:
                        pass
                return resp.content, total
            if resp.status_code == 200:
                body = resp.content
                return body[offset:], len(body)
            return b'', offset
        except Exception:
            return b'', offset

    def get_job_failures(self, job_id):
        return parse_failures(self.get_job_trace(job_id))

    def get_my_activity_counts(self, days=ACTIVITY_WINDOW_DAYS, force=False):
        now = time.time()
        if not force and self._activity_cache is not None:
            ts, cached = self._activity_cache
            if now - ts < ACTIVITY_TTL:
                return cached
        try:
            me = self.current_username()
            events = self._paged(f'/users/{me}/events', ACTIVITY_PAGE_CAP * 100)
            counts = {}
            for ev in events:
                created = ev.get('created_at')
                if not created:
                    continue
                try:
                    d = datetime.fromisoformat(created.replace('Z', '+00:00')).astimezone().date()
                except ValueError:
                    continue
                counts[d] = counts.get(d, 0) + 1
            today = datetime.now().astimezone().date()
            result = [{'date': (today - timedelta(days=i)).isoformat(), 'day_offset': i,
                       'count': counts.get(today - timedelta(days=i), 0)} for i in range(days)]
        except Exception:
            result = [{'date': '', 'day_offset': i, 'count': 0} for i in range(days)]
        self._activity_cache = (now, result)
        return result

    # -- pull requests as merge requests -------------------------------------

    @staticmethod
    def _pr_state(pr):
        if pr.get('merged_at'):
            return 'merged'
        return 'opened' if pr.get('state') == 'open' else 'closed'

    def _pr_to_dict(self, pr, project_path=None):
        base = pr.get('base') or {}
        head = pr.get('head') or {}
        key = project_path or github_key((base.get('repo') or {}).get('full_name') or '')
        return {
            'id': pr.get('id'),
            'iid': pr.get('number'),
            'project_id': (base.get('repo') or {}).get('id'),
            'project_path': key,
            'title': pr.get('title') or '',
            'description': pr.get('body') or '',
            'state': self._pr_state(pr),
            'draft': bool(pr.get('draft')),
            'merge_status': pr.get('mergeable_state'),
            'source_branch': head.get('ref') or '',
            'target_branch': base.get('ref') or '',
            'created_at': pr.get('created_at'),
            'updated_at': pr.get('updated_at'),
            'merged_at': pr.get('merged_at'),
            'closed_at': pr.get('closed_at'),
            'author': (pr.get('user') or {}).get('login') or 'unknown',
            'reviewers': [u.get('login') for u in pr.get('requested_reviewers') or [] if u.get('login')],
            'assignees': [u.get('login') for u in pr.get('assignees') or [] if u.get('login')],
            'sha': head.get('sha'),
            'web_url': pr.get('html_url') or '',
            'user_notes_count': (pr.get('comments') or 0) + (pr.get('review_comments') or 0),
            'upvotes': 0,
            'downvotes': 0,
            'head_pipeline_status': None,
            'head_pipeline_id': None,
            'head_pipeline_web_url': None,
            'has_conflicts': pr.get('mergeable_state') == 'dirty',
            'blocking_discussions_resolved': True,
            'merge_when_pipeline_succeeds': pr.get('auto_merge') is not None,
            'merge_commit_sha': pr.get('merge_commit_sha'),
        }

    @staticmethod
    def _rollup_pipeline(n):
        commits = (n.get('commits') or {}).get('nodes') or []
        roll = ((commits[0].get('commit') or {}).get('statusCheckRollup') if commits else None) or {}
        run = None
        for c in (roll.get('contexts') or {}).get('nodes') or []:
            wr = (c.get('checkSuite') or {}).get('workflowRun') if c else None
            if wr:
                run = wr
                break
        return {
            'status': _ROLLUP.get(roll.get('state') or '', None),
            'id': (run or {}).get('databaseId'),
            'web_url': (run or {}).get('url'),
        }

    def _gql_pr_to_dict(self, n):
        me = self.current_username()
        reviews = (n.get('latestReviews') or {}).get('nodes') or []
        states = {(r.get('author') or {}).get('login'): r.get('state') for r in reviews}
        approved_by = [u for u, s in states.items() if s == 'APPROVED' and u]
        requested = [((r.get('requestedReviewer') or {}).get('login') or (r.get('requestedReviewer') or {}).get('name'))
                     for r in (n.get('reviewRequests') or {}).get('nodes') or []]
        threads = n.get('reviewThreads') or {}
        resolved = sum(1 for t in threads.get('nodes') or [] if t.get('isResolved'))
        hp = self._rollup_pipeline(n)
        decision = n.get('reviewDecision')
        # reviewDecision is null only when the base branch requires no reviews
        required = 1 if decision else 0
        state = {'OPEN': 'opened', 'MERGED': 'merged', 'CLOSED': 'closed'}.get(n.get('state'), n.get('state'))
        return {
            'id': n.get('id'),
            'iid': int(n['number']),
            'project_id': (n.get('repository') or {}).get('databaseId'),
            'project_path': github_key((n.get('repository') or {}).get('nameWithOwner') or ''),
            'title': n.get('title') or '',
            'description': n.get('body') or '',
            'state': state,
            'draft': bool(n.get('isDraft')),
            'merge_status': (n.get('mergeable') or '').lower() or None,
            'source_branch': n.get('headRefName') or '',
            'target_branch': n.get('baseRefName') or '',
            'created_at': n.get('createdAt'),
            'updated_at': n.get('updatedAt'),
            'merged_at': n.get('mergedAt'),
            'closed_at': n.get('closedAt'),
            'author': (n.get('author') or {}).get('login') or 'unknown',
            'reviewers': [r for r in requested if r] + [u for u in states if u and u not in requested],
            'assignees': [a.get('login') for a in (n.get('assignees') or {}).get('nodes') or []],
            'sha': n.get('headRefOid'),
            'web_url': n.get('url') or '',
            'user_notes_count': (n.get('comments') or {}).get('totalCount') or 0,
            'upvotes': 0,
            'downvotes': 0,
            'head_pipeline_status': hp['status'],
            'head_pipeline_id': hp['id'],
            'head_pipeline_web_url': hp['web_url'],
            'has_conflicts': n.get('mergeable') == 'CONFLICTING',
            'blocking_discussions_resolved': resolved == (threads.get('totalCount') or 0),
            'merge_when_pipeline_succeeds': bool(n.get('autoMergeRequest')),
            'merge_commit_sha': (n.get('mergeCommit') or {}).get('oid'),
            'review_states': states,
            'approved_by': approved_by,
            'approvals_count': len(approved_by),
            'approvals_required': required,
            'approvals_left': max(required - len(approved_by), 0),
            'user_has_approved': states.get(me) == 'APPROVED',
            'threads_resolved': resolved,
            'threads_resolvable': threads.get('totalCount') or 0,
            'diff_files': n.get('changedFiles'),
            'additions': n.get('additions'),
            'deletions': n.get('deletions'),
            'changes_count': n.get('changedFiles') or 0,
            'commits_count': (n.get('commits') or {}).get('totalCount') or 0,
            'ready': state == 'opened' and hp['status'] not in ('failed',) and n.get('mergeable') != 'CONFLICTING'
                     and decision != 'CHANGES_REQUESTED',
        }

    def _search(self, query, limit=50, get_all=False):
        out = []
        cursor = None
        while True:
            data = self._graphql(
                'query($q:String!,$n:Int!,$c:String){search(query:$q,type:ISSUE,first:$n,after:$c){'
                'issueCount pageInfo{hasNextPage endCursor} nodes{...PR}}}' + _PR_NODE,
                {'q': query, 'n': min(100, max(1, limit - len(out))), 'c': cursor},
            )
            blk = data.get('search') or {}
            out.extend(self._gql_pr_to_dict(n) for n in blk.get('nodes') or [] if n)
            page = blk.get('pageInfo') or {}
            if not get_all or not page.get('hasNextPage') or len(out) >= limit:
                break
            cursor = page.get('endCursor')
        return out[:limit]

    def get_my_merge_requests(self, state='opened', limit=50, days=None, created_after=None, get_all=False):
        q = f'is:pr author:@me {_SEARCH_STATE.get(state, "")} sort:updated-desc'
        if days is not None:
            q += f' updated:>={_date_days_ago(days)}'
        if created_after:
            q += f' created:>={str(created_after)[:10]}'
        return self._search(q, limit, get_all)

    def get_review_requests(self, state='opened', limit=100, approved_by_me=False):
        if approved_by_me:
            rows = self._search(f'is:pr reviewed-by:@me {_SEARCH_STATE.get(state, "")} sort:updated-desc', limit)
            return [r for r in rows if r.get('user_has_approved')]
        return self._search(f'is:pr review-requested:@me {_SEARCH_STATE.get(state, "")} sort:updated-desc', limit)

    def get_project_merge_requests(self, project_path, state='merged', limit=50):
        owner, name = self._owner_name(project_path)
        data = self._graphql(
            'query($o:String!,$n:String!,$s:[PullRequestState!],$l:Int!){repository(owner:$o,name:$n){'
            'pullRequests(states:$s,first:$l,orderBy:{field:UPDATED_AT,direction:DESC}){nodes{...PR}}}}' + _PR_NODE,
            {'o': owner, 'n': name, 's': _PR_STATES.get(state, _PR_STATES['all']), 'l': min(limit, 100)},
        )
        nodes = ((data.get('repository') or {}).get('pullRequests') or {}).get('nodes') or []
        return [self._gql_pr_to_dict(n) for n in nodes]

    def _pr_node(self, project_path, iid):
        owner, name = self._owner_name(project_path)
        data = self._graphql(
            'query($o:String!,$n:String!,$i:Int!){repository(owner:$o,name:$n){pullRequest(number:$i){...PR}}}' + _PR_NODE,
            {'o': owner, 'n': name, 'i': int(iid)},
        )
        return (data.get('repository') or {}).get('pullRequest')

    def get_merge_request(self, project_path, iid):
        try:
            node = self._pr_node(project_path, iid)
            return self._gql_pr_to_dict(node) if node else None
        except Exception:
            return None

    def get_merge_request_basic(self, project_path, iid):
        return self._pr_to_dict(self._get(f'/repos/{self._repo(project_path)}/pulls/{int(iid)}'), project_path)

    def find_mr_by_branch(self, project_path, branch):
        owner, _ = self._owner_name(project_path)
        prs = self._get(f'/repos/{self._repo(project_path)}/pulls', head=f'{owner}:{branch}', state='all',
                        sort='updated', direction='desc', per_page=1)
        return self._pr_to_dict(prs[0], project_path) if prs else None

    def get_mr_approval_state(self, project_path, iid):
        try:
            d = self.get_merge_request(project_path, iid) or {}
        except Exception:
            return []
        if not d or not d.get('approvals_required'):
            return []
        return [{
            'id': None,
            'name': 'Reviews',
            'rule_type': 'any_approver',
            'approvals_required': d['approvals_required'],
            'approved': d.get('approvals_left', 1) == 0,
            'approved_by': d.get('approved_by') or [],
        }]

    def get_mr_approvals_summary(self, project_path, iid):
        d = self.get_merge_request(project_path, iid)
        if not d:
            return None
        return {
            'approvals_count': d['approvals_count'],
            'approvals_required': d['approvals_required'],
            'user_has_approved': d['user_has_approved'],
        }

    def get_mr_pipelines(self, project_path, iid):
        try:
            pr = self._get(f'/repos/{self._repo(project_path)}/pulls/{int(iid)}')
            sha = (pr.get('head') or {}).get('sha')
            return self._runs(project_path, 50, head_sha=sha) if sha else []
        except Exception:
            return []

    def get_mr_head_pipelines(self, project_path, iids):
        owner, name = self._owner_name(project_path)
        iids = [int(i) for i in iids][:100]
        if not iids:
            return {}
        fields = ' '.join(
            f'pr_{i}: pullRequest(number:{i}){{number commits(last:1){{nodes{{commit{{statusCheckRollup{{state '
            f'contexts(first:50){{nodes{{... on CheckRun{{checkSuite{{workflowRun{{databaseId url}}}}}}}}}}}}}}}}}}}}'
            for i in iids
        )
        data = self._graphql(f'query($o:String!,$n:String!){{repository(owner:$o,name:$n){{{fields}}}}}',
                             {'o': owner, 'n': name})
        out = {}
        for n in (data.get('repository') or {}).values():
            if isinstance(n, dict) and n.get('number'):
                hp = self._rollup_pipeline(n)
                if hp['status']:
                    out[int(n['number'])] = hp
        return out

    def get_commit_pipeline_status(self, project_path, sha):
        if not sha:
            return ''
        owner, name = self._owner_name(project_path)
        try:
            data = self._graphql(
                'query($o:String!,$n:String!,$s:GitObjectID!){repository(owner:$o,name:$n){object(oid:$s){'
                '... on Commit{statusCheckRollup{state}}}}}',
                {'o': owner, 'n': name, 's': sha},
            )
        except Exception:
            return ''
        roll = ((data.get('repository') or {}).get('object') or {}).get('statusCheckRollup') or {}
        return _ROLLUP.get(roll.get('state') or '', '')

    def get_commit_pipeline(self, project_path, sha):
        if not sha:
            return None
        runs = self._runs(project_path, 1, head_sha=sha)
        if not runs:
            return None
        r = runs[0]
        return {'id': r['id'], 'status': r['status'], 'ref': r['ref'], 'sha': sha[:40], 'web_url': r['web_url'],
                'project_path': project_path}

    def get_mr_commits(self, project_path, iid):
        try:
            commits = self._paged(f'/repos/{self._repo(project_path)}/pulls/{int(iid)}/commits', 250)
        except Exception:
            return []
        out = []
        for c in commits:
            meta = c.get('commit') or {}
            msg = (meta.get('message') or '').strip()
            out.append({
                'sha': c.get('sha') or '',
                'short_sha': (c.get('sha') or '')[:8],
                'title': msg.splitlines()[0] if msg else '',
                'author_name': (meta.get('author') or {}).get('name') or '',
                'created_at': (meta.get('author') or {}).get('date') or '',
                'web_url': c.get('html_url') or '',
                'pipeline_status': '',
            })
        return out

    @staticmethod
    def _note(c):
        return {
            'id': c.get('id') or c.get('databaseId'),
            'author': (c.get('author') or {}).get('login') or 'unknown',
            'body': c.get('body') or '',
            'created_at': c.get('createdAt') or '',
            'updated_at': c.get('updatedAt') or '',
            'system': False,
            'resolved': False,
            'resolvable': False,
        }

    def get_mr_discussions(self, project_path, iid):
        owner, name = self._owner_name(project_path)
        try:
            data = self._graphql(
                'query($o:String!,$n:String!,$i:Int!){repository(owner:$o,name:$n){pullRequest(number:$i){'
                'reviewThreads(first:100){nodes{id isResolved path line comments(first:50){nodes{'
                'id author{login} body createdAt updatedAt}}}} '
                'reviews(first:100){nodes{id author{login} body state createdAt updatedAt}} '
                'comments(first:100){nodes{id author{login} body createdAt updatedAt}}}}}',
                {'o': owner, 'n': name, 'i': int(iid)},
            )
        except Exception:
            return []
        pr = (data.get('repository') or {}).get('pullRequest') or {}
        out = []
        for t in (pr.get('reviewThreads') or {}).get('nodes') or []:
            comments = (t.get('comments') or {}).get('nodes') or []
            if not comments:
                continue
            unresolved = not t.get('isResolved')
            notes = [dict(self._note(c), resolvable=True, resolved=not unresolved) for c in comments]
            first = comments[0]
            out.append({
                'id': t.get('id'),
                'unresolved': unresolved,
                'resolvable': True,
                'notes': notes,
                'first_author': notes[0]['author'],
                'first_created_at': first.get('createdAt') or '',
                'system': False,
                'path': t.get('path') or '',
                'line': t.get('line'),
                'head_sha': '',
            })
        for r in (pr.get('reviews') or {}).get('nodes') or []:
            if not (r.get('body') or '').strip():
                continue
            note = self._note(r)
            out.append({'id': r.get('id'), 'unresolved': False, 'resolvable': False, 'notes': [note],
                        'first_author': note['author'], 'first_created_at': note['created_at'], 'system': False,
                        'path': '', 'line': None, 'head_sha': ''})
        for c in (pr.get('comments') or {}).get('nodes') or []:
            note = self._note(c)
            out.append({'id': c.get('id'), 'unresolved': False, 'resolvable': False, 'notes': [note],
                        'first_author': note['author'], 'first_created_at': note['created_at'], 'system': False,
                        'path': '', 'line': None, 'head_sha': ''})
        out.sort(key=lambda d: d['first_created_at'])
        return out

    def get_mr_unresolved_count(self, project_path, iid):
        return sum(1 for d in self.get_mr_discussions(project_path, iid) if d.get('unresolved'))

    def approve_merge_request(self, project_path, iid):
        self._req('POST', f'/repos/{self._repo(project_path)}/pulls/{int(iid)}/reviews', json={'event': 'APPROVE'})
        return True

    def close_merge_request(self, project_path, iid):
        self._req('PATCH', f'/repos/{self._repo(project_path)}/pulls/{int(iid)}', json={'state': 'closed'})
        return True

    def list_project_users(self, project_path, force=False):
        store = self.config.project_users
        users = None if force else store.get(project_path)
        if users is None:
            rows = self._paged(f'/repos/{self._repo(project_path)}/collaborators', 300)
            users = sorted(({'id': u.get('login'), 'username': u.get('login'), 'name': ''} for u in rows),
                           key=lambda u: u['username'].lower())
            store.set(project_path, users)
        return users

    def set_mr_title(self, project_path, iid, title):
        self._req('PATCH', f'/repos/{self._repo(project_path)}/pulls/{int(iid)}', json={'title': title})
        return True

    def set_mr_assignee(self, project_path, iid, user_id):
        self._req('PATCH', f'/repos/{self._repo(project_path)}/issues/{int(iid)}', json={'assignees': [user_id]})
        return True

    def set_merge_when_pipeline_succeeds(self, project_path, iid, enable):
        node = self._pr_node(project_path, iid) or {}
        if not node.get('id'):
            raise RuntimeError('pull request not found')
        if not enable:
            res = self._graphql('mutation($id:ID!){disablePullRequestAutoMerge(input:{pullRequestId:$id}){clientMutationId}}',
                                {'id': node['id']})
            return False
        if not self.supports('auto_merge', project_path):
            raise RuntimeError('repo has auto-merge disabled (Settings > General > Allow auto-merge)')
        res = self._graphql(
            'mutation($id:ID!){enablePullRequestAutoMerge(input:{pullRequestId:$id,mergeMethod:SQUASH}){'
            'pullRequest{autoMergeRequest{enabledAt}}}}',
            {'id': node['id']},
        )
        return bool(((res.get('enablePullRequestAutoMerge') or {}).get('pullRequest') or {}).get('autoMergeRequest'))

    def merge_mr(self, project_path, iid, squash=True, delete_source_branch=True):
        repo = self._repo(project_path)
        pr = self._get(f'/repos/{repo}/pulls/{int(iid)}')
        self._req('PUT', f'/repos/{repo}/pulls/{int(iid)}/merge', json={'merge_method': 'squash' if squash else 'merge'})
        head = pr.get('head') or {}
        same_repo = ((head.get('repo') or {}).get('full_name') or '') == repo
        if delete_source_branch and same_repo and head.get('ref'):
            try:
                self._req('DELETE', f'/repos/{repo}/git/refs/heads/{head["ref"]}')
            except requests.HTTPError:
                pass
        return True

    def create_mr_note(self, project_path, iid, body):
        return self._req('POST', f'/repos/{self._repo(project_path)}/issues/{int(iid)}/comments', json={'body': body})

    # -- dashboard -----------------------------------------------------------

    def get_mr_dashboard_lists(self, full, counts=()):
        merged_after = _date_days_ago(MR_DASHBOARD_MERGED_DAYS)
        aliases = []
        for s in list(full) + list(counts):
            q = GH_DASHBOARD_QUERIES.get(s[0])
            if q:
                aliases.append((s[0], q.format(merged_after=merged_after), s in full))
        if not aliases:
            return {s[0]: {'count': 0, 'mrs': []} for s in list(full) + list(counts)}
        body = ' '.join(
            f'{lid}: search(query:"{q} sort:updated-desc",type:ISSUE,first:{MR_DASHBOARD_PAGE if with_nodes else 1})'
            f'{{issueCount {"nodes{...PR}" if with_nodes else ""}}}'
            for lid, q, with_nodes in aliases
        )
        data = self._graphql(f'query{{{body}}}' + _PR_NODE)
        out = {}
        for s in list(full) + list(counts):
            blk = data.get(s[0]) or {}
            out[s[0]] = {'count': blk.get('issueCount') or 0,
                         'mrs': [self._gql_pr_to_dict(n) for n in blk.get('nodes') or [] if n]}
        return out

    # -- tags / commits ------------------------------------------------------

    def list_tags(self, project_path, limit=50):
        owner, name = self._owner_name(project_path)
        data = self._graphql(
            'query($o:String!,$n:String!,$l:Int!){repository(owner:$o,name:$n){refs(refPrefix:"refs/tags/",first:$l,'
            'orderBy:{field:TAG_COMMIT_DATE,direction:DESC}){nodes{name target{oid '
            '... on Tag{message tagger{date} target{oid ... on Commit{committedDate}}} '
            '... on Commit{committedDate messageHeadline}}}}}}',
            {'o': owner, 'n': name, 'l': min(limit, 100)},
        )
        out = []
        for t in ((data.get('repository') or {}).get('refs') or {}).get('nodes') or []:
            target = t.get('target') or {}
            inner = target.get('target') or {}
            sha = inner.get('oid') or target.get('oid') or ''
            created = (target.get('tagger') or {}).get('date') or inner.get('committedDate') or target.get('committedDate') or ''
            out.append({
                'name': t.get('name') or '',
                'target': sha,
                'short_sha': sha[:8],
                'created_at': created,
                'message': (target.get('message') or '').strip(),
                'pipeline_status': '',
            })
        return out

    def run_tag_pipeline(self, project_path, ref):
        repo = self._repo(project_path)
        workflows = [w for w in (self._get(f'/repos/{repo}/actions/workflows').get('workflows') or [])
                     if w.get('state') == 'active']
        if not workflows:
            raise RuntimeError('no active workflow to dispatch')
        try:
            self._req('POST', f'/repos/{repo}/actions/workflows/{workflows[0]["id"]}/dispatches', json={'ref': ref})
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 422:
                raise RuntimeError(f'{workflows[0].get("name")} has no workflow_dispatch trigger')
            raise
        return {'id': None, 'status': 'pending', 'web_url': f'{self.web_url}/{repo}/actions'}

    def revert_tag_commit(self, project_path, tag_name, sha, target_branch=None):
        raise Unsupported('GitHub has no revert endpoint; revert locally and open a PR')

    def create_tag(self, project_path, name, ref=None, message=''):
        repo = self._repo(project_path)
        ref = ref or self.get_default_branch(project_path) or 'HEAD'
        sha = self._get(f'/repos/{repo}/commits/{ref}').get('sha')
        if message:
            tag = self._req('POST', f'/repos/{repo}/git/tags',
                            json={'tag': name, 'message': message, 'object': sha, 'type': 'commit'})
            sha = tag.get('sha') or sha
        self._req('POST', f'/repos/{repo}/git/refs', json={'ref': f'refs/tags/{name}', 'sha': sha})
        return True

    def list_commits(self, project_path, ref=None, since_days=None, limit=50):
        params = {}
        ref = ref or self.get_default_branch(project_path)
        if ref:
            params['sha'] = ref
        if since_days is not None:
            params['since'] = _date_days_ago(since_days) + 'T00:00:00Z'
        commits = self._paged(f'/repos/{self._repo(project_path)}/commits', limit, **params)
        out = []
        for c in commits:
            meta = c.get('commit') or {}
            msg = (meta.get('message') or '').strip()
            cid = c.get('sha') or ''
            out.append({
                'id': cid,
                'short_id': cid[:8],
                'title': msg.splitlines()[0] if msg else '',
                'message': msg,
                'author_name': (meta.get('author') or {}).get('name') or '',
                'authored_date': (meta.get('author') or {}).get('date') or '',
                'web_url': c.get('html_url') or '',
                'pipeline_status': '',
            })
        return out

    def get_commit(self, project_path, sha):
        c = self._get(f'/repos/{self._repo(project_path)}/commits/{sha}')
        meta = c.get('commit') or {}
        msg = (meta.get('message') or '').strip()
        stats = c.get('stats') or {}
        cid = c.get('sha') or ''
        return {
            'id': cid,
            'short_id': cid[:8],
            'title': msg.splitlines()[0] if msg else '',
            'message': msg,
            'author_name': (meta.get('author') or {}).get('name') or '',
            'author_email': (meta.get('author') or {}).get('email') or '',
            'authored_date': (meta.get('author') or {}).get('date') or '',
            'committed_date': (meta.get('committer') or {}).get('date') or '',
            'web_url': c.get('html_url') or '',
            'pipeline_status': self.get_commit_pipeline_status(project_path, cid),
            'stats': {'additions': stats.get('additions', 0), 'deletions': stats.get('deletions', 0),
                      'total': stats.get('total', 0)},
            'files': [{
                'path': f.get('filename') or '',
                'new_file': f.get('status') == 'added',
                'deleted_file': f.get('status') == 'removed',
                'renamed_file': f.get('status') == 'renamed',
            } for f in c.get('files') or []],
        }
