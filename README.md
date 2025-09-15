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

### Prerequisite Setup (Skip if already set up)

**If you already have GitLab/GitHub Personal Access Tokens (PATs) and SSH keys configured, you can [skip to Download & Configure the Script ⏩](#download--configure-the-script).**

#### 1. Create a GitLab Personal Access Token (PAT)

1. Log in to GitLab.
2. Go to **User Settings > Access Tokens**.
3. Name your token, set an expiration, and select the `api` scope.
4. Click **Create personal access token** and copy/save it securely.

#### 2. Create a GitHub Personal Access Token (PAT)

1. Log in to GitHub.
2. Go to **Settings > Developer settings > Personal access tokens**.
3. Click **Generate new token** (classic).
4. Name your token, set an expiration, and select the `repo` scope.
5. Click **Generate token** and copy/save it securely.

#### 3. Create and Add SSH Keys to GitLab & GitHub

1. Generate a new SSH key (if you don't have one):

   ```bash
   ssh-keygen -t ed25519 -C "your_email@example.com"
   # or use rsa: ssh-keygen -t rsa -b 4096 -C "your_email@example.com"
   ```

2. Add your public key to GitLab:
   - Go to **User Settings > SSH Keys**
   - Paste the contents of your `~/.ssh/id_ed25519.pub` (or `id_rsa.pub`) file

3. Add your public key to GitHub:
   - Go to **Settings > SSH and GPG keys**
   - Click **New SSH key**, give it a title, and paste your public key

> For more details, see the official GitLab and GitHub documentation on PATs and SSH keys.

| Need                                | Why                                |
| ----------------------------------- | ---------------------------------- |
| GitLab Owner permissions            | Access to migrate full repos       |
| GitLab Personal Access Token (api)  | List & clone repos                 |
| GitHub Personal Access Token (repo) | Create & push repos                |
| SSH keys on both platforms          | Password-less clone/push           |
| Python 3.x                          | Run the script                     |
| Git CLI                             | Clone/push mirrors                 |
| ETSS/CSN F5 VPN                     | Reach internal GitLab/GitHub hosts |

> 💡 **Tip:** Store your PATs securely (e.g., password manager or environment variables).

---

## 3️⃣ Download & Configure the Script ⚙️ <a id="download--configure-the-script"></a>

```bash
# Grab the migration script
git clone https://github.com/bdsf/bdsf-tenant-migration.git
cd bdsf-tenant-migration
```

## Configure Environment Variables

All configuration is now handled via a `.env` file in the project root. Copy the example below and fill in your values:

```env
GITLAB_API_URL=https://gitlab.com/api/v4
GITHUB_API_URL=https://github.com/api/v3
GITLAB_TOKEN=your_gitlab_token
GITHUB_TOKEN=your_github_token
GITHUB_ORG=your_github_org
GITLAB_TOP_GROUP_ID=your_gitlab_top_group_id  # e.g. 2305
```

> **Never commit your `.env` file to version control!**

The script uses [python-dotenv](https://pypi.org/project/python-dotenv/) to load these variables automatically.

---

## 4️⃣ Run the Migration ▶️ <a id="run-the-migration"></a>

```bash
# 1. Connect to ETSS/CSN F5 VPN

# 2. Move to the script directory
cd path/to/script

# 3. (Recommended) Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate

# 4. Install dependencies
pip install -r requirements.txt

# 5. Fire away
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
- **CI/CD:** Reconfigure pipelines (using GitHub Actions).
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
| Large file (>100MB) error              | GitHub blocks files >100MB          | See below for workaround                    |

### Handling Large File (>100MB) Migration Issues

If you hit an error about files over 100MB (GitHub's limit), follow these steps to finish migrating your repo's that encountered LFS-related issues:

1. **Clone the repo from GitLab (if necessary), or 'cd' into it (if already present from script):**

   ```bash
   git clone --mirror https://gitlab.com/<your-gitlab-group>/<your-repo>
   cd <your-repo>
   ```

2. **Add the new GitHub remote:**

   ```bash
   git remote add github https://github.com/<your-org>/<your-repo>.git
   ```

3. **(Optional) Identify files over 100MBs:**

   ```bash
   git rev-list --objects --all | \
    git cat-file --batch-check='%(objecttype) %(objectname) %(objectsize) %(rest)' | \
    awk '$1 == "blob" && $3 >= 100000000' | \
    sort -k3 -n
   ```

4. **Install `git-lfs` to remove large files:**

   - With pip (Linux/macOS/Windows):

     ```bash
     pip install git-lfs
     # or
     pip3 install git-lfs
     ```

   - With Homebrew (macOS):

     ```bash
     brew install git-lfs
     ```

5. **Run git lfs Fetch**

   ```bash
   git lfs fetch --all
   ```

6. **Set Up git LFS Tracking:**

   ```bash
   git lfs track "*.<large-file-type> #only needed if not included in default list of .gitattributes file
   ```

7. **Run git LFS Migrate:**

   ```bash
   git lfs migrate import --everything
   ```

8. **Run git Push w/ force**

   ```bash
   git push --force --all github
   ```

9. **Run git LFS Push**

   ```bash
   git lfs push --all github
   ```

10. **Verify Repo Updates in GitHub**

    Navigate to the target GitHub repo to validate all contents have been successfully migrated.
    
---

## 8️⃣ Support <a id="support"></a>

Need help? Email [bdsfhelpdesk@bah.com](mailto:bdsfhelpdesk@bah.com).  
Happy migrating!
