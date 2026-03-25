# Copilot Instructions — pr-generator

## Commands

```bash
# Install (editable, no dev extras needed)
pip install -e .
pip install pytest

# Run full test suite
python -m pytest

# Run a single test file
python -m pytest tests/test_scanner.py -v

# Run a single test by name
python -m pytest tests/test_scanner.py::TestScanCycle::test_dry_run_does_not_create_prs -v

# Run the application locally
CONFIG_PATH=./config.yaml python -m pr_generator

# Run tests with coverage (configured in pyproject.toml)
python -m pytest --cov=pr_generator --cov-report=term-missing
```

There is no linter configured. There is no type-checker configured.

---

## Architecture

`pr-generator` is a long-running polling daemon. The main loop lives in `__main__.py`:

1. Load `AppConfig` from YAML (`CONFIG_PATH`) or legacy env vars (fallback).
2. Instantiate active providers (`GitHubProvider` / `BitbucketProvider`).
3. Start the health HTTP server in a daemon thread.
4. Loop: run `scan_cycle()` → sleep `scan_frequency` seconds → repeat.
5. Graceful shutdown on `SIGTERM`/`SIGINT` via a `threading.Event`.

**Scan cycle** (`scanner.py`) is two-phase, both phases concurrent via `ThreadPoolExecutor`:
- **Phase 1**: fetch all branch names from every active provider in parallel.
- **Phase 2**: for each `rule × provider` pair — filter branches by regex, check for existing PRs, create missing ones.

**Config loading** (`config.py`) priority: YAML file → legacy env vars. YAML supports multiple named providers and multiple rules. Legacy env-var mode supports exactly one rule.

**Provider abstraction** — `ProviderInterface` is a `runtime_checkable` Protocol in `providers/base.py`. Both `GitHubProvider` and `BitbucketProvider` satisfy it structurally (no explicit inheritance). The scanner only uses the interface.

**All HTTP** goes through `request_with_retry` in `http_client.py`. It handles retry/backoff (delays: 0.5 s, 1 s, 2 s) and logging. Providers never call `requests` directly.

**Releases** are automated via `semantic-release` on push to `main`. Version is in `src/pr_generator/__init__.py` and `pyproject.toml`.

---

## Key Conventions

### Logging format
All log lines follow the structured pattern:
```
[Component] Step: step_name action=verb cycle_id=N detail=...
```
Examples: `[GitHub] Step: get_branches action=end total=42`, `[Core] Step: scan_cycle action=start cycle_id=3`.

### `request_with_retry` — `headers` vs `headers_factory`
Pass **`headers`** (a plain dict) when auth tokens don't expire between retries (Bitbucket Bearer token).  
Pass **`headers_factory`** (a `() → dict` callable) when tokens may rotate between attempts (GitHub App installation tokens). The factory is called fresh on each retry attempt, so a token refresh is picked up automatically.

### Provider exceptions must carry `status_code`
Both `GitHubError` and `BitbucketError` have the constructor signature:
```python
def __init__(self, message: str, status_code: int | None = None) -> None:
```
`http_client.request_with_retry` calls `exception_cls(message, status_code)`. Any new provider exception class must match this signature.

### Per-cycle caches
Each provider caches PR-existence and branch-existence lookups within one scan cycle. `reset_cycle_cache()` is called at the start of every cycle. Do not persist cache state across cycles.

### Rule matching uses `re.match` (start-anchored)
Patterns are matched with `rule.compiled.match(branch_name)`, not `re.search`. Patterns must match from the beginning of the branch name.

### `AppConfig` and `ProviderConfig` are frozen dataclasses
Neither can be mutated after construction. In tests, build a new instance rather than modifying fields.

### New provider checklist
To add a third provider (e.g. GitLab):
1. Create `src/pr_generator/providers/gitlab.py` implementing all 5 methods of `ProviderInterface`.
2. Define `GitLabError(Exception)` with `(message: str, status_code: int | None = None)`.
3. Add `"gitlab"` to the `ptype` allowlist in `config._parse_providers_from_yaml`.
4. In `_request`, pass `headers=` if tokens are static or `headers_factory=` if they refresh mid-cycle.
5. Add a `_parse_gitlab_provider` function and wire it in `__main__.py`.
6. Add tests in `tests/test_providers.py`.

### Testing patterns
- **Scanner tests** — mock full providers with `MagicMock()` (see `_mock_provider` helper in `test_scanner.py`).
- **Provider tests** — mock `provider._request` directly, not `requests.request`.
- **Config tests** — use `tmp_path` fixture + `monkeypatch.setenv("CONFIG_PATH", path)`.
- Tests are plain classes with descriptive method names; no pytest markers are used.

### Docker
Config is mounted at `/etc/pr-generator/config.yaml` (the default `CONFIG_PATH`). The container runs as non-root user `prgen`. `requirements.txt` drives the Docker build; `pyproject.toml` is the authoritative dependency source — keep both in sync when adding dependencies.
