#!/usr/bin/env bash
# =============================================================================
# LM Chat — Release: tag a version and let CI do the rest
# =============================================================================
#
# What this script does, in full: validate, create one annotated+signed tag,
# push that one tag. Nothing else. The GitHub Actions workflow
# `.github/workflows/release.yml` triggers on `push: tags: v*` and owns
# everything downstream — building the multi-arch image, pushing it to GHCR,
# scanning it with Trivy, and creating the GitHub Release with auto-generated
# notes. This script's job ends the moment the tag lands on the remote.
#
# Usage:
#   scripts/release.sh <version> [--dry-run] [--skip-gates] [--message MSG]
#
#   <version>      e.g. 1.0.3 or v1.0.3 (the 'v' is optional on input; the
#                  tag is always created as v<version>).
#   --dry-run      Run every check, print what would happen, touch nothing.
#   --skip-gates   Skip `make gates` (backend statics + pytest + the routine
#                  frontend suite) / `make security-scan`. Prints a loud
#                  warning. See "Why gates run" below.
#   --message MSG  Override the annotated tag message (default: derived from
#                  the matching CHANGELOG.md section).
#
# Hard constraints (non-negotiable, not configurable by any flag):
#   - Never force-pushes anything.
#   - Never pushes a branch — only ever `git push origin refs/tags/<tag>`.
#   - Never deletes a tag, release, or branch.
#   - Never rewrites history.
#   If a real need for any of that ever comes up, it belongs in a separate,
#   deliberate, human-run command — not in this script. This script exists
#   because the previous one did all four of those things routinely and that
#   is precisely why it had to be deleted.
#
# Why gates run by default:
#   No CI workflow in .github/workflows/ currently runs the backend test
#   suite, pyright, or the frontend typecheck/lint/vitest suite on push —
#   only security/SAST scans (codeql, security-static, container-scan,
#   scorecard) and a stubbed Playwright job that's PR-only. CONTRIBUTING.md's
#   "must be green before a PR" bar (`make gates`, `make security-scan`) is
#   otherwise enforced by nobody but the person running it by hand. For a
#   release — the one moment a bad build becomes a public image — this script
#   is the last real gate, not a redundant one, so it runs what
#   CONTRIBUTING.md documents. The frontend suite is NOT a separate step here:
#   `make gates` runs `web-suite` itself, so invoking it again would run
#   install + typecheck + lint + vitest twice per release. It
#   deliberately does NOT run `make dogfood-live` (10-20 min, needs a live
#   LM Studio with specific models loaded — explicitly "on-demand only" per
#   the Makefile) or `make production-gate` (~10-12 min, Docker+DAST+stress).
#   Those are pre-ship gates meant to be run deliberately and separately;
#   folding them in here would make a routine release take 20+ minutes and
#   the first thing to get `--skip`ped out of habit, which defeats the point
#   of gating at all. `--skip-gates` exists for the case where you already
#   ran them seconds ago and don't want to pay the cost twice — not as a
#   default escape hatch.
#
# =============================================================================

set -euo pipefail

# --- helpers -----------------------------------------------------------------

die() {
  echo "release.sh: ERROR: $*" >&2
  exit 1
}

note() {
  echo "release.sh: $*"
}

warn() {
  echo "release.sh: WARNING: $*" >&2
}

usage() {
  cat <<'EOF'
Usage: scripts/release.sh <version> [--dry-run] [--skip-gates] [--message MSG]

  <version>      e.g. 1.0.3 or v1.0.3
  --dry-run      Run every check, print the plan, change nothing.
  --skip-gates   Skip make gates (includes the frontend suite) / make security-scan.
  --message MSG  Override the annotated tag message.
EOF
}

# a > b, both "MAJOR.MINOR.PATCH"
version_gt() {
  local a="$1" b="$2"
  local a_maj a_min a_pat b_maj b_min b_pat
  IFS='.' read -r a_maj a_min a_pat <<<"$a"
  IFS='.' read -r b_maj b_min b_pat <<<"$b"
  if ((a_maj != b_maj)); then ((a_maj > b_maj)); return; fi
  if ((a_min != b_min)); then ((a_min > b_min)); return; fi
  ((a_pat > b_pat))
}

# --- arg parsing ---------------------------------------------------------------

VERSION_ARG=""
DRY_RUN=0
SKIP_GATES=0
TAG_MESSAGE_OVERRIDE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --skip-gates)
      SKIP_GATES=1
      shift
      ;;
    --message)
      [ $# -ge 2 ] || die "--message needs an argument"
      TAG_MESSAGE_OVERRIDE="$2"
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    -*)
      die "unknown flag: $1 (see --help)"
      ;;
    *)
      if [ -n "$VERSION_ARG" ]; then
        die "unexpected extra argument: $1"
      fi
      VERSION_ARG="$1"
      shift
      ;;
  esac
done

[ -n "$VERSION_ARG" ] || {
  usage
  die "a version argument is required"
}

VERSION="${VERSION_ARG#v}"
TAG="v${VERSION}"

# --- setup ---------------------------------------------------------------------

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

EXPECTED_BRANCH="v1"
REMOTE="origin"
CHANGELOG="CHANGELOG.md"

note "repo root: $REPO_ROOT"
note "target version: $VERSION (tag: $TAG)"
[ "$DRY_RUN" -eq 1 ] && note "DRY RUN — no changes will be made"

# --- preflight: version format -------------------------------------------------

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || die "version '$VERSION' is not well-formed (expected MAJOR.MINOR.PATCH, e.g. 1.0.3)"

# --- preflight: clean working tree ---------------------------------------------

if [ -n "$(git status --porcelain)" ]; then
  echo "--- git status --short ---" >&2
  git status --short >&2
  die "working tree is not clean (uncommitted or untracked changes above)"
fi
note "working tree clean: OK"

# --- preflight: on the expected branch ------------------------------------------

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$CURRENT_BRANCH" = "$EXPECTED_BRANCH" ] \
  || die "on branch '$CURRENT_BRANCH', expected '$EXPECTED_BRANCH' — checkout $EXPECTED_BRANCH first"
note "on expected branch '$EXPECTED_BRANCH': OK"

# --- preflight: version not already tagged locally ------------------------------

git rev-parse --verify -q "refs/tags/$TAG" >/dev/null 2>&1 \
  && die "$TAG already exists locally (git tag -l | grep $TAG)"
note "no local tag $TAG: OK"

# --- fetch (need live remote state for the checks below) ------------------------

note "fetching $REMOTE (tags + refs)..."
git fetch "$REMOTE" --tags --quiet \
  || die "git fetch $REMOTE failed — resolve connectivity/auth before releasing"

# --- preflight: version not already tagged on the remote -------------------------

if git ls-remote --exit-code --tags "$REMOTE" "refs/tags/$TAG" >/dev/null 2>&1; then
  die "$TAG already exists on $REMOTE"
fi
note "no remote tag $TAG: OK"

# --- preflight: newer than the latest existing tag, both by version AND by commit -

# Prerelease tags (v1.2.3-rc.1) sort LOWER than their release per semver §11.4,
# which this script's version_gt() does not implement. Rather than silently
# excluding them from the scan (which would silently disable the newer-than
# check the moment one exists), refuse outright and say why — extend
# version_gt() first if prereleases become real practice here.
while IFS= read -r t; do
  [ -z "$t" ] && continue
  v="${t#v}"
  if [[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+-.+$ ]]; then
    die "existing tag $t looks like a semver prerelease — this script's version comparison doesn't implement prerelease precedence (semver §11.4), so the newer-than-latest check can't be trusted while it exists. Resolve/remove it, or extend version_gt() in this script, before releasing."
  fi
done < <(git tag -l 'v*')

LATEST=""
while IFS= read -r t; do
  [ -z "$t" ] && continue
  v="${t#v}"
  [[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || continue
  if [ -z "$LATEST" ] || version_gt "$v" "$LATEST"; then
    LATEST="$v"
  fi
done < <(git tag -l 'v*')

if [ -n "$LATEST" ]; then
  version_gt "$VERSION" "$LATEST" \
    || die "$VERSION is not newer than the latest existing tag v$LATEST"
  note "newer than latest tag v$LATEST: OK"

  # Version-number ordering and commit ordering are independent — a stale
  # checkout can carry a version bump without carrying the history that
  # produced the last release. Refuse unless HEAD actually descends from
  # the commit v$LATEST points at, so this tag can't land on an older or
  # diverged commit than the release it's supposed to follow.
  git merge-base --is-ancestor "v$LATEST" HEAD \
    || die "HEAD does not descend from v$LATEST (the latest tagged commit) — releasing from here would tag $VERSION at a commit older than, or diverged from, the last release. Check you're on the right branch/commit (git log --oneline v$LATEST..HEAD should be non-empty and one-directional)."
  note "HEAD descends from v$LATEST: OK"
else
  note "no existing v* tags found — skipping newer-than / ancestor checks"
fi

# --- preflight: CHANGELOG.md has this version's section, no stray Unreleased ----

[ -f "$CHANGELOG" ] || die "$CHANGELOG not found"

grep -qiE '^## +unreleased *$' "$CHANGELOG" \
  && die "$CHANGELOG has a '## Unreleased' heading — name it $VERSION or remove it before releasing"

grep -qxF "## $VERSION" "$CHANGELOG" \
  || die "$CHANGELOG has no '## $VERSION' section"

CHANGELOG_SECTION="$(awk -v ver="$VERSION" '
  $0 == "## " ver { found=1; next }
  found && /^## / { exit }
  found { print }
' "$CHANGELOG")"

[ -n "$(echo "$CHANGELOG_SECTION" | tr -d '[:space:]')" ] \
  || die "$CHANGELOG's '## $VERSION' section is empty"

note "CHANGELOG.md has a non-empty ## $VERSION section, no stray Unreleased: OK"

# --- preflight: local branch not behind its remote counterpart -------------------

if git rev-parse --verify -q "refs/remotes/$REMOTE/$EXPECTED_BRANCH" >/dev/null 2>&1; then
  BEHIND="$(git rev-list --count "HEAD..$REMOTE/$EXPECTED_BRANCH")"
  [ "$BEHIND" -eq 0 ] \
    || die "local $EXPECTED_BRANCH is behind $REMOTE/$EXPECTED_BRANCH by $BEHIND commit(s) — pull first"
  note "local $EXPECTED_BRANCH not behind $REMOTE/$EXPECTED_BRANCH: OK"
else
  warn "$REMOTE/$EXPECTED_BRANCH does not exist (no remote counterpart for '$EXPECTED_BRANCH') — 'not behind remote' cannot be checked and is being SKIPPED, not assumed true. Confirm independently that this is expected before proceeding."
fi

# --- preflight: version-declaration files agree with the tag being cut -----------

PYPROJECT_VERSION="$(grep -m1 '^version = "' pyproject.toml | sed -E 's/^version = "([^"]+)".*/\1/')" \
  || die "could not find a 'version = \"...\"' line in pyproject.toml"
INIT_VERSION="$(grep -m1 '__version__ = "' src/lmchat/__init__.py | sed -E 's/.*__version__ = "([^"]+)".*/\1/')" \
  || die "could not find a '__version__ = \"...\"' line in src/lmchat/__init__.py"
PKG_VERSION="$(grep -m1 '"version":' web/package.json | sed -E 's/.*"version": *"([^"]+)".*/\1/')" \
  || die "could not find a \"version\": ... line in web/package.json"

MISMATCH=0
[ "$PYPROJECT_VERSION" = "$VERSION" ] || { warn "pyproject.toml version is $PYPROJECT_VERSION, not $VERSION"; MISMATCH=1; }
[ "$INIT_VERSION" = "$VERSION" ] || { warn "src/lmchat/__init__.py __version__ is $INIT_VERSION, not $VERSION"; MISMATCH=1; }
[ "$PKG_VERSION" = "$VERSION" ] || { warn "web/package.json version is $PKG_VERSION, not $VERSION"; MISMATCH=1; }

[ "$MISMATCH" -eq 0 ] \
  || die "version-declaration files don't all say $VERSION — bump pyproject.toml, src/lmchat/__init__.py, web/package.json (and run 'uv lock') in a commit first"
note "pyproject.toml / __init__.py / package.json all say $VERSION: OK"

# --- gates -----------------------------------------------------------------------

if [ "$SKIP_GATES" -eq 1 ]; then
  warn "--skip-gates set — make gates (includes the frontend suite) / make security-scan will NOT run. This tag is going out unverified by this script."
else
  note "running make gates (pyright + ruff + bandit + frontend suite + pytest + doc/spec checks)..."
  make gates
  note "running make security-scan (bandit + pip-audit + secrets scan)..."
  make security-scan
  note "gates: ALL GREEN"
fi

# --- derive tag message -----------------------------------------------------------

if [ -n "$TAG_MESSAGE_OVERRIDE" ]; then
  TAG_MESSAGE="$TAG_MESSAGE_OVERRIDE"
else
  TAG_MESSAGE="$(printf 'LM Chat %s\n\n%s\n' "$VERSION" "$CHANGELOG_SECTION")"
fi

HEAD_SHA="$(git rev-parse HEAD)"
HEAD_SHORT="$(git rev-parse --short HEAD)"

# --- derive owner/repo for the URLs printed below (never hardcoded) --------------

REMOTE_URL="$(git remote get-url "$REMOTE")"
OWNER_REPO="$(echo "$REMOTE_URL" \
  | sed -E 's#^git@github\.com:##; s#^https://github\.com/##; s#\.git$##')"

# --- plan summary ------------------------------------------------------------------

echo ""
echo "=================================================================="
echo " Release plan"
echo "=================================================================="
echo "  tag:            $TAG"
echo "  commit:         $HEAD_SHA ($HEAD_SHORT)"
echo "  branch:         $CURRENT_BRANCH"
echo "  remote:         $REMOTE ($REMOTE_URL)"
echo "  gates:          $([ "$SKIP_GATES" -eq 1 ] && echo 'SKIPPED' || echo 'passed')"
echo ""
echo "  will run:"
echo "    git tag -s -a \"$TAG\" -m <message below> \"$HEAD_SHA\""
echo "    git push \"$REMOTE\" \"refs/tags/$TAG\""
echo ""
echo "  tag message:"
echo "$TAG_MESSAGE" | sed 's/^/    /'
echo ""
echo "  this pushes ONLY the tag — no branch, no force, no deletions."
echo "=================================================================="
echo ""

if [ "$DRY_RUN" -eq 1 ]; then
  note "DRY RUN complete — nothing was tagged or pushed."
  exit 0
fi

# --- confirm -------------------------------------------------------------------

read -r -p "Type '$TAG' to confirm and push it to $REMOTE: " CONFIRM
[ "$CONFIRM" = "$TAG" ] || die "confirmation did not match '$TAG' — aborted, nothing was tagged or pushed"

# --- do it -----------------------------------------------------------------------

note "creating annotated, signed tag $TAG at $HEAD_SHORT..."
git tag -s -a "$TAG" -m "$TAG_MESSAGE" "$HEAD_SHA"

# Re-check immediately before pushing: the earlier remote-tag check ran before
# gates and the confirmation prompt, minutes ago — enough time for someone
# else (or another session) to have pushed $TAG in the meantime. Catch that
# now rather than pushing blind.
note "re-checking $TAG still doesn't exist on $REMOTE..."
if git ls-remote --exit-code --tags "$REMOTE" "refs/tags/$TAG" >/dev/null 2>&1; then
  die "$TAG now exists on $REMOTE (created since the earlier check). The local tag $TAG was already created here and was NOT pushed — remove it with 'git tag -d $TAG' before investigating what pushed $TAG upstream."
fi

note "pushing $TAG to $REMOTE (this ref only)..."
if ! git push "$REMOTE" "refs/tags/$TAG"; then
  echo "" >&2
  echo "release.sh: ERROR: push of $TAG to $REMOTE failed." >&2
  echo "release.sh: the local tag $TAG still exists — it was NOT pushed, so no workflow has been triggered." >&2
  echo "release.sh: remove it with: git tag -d $TAG" >&2
  echo "release.sh: then resolve whatever the push error above says and re-run." >&2
  exit 1
fi

echo ""
note "$TAG pushed. The 'Release' workflow (.github/workflows/release.yml) is now building:"
note "  - multi-arch image (linux/amd64, linux/arm64) -> ghcr.io/${OWNER_REPO,,}"
note "  - Trivy scan of the built image"
note "  - a GitHub Release at $TAG with auto-generated notes"
note ""
note "watch it: https://github.com/$OWNER_REPO/actions"
note "release page once it completes: https://github.com/$OWNER_REPO/releases/tag/$TAG"
