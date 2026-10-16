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


def test_tag_auto_refresh_skips_settled_pipelines():
    async def run():
        api = _TagApi()
        app = App()
        async with app.run_test() as pilot:
            screen = TagListScreen(api, "group/project")
            await app.push_screen(screen)
            await _settle(pilot)
            initial = sorted(api.status_calls)

            api.status_calls.clear()
            await screen.load_tags(full=False)
            auto = sorted(api.status_calls)
            shown = {t["target"]: t["pipeline_status"] for t in screen.tags}

            api.status_calls.clear()
            await screen.load_tags()
            manual = sorted(api.status_calls)
        return initial, auto, shown, manual

    initial, auto, shown, manual = asyncio.run(run())
    assert initial == ["sha1", "sha2", "sha3"]
    # success is settled; running and manual can still change
    assert auto == ["sha2", "sha3"]
    assert shown == {"sha1": "success", "sha2": "running", "sha3": "manual"}
    assert manual == ["sha1", "sha2", "sha3"]


def test_buried_screen_stops_polling():
    async def run():
        app = App()
        async with app.run_test() as pilot:
            ticker = _Ticker()
            await app.push_screen(ticker)
            await _settle(pilot)
            assert ticker.ticks > 0

            await app.push_screen(Screen())
            await _settle(pilot, 2)
            buried = ticker.ticks
            await _settle(pilot)
            still_buried = ticker.ticks

            app.pop_screen()
            await _settle(pilot)
            return buried, still_buried, ticker.ticks

    buried, still_buried, resumed = asyncio.run(run())
    assert still_buried == buried
    assert resumed > buried


def test_idle_pauses_polling_until_input(monkeypatch):
    async def noop_mount(self):
        pass

    monkeypatch.setattr(PipelineMonitor, "on_mount", noop_mount)

    async def run():
        app = PipelineMonitor(_Config())
        async with app.run_test() as pilot:
            ticker = _Ticker()
            await app.push_screen(ticker)
            await _settle(pilot)

            app._last_input -= tui.IDLE_PAUSE_MINUTES * 60 + 1
            app._check_idle()
            await _settle(pilot, 2)
            paused = ticker.ticks
            await _settle(pilot)
            still_paused = ticker.ticks

            await pilot.press("z")
            await _settle(pilot)
            return paused, still_paused, ticker.ticks, app._idle_paused

    paused, still_paused, resumed, idle_flag = asyncio.run(run())
    assert still_paused == paused
    assert resumed > paused
    assert idle_flag is False


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
