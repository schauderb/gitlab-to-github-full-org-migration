
import subprocess
import requests
import logging
import os
from dotenv import load_dotenv


# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s:%(message)s')

# Load environment variables from .env
load_dotenv()

GITLAB_API_URL = os.getenv('GITLAB_API_URL')
GITHUB_API_URL = os.getenv('GITHUB_API_URL')
GITLAB_TOKEN = os.getenv('GITLAB_TOKEN')
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GITLAB_TOP_GROUP_ID = os.getenv('GITLAB_TOP_GROUP_ID')
GITHUB_ORG = os.getenv('GITHUB_ORG')

def get_subgroups(group_id):
    url = f"{GITLAB_API_URL}/groups/{group_id}/subgroups?per_page=100"
    headers = {'PRIVATE-TOKEN': GITLAB_TOKEN}
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()

def get_group_repos(group_id):
    url = f"{GITLAB_API_URL}/groups/{group_id}/projects?per_page=100"
    headers = {'PRIVATE-TOKEN': GITLAB_TOKEN}
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()

def get_all_repos_recursive(group_id, parent_path=""):
    repos = []
    # Get repos for this group
    for repo in get_group_repos(group_id):
        repo['full_path'] = f"{parent_path}/{repo['path']}" if parent_path else repo['path']
        repos.append(repo)
    # Get subgroups and recurse
    for subgroup in get_subgroups(group_id):
        sub_parent_path = f"{parent_path}/{subgroup['path']}" if parent_path else subgroup['path']
        repos.extend(get_all_repos_recursive(subgroup['id'], sub_parent_path))
    return repos

    # Removed: now using GITLAB_TOP_GROUP_ID from .env

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
    repo_name = repo['full_path'].replace('/', '__')  # Replace '/' to flatten structure in GitHub
    gitlab_ssh_url = repo['ssh_url_to_repo']
    github_url = f"git@github.boozallencsn.com:{GITHUB_ORG}/{repo_name}.git"

    logging.info(f"Creating GitHub repo '{repo_name}'")
    create_github_repo(repo_name)

    logging.info(f"Cloning GitLab repo '{repo_name}'")
    subprocess.run(['git', 'clone', '--mirror', gitlab_ssh_url], check=True)

    logging.info(f"Pushing to GitHub repo '{repo_name}'")
    subprocess.run(['git', 'remote', 'set-url', '--push', 'origin', github_url], cwd=f"{repo['path']}.git", check=True)
    subprocess.run(['git', 'push', '--mirror'], cwd=f"{repo['path']}.git", check=True)

    logging.info(f"Cleaning up local repo mirror '{repo['path']}.git'")
    subprocess.run(['rm', '-rf', f"{repo['path']}.git"], check=True)


def main():
    if not GITLAB_TOP_GROUP_ID:
        raise Exception("GITLAB_TOP_GROUP_ID must be set in the .env file.")
    repos = get_all_repos_recursive(GITLAB_TOP_GROUP_ID)
    for repo in repos:
        try:
            migrate_repo(repo)
            logging.info(f"Successfully migrated: {repo['full_path']}")
        except Exception as e:
            logging.error(f"Failed migration for {repo['full_path']}: {e}")

if __name__ == "__main__":
    main()
