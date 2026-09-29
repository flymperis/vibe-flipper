#!/usr/bin/env bash
# Update Vibe Flipper to the latest code from GitHub.
#
# Changes only the application code/image. Never touches your data or settings:
#   - the database (volume vibe-flipper-data): only backed up, to /data/backups inside it
#   - .env (not in git)
#   - the Quadlet units in ~/.config/containers/systemd (your copies; differences are only reported)
# Schema changes are applied by the app on start-up and only ever add columns.
#
#   ~/vibe-flipper/deploy/update.sh            # Quadlet (systemd --user) or compose, auto-detected
#   KEEP_BACKUPS=10 ~/vibe-flipper/deploy/update.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/containers/systemd"
CONTAINER=vibe-flipper
KEEP_BACKUPS="${KEEP_BACKUPS:-5}"
ENGINE="$(command -v podman || command -v docker)"
cd "$REPO"

# 1. local edits in the clone would be overwritten or block the pull: stop early
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Uncommitted changes in $REPO (settings belong in .env, which git ignores):" >&2
    git status --short >&2
    exit 1
fi

# 2. backup of the database (online, consistent: SQLite backup API)
if [ "$("$ENGINE" inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = true ]; then
    "$ENGINE" exec -i "$CONTAINER" python - "$(date +%Y%m%d-%H%M%S)" "$KEEP_BACKUPS" <<'PY'
import os, pathlib, sqlite3, sys
stamp, keep = sys.argv[1], int(sys.argv[2])
db = pathlib.Path(os.environ.get("DATABASE_URL", "sqlite:////data/vibe_flipper.db").removeprefix("sqlite:///"))
if not db.exists():
    print("no database yet, nothing to back up")
    sys.exit(0)
folder = db.parent / "backups"
folder.mkdir(exist_ok=True)
target = folder / f"{db.stem}-{stamp}.db"
src, dst = sqlite3.connect(db), sqlite3.connect(target)
src.backup(dst)
dst.close(); src.close()
for old in sorted(folder.glob(f"{db.stem}-*.db"))[:-keep]:
    old.unlink()
print(f"backup: {target} (keeping the last {keep})")
PY
else
    echo "container $CONTAINER not running: no backup taken"
fi

# 3. new code
before="$(git rev-parse HEAD)"
git pull --ff-only
if [ "$before" = "$(git rev-parse HEAD)" ]; then
    echo "already up to date ($(git log --oneline -1))"
else
    git --no-pager log --oneline "$before..HEAD"
fi

# 4. report (never overwrite) Quadlet units that differ from the repo's templates
if [ -d "$UNITS" ]; then
    for f in deploy/quadlet/vibe-flipper.*; do
        installed="$UNITS/$(basename "$f")"
        if [ -f "$installed" ] && ! diff -q "$f" "$installed" >/dev/null; then
            if git diff --quiet "$before" HEAD -- "$f"; then
                echo "note: $installed has your own changes (kept)"
            else
                echo "note: the template $f changed in this update; your $installed was kept."
                echo "      See what changed: git -C $REPO diff $before HEAD -- $f"
            fi
        fi
    done
fi

# 5. rebuild and restart
if systemctl --user cat vibe-flipper.service >/dev/null 2>&1; then
    systemctl --user restart vibe-flipper-build.service
    systemctl --user restart vibe-flipper.service
else
    if command -v podman-compose >/dev/null; then podman-compose up -d --build
    else docker compose up -d --build; fi
fi

# 6. wait for the healthcheck
for _ in $(seq 60); do
    status="$("$ENGINE" inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || true)"
    [ "$status" = healthy ] && break
    sleep 3
done
echo "$CONTAINER: ${status:-unknown} ($(git log --oneline -1))"
[ "$status" = healthy ]
