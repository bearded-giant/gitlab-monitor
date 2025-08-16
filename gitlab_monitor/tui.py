#!/usr/bin/env python3
# Copyright 2024 BeardedGiant
# https://github.com/bearded-giant/gitlab-tools
# Licensed under Apache License 2.0

import sys
import gitlab
import webbrowser
from datetime import datetime
from typing import Optional, List, Dict, Any
import re
import asyncio
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable, Static, Input, TextLog, Label, Button
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.screen import Screen
from textual.reactive import reactive
from textual.binding import Binding
from rich.text import Text
from rich.table import Table

from .config import Config


class GitLabAPI:
    """GitLab API wrapper for pipeline operations"""
    
    def __init__(self, config: Config):
        self.config = config
        self.gl = gitlab.Gitlab(config.gitlab_url, private_token=config.gitlab_token)
        self.project = self.gl.projects.get(config.project_path)
    
    def get_recent_pipelines(self, limit=50, ref=None, username=None):
        """Get recent pipelines with optional filters"""
        params = {'per_page': limit, 'order_by': 'id', 'sort': 'desc'}
        if ref:
            params['ref'] = ref
        if username:
            params['username'] = username
        
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
                'user': p.user.get('username') if p.user else 'unknown',
                'web_url': p.web_url
            })
        return results
    
    def get_pipeline_jobs(self, pipeline_id):
        """Get all jobs for a pipeline"""
        try:
            pipeline = self.project.pipelines.get(pipeline_id)
            jobs = pipeline.jobs.list(all=True)
            results = []
            for job in jobs:
                results.append({
                    'id': job.id,
                    'name': job.name,
                    'status': job.status,
                    'stage': job.stage,
                    'duration': job.duration,
                    'started_at': job.started_at,
                    'finished_at': job.finished_at,
                    'web_url': job.web_url
                })
            return results
        except Exception as e:
            return []
    
    def get_job_trace(self, job_id):
        """Get job trace/logs"""
        try:
            job = self.project.jobs.get(job_id)
            trace = job.trace()
            if isinstance(trace, bytes):
                trace = trace.decode("utf-8", errors="replace")
            return trace
        except Exception as e:
            return f"Error fetching trace: {e}"
    
    def get_job_failures(self, job_id):
        """Extract failure information from job trace"""
        trace = self.get_job_trace(job_id)
        
        failures = []
        
        # Extract short summary
        summary_pattern = re.compile(
            r"=+\s*short test summary info\s*=+\n(.*?)(?=^=+|\Z)",
            re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )
        summary_match = summary_pattern.search(trace)
        if summary_match:
            summary = summary_match.group(1).strip()
            for line in summary.split('\n'):
                if 'FAILED' in line:
                    failures.append(line.strip())
        
        # Look for generic errors if no test summary
        if not failures:
            for line in trace.split('\n'):
                if any(keyword in line.lower() for keyword in ['error:', 'failed:', 'exception:']):
                    failures.append(line.strip())
                    if len(failures) > 20:
                        break
        
        return failures


class PipelineListScreen(Screen):
    """Main screen showing list of pipelines"""
    
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("f", "filter", "Filter"),
        Binding("enter", "select", "View Jobs"),
        Binding("b", "browser", "Open in Browser"),
        Binding("/", "search", "Search"),
        Binding("?", "help", "Help"),
    ]
    
    def __init__(self, api: GitLabAPI):
        super().__init__()
        self.api = api
        self.pipelines = []
        self.filtered_pipelines = []
        self.filter_ref = None
        self.filter_user = None
    
    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Label("Filters: ", id="filter-label"),
            Input(placeholder="Branch filter (press 'f' to edit)", id="branch-filter"),
            Input(placeholder="User filter", id="user-filter"),
            Button("Apply", id="apply-filter"),
            id="filter-container"
        )
        yield DataTable(id="pipeline-table")
        yield Footer()
    
    async def on_mount(self) -> None:
        table = self.query_one("#pipeline-table", DataTable)
        table.add_columns("ID", "Status", "Branch", "User", "Created", "SHA")
        table.cursor_type = "row"
        await self.load_pipelines()
    
    async def load_pipelines(self) -> None:
        """Load pipelines from GitLab"""
        self.pipelines = self.api.get_recent_pipelines(
            ref=self.filter_ref,
            username=self.filter_user
        )
        self.filtered_pipelines = self.pipelines
        await self.update_table()
    
    async def update_table(self) -> None:
        """Update the pipeline table display"""
        table = self.query_one("#pipeline-table", DataTable)
        table.clear()
        
        for pipeline in self.filtered_pipelines:
            # Color-code status
            status = pipeline['status']
            if status == 'success':
                status_text = Text(f"✅ {status}", style="green")
            elif status == 'failed':
                status_text = Text(f"❌ {status}", style="red")
            elif status == 'running':
                status_text = Text(f"🔄 {status}", style="yellow")
            elif status == 'pending':
                status_text = Text(f"⏸ {status}", style="dim")
            else:
                status_text = Text(f"  {status}")
            
            created = datetime.fromisoformat(pipeline['created_at'].replace('Z', '+00:00'))
            created_str = created.strftime('%m-%d %H:%M')
            
            table.add_row(
                str(pipeline['id']),
                status_text,
                pipeline['ref'][:30],
                pipeline['user'],
                created_str,
                pipeline['sha']
            )
    
    async def action_refresh(self) -> None:
        """Refresh pipeline list"""
        await self.load_pipelines()
    
    async def action_filter(self) -> None:
        """Focus on filter input"""
        self.query_one("#branch-filter", Input).focus()
    
    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle filter apply button"""
        if event.button.id == "apply-filter":
            branch_filter = self.query_one("#branch-filter", Input)
            user_filter = self.query_one("#user-filter", Input)
            self.filter_ref = branch_filter.value if branch_filter.value else None
            self.filter_user = user_filter.value if user_filter.value else None
            await self.load_pipelines()
    
    async def action_select(self) -> None:
        """View jobs for selected pipeline"""
        table = self.query_one("#pipeline-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self.filtered_pipelines):
            pipeline = self.filtered_pipelines[table.cursor_row]
            self.app.push_screen(JobListScreen(self.api, pipeline))
    
    async def action_browser(self) -> None:
        """Open selected pipeline in browser"""
        table = self.query_one("#pipeline-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self.filtered_pipelines):
            pipeline = self.filtered_pipelines[table.cursor_row]
            webbrowser.open(pipeline['web_url'])


class JobListScreen(Screen):
    """Screen showing jobs for a specific pipeline"""
    
    BINDINGS = [
        Binding("q", "back", "Back"),
        Binding("r", "refresh", "Refresh"),
        Binding("enter", "select", "View Logs"),
        Binding("b", "browser", "Open in Browser"),
        Binding("f", "failures", "Show Failures"),
        Binding("?", "help", "Help"),
    ]
    
    def __init__(self, api: GitLabAPI, pipeline: dict):
        super().__init__()
        self.api = api
        self.pipeline = pipeline
        self.jobs = []
        self.jobs_by_stage = {}
    
    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Label(f"Pipeline {self.pipeline['id']} - {self.pipeline['ref']} ({self.pipeline['status']})", 
                  id="pipeline-info"),
            id="info-container"
        )
        yield DataTable(id="job-table")
        yield Footer()
    
    async def on_mount(self) -> None:
        table = self.query_one("#job-table", DataTable)
        table.add_columns("ID", "Stage", "Name", "Status", "Duration")
        table.cursor_type = "row"
        await self.load_jobs()
    
    async def load_jobs(self) -> None:
        """Load jobs for the pipeline"""
        self.jobs = self.api.get_pipeline_jobs(self.pipeline['id'])
        
        # Group by stage
        self.jobs_by_stage = {}
        for job in self.jobs:
            stage = job['stage']
            if stage not in self.jobs_by_stage:
                self.jobs_by_stage[stage] = []
            self.jobs_by_stage[stage].append(job)
        
        await self.update_table()
    
    async def update_table(self) -> None:
        """Update the job table display"""
        table = self.query_one("#job-table", DataTable)
        table.clear()
        
        # Display jobs grouped by stage
        stage_order = ['build', 'test', 'deploy', 'cleanup']
        all_stages = list(self.jobs_by_stage.keys())
        for stage in all_stages:
            if stage not in stage_order:
                stage_order.append(stage)
        
        for stage in stage_order:
            if stage not in self.jobs_by_stage:
                continue
            
            for job in self.jobs_by_stage[stage]:
                # Color-code status
                status = job['status']
                if status == 'success':
                    status_text = Text(f"✅ {status}", style="green")
                elif status == 'failed':
                    status_text = Text(f"❌ {status}", style="red bold")
                elif status == 'running':
                    status_text = Text(f"🔄 {status}", style="yellow")
                elif status == 'pending':
                    status_text = Text(f"⏸ {status}", style="dim")
                elif status == 'skipped':
                    status_text = Text(f"⏭ {status}", style="dim")
                else:
                    status_text = Text(f"  {status}")
                
                duration = self.format_duration(job['duration']) if job['duration'] else '-'
                
                # Make failed jobs stand out
                if status == 'failed':
                    table.add_row(
                        Text(str(job['id']), style="red"),
                        Text(job['stage'], style="red"),
                        Text(job['name'][:50], style="red bold"),
                        status_text,
                        Text(duration, style="red")
                    )
                else:
                    table.add_row(
                        str(job['id']),
                        job['stage'],
                        job['name'][:50],
                        status_text,
                        duration
                    )
    
    def format_duration(self, duration):
        """Format duration in seconds to human-readable"""
        if duration is None:
            return "N/A"
        minutes, seconds = divmod(duration, 60)
        return f"{int(minutes)}m{int(seconds)}s"
    
    async def action_back(self) -> None:
        """Go back to pipeline list"""
        self.app.pop_screen()
    
    async def action_refresh(self) -> None:
        """Refresh job list"""
        await self.load_jobs()
    
    async def action_select(self) -> None:
        """View logs for selected job"""
        table = self.query_one("#job-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self.jobs):
            job = self.jobs[table.cursor_row]
            self.app.push_screen(JobDetailScreen(self.api, job))
    
    async def action_browser(self) -> None:
        """Open selected job in browser"""
        table = self.query_one("#job-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self.jobs):
            job = self.jobs[table.cursor_row]
            webbrowser.open(job['web_url'])
    
    async def action_failures(self) -> None:
        """Show only failed jobs"""
        # Filter to show only failed jobs
        failed_jobs = [j for j in self.jobs if j['status'] == 'failed']
        if failed_jobs:
            self.app.push_screen(FailedJobsScreen(self.api, self.pipeline, failed_jobs))


class JobDetailScreen(Screen):
    """Screen showing job logs and details"""
    
    BINDINGS = [
        Binding("q", "back", "Back"),
        Binding("r", "refresh", "Refresh"),
        Binding("b", "browser", "Open in Browser"),
        Binding("f", "failures", "Show Failures Only"),
        Binding("?", "help", "Help"),
    ]
    
    def __init__(self, api: GitLabAPI, job: dict):
        super().__init__()
        self.api = api
        self.job = job
        self.trace = ""
        self.failures = []
    
    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Label(f"Job {self.job['id']}: {self.job['name']} ({self.job['status']})", 
                  id="job-info"),
            id="info-container"
        )
        yield ScrollableContainer(
            TextLog(id="job-log", wrap=True),
            id="log-container"
        )
        yield Footer()
    
    async def on_mount(self) -> None:
        await self.load_trace()
    
    async def load_trace(self) -> None:
        """Load job trace/logs"""
        log = self.query_one("#job-log", TextLog)
        log.clear()
        
        self.trace = self.api.get_job_trace(self.job['id'])
        
        # If job failed, highlight failures
        if self.job['status'] == 'failed':
            self.failures = self.api.get_job_failures(self.job['id'])
            if self.failures:
                log.write("=" * 80)
                log.write("FAILURE SUMMARY:", style="red bold")
                log.write("=" * 80)
                for failure in self.failures:
                    log.write(failure, style="red")
                log.write("=" * 80)
                log.write("")
        
        # Show full trace
        log.write("FULL TRACE:")
        log.write("-" * 80)
        for line in self.trace.split('\n'):
            # Highlight error lines
            if any(keyword in line.lower() for keyword in ['error', 'failed', 'exception']):
                log.write(line, style="red")
            else:
                log.write(line)
    
    async def action_back(self) -> None:
        """Go back to job list"""
        self.app.pop_screen()
    
    async def action_refresh(self) -> None:
        """Refresh trace"""
        await self.load_trace()
    
    async def action_browser(self) -> None:
        """Open job in browser"""
        webbrowser.open(self.job['web_url'])
    
    async def action_failures(self) -> None:
        """Show only failures"""
        log = self.query_one("#job-log", TextLog)
        log.clear()
        
        if self.failures:
            log.write("FAILURES ONLY:", style="red bold")
            log.write("=" * 80)
            for failure in self.failures:
                log.write(failure, style="red")
        else:
            log.write("No failures detected in this job")


class FailedJobsScreen(Screen):
    """Screen showing all failed jobs in a pipeline"""
    
    BINDINGS = [
        Binding("q", "back", "Back"),
        Binding("enter", "select", "View Job"),
        Binding("?", "help", "Help"),
    ]
    
    def __init__(self, api: GitLabAPI, pipeline: dict, failed_jobs: list):
        super().__init__()
        self.api = api
        self.pipeline = pipeline
        self.failed_jobs = failed_jobs
    
    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Label(f"Failed Jobs in Pipeline {self.pipeline['id']}", id="info"),
            id="info-container"
        )
        yield ScrollableContainer(
            TextLog(id="failures-log", wrap=True),
            id="log-container"
        )
        yield Footer()
    
    async def on_mount(self) -> None:
        await self.load_failures()
    
    async def load_failures(self) -> None:
        """Load all failure summaries"""
        log = self.query_one("#failures-log", TextLog)
        log.clear()
        
        for job in self.failed_jobs:
            log.write("=" * 80, style="red")
            log.write(f"Job {job['id']}: {job['name']}", style="red bold")
            log.write(f"Stage: {job['stage']} | Duration: {self.format_duration(job['duration'])}")
            log.write("-" * 80)
            
            failures = self.api.get_job_failures(job['id'])
            if failures:
                for failure in failures[:10]:  # Show first 10 failures
                    log.write(failure, style="red")
            else:
                log.write("No specific failures extracted")
            
            log.write("")
    
    def format_duration(self, duration):
        if duration is None:
            return "N/A"
        minutes, seconds = divmod(duration, 60)
        return f"{int(minutes)}m{int(seconds)}s"
    
    async def action_back(self) -> None:
        self.app.pop_screen()


class PipelineMonitor(App):
    """Main TUI application"""
    
    CSS = """
    #filter-container {
        height: 3;
        dock: top;
        background: $surface;
        padding: 0 1;
    }
    
    #filter-container Input {
        width: 30;
        margin: 0 1;
    }
    
    #filter-container Button {
        width: 10;
        margin: 0 1;
    }
    
    #info-container {
        height: 3;
        dock: top;
        background: $primary;
        padding: 1;
    }
    
    #pipeline-table, #job-table {
        background: $surface;
    }
    
    #log-container {
        background: black;
        padding: 1;
    }
    
    #job-log, #failures-log {
        background: black;
        color: white;
    }
    """
    
    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.api = GitLabAPI(config)
        self.refresh_task = None
        self.refresh_interval = config.refresh_interval
    
    async def on_mount(self) -> None:
        """Initialize the app with pipeline list"""
        self.push_screen(PipelineListScreen(self.api))
        
        # Start auto-refresh
        self.refresh_task = asyncio.create_task(self.auto_refresh())
    
    async def auto_refresh(self) -> None:
        """Auto-refresh current screen at configured interval"""
        while True:
            await asyncio.sleep(self.refresh_interval)
            screen = self.screen
            if hasattr(screen, 'load_pipelines'):
                await screen.load_pipelines()
            elif hasattr(screen, 'load_jobs'):
                await screen.load_jobs()
            elif hasattr(screen, 'load_trace'):
                await screen.load_trace()


def main():
    """Main entry point for the TUI"""
    config = Config()
    
    # Validate configuration
    valid, message = config.validate()
    if not valid:
        print(f"Error: {message}", file=sys.stderr)
        print("\nConfiguration can be set via environment variables:")
        print("  export GITLAB_URL=https://gitlab.example.com")
        print("  export GITLAB_TOKEN=your_personal_access_token")
        print("  export GITLAB_PROJECT=group/project")
        sys.exit(1)
    
    app = PipelineMonitor(config)
    app.run()


if __name__ == "__main__":
    main()