# GitLab Monitor - K9s for GitLab

A K9s-style Terminal User Interface for monitoring GitLab pipelines with real-time updates, interactive navigation, and detailed job inspection.

## Installation

### Method 1: Install from source (recommended for development)
```bash
# Clone and install in development mode
git clone <repo-url>
cd gitlab-monitor
pip install -e .
```

### Method 2: Install as package
```bash
cd gitlab-monitor
pip install .
```

### Method 3: Install directly from git
```bash
pip install git+https://github.com/yourusername/gitlab-monitor.git
```

## Configuration

### Required Environment Variables
```bash
export GITLAB_URL=https://gitlab.example.com
export GITLAB_TOKEN=your_personal_access_token
export GITLAB_PROJECT=group/project

# Optional
export GITLAB_REFRESH_INTERVAL=30  # seconds between auto-refresh
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
# Start the TUI
gitlab-monitor

# Or use the short alias
glmon
```

## Features

### 🎯 Main Features
- **Real-time pipeline monitoring** - Auto-refreshes every 30 seconds
- **Interactive navigation** - Navigate with arrow keys, select with Enter
- **Multi-level drill-down** - Pipelines → Jobs → Logs
- **Filtering** - Filter by branch name or user
- **Failed job highlighting** - Failed jobs shown in red for quick identification
- **Browser integration** - Open pipelines/jobs in browser with 'b' key
- **Failure extraction** - Automatically extracts and highlights test failures

### 📊 Views

#### 1. Pipeline List View (Main Screen)
Shows all recent pipelines with:
- Pipeline ID
- Status (color-coded with icons)
- Branch name
- User who triggered it
- Creation time
- Commit SHA

#### 2. Job List View
Shows all jobs in a pipeline:
- Grouped by stage (build → test → deploy → cleanup)
- Color-coded status indicators
- Failed jobs highlighted in red
- Duration for completed jobs

#### 3. Job Detail View
Shows job logs with:
- Failure summary at top (for failed jobs)
- Full trace/logs
- Error line highlighting
- Scrollable view for long logs

#### 4. Failed Jobs Summary View
Quick view of all failed jobs in a pipeline with extracted failure messages

## Keyboard Shortcuts

### Global
- `q` - Quit/Go back
- `r` - Refresh current view
- `?` - Show help
- `↑/↓` - Navigate up/down
- `Enter` - Select/drill down

### Pipeline List View
- `f` - Focus on filter inputs
- `b` - Open selected pipeline in browser
- `/` - Search (coming soon)

### Job List View
- `b` - Open selected job in browser
- `f` - Show only failed jobs

### Job Detail View
- `b` - Open job in browser
- `f` - Show failures only (hide full trace)

## Usage Examples

### Basic Workflow
1. Launch the monitor: `./pipeline_monitor.py`
2. Navigate pipelines with arrow keys
3. Press Enter to view jobs in a pipeline
4. Press Enter on a job to view its logs
5. Press 'q' to go back up a level

### Filtering Pipelines
1. Press 'f' to focus on filters
2. Type branch name (e.g., "feature-xyz")
3. Tab to user filter if needed
4. Click "Apply" or press Enter

### Investigating Failures
1. Navigate to a pipeline with failed status (red)
2. Press Enter to see jobs
3. Failed jobs will be highlighted in red
4. Press 'f' to see only failed jobs
5. Press Enter on a failed job to see extracted failures

### Opening in Browser
At any level, press 'b' to open the current selection in your browser for full GitLab UI access

## Status Icons

- ✅ Success (green)
- ❌ Failed (red, bold for emphasis)
- 🔄 Running (yellow)
- ⏸ Pending (dim)
- ⏭ Skipped (dim)
- 🚫 Canceled

## Auto-Refresh

The TUI automatically refreshes the current view every 30 seconds to show:
- New pipelines
- Updated job statuses
- Changed pipeline states

## Architecture

```
PipelineMonitor (App)
    ├── PipelineListScreen
    │   └── DataTable of pipelines
    ├── JobListScreen
    │   └── DataTable of jobs (grouped by stage)
    ├── JobDetailScreen
    │   └── TextLog with trace/failures
    └── FailedJobsScreen
        └── TextLog with all failures
```

## Tips

1. **Quick failure investigation**: From pipeline list, look for red ❌ status, Enter → press 'f' for failed jobs → Enter on job to see failures

2. **Monitor specific branch**: Use filters to watch only your branch's pipelines

3. **Bulk failure review**: Use Failed Jobs view ('f' from job list) to see all failures at once

4. **Browser fallback**: If you need more details, 'b' opens the current item in GitLab's web UI

## Comparison to CLI Tools

| Feature | CLI (`pipeline_explorer.py`) | TUI (`pipeline_monitor.py`) |
|---------|------------------------------|------------------------------|
| Navigation | Command-based | Interactive arrow keys |
| Updates | Manual refresh | Auto-refresh every 30s |
| View multiple items | Sequential commands | Side-by-side in table |
| Failed job highlighting | Text output | Visual red highlighting |
| Filtering | Command arguments | Interactive filters |
| Browser integration | No | Yes (press 'b') |

## Future Enhancements

- [ ] Search within logs
- [ ] Export failures to file
- [ ] Pipeline trends/statistics view
- [ ] Multiple project support
- [ ] Customizable refresh interval
- [ ] Job re-run capability
- [ ] Pipeline trigger from TUI
- [ ] Notification on failure