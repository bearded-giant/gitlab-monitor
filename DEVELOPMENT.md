<!-- caveman:compressed -->
# Development

Internal notes. End-users: see `README.md`.

## Demo project for screenshots

All README screenshots come from synthetic gitlab.com project. Real work data never leaves work GitLab.

**Demo repo:** `git@gitlab.com:beardedgiant-oss/glmon-demo.git` ([web](https://gitlab.com/beardedgiant-oss/glmon-demo))

Tiny calculator package + multi-stage CI w/ mixed outcomes + 3 MRs (passing / draft-failing / typing-fail).

## Bootstrap fresh demo repo

1. Create empty `glmon-demo` on gitlab.com (public, no init files).
2. Generate PAT on gitlab.com — scopes: `api`, `read_repository`, `write_repository`.
3. Clone + seed:
   ```bash
   git clone git@gitlab.com:<USER>/glmon-demo.git ~/Desktop/glmon-demo
   cd ~/Desktop/glmon-demo
   cp -r /path/to/gitlab-monitor/demo-seed/. .
   git add . && git commit -m "initial: calculator + ci pipeline"
   git push -u origin main
   ```
4. Wait for `main` pipeline to go green (~1min, gitlab.com shared runners).
5. Seed MRs:
   ```bash
   GITLAB_TOKEN=<gl.com PAT> GITLAB_HOST=gitlab.com ./seed-mrs.sh
   ```

Result: 3 MRs (!1 passing, !2 draft+failing, !3 typing-fail), `main` w/ green pipeline + manual deploy jobs.

## Capture screenshots

```bash
GITLAB_URL=https://gitlab.com \
GITLAB_TOKEN=<gl.com PAT> \
GITLAB_PROJECT=<USER>/glmon-demo \
glmon
```

Conventions:

- **Terminal size:** 180x50 (or whatever fits content + breadcrumb)
- **Theme:** Catppuccin Mocha (glmon default)
- **Save to:** `glmon-assets/<Purpose-Description>.png`
- **Naming:** `Pipeline-list.png`, `MR-detail.png`, `Quick-status-toggle.png` — describe content, not screen class
- **Format:** PNG, no transparency, scale 1x (Retina = 2x is fine, GitHub downscales)

Trigger fresh "running" pipeline state right before shot:

```bash
cd ~/Desktop/glmon-demo
git commit --allow-empty -m "rerun ci" && git push
```

Gives ~20-30s window of pending → running before jobs finish.

## Refresh README references

After dropping new PNGs in `glmon-assets/`, update `README.md`:

- Hero img near top (HTML `<p align="center">` block, width 900)
- Inline section imgs use markdown `![alt](glmon-assets/file.png)`
- Verify no orphans: `for f in glmon-assets/*.png; do grep -q "$(basename $f)" README.md || echo "UNREF: $f"; done`

## Demo seed maintenance

Seed files live in [`demo-seed/`](demo-seed/). Idempotent on first run only — re-running against populated repo fails on non-fast-forward push. Manual cleanup:

```bash
cd ~/Desktop/glmon-demo
git checkout main
for b in feature/add-modulo feature/divide-bug feature/loose-typing; do
  git branch -D "$b" 2>/dev/null
  git push origin --delete "$b" 2>/dev/null
done
# also close any open MRs via glab or web UI
```

Then re-run `./seed-mrs.sh`.

## Token rotation

Demo PAT only needs gitlab.com scope. Rotate if leaked:

1. gitlab.com → User Settings → Access Tokens → revoke
2. Generate fresh PAT
3. Update local `.envrc.demo` (gitignored — never commit real token)

## Release

Bump version + tag via `release.sh`:

```bash
./release.sh 1.5.4
```

Pushes commit + tag to `origin` (github) only. Mirror to gitlab work remote manually:

```bash
git push gitlab main v1.5.4
```

## Repo layout

| Path | Purpose |
|------|---------|
| `gitlab_monitor/tui.py` | Single-file TUI source (~800 LOC) |
| `gitlab_monitor/config.py` | Config + favorites + MR notes persistence |
| `glmon-assets/` | README screenshot PNGs |
| `demo-seed/` | Bootstrap files for synthetic gitlab.com demo project |
| `release.sh` | Version bump + tag + push (origin only) |
| `Makefile` | `install`, `install-dev`, `dev`, `version` targets |
