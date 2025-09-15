#!/usr/bin/env python3
"""
gitlab_pr_to_github.py — v1.1
- Fix: 422 "head invalid" by ensuring head/base branches exist in GitHub.
- Creates temp branch refs/heads/import/mr-<iid> from GitLab MR head SHA when needed.
- Better base-branch resolution (falls back to repo default branch).
- When there is truly no diff and GitHub rejects PR creation, optionally creates an Issue instead (--issue-when-nodiff).
"""

import os
import time
import csv
import logging
import argparse
from typing import Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter, Retry
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
load_dotenv()

GITLAB_API_URL = os.getenv("GITLAB_API_URL")
GITHUB_API_URL = os.getenv("GITHUB_API_URL")
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITLAB_TOP_GROUP_ID = os.getenv("GITLAB_TOP_GROUP_ID")
GITHUB_ORG = os.getenv("GITHUB_ORG")
USER_MAP_CSV = os.getenv("USER_MAP_CSV", "")

def flatten_repo_name(full_path: str) -> str:
    return full_path.replace("/", "__")

def make_session(token_header_key: str, token_value: str) -> requests.Session:
    sess = requests.Session()
    retries = Retry(total=5, backoff_factor=1.2, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retries)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)
    sess.headers.update({token_header_key: token_value})
    return sess

gl = make_session("PRIVATE-TOKEN", GITLAB_TOKEN)
gh = make_session("Authorization", f"Bearer {GITHUB_TOKEN}")
gh.headers.update({"Accept": "application/vnd.github+json"})

def load_user_map(csv_path: str) -> Dict[str, str]:
    mapping = {}
    if not csv_path or not os.path.isfile(csv_path):
        return mapping
    with open(csv_path, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        for row in r:
            if len(row) >= 2:
                mapping[row[0].strip()] = row[1].strip()
    logging.info(f"Loaded {len(mapping)} user mappings from {csv_path}")
    return mapping

USER_MAP = load_user_map(USER_MAP_CSV)

def paginate(session: requests.Session, url: str, params: dict = None):
    params = params or {}
    params.setdefault("per_page", 100)
    while True:
        resp = session.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            for item in data:
                yield item
        else:
            yield data
            break
        next_link = resp.links.get("next", {}).get("url")
        if not next_link:
            break
        url = next_link
        params = None

def map_user(gl_username: Optional[str]) -> str:
    if not gl_username:
        return "unknown"
    return USER_MAP.get(gl_username, gl_username)

# ---------- GitHub branch/ref helpers ----------

def github_repo_default_branch(repo_full_name: str) -> str:
    r = gh.get(f"{GITHUB_API_URL}/repos/{repo_full_name}")
    r.raise_for_status()
    return r.json().get("default_branch") or "main"

def github_branch_exists(repo_full_name: str, branch: str) -> bool:
    r = gh.get(f"{GITHUB_API_URL}/repos/{repo_full_name}/git/ref/heads/{branch}")
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        return False
    r.raise_for_status()
    return False

def github_commit_exists(repo_full_name: str, sha: str) -> bool:
    r = gh.get(f"{GITHUB_API_URL}/repos/{repo_full_name}/git/commits/{sha}")
    return r.status_code == 200

def create_branch_from_sha(repo_full_name: str, branch: str, sha: str) -> bool:
    """Create refs/heads/<branch> pointing to sha if sha exists in repo."""
    if not sha or not all(c in "0123456789abcdef" for c in sha.lower() if c.isalnum()):
        return False
    if not github_commit_exists(repo_full_name, sha):
        return False
    url = f"{GITHUB_API_URL}/repos/{repo_full_name}/git/refs"
    payload = {"ref": f"refs/heads/{branch}", "sha": sha}
    r = gh.post(url, json=payload)
    if r.status_code in (200, 201):
        logging.info(f"Created branch {branch} at {sha[:10]} in {repo_full_name}")
        return True
    if r.status_code == 422 and "Reference already exists" in r.text:
        return True
    logging.warning(f"Failed to create branch {branch} from sha {sha[:10]}: {r.status_code} {r.text}")
    return False

# ---------- GitHub PR/Issue helpers ----------

def ensure_labels(repo_full_name: str, labels: List[str]):
    for label in labels or []:
        url = f"{GITHUB_API_URL}/repos/{repo_full_name}/labels"
        r = gh.post(url, json={"name": label})
        if r.status_code not in (200, 201, 422):
            logging.warning(f"Failed to ensure label '{label}' on {repo_full_name}: {r.status_code} {r.text}")

def ensure_milestone(repo_full_name: str, title: str) -> Optional[int]:
    if not title:
        return None
    url = f"{GITHUB_API_URL}/repos/{repo_full_name}/milestones"
    r = gh.get(url, params={"state": "all", "per_page": 100})
    r.raise_for_status()
    for m in r.json():
        if m["title"] == title:
            return m["number"]
    r = gh.post(url, json={"title": title})
    if r.status_code in (200, 201):
        return r.json()["number"]
    logging.warning(f"Failed to ensure milestone '{title}' on {repo_full_name}: {r.status_code} {r.text}")
    return None

def set_labels_and_milestone(repo_full_name: str, pr_number: int, labels: List[str], milestone_title: Optional[str]):
    if labels:
        ensure_labels(repo_full_name, labels)
        gh.patch(f"{GITHUB_API_URL}/repos/{repo_full_name}/issues/{pr_number}", json={"labels": labels})
    if milestone_title:
        ms_no = ensure_milestone(repo_full_name, milestone_title)
        if ms_no:
            gh.patch(f"{GITHUB_API_URL}/repos/{repo_full_name}/issues/{pr_number}", json={"milestone": ms_no})

def add_issue_comment(repo_full_name: str, pr_number: int, body: str):
    gh.post(f"{GITHUB_API_URL}/repos/{repo_full_name}/issues/{pr_number}/comments", json={"body": body})

def create_issue(repo_full_name: str, title: str, body: str, labels: List[str] = None) -> Optional[int]:
    r = gh.post(f"{GITHUB_API_URL}/repos/{repo_full_name}/issues", json={"title": title, "body": body, "labels": labels or []})
    if r.status_code in (200, 201):
        return r.json()["number"]
    logging.error(f"Failed to create issue in {repo_full_name}: {r.status_code} {r.text}")
    return None

def find_existing_pr_by_marker(repo_full_name: str, marker: str) -> Optional[int]:
    page = 1
    while True:
        url = f"{GITHUB_API_URL}/repos/{repo_full_name}/pulls"
        params = {"state": "all", "per_page": 100, "page": page}
        r = gh.get(url, params=params)
        r.raise_for_status()
        pulls = r.json()
        if not pulls:
            return None
        for pr in pulls:
            ir = gh.get(f"{GITHUB_API_URL}/repos/{repo_full_name}/issues/{pr['number']}")
            ir.raise_for_status()
            body = ir.json().get("body") or ""
            if marker in body:
                return pr["number"]
        page += 1

def create_or_get_pr(repo_full_name: str, title: str, body: str, head: str, base: str, marker: str) -> int:
    existing = find_existing_pr_by_marker(repo_full_name, marker)
    if existing:
        logging.info(f"PR already imported (#{existing}) for marker {marker}")
        return existing
    url = f"{GITHUB_API_URL}/repos/{repo_full_name}/pulls"
    payload = {"title": title, "body": body, "head": head, "base": base, "maintainer_can_modify": False}
    r = gh.post(url, json=payload)
    r.raise_for_status()
    return r.json()["number"]

def set_pr_state(repo_full_name: str, pr_number: int, state: str, merge_message: Optional[str] = None):
    if state == "merged":
        merge_url = f"{GITHUB_API_URL}/repos/{repo_full_name}/pulls/{pr_number}/merge"
        mr = gh.put(merge_url, json={"commit_title": merge_message or "Merged in GitLab (historical import)"})
        if mr.status_code in (200, 201):
            logging.info(f"Merged PR #{pr_number} in {repo_full_name}")
            return
        logging.info(f"Could not merge PR #{pr_number}; closing instead.")
        state = "closed"
    if state == "closed":
        gh.patch(f"{GITHUB_API_URL}/repos/{repo_full_name}/issues/{pr_number}", json={"state": "closed"})

# ---------- GitLab discovery ----------

def get_subgroups(group_id: int):
    yield from paginate(gl, f"{GITLAB_API_URL}/groups/{group_id}/subgroups")

def get_group_projects(group_id: int):
    yield from paginate(gl, f"{GITLAB_API_URL}/groups/{group_id}/projects")

def get_all_projects_recursive(group_id: int, parent_path: str = "") -> List[dict]:
    projects = []
    for p in get_group_projects(group_id):
        p["full_path"] = f"{parent_path}/{p['path']}" if parent_path else p["path"]
        projects.append(p)
    for sg in get_subgroups(group_id):
        sub_parent_path = f"{parent_path}/{sg['path']}" if parent_path else sg["path"]
        projects.extend(get_all_projects_recursive(sg["id"], sub_parent_path))
    return projects

def get_merge_requests(project_id: int):
    yield from paginate(gl, f"{GITLAB_API_URL}/projects/{project_id}/merge_requests",
                        params={"state": "all", "order_by": "created_at", "sort": "asc"})

def get_mr_details(project_id: int, mr_iid: int) -> dict:
    r = gl.get(f"{GITLAB_API_URL}/projects/{project_id}/merge_requests/{mr_iid}")
    r.raise_for_status()
    return r.json()

def get_mr_notes(project_id: int, mr_iid: int, include_system: bool):
    url = f"{GITLAB_API_URL}/projects/{project_id}/merge_requests/{mr_iid}/notes"
    for note in paginate(gl, url):
        if not include_system and note.get("system"):
            continue
        yield note

# ---------- Import Logic ----------

def ensure_head_and_base(repo_full_name: str, source_branch: str, target_branch: str, head_sha: Optional[str]) -> (str, str, bool):
    """
    Ensure head/base exist. If head missing and a head_sha is available and present in repo,
    create refs/heads/import/mr-<iid> pointing to it.
    Return (head, base, had_to_create_head)
    """
    # Resolve base
    base = target_branch or github_repo_default_branch(repo_full_name)
    if not github_branch_exists(repo_full_name, base):
        base = github_repo_default_branch(repo_full_name)
        if not github_branch_exists(repo_full_name, base):
            # last resort
            base = "main"

    head = source_branch or base
    created = False
    if not github_branch_exists(repo_full_name, head):
        # Try import branch from sha
        if head_sha and create_branch_from_sha(repo_full_name, f"import/{head}", head_sha):
            head = f"import/{head}"
            created = True
        else:
            # final fallback: use base so caller can decide to create issue on no-diff
            head = base
    return head, base, created

def import_project_mrs(project: dict, args: argparse.Namespace):
    repo_name = flatten_repo_name(project["full_path"])
    repo_full_name = f"{GITHUB_ORG}/{repo_name}"
    logging.info(f"Importing MRs for {project.get('path_with_namespace', project['full_path'])} -> {repo_full_name}")

    for mr in get_merge_requests(project["id"]):
        gl_iid = mr["iid"]
        details = get_mr_details(project["id"], gl_iid)
        head_sha = ((details.get("diff_refs") or {}).get("head_sha")
                    or details.get("sha"))

        marker = f"[Imported-from-GitLab: project_id={project['id']} iid={gl_iid}]"

        title = f"{mr['title']}"
        author = (mr.get("author") or {}).get("username") or (mr.get("author") or {}).get("name") or "unknown"
        mapped_author = map_user(author)

        created_at = mr.get("created_at")
        state = "merged" if mr.get("merged_at") else ("closed" if mr.get("state") == "closed" else "open")
        source_branch = mr.get("source_branch") or ""
        target_branch = mr.get("target_branch") or ""

        labels = mr.get("labels") or []
        milestone_title = (mr.get("milestone") or {}).get("title")

        body_lines = [
            marker,
            "",
            f"**Imported from GitLab MR !{gl_iid}**",
            f"- Original author: `{author}` (mapped to `{mapped_author}`)",
            f"- Created at: `{created_at}`",
            f"- State on GitLab: `{mr.get('state')}` (merged_at={mr.get('merged_at')})",
            f"- Source → Target: `{source_branch or 'unknown'}` → `{target_branch or 'unknown'}`",
            f"- Head SHA: `{(head_sha or '')[:12]}`",
            f"- Original URL: {mr.get('web_url')}",
            "",
            "---",
            "",
            (mr.get("description") or "").strip()
        ]
        body = "\n".join(body_lines)

        if args.dry_run:
            logging.info(f"[DRY RUN] Would create PR for MR !{gl_iid} on {repo_full_name}")
            continue

        # Ensure branches
        head, base, created_head = ensure_head_and_base(repo_full_name, source_branch, target_branch, head_sha)

        # Create PR (or existing by marker)
        try:
            pr_number = create_or_get_pr(repo_full_name, title, body, head, base, marker)
        except requests.HTTPError as e:
            txt = e.response.text if e.response is not None else ""
            if "No commits between" in txt or ("Validation Failed" in txt and '"head","code":"invalid"' in txt):
                # Optional: create Issue instead of PR when no-diff or head invalid and we could not create a branch
                if args.issue_when_nodiff:
                    issue_labels = ["historical-mr"]
                    issue_title = f"[Historical MR] {title}"
                    issue_no = create_issue(repo_full_name, issue_title, body, labels=issue_labels)
                    if issue_no:
                        logging.warning(f"Created Issue #{issue_no} instead of PR for MR !{gl_iid} (no diff / invalid head).")
                    else:
                        logging.error(f"Failed to create fallback Issue for MR !{gl_iid}.")
                    continue
                else:
                    logging.warning(f"Skipping MR !{gl_iid}: {txt}")
                    continue
            raise

        # Labels & milestone
        set_labels_and_milestone(repo_full_name, pr_number, labels, milestone_title)

        # Notes -> comments
        for note in get_mr_notes(project["id"], gl_iid, include_system=args.include_system_notes):
            n_author = (note.get("author") or {}).get("username") or (note.get("author") or {}).get("name")
            n_mapped = map_user(n_author)
            n_created = note.get("created_at")
            n_body = (note.get("body") or "").strip()

            comment_text = (
                f"_Imported GitLab note_\n"
                f"- Author: `{n_author}` (mapped to `{n_mapped}`)\n"
                f"- Created at: `{n_created}`\n\n"
                f"{n_body}"
            )
            add_issue_comment(repo_full_name, pr_number, comment_text)
            time.sleep(0.15)

        # Set PR state
        set_pr_state(repo_full_name, pr_number, state, merge_message=f"Merged in GitLab (MR !{gl_iid})")

def main():
    parser = argparse.ArgumentParser(description="Import GitLab MR history and comments into GitHub PRs.")
    parser.add_argument("--dry-run", action="store_true", help="Show actions without writing to GitHub.")
    parser.add_argument("--include-system-notes", action="store_true", help="Include GitLab system notes (events).")
    parser.add_argument("--issue-when-nodiff", action="store_true",
                        help="Create an Issue when PR cannot be created due to no diff/invalid head.")
    args = parser.parse_args()

    if not all([GITLAB_API_URL, GITHUB_API_URL, GITLAB_TOKEN, GITHUB_TOKEN, GITLAB_TOP_GROUP_ID, GITHUB_ORG]):
        raise SystemExit("Missing required .env variables.")

    projects = get_all_projects_recursive(int(GITLAB_TOP_GROUP_ID), parent_path="")
    logging.info(f"Discovered {len(projects)} GitLab projects under group {GITLAB_TOP_GROUP_ID}")

    for p in projects:
        try:
            import_project_mrs(p, args)
        except requests.HTTPError as e:
            logging.error(f"HTTP error on project {p.get('path_with_namespace', p['full_path'])}: {e.response.status_code} {e.response.text}")
        except Exception as e:
            logging.exception(f"Failed importing MRs for {p.get('path_with_namespace', p['full_path'])}: {e}")

if __name__ == "__main__":
    main()
