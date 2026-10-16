import asyncio

from textual.app import App
from textual.screen import Screen

from gitlab_monitor import tui
from gitlab_monitor.tui import PipelineMonitor, ScreenBase, TagListScreen


class _Config:
    gitlab_url = "https://gitlab.example.com"
    gitlab_token = "x"

    def save_last_view(self, *a, **kw):
        pass


class _TagApi:
    config = _Config()
    statuses = {"sha1": "success", "sha2": "running", "sha3": "manual"}

    def __init__(self):
        self.status_calls = []

    def list_tags(self, project_path, limit=50):
        return [
            {
                "name": f"v{i}",
                "target": sha,
                "short_sha": sha,
                "created_at": "",
                "message": "",
                "pipeline_status": "",
            }
            for i, sha in enumerate(self.statuses, 1)
        ]

    def get_default_branch(self, project_path):
        return "main"

    def get_commit_pipeline_status(self, project_path, sha):
        self.status_calls.append(sha)
        return self.statuses[sha]


class _Ticker(ScreenBase):
    def __init__(self):
        super().__init__()
        self.ticks = 0

    def on_mount(self):
        self.set_interval(0.02, self._tick)

    def _tick(self):
        self.ticks += 1


async def _settle(pilot, n=10):
    for _ in range(n):
        await pilot.pause(0.03)


def test_commit_status_lookup_is_one_request():
    from gitlab_monitor.api import GitLabAPI

    api = GitLabAPI(_Config())
    paths = []

    def fake_get(path, **kw):
        paths.append(path)
        return {"id": "abc", "last_pipeline": {"status": "success"}}

    api.gl.http_get = fake_get
    assert api.get_commit_pipeline_status("group/project", "abc") == "success"
    assert paths == ["/projects/group%2Fproject/repository/commits/abc"]
