#!/usr/bin/env bash
# Installs this copy into a versioned layout, so that updates can be rolled back.
set -euo pipefail

ROOT="${THEFRAMEVANCED_ROOT:-$HOME/TheFrameVanced}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(cat "$SRC/VERSION")"
TARGET="$ROOT/versions/v$VERSION"
UNITS="$HOME/.config/systemd/user"

mkdir -p "$TARGET" "$ROOT/data/library" "$UNITS"
tar -C "$SRC" --exclude=.git -cf - . | tar -C "$TARGET" -xf -

[ -d "$ROOT/.venv" ] || python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/pip" install -q -r "$TARGET/requirements.txt"

ln -sfn "$TARGET" "$ROOT/current"
[ -f "$ROOT/data/config.json" ] || cp "$TARGET/config.example.json" "$ROOT/data/config.json"

for unit in "$TARGET"/systemd/*; do
  sed "s#%h/TheFrameVanced#$ROOT#g" "$unit" > "$UNITS/$(basename "$unit")"
done
systemctl --user daemon-reload
systemctl --user enable --now theframevanced-admin.service theframevanced-update.timer
systemctl --user restart theframevanced-admin.service
systemctl --user try-restart theframevanced.timer

echo "Installed v$VERSION in $ROOT"
echo "Open the admin page at http://127.0.0.1:8765"
