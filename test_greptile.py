import asyncio

from textual.app import App

from gitlab_monitor import tui
from gitlab_monitor.formatting import greptile_review

SUMMARY_BODY = """<!-- greptile_summary -->

<h2><a href="https://app.greptile.com/api/retrigger?id=1"><img alt="Retrigger" src="x.svg" height="24" align="right"></a>Confidence Score: 4/5</h2>

The PR appears safe to merge.

<details><summary><h3>Summary</h3></summary>

Adds metric badges.
- Metric records carry a badge.
</details>

<details open><summary><h3>Diagram</h3></summary>

```mermaid
flowchart LR
  A --> B
```
</details>

<!-- greptile_confidence_score:4 -->

<sub>Reviews (3) · Last reviewed commit: ["docs: changelog"](https://gitlab.example.com/c/85f5) · [Reviewed by Greptile](https://www.greptile.com/)</sub>"""


def _finding_body(title):
    return f"""<a href="#"><img alt="P2" src="p2.svg" height="20" align="top"></a> **{title}**

`metricKind()` drops the tooltip.

<details><summary>Prompt To Fix With AI</summary>

`````markdown
**{title}**
`````
</details>

<a href="https://app.greptile.com/ide/claude-code?prompt=x"><img alt="Fix in Claude Code" src="fix.svg" height="24"></a>"""


def _note(author, body, created, updated=None):
    return {"id": hash(body + created), "author": author, "body": body, "created_at": created,
            "updated_at": updated or created, "system": False}


def _disc(author, body, created, resolvable=False, unresolved=False, head_sha="", path="", line=None, updated=None):
    return {
        "id": f"{author}-{created}",
        "unresolved": unresolved,
        "resolvable": resolvable,
        "notes": [_note(author, body, created, updated)],
        "first_author": author,
        "first_created_at": created,
        "system": False,
        "path": path,
        "line": line,
        "head_sha": head_sha,
    }


def _discussions():
    return [
        _disc("greptile", SUMMARY_BODY, "2026-10-28T12:58:25Z", updated="2026-10-28T13:05:11Z"),
        _disc("greptile", _finding_body("Later finding"), "2026-10-28T13:03:00Z", True, True, "bbb", "ui/b.js", 9),
        _disc("greptile", _finding_body("Run badges lose their explanation"), "2026-10-28T12:58:27Z",
              True, False, "aaa", "ui/lib.js", 188),
        _disc("bryan", "lgtm", "2026-10-28T13:04:00Z"),
        _disc("greptile", "Greptile approved these changes.", "2026-10-28T13:05:15Z"),
    ]


def test_summary_score_updated_and_body():
    gt = greptile_review(_discussions())
    assert gt["score"] == 4
    assert gt["updated_at"] == "2026-10-28T13:05:11Z"
    text = repr(gt["segments"])
    assert "The PR appears safe to merge." in text
    assert "Adds metric badges." in text
    assert all(s[0] == "md" for s in gt["segments"])
    assert "Confidence Score" not in text
    assert "mermaid" not in text and "Diagram" not in text
    assert "Retrigger" not in text
    assert "Reviews (3)" in text


def test_findings_grouped_by_reviewed_sha_in_time_order():
    gt = greptile_review(_discussions())
    assert [[f["title"] for f in r] for r in gt["reviews"]] == [
        ["Run badges lose their explanation"],
        ["Later finding"],
    ]
    first = gt["reviews"][0][0]
    assert first["priority"] == "P2"
    assert first["unresolved"] is False
    assert (first["path"], first["line"]) == ("ui/lib.js", 188)
    assert gt["reviews"][1][0]["unresolved"] is True
    body = repr(first["segments"])
    assert "metricKind()" in body
    assert "Prompt To Fix" not in body
    assert "Fix in Claude Code" not in body
    assert "Run badges lose" not in body


def test_no_greptile_notes_returns_none():
    assert greptile_review([_disc("bryan", "lgtm", "2026-10-28T13:04:00Z")]) is None


class _ReviewFiles:
    def get(self, *a):
        return None


class _Recents:
    def remember(self, *a):
        pass


class _Config:
    gitlab_url = "https://gitlab.example.com"
    recent_projects = _Recents()
    review_files = _ReviewFiles()


class _Api:
    config = _Config()

    def __init__(self, discussions):
        self.discussions = discussions

    def get_merge_request(self, project_path, iid):
        return {
            "iid": iid, "id": 1, "title": "t", "author": "bryan", "assignees": [], "state": "merged",
            "source_branch": "a", "target_branch": "main", "created_at": "2026-10-28T12:55:31Z",
            "merged_at": "2026-10-28T13:08:43Z", "head_pipeline_status": None, "description": "d",
            "web_url": "https://gitlab.example.com/g/p/-/merge_requests/1",
        }

    def get_mr_discussions(self, project_path, iid):
        return self.discussions

    def get_mr_approval_state(self, project_path, iid):
        return []


def _run(discussions, steps):
    api = _Api(discussions)

    class _App(App):
        CSS = tui.PipelineMonitor.CSS

        def on_mount(self):
            self.push_screen(tui.MergeRequestDetailScreen(api, "g/p", 1))

    async def go():
        app = _App()
        async with app.run_test(size=(200, 50)) as pilot:
            for _ in range(40):
                await pilot.pause(0.05)
                if app.screen.mr is not None and app.screen._gt_sig is not None:
                    break
            await pilot.pause(0.1)
            return await steps(app, pilot, app.screen)

    return asyncio.run(go())


def _pane_text(screen):
    return "\n".join(str(w.render()) for w in screen.query("#mr-greptile Static"))


def test_split_is_seventy_thirty_and_summary_leaves_main_list():
    async def steps(app, pilot, screen):
        pane = screen.query_one("#mr-greptile")
        body = screen.query_one("#mr-body-scroll")
        left = [d["notes"][0]["body"][:20] for d in screen._visible_discussions()]
        text = _pane_text(screen)
        mds_before = len(screen.query("#mr-greptile Markdown"))
        await pilot.press("f")
        await pilot.pause(0.1)
        mds_after = len(screen.query("#mr-greptile Markdown"))
        await pilot.press("G")
        await pilot.pause(0.05)
        return (pane.display, pane.outer_size.width, body.outer_size.width, left, text,
                mds_before, mds_after, app.focused.id if app.focused else None)

    shown, pane_w, body_w, left, text, mds_before, mds_after, focused = _run(_discussions(), steps)
    assert shown
    assert pane_w == 60 and body_w == 140
    assert len(left) == 3 and not any("greptile_summary" in b for b in left)
    assert text.index("Review #1") < text.index("Run badges lose") < text.index("Review #2") < text.index("Later finding")
    assert "resolved" in text and "open" in text
    assert "(1 open · 1 resolved)" in text
    assert "4/5" in text
    # resolved finding body only after f
    assert mds_after == mds_before + 1
    assert focused == "mr-greptile"


def test_pane_hidden_without_greptile():
    async def steps(app, pilot, screen):
        pane = screen.query_one("#mr-greptile")
        return pane.display, screen.query_one("#mr-body-scroll").outer_size.width

    shown, body_w = _run([_disc("bryan", "lgtm", "2026-10-28T13:04:00Z")], steps)
    assert not shown
    assert body_w == 200
