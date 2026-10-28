import asyncio
import tempfile
from pathlib import Path

from textual.app import App

from gitlab_monitor import tui
from gitlab_monitor.config import OpenTabs


class _Recents:
    def remember(self, *a):
        pass


class _ReviewFiles:
    def get(self, *a):
        return None


class _Config:
    gitlab_url = "https://gitlab.example.com"
    recent_projects = _Recents()
    review_files = _ReviewFiles()

    def __init__(self):
        self.tabs = OpenTabs(Path(tempfile.mkdtemp()))


class _Api:
    project_name = "g/p"

    def __init__(self):
        self.config = _Config()

    def get_merge_request(self, project_path, iid):
        return {
            "iid": iid, "id": iid, "title": f"title {iid}", "author": "me", "assignees": [], "state": "merged",
            "source_branch": "a", "target_branch": "main", "created_at": "2026-10-28T12:55:31Z",
            "merged_at": "2026-10-28T13:08:43Z", "head_pipeline_status": None, "description": "d",
            "web_url": f"https://gitlab.example.com/g/p/-/merge_requests/{iid}",
        }

    def get_mr_discussions(self, project_path, iid):
        return []

    def get_mr_approval_state(self, project_path, iid):
        return []

    def get_pipeline_jobs(self, pid):
        return []

    def get_pipeline_detail(self, pid):
        return {}

    def get_pipeline_bridges(self, pid):
        return []


class _App(App):
    CSS = tui.PipelineMonitor.CSS


async def _settle(app, pilot):
    await pilot.pause(0.1)
    for _ in range(40):
        if getattr(app.screen, "mr", None) is not None:
            return
        await pilot.pause(0.05)


def _spans(app):
    return [e["key"] for _, _, e in app.screen.query_one("#tabstrip", tui.TabStrip)._spans]


def test_store_caps_at_five_keeps_order_and_roundtrips():
    d = Path(tempfile.mkdtemp())
    tabs = OpenTabs(d)
    for i in range(1, 7):
        tabs.open("mr", {"key": f"g/p:{i}", "label": f"!{i}", "project": "g/p", "iid": i})
    assert [t["iid"] for t in tabs.list("mr")] == [2, 3, 4, 5, 6]
    assert tabs.active("mr") == "g/p:6"
    tabs.open("mr", {"key": "g/p:3", "label": "!3 new"})
    assert [t["iid"] for t in tabs.list("mr")] == [2, 3, 4, 5, 6]
    assert tabs.list("mr")[1]["label"] == "!3 new"
    assert tabs.active("mr") == "g/p:3"
    tabs.close("mr", "g/p:3")
    again = OpenTabs(d)
    assert [t["iid"] for t in again.list("mr")] == [2, 4, 5, 6]
    assert again.active("mr") is None
    assert again.list("pipeline") == []


def test_mr_detail_registers_cycles_closes_and_refreshes_on_resume():
    api = _Api()

    async def go():
        app = _App()
        async with app.run_test(size=(200, 50)) as pilot:
            app.push_screen(tui.MergeRequestDetailScreen(api, "g/p", 1))
            await _settle(app, pilot)
            app.push_screen(tui.MergeRequestDetailScreen(api, "g/p", 2))
            await _settle(app, pilot)
            out = {"labels": [t["label"] for t in api.config.tabs.list("mr")]}
            await pilot.press("q")
            await _settle(app, pilot)
            out["resumed"] = (app.screen.iid, _spans(app))
            app.push_screen(tui.MergeRequestDetailScreen(api, "g/p", 2))
            await _settle(app, pilot)
            depth = len(app.screen_stack)
            await pilot.press("right_square_bracket")
            await _settle(app, pilot)
            out["next"] = (app.screen.iid, len(app.screen_stack) - depth)
            await pilot.press("left_square_bracket")
            await _settle(app, pilot)
            out["prev"] = app.screen.iid
            await pilot.press("X")
            await _settle(app, pilot)
            out["closed"] = (app.screen.iid, len(app.screen_stack) - depth, [t["key"] for t in api.config.tabs.list("mr")])
            await pilot.press("X")
            await pilot.pause(0.2)
            out["last_closed"] = (len(app.screen_stack) - depth, api.config.tabs.list("mr"))
            return out

    out = asyncio.run(go())
    assert out["labels"] == ["!1 title 1", "!2 title 2"]
    assert out["resumed"] == (1, ["g/p:1", "g/p:2"])
    assert out["next"] == (1, 0)
    assert out["prev"] == 2
    assert out["closed"] == (1, 0, ["g/p:1"])
    assert out["last_closed"] == (-1, [])


def test_click_on_strip_opens_that_tab():
    api = _Api()

    async def go():
        app = _App()
        async with app.run_test(size=(200, 50)) as pilot:
            app.push_screen(tui.MergeRequestDetailScreen(api, "g/p", 1))
            await _settle(app, pilot)
            app.push_screen(tui.MergeRequestDetailScreen(api, "g/p", 2))
            await _settle(app, pilot)
            strip = app.screen.query_one("#tabstrip", tui.TabStrip)
            start = strip._spans[0][0] + strip.content_region.x - strip.region.x
            await pilot.click("#tabstrip", offset=(start + 1, 0))
            await _settle(app, pilot)
            return app.screen.iid, api.config.tabs.active("mr")

    assert asyncio.run(go()) == (1, "g/p:1")


def test_pipeline_tab_strips_private_keys_and_single_tab_cycle_is_noop():
    api = _Api()
    pipeline = {"id": 7, "status": "success", "ref": "main", "_is_downstream": True, "_ds_project_path": None}

    async def go():
        app = _App()
        async with app.run_test(size=(200, 50)) as pilot:
            screen = tui.JobListScreen(api, pipeline)
            app.push_screen(screen)
            await pilot.pause(0.3)
            depth = len(app.screen_stack)
            await pilot.press("right_square_bracket")
            await pilot.pause(0.2)
            return api.config.tabs.list("pipeline"), app.screen is screen, len(app.screen_stack) - depth

    tabs, same, delta = asyncio.run(go())
    assert tabs == [{
        "key": "g/p:7", "label": "#7 main", "project": "g/p",
        "pipeline": {"id": 7, "status": "success", "ref": "main"},
    }]
    assert same and delta == 0
