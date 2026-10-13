import asyncio

from textual.app import App
from textual.widgets import RichLog

from gitlab_monitor.tui import FailedJobsScreen


class _Config:
    gitlab_url = "https://gitlab.example.com"


class _Api:
    config = _Config()
    project_name = "group/project"

    def get_job_failures(self, job_id):
        if job_id == 2:
            raise RuntimeError("boom")
        if job_id == 3:
            return []
        return [f"FAILED test_{job_id}"]


def test_failed_jobs_screen_lists_failures():
    jobs = [
        {"id": 1, "name": "unit", "stage": "test"},
        {"id": 2, "name": "lint", "stage": "test"},
        {"id": 3, "name": "e2e", "stage": "verify"},
    ]

    async def run():
        app = App()
        async with app.run_test() as pilot:
            screen = FailedJobsScreen(_Api(), {"id": 99}, jobs)
            await app.push_screen(screen)
            for _ in range(20):
                await pilot.pause(0.05)
            log = screen.query_one("#failures-log", RichLog)
            return "\n".join(line.text for line in log.lines)

    text = asyncio.run(run())
    assert "unit (stage: test, id: 1)" in text
    assert "FAILED test_1" in text
    # a failing fetch for one job must not hide the others
    assert "could not load failures: RuntimeError" in text
    assert "e2e (stage: verify, id: 3)" in text
    assert "No failures detected" in text


if __name__ == "__main__":
    test_failed_jobs_screen_lists_failures()
