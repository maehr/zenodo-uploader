# Agents Tooling Specification

Default Python tooling and conventions for agent codebases. Pin package versions in `pyproject.toml` and commit `uv.lock`. Dev tools must not ship to runtime.

## 1. Dev Tooling

Dev-only.

* **[uv](https://docs.astral.sh/uv/)** — deps, venvs, Python versions, resolver, lockfile.
* **[ruff](https://docs.astral.sh/ruff/)** — lint + format; replaces flake8/isort/black.
* **[ty](https://docs.astral.sh/ty/)** — static type checker; pin exactly while beta.
* **[pytest](https://docs.pytest.org/)** / **[pytest-cov](https://pytest-cov.readthedocs.io/)** — tests, doctests, coverage.
* **[prek](https://prek.j178.dev/)** — Rust hook manager; reads `.pre-commit-config.yaml` or `prek.toml`.
* **[commitizen](https://commitizen-tools.github.io/commitizen/)** (`cz`) — Conventional Commits + SemVer bumps.
* **[git-cliff](https://git-cliff.org/docs/)** — changelog generation from Conventional Commits.

## 2. Service / I/O

Runtime.

* **[FastAPI](https://fastapi.tiangolo.com/)** — server-side async APIs; not for browser/WASM serving.
* **[HTTPX2](https://github.com/pydantic/httpx2)** — Pydantic-maintained sync/async HTTP client. Package/import: `httpx2`.
* **[sqlite3](https://docs.python.org/3/library/sqlite3.html)** — stdlib embedded DB interface; no Python package pin.

## 3. Data / Models

* **[Pydantic](https://pydantic.dev/)** — validation, schemas, serialization; use v2.
* **[pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)** — typed config.
* **[pandas](https://pandas.pydata.org/docs/)** — tabular data.

## 4. Interaction / Orchestration

* **[Typer](https://typer.tiangolo.com/)** — CLIs from type hints.
* **[marimo](https://docs.marimo.io/)** — reactive notebooks/apps; WASM-capable with checks.

## 5. Observability

* **[structlog](https://www.structlog.org/)** — structured logging.

## 6. Visualization

* **[Altair / Vega-Altair](https://altair-viz.github.io/)** — declarative Vega-Lite charts.
* **[Matplotlib](https://matplotlib.org/stable/)** — general plotting.

## 7. Companion Specs

Optional; not Python runtime deps.

* **[DESIGN.md](https://github.com/google-labs-code/design.md)** — visual identity spec for UI projects. Node CLI: `npx @google/design.md`. Pin usage.

## 8. Standards & Governance

* **[SemVer 2.0.0](https://semver.org/)** — versioning; drives `cz bump`.
* **[Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/)** — commit format; feeds `cz` and `git-cliff`.
* **[Contributor Covenant 3.0](https://www.contributor-covenant.org/version/3/0/code_of_conduct/)** — default `CODE_OF_CONDUCT.md`.
* **[GNU AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.en.html)** — default `LICENSE`; prefer SPDX `AGPL-3.0-only`.

## Coding Guidelines

* Type hints everywhere; check with `ty`.
* Pydantic at I/O boundaries: APIs, tools, config, serialized state.
* Dataclasses for simple internal carriers.
* Prefer pure functions for core logic.
* Docstrings include runnable examples; test with:

```bash
pytest --doctest-modules
```

* Require 100% coverage on core logic:

```bash
pytest --cov --cov-fail-under=100
```

* Keep exclusions narrow and explicit.
* Enforce Conventional Commits with `cz` in a `prek` `commit-msg` hook.
* Generate changelogs with `git-cliff`.
* Manage hooks with `prek`, preferably `prek.toml`.

## Pyodide / Runtime Constraints

Separate **server** from **browser/WASM** targets. WASM rules apply only to browser-executed Python.

### Target Rules

* **Server:** FastAPI, HTTPX2, sqlite3, Pydantic, pandas, Typer, marimo, structlog, Altair, Matplotlib.
* **Browser/WASM:** pure Python or compatible Pyodide/PyPI WASM wheels only.
* **Never ship dev tools:** uv, ruff, ty, pytest, pytest-cov, prek, commitizen, git-cliff.

### WASM Candidates

Potentially compatible, subject to Pyodide/version checks:

* Pydantic
* pandas
* sqlite3
* Altair
* Matplotlib
* marimo
* structlog
* Typer

### Browser/WASM Networking

* Do not assume OS sockets, subprocesses, or threads.
* Do not run FastAPI as an in-browser server.
* Prefer `pyodide.http.pyfetch` or `pyxhr` for browser HTTP.
* Use HTTPX2 in WASM only with a tested adapter/custom transport.
