import asyncio

from textual.app import App

from gitlab_monitor.tui import PipelineListScreen


class _Recent:
    def remember(self, name):
        pass


class _Config:
    gitlab_url = "https://gitlab.example.com"
    recent_projects = _Recent()

    def save_last_view(self, *a, **kw):
        pass


def _pipe(pid, status, **extra):
    return {
        "id": pid,
        "status": status,
        "ref": "main",
        "sha": "abc12345",
        "created_at": "",
        "user": "dev",
        "web_url": f"https://gitlab.example.com/p/{pid}",
        **extra,
    }


class _Api:
    config = _Config()
    project_name = "group/project"

    def get_recent_pipelines(self, limit=50, ref=None, username=None, days=None):
        return [
            _pipe(1, "running"),
            _pipe(2, "success"),
            _pipe(3, "success"),
            _pipe(4, "failed"),
            _pipe(5, "manual"),
        ]

    def get_pipeline_bridges(self, pid):
        child = {2: "running", 3: "success"}.get(pid)
        if child is None:
            return []
        return [
            _pipe(
                pid * 10 + 1,
                child,
                _is_downstream=True,
                _parent_id=pid,
                _bridge_name="deploy",
            )
        ]


def _ids(screen):
    return [p["id"] for p in screen.filtered_pipelines]


def test_pipeline_list_defaults_to_active_and_toggles():
    async def run():
        app = App()
        async with app.run_test() as pilot:
            screen = PipelineListScreen(_Api())
            await app.push_screen(screen)
            for _ in range(20):
                await pilot.pause(0.05)
            seen = {"default": _ids(screen)}
            await pilot.press("a")
            await pilot.pause(0.05)
            seen["all"] = _ids(screen)
            await pilot.press("a")
            await pilot.pause(0.05)
            seen["back"] = _ids(screen)
            await pilot.press("f")
            await pilot.pause(0.05)
            seen["failed"] = _ids(screen)
            return seen

    seen = asyncio.run(run())
    # finished parent with a running child stays, child stays under it
    assert seen["default"] == [1, 2, 21]
    assert seen["all"] == [1, 2, 21, 3, 31, 4, 5]
    assert seen["back"] == [1, 2, 21]
    # status filter overrides active-only so failed pipelines still show
    assert seen["failed"] == [4]


def test_pipeline_list_active_off_shows_all():
    async def run():
        app = App()
        async with app.run_test() as pilot:
            screen = PipelineListScreen(
                _Api(), initial_filter="main", active_only=False
            )
            await app.push_screen(screen)
            for _ in range(20):
                await pilot.pause(0.05)
            return _ids(screen)

    assert asyncio.run(run()) == [1, 2, 21, 3, 31, 4, 5]


if __name__ == "__main__":
    test_pipeline_list_defaults_to_active_and_toggles()
    test_pipeline_list_active_off_shows_all()
