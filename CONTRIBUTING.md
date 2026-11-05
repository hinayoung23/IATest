# Contributing to IATest

Use Python 3.11 or newer and install the existing optional development dependencies:

```sh
python -m pip install -e ".[test,database,hid]"
python -m pytest -q
```

CI and release builds use `uv.lock` and the hash-checked exports in `requirements/`. For the same environment, install `requirements/ci.txt` with `--require-hashes`, then install this checkout with `--no-deps --no-build-isolation`. When updating dependencies, refresh the lock and both exports together. Run `python -m ruff check .`, `python -m mypy`, and the critical-module coverage checks described in `docs/guides/deterministic-ci.md` before release. Benchmark outputs include the measured environment; compare only matching environments.

## Source layout

| Directory | Purpose |
|---|---|
| `iatest/application/` | Goal exploration, asset inventory, workflows and application services |
| `iatest/engines/` | Typed API, UI, performance, security, data and protocol execution |
| `iatest/mobile/` | Native Android/iOS drivers, observation, input and collectors |
| `iatest/nodes/` | Authorized workers, controller and resource coordination |
| `iatest/web/` | Console routes, AI workspace, asset library, templates and static resources |
| `iatest/agents/`, `ai/`, `context/` | Model roles, provider integration and project knowledge |
| `tests/` | Regression tests and minimal fixtures; no personal device recordings |
| `examples/plans/` | Editable execution plans grouped by explicit filenames |
| `examples/devices/` | Device and emulator profile templates |
| `examples/measurements/`, `examples/hid/` | Collector and HID configuration examples |
| `scripts/` | Build tools and opt-in integration acceptance |
| `docs/guides/`, `reference/`, `design/` | Usage, contracts/boundaries and implementation design |

API-specific modules retain API names. Shared execution and UI modules use names appropriate to the whole framework. The console templates `api_contracts.html`, `api_cases.html` and `api_reports.html` explicitly identify the API workflow; `overview.html`, `settings.html` and `project_knowledge.html` are shared pages.

## Validation

Run the smallest relevant regressions during development, then the full suite for release. Set `IATEST_CHROMIUM_PATH` to an installed browser for native browser integration. Native platforms require the tools, permissions and hardware described in the environment guide. Keep fixture-based tests distinct from physical-device acceptance.

Build wheel and sdist, validate their metadata, then run `scripts/validate_package_install.py` using a clean environment with only the wheel installed. The script checks CLI startup, a process worker, optional dependency loading and packaged console resources. CI also exercises supported Python versions and Linux/macOS/Windows.

## Data and credentials

Keep generated projects, `.iatest` state, recordings, signatures, credentials, native build products, virtual environments and local configuration outside commits. Acceptance scripts should use temporary or explicitly owned disposable projects. Preserve test fixtures that are required to reproduce contract behavior.

Use environment-variable references for credentials. Do not add production dependencies or change established execution/authorization semantics without explaining and reviewing the change.
