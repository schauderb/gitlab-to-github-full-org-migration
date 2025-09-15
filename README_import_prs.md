# GitLab → GitHub: Import Merge Requests (PRs) + Comments

This tool **backfills pull request history and comments** from GitLab into GitHub **after** code has already been migrated. It creates GitHub PRs that mirror GitLab MRs, copies comments, labels, and milestones where possible, and closes/merges PRs to reflect historical state. It is **idempotent**—imports are tagged and skipped on re-run.

## What it does

- For every project under a GitLab top group:
  - Creates a GitHub PR for each GitLab MR (open/closed/merged).
  - Preserves original author, timestamps, and URL **in the PR body** (GitHub API can’t set historical timestamps/authors).
  - Copies MR **labels** and **milestone** (creates them if missing).
  - Copies MR **notes** as PR comments (optionally include system notes).
  - Attempts to **merge** the PR if the MR was merged in GitLab; if not possible, it is **closed** with context.
  - Skips already-imported PRs using a body marker: `[Imported-from-GitLab: project_id=XXX iid=YYY]`.

> Inline/file-positioned comments are imported as regular PR comments to avoid fragile diff/position mapping problems.

## Prereqs

- Python 3.9+
- `pip install -r requirements.txt` (requests, python-dotenv)
- Your GitHub repos already exist and contain the branches referenced by old MRs.

## .env (example)

```
GITLAB_API_URL=https://gitlab.example.com/api/v4
GITHUB_API_URL=https://api.github.com
GITLAB_TOKEN=glpat_xxx
GITHUB_TOKEN=ghp_xxx
GITLAB_TOP_GROUP_ID=1234
GITHUB_ORG=my-github-org
# Optional: CSV file mapping GitLab usernames to GitHub usernames
USER_MAP_CSV=/path/to/user_map.csv
```

`USER_MAP_CSV` format (no header):

```
gitlab_username,github_username
alice,alice-gh
bob,bob-gh
```

## Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install requests python-dotenv
```

## Run

Dry-run first:

```bash
python3 gitlab_pr_to_github.py --dry-run
```

Import (no system notes):

```bash
python3 gitlab_pr_to_github.py
```

Include GitLab system notes (status changes, branch updates):

```bash
python3 gitlab_pr_to_github.py --include-system-notes
```

## Behavior details & limits

- **Branch names:** The script uses `source_branch` → `target_branch`. If a pair has no diff now, it creates a placeholder PR and then closes it, preserving history.
- **Merges:** If the PR can be merged (fast-forward possible), it's merged. Otherwise closed as “merged in GitLab.”
- **Labels/Milestones:** Created on the fly if not present.
- **Idempotency:** Re-running will skip PRs already containing the import marker.
- **Rate limits:** Light delays are added between comment posts. Adjust if you hit limits in your GitHub Enterprise.
- **Authorship/timestamps:** GitHub doesn’t allow setting these. The script records original author/time in text.

## Safety & rollback

- The script only **creates** PRs, comments, labels, and milestones, and **closes/merges** PRs. It never deletes anything.
- If needed, bulk-close imported PRs identified by the marker via the GitHub API.

## Troubleshooting

- **404 creating PR:** Confirm target repo exists in `GITHUB_ORG` and you have `repo` scope.
- **"No commits between" error:** Expected for stale branches. The script handles this by creating a placeholder PR and closing it.
- **Missing branches:** Ensure the old MR source/target branches exist in the migrated repo; otherwise you’ll see 422 errors.

## Tip: Run for a single project

If you want to test on one project, temporarily change `get_all_projects_recursive` to return a filtered list containing just that `path_with_namespace`.
