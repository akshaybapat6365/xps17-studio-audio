#!/usr/bin/env bash
# deploy-studio-dsp.sh — Safety-First Atomic Installer for Dell XPS 17 9720 Studio Audio Stack
#
# Safety guarantees:
#   - Defaults to DRY-RUN mode; refuses to mutate any file unless --apply is explicitly passed.
#   - Supports TARGET_HOME (env var or --target-home) for isolated sandbox testing.
#   - Creates a complete timestamped pre-deploy backup snapshot with SHA256 manifest before touching anything.
#   - Installs every target file atomically (same-directory temp file + chmod + mv -f).
#   - Verifies no-mic-monitor.conf, 51-bluetooth-no-phone-audio.conf (and 60-soundwire-input-default-volume.conf)
#     SHA256 hashes are BYTE-FOR-BYTE IDENTICAL before and after deploy.
#   - NEVER touches PipeWire/WirePlumber system daemons unless --apply is given without --no-restart
#     AND TARGET_HOME matches real $HOME; only restarts user speaker-engine / controller services.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_WORKSPACE="$(cd -- "$SCRIPT_DIR/../.." && pwd)"

WORKSPACE="${WORKSPACE:-$DEFAULT_WORKSPACE}"
TARGET_HOME="${TARGET_HOME:-$HOME}"
APPLY=0
NO_RESTART=0
EXPLICIT_DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: deploy-studio-dsp.sh [OPTIONS]

Safety-first atomic installer for the Dell XPS 17 9720 Studio Audio stack.
By default (without --apply), runs in --dry-run mode and makes ZERO changes.

Options:
  --dry-run             Show exact deployment plan without modifying any files (default)
  --apply               Execute atomic deployment (requires all staged artifacts present)
  --no-restart          Do not run systemctl --user restart even with --apply; print commands instead
  --target-home DIR     Install into DIR instead of $HOME (also reads TARGET_HOME env var)
  --workspace DIR       Workspace root (default: auto-detected xps17-studio-audio root)
  -h, --help            Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      EXPLICIT_DRY_RUN=1
      APPLY=0
      shift
      ;;
    --apply)
      APPLY=1
      shift
      ;;
    --no-restart)
      NO_RESTART=1
      shift
      ;;
    --target-home)
      [[ $# -ge 2 ]] || { echo "ERROR: --target-home requires a directory argument" >&2; exit 2; }
      TARGET_HOME="$2"
      shift 2
      ;;
    --workspace)
      [[ $# -ge 2 ]] || { echo "ERROR: --workspace requires a directory argument" >&2; exit 2; }
      WORKSPACE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$EXPLICIT_DRY_RUN" -eq 1 && "$APPLY" -eq 1 ]]; then
  echo "ERROR: Cannot combine --dry-run and --apply" >&2
  exit 2
fi

WORKSPACE="$(realpath -m "$WORKSPACE")"
TARGET_HOME="$(realpath -m "$TARGET_HOME")"
REAL_HOME="$(realpath -m "$HOME")"

# Safety guard: refuse to mutate real $HOME unless --apply was explicitly passed
if [[ "$APPLY" -eq 0 ]]; then
  echo "[deploy-studio-dsp] MODE: DRY-RUN (no files will be modified; pass --apply to execute)"
else
  echo "[deploy-studio-dsp] MODE: APPLY (atomic install)"
fi
echo "  WORKSPACE   = $WORKSPACE"
echo "  TARGET_HOME = $TARGET_HOME"
echo "  NO_RESTART  = $NO_RESTART"

# ---------------------------------------------------------------------------
# Source & Destination Inventory (Standardized Runtime Paths)
# ---------------------------------------------------------------------------
SRC_SO="$WORKSPACE/dsp-engine/lib/libxps17_studio_dsp.so"
SRC_CLI="$WORKSPACE/dsp-engine/bin/xps17-studio-cli"
SRC_PARAMS="$WORKSPACE/acoustics-research/dsp_params.json"
SRC_K_DEEP="$WORKSPACE/acoustics-research/kernels/xps17-studio-deep.wav"
SRC_K_SPATIAL="$WORKSPACE/acoustics-research/kernels/xps17-studio-spatial.wav"
SRC_K_REF="$WORKSPACE/acoustics-research/kernels/xps17-studio-reference.wav"
SRC_K_VOICE="$WORKSPACE/acoustics-research/kernels/xps17-studio-voice.wav"
SRC_PW_CONF="$WORKSPACE/verify-integrate/staged/pipewire/92-xps17-studio.conf"
SRC_MODEL="$WORKSPACE/verify-integrate/staged/controller/model.py"
SRC_RUNTIME="$WORKSPACE/verify-integrate/staged/controller/runtime.py"
SRC_PROFILE="$WORKSPACE/verify-integrate/staged/bin/speaker-profile"
SRC_MENU="$WORKSPACE/verify-integrate/staged/menu/omarchy-menu.jsonc"
SRC_SYSTEMD_DROPIN="$WORKSPACE/verify-integrate/staged/systemd/xps17-studio-acoustics-dir.conf"

DST_SO="$TARGET_HOME/.local/share/xps17-studio-audio/lib/libxps17_studio_dsp.so"
DST_CLI="$TARGET_HOME/.local/bin/xps17-studio-cli"
DST_PARAMS="$TARGET_HOME/.local/share/xps17-studio-audio/dsp_params.json"
DST_K_DEEP="$TARGET_HOME/.local/share/xps17-studio-audio/kernels/xps17-studio-deep.wav"
DST_K_SPATIAL="$TARGET_HOME/.local/share/xps17-studio-audio/kernels/xps17-studio-spatial.wav"
DST_K_REF="$TARGET_HOME/.local/share/xps17-studio-audio/kernels/xps17-studio-reference.wav"
DST_K_VOICE="$TARGET_HOME/.local/share/xps17-studio-audio/kernels/xps17-studio-voice.wav"
DST_PW_CONF="$TARGET_HOME/.config/pipewire/omarchy-speaker-engine.conf.d/92-xps17-studio.conf"
DST_MODEL="$TARGET_HOME/.local/lib/omarchy_audio/model.py"
DST_RUNTIME="$TARGET_HOME/.local/lib/omarchy_audio/runtime.py"
DST_PROFILE="$TARGET_HOME/.local/bin/speaker-profile"
DST_MENU="$TARGET_HOME/.config/omarchy/extensions/omarchy-menu.jsonc"
DST_SYSTEMD_DROPIN="$TARGET_HOME/.config/systemd/user/omarchy-speaker-engine.service.d/xps17-studio-acoustics-dir.conf"

# Protected read-only safety guards (MUST remain byte-for-byte identical!)
GUARD_NO_MIC="$TARGET_HOME/.config/systemd/user/easyeffects-speaker-dsp.service.d/no-mic-monitor.conf"
GUARD_BT="$TARGET_HOME/.config/wireplumber/wireplumber.conf.d/51-bluetooth-no-phone-audio.conf"
GUARD_MIC_VOL="$TARGET_HOME/.config/wireplumber/wireplumber.conf.d/60-soundwire-input-default-volume.conf"

# Mapping table: key|src|dst|mode|rewrite_pw
DEPLOY_ITEMS=(
  "libxps17_studio_dsp.so|$SRC_SO|$DST_SO|0755|0"
  "xps17-studio-cli|$SRC_CLI|$DST_CLI|0755|0"
  "dsp_params.json|$SRC_PARAMS|$DST_PARAMS|0644|0"
  "xps17-studio-deep.wav|$SRC_K_DEEP|$DST_K_DEEP|0644|0"
  "xps17-studio-spatial.wav|$SRC_K_SPATIAL|$DST_K_SPATIAL|0644|0"
  "xps17-studio-reference.wav|$SRC_K_REF|$DST_K_REF|0644|0"
  "xps17-studio-voice.wav|$SRC_K_VOICE|$DST_K_VOICE|0644|0"
  "92-xps17-studio.conf|$SRC_PW_CONF|$DST_PW_CONF|0644|1"
  "xps17-studio-acoustics-dir.conf|$SRC_SYSTEMD_DROPIN|$DST_SYSTEMD_DROPIN|0644|0"
  "model.py|$SRC_MODEL|$DST_MODEL|0644|0"
  "runtime.py|$SRC_RUNTIME|$DST_RUNTIME|0644|0"
  "speaker-profile|$SRC_PROFILE|$DST_PROFILE|0755|0"
  "omarchy-menu.jsonc|$SRC_MENU|$DST_MENU|0644|0"
)

# ---------------------------------------------------------------------------
# Pre-flight verification of required source artifacts & protected guards
# ---------------------------------------------------------------------------
MISSING_SRCS=()
for entry in "${DEPLOY_ITEMS[@]}"; do
  IFS='|' read -r key src dst mode rewrite_pw <<< "$entry"
  if [[ ! -f "$src" ]]; then
    MISSING_SRCS+=("$src")
  fi
done

if [[ "${#MISSING_SRCS[@]}" -gt 0 ]]; then
  echo "ERROR: Missing required staged/built artifact(s):" >&2
  for m in "${MISSING_SRCS[@]}"; do
    echo "  - $m" >&2
  done
  if [[ "$APPLY" -eq 1 ]]; then
    echo "Refusing --apply until all artifacts are present and verified." >&2
    exit 1
  else
    echo "[dry-run] Dry-run aborting because staged artifacts are incomplete." >&2
    exit 1
  fi
fi

# Verify mandatory live safety guards exist in TARGET_HOME and capture pre-deploy SHA256
sha256_of() {
  sha256sum "$1" | awk '{print $1}'
}

if [[ ! -f "$GUARD_NO_MIC" ]]; then
  echo "ERROR: Mandatory safety guard missing: $GUARD_NO_MIC" >&2
  exit 1
fi
if [[ ! -f "$GUARD_BT" ]]; then
  echo "ERROR: Mandatory safety guard missing: $GUARD_BT" >&2
  exit 1
fi

PRE_HASH_NO_MIC="$(sha256_of "$GUARD_NO_MIC")"
PRE_HASH_BT="$(sha256_of "$GUARD_BT")"
PRE_HASH_MIC_VOL="ABSENT"
if [[ -f "$GUARD_MIC_VOL" ]]; then
  PRE_HASH_MIC_VOL="$(sha256_of "$GUARD_MIC_VOL")"
fi

echo "[deploy-studio-dsp] Pre-deploy safety guard SHA256:"
echo "  no-mic-monitor.conf                : $PRE_HASH_NO_MIC"
echo "  51-bluetooth-no-phone-audio.conf   : $PRE_HASH_BT"
echo "  60-soundwire-input-default-vol.conf: $PRE_HASH_MIC_VOL"

# Verify destination paths NEVER collide with protected guards
for entry in "${DEPLOY_ITEMS[@]}"; do
  IFS='|' read -r key src dst mode rewrite_pw <<< "$entry"
  if [[ "$dst" == "$GUARD_NO_MIC" || "$dst" == "$GUARD_BT" || "$dst" == "$GUARD_MIC_VOL" ]]; then
    echo "FATAL: Deploy destination collides with protected safety guard: $dst" >&2
    exit 1
  fi
done

# ---------------------------------------------------------------------------
# Dry-run plan preview
# ---------------------------------------------------------------------------
BACKUP_ROOT="$TARGET_HOME/.local/state/xps17-studio-audio/backups"
BACKUP_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
BACKUP_DIR="$BACKUP_ROOT/$BACKUP_ID"

if [[ "$APPLY" -eq 0 ]]; then
  echo ""
  echo "[dry-run] Would create pre-deploy backup snapshot at:"
  echo "  $BACKUP_DIR"
  echo ""
  echo "[dry-run] Planned atomic file installs:"
  for entry in "${DEPLOY_ITEMS[@]}"; do
    IFS='|' read -r key src dst mode rewrite_pw <<< "$entry"
    state="ABSENT"
    [[ -f "$dst" ]] && state="EXISTED"
    extra=""
    [[ "$rewrite_pw" -eq 1 ]] && extra=" (rewriting asset paths to $TARGET_HOME/.local/share/xps17-studio-audio)"
    echo "  [$state] $src -> $dst (mode $mode)$extra"
  done
  echo ""
  echo "[dry-run] Post-deploy restart commands (not run in dry-run):"
  echo "  systemctl --user try-restart omarchy-speaker-engine.service omarchy-speaker-dsp-controller.service"
  echo "[dry-run] Complete. Zero files modified."
  exit 0
fi

# ---------------------------------------------------------------------------
# Atomic Backup Snapshot Creation
# ---------------------------------------------------------------------------
TMP_FILES=()
cleanup_temps() {
  for tf in "${TMP_FILES[@]:-}"; do
    [[ -n "$tf" && -e "$tf" ]] && rm -f "$tf" || true
  done
}
trap cleanup_temps EXIT

mkdir -p "$BACKUP_DIR/files"
MANIFEST="$BACKUP_DIR/manifest.tsv"
GUARD_FILE="$BACKUP_DIR/guard-hashes.tsv"

{
  printf "guard\tpath\tsha256\n"
  printf "no-mic-monitor.conf\t%s\t%s\n" "$GUARD_NO_MIC" "$PRE_HASH_NO_MIC"
  printf "51-bluetooth-no-phone-audio.conf\t%s\t%s\n" "$GUARD_BT" "$PRE_HASH_BT"
  printf "60-soundwire-input-default-volume.conf\t%s\t%s\n" "$GUARD_MIC_VOL" "$PRE_HASH_MIC_VOL"
} > "$GUARD_FILE"

printf "key\tstatus\tdst_path\tbackup_rel\tpre_sha256\tpost_sha256\n" > "$MANIFEST"

# Record pre-existing state and back up existing target files
declare -A PRE_STATUS
declare -A PRE_SHA
for entry in "${DEPLOY_ITEMS[@]}"; do
  IFS='|' read -r key src dst mode rewrite_pw <<< "$entry"
  if [[ -f "$dst" ]]; then
    PRE_STATUS["$key"]="EXISTED"
    PRE_SHA["$key"]="$(sha256_of "$dst")"
    cp -a "$dst" "$BACKUP_DIR/files/$key"
  else
    PRE_STATUS["$key"]="ABSENT"
    PRE_SHA["$key"]="-"
  fi
done

# ---------------------------------------------------------------------------
# Atomic Install Helper (same-dir temp file + chmod + mv -f)
# ---------------------------------------------------------------------------
install_atomic() {
  local src="$1"
  local dst="$2"
  local mode="$3"
  local rewrite_pw="$4"
  local dst_dir
  dst_dir="$(dirname "$dst")"
  mkdir -p "$dst_dir"
  local tmp="${dst}.tmp.$$"
  TMP_FILES+=("$tmp")

  if [[ "$rewrite_pw" -eq 1 ]]; then
    python3 - "$src" "$tmp" "$TARGET_HOME" "$WORKSPACE" <<'PYEOF'
import re, sys
from pathlib import Path

src_p, tmp_p, target_home, workspace = sys.argv[1:5]
text = Path(src_p).read_text(encoding="utf-8")

lib_dst = f"{target_home}/.local/share/xps17-studio-audio/lib/libxps17_studio_dsp.so"
kern_dst_prefix = f"{target_home}/.local/share/xps17-studio-audio/kernels/"

# 1. Replace placeholder forms (__HOME__, $HOME, ${HOME})
text = text.replace("__HOME__", target_home)
text = text.replace("${HOME}", target_home)
text = text.replace("$HOME", target_home)

# 2. Replace workspace build paths with standardized runtime asset paths
text = text.replace(f"{workspace}/dsp-engine/lib/libxps17_studio_dsp.so", lib_dst)
text = text.replace(f"{workspace}/acoustics-research/kernels/", kern_dst_prefix)

# 3. Replace any hardcoded /home/<user>/.local/share/xps17-studio-audio/... with $TARGET_HOME/...
text = re.sub(
    r"/home/[^/\"\s]+/\.local/share/xps17-studio-audio/lib/libxps17_studio_dsp\.so",
    lib_dst,
    text,
)
text = re.sub(
    r"/home/[^/\"\s]+/\.local/share/xps17-studio-audio/kernels/",
    kern_dst_prefix,
    text,
)
# Also rewrite any hardcoded /home/<user>/project/xps17-studio-audio/{dsp-engine/lib,acoustics-research/kernels}
text = re.sub(
    r"/home/[^/\"\s]+/project/xps17-studio-audio/dsp-engine/lib/libxps17_studio_dsp\.so",
    lib_dst,
    text,
)
text = re.sub(
    r"/home/[^/\"\s]+/project/xps17-studio-audio/acoustics-research/kernels/",
    kern_dst_prefix,
    text,
)

Path(tmp_p).write_text(text, encoding="utf-8")
PYEOF
  else
    cp -p "$src" "$tmp"
  fi

  chmod "$mode" "$tmp"
  mv -f "$tmp" "$dst"
}

echo "[deploy-studio-dsp] Installing artifacts atomically..."
for entry in "${DEPLOY_ITEMS[@]}"; do
  IFS='|' read -r key src dst mode rewrite_pw <<< "$entry"
  install_atomic "$src" "$dst" "$mode" "$rewrite_pw"
  post_sha="$(sha256_of "$dst")"
  backup_rel="-"
  [[ "${PRE_STATUS[$key]}" == "EXISTED" ]] && backup_rel="files/$key"
  printf "%s\t%s\t%s\t%s\t%s\t%s\n" \
    "$key" "${PRE_STATUS[$key]}" "$dst" "$backup_rel" "${PRE_SHA[$key]}" "$post_sha" >> "$MANIFEST"
  echo "  [OK] $dst (sha256=${post_sha:0:12}...)"
done

# Atomically update 'latest' backup symlink
LATEST_TMP="$BACKUP_ROOT/.latest.tmp.$$"
ln -sfn "$BACKUP_ID" "$LATEST_TMP"
mv -Tf "$LATEST_TMP" "$BACKUP_ROOT/latest"

# ---------------------------------------------------------------------------
# Post-Deploy Safety Guard Integrity Assertion
# ---------------------------------------------------------------------------
POST_HASH_NO_MIC="$(sha256_of "$GUARD_NO_MIC")"
POST_HASH_BT="$(sha256_of "$GUARD_BT")"
POST_HASH_MIC_VOL="ABSENT"
if [[ -f "$GUARD_MIC_VOL" ]]; then
  POST_HASH_MIC_VOL="$(sha256_of "$GUARD_MIC_VOL")"
fi

if [[ "$PRE_HASH_NO_MIC" != "$POST_HASH_NO_MIC" ]]; then
  echo "FATAL: no-mic-monitor.conf SHA256 CHANGED during deploy!" >&2
  exit 1
fi
if [[ "$PRE_HASH_BT" != "$POST_HASH_BT" ]]; then
  echo "FATAL: 51-bluetooth-no-phone-audio.conf SHA256 CHANGED during deploy!" >&2
  exit 1
fi
if [[ "$PRE_HASH_MIC_VOL" != "$POST_HASH_MIC_VOL" ]]; then
  echo "FATAL: 60-soundwire-input-default-volume.conf SHA256 CHANGED during deploy!" >&2
  exit 1
fi

echo "[deploy-studio-dsp] Guard integrity VERIFIED (SHA256 identical before & after deploy):"
echo "  no-mic-monitor.conf                : $POST_HASH_NO_MIC == $PRE_HASH_NO_MIC [OK]"
echo "  51-bluetooth-no-phone-audio.conf   : $POST_HASH_BT == $PRE_HASH_BT [OK]"
echo "  60-soundwire-input-default-vol.conf: $POST_HASH_MIC_VOL == $PRE_HASH_MIC_VOL [OK]"
echo "[deploy-studio-dsp] Backup snapshot saved: $BACKUP_DIR"

# ---------------------------------------------------------------------------
# Service Restart Handling (NEVER run against live session if TARGET_HOME != $HOME or --no-restart)
# ---------------------------------------------------------------------------
if [[ "$NO_RESTART" -eq 1 || "$TARGET_HOME" != "$REAL_HOME" ]]; then
  echo "[deploy-studio-dsp] Skipping systemd service restart (--no-restart=$NO_RESTART, sandbox=$([[ "$TARGET_HOME" != "$REAL_HOME" ]] && echo yes || echo no))."
  echo "  To activate on live host, run:"
  echo "    systemctl --user try-restart omarchy-speaker-engine.service omarchy-speaker-dsp-controller.service"
else
  echo "[deploy-studio-dsp] Reloading systemd user units & restarting speaker DSP services (PipeWire & WirePlumber untouched)..."
  systemctl --user daemon-reload
  systemctl --user try-restart omarchy-speaker-engine.service omarchy-speaker-dsp-controller.service
  echo "[deploy-studio-dsp] Services restarted cleanly."
fi

echo "[deploy-studio-dsp] DEPLOY COMPLETE (backup_id=$BACKUP_ID)."
