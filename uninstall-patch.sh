#!/usr/bin/env bash
set -euo pipefail

systemctl --user stop vice-patch.service 2>/dev/null || true
systemctl --user disable vice-patch.service 2>/dev/null || true
rm -f "$HOME/.local/bin/vice-patch" "$HOME/.local/bin/vice-patch-app"
rm -f "$HOME/.config/systemd/user/vice-patch.service"
rm -f "$HOME/.local/share/applications/vice-patch.desktop"
rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/vice-patch.svg"
rm -rf "$HOME/.local/share/vice-patch" "$HOME/.config/vice-patch" "$HOME/.cache/vice-patch" /tmp/vice-patch
systemctl --user daemon-reload
echo "Vice Patch artifacts removed. Existing clips outside Vice-Patch data were not touched."
