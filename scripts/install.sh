#!/usr/bin/env bash
# Install nitpick as two macOS LaunchAgents for the current user: the hourly
# pipeline run and the always-on dashboard. Safe to re-run — existing jobs are
# replaced, and .env / config.yml are never overwritten.
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LABEL_PREFIX="com.$(id -un)"

die() { echo "error: $*" >&2; exit 1; }

for bin in uv claude gh; do
  command -v "$bin" >/dev/null || die "'$bin' not found on PATH — install it first"
done
UV_PATH="$(command -v uv)"
# A pyenv shim needs pyenv's own environment, which launchd does not provide;
# resolve it to the real binary.
if [[ "$UV_PATH" == */shims/* ]] && command -v pyenv >/dev/null; then
  UV_PATH="$(pyenv which uv)"
fi

# launchd starts with a bare PATH. Give the jobs the directories that hold
# claude, gh and uv, followed by the usual system ones.
JOB_PATH=""
for bin in claude gh; do
  dir="$(dirname "$(command -v "$bin")")"
  case ":$JOB_PATH:" in *":$dir:"*) ;; *) JOB_PATH="${JOB_PATH:+$JOB_PATH:}$dir" ;; esac
done
JOB_PATH="$JOB_PATH:$(dirname "$UV_PATH"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

claude auth status >/dev/null 2>&1 || echo "warning: claude does not look logged in — run 'claude' once and sign in"
gh auth status >/dev/null 2>&1 || echo "warning: gh is not authenticated — run 'gh auth login'"

cd "$INSTALL_DIR"
mkdir -p logs data
[[ -f .env ]] || { cp .env.example .env; echo "created .env from .env.example — fill it in"; }
[[ -f config.yml ]] || { cp config.example.yml config.yml; echo "created config.yml from config.example.yml — fill it in"; }

"$UV_PATH" sync --quiet

# Every repo config.yml points at must exist locally, or investigations run
# without the code they are meant to read.
"$UV_PATH" run python - <<'EOF'
import os, yaml
repos = (yaml.safe_load(open("config.yml")) or {}).get("repos") or {}
missing = {k: v for k, v in repos.items() if not os.path.isdir(os.path.expanduser(v))}
for name, path in missing.items():
    print(f"warning: repo '{name}' not found at {path} — clone it or fix the path in config.yml")
EOF

mkdir -p "$AGENTS_DIR"
for kind in nitpick nitpick-dashboard; do
  label="$LABEL_PREFIX.$kind"
  plist="$AGENTS_DIR/$label.plist"
  sed -e "s#com\.example\.$kind<#$label<#" \
      -e "s#__INSTALL_DIR__#$INSTALL_DIR#g" \
      -e "s#__UV_PATH__#$UV_PATH#g" \
      -e "s#__PATH__#$JOB_PATH#g" \
      "com.example.$kind.plist" > "$plist"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
  echo "loaded $label"
done

echo
echo "done. pipeline runs hourly on the hour; dashboard at http://localhost:${DASHBOARD_PORT:-8111}"
echo "trigger a run now with: launchctl kickstart gui/$(id -u)/$LABEL_PREFIX.nitpick"
