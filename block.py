#!/usr/bin/env python3

import os, time, requests
from dotenv import load_dotenv
load_dotenv()


GITLAB_API_URL = os.getenv("GITLAB_API_URL")
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN")
DRY_RUN = os.getenv("DRY_RUN", "1").lower() in ("1", "true", "yes")

s = requests.Session()
s.headers.update({"PRIVATE-TOKEN": GITLAB_TOKEN})

targets = []
page = 1
while True:
    r = s.get(f"{GITLAB_API_URL}/users",
              params={"per_page":100,"page":page,"order_by":"id","sort":"asc","active":True})
    r.raise_for_status()
    batch = r.json()
    if not batch:
        break
    for u in batch:
        if not u.get("admin") and u.get("state") == "active":
            targets.append((u["id"], u["username"]))
    page += 1


print(f"Targets: {len(targets)}")

for i, (uid, uname) in enumerate(targets, 1):
    print(f"[{i}/{len(targets)}] {uid} {uname}")
if DRY_RUN:
    print("DRY_RUN is enabled. No users will be blocked.")
    raise SystemExit(0)


failures = []
for i, (uid, uname) in enumerate(targets, 1):
    resp = s.post(f"{GITLAB_API_URL}/users/{uid}/block")
    if resp.status_code == 201 or resp.status_code == 200:
        print(f"[{i}/{len(targets)}] Blocked: {uid} {uname}")
    else:
        print(f"[{i}/{len(targets)}] FAILED to block {uid} {uname}: {resp.status_code} {resp.text}")
        failures.append((uid, uname, resp.status_code, resp.text))
    time.sleep(0.1)

if failures:
    print(f"\nFailed to block {len(failures)} users:")
    for uid, uname, code, text in failures:
        print(f"  {uid} {uname}: {code} {text}")
