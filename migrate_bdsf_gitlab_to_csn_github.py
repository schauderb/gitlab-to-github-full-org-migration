import subprocess
import requests
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s:%(message)s')

GITLAB_API_URL = 'https://gitlab.dsf.boozallencsn.com/api/v4'
GITHUB_API_URL = 'https://github.boozallencsn.com/api/v3'

##### Configuration - replace with your actual values ####### 
GITLAB_TOKEN = 'YOUR_GITLAB_TOKEN'
GITHUB_TOKEN = 'YOUR_GITHUB_TOKEN'
GITLAB_GROUP = 'YOUR_GITLAB_GROUP'
GITHUB_ORG = 'YOUR_GITHUB_ORG'
#############################################################

def get_gitlab_repos():
    url = f"{GITLAB_API_URL}/groups/{GITLAB_GROUP}/projects?per_page=100"
    headers = {'PRIVATE-TOKEN': GITLAB_TOKEN}
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()

def create_github_repo(repo_name, private=True):
    url = f"{GITHUB_API_URL}/orgs/{GITHUB_ORG}/repos"
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json'
    }
    data = {
        "name": repo_name,
        "private": private,
        "auto_init": False,
        "has_issues": True,
        "has_wiki": True
    }
    resp = requests.post(url, headers=headers, json=data)
    if resp.status_code == 422:
        logging.warning(f"Repo {repo_name} already exists on GitHub")
        return True
    resp.raise_for_status()
    return resp.json()

def migrate_repo(repo):
    repo_name = repo['path']
    gitlab_ssh_url = repo['ssh_url_to_repo']
    github_url = f"git@github.boozallencsn.com:{GITHUB_ORG}/{repo_name}.git"

    logging.info(f"Creating GitHub repo '{repo_name}'")
    create_github_repo(repo_name)

    logging.info(f"Cloning GitLab repo '{repo_name}'")
    subprocess.run(['git', 'clone', '--mirror', gitlab_ssh_url], check=True)

    logging.info(f"Pushing to GitHub repo '{repo_name}'")
    subprocess.run(['git', 'remote', 'set-url', '--push', 'origin', github_url], cwd=f"{repo_name}.git", check=True)
    subprocess.run(['git', 'push', '--mirror'], cwd=f"{repo_name}.git", check=True)

    logging.info(f"Cleaning up local repo mirror '{repo_name}.git'")
    subprocess.run(['rm', '-rf', f"{repo_name}.git"], check=True)

def main():
    repos = get_gitlab_repos()
    for repo in repos:
        try:
            migrate_repo(repo)
            logging.info(f"Successfully migrated: {repo['path']}")
        except Exception as e:
            logging.error(f"Failed migration for {repo['path']}: {e}")

if __name__ == "__main__":
    main()
