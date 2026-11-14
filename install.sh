#!/usr/bin/env bash
#
# install.sh — side-load the Musqet POS integration (pos_musqet) into Odoo 20
# (or Odoo 19, from a 19.0 ref).
#
# Places the pos_musqet addon onto an Odoo addons path (fetched from GitHub via git,
# or curl/wget when git is absent — e.g. the stock odoo image — or copied from a
# local checkout) and OPTIONALLY installs it into a database. Until pos_musqet
# is on the Odoo App Store this is the supported way to install it; it is also what
# the Musqet demo image runs at build time.
#
# Two operations, deliberately separable:
#   - placing the files needs no database and is safe inside a Docker build;
#   - installing the module (--db) needs a running Odoo + Postgres.
#
# Usage:
#   install.sh --addons-path DIR [options] [-- <extra odoo args>]
#
# Options:
#   --addons-path DIR   Where to place pos_musqet/ (must be on Odoo's addons_path). Required.
#   --db NAME           Also install/upgrade the module into this database (needs odoo + DB).
#   --ref REF           Release tag, full commit SHA or branch to install. Required unless
#                       --source. Use a release from the Releases page in production. A tag
#                       can be moved; a full 40-character SHA is checked against what is
#                       fetched. A branch (20.0, or 19.0 for Odoo 19) is unreleased
#                       development code.
#   --repo URL          Source repo (default: https://github.com/Musqet/musqet-odoo.git).
#   --source DIR        Use a local checkout instead of cloning (no git/network needed).
#   --odoo-bin CMD      Odoo executable (default: odoo).
#   --restart CMD       Shell command to restart Odoo after install (e.g. "systemctl restart odoo").
#   --upgrade           Use -u (upgrade) instead of -i (install) for --db.
#   -h, --help          Show this help.
#
# Anything after `--` is forwarded to the odoo install command, e.g. to point at a
# config file or pass DB connection args:
#   install.sh --addons-path /opt/odoo/extra --db live -- -c /etc/odoo/odoo.conf
#
# Examples:
#   install.sh --addons-path /mnt/extra-addons --ref v20.0.1.7.0     # place only (Docker build)
#   install.sh --addons-path /opt/odoo/extra --ref v20.0.1.7.0 --db live \
#              --restart "sudo systemctl restart odoo"               # merchant: place + install
#   install.sh --addons-path /mnt/extra-addons --ref 20.0            # development code
#   install.sh --source . --addons-path /mnt/extra-addons           # from a local checkout
#
set -euo pipefail
umask 022   # ensure placed files are not group/world-writable regardless of caller

REPO_URL="https://github.com/Musqet/musqet-odoo.git"
REF=""
ref_lower=""          # REF lowercased, set once REF is checked
ADDONS_PATH=""
SOURCE_DIR=""
DB=""
ODOO_BIN="odoo"
RESTART_CMD=""
ACTION="-i"            # -i install / -u upgrade
MODULE="pos_musqet"

err()  { printf 'install.sh: %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }
info() { printf '==> %s\n' "$*"; }

# The ref goes on git's command line and into a download URL: accept only ASCII
# letters, digits and ._/+- (never first, and no ".."), so it can't read as an option
# or change the URL. LC_ALL=C keeps [[:alnum:]] to ASCII whatever the caller's locale.
plain_ref() {
  local LC_ALL=C
  [[ "$1" =~ ^[[:alnum:]][[:alnum:]._/+-]*$ && "$1" != *..* ]]
}

usage() {
  cat >&2 <<'EOF'
Usage: install.sh --addons-path DIR [options] [-- <extra odoo args>]

  --addons-path DIR   Where to place pos_musqet/ (must be on Odoo's addons_path). Required.
  --db NAME           Also install/upgrade the module into this database.
  --ref REF           Release tag, full commit SHA or branch. Required unless --source.
                      Use a release in production. A tag can be moved; a full
                      40-character SHA is checked against what is fetched.
                      A branch (20.0, or 19.0) is development code.
  --repo URL          Source repo (default: Musqet/musqet-odoo).
  --source DIR        Use a local checkout instead of cloning.
  --odoo-bin CMD      Odoo executable (default: odoo).
  --restart CMD       Command to restart Odoo after install.
  --upgrade           Use -u instead of -i for --db.
  -h, --help          Show this help.

Anything after `--` is forwarded to the odoo install command.
EOF
  exit "${1:-0}"
}

EXTRA_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --addons-path) ADDONS_PATH="${2:?--addons-path needs a value}"; shift 2;;
    --source)      SOURCE_DIR="${2:?--source needs a value}"; shift 2;;
    --repo)        REPO_URL="${2:?--repo needs a value}"; shift 2;;
    --ref)         REF="${2:?--ref needs a value}"; shift 2;;
    --db)          DB="${2:?--db needs a value}"; shift 2;;
    --odoo-bin)    ODOO_BIN="${2:?--odoo-bin needs a value}"; shift 2;;
    --restart)     RESTART_CMD="${2:?--restart needs a value}"; shift 2;;
    --upgrade)     ACTION="-u"; shift;;
    -h|--help)     usage 0;;
    --)            shift; EXTRA_ARGS=("$@"); break;;
    *)             err "unknown option: $1"; usage 1;;
  esac
done

[[ -n "$ADDONS_PATH" ]] || { err "missing required --addons-path DIR"; usage 1; }

# No default ref: a silent default would install whatever that branch holds today, so
# a rebuild could pull different code. The caller names what they install.
if [[ -n "$SOURCE_DIR" ]]; then
  [[ -z "$REF" ]] || err "warning: ignoring --ref with --source (the checkout is used as it is)"
else
  [[ -n "$REF" ]] || { err "missing required --ref: a release tag from" \
    "https://github.com/Musqet/musqet-odoo/releases, a full 40-character commit SHA, or a branch (20.0, 19.0) for development code"; usage 1; }
  plain_ref "$REF" || die "--ref '$REF' is not a plain tag, branch or commit name"
  ref_lower="$(printf '%s' "$REF" | tr 'A-F' 'a-f')"   # not ${REF,,}: bash 3.2 lacks it
  # Only a full SHA is checked against the fetched commit (below), so refuse any other
  # length rather than let it look like a pin. An all-hex branch or tag name is refused
  # too; this repo names neither that way.
  [[ "$ref_lower" =~ ^[0-9a-f]{4,}$ && ${#ref_lower} -ne 40 ]] \
    && die "--ref $REF looks like a commit SHA but is not 40 characters; pass the full SHA"
fi

# --upgrade and any `-- <extra odoo args>` are only meaningful with --db; don't let
# them silently no-op.
[[ "$ACTION" == "-u" && -z "$DB" ]] && die "--upgrade requires --db NAME"
[[ ${#EXTRA_ARGS[@]} -eq 0 || -n "$DB" ]] || err "warning: ignoring extra args after '--' (only used with --db)"

# Obtain the source tree (local checkout or a fresh clone).
CLEANUP=""
COMMIT=""
trap '[[ -n "$CLEANUP" ]] && rm -rf "$CLEANUP"' EXIT

if [[ -n "$SOURCE_DIR" ]]; then
  SRC="$SOURCE_DIR"
  [[ -d "$SRC/$MODULE" ]] || die "--source '$SRC' has no $MODULE/ directory"
elif command -v git >/dev/null 2>&1; then
  TMP="$(mktemp -d)"; CLEANUP="$TMP"
  info "Cloning $REPO_URL @ $REF (git)"
  git clone --quiet "$REPO_URL" "$TMP/repo" || die "clone failed: $REPO_URL"
  # The trailing -- makes git read REF only as a revision: without it, a ref that
  # matches no revision but names a file (e.g. pos_musqet) checks out that path and
  # leaves the default branch in place.
  git -C "$TMP/repo" checkout --quiet "$REF" -- || die "ref not found: $REF"
  COMMIT="$(git -C "$TMP/repo" rev-parse HEAD)"
  SRC="$TMP/repo"
else
  # No git (e.g. the stock odoo image) — download a tarball with curl/wget instead.
  # Works for GitHub repos and any --ref (branch, tag, or commit SHA).
  # Validate the github.com prefix BEFORE stripping .git, else a non-GitHub repo
  # ending in .git (e.g. gitlab.com/foo/bar.git) would slip past the guard.
  slug="$(printf '%s' "$REPO_URL" | sed -E 's#^https?://github\.com/##')"
  [[ "$slug" != "$REPO_URL" ]] || die "git not found and --repo is not a github.com URL — install git or use --source DIR"
  slug="${slug%.git}"
  url="https://codeload.github.com/$slug/tar.gz/$REF"
  TMP="$(mktemp -d)"; CLEANUP="$TMP"
  info "Downloading $url (no git)"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$TMP/src.tgz" || die "download failed — bad --ref '$REF'?"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$TMP/src.tgz" "$url" || die "download failed — bad --ref '$REF'?"
  else
    die "need git, curl or wget to fetch the addon (or pass --source DIR)"
  fi
  command -v tar >/dev/null 2>&1 || die "tar is required to extract the download"
  mkdir -p "$TMP/repo"
  tar -xzf "$TMP/src.tgz" -C "$TMP/repo" --strip-components=1 --no-same-owner || die "failed to extract archive"
  # GitHub builds the archive with git archive, which records the commit in the tar's
  # global header ("comment=<sha>"). That header is the first entry: a 512-byte header
  # block, then its pax record padded to 512, so the first 1536 bytes always hold it.
  COMMIT="$( { gzip -dc "$TMP/src.tgz" 2>/dev/null || true; } | head -c 1536 | tr -d '\0' \
    | grep -ao 'comment=[0-9a-f]\{40\}' | head -n 1 | cut -d= -f2 )" || true
  SRC="$TMP/repo"
fi

if [[ -z "$SOURCE_DIR" ]]; then
  info "Using commit ${COMMIT:-(not recorded in the download)}"
  # A tag or branch can be moved; a full commit SHA can't. When --ref is one, insist
  # on getting exactly that commit, on either fetch path.
  if [[ "$ref_lower" =~ ^[0-9a-f]{40}$ && "$COMMIT" != "$ref_lower" ]]; then
    die "fetched commit ${COMMIT:-(unknown)} does not match --ref $REF"
  fi
fi

[[ -f "$SRC/$MODULE/__manifest__.py" ]] || die "$MODULE/__manifest__.py not found in source — wrong --repo/--ref?"

# Place the addon. Stage a full copy first, then swap — so a failed copy (disk full,
# permissions) can never leave an existing install deleted-but-not-replaced.
mkdir -p "$ADDONS_PATH"
DEST="$ADDONS_PATH/$MODULE"
STAGE="$DEST.musqet-tmp"
rm -rf "$STAGE"
cp -R "$SRC/$MODULE" "$STAGE"
[[ -e "$DEST" ]] && { info "Refreshing existing $DEST"; rm -rf "$DEST"; }
mv "$STAGE" "$DEST"
info "Placed $MODULE at $DEST"

# Optionally install/upgrade into a database.
if [[ -n "$DB" ]]; then
  command -v "${ODOO_BIN%% *}" >/dev/null 2>&1 || die "odoo binary not found: '$ODOO_BIN' (set --odoo-bin)"
  info "Running module $ACTION into database '$DB'"
  # shellcheck disable=SC2086
  $ODOO_BIN -d "$DB" $ACTION "$MODULE" --stop-after-init "${EXTRA_ARGS[@]}"
fi

# Optionally restart Odoo.
if [[ -n "$RESTART_CMD" ]]; then
  info "Restarting Odoo: $RESTART_CMD"
  eval "$RESTART_CMD"
fi

if [[ -z "$DB" ]]; then
  cat <<EOF

Files are in place; the module is not installed yet. To finish:
  1. Ensure '$ADDONS_PATH' is on your Odoo addons_path.
  2. Restart Odoo.
  3. Install it — UI: developer mode > Apps > Update Apps List > "POS Musqet" > Install;
     or CLI: $ODOO_BIN -d <your_db> -i $MODULE --stop-after-init
See pos_musqet/docs/INSTALL.md for configuration (API key, terminal serial, currency).
EOF
fi
info "Done."
