# Jira Sync (`glmon-jirad`)

`glmon-jirad` keeps one Jira ticket per merge request you author, as a side effect of the MR. You never touch Jira: open an MR and a ticket shows up, merge it and the ticket closes.

There's no webhook and no server. Each run lists your MRs, works out what each ticket's status should be from the MR's current state, and fixes whatever drifted. A run missed while the laptop slept gets caught up by the next one, so nothing depends on catching an event.

## What a run does

For an open MR whose title has no ticket key, it creates a ticket in `jira.project`. The summary is the MR title. The description is up to three plain-language bullets about what the MR is for, written by `claude -p --model haiku` from the MR description (if that call fails, it uses the first line of the description instead). The ticket gets the `jira.label` label, is assigned to you, and carries a link back to the MR.

Then it writes the key into the MR title. A placeholder key such as `PE-0000` is swapped in place, so `chore(PE-0000): add x` becomes `chore(PE-123): add x`. Titles without a placeholder get a prefix: `feat: add x` becomes `[PE-123] feat: add x`, and a `Draft:` prefix stays in front. The title is the only place the link lives. Jira's GitLab integration reads it from there, and the next run finds the ticket from there.

After that, the ticket status follows the MR:

| MR state | Ticket status |
|----------|---------------|
| draft, or head pipeline not green | In Progress |
| not draft, head pipeline green (or the project runs no MR CI) | In Review |
| merged | Done |
| closed without merging | Won't fix |

A closed MR that gets reopened moves its ticket back out of Won't fix. Done is final.

## What it leaves alone

It only moves tickets it created (the ones carrying `jira.label`). A ticket key you typed into a title yourself, say an infra ticket someone filed, is never touched, because one ticket can span several MRs.

Even on its own tickets, it only moves them out of a To Do category status or a status it set itself. If someone moves a ticket to In QA, In Monitoring, or Blocked by hand, it stays there.

MRs under a namespace in `jira.exclude_namespaces` are skipped entirely, and so are MRs created before you enabled the sync. Turning it on never backfills old MRs; use `--mr` for one-offs.

## Setup

1. Add a `jira` block to `~/.config/gitlab-monitor/config.json`. Only `base`, `email`, and `project` are required.

   ```json
   "jira": {
     "base": "https://your-domain.atlassian.net",
     "email": "you@example.com",
     "project": "PE",
     "exclude_namespaces": ["your.username", "some-group"]
   }
   ```

   The other keys and their defaults: `issue_type` (`Story`), `label` (`mr-sync`), `lookback_days` (`14`, how far back a run looks for MRs that changed), and `since` (set for you by `--enable`).

2. Put your Jira API token in a file only you can read. The background job runs under launchd, which doesn't see your shell's environment, so an exported token alone isn't enough.

   ```bash
   printf '%s' "$JIRA_API_TOKEN" > ~/.config/gitlab-monitor/jira_token && chmod 600 ~/.config/gitlab-monitor/jira_token
   ```

   From a shell, `JIRA_TOKEN` or `JIRA_API_TOKEN` work too, and `JIRA_EMAIL` and `JIRA_BASE` override the config block. These are the same names jira-tui reads.

3. Turn it on and check the result.

   ```bash
   glmon-jirad --enable
   glmon-jirad --status
   ```

   You want `enabled=True` and `token=file+env` or `token=file`. If you see `token=env-only(launchd-needs-file)`, step 2 didn't happen.

4. Try it on one MR before scheduling it.

   ```bash
   glmon-jirad --dry-run --mr 'group/project!123'
   glmon-jirad --mr 'group/project!123'
   glmon-jirad --lookup 'group/project!123'
   ```

5. Schedule it every five minutes.

   ```bash
   cp launchd/com.bryan.glmon-jirad.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.bryan.glmon-jirad.plist
   tail -f ~/.cache/glmon/jirad.err
   ```

   Edit the paths and username in the plist if yours differ.

## Commands

| Command | What it does |
|---------|--------------|
| `glmon-jirad` or `--once` | one run over your recent MRs |
| `--dry-run` | print what a run would change, write nothing; works while disabled |
| `--mr <ref>` | sync one MR now, even one created before `--enable`; waits up to 90 seconds if a scheduled run is in progress |
| `--mr <ref> --summary-file <path>` | same, but use your own bullets for a new ticket's description |
| `--lookup [<ref>]` | read-only check of the ticket behind a branch or MR (see below) |
| `--status` | config, token source, and how many MRs are tracked |
| `--enable` / `--disable` | turn the sync on or off; `--lookup` keeps working either way |

`<ref>` is an MR URL or `group/project!123`. `--lookup` also takes a branch name, and with no argument it uses the current checkout and branch.

## Checking a ticket

`glmon-jirad --lookup` finds the MR for your current branch and prints the ticket and whether it matches the MR. It only reads.

```
$ glmon-jirad --lookup
PE-123  In Review  feat: add the thing  https://your-domain.atlassian.net/browse/PE-123
group/project!77  opened  pipeline success  expects In Review: in sync
```

The end of the second line tells you where things stand:

| Verdict | Meaning |
|---------|---------|
| `in sync` | ticket status matches what the MR implies |
| `drift: the next jirad tick moves it` | the MR changed since the last run; the next scheduled run fixes it |
| `drift: jirad is disabled` | they disagree and nothing will fix it until you `--enable` |
| `held at <status> by hand, jirad leaves it` | someone moved the ticket to a status the sync never touches |
| `not created by jirad, so it is left alone` | the key belongs to a ticket someone made by hand |

With no ticket, you get one line saying why: the namespace is excluded, there's no MR for the branch yet, the MR closed without one, the sync is off, the MR predates `--enable` (the line gives you the `--mr` command), or the next run will create it.

## Files

| Path | Purpose |
|------|---------|
| `~/.config/gitlab-monitor/config.json` | the `jira` block |
| `~/.config/gitlab-monitor/jira_token` | 0600 API token for the launchd job |
| `~/.cache/glmon/jirad-state.json` | last status applied per MR, so unchanged MRs cost no Jira calls |
| `~/.cache/glmon/jirad.{out,err}` | launchd logs |
| `~/.cache/glmon/_locks/jirad.lock` | held while a run is going, so two runs never create the same ticket |

## Troubleshooting

| You see | Why | Fix |
|---------|-----|-----|
| `jira config missing: token` in `jirad.err` | launchd can't see your shell's token | write `~/.config/gitlab-monitor/jira_token` (setup step 2) |
| `jira config missing: base` (or `email`, `project`) | the config block is incomplete | add the key to the `jira` block |
| a new MR never gets a ticket | it's in an excluded namespace, it predates `--enable`, or the job isn't loaded | `glmon-jirad --lookup <ref>` says which; `launchctl list \| grep jirad` |
| `no transition to 'Done'` | the Jira workflow blocks the move, usually open sub-tasks | close the sub-tasks or move the ticket by hand |
| `another jirad run holds the lock` | a run was already in progress | nothing; the next run picks the MR up |
| `jira lookup failed: 404` | the key in the title has no ticket | fix the title, or remove the key and let the sync create one |
| `jira lookup failed: 401` | the token is wrong or expired | replace the token file |

## Turning it off

`glmon-jirad --disable` makes scheduled runs exit straight away. `launchctl unload ~/Library/LaunchAgents/com.bryan.glmon-jirad.plist` stops the schedule. Neither touches existing tickets or MR titles.
