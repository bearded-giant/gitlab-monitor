import asyncio
import tempfile
from pathlib import Path

from textual.app import App
from textual.screen import Screen

from gitlab_monitor import tui
from gitlab_monitor.config import OpenTabs
from gitlab_monitor.api import GitLabAPI
from gitlab_monitor.constants import MR_DASHBOARD_LISTS
from gitlab_monitor.formatting import _dashboard_badge, _reviewers_text
from gitlab_monitor.tui import MRDashboardScreen, MyMergeRequestsScreen


class _Notes:
    def has(self, *a):
        return False


class _Config:
    gitlab_url = "https://gitlab.example.com"
    gitlab_token = "x"
    mr_notes = _Notes()
    recent_projects = None
    tabs = OpenTabs(Path(tempfile.mkdtemp()))

    def save_last_view(self, *a, **kw):
        pass


def _mr(iid, **kw):
    m = {
        "iid": iid,
        "project_path": "g/proj",
        "title": f"mr {iid}",
        "draft": False,
        "state": "opened",
        "web_url": f"https://gitlab.example.com/g/proj/-/merge_requests/{iid}",
        "source_branch": f"b{iid}",
        "target_branch": "main",
        "created_at": "2026-10-20T00:00:00Z",
        "updated_at": "2026-10-20T00:00:00Z",
        "merged_at": None,
        "author": "me",
        "assignees": ["me"],
        "reviewers": [],
        "review_states": {},
        "approved_by": [],
        "approvals_count": 0,
        "approvals_required": 1,
        "approvals_left": 1,
        "head_pipeline_status": "success",
        "head_pipeline_id": 1,
        "threads_resolved": 0,
        "threads_resolvable": 0,
        "ready": False,
        "merge_when_pipeline_succeeds": False,
    }
    m.update(kw)
    return m


class _DashApi:
    config = _Config()
    dashboard_batches = staticmethod(GitLabAPI.dashboard_batches)

    def __init__(self):
        self.calls = []

    def current_username(self):
        return "me"

    def get_mr_dashboard_lists(self, full, counts=()):
        self.calls.append(([s[0] for s in full], [s[0] for s in counts]))
        out = {s[0]: {"count": 0, "mrs": []} for s in counts}
        for s in full:
            mrs = [_mr(i) for i in range(1, 3)] if s[0] in ("assigned_to_you", "approved_by_you", "merged_recently_assigned") else []
            out[s[0]] = {"count": len(mrs) + (5 if s[0] == "merged_recently_assigned" else 0), "mrs": mrs}
        return out


def _ids(batch):
    return [s[0] for s in batch]


def test_batches_stay_within_three_lists_and_carry_other_tab_counts():
    active = GitLabAPI.dashboard_batches("active")
    assert [len(full) for full, _ in active] == [3, 3, 1]
    assert [_ids(c) for _, c in active] == [[], [], ["merged_recently_reviews", "merged_recently_assigned"]]
    merged = GitLabAPI.dashboard_batches("merged")
    assert [_ids(f) for f, _ in merged] == [["merged_recently_reviews", "merged_recently_assigned"]]
    assert len(merged[0][1]) == 7


def test_dashboard_query_and_node_mapping():
    api = GitLabAPI(_Config())
    seen = []

    def fake_graphql(query, variables):
        seen.append((query, variables))
        node = {
            "iid": "7", "project": {"fullPath": "g/a"}, "title": "t", "draft": False, "state": "opened",
            "webUrl": "u", "sourceBranch": "s", "targetBranch": "main", "createdAt": "c", "updatedAt": "u2", "mergedAt": None,
            "author": {"username": "me"}, "assignees": {"nodes": [{"username": "me"}]},
            "reviewers": {"nodes": [{"username": "rev", "mergeRequestInteraction": {"reviewState": "APPROVED"}}]},
            "approvalsRequired": 2, "approvalsLeft": 1, "approvedBy": {"nodes": [{"username": "rev"}]},
            "headPipeline": {"id": "gid://gitlab/Ci::Pipeline/55", "status": "FAILED", "path": "/g/a/-/pipelines/55"},
            "resolvedDiscussionsCount": 1, "resolvableDiscussionsCount": 2,
            "diffStatsSummary": {"additions": 3, "deletions": 1, "fileCount": 2},
            "autoMergeEnabled": True, "mergeabilityChecks": [{"status": "SUCCESS"}, {"status": "INACTIVE"}],
        }
        return {"currentUser": {
            "assigned_to_you": {"count": 1, "nodes": [node]},
            "merged_recently_reviews": {"count": 43},
        }}

    api._graphql = fake_graphql
    spec = {s[0]: s for s in MR_DASHBOARD_LISTS}
    out = api.get_mr_dashboard_lists([spec["assigned_to_you"]], [spec["merged_recently_reviews"]])
    query, variables = seen[0]
    assert query.startswith("query($mergedAfter: Time) { currentUser {")
    assert "assigned_to_you: authoredMergeRequests(includeAssigned: true, state: opened, reviewerWildcardId: NONE, first: 20)" in query
    assert "merged_recently_reviews: reviewRequestedMergeRequests(state: merged, mergedAfter: $mergedAfter, sort: MERGED_AT_DESC) { count }" in query
    assert variables["mergedAfter"].endswith("Z")
    assert out["merged_recently_reviews"] == {"count": 43, "mrs": []}
    m = out["assigned_to_you"]["mrs"][0]
    assert (m["iid"], m["project_path"], m["head_pipeline_status"], m["head_pipeline_id"]) == (7, "g/a", "failed", 55)
    assert m["head_pipeline_web_url"] == "https://gitlab.example.com/g/a/-/pipelines/55"
    assert (m["approvals_count"], m["approvals_required"], m["approvals_left"]) == (1, 2, 1)
    assert m["review_states"] == {"rev": "APPROVED"}
    assert (m["threads_resolved"], m["threads_resolvable"]) == (1, 2)
    assert m["ready"] is True
    assert m["merge_when_pipeline_succeeds"] is True

    seen.clear()
    api.get_mr_dashboard_lists([spec["reviews_requested"]])
    assert seen[0][0].startswith("query { currentUser {")
    assert seen[0][1] == {}


def test_badges_follow_gitlab_rules():
    me = "me"
    cases = [
        ("returned_to_you", _mr(1, review_states={"a": "REVIEWED", "b": "REQUESTED_CHANGES"}), "Changes requested"),
        ("returned_to_you", _mr(1, review_states={"a": "REVIEWED"}), "Reviewer commented"),
        ("reviews_requested", _mr(1, review_states={"me": "REVIEW_STARTED"}), "Review started"),
        ("reviews_requested", _mr(1, review_states={"me": "UNREVIEWED"}), "Requested"),
        ("assigned_to_you", _mr(1, draft=True), "Draft"),
        ("assigned_to_you", _mr(1, reviewers=["GitLabDuo"]), "Reviewers needed"),
        ("assigned_to_you", _mr(1, reviewers=["a"]), ""),
        ("waiting_for_assignee", _mr(1, review_states={"me": "REQUESTED_CHANGES"}), "You requested changes"),
        ("waiting_for_assignee", _mr(1, review_states={"me": "REVIEWED"}), "You commented"),
        ("waiting_for_approvals", _mr(1, approvals_left=2), "2 approvals required"),
        ("waiting_for_approvals", _mr(1, approvals_left=0, approvals_required=0, review_states={"a": "UNREVIEWED"}), "1 approval required"),
        ("approved_by_you", _mr(1, approvals_left=0), "Approved"),
        ("approved_by_others", _mr(1, approvals_left=None, approvals_required=None), "Waiting for approval"),
        ("assigned_to_you", _mr(1, draft=True, ready=True), "Ready to merge"),
        ("merged_recently_assigned", _mr(1, state="merged"), "Merged"),
    ]
    for list_id, m, want in cases:
        assert _dashboard_badge(list_id, m, me).plain == want, (list_id, want)


def test_reviewers_cell_puts_acted_reviewers_first():
    m = _mr(1, reviewers=["a", "b", "c", "d"], review_states={"a": "UNREVIEWED", "b": "UNREVIEWED", "c": "APPROVED", "d": "REQUESTED_CHANGES"})
    assert _reviewers_text(m).plain == "c✓ d✗ +2"
    assert _reviewers_text(_mr(1)).plain == "—"


def _run(steps):
    async def run():
        app = App()
        async with app.run_test(size=(200, 40)) as pilot:
            api = _DashApi()
            screen = MRDashboardScreen(api)
            await app.push_screen(screen)
            for _ in range(10):
                await pilot.pause(0.03)
            return await steps(app, pilot, screen, api)

    return asyncio.run(run())


def _kinds(screen):
    return [(k, lid) for k, lid, _ in screen._rows]


def test_active_tab_layout_and_request_budget():
    async def steps(app, pilot, screen, api):
        initial = list(api.calls)
        api.calls.clear()
        await screen._safe_refresh()
        return initial, list(api.calls), _kinds(screen), dict(screen._keys()[2]), screen._status_text().plain, dict(screen._info_pairs())

    initial, tick, kinds, keys, footer, info = _run(steps)
    assert len(initial) == 3
    assert len(tick) == 3
    sections = [lid for k, lid in kinds if k == "section"]
    assert sections == [s[0] for s in MR_DASHBOARD_LISTS if s[2] == "active"]
    # group 1 expanded, group 2 collapsed
    assert ("mr", "assigned_to_you") in kinds
    assert ("mr", "approved_by_you") not in kinds
    assert (None, None) in kinds
    assert keys["t"] == "show merged"
    assert "tab: active" in footer
    assert info["Active"] == "2"
    assert info["Merged"] == "0"


class _StubDetail(Screen):
    def __init__(self, api, project_path, iid):
        super().__init__()
        self.target = (project_path, iid)


def test_enter_toggles_section_and_opens_mr(monkeypatch):
    monkeypatch.setattr(tui, "MergeRequestDetailScreen", _StubDetail)

    async def steps(app, pilot, screen, api):
        table = screen.query_one("#mr-table")
        row = next(i for i, (k, lid, _) in enumerate(screen._rows) if k == "section" and lid == "approved_by_you")
        table.move_cursor(row=row)
        await pilot.press("enter")
        await pilot.pause(0.05)
        expanded = ("mr", "approved_by_you") in _kinds(screen)
        mr_row = next(i for i, (k, _, _) in enumerate(screen._rows) if k == "mr")
        table.move_cursor(row=mr_row)
        await pilot.press("enter")
        await pilot.pause(0.05)
        return expanded, getattr(app.screen, "target", None)

    expanded, target = _run(steps)
    assert expanded
    assert target == ("g/proj", 1)


def test_tab_key_cycles_active_merged_list_and_back():
    async def steps(app, pilot, screen, api):
        api.calls.clear()
        await pilot.press("t")
        for _ in range(5):
            await pilot.pause(0.03)
        merged_calls = list(api.calls)
        merged_state = (screen.tab, dict(screen._keys()[2])["t"], _kinds(screen))
        await pilot.press("t")
        for _ in range(5):
            await pilot.pause(0.03)
        listed = app.screen
        list_key = dict(listed._keys()[5])["t"] if isinstance(listed, MyMergeRequestsScreen) else None
        return merged_calls, merged_state, type(listed).__name__, list_key

    merged_calls, (tab, key, kinds), listed, list_key = _run(steps)
    assert len(merged_calls) == 1
    assert tab == "merged"
    assert key == "show list"
    assert ("more", "merged_recently_assigned") in kinds
    assert listed == "MyMergeRequestsScreen"
    assert list_key == "show active"


def test_stale_refresh_after_tab_switch_keeps_new_tab_rows():
    import time

    class SlowApi(_DashApi):
        slow = False

        def get_mr_dashboard_lists(self, full, counts=()):
            out = super().get_mr_dashboard_lists(full, counts)
            if self.slow and full[0][2] == "active":
                time.sleep(0.5)
            return out

    async def run():
        app = App()
        async with app.run_test(size=(200, 40)) as pilot:
            api = SlowApi()
            screen = MRDashboardScreen(api)
            await app.push_screen(screen)
            for _ in range(10):
                await pilot.pause(0.03)
            api.slow = True
            task = asyncio.create_task(screen._safe_refresh())
            await pilot.pause(0.1)
            await pilot.press("t")
            await pilot.pause(0.2)
            await task
            await pilot.pause(0.1)
            return screen.tab, _kinds(screen)

    tab, kinds = asyncio.run(run())
    assert tab == "merged"
    assert ("mr", "merged_recently_assigned") in kinds


def test_username_lookup_retries_after_auth_failure():
    api = GitLabAPI(_Config())
    attempts = []

    class _User:
        username = "me"

    def flaky_auth():
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("down")
        api.gl.user = _User()

    api.gl.auth = flaky_auth
    assert api.current_username() == ""
    assert api.current_username() == "me"
    assert api.current_username() == "me"
    assert len(attempts) == 2
