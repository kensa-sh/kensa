#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
ROOT="$(cd -P "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"

die() {
  echo "error: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
usage:
  ./scripts/release.sh <patch|minor|major> [--dry-run] [--run-tests]
  ./scripts/release.sh publish [--dry-run] [--run-tests]

Release flow:
  1. Run patch/minor/major to open a version-bump PR.
  2. Merge the PR after CI passes.
  3. Run publish from main to push the tag. The tag workflow publishes to PyPI
     and creates the GitHub Release with generated notes.

Examples:
  ./scripts/release.sh patch --dry-run
  ./scripts/release.sh patch
  ./scripts/release.sh minor --run-tests
  ./scripts/release.sh publish --dry-run
  ./scripts/release.sh publish
EOF
}

version_valid() {
  printf '%s' "$1" | grep -Eq '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
}

current_version() {
  awk -F'"' '/^version = / { print $2; exit }' "$ROOT/pyproject.toml"
}

lock_version() {
  awk '
    $0 == "name = \"kensa\"" { found = 1; next }
    found && /^version = / {
      gsub(/"/, "", $3)
      print $3
      exit
    }
  ' "$ROOT/uv.lock"
}

next_version() {
  local bump="$1"
  local current="$2"
  local major minor patch

  version_valid "$current" || die "invalid current version: $current"
  IFS=. read -r major minor patch <<EOF
$current
EOF

  case "$bump" in
    major)
      major=$((major + 1))
      minor=0
      patch=0
      ;;
    minor)
      minor=$((minor + 1))
      patch=0
      ;;
    patch)
      patch=$((patch + 1))
      ;;
    *) die "unknown bump: $bump" ;;
  esac

  printf '%s.%s.%s\n' "$major" "$minor" "$patch"
}

current_branch() {
  git -C "$ROOT" rev-parse --abbrev-ref HEAD
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required"
}

assert_clean_tree() {
  [ -z "$(git -C "$ROOT" status --porcelain)" ] || die "working tree must be clean before release"
}

assert_main_branch() {
  local branch
  branch="$(current_branch)"
  [ "$branch" = "main" ] || die "must release from main, on $branch"
}

assert_head_matches_origin_main() {
  local local_head origin_head
  git -C "$ROOT" fetch --quiet origin main || die "failed to fetch origin/main"
  local_head="$(git -C "$ROOT" rev-parse HEAD)"
  origin_head="$(git -C "$ROOT" rev-parse FETCH_HEAD)"
  [ "$local_head" = "$origin_head" ] || die "local HEAD must match origin/main before release"
}

assert_remote_ref_missing() {
  local kind="$1"
  local ref="$2"
  local status

  set +e
  git -C "$ROOT" ls-remote --exit-code "--$kind" origin "$ref" >/dev/null 2>&1
  status=$?
  set -e

  case "$status" in
    0) die "$kind $ref already exists on origin" ;;
    2) ;;
    *) die "failed to check origin for $kind $ref" ;;
  esac
}

assert_tag_available() {
  local tag="$1"
  if git -C "$ROOT" rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
    die "tag $tag already exists locally"
  fi
  assert_remote_ref_missing tags "refs/tags/$tag"
}

assert_branch_available() {
  local branch="$1"
  if git -C "$ROOT" rev-parse -q --verify "refs/heads/$branch" >/dev/null; then
    die "branch $branch already exists locally"
  fi
  assert_remote_ref_missing heads "refs/heads/$branch"
}

assert_version_files() {
  local version="$1"
  [ "$(current_version)" = "$version" ] || die "pyproject.toml version does not match $version"
  [ "$(lock_version)" = "$version" ] || die "uv.lock version does not match $version"
}

run_local_checks() {
  uv run ruff check .
  uv run ruff format --check .
  uv run ty check
  uv run pytest -q -m "not live"
}

release_pr_body() {
  local version="$1"
  cat <<EOF
Release PR for v$version.

After this merges, publish with:

\`\`\`bash
./scripts/release.sh publish
\`\`\`
EOF
}

prepare_release_pr() {
  local bump="$1"
  local dry_run="$2"
  local run_tests="$3"
  local current version tag branch title

  require_command git
  require_command uv

  current="$(current_version)"
  version="$(next_version "$bump" "$current")"
  tag="v$version"
  branch="chore/release-$tag"
  title="chore: release $version"

  if [ "$dry_run" = true ]; then
    echo "current: $current"
    echo "next: $version ($tag)"
    echo "would require clean main branch matching origin/main"
    echo "would require available branch: $branch"
    echo "would require available tag: $tag"
    echo "would update pyproject.toml and uv.lock"
    if [ "$run_tests" = true ]; then
      echo "would run local ruff, ty, and pytest checks"
    else
      echo "would skip local checks; PR CI remains authoritative"
    fi
    echo "would commit: $title"
    echo "would push branch: $branch"
    echo "would open PR: $title"
    return 0
  fi

  require_command gh
  assert_main_branch
  assert_head_matches_origin_main
  assert_clean_tree
  assert_branch_available "$branch"
  assert_tag_available "$tag"

  git -C "$ROOT" switch -c "$branch"
  uv version "$version" --no-sync >/dev/null
  assert_version_files "$version"

  if [ "$run_tests" = true ]; then
    run_local_checks
  else
    echo "skip local checks; PR CI remains authoritative"
  fi

  git -C "$ROOT" add pyproject.toml uv.lock
  git -C "$ROOT" commit -m "$title"
  git -C "$ROOT" push -u origin "$branch"
  release_pr_body "$version" | gh pr create --base main --head "$branch" --title "$title" --body-file -

  echo "opened release PR for $tag"
}

publish_release() {
  local dry_run="$1"
  local run_tests="$2"
  local version tag

  require_command git

  version="$(current_version)"
  version_valid "$version" || die "invalid current version: $version"
  tag="v$version"

  if [ "$dry_run" = true ]; then
    echo "version: $version"
    echo "tag: $tag"
    echo "would require clean main branch matching origin/main"
    echo "would require pyproject.toml and uv.lock to match"
    echo "would require available tag: $tag"
    if [ "$run_tests" = true ]; then
      echo "would run local ruff, ty, and pytest checks"
    else
      echo "would skip local checks; tag workflow runs the release gates"
    fi
    echo "would create annotated tag: $tag"
    echo "would push tag: $tag"
    echo "tag workflow publishes to PyPI and creates the GitHub Release"
    return 0
  fi

  assert_main_branch
  assert_head_matches_origin_main
  assert_clean_tree
  assert_version_files "$version"
  assert_tag_available "$tag"

  if [ "$run_tests" = true ]; then
    run_local_checks
  else
    echo "skip local checks; tag workflow runs the release gates"
  fi

  git -C "$ROOT" tag -a "$tag" -m "Release $tag"
  git -C "$ROOT" push origin "$tag"

  echo "published $tag"
  echo "GitHub Actions will publish kensa==$version to PyPI and create the GitHub Release."
}

main() {
  local action=""
  local dry_run=false
  local run_tests=false

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dry-run) dry_run=true ;;
      --run-tests) run_tests=true ;;
      -h|--help) usage; return 0 ;;
      --*)
        usage >&2
        die "unknown option: $1"
        ;;
      *)
        [ -z "$action" ] || die "only one action argument is allowed"
        action="$1"
        ;;
    esac
    shift
  done

  case "$action" in
    patch|minor|major) prepare_release_pr "$action" "$dry_run" "$run_tests" ;;
    publish) publish_release "$dry_run" "$run_tests" ;;
    "")
      usage >&2
      return 2
      ;;
    *)
      usage >&2
      die "invalid action: $action"
      ;;
  esac
}

main "$@"
