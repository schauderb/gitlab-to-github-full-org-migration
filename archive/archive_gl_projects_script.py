import requests

# CONFIGURATION
GITLAB_URL = "https://gitlab.com/"  # Change to your GitLab server URL if self-hosted
ACCESS_TOKEN = "your_personal_access_token"
GROUP_ID = 12345678  # Replace with your actual group ID (integer)
PROJECTS_TO_ARCHIVE = []  # Optional: specify project names to archive, e.g., ["project1", "project2"]

# HEADERS
headers = {
    "PRIVATE-TOKEN": ACCESS_TOKEN
}

# Get all projects in a group by ID
def get_group_projects(group_id):
    projects = []
    page = 1
    per_page = 100
    while True:
        url = f"{GITLAB_URL}/api/v4/groups/{group_id}/projects?per_page={per_page}&page={page}&include_subgroups=true"
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            print(f"❌ Failed to fetch projects (page {page}): {response.status_code} - {response.text}")
            break

        page_projects = response.json()
        if not page_projects:
            break

        projects.extend(page_projects)
        page += 1

    return projects

# Archive a project
def archive_project(project_id, project_name):
    url = f"{GITLAB_URL}/api/v4/projects/{project_id}/archive"
    response = requests.post(url, headers=headers)
    if response.status_code == 202:
        print(f"✅ Archived: {project_name}")
    elif response.status_code == 409:
        print(f"⚠️ Already archived or cannot archive: {project_name}")
    else:
        print(f"❌ Failed to archive {project_name}: {response.status_code} - {response.text}")

# Main logic
def main():
    print(f"🔍 Fetching projects for group ID {GROUP_ID}...")
    projects = get_group_projects(GROUP_ID)

    if not projects:
        print("No projects found.")
        return

    print(f"Found {len(projects)} projects.")
    for project in projects:
        name = project['name']
        pid = project['id']

        if PROJECTS_TO_ARCHIVE and name not in PROJECTS_TO_ARCHIVE:
            continue

        if project['archived']:
            print(f"ℹ️ Already archived: {name}")
            continue

        archive_project(pid, name)

if __name__ == "__main__":
    main()
