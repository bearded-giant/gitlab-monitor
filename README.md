# GitLab Monitor - K9s for GitLab

A K9s-style Terminal User Interface for monitoring GitLab pipelines with real-time updates, interactive navigation, and detailed job inspection.

## Installation

Install with pipx for an isolated, globally available command:

```bash
pipx install ./gitlab-monitor
```

For development:

```bash
pipx install -e ./gitlab-monitor
```

To upgrade after making changes:

```bash
pipx install --force ./gitlab-monitor
```

## Configuration

### Required Environment Variables
```bash
export GITLAB_URL=https://gitlab.example.com
export GITLAB_TOKEN=your_personal_access_token
```

### Optional Environment Variables
```bash
export GITLAB_PROJECT=group/project         # skip project picker, go straight to pipelines
export GITLAB_REFRESH_INTERVAL=30           # seconds between auto-refresh (default: 30)
```

### Config File (Optional)
Configuration can also be stored in `~/.config/gitlab-monitor/config.json`:
```json
{
  "gitlab_url": "https://gitlab.example.com",
  "project_path": "group/project",
  "refresh_interval": 30,
  "max_pipelines": 50
}
```

Note: Never store tokens in config files. Always use environment variables for tokens.

## Quick Start

After installation, the tool is available as `gitlab-monitor` or `glmon` (short alias):

```bash
glmon
```

Without `GITLAB_PROJECT` set, you'll get an interactive project picker with typeahead search. With it set, you go straight to that project's pipelines.

## Features

1. **Project picker** -- browse and search all your GitLab projects with typeahead filtering
2. **Real-time pipeline monitoring** -- auto-refreshes every 30 seconds
3. **Interactive navigation** -- arrow keys to navigate, Enter to drill down
4. **Multi-level drill-down** -- Projects -> Pipelines -> Jobs -> Logs
5. **Filtering** -- filter pipelines by branch name or user
6. **Failed job highlighting** -- failed jobs shown in red for quick identification
7. **Browser integration** -- open pipelines/jobs in browser with 'b' key
8. **Failure extraction** -- automatically extracts and highlights test failures

## Views

### 1. Project Picker (when GITLAB_PROJECT is not set)
Lists all your GitLab projects sorted by last activity. Type to filter, Enter to select.

### 2. Pipeline List View
Shows recent pipelines with ID, status (color-coded), branch, creation time, and commit SHA.

### 3. Job List View
Shows all jobs in a pipeline grouped by stage (build, test, deploy, cleanup) with color-coded status and duration.

### 4. Job Detail View
Shows job logs with failure summary at top (for failed jobs), full trace, and error line highlighting.

### 5. Failed Jobs Summary View
Quick view of all failed jobs in a pipeline with extracted failure messages.

## Keyboard Shortcuts

### Global

| Key | Action |
|-----|--------|
| `q` | Go back / quit |
| `r` | Refresh current view |
| `Ctrl+c` | Quit immediately |
| `Ctrl+q` | Quit immediately |
| `?` | Show help |
| `Up/Down` | Navigate |
| `Enter` | Select / drill down |

### Project Picker

| Key | Action |
|-----|--------|
| `/` | Focus search input |
| `Down` / `Escape` | Move from search to project list |

### Pipeline List View

| Key | Action |
|-----|--------|
| `f` | Focus on filter inputs |
| `b` | Open selected pipeline in browser |

### Job List View

| Key | Action |
|-----|--------|
| `b` | Open selected job in browser |
| `f` | Show only failed jobs |

### Job Detail View

| Key | Action |
|-----|--------|
| `b` | Open job in browser |
| `f` | Show failures only (hide full trace) |

## Usage Examples

### Basic Workflow
1. Launch with `glmon`
2. Search or scroll to find your project, press Enter
3. Navigate pipelines with arrow keys
4. Press Enter to view jobs in a pipeline
5. Press Enter on a job to view its logs
6. Press `q` to go back up a level

### Investigating Failures
1. Navigate to a pipeline with failed status (red)
2. Press Enter to see jobs
3. Failed jobs are highlighted in red
4. Press `f` to see only failed jobs
5. Press Enter on a failed job to see extracted failures

### Opening in Browser
At any level, press `b` to open the current selection in your browser for full GitLab UI access.

## Status Icons

| Icon | Status |
|------|--------|
| Green check | Success |
| Red X | Failed |
| Yellow arrows | Running |
| Dim pause | Pending |
| Dim skip | Skipped |

## Architecture

```
PipelineMonitor (App)
    ├── ProjectSelectScreen
    │   └── DataTable of projects + search input
    ├── PipelineListScreen
    │   └── DataTable of pipelines
    ├── JobListScreen
    │   └── DataTable of jobs (grouped by stage)
    ├── JobDetailScreen
    │   └── RichLog with trace/failures
    └── FailedJobsScreen
        └── RichLog with all failures
```

## Future Enhancements

- [ ] Search within logs
- [ ] Export failures to file
- [ ] Pipeline trends/statistics view
- [ ] Customizable refresh interval
- [ ] Job re-run capability
- [ ] Pipeline trigger from TUI
- [ ] Notification on failure
