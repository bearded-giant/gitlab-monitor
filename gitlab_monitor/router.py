# Copyright 2024 BeardedGiant
# https://github.com/bearded-giant/gitlab-tools
# Licensed under Apache License 2.0

from concurrent.futures import ThreadPoolExecutor

from .api import GitLabAPI
from .github_api import GitHubAPI, is_github_key

_IMPLICIT_PROJECT = frozenset({
    'get_recent_pipelines', 'get_pipeline_bridges', 'get_pipeline_detail', 'cancel_pipeline',
    'get_pipeline_jobs', 'get_job', 'get_job_trace', 'get_job_trace_range', 'get_job_failures',
})
_FANOUT = frozenset({'get_my_merge_requests', 'get_review_requests', 'get_projects'})


class MultiAPI:

    def __init__(self, config, providers=None):
        self.config = config
        if providers is None:
            providers = {}
            gl = getattr(config, 'gitlab_enabled', None)
            if gl is None:
                gl = bool(getattr(config, 'gitlab_url', None) and getattr(config, 'gitlab_token', None))
            if gl:
                providers['gitlab'] = GitLabAPI(config)
            if getattr(config, 'github_enabled', False):
                providers['github'] = GitHubAPI(config)
        self.providers = providers
        self._current = providers.get('gitlab') or next(iter(providers.values()), None)
        self._activity_cache = None

    def fresh(self):
        return MultiAPI(self.config)

    @property
    def project(self):
        return self._current.project if self._current else None

    @property
    def project_name(self):
        return self._current.project_name if self._current else None

    def provider_for(self, project_path):
        name = 'github' if is_github_key(project_path) else 'gitlab'
        p = self.providers.get(name)
        if p is None:
            raise RuntimeError(f'{name} is not configured')
        return p

    def set_project(self, project_path):
        p = self.provider_for(project_path)
        p.set_project(project_path)
        self._current = p

    def connect_project(self):
        if self.config.project_path:
            self.set_project(self.config.project_path)

    def supports(self, action, project_path=None):
        p = self.provider_for(project_path) if project_path else self._current
        return bool(p and p.supports(action, project_path))

    def host_label(self, project_path=None):
        key = project_path or self.project_name
        if key or len(self.providers) == 1:
            p = self.provider_for(key) if key else self._current
            return p.host_label(key)
        return ('Hosts', ' + '.join((p.host_label()[1] or '').replace('https://', '') for p in self.providers.values()))

    def project_url(self, project_path):
        return self.provider_for(project_path).project_url(project_path)

    def dashboard_url(self, merged=False):
        return self._current.dashboard_url(merged) if self._current else ''

    def _fanout(self, name, *args, **kwargs):
        if len(self.providers) == 1:
            return [getattr(self._current, name)(*args, **kwargs)]
        with ThreadPoolExecutor(max_workers=len(self.providers)) as pool:
            futures = [pool.submit(getattr(p, name), *args, **kwargs) for p in self.providers.values()]
        results, err = [], None
        for f in futures:
            try:
                results.append(f.result())
            except Exception as e:
                err = e
        if not results and err:
            raise err
        return results

    def current_username(self):
        return self._current.current_username() if self._current else ''

    def current_user_id(self):
        return self._current.current_user_id() if self._current else None

    def get_projects_by_paths(self, paths):
        out = []
        for p in self.providers.values():
            mine = [x for x in paths if (self.providers.get('github') is p) == is_github_key(x)]
            if mine:
                out.extend(p.get_projects_by_paths(mine))
        by_path = {m['path']: m for m in out}
        return [by_path[x] for x in paths if x in by_path]

    def get_my_activity_counts(self, days=7, force=False):
        merged = {}
        for rows in self._fanout('get_my_activity_counts', days, force):
            for r in rows or []:
                cur = merged.get(r['day_offset'])
                if cur is None:
                    merged[r['day_offset']] = dict(r)
                else:
                    cur['count'] = cur.get('count', 0) + (r.get('count') or 0)
                    cur['date'] = cur.get('date') or r.get('date', '')
        return [merged[i] for i in sorted(merged)]

    @staticmethod
    def dashboard_batches(tab):
        return GitLabAPI.dashboard_batches(tab)

    def get_mr_dashboard_lists(self, full, counts=()):
        out = {}
        for res in self._fanout('get_mr_dashboard_lists', full, counts):
            for lid, blk in (res or {}).items():
                cur = out.setdefault(lid, {'count': 0, 'mrs': []})
                cur['count'] += blk.get('count') or 0
                cur['mrs'].extend(blk.get('mrs') or [])
        for blk in out.values():
            blk['mrs'].sort(key=lambda m: m.get('updated_at') or '', reverse=True)
        return out

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        if name in _FANOUT:
            def fan(*args, **kwargs):
                rows = []
                for r in self._fanout(name, *args, **kwargs):
                    rows.extend(r or [])
                rows.sort(key=lambda m: m.get('updated_at') or m.get('last_activity') or '', reverse=True)
                return rows
            return fan
        if name in _IMPLICIT_PROJECT:
            return getattr(self._current, name)

        def dispatch(project_path, *args, **kwargs):
            return getattr(self.provider_for(project_path), name)(project_path, *args, **kwargs)
        return dispatch
