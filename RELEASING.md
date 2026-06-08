# Releasing gitlab-monitor

## Cut a release

```bash
git push origin main          # push code first (note: origin = github, not gitlab)
./release.sh 1.5.4
```

`release.sh` does:
1. Bumps `version = "..."` in `pyproject.toml`
2. Commits `bump to v1.5.4`
3. Tags `v1.5.4`
4. Pushes `main` + tag to `origin` (github)

## What happens after push

```
v1.5.4 tag pushed
  → release.yml (pyinstaller on macos-14) builds glmon-aarch64-apple-darwin
  → publishes GitHub release w/ asset
  → release:published event triggers update-homebrew.yml
  → downloads asset, computes SHA256, regenerates Formula/gitlab-monitor.rb
  → commits to bearded-giant/homebrew-tap as github-actions[bot]
  → brew upgrade gitlab-monitor works
```

## Watch

```bash
gh run watch -R bearded-giant/gitlab-monitor
gh run list -R bearded-giant/gitlab-monitor --workflow=update-homebrew.yml --limit 1
```

## Verify install

```bash
brew update
brew upgrade gitlab-monitor
glmon --version
```

## Secrets

| Secret | Required for |
|---|---|
| `GITHUB_TOKEN` | auto, release upload + asset download in update-homebrew.yml |
| `HOMEBREW_TAP_TOKEN` | update-homebrew.yml push to `bearded-giant/homebrew-tap` |

Set via:
```bash
gh secret set HOMEBREW_TAP_TOKEN -R bearded-giant/gitlab-monitor --body "$HOMEBREW_TAP_TOKEN"
```

## Manual backfill

If `update-homebrew.yml` failed/skipped for a tag that already exists:

```bash
gh workflow run update-homebrew.yml -R bearded-giant/gitlab-monitor -f tag=v1.5.4
```

## Remotes

```
origin   github.com:bearded-giant/gitlab-monitor   ← releases happen here
gitlab   gitlab.rechargeapps.net:...               ← recharge mirror, no releases
```

`./release.sh` pushes to `origin` only. Recharge mirror stays cold.

## Failure recovery

| Symptom | Fix |
|---|---|
| release.yml red | fix on main, `git tag -d v1.5.4 && git push origin :v1.5.4 && ./release.sh 1.5.4` |
| Tap formula stale | `gh workflow run update-homebrew.yml -R bearded-giant/gitlab-monitor -f tag=v1.5.4` |
| Asset name changed in release.yml | update pattern in `update-homebrew.yml` (line ~28: `glmon-aarch64-apple-darwin`) |

See also: tap-wide `~/dev/homebrew-tap/RELEASING.md`.
