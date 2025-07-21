# 🚀 GitLab → GitHub Repository Migration Guide

A single playbook to move every repository in a GitLab group to a GitHub organization—branches, tags, and full history included.

---

## 📑 Table of Contents

- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [Download & Configure the Script](#download--configure-the-script)
- [Run the Migration](#run-the-migration)
- [What Happens Behind the Curtain](#what-happens-behind-the-curtain)
- [Post-Migration Checklist](#post-migration-checklist)
- [Troubleshooting](#troubleshooting)
- [Support](#support)

---

## 1️⃣ Overview <a id="overview"></a>

This guide migrates every repository in one GitLab group to one GitHub organization.  
Expect ≈5 minutes of prep, then let the script do the heavy lifting. ✔️

---

## 2️⃣ Prerequisites 📝 <a id="prerequisites"></a>

| Need                                   | Why                                 |
|-----------------------------------------|-------------------------------------|
| GitLab Personal Access Token (api)      | List & clone repos                  |
| GitHub Personal Access Token (repo)     | Create & push repos                 |
| SSH keys on both platforms              | Password-less clone/push            |
| Python 3.x                             | Run the script                      |
| Git CLI                                | Clone/push mirrors                  |
| ETSS/CSN F5 VPN                        | Reach internal GitLab/GitHub hosts  |

> 💡 **Tip:** Store your PATs securely (e.g., password manager or environment variables).

---

## 3️⃣ Download & Configure the Script ⚙️ <a id="download--configure-the-script"></a>

```bash
# Grab the migration script
git clone https://github.boozallencsn.com/bdsf/bdsf-tenant-migration.git
cd bdsf-tenant-migration
```

Open `migrate_bdsf_gitlab_to_csn_github.py` and replace the placeholders:

```python
GITLAB_TOKEN = 'YOUR_GITLAB_TOKEN'
GITHUB_TOKEN = 'YOUR_GITHUB_TOKEN'
GITLAB_GROUP = 'YOUR_GITLAB_GROUP'   # e.g. "BAH-Tenant1"
GITHUB_ORG   = 'YOUR_GITHUB_ORG'     # e.g. "BAH-Tenant1"
```

**🔄 Prefer environment variables?**

```python
import os
GITLAB_TOKEN = os.getenv("GL_TOKEN")
GITHUB_TOKEN = os.getenv("GH_TOKEN")
GITLAB_GROUP = os.getenv("GL_GROUP")
GITHUB_ORG   = os.getenv("GH_ORG")
```

Export them before running:

```bash
export GL_TOKEN=xxxxxxxx
export GH_TOKEN=yyyyyyyy
export GL_GROUP=BAH-Tenant1
export GH_ORG=BAH-Tenant1
```

---

## 4️⃣ Run the Migration ▶️ <a id="run-the-migration"></a>

```bash
# 1. Connect to ETSS/CSN F5 VPN

# 2. Move to the script directory
cd path/to/script

# 3. Fire away
python3 migrate_bdsf_gitlab_to_csn_github.py
```

Watch the log stream—each repo shows: **create → clone → push → cleanup → done**.

---

## 5️⃣ What Happens Behind the Curtain 🔍 <a id="what-happens-behind-the-curtain"></a>

| Step | Function / Command                        | Purpose                                               |
|------|-------------------------------------------|-------------------------------------------------------|
| 1    | `get_gitlab_repos()`                      | `GET /groups/<group>/projects` to list up to 100 repos|
| 2    | `create_github_repo(repo_name)`           | `POST /orgs/<org>/repos`; warns if repo exists        |
| 3    | `git clone --mirror <name>.git`           | Bare-clone locally                                    |
| 4    | `git remote set-url --push origin <ssh>`  | Point origin to GitHub                                |
| 5    | `git push --mirror`                       | Ship all branches, tags & refs                        |
| 6    | `rm -rf <name>.git`                       | Delete the local mirror                               |
| 7    | Loop                                      | Repeat for each repo, logging success/failure         |

> 🤔 **Need verbose logs?**  
> Change  
> `logging.basicConfig(level=logging.INFO, …)`  
> to  
> `logging.basicConfig(level=logging.DEBUG, …)`

---

## 6️⃣ Post-Migration Checklist ✅ <a id="post-migration-checklist"></a>

- **Repository list:** Confirm every repo appears in GitHub.
- **Permissions:** Adjust org teams or repo settings if needed.
- **CI/CD:** Re-point pipelines (GitHub Actions, Jenkinsfiles, etc.).
- **Issues & Wiki:** GitHub enables both; import extra data manually if required.
- **Team notification:** Share new URLs and update docs/bookmarks.

---

## 7️⃣ Troubleshooting 🆘 <a id="troubleshooting"></a>

| Symptom                               | Likely Cause                       | Fix                                         |
|----------------------------------------|------------------------------------|---------------------------------------------|
| 401 Unauthorized                      | PAT scopes missing / typo          | Regenerate token with correct scopes        |
| “Repo already exists” warning          | Repo exists in GitHub              | Safe to ignore—script continues             |
| Permission denied (publickey)          | SSH key not on GitHub/GitLab       | Add key or use HTTPS with PAT               |
| `requests.exceptions.ConnectionError`  | Off VPN                            | Connect to ETSS/CSN F5 first                |
| Partial history pushed                 | Used `git clone` (non-mirror)      | Delete GitHub repo, rerun script            |

---

## 8️⃣ Support <a id="support"></a>

Need help? Email [bdsfhelpdesk@bah.com](mailto:bdsfhelpdesk@bah.com).  
Happy migrating!
