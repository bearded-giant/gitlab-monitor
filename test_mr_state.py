import asyncio
import tempfile
from pathlib import Path

from textual.app import App

from gitlab_monitor.api import GitLabAPI
from gitlab_monitor.config import OpenTabs
from gitlab_monitor.tui import (
    MyMergeRequestsScreen,
    ProjectMergeRequestsScreen,
    _next_mr_state,
)


class _Notes:
    def has(self, *a):
        return False


class _Config:
    gitlab_url = "https://gitlab.example.com"
    gitlab_token = "x"
    mr_notes = _Notes()
    tabs = OpenTabs(Path(tempfile.mkdtemp()))

    def save_last_view(self, *a, **kw):
        pass


def _mr(proj, iid, state="opened"):
    return {
        "project_path": proj,
        "iid": iid,
        "title": f"mr {iid}",
        "draft": False,
        "state": state,
        "author": "me",
        "source_branch": f"b{iid}",
        "target_branch": "main",
        "created_at": "2026-10-20T00:00:00Z",
        "updated_at": "2026-10-20T00:00:00Z",
        "head_pipeline_status": None,
        "head_pipeline_id": None,
    }


class _MrApi:
    config = _Config()
    pipelines = {
        ("g/a", 1): "failed",
        ("g/a", 2): "success",
        ("g/b", 3): "failed",
        ("g/b", 4): "running",
    }

    def __init__(self):
        self.states = []
        self.pipeline_calls = []

    def _mrs(self, proj=None):
        return [_mr(p, i) for (p, i) in self.pipelines if proj in (None, p)]

    def get_my_merge_requests(self, state="opened", limit=50, days=None):
        self.states.append(state)
        return self._mrs()

    def get_project_merge_requests(self, project_path, state="merged", limit=50):
        self.states.append(state)
        return self._mrs(project_path)

    def get_mr_head_pipelines(self, project_path, iids):
        self.pipeline_calls.append((project_path, sorted(iids)))
        return {
            i: {"status": self.pipelines[(project_path, i)], "id": i * 100, "web_url": None}
            for i in iids
        }

    def get_mr_unresolved_count(self, *a):
        return 0

    def get_mr_approvals_summary(self, *a):
        return {"approvals_count": 0, "approvals_required": 0}


def _run(screen, state):
    async def run():
        app = App()
        async with app.run_test() as pilot:
            await app.push_screen(screen)
            for _ in range(10):
                await pilot.pause(0.03)
            screen.api.states.clear()
            screen.api.pipeline_calls.clear()
            await screen._set_state(state)
            return (
                screen.api.states,
                sorted(screen.api.pipeline_calls),
                sorted((m["project_path"], m["iid"]) for m in screen.mrs),
                dict(screen._keys()[1] + screen._keys()[2]),
                screen._status_text().plain,
            )

    return asyncio.run(run())


def test_state_cycle_order():
    seen = ["opened"]
    for _ in range(4):
        seen.append(_next_mr_state(seen[-1]))
    assert seen == ["opened", "merged", "closed", "failed", "opened"]
    assert _next_mr_state("all") == "opened"


def test_my_mrs_failed_view_filters_on_head_pipeline():
    api = _MrApi()
    states, calls, shown, keys, footer = _run(MyMergeRequestsScreen(api), "failed")
    assert states == ["opened"]
    # one GraphQL lookup per project, not per MR
    assert calls == [("g/a", [1, 2]), ("g/b", [3, 4])]
    assert shown == [("g/a", 1), ("g/b", 3)]
    assert keys["s"] == "show open"
    assert keys["a"] == "show all"
    assert "state failed" in footer.replace(":", "")


def test_my_mrs_defaults_open_and_header_shows_next():
    api = _MrApi()
    screen = MyMergeRequestsScreen(api)
    assert screen.state == "opened"
    states, _, shown, keys, footer = _run(screen, "opened")
    assert states == ["opened"]
    assert len(shown) == 4
    assert keys["s"] == "show merged"
    assert "state open" in footer.replace(":", "")


def test_all_toggle_is_separate_from_cycle():
    api = _MrApi()
    states, _, _, keys, footer = _run(MyMergeRequestsScreen(api), "all")
    assert states == ["all"]
    assert keys["a"] == "show open"
    assert keys["s"] == "show open"
    assert "state all" in footer.replace(":", "")


def test_project_mrs_default_open_and_failed_view():
    api = _MrApi()
    screen = ProjectMergeRequestsScreen(api, "g/b")
    assert screen.state == "opened"
    states, calls, shown, keys, _ = _run(screen, "failed")
    assert states == ["opened"]
    assert calls == [("g/b", [3, 4])]
    assert shown == [("g/b", 3)]
    assert keys["s"] == "show open"


def test_get_mr_head_pipelines_parses_graphql():
    api = GitLabAPI(_Config())
    seen = {}

    def fake_graphql(query, variables):
        seen.update(variables)
        return {"project": {"mergeRequests": {"nodes": [
            {"iid": "7", "headPipeline": {"id": "gid://gitlab/Ci::Pipeline/123", "status": "FAILED", "path": "/g/a/-/pipelines/123"}},
            {"iid": "8", "headPipeline": None},
        ]}}}

    api._graphql = fake_graphql
    out = api.get_mr_head_pipelines("g/a", [7, 8])
    assert seen == {"p": "g/a", "i": ["7", "8"]}
    assert out == {7: {"status": "failed", "id": 123, "web_url": "https://gitlab.example.com/g/a/-/pipelines/123"}}
