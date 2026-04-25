# GitLab Monitor - K9s for GitLab

A K9s-style Terminal User Interface for monitoring GitLab pipelines with real-time updates, interactive navigation, and detailed job inspection.

## Installation

### Homebrew (recommended)

```bash
brew tap bearded-giant/tap
brew install gitlab-monitor
```

### From source (development)

```bash
pipx install -e ./gitlab-monitor
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
glmon                                # project picker (favorites first if any)
glmon -p group/subgroup/project      # jump straight into a project
glmon --days 7                       # default pipeline window: 7 days (default: 3)
glmon -p group/project --days 30     # combined
```

Without `GITLAB_PROJECT` or `--project` set, you'll get an interactive project picker. Starred projects load first for speed -- press `a` to load all projects.

### CLI Arguments

| Flag | Description |
|------|-------------|
| `-p, --project PATH` | Jump directly into `group/project` (skips picker). Overrides `GITLAB_PROJECT`. |
| `--days N` | Default pipeline age window in days. Default: 3. Use with `t` key to cycle in-app. |

## Features

1. **Loading splash** -- ASCII art splash screen while connecting to GitLab
2. **Project picker with favorites** -- star projects you care about; favorites load first (fast), toggle to full list on demand
3. **Pipeline age window** -- default 3 days (huge speedup on busy projects); cycle `3d / 7d / 30d / all` with `t`
4. **Direct project jump** -- `--project` arg bypasses picker
5. **Real-time auto-refresh** -- pipeline and job lists refresh every 10 seconds, job detail every 5 seconds
6. **Interactive navigation** -- arrow keys to navigate, Enter to drill down
7. **Multi-level drill-down** -- Projects -> Pipelines -> Jobs -> Logs
8. **Filtering** -- filter pipelines by branch name or user
9. **Failed job highlighting** -- failed jobs shown in red for quick identification
10. **Browser integration** -- open pipelines/jobs in browser with 'b' key
11. **Clipboard support** -- copy URLs or log output with 'y' key
12. **Failure extraction** -- automatically extracts and highlights test failures
13. **Job detail info bar** -- live status and duration display while viewing job logs

## Views

### 1. Project Picker (when no project set)
Defaults to **Favorites** view if any projects are starred (fast, no full project listing). Press `a` to toggle to **All** projects (sorted by last activity, favorites pinned to top). A leading `*` column marks starred projects. Type to filter, Enter to select, `s` to star/unstar.

Favorites persist to `~/.config/gitlab-monitor/favorites.json`.

### 2. Pipeline List View
Shows recent pipelines with ID, status (color-coded), branch, creation time, and commit SHA. Defaults to the last 3 days to keep load fast on busy projects. Press `t` to cycle window: `3d -> 7d -> 30d -> all`. Current window shown in breadcrumb.

### 3. Job List View
Shows all jobs in a pipeline grouped by stage (build, test, deploy, cleanup) with color-coded status and duration.

### 4. Job Detail View
Shows job logs with a live status/duration info bar, failure summary at top (for failed jobs), full trace, and error line highlighting. Log output streams incrementally as the job runs.

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
| `y` | Copy URL or content to clipboard |
| `Up/Down` | Navigate |
| `Enter` | Select / drill down |

### Project Picker

| Key | Action |
|-----|--------|
| `/` | Focus search input |
| `s` | Star / unstar current project |
| `a` | Toggle view: Favorites only <-> All projects |
| `Down` / `Escape` | Move from search to project list |

### Pipeline List View

| Key | Action |
|-----|--------|
| `/` | Focus filter input |
| `t` | Cycle age window: 3d -> 7d -> 30d -> all |
| `b` | Open selected pipeline in browser |
| `y` | Copy pipeline URL |

### Job List View

| Key | Action |
|-----|--------|
| `/` | Focus filter input |
| `b` | Open selected job in browser |
| `f` | Show failed jobs summary |
| `y` | Copy job URL |

### Job Detail View

| Key | Action |
|-----|--------|
| `b` | Open job in browser |
| `f` | Show failures only (hide full trace) |
| `y` | Copy log output |

## Usage Examples

### Basic Workflow
1. Launch with `glmon`
2. Search or scroll to find your project, press Enter
3. Navigate pipelines with arrow keys
4. Press Enter to view jobs in a pipeline
5. Press Enter on a job to view its logs
6. Press `q` to go back up a level

### Building a Favorites List
1. Launch `glmon`, press `a` to load all projects
2. Navigate to a project you care about, press `s` to star it
3. Repeat for other projects
4. Next launch: starred projects load instantly in the default Favorites view

### Jump Straight to a Project
```bash
glmon -p my-group/my-project
```
Skips the picker entirely. Useful for shell aliases or keyboard launchers.

### Loading Older Pipelines
Default view is last 3 days. Press `t` in the pipeline list to expand to 7d, 30d, or all. Or launch with `--days 30` to set a different default.

### Investigating Failures
1. Navigate to a pipeline with failed status (red)
2. Press Enter to see jobs
3. Failed jobs are highlighted in red
4. Press `f` to see only failed jobs
5. Press Enter on a failed job to see extracted failures

### Opening in Browser
At any level, press `b` to open the current selection in your browser for full GitLab UI access.

## Status Badges

| Badge | Style |
|-------|-------|
| `success` | Green |
| `failed` | Red |
| `running` | Yellow |
| `pending` | Dim |
| `canceled` | Dim |
| `created` | Dim |
| `manual` | Blue |
| `skipped` | Dim |

## Architecture

```
PipelineMonitor (App)
    ├── LoadingScreen (splash while connecting)
    ├── ProjectSelectScreen
    │   └── DataTable of projects + search input
    ├── PipelineListScreen (auto-refresh 10s)
    │   └── DataTable of pipelines
    ├── JobListScreen (auto-refresh 10s)
    │   └── DataTable of jobs (grouped by stage)
    ├── JobDetailScreen (auto-refresh 5s)
    │   └── Info bar (status + duration) + RichLog with trace/failures
    └── FailedJobsScreen
        └── RichLog with all failures
```

## Files

| Path | Purpose |
|------|---------|
| `~/.config/gitlab-monitor/config.json` | Optional non-token config (url, project, refresh_interval) |
| `~/.config/gitlab-monitor/favorites.json` | List of starred project paths |

## Future Enhancements

- [ ] Search within logs
- [ ] Export failures to file
- [ ] Pipeline trends/statistics view
- [ ] Job re-run capability
- [ ] Pipeline trigger from TUI
- [ ] Notification on failure
