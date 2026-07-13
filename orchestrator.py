#!/usr/bin/env python3
import os
import re
import sys
import json
import shutil
import subprocess
import tempfile
import argparse
from pathlib import Path

# Try importing LLM libraries
try:
    from google import genai as google_genai
    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False

try:
    from anthropic import Anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

# Try importing PyGithub
try:
    from github import Github
    from github.GithubException import GithubException
    HAS_PYGITHUB = True
except ImportError:
    HAS_PYGITHUB = False


def parse_args():
    parser = argparse.ArgumentParser(description="Centralized README Maintainer Orchestrator")
    parser.add_argument(
        "--config",
        type=str,
        default="repositories.json",
        help="Path to the JSON file listing repositories"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without pushing branches or opening Pull Requests"
    )
    parser.add_argument(
        "--provider",
        type=str,
        choices=["gemini", "claude"],
        default=None,
        help="Explicitly choose LLM provider (gemini or claude). Defaults to auto-detect."
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Process only this single 'owner/repo' instead of the full config list "
             "(used by push-triggered runs via repository_dispatch)."
    )
    parser.add_argument(
        "--auto-discover",
        action="store_true",
        help="Ignore repositories.json and instead ask GitHub for every repo owned "
             "by the PAT's account. The orchestrator's own repo is always excluded. "
             "Forks and archived repos are excluded by default (see --include-forks "
             "and --include-archived)."
    )
    parser.add_argument(
        "--include-forks",
        action="store_true",
        help="When used with --auto-discover, also include forked repositories."
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="When used with --auto-discover, also include archived repositories."
    )
    parser.add_argument(
        "--exclude-config",
        type=str,
        default="excluded_repositories.json",
        help="When used with --auto-discover, path to an optional JSON list of "
             "'owner/repo' strings to always skip (e.g. repos you never want "
             "auto-documented). Fine if this file doesn't exist."
    )
    return parser.parse_args()


def discover_repositories(github_client, include_forks: bool, include_archived: bool, exclude_config: str) -> list:
    """Lists every repo owned by the authenticated PAT's account, minus this
    orchestrator's own repo and anything in the optional exclude list."""
    user = github_client.get_user()
    orchestrator_repo_name = None
    # GITHUB_REPOSITORY is set automatically by GitHub Actions to "owner/repo"
    # for whichever repo the workflow is running in.
    orchestrator_repo_name = os.environ.get("GITHUB_REPOSITORY")

    excluded = set()
    if os.path.exists(exclude_config):
        try:
            with open(exclude_config, "r") as f:
                excluded = set(name.strip() for name in json.load(f))
        except Exception as e:
            print(f"Warning: could not parse '{exclude_config}': {e}")

    discovered = []
    for repo in user.get_repos(affiliation="owner"):
        if orchestrator_repo_name and repo.full_name == orchestrator_repo_name:
            continue
        if repo.full_name in excluded:
            continue
        if repo.fork and not include_forks:
            continue
        if repo.archived and not include_archived:
            continue
        discovered.append(repo.full_name)

    return discovered


def redact_token(text: str, token: str) -> str:
    """Strips a secret token out of any text before it gets logged."""
    if not text or not token:
        return text
    return text.replace(token, "***REDACTED***")


def get_llm_provider(explicit_provider):
    if explicit_provider:
        if explicit_provider == "gemini":
            if not HAS_GEMINI:
                print("Error: google-generativeai package not installed.")
                sys.exit(1)
            if not os.environ.get("GEMINI_API_KEY"):
                print("Error: --provider gemini was specified but GEMINI_API_KEY is not set.")
                sys.exit(1)
        if explicit_provider == "claude":
            if not HAS_ANTHROPIC:
                print("Error: anthropic package not installed.")
                sys.exit(1)
            if not os.environ.get("ANTHROPIC_API_KEY"):
                print("Error: --provider claude was specified but ANTHROPIC_API_KEY is not set.")
                sys.exit(1)
        return explicit_provider

    # Auto-detect based on env vars and packages
    if os.environ.get("GEMINI_API_KEY") and HAS_GEMINI:
        return "gemini"
    elif os.environ.get("ANTHROPIC_API_KEY") and HAS_ANTHROPIC:
        return "claude"

    print("Error: No LLM provider could be configured. Ensure GEMINI_API_KEY or ANTHROPIC_API_KEY is set and package is installed.")
    sys.exit(1)


def scan_directory(base_path: Path, max_depth: int = 3, current_depth: int = 1) -> str:
    """Recursively lists files and directories up to max_depth, ignoring common patterns."""
    ignored_dirs = {
        ".git", "node_modules", "venv", ".venv", "__pycache__", "target",
        "dist", "build", ".gemini", ".agents", ".github", ".vscode", ".idea"
    }

    tree_lines = []
    try:
        items = sorted(base_path.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        for item in items:
            if item.is_dir() and item.name in ignored_dirs:
                continue

            indent = "  " * (current_depth - 1)
            if item.is_dir():
                tree_lines.append(f"{indent}📁 {item.name}/")
                if current_depth < max_depth:
                    subtree = scan_directory(item, max_depth, current_depth + 1)
                    if subtree:
                        tree_lines.append(subtree)
            else:
                tree_lines.append(f"{indent}📄 {item.name}")
    except Exception as e:
        return f"Error scanning directory {base_path}: {e}"

    return "\n".join(tree_lines)


def read_manifest_files(repo_path: Path) -> dict:
    """Finds and reads key manifest files to understand the project dependencies/stack."""
    manifests = {}
    manifest_names = [
        "package.json", "Cargo.toml", "requirements.txt", "go.mod",
        "Gemfile", "pyproject.toml", "pom.xml", "build.gradle",
        "setup.py", "composer.json"
    ]

    for name in manifest_names:
        file_path = repo_path / name
        if file_path.is_file():
            try:
                # Read first 150 lines of manifests to avoid token bloat
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = "".join([f.readline() for _ in range(150)])
                manifests[name] = content
            except Exception as e:
                manifests[name] = f"Error reading file: {e}"

    return manifests


def read_sample_source_files(repo_path: Path) -> dict:
    """Reads portions of primary source files to understand purpose/APIs."""
    samples = {}
    # Common entry points and source directories.
    # Note: "src/main/java" is intentionally omitted here - it's a directory,
    # not a file, so an is_file() check can never match it. Detecting a
    # representative Java entrypoint would need a glob (e.g. **/Main.java)
    # rather than a fixed path, which is out of scope for this simple sampler.
    common_patterns = [
        "src/index.ts", "src/index.js", "src/main.rs", "main.py", "app.py",
        "src/app.ts", "src/main.ts", "index.js", "server.js"
    ]

    for pattern in common_patterns:
        file_path = repo_path / pattern
        if file_path.is_file():
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    # Get first 100 lines for sample
                    samples[pattern] = "".join([f.readline() for _ in range(100)])
            except Exception as e:
                print(f"  Warning: could not read sample file '{pattern}': {e}")

    return samples


def generate_readme_with_gemini(prompt: str) -> str:
    # Migrated from the deprecated `google.generativeai` package to the
    # current `google.genai` SDK (client.interactions.create). The old
    # package has ended all support - see:
    # https://github.com/google-gemini/deprecated-generative-ai-python
    client = google_genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    interaction = client.interactions.create(
        model="gemini-3.5-flash",
        input=prompt,
        generation_config={"temperature": 0.2},
    )
    return interaction.output_text


def generate_readme_with_claude(prompt: str) -> str:
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    # claude-3-5-sonnet-20241022 is a legacy model ID. Updated to a current
    # model string; check https://docs.claude.com for the latest available IDs.
    message = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=4000,
        temperature=0.2,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text


def generate_readme(provider: str, repo_name: str, directory_tree: str, manifests: dict, samples: dict, current_readme: str) -> str:
    """Generates the README content using the configured LLM provider."""

    manifests_str = ""
    for name, content in manifests.items():
        manifests_str += f"\n--- File: {name} ---\n{content}\n"

    samples_str = ""
    for path, content in samples.items():
        samples_str += f"\n--- File: {path} ---\n{content}\n"

    prompt = f"""
You are an expert Documentation Engineer and Technical Writer. Your goal is to draft or update the root README.md for the repository: '{repo_name}'.

Here is the context of the repository:

### Directory Tree:
```
{directory_tree}
```

### Manifest / Configuration Files:
{manifests_str if manifests_str else "No common manifest files found."}

### Code Samples (Main entrypoints):
{samples_str if samples_str else "No common code samples found."}

### Current README.md Content:
```markdown
{current_readme if current_readme else "(Empty or missing README.md)"}
```

---

### Instructions:
Generate a complete, high-quality, and modern README.md for this repository.
The README MUST include:
1. **Project Purpose & Overview**: A clear, concise description of what the project does and why it exists.
2. **Tech Stack**: A clean, formatted list/table of the main programming languages, frameworks, major libraries, databases, and tooling used (deduced from the folder structure and manifests).
3. **Getting Started & Installation**:
   - Prerequisites (e.g. Node.js version, Python version, Rust toolchain, Docker, etc.).
   - Installation commands (e.g. dependencies setup, build commands).
   - How to run or execute the project (e.g. how to start dev servers, start scripts, or run unit tests).
4. **Usage / Quick Start**: Practical code examples, configuration setups, or CLI command invocations showing how to use the project.
5. **License**: A short section stating the project license if you detect a license, or leave a placeholder.

### Formatting & Style Rules:
- Output ONLY the raw markdown content of the README.md. Do not include introductory text, explanations, or enclosing triple-backticks (other than the markdown code itself).
- Ensure the tone is professional, technical, and developer-friendly.
- Preserve custom elements or custom headers from the Current README if they contain specific context that cannot be derived from code scanning (such as customized deployment instructions, badges, custom logos, or project credits). Integrate them seamlessly.
"""

    if provider == "gemini":
        return generate_readme_with_gemini(prompt)
    elif provider == "claude":
        return generate_readme_with_claude(prompt)
    else:
        raise ValueError(f"Unknown provider: {provider}")


def handle_repository(repo_full_name: str, token: str, provider: str, dry_run: bool):
    print(f"\nProcessing repository: {repo_full_name}")

    # Authenticate with PyGithub if not dry-run
    github_client = None
    if not dry_run:
        if not HAS_PYGITHUB:
            print("Error: PyGithub package is not installed. PR creation will be skipped. Run in --dry-run or install PyGithub.")
            return
        github_client = Github(token)

    # Create temp directory
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # Clone repo
        # Use token in URL for auth
        clone_url = f"https://x-access-token:{token}@github.com/{repo_full_name}.git"
        print(f"Cloning {repo_full_name} into temporary workspace...")

        try:
            subprocess.run(
                ["git", "clone", clone_url, temp_dir],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True
            )
        except subprocess.CalledProcessError as e:
            # IMPORTANT: git often echoes the remote URL (which contains the
            # token) back in its error output. Redact it before printing/logging,
            # otherwise the PAT can leak into CI logs.
            safe_stderr = redact_token(e.stderr.decode(errors="ignore").strip(), token)
            print(f"Failed to clone repository: {safe_stderr}")
            return

        # Check current README
        readme_path = None
        current_readme_content = ""
        for name in ["README.md", "readme.md", "README.markdown"]:
            p = temp_path / name
            if p.is_file():
                readme_path = p
                try:
                    with open(p, "r", encoding="utf-8", errors="ignore") as f:
                        current_readme_content = f.read()
                except Exception as e:
                    print(f"Failed to read existing README: {e}")
                break

        if not readme_path:
            readme_path = temp_path / "README.md"

        # Scan repo
        print("Scanning repository structure and manifests...")
        directory_tree = scan_directory(temp_path, max_depth=3)
        manifests = read_manifest_files(temp_path)
        samples = read_sample_source_files(temp_path)

        # Generate new README
        print("Calling LLM to generate README.md...")
        try:
            generated_readme = generate_readme(
                provider, repo_full_name, directory_tree, manifests, samples, current_readme_content
            ).strip()
        except Exception as e:
            print(f"LLM generation failed: {e}")
            return

        # Compare
        if current_readme_content.strip() == generated_readme.strip():
            print("No documentation changes detected. Skipping PR creation.")
            return

        print("README changes detected.")

        if dry_run:
            print("--- DRY RUN: Generated README.md ---")
            print(generated_readme[:1000] + "\n... (truncated for brevity) ...")
            print("-----------------------------------")
            return

        # Write back changes
        with open(readme_path, "w", encoding="utf-8") as f:
            f.write(generated_readme)

        # Create branch and push
        branch_name = "docs/readme-update"
        print(f"Creating branch '{branch_name}' and committing changes...")
        try:
            # Configure git for actions runner
            subprocess.run(["git", "config", "user.name", "github-actions[bot]"], cwd=temp_dir, check=True)
            subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], cwd=temp_dir, check=True)

            # Commit and push
            subprocess.run(["git", "checkout", "-b", branch_name], cwd=temp_dir, check=True)
            subprocess.run(["git", "add", "README.md"], cwd=temp_dir, check=True)
            subprocess.run(["git", "commit", "-m", "docs: automated README.md update"], cwd=temp_dir, check=True)
            subprocess.run(
                ["git", "push", "origin", branch_name, "--force"],
                cwd=temp_dir,
                check=True,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as e:
            safe_stderr = redact_token((e.stderr or b"").decode(errors="ignore").strip(), token)
            print(f"Git execution failed: {safe_stderr or e}")
            return

        # Open PR using PyGithub
        print("Opening Pull Request...")
        try:
            repo = github_client.get_repo(repo_full_name)

            # Check if PR already exists
            existing_prs = repo.get_pulls(state="open", head=f"{repo.owner.login}:{branch_name}")
            if existing_prs.totalCount > 0:
                print(f"A Pull Request for branch '{branch_name}' is already open. Skipping creation.")
                return

            pr = repo.create_pull(
                title="docs: automated update of README.md",
                body="This automated PR was generated by your centralized Documentation Orchestrator.\n\nIt analyzes the codebase, libraries, and manifest files to keep the README installation, tech stack, and description updated.",
                head=branch_name,
                base=repo.default_branch
            )
            print(f"Successfully created PR #{pr.number}: {pr.html_url}")
        except GithubException as e:
            print(f"Failed to create Pull Request via GitHub API: {e.data.get('message', str(e))}")
        except Exception as e:
            print(f"Failed to create Pull Request: {e}")


def main():
    args = parse_args()

    # Get authentication token
    token = os.environ.get("ORCHESTRATOR_PAT") or os.environ.get("GITHUB_TOKEN")
    if not token and not args.dry_run:
        print("Error: ORCHESTRATOR_PAT or GITHUB_TOKEN environment variable is required when not running in --dry-run.")
        sys.exit(1)

    if args.auto_discover:
        # Auto-discovery needs the GitHub API even in dry-run mode, since
        # there's no local list of repos to fall back on.
        if not token:
            print("Error: --auto-discover requires ORCHESTRATOR_PAT or GITHUB_TOKEN to list repos, even in --dry-run.")
            sys.exit(1)
        if not HAS_PYGITHUB:
            print("Error: PyGithub package is required for --auto-discover.")
            sys.exit(1)
        print("Auto-discovering repositories via GitHub API...")
        discovery_client = Github(token)
        repositories = discover_repositories(
            discovery_client, args.include_forks, args.include_archived, args.exclude_config
        )
        print(f"Discovered {len(repositories)} repo(s): {', '.join(repositories) if repositories else '(none)'}")
    elif args.repo:
        # Single-repo mode: process only this repo instead of a full list.
        repositories = [args.repo]
    else:
        # Read config
        if not os.path.exists(args.config):
            print(f"Error: Config file '{args.config}' not found.")
            sys.exit(1)

        try:
            with open(args.config, "r") as f:
                repositories = json.load(f)
        except Exception as e:
            print(f"Error parsing config file: {e}")
            sys.exit(1)

        if not isinstance(repositories, list):
            print("Error: Config file must contain a JSON list of repository strings.")
            sys.exit(1)

    if not repositories:
        print("No repositories to process. Exiting.")
        return

    # Determine LLM provider
    provider = get_llm_provider(args.provider)
    print(f"Using LLM Provider: {provider.upper()}")
    if args.dry_run:
        print("Running in DRY-RUN mode (no commits, pushes, or PRs will be made).")

    for repo in repositories:
        try:
            handle_repository(repo.strip(), token, provider, args.dry_run)
        except Exception as e:
            print(f"Unexpected error processing {repo}: {e}")

    print("\nOrchestrator run complete.")


if __name__ == "__main__":
    main()
