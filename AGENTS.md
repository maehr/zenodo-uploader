# Agents Tooling Specification

Default Python tooling for agent codebases. Pin versions in `pyproject.toml`, commit `uv.lock`. Dev tools never ship to runtime.

## 1. Orchestration

Rules for the agent that reads this file. Context is the scarce resource. Spend it like money.

**Model tier.** Frontier model orchestrates: holds the plan, the decisions, the shared context. Cheap small-window models do bounded subtasks. Pick the tier before you spawn, not after.

**Delegate on evidence.** A subagent starts cold and re-derives context. Delegate work that reads far more than it reports — broad search, log triage, fan-out across many files. Do small local edits inline. Estimate the cost first; state it when the call is close.

**Contracts, not conversations.** One task per subagent: the context it cannot infer, the output shape, the stop condition. Subagents return conclusions, never file dumps. Reports are claims — verify before acting.

**Parallel only when independent.** Concurrent agents only when no result feeds another. Cap fan-out at three.

**Context ladder.** Quarters of the usable window. At 25%, name the pressure source. At 50%, propose two reductions: offload to disk, delegate the reading, narrow the re-reads. At 75%, stop and reduce before further work.

**State lives on disk.** Plans, findings, and decisions go to files. Anything held only in the transcript dies at the next compaction.

**Read narrow.** Read the slice, not the file. Never re-read a file you just wrote.

## 2. Tooling

**Dev-only:** [uv](https://docs.astral.sh/uv/) deps/venvs/lockfile · [ruff](https://docs.astral.sh/ruff/) lint+format · [ty](https://docs.astral.sh/ty/) types, pin exactly while beta · [pytest](https://docs.pytest.org/) + [pytest-cov](https://pytest-cov.readthedocs.io/) tests/doctests/coverage · [prek](https://prek.j178.dev/) hooks, prefer `prek.toml` · [commitizen](https://commitizen-tools.github.io/commitizen/) (`cz`) commits + SemVer bumps · [git-cliff](https://git-cliff.org/docs/) changelog.

**Runtime:** [Pydantic](https://pydantic.dev/) v2 validation · [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) config · [FastAPI](https://fastapi.tiangolo.com/) async APIs, server-only · [HTTPX2](https://github.com/pydantic/httpx2) HTTP client, imports as `httpx2` · [sqlite3](https://docs.python.org/3/library/sqlite3.html) stdlib DB, no pin · [pandas](https://pandas.pydata.org/docs/) tables · [Typer](https://typer.tiangolo.com/) CLIs · [mcp](https://github.com/modelcontextprotocol/python-sdk) MCP servers, `FastMCP` over stdio · [marimo](https://docs.marimo.io/) reactive notebooks · [structlog](https://www.structlog.org/) logging · [Altair](https://altair-viz.github.io/) + [Matplotlib](https://matplotlib.org/stable/) charts.

## 3. Standards

[SemVer 2.0.0](https://semver.org/) drives `cz bump` · [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/) feeds `cz` and `git-cliff` · [Contributor Covenant 3.0](https://www.contributor-covenant.org/version/3/0/code_of_conduct/) as `CODE_OF_CONDUCT.md` · [AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.en.html) as `LICENSE`, SPDX `AGPL-3.0-only`.

## 4. Code

* Type hints everywhere. Pydantic at I/O boundaries (APIs, tools, config, serialized state), dataclasses for internal carriers, pure functions for core logic.
* Docstrings carry runnable examples. 100% coverage on core logic; keep exclusions narrow and explicit.
* `prek` runs the gate below plus a `cz` `commit-msg` hook. `git-cliff` writes the changelog.

```bash
ruff check . && ruff format --check .
ty check
pytest --doctest-modules --cov --cov-fail-under=100
```

## 5. GitHub Workflow

**Fork and pull.** No direct pushes upstream.

```bash
gh repo fork OWNER/REPO --clone
git switch -c feat/thing
gh pr create --repo OWNER/REPO
```

Allow maintainer edits, resync with `gh repo sync`, one logical change per PR, Conventional Commits title.

**Stacking.** Each PR targets the branch below it, the bottom one targets the trunk. Merge bottom-up; GitHub re-targets the rest. Requirements come from the trunk only. Use the `gh stack` extension. Stacks live in one repo, never across forks.

**Protect the trunk.** Both layers are idempotent — re-runs converge.

```bash
gh repo edit --enable-squash-merge --enable-merge-commit=false --enable-rebase-merge=false \
  --delete-branch-on-merge --allow-update-branch \
  --enable-secret-scanning --enable-secret-scanning-push-protection
gh api -X PUT repos/OWNER/REPO/branches/main/protection --input protection.json
```

`PUT` replaces the whole config; take the body shape from the [API reference](https://docs.github.com/en/rest/branches/branch-protection) rather than a stale snippet. Enforce: PR before merge, ≥1 approval, dismiss stale approvals on push, code-owner and last-push approval, conversation resolution, strict status checks, linear history, `enforce_admins: true`, no force-push, no deletion.

* Without `enforce_admins`, the rule is advisory for whoever is most likely to bypass it.
* Solo maintainer: 0 approvals — you cannot approve your own PR, and admin enforcement turns ≥1 into a deadlock.
* Free plan covers public repos; private needs Pro or above.
* Rulesets are the org-scale successor. `gh ruleset` only reads and creation is `POST`, so re-runs duplicate. Prefer the `PUT` for a single repo.

## 6. CI/CD Security

Baseline: [secure use of Actions](https://docs.github.com/en/actions/reference/security/secure-use).

* Top-level `permissions: contents: read`, widened per job only as needed. Default the repo token: `gh api -X PUT repos/OWNER/REPO/actions/permissions/workflow -f default_workflow_permissions=read`.
* Pin actions to full commit SHAs verified against the upstream repo, not a fork. Dependabot bumps them.
* Never check out fork code under `pull_request_target`. Prefer `workflow_run`; treat its artifacts as untrusted.
* Never interpolate `github.event.*` into `run:`. Pass via `env:` and quote `"$VAR"`.
* OIDC to short-lived cloud roles, no long-lived secrets. Secrets are scalars, never JSON blobs. Rotate them.
* Gate deploys on an Environment with required reviewers; environment secrets over repo secrets.
* No self-hosted runners on public repos.
* `CODEOWNERS` covers `.github/workflows/**`.
* Required checks: the §4 gate, CodeQL, `dependency-review-action`. Same commands locally via `prek`, so CI never surprises.

## 7. Browser / WASM

Applies only to browser-executed Python; server targets are unconstrained.

* Ship pure Python or verified Pyodide/PyPI WASM wheels. Never ship dev tools.
* Candidates, subject to version checks: everything under Runtime except FastAPI and HTTPX2.
* No OS sockets, subprocesses, or threads. No FastAPI in-browser.
* HTTP via `pyodide.http.pyfetch` or `pyxhr`. HTTPX2 only with a tested custom transport.
