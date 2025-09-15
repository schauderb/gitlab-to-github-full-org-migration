# USAGE
# python migrate_repo_with_lfs.py \
#   --gitlab-url https://gitlab.dsf.boozallencsn.com/api/v4 \
#   --gitlab-project "group/subgroup/repo" \
#   --gitlab-token $GITLAB_TOKEN \
#   --github-api https://github.boozallencsn.com/api/v3 \
#   --github-org YourOrg \
#   --github-repo repo \
#   --github-token $GITHUB_TOKEN \
#   --src-clone-url "git@gitlab.example.com:group/subgroup/repo.git" \
#   --dst-push-url "git@github.yourco.com:YourOrg/repo.git" \
#   --lfs-patterns "*.mp4,*.webm,*.war,*.jar,*.zip,*.json,*.deb,*.db,*.apk,*.onnx,*.rpm" \
#   --workdir /tmp/repo-migrate

#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

def sh(cmd: List[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> str:
    res = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if res.returncode != 0:
        msg = f"Command failed: {' '.join(cmd)}\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        raise RuntimeError(msg)
    return res.stdout.strip()

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def write_gitattributes(repo_dir: Path, patterns: List[str]):
    gattr = repo_dir / ".gitattributes"
    lines = []
    for pat in patterns:
        pat = pat.strip()
        if not pat:
            continue
        lines.append(f"{pat} filter=lfs diff=lfs merge=lfs -text")
    content = "\n".join(lines) + "\n"
    if gattr.exists():
        # merge without duplicates
        existing = set(gattr.read_text().splitlines())
        for line in lines:
            if line not in existing:
                existing.add(line)
        content = "\n".join(sorted(existing)) + "\n"
    gattr.write_text(content, encoding="utf-8")

def github_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }

def gitlab_headers(token: str) -> Dict[str, str]:
    return {
        "PRIVATE-TOKEN": token,
    }

def ensure_github_repo(github_api: str, org: str, repo: str, token: str, private: bool = True) -> None:
    # check existence
    url = f"{github_api}/repos/{org}/{repo}"
    r = requests.get(url, headers=github_headers(token))
    if r.status_code == 200:
        return
    if r.status_code not in (301, 302, 404):
        r.raise_for_status()

    # create
    url = f"{github_api}/orgs/{org}/repos"
    payload = {"name": repo, "private": private, "auto_init": False}
    r = requests.post(url, headers=github_headers(token), json=payload)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Failed to create GitHub repo {org}/{repo}: {r.status_code} {r.text}")

def clone_all_branches(src_url: str, workdir: Path) -> Path:
    # Use a normal clone that fetches all branches and tags so we can rewrite history
    repo_dir = workdir / "repo"
    if repo_dir.exists():
        shutil.rmtree(repo_dir)
    sh(["git", "clone", "--no-single-branch", "--origin", "gitlab", src_url, str(repo_dir)])
    # Make sure we have all refs and tags
    sh(["git", "fetch", "--all", "--tags"], cwd=repo_dir)
    return repo_dir

def configure_lfs_and_rewrite(repo_dir: Path, lfs_patterns: List[str]):
    # Ensure LFS is available
    try:
        sh(["git", "lfs", "version"])
    except Exception as e:
        raise RuntimeError("git-lfs not found in PATH. Install git-lfs and run 'git lfs install'.") from e

    # Ensure lfs is initialized locally
    sh(["git", "lfs", "install"], cwd=repo_dir)

    # Track requested patterns
    write_gitattributes(repo_dir, lfs_patterns)
    sh(["git", "add", ".gitattributes"], cwd=repo_dir)
    # Commit only if this actually changed something
    try:
        sh(["git", "commit", "-m", "Track large binaries with Git LFS"], cwd=repo_dir)
    except RuntimeError as e:
        # likely "nothing to commit", ignore
        pass

    # Rewrite all history so matching files become LFS pointers
    include_arg = ",".join([p.strip() for p in lfs_patterns if p.strip()])
    sh(["git", "lfs", "migrate", "import", "--everything", f"--include={include_arg}"], cwd=repo_dir)

def push_to_github(repo_dir: Path, dst_push_url: str):
    # Add a separate remote named 'github'
    remotes = sh(["git", "remote"], cwd=repo_dir).splitlines()
    if "github" not in remotes:
        sh(["git", "remote", "add", "github", dst_push_url], cwd=repo_dir)
    else:
        sh(["git", "remote", "set-url", "github", dst_push_url], cwd=repo_dir)

    # Push branches and tags with force because we rewrote history
    sh(["git", "push", "github", "+refs/heads/*:refs/heads/*"], cwd=repo_dir)
    sh(["git", "push", "github", "+refs/tags/*:refs/tags/*"], cwd=repo_dir)

    # Push all LFS objects
    sh(["git", "lfs", "push", "--all", "github"], cwd=repo_dir)

def get_gitlab_project_id(gitlab_url: str, project_path: str, token: str) -> int:
    # project_path is "group/subgroup/repo"
    # URL encode it for API lookup
    from urllib.parse import quote_plus
    encoded = quote_plus(project_path)
    url = f"{gitlab_url}/api/v4/projects/{encoded}"
    r = requests.get(url, headers=gitlab_headers(token))
    r.raise_for_status()
    return r.json()["id"]

def paginate(url: str, headers: Dict[str, str], params: Dict[str, str]) -> List[dict]:
    out = []
    page = 1
    per_page = 100
    while True:
        p = params.copy()
        p.update({"page": page, "per_page": per_page})
        r = requests.get(url, headers=headers, params=p)
        r.raise_for_status()
        items = r.json()
        if not items:
            break
        out.extend(items)
        if len(items) < per_page:
            break
        page += 1
    return out

def migrate_issues_gitlab_to_github(
    gitlab_url: str,
    project_id: int,
    gitlab_token: str,
    github_api: str,
    github_org: str,
    github_repo: str,
    github_token: str,
    user_map: Optional[Dict[str, str]] = None,
) -> None:
    """
    Copies issues from GitLab to GitHub with labels, state, and comments.
    user_map can map GitLab usernames to GitHub usernames: {"gl_user":"gh_user", ...}
    """
    gl_headers = gitlab_headers(gitlab_token)
    gh_headers = github_headers(github_token)

    # Labels first, so we can assign them on issue creation
    gl_labels_url = f"{gitlab_url}/api/v4/projects/{project_id}/labels"
    gl_labels = paginate(gl_labels_url, gl_headers, params={})
    # Ensure labels exist in GitHub
    gh_labels_url = f"{github_api}/repos/{github_org}/{github_repo}/labels"
    existing = requests.get(gh_labels_url, headers=gh_headers)
    existing.raise_for_status()
    existing_names = {lbl["name"] for lbl in existing.json()}
    for lbl in gl_labels:
        name = lbl["name"]
        color = (lbl.get("color") or "#ededed").lstrip("#")
        if name not in existing_names:
            payload = {"name": name, "color": color[:6] or "ededed"}
            # ignore conflicts
            requests.post(gh_labels_url, headers=gh_headers, json=payload)

    # Issues
    gl_issues_url = f"{gitlab_url}/api/v4/projects/{project_id}/issues"
    gl_issues = paginate(gl_issues_url, gl_headers, params={"scope": "all", "order_by": "iid"})
    gh_issues_url = f"{github_api}/repos/{github_org}/{github_repo}/issues"

    for issue in gl_issues:
        title = issue["title"]
        body = issue.get("description") or ""
        labels = issue.get("labels") or []
        state = issue.get("state")  # "opened" or "closed"
        assignee = None
        if issue.get("assignee"):
            gl_user = issue["assignee"]["username"]
            if user_map and gl_user in user_map:
                assignee = user_map[gl_user]

        # Create issue
        payload = {
            "title": title,
            "body": f"{body}\n\n_Imported from GitLab issue #{issue['iid']}_",
            "labels": labels,
        }
        if assignee:
            payload["assignees"] = [assignee]
        gh_issue = requests.post(gh_issues_url, headers=gh_headers, json=payload)
        gh_issue.raise_for_status()
        gh_i = gh_issue.json()
        gh_issue_number = gh_i["number"]

        # Comments
        gl_notes_url = f"{gitlab_url}/api/v4/projects/{project_id}/issues/{issue['iid']}/notes"
        notes = paginate(gl_notes_url, gl_headers, params={})
        for note in notes:
            if note.get("system"):
                continue
            author = note["author"]["username"]
            mapped = user_map.get(author) if user_map else None
            author_str = f"@{mapped}" if mapped else f"(GitLab user: {author})"
            body = f"{note['body']}\n\n_Imported comment by {author_str}_"
            gh_comment_url = f"{gh_issues_url}/{gh_issue_number}/comments"
            r = requests.post(gh_comment_url, headers=gh_headers, json={"body": body})
            r.raise_for_status()

        # Close if necessary
        if state == "closed":
            close_url = f"{gh_issues_url}/{gh_issue_number}"
            r = requests.patch(close_url, headers=gh_headers, json={"state": "closed"})
            r.raise_for_status()

def main():
    ap = argparse.ArgumentParser(description="Migrate a GitLab repo to GitHub, converting large binaries to Git LFS and copying issues.")
    ap.add_argument("--gitlab-url", required=True, help="Base GitLab URL, e.g. https://gitlab.example.com")
    ap.add_argument("--gitlab-project", required=True, help="GitLab project path, e.g. group/subgroup/repo")
    ap.add_argument("--gitlab-token", required=True)

    ap.add_argument("--github-api", required=True, help="GitHub API base, e.g. https://api.github.com or https://ghe/api/v3")
    ap.add_argument("--github-org", required=True)
    ap.add_argument("--github-repo", required=True)
    ap.add_argument("--github-token", required=True)
    ap.add_argument("--private", action="store_true", default=False, help="Create GitHub repo as private if it does not exist")

    ap.add_argument("--src-clone-url", required=True, help="GitLab clone URL (SSH or HTTPS) used by git clone")
    ap.add_argument("--dst-push-url", required=True, help="GitHub push URL (SSH or HTTPS) used by git push")

    ap.add_argument("--lfs-patterns", default="*.mp4", help="Comma-separated patterns to track with LFS, e.g. *.mp4,*.webm,*.onnx")
    ap.add_argument("--workdir", default="", help="Working directory to use. Defaults to a temp dir.")
    ap.add_argument("--user-map-json", default="", help='Optional path to JSON mapping GitLab->GitHub users: {"gl_user":"gh_user"}')

    args = ap.parse_args()

    lfs_patterns = [p.strip() for p in args.lfs_patterns.split(",") if p.strip()]
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="repo-migrate-"))

    print(f"Working dir: {workdir}")
    ensure_dir(workdir)

    try:
        print("Cloning...")
        repo_dir = clone_all_branches(args.src_clone_url, workdir)

        print("Configuring Git LFS and rewriting history...")
        configure_lfs_and_rewrite(repo_dir, lfs_patterns)

        print("Ensuring GitHub repo exists...")
        ensure_github_repo(args.github_api, args.github_org, args.github_repo, args.github_token, private=args.private)

        print("Pushing branches, tags, and LFS objects...")
        push_to_github(repo_dir, args.dst_push_url)

        print("Migrating issues and comments...")
        project_id = get_gitlab_project_id(args.gitlab_url, args.gitlab_project, args.gitlab_token)

        user_map = {}
        if args.user_map_json:
            with open(args.user_map_json, "r", encoding="utf-8") as f:
                user_map = json.load(f)

        migrate_issues_gitlab_to_github(
            gitlab_url=args.gitlab_url,
            project_id=project_id,
            gitlab_token=args.gitlab_token,
            github_api=args.github_api,
            github_org=args.github_org,
            github_repo=args.github_repo,
            github_token=args.github_token,
            user_map=user_map or None,
        )

        print("Done.")
    finally:
        if not args.workdir:
            shutil.rmtree(workdir, ignore_errors=True)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("\nERROR:\n", e, file=sys.stderr)
        sys.exit(1)
