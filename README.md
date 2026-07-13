# Centralized Documentation Orchestrator

This repository functions as a centralized documentation steward. On a schedule (or on demand), it scans your GitHub repositories and submits documentation updates for your `README.md` files via Pull Requests — you review and merge them, nothing is ever merged automatically.

By running this orchestrator on a schedule, you eliminate the need to maintain separate agentic workflow files in every single one of your repositories.

## How It Works

```mermaid
graph TD
    A[Schedule / Manual Trigger] --> B[Discover Repositories]
    B --> C[Loop through Repos]
    C --> D[Clone Repo to Temp Directory]
    D --> E[Scan Project Files & Manifests]
    E --> F[Generate README.md via LLM]
    F --> G{README changed?}
    G -- No --> H[Skip & Log]
    G -- Yes --> I[Push branch docs/readme-update]
    I --> J[Open PR via GitHub REST API]
```

The orchestrator never pushes directly to `main` and never merges anything itself — every change lands as a Pull Request on the target repo for you to review.

## Repository Structure

- `orchestrator.py`: The Python engine which discovers/clones repos, scans them, runs the LLM, and creates the PR.
- `repositories.json` *(optional)*: A fixed list of `owner/repo` strings to process. Only used if `--auto-discover` is **not** passed.
- `excluded_repositories.json` *(optional)*: A list of `owner/repo` strings to always skip, used only in `--auto-discover` mode (e.g. throwaway or private repos you never want auto-documented).
- `requirements.txt`: Python package dependencies.
- `.github/workflows/documentation-orchestrator.yml`: Scheduled GitHub Actions workflow.

---

## Choosing Repos: Auto-Discovery vs. a Fixed List

**Auto-discovery (default, recommended)** — the orchestrator asks GitHub for every repo your token's account owns, and automatically:
- Excludes this orchestrator's own repo (so it never rewrites its own README)
- Skips forks and archived repos (override with `--include-forks` / `--include-archived`)
- Skips anything listed in `excluded_repositories.json`, if that file exists

No file to maintain — add a new repo on GitHub, it's picked up on the next scheduled run automatically.

**Fixed list** — if you'd rather explicitly control exactly which repos get touched, omit `--auto-discover` and list them in `repositories.json`:
```json
[
  "your-github-username/my-first-project",
  "your-github-username/my-second-project"
]
```

## Setup Instructions

### 1. Configure GitHub Secrets in this Repo
Go to **Settings** ➔ **Secrets and variables** ➔ **Actions** in this orchestrator repository and add:

| Secret Name | Description | Scope |
|-------------|-------------|-------|
| `ORCHESTRATOR_PAT` | Personal Access Token (PAT) with `repo` scope | Required (so the runner can list, push branches to, and open PRs on your repos) |
| `GEMINI_API_KEY` | Google AI Studio API Key | Required if using Gemini |
| `ANTHROPIC_API_KEY`| Anthropic Console API Key | Required if using Claude |

> [!IMPORTANT]
> A custom PAT is required because the default `GITHUB_TOKEN` provided to actions cannot authenticate operations on other repositories. This secret only ever needs to exist **here**, in this orchestrator repo — target repos don't need any secrets of their own.

### 2. Choose your LLM Provider
The workflow will automatically auto-detect which API key is provided:
- If `GEMINI_API_KEY` is present, it uses Gemini (`gemini-3.5-flash`, via the current `google-genai` SDK).
- If `ANTHROPIC_API_KEY` is present, it uses Claude (`claude-sonnet-5`).
- Force a specific provider with `--provider gemini` / `--provider claude`, or via the `provider` input when running the workflow manually.

---

## Local Verification & Development

You can run the orchestrator locally to test it before pushing to GitHub.

### Setup environment:
```bash
# Install dependencies
pip install -r requirements.txt

# Export your credentials (bash/macOS/Linux)
export ORCHESTRATOR_PAT="your-github-token"
export GEMINI_API_KEY="your-gemini-key" # OR ANTHROPIC_API_KEY="your-claude-key"
```
On Windows PowerShell, use `$env:ORCHESTRATOR_PAT="your-github-token"` instead of `export`.

### Run in Dry-Run Mode (Recommended for testing)
Runs discovery, scan, and displays the generated README in your terminal *without* committing, pushing, or opening any Pull Requests:
```bash
python orchestrator.py --dry-run --auto-discover
```

### Run live update (force specific provider)
```bash
python orchestrator.py --auto-discover --provider gemini
```

### Run against a fixed list instead of auto-discovery
```bash
python orchestrator.py --config repositories.json
```

### Process a single repo only
```bash
python orchestrator.py --repo your-github-username/some-repo
```

---

## Reviewing Generated PRs

The orchestrator opens each PR on the **target repo**, not on this orchestrator repo — check that repo's own **Pull Requests** tab (e.g. `github.com/your-username/some-repo/pulls`), titled `docs: automated update of README.md`. Review the diff like any other PR, then merge (or close, and let the next scheduled run try again) — merging happens on GitHub, there's nothing to push manually from your machine.

> [!NOTE]
> The orchestrator force-pushes the `docs/readme-update` branch on every run. If you leave a PR open across multiple runs, the next run will overwrite it with a fresh AI draft rather than stacking changes — merge or close promptly if you've made manual edits you want to keep.
