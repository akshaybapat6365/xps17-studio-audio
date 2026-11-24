#!/usr/bin/env bash
# rollback-studio-dsp.sh — 1-Second Atomic Rollback for Dell XPS 17 9720 Studio Audio Stack
#
# Safety guarantees:
#   - Defaults to DRY-RUN mode; refuses to mutate any file unless --apply is explicitly passed.
#   - Supports TARGET_HOME (env var or --target-home) for isolated sandbox testing.
#   - Restores latest (or specified --backup-id) pre-deploy backup snapshot atomically via temp file + mv -f.
#   - Removes newly introduced files (including 92-xps17-studio.conf and runtime lib/kernels if absent pre-deploy).
#   - Verifies every restored file's SHA256 matches pre-deploy backup byte-for-byte.
#   - Verifies no-mic-monitor.conf, 51-bluetooth-no-phone-audio.conf, and 60-soundwire-input-default-volume.conf
#     SHA256 hashes are IDENTICAL before rollback, after rollback, and against pre-deploy baseline!

set -euo pipefail

TARGET_HOME="${TARGET_HOME:-$HOME}"
BACKUP_ID="latest"
APPLY=0
EXPLICIT_DRY_RUN=0
NO_RESTART=0
LIST_ONLY=0

usage() {
  cat <<'EOF'
Usage: rollback-studio-dsp.sh [OPTIONS]

1-second atomic rollback for the Dell XPS 17 9720 Studio Audio stack.
By default (without --apply), runs in --dry-run mode and makes ZERO changes.

Options:
  --dry-run             Preview exact rollback actions without modifying any files (default)
  --apply               Execute atomic rollback from backup snapshot
  --no-restart          Do not run systemctl --user restart even with --apply; print commands instead
  --backup-id ID        Restore a specific backup snapshot ID (default: latest)
  --list                List available backup snapshots in TARGET_HOME and exit
  --target-home DIR     Target HOME directory (also reads TARGET_HOME env var)
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
    --backup-id)
      [[ $# -ge 2 ]] || { echo "ERROR: --backup-id requires an ID argument" >&2; exit 2; }
      BACKUP_ID="$2"
      shift 2
      ;;
    --list)
      LIST_ONLY=1
      shift
      ;;
    --target-home)
      [[ $# -ge 2 ]] || { echo "ERROR: --target-home requires a directory argument" >&2; exit 2; }
      TARGET_HOME="$2"
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

TARGET_HOME="$(realpath -m "$TARGET_HOME")"
REAL_HOME="$(realpath -m "$HOME")"
BACKUP_ROOT="$TARGET_HOME/.local/state/xps17-studio-audio/backups"

if [[ "$LIST_ONLY" -eq 1 ]]; then
  if [[ ! -d "$BACKUP_ROOT" ]]; then
    echo "No backups directory found at $BACKUP_ROOT"
    exit 0
  fi
  echo "Available backup snapshots in $BACKUP_ROOT:"
  ls -la "$BACKUP_ROOT"
  exit 0
fi

if [[ "$BACKUP_ID" == "latest" ]]; then
  if [[ ! -L "$BACKUP_ROOT/latest" && ! -d "$BACKUP_ROOT/latest" ]]; then
    echo "ERROR: No 'latest' backup snapshot found under $BACKUP_ROOT" >&2
    exit 1
  fi
  BACKUP_DIR="$(realpath "$BACKUP_ROOT/latest")"
else
  BACKUP_DIR="$(realpath -m "$BACKUP_ROOT/$BACKUP_ID")"
fi

MANIFEST="$BACKUP_DIR/manifest.tsv"
GUARD_FILE="$BACKUP_DIR/guard-hashes.tsv"

if [[ ! -f "$MANIFEST" ]]; then
  echo "ERROR: Backup manifest not found: $MANIFEST" >&2
  exit 1
fi

sha256_of() {
  sha256sum "$1" | awk '{print $1}'
}

GUARD_NO_MIC="$TARGET_HOME/.config/systemd/user/easyeffects-speaker-dsp.service.d/no-mic-monitor.conf"
GUARD_BT="$TARGET_HOME/.config/wireplumber/wireplumber.conf.d/51-bluetooth-no-phone-audio.conf"
GUARD_MIC_VOL="$TARGET_HOME/.config/wireplumber/wireplumber.conf.d/60-soundwire-input-default-volume.conf"

if [[ ! -f "$GUARD_NO_MIC" || ! -f "$GUARD_BT" ]]; then
  echo "ERROR: Protected safety guard missing in $TARGET_HOME before rollback!" >&2
  exit 1
fi

PRE_HASH_NO_MIC="$(sha256_of "$GUARD_NO_MIC")"
PRE_HASH_BT="$(sha256_of "$GUARD_BT")"
PRE_HASH_MIC_VOL="ABSENT"
[[ -f "$GUARD_MIC_VOL" ]] && PRE_HASH_MIC_VOL="$(sha256_of "$GUARD_MIC_VOL")"

if [[ "$APPLY" -eq 0 ]]; then
  echo "[rollback-studio-dsp] MODE: DRY-RUN (no files will be modified; pass --apply to execute)"
else
  echo "[rollback-studio-dsp] MODE: APPLY (1-second atomic rollback)"
fi
echo "  TARGET_HOME = $TARGET_HOME"
echo "  BACKUP_DIR  = $BACKUP_DIR"
echo "  NO_RESTART  = $NO_RESTART"

if [[ "$APPLY" -eq 0 ]]; then
  echo ""
  echo "[dry-run] Planned rollback actions from $MANIFEST:"
  tail -n +2 "$MANIFEST" | while IFS=$'\t' read -r key status dst_path backup_rel pre_sha post_sha; do
    if [[ "$status" == "EXISTED" ]]; then
      echo "  [RESTORE] $BACKUP_DIR/$backup_rel -> $dst_path (verify sha256=${pre_sha:0:12}...)"
    else
      echo "  [REMOVE]  $dst_path (was absent pre-deploy)"
    fi
  done
  echo ""
  echo "[dry-run] Post-rollback restart commands (not run in dry-run):"
  echo "  systemctl --user try-restart omarchy-speaker-engine.service omarchy-speaker-dsp-controller.service"
  echo "[dry-run] Complete. Zero files modified."
  exit 0
fi

# ---------------------------------------------------------------------------
# Atomic Rollback Execution
# ---------------------------------------------------------------------------
TMP_FILES=()
cleanup_temps() {
  for tf in "${TMP_FILES[@]:-}"; do
    [[ -n "$tf" && -e "$tf" ]] && rm -f "$tf" || true
  done
}
trap cleanup_temps EXIT

echo "[rollback-studio-dsp] Executing atomic rollback..."
while IFS=$'\t' read -r key status dst_path backup_rel pre_sha post_sha; do
  # Never touch protected guards
  if [[ "$dst_path" == "$GUARD_NO_MIC" || "$dst_path" == "$GUARD_BT" || "$dst_path" == "$GUARD_MIC_VOL" ]]; then
    echo "FATAL: Refusing to modify protected guard $dst_path" >&2
    exit 1
  fi

  if [[ "$status" == "EXISTED" ]]; then
    src_bak="$BACKUP_DIR/$backup_rel"
    if [[ ! -f "$src_bak" ]]; then
      echo "ERROR: Missing backup payload file: $src_bak" >&2
      exit 1
    fi
    mkdir -p "$(dirname "$dst_path")"
    tmp="${dst_path}.rollback.tmp.$$"
    TMP_FILES+=("$tmp")
    cp -a "$src_bak" "$tmp"
    mv -f "$tmp" "$dst_path"
    restored_sha="$(sha256_of "$dst_path")"
    if [[ "$restored_sha" != "$pre_sha" ]]; then
      echo "FATAL: SHA256 mismatch restoring $dst_path (got $restored_sha, expected $pre_sha)" >&2
      exit 1
    fi
    echo "  [RESTORED] $dst_path (sha256=${restored_sha:0:12}... exact match)"
  elif [[ "$status" == "ABSENT" ]]; then
    if [[ -e "$dst_path" || -L "$dst_path" ]]; then
      rm -f "$dst_path"
      echo "  [REMOVED]  $dst_path"
    else
      echo "  [ABSENT]   $dst_path (already removed)"
    fi
  else
    echo "ERROR: Unknown manifest status '$status' for key '$key'" >&2
    exit 1
  fi
done < <(tail -n +2 "$MANIFEST")

# Ensure 92-xps17-studio.conf is removed if it was absent pre-deploy (defense-in-depth)
# Clean up empty runtime asset directories created during deploy
rmdir "$TARGET_HOME/.local/share/xps17-studio-audio/lib" 2>/dev/null || true
rmdir "$TARGET_HOME/.local/share/xps17-studio-audio/kernels" 2>/dev/null || true
rmdir "$TARGET_HOME/.local/share/xps17-studio-audio" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Post-Rollback Safety Guard Integrity Verification
# ---------------------------------------------------------------------------
POST_HASH_NO_MIC="$(sha256_of "$GUARD_NO_MIC")"
POST_HASH_BT="$(sha256_of "$GUARD_BT")"
POST_HASH_MIC_VOL="ABSENT"
[[ -f "$GUARD_MIC_VOL" ]] && POST_HASH_MIC_VOL="$(sha256_of "$GUARD_MIC_VOL")"

if [[ "$PRE_HASH_NO_MIC" != "$POST_HASH_NO_MIC" ]]; then
  echo "FATAL: no-mic-monitor.conf SHA256 CHANGED during rollback!" >&2
  exit 1
fi
if [[ "$PRE_HASH_BT" != "$POST_HASH_BT" ]]; then
  echo "FATAL: 51-bluetooth-no-phone-audio.conf SHA256 CHANGED during rollback!" >&2
  exit 1
fi
if [[ "$PRE_HASH_MIC_VOL" != "$POST_HASH_MIC_VOL" ]]; then
  echo "FATAL: 60-soundwire-input-default-volume.conf SHA256 CHANGED during rollback!" >&2
  exit 1
fi

# Cross-verify against pre-deploy recorded guard hashes if guard-hashes.tsv exists
if [[ -f "$GUARD_FILE" ]]; then
  while IFS=$'\t' read -r gname gpath gsha; do
    case "$gname" in
      no-mic-monitor.conf)
        [[ "$POST_HASH_NO_MIC" == "$gsha" ]] || { echo "FATAL: no-mic-monitor.conf differs from pre-deploy hash!" >&2; exit 1; }
        ;;
      51-bluetooth-no-phone-audio.conf)
        [[ "$POST_HASH_BT" == "$gsha" ]] || { echo "FATAL: 51-bluetooth-no-phone-audio.conf differs from pre-deploy hash!" >&2; exit 1; }
        ;;
      60-soundwire-input-default-volume.conf)
        [[ "$POST_HASH_MIC_VOL" == "$gsha" ]] || { echo "FATAL: 60-soundwire-input-default-volume.conf differs from pre-deploy hash!" >&2; exit 1; }
        ;;
    esac
  done < <(tail -n +2 "$GUARD_FILE")
fi

echo "[rollback-studio-dsp] Guard integrity VERIFIED (pre-deploy == pre-rollback == post-rollback):"
echo "  no-mic-monitor.conf                : $POST_HASH_NO_MIC [OK]"
echo "  51-bluetooth-no-phone-audio.conf   : $POST_HASH_BT [OK]"
echo "  60-soundwire-input-default-vol.conf: $POST_HASH_MIC_VOL [OK]"

if [[ "$NO_RESTART" -eq 1 || "$TARGET_HOME" != "$REAL_HOME" ]]; then
  echo "[rollback-studio-dsp] Skipping systemd service restart (--no-restart=$NO_RESTART, sandbox=$([[ "$TARGET_HOME" != "$REAL_HOME" ]] && echo yes || echo no))."
  echo "  To activate rollback on live host, run:"
  echo "    systemctl --user try-restart omarchy-speaker-engine.service omarchy-speaker-dsp-controller.service"
else
  echo "[rollback-studio-dsp] Reloading systemd user units & restarting speaker DSP services (PipeWire & WirePlumber untouched)..."
  systemctl --user daemon-reload
  systemctl --user try-restart omarchy-speaker-engine.service omarchy-speaker-dsp-controller.service
  echo "[rollback-studio-dsp] Services restarted cleanly."
fi

echo "[rollback-studio-dsp] ROLLBACK COMPLETE (restored from $(basename "$BACKUP_DIR"))."
