# Repository Guidelines

## Project Structure & Module Organization

- `app/`: FastAPI backend, event runtime, AI orchestration, tasks, memory, tools, and device WebSocket APIs.
- `watch-client/`: maintained Java Android/Wear OS client for the 360×480 watch; build with Gradle.
- `console/`: browser control plane (`index.html`, `app.js`, `style.css`).
- `tests/`: Python unit and integration tests.
- `docs/`: product requirements, architecture, activity contracts, watch runtime, and acceptance criteria.
- `deploy/`: service and reverse-proxy deployment files.


Keep runtime data, `.env`, APKs, ZIPs, Gradle caches, and Python caches out of source changes.

## Build, Test, and Development Commands

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m pytest -q
uvicorn app.main:app --reload --port 8787
Set-Location watch-client; .\gradlew.bat assembleDebug
```

Use the first commands to create the backend environment, `pytest` for the full suite, Uvicorn for local API/console development, and Gradle for the debug APK. The local console is at `http://127.0.0.1:8787/console/`.

## Coding Style & Naming Conventions

Use 4-space indentation in Python and Java. Follow existing type hints, dataclasses, and small service-layer methods. Use `snake_case` for Python names, `camelCase` for JavaScript/Java methods and variables, and `PascalCase` for classes. Prefer existing event, policy, tool, and database abstractions over new parallel paths. Keep comments short and explain only non-obvious decisions. Use `apply_patch` for manual edits.

## Testing Guidelines

Tests use `pytest`; name files `test_*.py` and test functions `test_*`. Add focused tests for API contracts, event idempotency, task transitions, device messages, and activity projections. Run `python -m pytest -q` before submitting backend changes. Android changes must at least pass `.\gradlew.bat assembleDebug`; real watch behavior requires later ADB validation.

## Commit & Pull Request Guidelines

Use Conventional Commit prefixes, for example `feat:`, `fix:`, `build:`, `docs:`, or `test:`. Keep each commit focused. Pull requests should describe behavior changes, affected modules, configuration requirements, tests run, and any device limitations. Include screenshots for UI changes and note when validation was not possible without the physical watch.

## Security & Configuration

Copy `.env.example` to `.env`; never commit tokens, model keys, databases, recordings, or device credentials. Backend APIs require the configured device token. Keep model-provider keys on the server, and preserve Policy, capability validation, audit logging, and explicit confirmation for high-risk or external actions.
