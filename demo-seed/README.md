# glmon-demo

Sample project used to generate screenshots for [gitlab-monitor](https://github.com/bearded-giant/gitlab-tools).

Tiny calculator package with a CI pipeline that exercises multiple stages, mixed job outcomes, and manual deploys — gives `glmon` something realistic to render.

## Pipeline shape

| Stage | Job | Notes |
|-------|-----|-------|
| lint | ruff | passes |
| lint | mypy | passes on `main`, fails on `feature/loose-typing` |
| build | package | passes, uploads artifact |
| build | docs | passes, slow (~8s) |
| test | unit | passes |
| test | integration | passes on `main`, fails on `feature/divide-bug` |
| test | coverage | passes |
| deploy | staging | manual |
| deploy | prod | manual, needs staging |

## Running locally

```bash
pip install pytest mypy ruff
pytest tests/
mypy --strict src/
ruff check src/
```
