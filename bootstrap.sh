#!/usr/bin/env bash
# bootstrap.sh — One-line installer for Studio Audio Stack (Dell XPS 17 9720 + Any Omarchy Machine)
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/akshaybapat6365/xps17-studio-audio/main/bootstrap.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/akshaybapat6365/xps17-studio-audio/main/bootstrap.sh | bash -s -- --portable
#   curl -fsSL https://raw.githubusercontent.com/akshaybapat6365/xps17-studio-audio/main/bootstrap.sh | sudo bash -s -- --with-kernel
set -euo pipefail

GH_USER="${STUDIO_AUDIO_GH_USER:-akshaybapat6365}"
REPO_URL="https://github.com/${GH_USER}/xps17-studio-audio.git"

# Resolve invoking non-root user home safely even under sudo
if [[ "${EUID:-$(id -u)}" -eq 0 && -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
  USER_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
else
  USER_HOME="${HOME}"
fi
TARGET_DIR="${STUDIO_AUDIO_DIR:-${USER_HOME}/.local/share/xps17-studio-audio-repo}"

for cmd in git bash; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "error: required tool '$cmd' not found in PATH." >&2
    exit 1
  }
done

mkdir -p "$(dirname "$TARGET_DIR")"
if [[ -d "$TARGET_DIR/.git" ]]; then
  echo "[studio-audio] Updating existing checkout in $TARGET_DIR..."
  git -C "$TARGET_DIR" pull --ff-only
else
  echo "[studio-audio] Cloning $REPO_URL into $TARGET_DIR..."
  git clone --depth 1 "$REPO_URL" "$TARGET_DIR"
fi

if [[ "${EUID:-$(id -u)}" -eq 0 && -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
  chown -R "$SUDO_USER:$(id -gn "$SUDO_USER")" "$TARGET_DIR"
fi

if [[ $# -gt 0 ]]; then
  exec bash "$TARGET_DIR/install.sh" "$@"
fi

# Auto-detect hardware: Dell XPS 17 9720 (0AFF) gets unit-calibrated XPS 17 FIRs;
# any other Omarchy / Arch PipeWire machine gets Universal Portable Studio DSP mode!
SKU="$(tr -d '[:space:]' < /sys/class/dmi/id/product_sku 2>/dev/null || true)"
if [[ "$SKU" == "0AFF" ]]; then
  echo "[studio-audio] Detected Dell XPS 17 9720 (DMI 0AFF) -> installing calibrated XPS 17 Studio DSP."
  exec bash "$TARGET_DIR/install.sh" --dsp-only
else
  echo "[studio-audio] Detected generic Omarchy/Linux host (DMI ${SKU:-unknown}) -> installing Universal Portable Studio DSP."
  exec bash "$TARGET_DIR/install.sh" --portable
fi
