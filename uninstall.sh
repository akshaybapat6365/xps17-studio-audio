#!/usr/bin/env bash
# Dell XPS 17 9720 Studio Audio Engineering Stack — Clean Atomic Uninstaller
#
# Safety guarantees enforced unconditionally:
#   1. LIVE_USER_HOME guard: resolves the true non-root user home (never touches
#      /root under sudo).
#   2. Restores pre-install files atomically from the latest backup manifest and
#      removes only files owned by xps17-studio-audio.
#   3. Mic-privacy & Bluetooth guard: verifies SHA-256 immutability of
#      no-mic-monitor.conf, 51-bluetooth-no-phone-audio.conf, and
#      easyeffectsrc (listenToMic=false) before and after uninstall.
#   4. Kernel DKMS uninstall (--with-kernel) requires root + DMI 0AFF match.

set -euo pipefail
IFS=$'\n\t'

readonly SCRIPT_NAME="xps17-studio-audio-uninstall"
readonly EXPECTED_DMI_SKU="0AFF"
readonly DKMS_PKG_NAME="snd-soc-rt1316-sdw-xps17"
readonly DKMS_PKG_VER="7.2.15.2"

# CLI defaults
WITH_KERNEL=0
DRY_RUN=0
NO_RESTART=0
KEEP_BACKUP=0
FORCE_SKIP_DMI=0
PREFIX_OVERRIDE=""
USER_HOME_OVERRIDE="${XPS17_LIVE_USER_HOME:-}"
DMI_DIR="${XPS17_DMI_DIR:-/sys/class/dmi/id}"

# Resolved runtime globals
LIVE_USER=""
LIVE_GROUP=""
LIVE_USER_HOME=""
PREFIX=""
STATE_DIR=""

# Pre-flight privacy guard snapshots
GUARD_MIC_MON_HASH=""
GUARD_BT_HASH=""
GUARD_EE_RC_HASH=""
GUARD_EE_LISTEN_MIC=""

log()    { printf '[%s] %s\n' "$SCRIPT_NAME" "$*"; }
warn()   { printf '[%s] WARN: %s\n' "$SCRIPT_NAME" "$*" >&2; }
err()    { printf '[%s] ERROR: %s\n' "$SCRIPT_NAME" "$*" >&2; }
die()    { err "$*"; exit 1; }
refuse() { err "REFUSED: $*"; exit 2; }

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Cleanly uninstall the Dell XPS 17 9720 Studio Audio Engineering Stack and
restore pre-install configs from the atomic backup manifest.

Options:
  --with-kernel              Also remove GPL-2.0-only rt1316-sdca DKMS module & UCM2 override
                             (requires sudo/root AND DMI product_sku == 0AFF)
  --dry-run                  Print every planned restoration/removal without modifying files
  --keep-backup              Keep backup archive under ~/.local/state/xps17-studio-audio/backups/
  --prefix <dir>             Userspace install prefix used during install (default: <LIVE_USER_HOME>/.local)
  --no-restart               Do not reload/restart user systemd audio services
  --force-skip-dmi-check     Allow userspace uninstall on non-0AFF hosts (prohibited with --with-kernel)
  --user-home <dir>          Explicit non-/root target user home (sandbox / staging use)
  -h, --help                 Show this help text and exit
EOF
}

parse_args() {
    while (($# > 0)); do
        case "$1" in
            --with-kernel)
                WITH_KERNEL=1
                ;;
            --dry-run)
                DRY_RUN=1
                ;;
            --keep-backup)
                KEEP_BACKUP=1
                ;;
            --no-restart)
                NO_RESTART=1
                ;;
            --force-skip-dmi-check)
                FORCE_SKIP_DMI=1
                ;;
            --prefix)
                shift
                (($# > 0)) || die "--prefix requires a directory argument"
                PREFIX_OVERRIDE="$1"
                ;;
            --user-home)
                shift
                (($# > 0)) || die "--user-home requires a directory argument"
                USER_HOME_OVERRIDE="$1"
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                die "Unknown argument: $1 (use --help for usage)"
                ;;
        esac
        shift
    done

    if ((WITH_KERNEL && FORCE_SKIP_DMI)); then
        refuse "--force-skip-dmi-check is STRICTLY PROHIBITED with --with-kernel!"
    fi
}

resolve_live_user_home() {
    local resolved_home=""

    if [[ -n "$USER_HOME_OVERRIDE" ]]; then
        resolved_home="$USER_HOME_OVERRIDE"
        if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
            LIVE_USER="$SUDO_USER"
        else
            LIVE_USER="$(id -un)"
        fi
    elif ((EUID == 0)); then
        if [[ -z "${SUDO_USER:-}" || "$SUDO_USER" == "root" ]]; then
            refuse "Invoked as root without non-root SUDO_USER! Refusing to operate on /root."
        fi
        LIVE_USER="$SUDO_USER"
        resolved_home="$(getent passwd "$LIVE_USER" | cut -d: -f6 || true)"
    else
        LIVE_USER="$(id -un)"
        resolved_home="${HOME:-}"
        if [[ -z "$resolved_home" ]]; then
            resolved_home="$(getent passwd "$LIVE_USER" | cut -d: -f6 || true)"
        fi
    fi

    while [[ "$resolved_home" != "/" && "$resolved_home" == */ ]]; do
        resolved_home="${resolved_home%/}"
    done

    if [[ -z "$resolved_home" || "$resolved_home" != /* ]]; then
        refuse "Resolved LIVE_USER_HOME '$resolved_home' is empty or not absolute."
    fi

    if [[ "$resolved_home" == "/" || "$resolved_home" == "/root" || "$resolved_home" == "/root/"* ]]; then
        refuse "Resolved LIVE_USER_HOME '$resolved_home' points to root! Aborting."
    fi

    if [[ ! -d "$resolved_home" ]]; then
        refuse "Resolved LIVE_USER_HOME '$resolved_home' does not exist."
    fi

    LIVE_USER_HOME="$resolved_home"
    LIVE_GROUP="$(id -gn "$LIVE_USER" 2>/dev/null || id -gn)"

    if [[ -n "$PREFIX_OVERRIDE" ]]; then
        PREFIX="${PREFIX_OVERRIDE%/}"
    else
        PREFIX="$LIVE_USER_HOME/.local"
    fi

    if [[ "$PREFIX" == "/" || "$PREFIX" == "/root" || "$PREFIX" == "/root/"* ]]; then
        refuse "Invalid --prefix '$PREFIX' (must not target / or /root)."
    fi

    STATE_DIR="$LIVE_USER_HOME/.local/state/xps17-studio-audio"
    log "Resolved LIVE_USER=$LIVE_USER LIVE_USER_HOME=$LIVE_USER_HOME PREFIX=$PREFIX"
}

check_dmi_if_kernel() {
    if ((!WITH_KERNEL)); then
        return 0
    fi

    local sku_file="$DMI_DIR/product_sku"
    local sku=""
    if [[ -r "$sku_file" ]]; then
        sku="$(tr -d '[:space:]' < "$sku_file")"
    fi

    if [[ "$sku" != "$EXPECTED_DMI_SKU" ]]; then
        refuse "Hardware DMI mismatch for --with-kernel uninstall: expected '$EXPECTED_DMI_SKU', found '${sku:-<missing>}'."
    fi
    if ((!DRY_RUN && EUID != 0)); then
        refuse "--with-kernel uninstall requires root privileges (use sudo)."
    fi
    log "DMI hardware guard PASSED ($EXPECTED_DMI_SKU) for kernel DKMS removal."
}

file_sha256_or_absent() {
    local path="$1"
    if [[ -f "$path" ]]; then
        sha256sum "$path" | awk '{print $1}'
    else
        printf 'ABSENT'
    fi
}

read_listen_to_mic_setting() {
    local rc_path="$1"
    if [[ ! -f "$rc_path" ]]; then
        printf 'ABSENT'
        return 0
    fi
    local val
    val="$(grep -E '^[[:space:]]*listenToMic[[:space:]]*=' "$rc_path" | tail -n 1 | cut -d= -f2- | tr -d '[:space:]' || true)"
    printf '%s' "${val:-UNSET}"
}

snapshot_privacy_guards() {
    local mic_mon="$LIVE_USER_HOME/.config/systemd/user/easyeffects-speaker-dsp.service.d/no-mic-monitor.conf"
    local bt_guard="$LIVE_USER_HOME/.config/wireplumber/wireplumber.conf.d/51-bluetooth-no-phone-audio.conf"
    local ee_rc="$LIVE_USER_HOME/.config/easyeffects/db/easyeffectsrc"

    GUARD_MIC_MON_HASH="$(file_sha256_or_absent "$mic_mon")"
    GUARD_BT_HASH="$(file_sha256_or_absent "$bt_guard")"
    GUARD_EE_RC_HASH="$(file_sha256_or_absent "$ee_rc")"
    GUARD_EE_LISTEN_MIC="$(read_listen_to_mic_setting "$ee_rc")"

    log "Privacy & Bluetooth guard pre-flight snapshot:"
    log "  no-mic-monitor.conf              : $GUARD_MIC_MON_HASH"
    log "  51-bluetooth-no-phone-audio.conf : $GUARD_BT_HASH"
    log "  easyeffectsrc                    : $GUARD_EE_RC_HASH (listenToMic=$GUARD_EE_LISTEN_MIC)"
}

verify_privacy_guards_preserved() {
    local mic_mon="$LIVE_USER_HOME/.config/systemd/user/easyeffects-speaker-dsp.service.d/no-mic-monitor.conf"
    local bt_guard="$LIVE_USER_HOME/.config/wireplumber/wireplumber.conf.d/51-bluetooth-no-phone-audio.conf"
    local ee_rc="$LIVE_USER_HOME/.config/easyeffects/db/easyeffectsrc"

    local post_mic post_bt post_ee post_listen
    post_mic="$(file_sha256_or_absent "$mic_mon")"
    post_bt="$(file_sha256_or_absent "$bt_guard")"
    post_ee="$(file_sha256_or_absent "$ee_rc")"
    post_listen="$(read_listen_to_mic_setting "$ee_rc")"

    local drift=0
    if [[ "$post_mic" != "$GUARD_MIC_MON_HASH" ]]; then
        err "PRIVACY GUARD VIOLATION: no-mic-monitor.conf changed ($GUARD_MIC_MON_HASH -> $post_mic)!"
        drift=1
    fi
    if [[ "$post_bt" != "$GUARD_BT_HASH" ]]; then
        err "BLUETOOTH GUARD VIOLATION: 51-bluetooth-no-phone-audio.conf changed ($GUARD_BT_HASH -> $post_bt)!"
        drift=1
    fi
    if [[ "$post_ee" != "$GUARD_EE_RC_HASH" ]]; then
        err "PRIVACY GUARD VIOLATION: easyeffectsrc changed ($GUARD_EE_RC_HASH -> $post_ee)!"
        drift=1
    fi
    if [[ "$GUARD_EE_LISTEN_MIC" == "false" && "$post_listen" != "false" ]]; then
        err "PRIVACY GUARD VIOLATION: listenToMic drifted from false to '$post_listen'!"
        drift=1
    fi

    if ((drift)); then
        exit 3
    fi

    log "Privacy & Bluetooth guard post-flight check PASSED (all guards untouched)."
}

assert_not_protected_guard() {
    local path="$1"
    case "$path" in
        *no-mic-monitor.conf|*51-bluetooth-no-phone-audio.conf|*easyeffectsrc)
            refuse "Internal safety tripwire: refusing to remove or modify protected guard path '$path'!"
            ;;
    esac
}

ensure_owned_by_live_user() {
    local target="$1"
    if ((EUID == 0)) && [[ -n "$LIVE_USER" && "$LIVE_USER" != "root" && "$target" == "$LIVE_USER_HOME"* ]]; then
        chown -h "$LIVE_USER:$LIVE_GROUP" "$target"
    fi
}

restore_from_manifest() {
    local latest_dir="$STATE_DIR/latest"
    local manifest="$latest_dir/manifest.tsv"

    if [[ ! -f "$manifest" ]]; then
        log "No backup manifest found at $manifest; performing targeted owned-artifact cleanup."
        cleanup_known_artifacts
        return 0
    fi

    log "Restoring pre-install state from manifest: $manifest"
    local action dst backup_rel _mode
    while IFS=$'\t' read -r action dst backup_rel _mode; do
        [[ -z "$action" || "$action" == \#* ]] && continue
        case "$action" in
            INSTALL)
                assert_not_protected_guard "$dst"
                # Skip system /etc paths unless --with-kernel was requested
                if [[ "$dst" == /etc/* && "$WITH_KERNEL" -eq 0 ]]; then
                    log "  [skip] system path $dst (pass --with-kernel to remove)"
                    continue
                fi
                if [[ "$backup_rel" != "-" && -e "$latest_dir/files/$backup_rel" ]]; then
                    if ((DRY_RUN)); then
                        log "  [dry-run] restore backup: $latest_dir/files/$backup_rel -> $dst"
                    else
                        local tmp_dst="${dst}.xps17restore.$$"
                        mkdir -p "$(dirname "$dst")"
                        cp -a "$latest_dir/files/$backup_rel" "$tmp_dst"
                        mv -f "$tmp_dst" "$dst"
                        ensure_owned_by_live_user "$dst"
                        log "  restored: $dst"
                    fi
                else
                    if [[ -e "$dst" || -L "$dst" ]]; then
                        if ((DRY_RUN)); then
                            log "  [dry-run] rm -f $dst"
                        else
                            rm -f "$dst"
                            log "  removed: $dst"
                        fi
                    fi
                fi
                ;;
            KERNEL_DKMS)
                if ((WITH_KERNEL)); then
                    remove_kernel_dkms "$dst"
                else
                    log "  [skip] kernel DKMS $dst (pass --with-kernel to remove)"
                fi
                ;;
        esac
    done < "$manifest"

    if ((DRY_RUN)); then
        log "  [dry-run] remove $STATE_DIR/active-profile and $STATE_DIR/latest"
        if ((!KEEP_BACKUP)); then
            log "  [dry-run] clean backup snapshot directory $(readlink -f "$latest_dir" || echo "$latest_dir")"
        fi
    else
        rm -f "$STATE_DIR/active-profile"
        local resolved_backup
        resolved_backup="$(readlink -f "$latest_dir" 2>/dev/null || true)"
        rm -f "$STATE_DIR/latest"
        if ((!KEEP_BACKUP)) && [[ -n "$resolved_backup" && "$resolved_backup" == "$STATE_DIR/backups/"* ]]; then
            rm -rf "$resolved_backup"
            log "  removed applied backup snapshot: $resolved_backup"
        else
            log "  kept backup archive (--keep-backup)."
        fi
    fi
}

cleanup_known_artifacts() {
    local owned_paths=(
        "$PREFIX/share/xps17-studio-audio/lib/libxps17_studio_dsp.so"
        "$PREFIX/lib/ladspa/libxps17_studio_dsp.so"
        "$PREFIX/bin/xps17-studio-cli"
        "$PREFIX/share/xps17-studio-audio/kernels/xps17-studio-deep.wav"
        "$PREFIX/share/xps17-studio-audio/kernels/xps17-studio-spatial.wav"
        "$PREFIX/share/xps17-studio-audio/kernels/xps17-studio-reference.wav"
        "$PREFIX/share/xps17-studio-audio/kernels/xps17-studio-voice.wav"
        "$PREFIX/share/xps17-studio-audio/kernels/dsp_params.json"
        "$LIVE_USER_HOME/.config/pipewire/omarchy-speaker-engine.conf.d/92-xps17-studio.conf"
        "$LIVE_USER_HOME/.config/systemd/user/omarchy-speaker-engine.service.d/xps17-studio-acoustics-dir.conf"
        "$STATE_DIR/active-profile"
    )
    local p
    for p in "${owned_paths[@]}"; do
        assert_not_protected_guard "$p"
        if [[ -e "$p" || -L "$p" ]]; then
            if ((DRY_RUN)); then
                log "  [dry-run] rm -f $p"
            else
                rm -f "$p"
                log "  removed: $p"
            fi
        else
            log "  [dry-run/check] absent: $p"
        fi
    done

    if ((WITH_KERNEL)); then
        remove_kernel_dkms "${DKMS_PKG_NAME}/${DKMS_PKG_VER}"
    fi
}

remove_kernel_dkms() {
    local spec="${1:-${DKMS_PKG_NAME}/${DKMS_PKG_VER}}"
    if ((DRY_RUN)); then
        log "  [dry-run] Would run: dkms remove $spec --all && depmod -a"
        return 0
    fi
    if command -v dkms >/dev/null 2>&1; then
        if dkms status "$spec" 2>/dev/null | grep -q "$DKMS_PKG_NAME"; then
            log "  removing DKMS module: $spec"
            dkms remove "$spec" --all
            depmod -a
        else
            log "  DKMS module $spec not currently registered."
        fi
    else
        warn "dkms binary not found; cannot unregister $spec automatically."
    fi
}

reload_user_services() {
    if ((DRY_RUN)); then
        log "[dry-run] Would reload user systemd daemon and restart speaker services (skipped in --dry-run)."
        return 0
    fi
    if ((NO_RESTART)); then
        log "Skipping service restarts (--no-restart)."
        return 0
    fi

    local live_uid runtime_dir
    live_uid="$(id -u "$LIVE_USER")"
    runtime_dir="/run/user/$live_uid"

    if [[ ! -S "$runtime_dir/bus" ]]; then
        warn "User D-Bus socket ($runtime_dir/bus) not active; skipping systemd --user restart."
        return 0
    fi

    log "Reloading user systemd units for $LIVE_USER..."
    if ((EUID == 0)); then
        sudo -u "$LIVE_USER" XDG_RUNTIME_DIR="$runtime_dir" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime_dir/bus" \
            systemctl --user daemon-reload
        sudo -u "$LIVE_USER" XDG_RUNTIME_DIR="$runtime_dir" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime_dir/bus" \
            systemctl --user try-restart omarchy-speaker-engine.service omarchy-speaker-dsp-controller.service
    else
        systemctl --user daemon-reload
        systemctl --user try-restart omarchy-speaker-engine.service omarchy-speaker-dsp-controller.service
    fi
}

main() {
    parse_args "$@"
    resolve_live_user_home
    check_dmi_if_kernel
    snapshot_privacy_guards
    restore_from_manifest
    verify_privacy_guards_preserved
    reload_user_services

    if ((DRY_RUN)); then
        log "DRY-RUN COMPLETE: zero files modified, all privacy & Bluetooth guards verified."
    else
        log "UNINSTALL COMPLETE: pre-install state restored cleanly."
    fi
}

main "$@"
