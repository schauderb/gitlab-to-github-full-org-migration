#!/usr/bin/env python3
"""
export_gitlab_audit.py

Export **audit-grade** history from a GitLab top-level group (aka org) into **separate CSV files**:
  - pipelines.csv                (/projects/:id/pipelines)
  - pipeline_jobs.csv            (/projects/:id/pipelines/:pipeline_id/jobs)
  - commits.csv                  (/projects/:id/repository/commits)
  - merge_requests.csv           (/projects/:id/merge_requests)

Best practices:
  - Recurses all subgroups and projects (optionally include archived).
  - Robust pagination with retries and backoff.
  - Time-window filters (--since/--until) applied where supported.
  - Project filtering via regex.
  - Writes headers once, appends rows safely.
  - Fail-soft: continues on API hiccups per project/endpoint.
  - Minimal rate limiting via --sleep to avoid burst limits.

Usage
-----
1) .env file (example):
   GITLAB_API_URL=https://gitlab.example.com/api/v4
   GITLAB_TOKEN=glpat_xxx
   GITLAB_TOP_GROUP_ID=1234

2) Install deps:
   python3 -m venv venv && source venv/bin/activate
   pip install requests python-dotenv

3) Run export:
   python3 export_gitlab_audit.py --out-dir ./out --since 2023-01-01 --until 2025-09-03 --include-archived --sleep 0.2
"""

import os
import re
import csv
import sys
import time
import argparse
from typing import Dict, Iterator, List, Optional

import requests
from requests.adapters import HTTPAdapter, Retry
from dotenv import load_dotenv

# ---------------------- Config & Sessions ----------------------

load_dotenv()
GITLAB_API_URL = os.getenv("GITLAB_API_URL")
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN")
GITLAB_TOP_GROUP_ID = os.getenv("GITLAB_TOP_GROUP_ID")

if not all([GITLAB_API_URL, GITLAB_TOKEN, GITLAB_TOP_GROUP_ID]):
    print("Missing .env values: GITLAB_API_URL, GITLAB_TOKEN, GITLAB_TOP_GROUP_ID", file=sys.stderr)

def make_session() -> requests.Session:
    sess = requests.Session()
    retries = Retry(total=5, backoff_factor=1.2, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retries)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)
    sess.headers.update({"PRIVATE-TOKEN": GITLAB_TOKEN})
    return sess

gl = make_session()

# ---------------------- Helpers ----------------------

def paginate(url: str, params: Optional[dict] = None) -> Iterator[dict]:
    """Yield all items across pages. If the endpoint returns a dict, yield it once."""
    params = params or {}
    params.setdefault("per_page", 100)
    while True:
        r = gl.get(url, params=params)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            for item in data:
                yield item
        else:
            yield data
            break
        next_url = r.links.get("next", {}).get("url")
        if not next_url:
            break
        url = next_url
        params = None  # carry next's querystring

def get_subgroups(group_id: int) -> Iterator[dict]:
    yield from paginate(f"{GITLAB_API_URL}/groups/{group_id}/subgroups")

def get_group_projects(group_id: int, include_archived: bool) -> Iterator[dict]:
    params = {
        "include_subgroups": False,
        "with_shared": False,
        "archived": "true" if include_archived else "false",
        "per_page": 100,
        "order_by": "id",
        "sort": "asc",
    }
    yield from paginate(f"{GITLAB_API_URL}/groups/{group_id}/projects", params=params)

def get_all_projects_recursive(group_id: int, include_archived: bool) -> List[dict]:
    projects = []
    # projects in this group
    for p in get_group_projects(group_id, include_archived):
        p["full_path"] = p.get("path_with_namespace") or p.get("name_with_namespace")
        projects.append(p)
    # subgroups
    for sg in get_subgroups(group_id):
        projects.extend(get_all_projects_recursive(sg["id"], include_archived))
    return projects

# ---------------------- Endpoint Pullers ----------------------

def get_pipelines(project_id: int, since: Optional[str], until: Optional[str]) -> Iterator[dict]:
    params = {"per_page": 100,
    }
    if since:
        params["updated_after"] = since
    if until:
        params["updated_before"] = until
    yield from paginate(f"{GITLAB_API_URL}/projects/{project_id}/pipelines", params)

def get_pipeline_detail(project_id: int, pipeline_id: int) -> dict:
    r = gl.get(f"{GITLAB_API_URL}/projects/{project_id}/pipelines/{pipeline_id}")
    r.raise_for_status()
    return r.json()

def get_pipeline_jobs(project_id: int, pipeline_id: int) -> Iterator[dict]:
    yield from paginate(f"{GITLAB_API_URL}/projects/{project_id}/pipelines/{pipeline_id}/jobs")

def get_commits(project_id: int, since: Optional[str], until: Optional[str]) -> Iterator[dict]:
    params = {"per_page": 100}
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    yield from paginate(f"{GITLAB_API_URL}/projects/{project_id}/repository/commits", params)

def get_merge_requests(project_id: int, since: Optional[str], until: Optional[str]) -> Iterator[dict]:
    params = {
        "state": "all",
        "order_by": "created_at",
        "sort": "asc",
        "per_page": 100,
    }
    if since:
        params["created_after"] = since
    if until:
        params["created_before"] = until
    yield from paginate(f"{GITLAB_API_URL}/projects/{project_id}/merge_requests", params)

# ---------------------- CSV Writers ----------------------

PIPELINE_COLS = [
    "group_id","project_id","project_path",
    "pipeline_id","iid","status","source","ref","sha","web_url",
    "duration","queued_duration",
    "created_at","updated_at","started_at","finished_at",
    "user_username","user_name"
]

JOB_COLS = [
    "group_id","project_id","project_path",
    "pipeline_id","job_id","name","stage","status","ref","sha",
    "runner_description","tag_list","allow_failure",
    "duration","queued_duration",
    "created_at","started_at","finished_at",
    "user_username","user_name","web_url"
]

COMMIT_COLS = [
    "group_id","project_id","project_path",
    "id","short_id","title","message","author_name","author_email",
    "committed_date","created_at","parent_ids","web_url"
]

MR_COLS = [
    "group_id","project_id","project_path",
    "iid","id","title","state","source_branch","target_branch",
    "author_username","author_name",
    "created_at","updated_at","merged_at","closed_at",
    "merge_user","assignees","reviewers","labels","milestone","web_url"
]

def write_header(path: str, headers: List[str]):
    first = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        if first:
            w.writeheader()

def write_row(path: str, headers: List[str], row: Dict):
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writerow({k: row.get(k, "") for k in headers})

# ---------------------- Main ----------------------

def main():
    ap = argparse.ArgumentParser(description="Export GitLab audit history (pipelines, jobs, commits, MRs) to CSVs.")
    ap.add_argument("--out-dir", default="./out", help="Directory to write CSVs (default: ./out)")
    ap.add_argument("--since", default=None, help="ISO datetime/date lower bound (created/updated after)")
    ap.add_argument("--until", default=None, help="ISO datetime/date upper bound (created/updated before)")
    ap.add_argument("--project-filter", default=None, help="Regex; only include projects whose full path matches")
    ap.add_argument("--include-archived", action="store_true", help="Include archived projects")
    ap.add_argument("--sleep", type=float, default=0.0, help="Sleep seconds between API calls (e.g., 0.15)")
    ap.add_argument("--max-projects", type=int, default=0, help="For testing: limit number of projects processed (0 = all)")
    args = ap.parse_args()

    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)
    pipelines_csv = os.path.join(out_dir, "pipelines.csv")
    jobs_csv = os.path.join(out_dir, "pipeline_jobs.csv")
    commits_csv = os.path.join(out_dir, "commits.csv")
    mrs_csv = os.path.join(out_dir, "merge_requests.csv")

    write_header(pipelines_csv, PIPELINE_COLS)
    write_header(commits_csv,   COMMIT_COLS)
    write_header(mrs_csv,       MR_COLS)
    write_header(jobs_csv,      JOB_COLS)

    proj_re = re.compile(args.project_filter) if args.project_filter else None
    group_id = int(GITLAB_TOP_GROUP_ID)

    # Discover projects
    projects = get_all_projects_recursive(group_id, include_archived=args.include_archived)
    if args.max_projects and args.max_projects > 0:
        projects = projects[:args.max_projects]

    for p in projects:
        project_id = p["id"]
        project_path = p["full_path"]
        if proj_re and not proj_re.search(project_path):
            continue

        # --- Pipelines & Jobs ---
        try:
            for pl in get_pipelines(project_id, args.since, args.until):
                try:
                    det = get_pipeline_detail(project_id, pl["id"])
                except requests.HTTPError:
                    det = pl

                prow = {
                    "group_id": group_id,
                    "project_id": project_id,
                    "project_path": project_path,
                    "pipeline_id": pl.get("id"),
                    "iid": pl.get("iid"),
                    "status": pl.get("status"),
                    "source": pl.get("source"),
                    "ref": pl.get("ref"),
                    "sha": pl.get("sha"),
                    "web_url": pl.get("web_url"),
                    "duration": det.get("duration"),
                    "queued_duration": det.get("queued_duration"),
                    "created_at": pl.get("created_at"),
                    "updated_at": pl.get("updated_at"),
                    "started_at": det.get("started_at"),
                    "finished_at": det.get("finished_at"),
                    "user_username": ((det.get("user") or {}).get("username") if det.get("user") else ""),
                    "user_name": ((det.get("user") or {}).get("name") if det.get("user") else ""),
                }
                write_row(pipelines_csv, PIPELINE_COLS, prow)

                try:
                    for job in get_pipeline_jobs(project_id, pl["id"]):
                        jrow = {
                            "group_id": group_id,
                            "project_id": project_id,
                            "project_path": project_path,
                            "pipeline_id": pl.get("id"),
                            "job_id": job.get("id"),
                            "name": job.get("name"),
                            "stage": job.get("stage"),
                            "status": job.get("status"),
                            "ref": job.get("ref"),
                            "sha": (job.get("commit", {}) or {}).get("id", ""),
                            "runner_description": (job.get("runner", {}) or {}).get("description", ""),
                            "tag_list": ",".join(job.get("tag_list") or []),
                            "allow_failure": job.get("allow_failure"),
                            "duration": job.get("duration"),
                            "queued_duration": job.get("queued_duration"),
                            "created_at": job.get("created_at"),
                            "started_at": job.get("started_at"),
                            "finished_at": job.get("finished_at"),
                            "user_username": (job.get("user", {}) or {}).get("username", ""),
                            "user_name": (job.get("user", {}) or {}).get("name", ""),
                            "web_url": job.get("web_url"),
                        }
                        write_row(jobs_csv, JOB_COLS, jrow)
                        if args.sleep: time.sleep(args.sleep)
                except requests.HTTPError as e:
                    print(f"[WARN] jobs fetch failed for {project_path} pipeline {pl.get('id')}: {e}", file=sys.stderr)

                if args.sleep: time.sleep(args.sleep)
        except requests.HTTPError as e:
            print(f"[WARN] pipelines fetch failed for {project_path}: {e}", file=sys.stderr)

        # --- Commits ---
        try:
            for c in get_commits(project_id, args.since, args.until):
                crow = {
                    "group_id": group_id,
                    "project_id": project_id,
                    "project_path": project_path,
                    "id": c.get("id"),
                    "short_id": c.get("short_id"),
                    "title": c.get("title"),
                    "message": c.get("message"),
                    "author_name": c.get("author_name"),
                    "author_email": c.get("author_email"),
                    "committed_date": c.get("committed_date"),
                    "created_at": c.get("created_at"),
                    "parent_ids": ",".join(c.get("parent_ids") or []),
                    "web_url": c.get("web_url"),
                }
                write_row(commits_csv, COMMIT_COLS, crow)
                if args.sleep: time.sleep(args.sleep)
        except requests.HTTPError as e:
            print(f"[WARN] commits fetch failed for {project_path}: {e}", file=sys.stderr)

        # --- Merge Requests ---
        try:
            for mr in get_merge_requests(project_id, args.since, args.until):
                mrow = {
                    "group_id": group_id,
                    "project_id": project_id,
                    "project_path": project_path,
                    "iid": mr.get("iid"),
                    "id": mr.get("id"),
                    "title": mr.get("title"),
                    "state": mr.get("state"),
                    "source_branch": mr.get("source_branch"),
                    "target_branch": mr.get("target_branch"),
                    "author_username": (mr.get("author") or {}).get("username", ""),
                    "author_name": (mr.get("author") or {}).get("name", ""),
                    "created_at": mr.get("created_at"),
                    "updated_at": mr.get("updated_at"),
                    "merged_at": mr.get("merged_at"),
                    "closed_at": mr.get("closed_at"),
                    "merge_user": ((mr.get("merged_by") or {}).get("username") if mr.get("merged_by") else ""),
                    "assignees": ",".join([a.get("username","") for a in (mr.get("assignees") or [])]),
                    "reviewers": ",".join([r.get("username","") for r in (mr.get("reviewers") or [])]),
                    "labels": ",".join(mr.get("labels") or []),
                    "milestone": ((mr.get("milestone") or {}).get("title") if mr.get("milestone") else ""),
                    "web_url": mr.get("web_url"),
                }
                write_row(mrs_csv, MR_COLS, mrow)
                if args.sleep: time.sleep(args.sleep)
        except requests.HTTPError as e:
            print(f"[WARN] merge_requests fetch failed for {project_path}: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
