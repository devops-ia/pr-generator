# PR generator

[![CI](https://github.com/devops-ia/pr-generator/actions/workflows/docker-build.yml/badge.svg)](https://github.com/devops-ia/pr-generator/actions/workflows/docker-build.yml)
[![GitHub release](https://img.shields.io/github/v/release/devops-ia/pr-generator)](https://github.com/devops-ia/pr-generator/releases)
[![Docker Hub](https://img.shields.io/docker/v/devopsiaci/pr-generator?label=Docker%20Hub&logo=docker)](https://hub.docker.com/r/devopsiaci/pr-generator)
[![Docker Pulls](https://img.shields.io/docker/pulls/devopsiaci/pr-generator?logo=docker)](https://hub.docker.com/r/devopsiaci/pr-generator)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Automated Pull Request creation daemon for **GitHub** and **Bitbucket Cloud**.

`pr-generator` runs as a long-lived service that periodically scans your repository branches, matches them against configurable regex patterns, and automatically opens Pull Requests toward the configured destination branches — skipping any PR that already exists.

---

## Table of Contents

- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Configuration](#configuration)
  - [YAML file](#yaml-file)
  - [Environment variables](#environment-variables)
- [Providers](#providers)
  - [GitHub — App authentication](#github--app-authentication)
  - [GitHub — PAT authentication](#github--pat-authentication)
  - [Bitbucket Cloud](#bitbucket-cloud)
- [Rules](#rules)
- [ArgoCD Image Updater integration](#argocd-image-updater-integration)
- [Health endpoints](#health-endpoints)
- [Docker](#docker)
- [Development](#development)
- [Troubleshooting](#troubleshooting)

---

## How it works

```
┌─────────────────────────────────────────────────────────────┐
│                        Scan cycle                           │
│                                                             │
│  1. Fetch all branches   ──▶  GitHub  /  Bitbucket          │
│  2. For every rule                                          │
│       match branches against regex pattern                  │
│       for each match                                        │
│         skip  if open PR already exists                     │
│         create PR  source ──▶ destination                   │
│  3. Sleep scan_frequency seconds                            │
│  4. Repeat                                                  │
└─────────────────────────────────────────────────────────────┘
```

Key design points:

- **Concurrent**: branches are fetched from all providers in parallel; rule×provider pairs are also processed concurrently (up to 10 workers).
- **Idempotent**: an existing open PR for the same source→destination pair is detected and skipped.
- **Dry-run mode**: log what would be created without actually calling the API.
- **Graceful shutdown**: handles `SIGTERM` / `SIGINT` and drains in-progress work.

---

## Quick start

```bash
# Install
pip install -e .

# Point to your config file and run
CONFIG_PATH=./config.yaml pr-generator
```

Or with Docker:

```bash
docker run --rm \
  -v "$(pwd)/config.yaml:/etc/pr-generator/config.yaml:ro" \
  ghcr.io/devops-ia/pr-generator:latest
```

---

## Configuration

### YAML file

The default config path is `/etc/pr-generator/config.yaml`. Override with the `CONFIG_PATH` environment variable. The application exits with an error at startup if the file is not found.

```yaml
# config.yaml

# How often (seconds) to scan for new branches.
scan_frequency: 300        # default: 300

# Logging level: DEBUG | INFO | WARNING | ERROR
log_level: INFO            # default: INFO

# Log format: "text" (human-readable) or "json" (structured, for log aggregators)
log_format: text           # default: text

# When true, PRs are logged but never actually created.
dry_run: false             # default: false

# Port for the built-in health server.
health_port: 8080          # default: 8080

providers:
  github:
    enabled: true
    owner: my-org
    repo: my-repo
    app_id: "123456"
    installation_id: "78901234"   # optional — auto-resolved if omitted
    private_key_path: /secrets/github-app.pem   # path to PEM file
    # Alternative: set GITHUB_APP_PRIVATE_KEY env var (plain PEM or base64-encoded)
    timeout: 30            # HTTP timeout in seconds

  bitbucket:
    enabled: true
    workspace: my-workspace
    repo_slug: my-repo
    token_env: BITBUCKET_TOKEN   # name of the env var that holds the token
    close_source_branch: true    # delete source branch after merge (default: true)
    timeout: 30

rules:
  - pattern: "feature/.*"          # Python regex matched against branch names
    destinations:
      github: main
      bitbucket: develop

  - pattern: "release/.*"
    destinations:
      github: main

  - pattern: ".*-hotfix-.*"
    destinations:
      bitbucket: master
```

#### Multiple GitHub organisations

Use any name as the provider key and set `type: github` (or `type: bitbucket`) to identify the implementation. Rules reference providers by their name.

```yaml
providers:
  github-acme:
    type: github          # required for non-standard key names
    enabled: true
    owner: acme-org
    repo: backend
    app_id: "111"
    private_key_path: /secrets/acme-app.pem

  github-skunkworks:
    type: github
    enabled: true
    owner: skunkworks-org
    repo: platform
    auth_method: pat
    token_env: SKUNKWORKS_GITHUB_TOKEN

  bitbucket:              # "github" / "bitbucket" keys default type automatically
    enabled: true
    workspace: my-workspace
    repo_slug: my-repo
    token_env: BITBUCKET_TOKEN

rules:
  - pattern: "feature/.*"
    destinations:
      github-acme: main
      github-skunkworks: develop
      bitbucket: develop
```

**Config fields reference**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `scan_frequency` | int | `300` | Seconds between scan cycles |
| `log_level` | string | `"INFO"` | Python logging level |
| `dry_run` | bool | `false` | Simulate PR creation without API calls |
| `health_port` | int | `8080` | Port for health HTTP server |
| `providers.<name>.type` | string | *(key name)* | Provider implementation: `github` or `bitbucket`. Required when the key name is not `github` or `bitbucket` |
| `providers.<name>.enabled` | bool | `false` | Activate this provider instance. If no providers are enabled the application starts in **idle mode** — it logs a warning and keeps running without performing any scans |
| `providers.<name>.owner` | string | — | GitHub organisation or user *(GitHub only)* |
| `providers.<name>.repo` | string | — | Repository name *(GitHub only)* |
| `providers.<name>.app_id` | string | — | GitHub App ID *(GitHub App auth)* |
| `providers.<name>.installation_id` | string | *(auto)* | Installation ID; resolved automatically if omitted *(GitHub App auth)* |
| `providers.<name>.private_key_path` | string | — | Path to GitHub App private key PEM file *(GitHub App auth)* |
| `providers.<name>.auth_method` | string | `"app"` | `app` (GitHub App) or `pat` (Personal Access Token) *(GitHub only)* |
| `providers.<name>.token_env` | string | `"GITHUB_TOKEN"` / `"BITBUCKET_TOKEN"` | Env var name containing the token *(PAT / Bitbucket)*. Must be **unique** across all enabled providers of the same type — duplicate values raise a `ValueError` at startup |
| `providers.<name>.workspace` | string | — | Bitbucket workspace slug *(Bitbucket only)* |
| `providers.<name>.repo_slug` | string | — | Bitbucket repository slug *(Bitbucket only)* |
| `providers.<name>.close_source_branch` | bool | `true` | Delete source branch after PR merges *(Bitbucket only)* |
| `providers.<name>.timeout` | float | `30` | HTTP timeout (seconds) |
| `rules[].pattern` | string | — | Python regex applied to branch names |
| `rules[].destinations` | map | — | `provider_name: destination_branch` pairs |

---

## Environment variables

| Variable | Description |
|----------|-------------|
| `CONFIG_PATH` | Path to the YAML config file. Default: `/etc/pr-generator/config.yaml` |
| `GITHUB_APP_PRIVATE_KEY` | GitHub App PEM key (plain text or base64-encoded). Used **only** when `private_key_path` is absent or empty in config — if `private_key_path` is set but the file does not exist, the application raises `FileNotFoundError` without falling back to this variable |
| `GITHUB_TOKEN` | Default token env var for GitHub PAT providers (`token_env: GITHUB_TOKEN`) |
| `BITBUCKET_TOKEN` | Default token env var for Bitbucket providers (`token_env: BITBUCKET_TOKEN`) |
| *any name* | Custom env var referenced by `token_env` in provider config |

---

## Providers

### GitHub App

Authentication uses a [GitHub App](https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/about-creating-github-apps). Two modes are available:

**GitHub App (recommended)** — the provider:
1. Signs a short-lived JWT with the App's RSA private key.
2. Exchanges it for an installation access token (cached up to ~55 minutes).
3. Uses the installation token for all API calls.
4. Caches per-cycle PR-existence and branch-existence lookups to reduce API usage.

**Personal Access Token (PAT)** — set `auth_method: pat` and point `token_env` at an env var holding the PAT.

Required GitHub App permissions: **Contents** (read), **Pull requests** (read & write).

### Bitbucket Cloud

Authentication uses a project/repository **Bearer token** (HTTP access token).

The provider fetches default reviewers at PR creation time and automatically includes them in the payload.

Required Bitbucket permissions: **Repositories** (read), **Pull requests** (read & write).

---

## Rules

Each rule has:

- **`pattern`** — a Python regex (`re.compile`) matched against branch names using `re.match` (anchored at the start). The destination branch is excluded from matching.
- **`destinations`** — a map of `provider_name → destination_branch`. Only providers that are both listed here **and** active in `providers` are processed.

```yaml
rules:
  - pattern: "feature/.*"
    destinations:
      github: main          # create PRs toward "main" on GitHub
      bitbucket: develop    # create PRs toward "develop" on Bitbucket
```

Multiple rules are supported.

---

## ArgoCD Image Updater integration

`pr-generator` pairs naturally with [Argo CD Image Updater](https://argocd-image-updater.readthedocs.io/).
Image Updater creates branches named `argocd-image-updater-set-<app>-<env>-<image>`.
Configure rules to catch those branches and open PRs toward the appropriate target branch per environment.

```yaml
scan_frequency: 120

providers:
  github:
    enabled: true
    owner: my-org
    repo: gitops-repo
    auth_method: app
    app_id: "123456"
    private_key_path: /secrets/github-app.pem

rules:
  - pattern: "argocd-image-updater-.*-dev-.*"
    destinations:
      github: develop

  - pattern: "argocd-image-updater-.*-staging-.*"
    destinations:
      github: staging

  - pattern: "argocd-image-updater-.*-pro-.*"
    destinations:
      github: main
```

---

## Health endpoints

A lightweight HTTP server starts on `health_port` (default `8080`):

| Endpoint | Behaviour |
|----------|-----------|
| `GET /livez` | `200 live` while running; `503 shutting down` during shutdown |
| `GET /healthz` | Same as `/livez` (alias) |
| `GET /readyz` | `200 ready` after the **first** scan cycle completes; `503 not ready` before that |

Suitable for Kubernetes liveness, readiness, and startup probes:

```yaml
livenessProbe:
  httpGet:
    path: /livez
    port: 8080
readinessProbe:
  httpGet:
    path: /readyz
    port: 8080
```

---

## Docker

The image is built from a two-stage Dockerfile:

- **Stage 1** – installs Python dependencies into `/install`.
- **Stage 2** – minimal `python:3.14-slim` runtime; runs as a non-root user (`prgen`).

```bash
# Build
docker build -t pr-generator .

# Run with YAML config
docker run --rm \
  -v "$(pwd)/config.yaml:/etc/pr-generator/config.yaml:ro" \
  -v "$(pwd)/github-app.pem:/secrets/github-app.pem:ro" \
  -e BITBUCKET_TOKEN=<token> \
  -p 8080:8080 \
  pr-generator
```

---

## Development

**Prerequisites**: Python ≥ 3.11

```bash
# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate

# Install the package in editable mode with dev extras
pip install -e .
pip install pytest

# Run tests
pytest

# Run with a local config
CONFIG_PATH=./config.yaml python -m pr_generator
```

**Project layout**

```
src/pr_generator/
├── __main__.py          # Entry point: startup, provider init, scan loop
├── config.py            # Config loading from YAML file
├── models.py            # Dataclasses: AppConfig, ProviderConfig, ScanRule, …
├── scanner.py           # Concurrent scan cycle orchestrator
├── health.py            # HTTP health server (/livez, /readyz, /healthz)
├── http_client.py       # Shared HTTP client with retry/backoff
├── logging_config.py    # Logging setup (plain text or structured JSON)
└── providers/
    ├── base.py          # ProviderInterface Protocol
    ├── github.py        # GitHub App provider
    └── bitbucket.py     # Bitbucket Cloud provider

tests/
├── conftest.py          # Shared pytest fixtures
├── test_config.py       # Config loading tests
├── test_health.py       # Health server tests
├── test_models.py       # Model tests
└── test_scanner.py      # Scan cycle tests
```

---

## Troubleshooting

### Application exits with `FileNotFoundError`

```
FileNotFoundError: [Core] private_key_path '/secrets/github-app.pem' does not exist.
```

`private_key_path` is set in `config.yaml` but the file is not present at that path.
Either mount the PEM file at the configured path, or remove `private_key_path` from
the config and set the `GITHUB_APP_PRIVATE_KEY` environment variable instead.

### `ValueError: duplicate tokenEnv`

```
ValueError: [Core] Providers 'bb-eu' and 'bb-us' both use tokenEnv 'BITBUCKET_TOKEN'.
```

Two enabled providers of the same type share the same `token_env` value. Assign a
unique env var name to each provider and export the corresponding variable in your
runtime environment.

### `/readyz` returns `503`

This is expected during startup. The endpoint returns `503 not ready` until the first
full scan cycle completes. If it never flips to `200`, check the application logs for
errors in the scan cycle (API auth failures, missing config fields, network issues).

### No PRs are created (dry_run is false, branches exist)

1. **Regex anchoring** — rules use `re.match`, which is anchored at the start of the
   string. A pattern `feature/.*` will **not** match `hotfix/feature/x`. Enable
   `log_level: DEBUG` to see per-branch matching decisions.
2. **Provider name mismatch** — the name in `rules[].destinations` must exactly match
   the provider key under `providers:`.
3. **Destination branch excluded** — pr-generator skips branches whose name equals the
   destination branch to avoid self-targeting PRs.

### GitHub App: `RuntimeError: Could not resolve installation id`

Set `installation_id` explicitly in the provider config (find it in your GitHub App
settings under _Installations_), or ensure the GitHub App is installed on the target
repository.

