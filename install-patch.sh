#!/usr/bin/env bash
set -euo pipefail

PATCH_DATA="$HOME/.local/share/vice-patch"
PATCH_CONFIG="$HOME/.config/vice-patch"
PATCH_CACHE="$HOME/.cache/vice-patch"
USER_BIN="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
APP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
SOURCE_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"

missing=()
for command_name in python3 ffmpeg ffprobe gpu-screen-recorder systemctl; do
  command -v "$command_name" >/dev/null 2>&1 || missing+=("$command_name")
done
if ((${#missing[@]})); then
  echo "Vice Patch needs existing commands: ${missing[*]}" >&2
  echo "Install them with your distribution package manager, then rerun this script." >&2
  exit 1
fi

mkdir -p "$PATCH_DATA" "$PATCH_CONFIG" "$PATCH_CACHE" "$USER_BIN" "$UNIT_DIR" "$APP_DIR" "$ICON_DIR"
python3 -m venv "$PATCH_DATA/venv"
"$PATCH_DATA/venv/bin/python" -m pip install --upgrade --force-reinstall "$SOURCE_DIR"

if [[ ! -e "$PATCH_CONFIG/config.toml" ]]; then
  cat >"$PATCH_CONFIG/config.toml" <<'CONFIG'
[recording]
audio_tracks_mix_first = false
audio_track_mix_gains = {}

[output]
directory = "~/Videos/Vice-Patch"

[sharing]
port = 8875
public_port = 8876
cloudflare_tunnel = false

[updates]
check_on_start = false
CONFIG
fi

write_wrapper() {
  local target="$1" entry="$2"
  cat >"$target" <<EOF
#!/usr/bin/env bash
export VICE_INSTANCE=vice-patch
export VICE_APP_NAME="Vice Patch"
export VICE_CONFIG_DIR="\$HOME/.config/vice-patch"
export VICE_DATA_DIR="\$HOME/.local/share/vice-patch"
export VICE_CACHE_DIR="\$HOME/.cache/vice-patch"
export VICE_RUNTIME_DIR="/tmp/vice-patch"
export VICE_RUNTIME_NAME=vice-patch
export VICE_CLI_NAME=vice-patch
export VICE_APP_CLI_NAME=vice-patch-app
export VICE_SERVICE_NAME=vice-patch.service
exec "\$HOME/.local/share/vice-patch/venv/bin/$entry" "\$@"
EOF
  chmod 0755 "$target"
}
write_wrapper "$USER_BIN/vice-patch" vice
write_wrapper "$USER_BIN/vice-patch-app" vice-app
install -m 0644 "$SOURCE_DIR/assets/vice.svg" "$ICON_DIR/vice-patch.svg"

cat >"$UNIT_DIR/vice-patch.service" <<EOF
[Unit]
Description=Vice Patch isolated replay recorder
After=graphical-session.target

[Service]
Type=simple
ExecStart=$USER_BIN/vice-patch start --no-open-ui
Restart=on-failure
Environment=PATH=$USER_BIN:/usr/local/bin:/usr/bin:/bin
Environment=VICE_INSTANCE=vice-patch
Environment=VICE_APP_NAME=Vice\x20Patch
Environment=VICE_CONFIG_DIR=$PATCH_CONFIG
Environment=VICE_DATA_DIR=$PATCH_DATA
Environment=VICE_CACHE_DIR=$PATCH_CACHE
Environment=VICE_RUNTIME_DIR=/tmp/vice-patch
Environment=VICE_RUNTIME_NAME=vice-patch
Environment=VICE_CLI_NAME=vice-patch
Environment=VICE_APP_CLI_NAME=vice-patch-app
Environment=VICE_SERVICE_NAME=vice-patch.service
PassEnvironment=WAYLAND_DISPLAY DISPLAY XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS XDG_SESSION_TYPE XDG_CURRENT_DESKTOP

[Install]
WantedBy=default.target
EOF

cat >"$APP_DIR/vice-patch.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Vice Patch
Comment=Isolated experimental Vice multi-stream editor
Exec=$USER_BIN/vice-patch-app
Icon=vice-patch
Terminal=false
Categories=AudioVideo;Recorder;
StartupWMClass=VicePatch
EOF

systemctl --user daemon-reload
echo "Vice Patch installed but not started or enabled."
echo "When production is stopped manually: systemctl --user start vice-patch.service"
