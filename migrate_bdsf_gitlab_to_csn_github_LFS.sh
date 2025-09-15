#!/usr/bin/env bash
set -euo pipefail

# ================== Global config ==================
umask 077

: "${GITLAB_BASE_URL:?Set GITLAB_BASE_URL (e.g. https://gitlab.dsf.boozallencsn.com)}"
: "${GITLAB_TOKEN:?Set GITLAB_TOKEN}"
: "${GITHUB_TOKEN:?Set GITHUB_TOKEN}"
: "${GITHUB_ORG:?Set GITHUB_ORG}"


GROUP_ID="${1:-}"
SINGLE_REPO_URL="${2:-}"
if [[ -z "$GROUP_ID" ]]; then
  echo "Usage: $0 <GITLAB_TOP_GROUP_ID> [GITLAB_REPO_URL]" >&2
  exit 1
fi

# Tunables (env‑overridable)
INCLUDE_ARCHIVED="${INCLUDE_ARCHIVED:-true}"
LFS_ABOVE="${LFS_ABOVE:-100MB}"
MIGRATE_CONCURRENCY="${MIGRATE_CONCURRENCY:-3}"  # number of repos in flight
LFS_CONCURRENCY="${LFS_CONCURRENCY:-4}"
SKIP_EXISTING_GH="${SKIP_EXISTING_GH:-true}"     # skip if GH repo exists & non‑empty
REWRITE_SUBMODULES="${REWRITE_SUBMODULES:-false}" # rewrite .gitmodules URLs to GitHub
DRY_RUN="${DRY_RUN:-false}"

# Work directories
ROOT_DIR="${MIRROR_ROOT:-$PWD/migration-work}"
SRC_DIR="$ROOT_DIR/source"
STATE_DIR="$ROOT_DIR/state"
LOG_DIR="$ROOT_DIR/logs"
mkdir -p "$SRC_DIR" "$STATE_DIR" "$LOG_DIR"

# Safer LFS defaults
export GIT_LFS_SKIP_SMUDGE=1
git lfs install --skip-smudge >/dev/null 2>&1 || true
git config --global lfs.concurrenttransfers "$LFS_CONCURRENCY"

# ---------- Logging & helpers ----------
ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "[$(ts)] $*"; }
warn() { echo "[$(ts)] WARN: $*" >&2; }
die() { echo "[$(ts)] ERROR: $*" >&2; exit 1; }

# retries with jitter
retry() {
  local max=${1:-5}; shift
  local n=0
  local delay=2
  local rc
  while true; do
    set +e
    "$@"
    rc=$?
    set -e
    if [[ $rc -eq 0 ]]; then return 0; fi
    n=$((n+1))
    if [[ $n -ge $max ]]; then return $rc; fi
    sleep $((delay + RANDOM % 3))
    delay=$((delay * 2))
  done
}

curl_gl() {
  retry 5 curl -sS --fail \
    -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
    -H "Content-Type: application/json" \
    "$@"
}

curl_gh() {
  # Back off on secondary rate limits if needed
  retry 5 curl -sS --fail \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "$@"
}

slugify_repo_name() { echo "${1//\//-}"; }

# ---------- Keyset pagination (fallbacks to page headers) ----------
list_group_projects() {
  local page_size=100
  local url="$GITLAB_BASE_URL/api/v4/groups/$GROUP_ID/projects?include_subgroups=true&per_page=$page_size&order_by=id&sort=asc&pagination=keyset&archived=$INCLUDE_ARCHIVED"
  local results=()
  while :; do
    # include headers
    local response headers body next
    response=$(retry 5 curl -sS --fail --include -H "PRIVATE-TOKEN: $GITLAB_TOKEN" "$url")
    headers=$(printf "%s" "$response" | sed -n '1,/^\r$/p')
    body=$(printf "%s" "$response" | sed '1,/^\r$/d')
    results+=("$body")
    next=$(printf "%s" "$headers" | awk -F'[<>]' '/rel="next"/{print $2}')
    if [[ -n "$next" ]]; then
      url="$next"
    else
      # fallback for instances without keyset: X-Next-Page
      local np
      np=$(printf "%s" "$headers" | awk '/^X-Next-Page:/ {print $2}' | tr -d '\r')
      if [[ -z "$np" ]]; then break; fi
      url="$GITLAB_BASE_URL/api/v4/groups/$GROUP_ID/projects?include_subgroups=true&per_page=$page_size&page=$np&archived=$INCLUDE_ARCHIVED"
    fi
  done
  jq -s 'add' <<<"$(printf "%s\n" "${results[@]}")"
}

# ---------- GitHub repo operations ----------
gh_repo_exists_nonempty() {
  local name="$1"
  local resp
  resp=$(curl_gh -o /dev/stderr -w "%{http_code}" "https://api.github.boozallencsn.com/repos/$GITHUB_ORG/$name") || return 1
  [[ "$resp" == "200" ]] || return 1
  # consider non‑empty if it has any refs
  local tmp
  tmp=$(mktemp)
  GIT_ASKPASS= GIT_TERMINAL_PROMPT=0 git ls-remote "https://oauth2:${GITHUB_TOKEN}@github.boozallencsn.com/$GITHUB_ORG/$name.git" >"$tmp" 2>/dev/null || true
  [[ -s "$tmp" ]]
}

create_or_update_gh_repo() {
  local name="$1" description="$2" default_branch="$3"
  if curl_gh -o /dev/null -w "%{http_code}" "https://github.boozallencsn.com/api/v3/repos/$GITHUB_ORG/$name" | grep -q '^200$'; then
    log "GitHub repo exists: $GITHUB_ORG/$name"
  else
    log "Creating GitHub repo: $GITHUB_ORG/$name"
    curl_gh -X POST "https://github.boozallencsn.com/api/v3/orgs/$GITHUB_ORG/repos" \
      -d @- <<JSON >/dev/null
{"name":"$name","private":true,"has_issues":true,"has_projects":false,"has_wiki":false,"description":$(jq -Rn --arg d "$description" '$d')}
JSON
  fi
  # set default branch to match source (after push completes we set again)
  if [[ -n "$default_branch" ]]; then
  curl_gh -X PATCH "https://github.boozallencsn.com/api/v3/repos/$GITHUB_ORG/$name" \
      -d "{\"default_branch\":\"$default_branch\"}" >/dev/null || true
  fi
}

# Use a temporary, repo‑local credential helper instead of embedding tokens in remotes that may leak to shell history
with_temp_credentials() {
  # usage: with_temp_credentials repo_dir git push ...
  local repo="$1"; shift
  (
    cd "$repo"
    git config credential.helper ''
    git config --local credential.useHttpPath true
  git config --local http.https://github.boozallencsn.com/.extraheader "AUTHORIZATION: basic $(printf "oauth2:%s" "$GITHUB_TOKEN" | base64)"
    "$@"
  )
}

push_with_mirror() {
  local repo_path="$1" gh_url="$2"
  (
    cd "$repo_path"
    git lfs install --local
    git remote remove github 2>/dev/null || true
    git remote add github "$gh_url"
    with_temp_credentials "$repo_path" git push --prune --mirror github
    if ! with_temp_credentials "$repo_path" git lfs push --all github; then
      warn "LFS push failed for $gh_url. Some large files may not have been uploaded."
    fi
  )
}

# ---------- LFS: fast pre‑scan & migration ----------
# returns 0 if any blob >= threshold exists, else 1
repo_has_large_blobs() {
  local repo="$1" thresh_bytes
  # convert e.g. 100MB/2GB/750k to bytes
  local t="$LFS_ABOVE"
  case "$t" in
    *KB|*kb|*Kb|*kB) thresh_bytes=$(( ${t%[Kk][Bb]} * 1024 ));;
    *MB|*mb|*Mb|*mB) thresh_bytes=$(( ${t%[Mm][Bb]} * 1024 * 1024 ));;
    *GB|*gb|*Gb|*gB) thresh_bytes=$(( ${t%[Gg][Bb]} * 1024 * 1024 * 1024 ));;
    *k) thresh_bytes=$(( ${t%k} * 1024 ));;
    *M) thresh_bytes=$(( ${t%M} * 1024 * 1024 ));;
    *G) thresh_bytes=$(( ${t%G} * 1024 * 1024 * 1024 ));;
    *) thresh_bytes="$t";;
  esac
  (
    cd "$repo"
    # enumerate all blobs reachable from any ref; check sizes
    git rev-list --objects --all \
      | cut -d' ' -f1 \
      | git cat-file --batch-check='%(objectname) %(objecttype) %(objectsize)' \
      | awk -v th="$thresh_bytes" '$2=="blob" && $3>=th {exit 0} END{exit 1}'
  )
}

run_lfs_migrate_if_needed() {
  local workdir="$1"
  (
    cd "$workdir"
    git lfs install --local
    if repo_has_large_blobs "$workdir"; then
      log "LFS migrate (>= $LFS_ABOVE) on all refs..."
      git lfs migrate import --everything --above="$LFS_ABOVE"
      # optional: rewrite submodule URLs to GitHub org
      if [[ "$REWRITE_SUBMODULES" == "true" ]] && [[ -f .gitmodules ]]; then
        log "Rewriting submodule URLs to GitHub org '$GITHUB_ORG'..."
  sed -i.bak -E "s#(url = ).*?[/:]([^/]+/.*)\.git#\1https://github.boozallencsn.com/${GITHUB_ORG}/\2.git#g;s#/#-#3g" .gitmodules || true
        git add .gitmodules || true
        git commit -m "Rewrite submodule URLs to $GITHUB_ORG (automated)" || true
      fi
      # Always push LFS objects after migration
      if ! git lfs push --all origin; then
        warn "LFS push to origin failed after migration. Some large files may not have been uploaded."
      fi
    else
      log "No blobs >= $LFS_ABOVE found; skipping LFS migration."
    fi
  )
}

# ---------- Verification ----------
verify_push() {
  local mirror_dir="$1" gh="https://oauth2:${GITHUB_TOKEN}@github.boozallencsn.com/$GITHUB_ORG/$2.git"
  (
    cd "$mirror_dir"
    # compare set of refs (names and oids)
    local local_refs remote_refs
    local_refs=$(git for-each-ref --format='%(refname):%(objectname)')
    remote_refs=$(GIT_ASKPASS= GIT_TERMINAL_PROMPT=0 git ls-remote "$gh" | awk '{print $2":"$1}')
    # ensure every local ref exists remotely with same oid
    local missing=0
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      local ref=${line%%:*}
      local oid=${line##*:}
      if ! grep -q "^${ref}:${oid}$" <<<"$remote_refs"; then
        warn "Ref mismatch or missing on GitHub: $ref"
        missing=1
      fi
    done <<<"$local_refs"
    # ensure no pending LFS objects
    with_temp_credentials "$mirror_dir" git lfs push --all --dry-run "$gh" | grep -q . && {
      warn "LFS objects still pending for $gh"
      missing=1
    }
    return $missing
  )
}

# ---------- State handling ----------
mark_state() { echo "$(ts) $2" >> "$STATE_DIR/$1.state"; }
has_state()  { grep -q "$2" "$STATE_DIR/$1.state" 2>/dev/null; }

# ---------- Migrate one project ----------
migrate_one_project() {
  local name="$1" pwn="$2" http_url="$3" ssh_url="$4" description="$5" default_branch="$6"

  local slug; slug="$(slugify_repo_name "$pwn")"
  local mirror_dir="$SRC_DIR/${slug}.git"
  local workdir="$SRC_DIR/${slug}-work"
  local logf="$LOG_DIR/${slug}.log"

  {
    log "=== [$pwn] ($name) ==="

    if [[ "$SKIP_EXISTING_GH" == "true" ]] && gh_repo_exists_nonempty "$slug"; then
      log "GitHub repo non‑empty; skipping migration. ($GITHUB_ORG/$slug)"
      mark_state "$slug" "skipped-existing"
      return 0
    fi

    # Clone/update mirror
    if has_state "$slug" "mirror-cloned"; then
      log "Mirror exists; fetching updates..."
      retry 5 bash -c "cd \"$mirror_dir\" && git remote set-url origin \"$http_url\" && git fetch --prune --all --tags"
    else
      log "Cloning mirror: $http_url -> $mirror_dir"
      retry 5 git clone --mirror "$http_url" "$mirror_dir"
      mark_state "$slug" "mirror-cloned"
    fi

    # Make a working copy for migration
    rm -rf "$workdir"
    git clone "$mirror_dir" "$workdir"

    # LFS migration (if needed)
    run_lfs_migrate_if_needed "$workdir"

    # Replace mirror with rewritten refs
    log "Syncing rewritten refs back to mirror..."
    (
      cd "$workdir"
      git remote remove origin
      git remote add origin "$mirror_dir"
      git push --prune --mirror origin
    )
    mark_state "$slug" "lfs-migrated"

    # Create or update GitHub repo (set tentative default branch)
    if [[ "$DRY_RUN" == "true" ]]; then
      log "DRY_RUN: would create/update GitHub repo $GITHUB_ORG/$slug"
    else
      create_or_update_gh_repo "$slug" "$description" "$default_branch"
    fi

    # Push to GitHub
    if [[ "$DRY_RUN" == "true" ]]; then
      log "DRY_RUN: would push --mirror and LFS to github.boozallencsn.com/$GITHUB_ORG/$slug"
    else
      push_with_mirror "$mirror_dir" "https://github.boozallencsn.com/$GITHUB_ORG/$slug.git"
      mark_state "$slug" "pushed"
    fi

    # Verification
    if [[ "$DRY_RUN" != "true" ]]; then
      if verify_push "$mirror_dir" "$slug"; then
        log "✅ Verified: refs and LFS present on GitHub."
      else
        warn "Verification found differences. Attempting final LFS push..."
  with_temp_credentials "$mirror_dir" git lfs push --all "https://github.boozallencsn.com/$GITHUB_ORG/$slug.git" || true
      fi
      # Enforce default branch again (now refs exist)
      if [[ -n "$default_branch" ]]; then
        curl_gh -X PATCH "https://github.boozallencsn.com/api/v3/repos/$GITHUB_ORG/$slug" \
          -d "{\"default_branch\":\"$default_branch\"}" >/dev/null || true
      fi
    fi

    log "🎯 Migrated: $pwn → github.boozallencsn.com/$GITHUB_ORG/$slug"
  } 2>&1 | tee -a "$logf"
}

# ---------- Concurrency primitives (bash semaphore) ----------
sem_init() {
  mkfifo "$STATE_DIR/.sem.$$"
  exec 3<>"$STATE_DIR/.sem.$$"
  rm -f "$STATE_DIR/.sem.$$"
  local i
  for ((i=0;i<MIGRATE_CONCURRENCY;i++)); do echo >&3; done
}
sem_wait() { read -r -u 3; }
sem_post() { echo >&3; }
sem_close(){ exec 3>&- || true; }

# ================== MAIN ==================
log "Discovering projects under GitLab group $GROUP_ID (include_archived=$INCLUDE_ARCHIVED)"

if [[ -n "$SINGLE_REPO_URL" ]]; then
  log "Targeting single repo: $SINGLE_REPO_URL"
  # Fetch project info from GitLab API
  project_json=$(curl_gl "$GITLAB_BASE_URL/api/v4/projects/$(python3 -c "import urllib.parse; print(urllib.parse.quote('''${SINGLE_REPO_URL#*://*/}''', safe=''))")")
  name=$(jq -r '.name // .path' <<<"$project_json")
  pwn=$(jq -r '.path_with_namespace' <<<"$project_json")
  http_url=$(jq -r '.http_url_to_repo' <<<"$project_json")
  ssh_url=$(jq -r '.ssh_url_to_repo' <<<"$project_json")
  desc=$(jq -r '.description' <<<"$project_json")
  defb=$(jq -r '.default_branch' <<<"$project_json")
  migrate_one_project "$name" "$pwn" "$http_url" "$ssh_url" "$desc" "$defb"
  log "🎉 Done. Mirrored single repo to GitHub org '$GITHUB_ORG' with ≥$LFS_ABOVE stored via Git LFS."
else
  projects_json="$(list_group_projects)"
  count=$(jq 'length' <<<"$projects_json")
  if [[ "$count" -eq 0 ]]; then
    log "No projects found. Exiting."
    exit 0
  fi
  log "Found $count project(s)."

  sem_init
  jq -c '.[] | {
    name: (.name // .path),
    path_with_namespace,
    http_url_to_repo,
    ssh_url_to_repo,
    description: (.description // ""),
    default_branch: (.default_branch // "")
  }' <<<"$projects_json" | while IFS= read -r proj; do
    sem_wait
    {
      name=$(jq -r '.name' <<<"$proj")
      pwn=$(jq -r '.path_with_namespace' <<<"$proj")
      http_url=$(jq -r '.http_url_to_repo' <<<"$proj")
      ssh_url=$(jq -r '.ssh_url_to_repo' <<<"$proj")
      desc=$(jq -r '.description' <<<"$proj")
      defb=$(jq -r '.default_branch' <<<"$proj")
      migrate_one_project "$name" "$pwn" "$http_url" "$ssh_url" "$desc" "$defb"
      sem_post
    } &
  done
  wait
  sem_close

  log "🎉 All done. Mirrored to GitHub org '$GITHUB_ORG' with ≥$LFS_ABOVE stored via Git LFS."
fi
