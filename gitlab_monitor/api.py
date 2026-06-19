# Copyright 2024 BeardedGiant
# https://github.com/bearded-giant/gitlab-tools
# Licensed under Apache License 2.0

import re
import time
from datetime import datetime, timedelta, timezone

import gitlab

from .config import Config
from .constants import LOG_FETCH_TIMEOUT


ACTIVITY_TTL = 300
ACTIVITY_WINDOW_DAYS = 7
ACTIVITY_PAGE_CAP = 5


class GitLabAPI:

    def __init__(self, config: Config):
        self.config = config
        self.gl = gitlab.Gitlab(config.gitlab_url, private_token=config.gitlab_token)
        self.project = None
        self.project_name = None
        self._username_cache = None
        self._activity_cache = None  # (timestamp, result)

    def current_username(self):
        if self._username_cache is None:
            try:
                self.gl.auth()
                self._username_cache = getattr(self.gl.user, 'username', None) or ''
            except Exception:
                self._username_cache = ''
        return self._username_cache

    def connect_project(self):
        if self.config.project_path:
            self.project = self.gl.projects.get(self.config.project_path)
            self.project_name = self.config.project_path

    def set_project(self, project_path: str):
        self.project = self.gl.projects.get(project_path)
        self.project_name = project_path

    @staticmethod
    def _project_activity(p):
        # GitLab's last_activity_at lags + skips pipeline events; take max with updated_at
        a = getattr(p, 'last_activity_at', None) or ''
        u = getattr(p, 'updated_at', None) or ''
        return max(a, u) or None

    def get_projects(self, search=None, per_page=50):
        params = {'per_page': per_page, 'order_by': 'last_activity_at', 'sort': 'desc', 'membership': True}
        if search:
            params['search'] = search
        projects = self.gl.projects.list(**params)
        return [{
            'id': p.id,
            'path': p.path_with_namespace,
            'name': p.name,
            'description': p.description or '',
            'last_activity': self._project_activity(p),
        } for p in projects]

    def get_project_meta(self, project_path):
        try:
            p = self.gl.projects.get(project_path)
            return {
                'id': p.id,
                'path': p.path_with_namespace,
                'name': p.name,
                'description': p.description or '',
                'last_activity': self._project_activity(p),
            }
        except Exception:
            return None

    def get_projects_by_paths(self, paths):
        results = []
        for path in paths:
            meta = self.get_project_meta(path)
            if meta:
                results.append(meta)
        return results

    def get_recent_pipelines(self, limit=50, ref=None, username=None, days=None):
        params = {'per_page': limit, 'order_by': 'id', 'sort': 'desc'}
        if ref:
            params['ref'] = ref
        if username:
            params['username'] = username
        if days is not None:
            since = datetime.now(timezone.utc) - timedelta(days=days)
            params['updated_after'] = since.isoformat().replace('+00:00', 'Z')
        pipelines = self.project.pipelines.list(**params)
        results = []
        for p in pipelines:
            results.append({
                'id': p.id,
                'status': p.status,
                'ref': p.ref,
                'sha': p.sha[:8],
                'created_at': p.created_at,
                'updated_at': p.updated_at,
                'user': getattr(p, 'user', {}).get('username', 'unknown') if hasattr(p, 'user') and p.user else 'unknown',
                'web_url': p.web_url,
            })
        return results

    @staticmethod
    def _pipeline_to_dict(p, project_path=None):
        user = getattr(p, 'user', None) or {}
        username = user.get('username') if isinstance(user, dict) else getattr(user, 'username', None)
        return {
            'id': getattr(p, 'id', None),
            'status': getattr(p, 'status', 'unknown'),
            'ref': getattr(p, 'ref', '') or '',
            'sha': (getattr(p, 'sha', '') or '')[:8],
            'created_at': getattr(p, 'created_at', '') or '',
            'updated_at': getattr(p, 'updated_at', '') or '',
            'user': username or 'unknown',
            'web_url': getattr(p, 'web_url', '') or '',
            'project_path': project_path or '',
        }

    def list_pipelines_for_ref_since(self, project_path, ref, since_iso, limit=20):
        if not (project_path and ref):
            return []
        params = {'per_page': limit, 'order_by': 'id', 'sort': 'desc', 'ref': ref}
        if since_iso:
            params['updated_after'] = since_iso
        try:
            project = self.gl.projects.get(project_path)
            pipelines = project.pipelines.list(**params)
            return [self._pipeline_to_dict(p, project_path=project_path) for p in pipelines]
        except Exception:
            return []

    def list_my_pipelines_for_project(self, project_path, username, limit=25, days=None):
        if not (project_path and username):
            return []
        params = {'per_page': limit, 'order_by': 'id', 'sort': 'desc', 'username': username}
        if days is not None:
            since = datetime.now(timezone.utc) - timedelta(days=days)
            params['updated_after'] = since.isoformat().replace('+00:00', 'Z')
        try:
            project = self.gl.projects.get(project_path)
            pipelines = project.pipelines.list(**params)
            return [self._pipeline_to_dict(p, project_path=project_path) for p in pipelines]
        except Exception:
            return []

    def _project_path_from_url(self, web_url):
        # extract project path from pipeline web_url like
        # https://gitlab.example.com/group/subgroup/project/-/pipelines/123
        try:
            base = self.config.gitlab_url.rstrip('/')
            path = web_url.replace(base, '').strip('/')
            # path: group/subgroup/project/-/pipelines/123
            idx = path.find('/-/')
            if idx > 0:
                return path[:idx]
        except Exception:
            pass
        return None

    def get_pipeline_bridges(self, pipeline_id):
        try:
            pipeline = self.project.pipelines.get(pipeline_id)
            bridges = pipeline.bridges.list(all=True)
            results = []
            for b in bridges:
                dp = getattr(b, 'downstream_pipeline', None)
                if not dp:
                    continue
                web_url = dp.get('web_url', '')
                ds_project_path = self._project_path_from_url(web_url)
                is_same_project = (ds_project_path == self.project_name) if ds_project_path else True
                results.append({
                    'id': dp.get('id'),
                    'status': dp.get('status', 'unknown'),
                    'ref': dp.get('ref', ''),
                    'sha': (dp.get('sha') or '')[:8],
                    'created_at': dp.get('created_at', ''),
                    'updated_at': dp.get('updated_at', ''),
                    'web_url': web_url,
                    'user': 'unknown',
                    '_ds_project_path': ds_project_path if not is_same_project else None,
                    '_is_downstream': True,
                    '_parent_id': pipeline_id,
                    '_bridge_name': b.name,
                    '_bridge_id': getattr(b, 'id', None),
                    '_bridge_status': getattr(b, 'status', None),
                    '_bridge_stage': getattr(b, 'stage', None),
                    '_bridge_duration': getattr(b, 'duration', None),
                    '_bridge_started_at': getattr(b, 'started_at', None),
                    '_bridge_finished_at': getattr(b, 'finished_at', None),
                    '_bridge_web_url': getattr(b, 'web_url', None),
                })
            return results
        except Exception:
            return []

    def get_pipeline_detail(self, pipeline_id):
        try:
            p = self.project.pipelines.get(pipeline_id)
            user = getattr(p, 'user', None) or {}
            return {
                'duration': getattr(p, 'duration', None),
                'queued_duration': getattr(p, 'queued_duration', None),
                'started_at': getattr(p, 'started_at', None),
                'finished_at': getattr(p, 'finished_at', None),
                'source': getattr(p, 'source', None),
                'coverage': getattr(p, 'coverage', None),
                'user': user.get('username') if isinstance(user, dict) else getattr(user, 'username', None),
            }
        except Exception:
            return {}

    def cancel_pipeline(self, pipeline_id):
        pipeline = self.project.pipelines.get(pipeline_id)
        pipeline.cancel()
        return pipeline.status

    def get_pipeline_jobs(self, pipeline_id):
        try:
            pipeline = self.project.pipelines.get(pipeline_id)
            jobs = pipeline.jobs.list(all=True)
            return [{
                'id': job.id,
                'name': job.name,
                'status': job.status,
                'stage': job.stage,
                'duration': job.duration,
                'started_at': job.started_at,
                'finished_at': job.finished_at,
                'web_url': job.web_url,
            } for job in jobs]
        except Exception:
            return []

    def get_my_activity_counts(self, days=ACTIVITY_WINDOW_DAYS, force=False):
        # cache check
        now = time.time()
        if not force and self._activity_cache is not None:
            ts, cached = self._activity_cache
            if now - ts < ACTIVITY_TTL:
                return cached
        try:
            since = datetime.now(timezone.utc) - timedelta(days=days - 1)
            since_iso = since.strftime('%Y-%m-%d')
            events = self.gl.events.list(
                after=since_iso,
                per_page=100,
                iterator=True,
            )
            counts_by_date = {}
            seen = 0
            cap = ACTIVITY_PAGE_CAP * 100
            for ev in events:
                seen += 1
                if seen > cap:
                    break
                created = getattr(ev, 'created_at', None)
                if not created:
                    continue
                try:
                    dt = datetime.fromisoformat(created.replace('Z', '+00:00'))
                except Exception:
                    continue
                local_date = dt.astimezone().date()
                counts_by_date[local_date] = counts_by_date.get(local_date, 0) + 1
            today = datetime.now().astimezone().date()
            result = []
            for offset in range(days):
                d = today - timedelta(days=offset)
                result.append({
                    'date': d.isoformat(),
                    'day_offset': offset,
                    'count': counts_by_date.get(d, 0),
                })
            self._activity_cache = (now, result)
            return result
        except Exception:
            # cache empty result briefly to avoid hammering on persistent failure
            empty = [
                {'date': '', 'day_offset': i, 'count': 0}
                for i in range(days)
            ]
            self._activity_cache = (now, empty)
            return empty

    def get_job(self, job_id):
        try:
            job = self.project.jobs.get(job_id)
            return {
                'id': job.id,
                'name': job.name,
                'status': job.status,
                'stage': job.stage,
                'duration': job.duration,
                'started_at': job.started_at,
                'finished_at': job.finished_at,
                'web_url': job.web_url,
            }
        except Exception:
            return None

    def get_job_trace(self, job_id):
        try:
            job = self.project.jobs.get(job_id)
            trace = job.trace()
            if isinstance(trace, bytes):
                trace = trace.decode("utf-8", errors="replace")
            return trace
        except Exception as e:
            return f"Error fetching trace: {e}"

    def get_job_trace_range(self, job_id, offset):
        try:
            project_id = self.project.id
            url = f"{self.gl.api_url}/projects/{project_id}/jobs/{int(job_id)}/trace"
            headers = dict(getattr(self.gl, 'headers', {}) or {})
            headers["Range"] = f"bytes={int(offset)}-"
            if getattr(self.gl, 'private_token', None):
                headers["PRIVATE-TOKEN"] = self.gl.private_token
            elif getattr(self.gl, 'oauth_token', None):
                headers["Authorization"] = f"Bearer {self.gl.oauth_token}"
            elif getattr(self.gl, 'job_token', None):
                headers["JOB-TOKEN"] = self.gl.job_token
            resp = self.gl.session.get(url, headers=headers, timeout=LOG_FETCH_TIMEOUT)
            if resp.status_code == 416:
                return b"", offset
            if resp.status_code not in (200, 206):
                return b"", offset
            total = offset + len(resp.content)
            cr = resp.headers.get("Content-Range") or ""
            if "/" in cr:
                try:
                    total = int(cr.rsplit("/", 1)[1])
                except ValueError:
                    pass
            return resp.content, total
        except Exception:
            return b"", offset

    def get_job_failures(self, job_id):
        trace = self.get_job_trace(job_id)
        failures = []
        summary_pattern = re.compile(
            r"=+\s*short test summary info\s*=+\n(.*?)(?=^=+|\Z)",
            re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )
        summary_match = summary_pattern.search(trace)
        if summary_match:
            for line in summary_match.group(1).strip().split('\n'):
                if 'FAILED' in line:
                    failures.append(line.strip())
        if not failures:
            for line in trace.split('\n'):
                if any(kw in line.lower() for kw in ['error:', 'failed:', 'exception:']):
                    failures.append(line.strip())
                    if len(failures) > 20:
                        break
        return failures

    # -- merge requests ------------------------------------------------------

    @staticmethod
    def _mr_project_path_from_refs(mr):
        # `references.full` looks like "group/sub/project!123"
        try:
            refs = getattr(mr, 'references', None) or {}
            full = refs.get('full') if isinstance(refs, dict) else None
            if full and '!' in full:
                return full.split('!', 1)[0]
        except Exception:
            pass
        return None

    @staticmethod
    def _mr_to_dict(mr, project_path=None):
        author = getattr(mr, 'author', None) or {}
        head_pipeline = getattr(mr, 'head_pipeline', None) or {}
        path = project_path or GitLabAPI._mr_project_path_from_refs(mr)
        return {
            'id': mr.id,
            'iid': mr.iid,
            'project_id': mr.project_id,
            'project_path': path or '',
            'title': mr.title,
            'description': getattr(mr, 'description', '') or '',
            'state': mr.state,
            'draft': bool(getattr(mr, 'draft', False) or getattr(mr, 'work_in_progress', False)),
            'merge_status': getattr(mr, 'merge_status', None) or getattr(mr, 'detailed_merge_status', None),
            'source_branch': mr.source_branch,
            'target_branch': mr.target_branch,
            'created_at': mr.created_at,
            'updated_at': mr.updated_at,
            'merged_at': getattr(mr, 'merged_at', None),
            'closed_at': getattr(mr, 'closed_at', None),
            'author': author.get('username') if isinstance(author, dict) else getattr(author, 'username', 'unknown'),
            'web_url': mr.web_url,
            'user_notes_count': getattr(mr, 'user_notes_count', 0) or 0,
            'upvotes': getattr(mr, 'upvotes', 0) or 0,
            'downvotes': getattr(mr, 'downvotes', 0) or 0,
            'head_pipeline_status': head_pipeline.get('status') if isinstance(head_pipeline, dict) else getattr(head_pipeline, 'status', None),
            'head_pipeline_id': head_pipeline.get('id') if isinstance(head_pipeline, dict) else getattr(head_pipeline, 'id', None),
            'head_pipeline_web_url': head_pipeline.get('web_url') if isinstance(head_pipeline, dict) else getattr(head_pipeline, 'web_url', None),
            'has_conflicts': getattr(mr, 'has_conflicts', False),
            'blocking_discussions_resolved': getattr(mr, 'blocking_discussions_resolved', True),
            'merge_when_pipeline_succeeds': bool(getattr(mr, 'merge_when_pipeline_succeeds', False)),
            'merge_commit_sha': getattr(mr, 'merge_commit_sha', None) or getattr(mr, 'squash_commit_sha', None),
        }

    def get_my_merge_requests(self, state='opened', limit=50, days=None):
        params = dict(
            scope='created_by_me',
            state=state,
            per_page=limit,
            order_by='updated_at',
            sort='desc',
        )
        if days is not None:
            since = datetime.now(timezone.utc) - timedelta(days=days)
            params['updated_after'] = since.isoformat().replace('+00:00', 'Z')
        mrs = self.gl.mergerequests.list(**params)
        return [self._mr_to_dict(mr) for mr in mrs]

    def get_project_merge_requests(self, project_path, state='merged', limit=50):
        project = self.gl.projects.get(project_path)
        mrs = project.mergerequests.list(
            state=state,
            per_page=limit,
            order_by='updated_at',
            sort='desc',
        )
        return [self._mr_to_dict(mr, project_path=project_path) for mr in mrs]

    def get_merge_request(self, project_path, iid):
        try:
            project = self.gl.projects.get(project_path)
            mr = project.mergerequests.get(iid)
            data = self._mr_to_dict(mr, project_path=project_path)
            # diff stats
            try:
                changes = mr.changes()
                data['changes_count'] = len(changes.get('changes', [])) if isinstance(changes, dict) else 0
            except Exception:
                data['changes_count'] = 0
            # commits count
            try:
                commits = mr.commits()
                data['commits_count'] = sum(1 for _ in commits)
            except Exception:
                data['commits_count'] = 0
            # approvals
            try:
                approvals = mr.approvals.get()
                approved_by = getattr(approvals, 'approved_by', None) or []
                data['approvals_count'] = len(approved_by)
                data['approved_by'] = [
                    (a.get('user') or {}).get('username', '') if isinstance(a, dict) else ''
                    for a in approved_by
                ]
                data['approvals_required'] = getattr(approvals, 'approvals_required', 0) or 0
            except Exception:
                data['approvals_count'] = 0
                data['approved_by'] = []
                data['approvals_required'] = 0
            return data
        except Exception:
            return None

    def get_mr_approval_state(self, project_path, iid):
        try:
            project = self.gl.projects.get(project_path)
            mr = project.mergerequests.get(iid)
            state = mr.approval_state.get()
            rules_raw = getattr(state, 'rules', None) or []
            rules = []
            for r in rules_raw:
                d = r if isinstance(r, dict) else (r.attributes if hasattr(r, 'attributes') else dict(r))
                approved_by = d.get('approved_by') or []
                approved_names = [
                    (a.get('username') if isinstance(a, dict) else getattr(a, 'username', '')) or ''
                    for a in approved_by
                ]
                rules.append({
                    'id': d.get('id'),
                    'name': d.get('name') or '',
                    'rule_type': d.get('rule_type') or '',
                    'approvals_required': d.get('approvals_required', 0) or 0,
                    'approved': bool(d.get('approved', False)),
                    'approved_by': [n for n in approved_names if n],
                })
            return rules
        except Exception:
            return []

    def get_mr_pipelines(self, project_path, iid):
        try:
            project = self.gl.projects.get(project_path)
            mr = project.mergerequests.get(iid)
            pipelines = mr.pipelines.list(all=False, per_page=50)
            results = []
            for p in pipelines:
                pd = p.attributes if hasattr(p, 'attributes') else {}
                results.append({
                    'id': pd.get('id') or getattr(p, 'id', None),
                    'status': pd.get('status') or getattr(p, 'status', 'unknown'),
                    'ref': pd.get('ref') or getattr(p, 'ref', ''),
                    'sha': (pd.get('sha') or getattr(p, 'sha', '') or '')[:8],
                    'created_at': pd.get('created_at') or getattr(p, 'created_at', ''),
                    'updated_at': pd.get('updated_at') or getattr(p, 'updated_at', ''),
                    'web_url': pd.get('web_url') or getattr(p, 'web_url', ''),
                    'user': 'unknown',
                })
            return results
        except Exception:
            return []

    @staticmethod
    def _discussion_unresolved(disc_dict):
        notes = disc_dict.get('notes', []) if isinstance(disc_dict, dict) else []
        return any(n.get('resolvable') and not n.get('resolved') for n in notes if isinstance(n, dict))

    def get_mr_commits(self, project_path, iid):
        try:
            project = self.gl.projects.get(project_path)
            mr = project.mergerequests.get(iid)
            commits = mr.commits()
            results = []
            for c in commits:
                cd = c.attributes if hasattr(c, 'attributes') else {}
                sha = cd.get('id') or getattr(c, 'id', '') or ''
                results.append({
                    'sha': sha,
                    'short_sha': (sha or '')[:8],
                    'title': cd.get('title') or getattr(c, 'title', '') or '',
                    'author_name': cd.get('author_name') or getattr(c, 'author_name', '') or '',
                    'created_at': cd.get('created_at') or getattr(c, 'created_at', '') or '',
                    'web_url': cd.get('web_url') or getattr(c, 'web_url', '') or '',
                    'pipeline_status': '',
                })
            return results
        except Exception:
            return []

    def get_commit_pipeline_status(self, project_path, sha):
        if not sha:
            return ''
        try:
            project = self.gl.projects.get(project_path)
            commit = project.commits.get(sha)
            lp = getattr(commit, 'last_pipeline', None)
            if isinstance(lp, dict):
                return lp.get('status', '') or ''
            if lp is not None:
                return getattr(lp, 'status', '') or ''
            return ''
        except Exception:
            return ''

    def get_mr_discussions(self, project_path, iid):
        try:
            project = self.gl.projects.get(project_path)
            mr = project.mergerequests.get(iid)
            discussions = mr.discussions.list(all=True)
            results = []
            for d in discussions:
                dd = d.attributes if hasattr(d, 'attributes') else dict(d)
                notes = dd.get('notes', []) or []
                if not notes:
                    continue
                first = notes[0]
                results.append({
                    'id': dd.get('id'),
                    'unresolved': self._discussion_unresolved(dd),
                    'resolvable': any(n.get('resolvable') for n in notes),
                    'notes': [
                        {
                            'id': n.get('id'),
                            'author': (n.get('author') or {}).get('username', 'unknown'),
                            'body': n.get('body') or '',
                            'created_at': n.get('created_at', ''),
                            'system': bool(n.get('system')),
                            'resolved': bool(n.get('resolved')),
                            'resolvable': bool(n.get('resolvable')),
                        }
                        for n in notes
                    ],
                    'first_author': (first.get('author') or {}).get('username', 'unknown'),
                    'first_created_at': first.get('created_at', ''),
                    'system': bool(first.get('system')),
                })
            return results
        except Exception:
            return []

    def get_mr_unresolved_count(self, project_path, iid):
        discussions = self.get_mr_discussions(project_path, iid)
        return sum(1 for d in discussions if d.get('unresolved'))

    def get_mr_approvals_summary(self, project_path, iid):
        try:
            project = self.gl.projects.get(project_path)
            mr = project.mergerequests.get(iid)
            approvals = mr.approvals.get()
            approved_by = getattr(approvals, 'approved_by', None) or []
            return {
                'approvals_count': len(approved_by),
                'approvals_required': getattr(approvals, 'approvals_required', 0) or 0,
            }
        except Exception:
            return None

    def approve_merge_request(self, project_path, iid):
        project = self.gl.projects.get(project_path)
        mr = project.mergerequests.get(iid)
        mr.approve()
        return True

    def close_merge_request(self, project_path, iid):
        project = self.gl.projects.get(project_path)
        mr = project.mergerequests.get(iid)
        mr.state_event = 'close'
        mr.save()
        return True

    def set_merge_when_pipeline_succeeds(self, project_path, iid, enable):
        project = self.gl.projects.get(project_path)
        mr = project.mergerequests.get(iid)
        if not enable:
            mr.cancel_merge_when_pipeline_succeeds()
            return False
        # gitlab parks MWPS only while a pipeline is unfinished; on a finished/absent
        # pipeline mr.merge() falls through to an immediate merge attempt — gate it out
        hp = getattr(mr, 'head_pipeline', None)
        status = hp.get('status') if isinstance(hp, dict) else getattr(hp, 'status', None)
        if not status or status in ('success', 'failed', 'canceled', 'skipped'):
            raise RuntimeError(f"no running pipeline (status: {status or 'none'}) — auto-merge needs an active pipeline")
        mr.merge(merge_when_pipeline_succeeds=True)
        return True

    def merge_mr(self, project_path, iid, squash=True, delete_source_branch=True):
        project = self.gl.projects.get(project_path)
        mr = project.mergerequests.get(iid)
        mr.merge(squash=squash, should_remove_source_branch=delete_source_branch)
        return True

    def commit_has_tag(self, project_path, sha):
        if not sha:
            return False
        project = self.gl.projects.get(project_path)
        refs = project.commits.get(sha).refs('tag')
        if not isinstance(refs, list):
            return bool(refs)
        return any((r.get('type') == 'tag') if isinstance(r, dict) else False for r in refs)

    def create_mr_note(self, project_path, iid, body):
        project = self.gl.projects.get(project_path)
        mr = project.mergerequests.get(iid)
        return mr.notes.create({'body': body})

    def get_default_branch(self, project_path):
        project = self.gl.projects.get(project_path)
        return getattr(project, 'default_branch', '') or ''

    def list_tags(self, project_path, limit=50):
        project = self.gl.projects.get(project_path)
        tags = project.tags.list(per_page=limit, order_by='updated', sort='desc')
        results = []
        for t in tags:
            commit = getattr(t, 'commit', None) or {}
            target = getattr(t, 'target', '') or (commit.get('id') if isinstance(commit, dict) else '') or ''
            created = commit.get('created_at') if isinstance(commit, dict) else ''
            results.append({
                'name': getattr(t, 'name', '') or '',
                'target': target,
                'short_sha': target[:8],
                'created_at': created or '',
                'message': (getattr(t, 'message', '') or '').strip(),
                'pipeline_status': '',
            })
        return results

    def create_tag(self, project_path, name, ref=None, message=''):
        project = self.gl.projects.get(project_path)
        ref = ref or getattr(project, 'default_branch', '') or 'HEAD'
        payload = {'tag_name': name, 'ref': ref}
        if message:
            payload['message'] = message
        project.tags.create(payload)
        return True

    def list_commits(self, project_path, ref=None, since_days=None, limit=50):
        project = self.gl.projects.get(project_path)
        ref = ref or getattr(project, 'default_branch', '') or ''
        params = {'per_page': limit}
        if ref:
            params['ref_name'] = ref
        if since_days is not None:
            since = datetime.now(timezone.utc) - timedelta(days=since_days)
            params['since'] = since.isoformat().replace('+00:00', 'Z')
        commits = project.commits.list(**params)
        results = []
        for c in commits:
            msg = (getattr(c, 'message', '') or '').strip()
            cid = getattr(c, 'id', '') or ''
            results.append({
                'id': cid,
                'short_id': getattr(c, 'short_id', '') or cid[:8],
                'title': getattr(c, 'title', '') or (msg.splitlines()[0] if msg else ''),
                'message': msg,
                'author_name': getattr(c, 'author_name', '') or '',
                'authored_date': getattr(c, 'authored_date', '') or getattr(c, 'created_at', '') or '',
                'web_url': getattr(c, 'web_url', '') or '',
                'pipeline_status': '',
            })
        return results

    def get_commit(self, project_path, sha):
        project = self.gl.projects.get(project_path)
        c = project.commits.get(sha)
        msg = (getattr(c, 'message', '') or '').strip()
        stats = getattr(c, 'stats', None)
        stats = stats if isinstance(stats, dict) else {}
        lp = getattr(c, 'last_pipeline', None)
        if isinstance(lp, dict):
            pstatus = lp.get('status', '') or ''
        elif lp is not None:
            pstatus = getattr(lp, 'status', '') or ''
        else:
            pstatus = ''
        files = []
        try:
            for d in c.diff(all=True):
                files.append({
                    'path': d.get('new_path') or d.get('old_path') or '',
                    'new_file': bool(d.get('new_file')),
                    'deleted_file': bool(d.get('deleted_file')),
                    'renamed_file': bool(d.get('renamed_file')),
                })
        except Exception:
            pass
        cid = getattr(c, 'id', '') or ''
        return {
            'id': cid,
            'short_id': getattr(c, 'short_id', '') or cid[:8],
            'title': getattr(c, 'title', '') or '',
            'message': msg,
            'author_name': getattr(c, 'author_name', '') or '',
            'author_email': getattr(c, 'author_email', '') or '',
            'authored_date': getattr(c, 'authored_date', '') or '',
            'committed_date': getattr(c, 'committed_date', '') or '',
            'web_url': getattr(c, 'web_url', '') or '',
            'pipeline_status': pstatus,
            'stats': {
                'additions': stats.get('additions', 0),
                'deletions': stats.get('deletions', 0),
                'total': stats.get('total', 0),
            },
            'files': files,
        }

    def get_commit_pipeline(self, project_path, sha):
        if not sha:
            return None
        project = self.gl.projects.get(project_path)
        lp = getattr(project.commits.get(sha), 'last_pipeline', None)
        if not lp:
            return None
        if isinstance(lp, dict):
            d = lp
        else:
            d = {k: getattr(lp, k, None) for k in ('id', 'status', 'ref', 'web_url', 'sha')}
        return {
            'id': d.get('id'),
            'status': d.get('status', 'unknown') or 'unknown',
            'ref': d.get('ref', '') or '',
            'sha': (d.get('sha') or sha)[:40],
            'web_url': d.get('web_url', '') or '',
            'project_path': project_path,
        }
