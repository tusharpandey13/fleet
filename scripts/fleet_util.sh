#!/bin/bash
# Fleet bash utilities — git/gh CLI wrappers and path resolution

set -euo pipefail

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# ─────────────────────────────────────────────────────────────────────────────
# Git wrappers
# ─────────────────────────────────────────────────────────────────────────────

git_rev_parse() {
  # Parse a git ref to its SHA
  # Usage: git_rev_parse <ref>
  # Returns: SHA, or error if ref not found
  local ref="${1:?git_rev_parse: ref required}"
  git rev-parse "$ref" 2>/dev/null || {
    echo "error: git_rev_parse: $ref not found" >&2
    return 1
  }
}

git_merge_base() {
  # Find merge-base between two refs
  # Usage: git_merge_base <ref1> <ref2>
  # Returns: Merge-base SHA
  local ref1="${1:?git_merge_base: ref1 required}"
  local ref2="${2:?git_merge_base: ref2 required}"
  git merge-base "$ref1" "$ref2" 2>/dev/null || {
    echo "error: git_merge_base: failed to find merge-base" >&2
    return 1
  }
}

git_branch_show_current() {
  # Get the current branch name
  # Usage: git_branch_show_current
  # Returns: Branch name, or "HEAD" if detached
  git branch --show-current 2>/dev/null || echo "HEAD"
}

git_worktree_list() {
  # List all worktrees
  # Usage: git_worktree_list
  # Returns: Lines of worktree info (path, branch, detached)
  git worktree list 2>/dev/null || {
    echo "error: git_worktree_list: no worktrees" >&2
    return 1
  }
}

git_rebase_update_refs() {
  # Execute a rebase with --update-refs flag
  # Usage: git_rebase_update_refs <base> <tip>
  # Returns: 0 on success, 1 on conflict
  local base="${1:?git_rebase_update_refs: base required}"
  local tip="${2:?git_rebase_update_refs: tip required}"

  git config rebase.updateRefs true || true
  git rebase --update-refs "$base".."$tip" && return 0 || {
    local status=$?
    if [ $status -eq 1 ]; then
      echo "error: git_rebase_update_refs: merge conflict" >&2
      return 1
    else
      echo "error: git_rebase_update_refs: rebase failed (status: $status)" >&2
      return 1
    fi
  }
}

git_push_force_with_lease() {
  # Force-push with lease safety
  # Usage: git_push_force_with_lease <remote> <branch>
  # Returns: 0 on success, 1 on lease fail
  local remote="${1:?git_push_force_with_lease: remote required}"
  local branch="${2:?git_push_force_with_lease: branch required}"

  git push --force-with-lease "$remote" "$branch" && return 0 || {
    local status=$?
    if [ $status -eq 1 ]; then
      echo "error: git_push_force_with_lease: lease failed (remote tip changed)" >&2
      return 1
    else
      echo "error: git_push_force_with_lease: push failed (status: $status)" >&2
      return 1
    fi
  }
}

# ─────────────────────────────────────────────────────────────────────────────
# Path resolution
# ─────────────────────────────────────────────────────────────────────────────

fleet_home() {
  # Get fleet home directory
  # Usage: fleet_home
  # Returns: ~/.fleet path
  echo "$HOME/.fleet"
}

feature_path() {
  # Get feature directory
  # Usage: feature_path <feature>
  # Returns: ~/.fleet/<feature> path
  local feature="${1:?feature_path: feature required}"
  echo "$(fleet_home)/$feature"
}

manifest_path() {
  # Get manifest file path
  # Usage: manifest_path <feature>
  # Returns: ~/.fleet/<feature>/manifest.yaml path
  local feature="${1:?manifest_path: feature required}"
  echo "$(feature_path "$feature")/manifest.yaml"
}

wal_path() {
  # Get WAL file path
  # Usage: wal_path <feature>
  # Returns: ~/.fleet/<feature>/wal.jsonl path
  local feature="${1:?wal_path: feature required}"
  echo "$(feature_path "$feature")/wal.jsonl"
}

lock_path() {
  # Get lock file path
  # Usage: lock_path <feature>
  # Returns: ~/.fleet/<feature>/.fleet.lock path
  local feature="${1:?lock_path: feature required}"
  echo "$(feature_path "$feature")/.fleet.lock"
}

worktree_pool_root() {
  # Get worktree pool directory
  # Usage: worktree_pool_root <feature>
  # Returns: ~/.fleet/<feature>/wt path
  local feature="${1:?worktree_pool_root: feature required}"
  echo "$(feature_path "$feature")/wt"
}

# ─────────────────────────────────────────────────────────────────────────────
# Utility functions
# ─────────────────────────────────────────────────────────────────────────────

die() {
  # Print error message and exit
  # Usage: die <message>
  echo -e "${RED}✗ $*${NC}" >&2
  exit 1
}

warn() {
  # Print warning message
  # Usage: warn <message>
  echo -e "${YELLOW}⚠ $*${NC}" >&2
}

ok() {
  # Print success message
  # Usage: ok <message>
  echo -e "${GREEN}✓ $*${NC}"
}

ensure_dir() {
  # Ensure directory exists
  # Usage: ensure_dir <path>
  local path="${1:?ensure_dir: path required}"
  mkdir -p "$path" || die "Failed to create directory: $path"
}

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  # Script is being run directly; run basic self-test
  echo "Fleet bash utilities"
  echo "Available functions:"
  echo "  - git_rev_parse <ref>"
  echo "  - git_merge_base <ref1> <ref2>"
  echo "  - git_branch_show_current"
  echo "  - git_worktree_list"
  echo "  - git_rebase_update_refs <base> <tip>"
  echo "  - git_push_force_with_lease <remote> <branch>"
  echo "  - fleet_home"
  echo "  - feature_path <feature>"
  echo "  - manifest_path <feature>"
  echo "  - wal_path <feature>"
  echo "  - lock_path <feature>"
  echo "  - worktree_pool_root <feature>"
fi
