import asyncio

from textual.app import App
from textual.widgets import RichLog

from gitlab_monitor.tui import JobDetailScreen, TagListScreen


class _Config:
    gitlab_url = "https://gitlab.example.com"
    gitlab_token = "x"

    def save_last_view(self, *a, **kw):
        pass


class _GhJobApi:
    config = _Config()
    project_name = "gh:o/r"

    def __init__(self):
        self.trace_calls = 0
        self.job = {'id': 9, 'name': 'test', 'status': 'running', 'stage': '', 'duration': None, 'started_at': '',
                    'finished_at': None, 'web_url': '', 'pipeline_id': 1,
                    'steps': [{'number': 1, 'name': 'Set up job', 'status': 'success'},
                              {'number': 2, 'name': 'Run tests', 'status': 'running'}]}

    def supports(self, action, key=None):
        return action not in ('job_trace_live', 'revert_tag_commit')

    def host_label(self, key=None):
        return ('GitHub', 'github.com')

    def get_job(self, job_id):
        return dict(self.job)

    def get_job_trace_range(self, job_id, offset):
        self.trace_calls += 1
        return b'line one\nline two\n', 18

    def get_job_failures(self, job_id):
        return []

    def list_tags(self, project_path, limit=50):
        return []

    def get_default_branch(self, project_path):
        return 'main'


def _lines(screen):
    log = screen.query_one("#job-log", RichLog)
    return [str(getattr(line, 'text', line)) for line in log.lines]


def test_running_github_job_shows_steps_then_log_when_done():
    async def run():
        api = _GhJobApi()
        app = App()
        async with app.run_test() as pilot:
            screen = JobDetailScreen(api, dict(api.job))
            await app.push_screen(screen)
            for _ in range(10):
                await pilot.pause(0.03)
            running_text = "\n".join(_lines(screen))
            calls_while_running = api.trace_calls
            api.job['status'] = 'success'
            await screen.action_refresh()
            done_text = "\n".join(_lines(screen))
        return running_text, calls_while_running, done_text, api.trace_calls

    running_text, calls_while_running, done_text, calls_done = asyncio.run(run())
    assert 'Run tests' in running_text and 'STEPS' in running_text
    assert calls_while_running == 0
    assert 'line one' in done_text and 'STEPS' not in done_text and calls_done == 1


def test_tag_screen_hides_revert_without_endpoint():
    async def run():
        app = App()
        async with app.run_test():
            screen = TagListScreen(_GhJobApi(), "gh:o/r")
            await app.push_screen(screen)
            keys = [k for row in screen._keys() for k, _ in row]
            await screen.action_revert_commit()
        return keys

    keys = asyncio.run(run())
    assert 'R' in keys and 'V' not in keys
