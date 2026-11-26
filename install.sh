#!/usr/bin/env bash
# Dell XPS 17 9720 Studio Audio Engineering Stack — Safe Atomic Installer
#
# Safety guarantees enforced unconditionally:
#   1. LIVE_USER_HOME guard: resolves the true non-root user home (never writes
#      user PipeWire/controller/profile files into /root under sudo).
#   2. DMI 0AFF hardware guard: verifies /sys/class/dmi/id/product_sku == 0AFF
#      (Dell XPS 17 9720). --force-skip-dmi-check is ONLY permitted in
#      --dsp-only mode and is hard-rejected for --with-kernel.
#   3. Mic-privacy & Bluetooth guard: verifies SHA-256 immutability of
#      no-mic-monitor.conf, 51-bluetooth-no-phone-audio.conf, and
#      easyeffectsrc (listenToMic=false) before and after installation.
#   4. Atomic staging + timestamped pre-install backup manifest for clean
#      restoration via uninstall.sh.

set -euo pipefail
IFS=$'\n\t'

readonly SCRIPT_NAME="xps17-studio-audio-install"
readonly EXPECTED_DMI_SKU="0AFF"
readonly EXPECTED_PRODUCT_SUBSTR="XPS 17 9720"
readonly DKMS_PKG_NAME="snd-soc-rt1316-sdw"
readonly DKMS_PKG_VER="7.2.15.2"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Detect repository root whether run from published repo root (./install.sh)
# or from staged monorepo (verify-integrate/packaging/install.sh).
if [[ -d "$SCRIPT_DIR/dsp-engine" || -d "$SCRIPT_DIR/acoustics-research" || -d "$SCRIPT_DIR/verify-integrate" ]]; then
    REPO_ROOT="$SCRIPT_DIR"
elif [[ -d "$SCRIPT_DIR/../../verify-integrate" ]]; then
    REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
else
    REPO_ROOT="$SCRIPT_DIR"
fi

# CLI defaults
WITH_KERNEL=0
PORTABLE_MODE=0
DRY_RUN=0
NO_RESTART=0
FORCE_SKIP_DMI=0
PREFIX_OVERRIDE=""
USER_HOME_OVERRIDE="${XPS17_LIVE_USER_HOME:-}"
DEFAULT_PROFILE="studio-deep"
DMI_DIR="${XPS17_DMI_DIR:-/sys/class/dmi/id}"

# Resolved runtime globals
LIVE_USER=""
LIVE_GROUP=""
LIVE_USER_HOME=""
PREFIX=""
STATE_DIR=""
BACKUP_ID="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR=""
MANIFEST_FILE=""

# Pre-flight privacy guard snapshots
GUARD_MIC_MON_HASH=""
GUARD_BT_HASH=""
GUARD_EE_RC_HASH=""
GUARD_EE_LISTEN_MIC=""

log()  { printf '[%s] %s\n' "$SCRIPT_NAME" "$*"; }
warn() { printf '[%s] WARN: %s\n' "$SCRIPT_NAME" "$*" >&2; }
err()  { printf '[%s] ERROR: %s\n' "$SCRIPT_NAME" "$*" >&2; }
die()  { err "$*"; exit 1; }
refuse() { err "REFUSED: $*"; exit 2; }

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Install the Dell XPS 17 9720 Studio Audio Engineering Stack.

Modes:
  --dsp-only                 Install Dell XPS 17 9720 calibrated userspace DSP stack (default on 0AFF)
  --portable, --any-omarchy  Universal mode for ANY x86_64 Omarchy / PipeWire machine (uses neutral
                             Harman-tilt FIRs, auto-detects host speaker sink & installs standalone controller)
  --with-kernel              Also install GPL-2.0-only rt1316-sdca DKMS + SOF IPC4 DRC fence
                             (requires sudo/root AND strict DMI product_sku == 0AFF)

Options:
  --dry-run                  Print every planned action and run safety checks without writing
  --prefix <dir>             Userspace install prefix (default: <LIVE_USER_HOME>/.local)
  --no-restart               Install files without reloading/restarting PipeWire or user units
  --profile <name>           Default profile: studio-deep | studio-spatial | studio-ref | studio-voice
                             (default: studio-deep)
  --force-skip-dmi-check     Bypass DMI 0AFF check ONLY for --dsp-only offline testing
                             (STRICTLY PROHIBITED with --with-kernel)
  --user-home <dir>          Explicit non-/root target user home (sandbox / staging use)
  -h, --help                 Show this help text and exit

Safety Guards:
  - Refuses to install user PipeWire/controller files into /root.
  - Enforces Dell XPS 17 9720 DMI product_sku == 0AFF before touching hardware.
  - Preserves no-mic-monitor.conf, listenToMic=false, and 51-bluetooth-no-phone-audio.conf.
EOF
}

parse_args() {
    while (($# > 0)); do
        case "$1" in
            --dsp-only)
                WITH_KERNEL=0
                PORTABLE_MODE=0
                ;;
            --portable|--any-omarchy)
                WITH_KERNEL=0
                PORTABLE_MODE=1
                ;;
            --with-kernel)
                WITH_KERNEL=1
                ;;
            --dry-run)
                DRY_RUN=1
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
            --profile)
                shift
                (($# > 0)) || die "--profile requires a profile name"
                case "$1" in
                    studio-deep|studio-spatial|studio-ref|studio-voice)
                        DEFAULT_PROFILE="$1"
                        ;;
                    *)
                        die "Invalid --profile '$1' (expected: studio-deep, studio-spatial, studio-ref, or studio-voice)"
                        ;;
                esac
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

    # Hard policy: NEVER allow DMI bypass or portable mode when touching kernel driver / hardware layer.
    if ((WITH_KERNEL && (FORCE_SKIP_DMI || PORTABLE_MODE))); then
        refuse "--force-skip-dmi-check / --portable is STRICTLY PROHIBITED with --with-kernel! Hardware register patches are calibrated exclusively for Dell XPS 17 9720 (DMI 0AFF)."
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
            refuse "Invoked as root without non-root SUDO_USER! Refusing to install user audio configs into /root. Run without sudo for --dsp-only, invoke via 'sudo ./install.sh --with-kernel' from your normal account, or pass --user-home <dir>."
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

    # Normalize trailing slashes (except literal "/")
    while [[ "$resolved_home" != "/" && "$resolved_home" == */ ]]; do
        resolved_home="${resolved_home%/}"
    done

    if [[ -z "$resolved_home" || "$resolved_home" != /* ]]; then
        refuse "Resolved LIVE_USER_HOME '$resolved_home' is empty or not an absolute path."
    fi

    # Absolute non-negotiable /root protection invariant
    if [[ "$resolved_home" == "/" || "$resolved_home" == "/root" || "$resolved_home" == "/root/"* ]]; then
        refuse "Resolved LIVE_USER_HOME '$resolved_home' points to root! Never install user PipeWire/controller files into /root."
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
    BACKUP_DIR="$STATE_DIR/backups/$BACKUP_ID"
    MANIFEST_FILE="$BACKUP_DIR/manifest.tsv"

    log "Resolved LIVE_USER=$LIVE_USER LIVE_USER_HOME=$LIVE_USER_HOME PREFIX=$PREFIX"
}

check_dmi_hardware_guard() {
    local sku_file="$DMI_DIR/product_sku"
    local name_file="$DMI_DIR/product_name"
    local sku=""
    local pname=""

    if [[ "$DMI_DIR" != "/sys/class/dmi/id" ]]; then
        log "Using DMI override directory (sandbox/test mode): $DMI_DIR"
        if ((WITH_KERNEL && !DRY_RUN)); then
            refuse "Custom XPS17_DMI_DIR is forbidden for live --with-kernel installation! Real /sys/class/dmi/id must verify 0AFF."
        fi
    fi

    if [[ -r "$sku_file" ]]; then
        sku="$(tr -d '[:space:]' < "$sku_file")"
    fi
    if [[ -r "$name_file" ]]; then
        pname="$(tr -d '\r\n' < "$name_file" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    fi

    log "Hardware DMI probe: product_sku='${sku:-<unreadable>}' product_name='${pname:-<unreadable>}'"

    if [[ "$sku" == "$EXPECTED_DMI_SKU" ]]; then
        if [[ -n "$pname" && "$pname" != *"$EXPECTED_PRODUCT_SUBSTR"* ]]; then
            warn "DMI product_sku matches '$EXPECTED_DMI_SKU' but product_name is '$pname' (expected '*$EXPECTED_PRODUCT_SUBSTR*')."
        fi
        log "DMI hardware guard PASSED: Dell XPS 17 9720 ($EXPECTED_DMI_SKU) confirmed."
        return 0
    fi

    if ((WITH_KERNEL)); then
        refuse "Hardware DMI mismatch! Expected product_sku='$EXPECTED_DMI_SKU' (Dell XPS 17 9720), found '${sku:-<missing>}' ('${pname:-<missing>}'). Aborting --with-kernel to protect non-XPS17 speaker amplifiers."
    fi

    if ((PORTABLE_MODE)); then
        log "Universal Omarchy Portable Mode enabled (--portable): using neutral Harman-tilt FIRs & host-detected speaker sink."
        return 0
    fi

    if ((FORCE_SKIP_DMI)); then
        warn "Bypassing DMI '$EXPECTED_DMI_SKU' hardware check (--force-skip-dmi-check in --dsp-only mode). Acoustic FIR & Klippel parameters are calibrated specifically for Dell XPS 17 9720."
        return 0
    fi

    refuse "Hardware DMI mismatch! Expected product_sku='$EXPECTED_DMI_SKU' (Dell XPS 17 9720), found '${sku:-<missing>}'. On any other Omarchy machine, run: ./install.sh --portable"
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

    if [[ "$GUARD_EE_LISTEN_MIC" == "true" ]]; then
        warn "Detected listenToMic=true in $ee_rc before install (not modified by this installer; check no-mic-monitor.conf)."
    fi
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

ensure_owned_by_live_user() {
    local target="$1"
    if ((EUID == 0)) && [[ -n "$LIVE_USER" && "$LIVE_USER" != "root" && "$target" == "$LIVE_USER_HOME"* ]]; then
        chown -h "$LIVE_USER:$LIVE_GROUP" "$target"
    fi
}

ensure_dir() {
    local dir="$1"
    if ((DRY_RUN)); then
        log "  [dry-run] mkdir -p $dir"
        return 0
    fi
    if [[ ! -d "$dir" ]]; then
        mkdir -p "$dir"
        ensure_owned_by_live_user "$dir"
    fi
}

init_backup_manifest() {
    if ((DRY_RUN)); then
        log "[dry-run] Would initialize backup directory: $BACKUP_DIR"
        log "[dry-run] Would write manifest: $MANIFEST_FILE"
        return 0
    fi
    ensure_dir "$STATE_DIR/backups"
    ensure_dir "$BACKUP_DIR/files"
    printf '# xps17-studio-audio install manifest v1 (%s)\n' "$BACKUP_ID" > "$MANIFEST_FILE"
    printf '# ACTION\tDEST_PATH\tBACKUP_REL\tMODE\n' >> "$MANIFEST_FILE"
    ensure_owned_by_live_user "$MANIFEST_FILE"
}

atomic_install_file() {
    local src="$1"
    local dst="$2"
    local mode="$3"
    local label="$4"

    # Never permit writing into protected privacy/Bluetooth guard paths.
    case "$dst" in
        *no-mic-monitor.conf|*51-bluetooth-no-phone-audio.conf|*easyeffectsrc)
            refuse "Internal safety tripwire: attempted to write protected guard path '$dst'!"
            ;;
    esac

    if [[ ! -f "$src" ]]; then
        if ((DRY_RUN)); then
            log "  [dry-run] PENDING BUILD ARTIFACT ($label): $src -> $dst (mode $mode)"
            return 0
        fi
        die "Required artifact missing ($label): $src. Build the stack before running live install."
    fi

    local dst_dir
    dst_dir="$(dirname "$dst")"

    if ((DRY_RUN)); then
        if [[ -e "$dst" || -L "$dst" ]]; then
            log "  [dry-run] backup existing $dst -> $BACKUP_DIR/files/..."
        fi
        log "  [dry-run] atomic install ($label): $src -> $dst (mode $mode)"
        return 0
    fi

    ensure_dir "$dst_dir"

    local backup_rel="-"
    if [[ -e "$dst" || -L "$dst" ]]; then
        backup_rel="${dst#/}"
        local bpath="$BACKUP_DIR/files/$backup_rel"
        mkdir -p "$(dirname "$bpath")"
        cp -a "$dst" "$bpath"
        ensure_owned_by_live_user "$bpath"
    fi

    local tmp_dst="${dst}.xps17tmp.$$"
    cp -f "$src" "$tmp_dst"
    chmod "$mode" "$tmp_dst"
    ensure_owned_by_live_user "$tmp_dst"
    mv -f "$tmp_dst" "$dst"
    ensure_owned_by_live_user "$dst"

    printf 'INSTALL\t%s\t%s\t%s\n' "$dst" "$backup_rel" "$mode" >> "$MANIFEST_FILE"
    log "  installed ($label): $dst"
}

install_userspace_dsp() {
    log "=== Installing Userspace Studio DSP Stack (--dsp-only layer) ==="
    log "Repository root: $REPO_ROOT"

    local ladspa_dir="$PREFIX/lib/ladspa"
    local share_lib_dir="$PREFIX/share/xps17-studio-audio/lib"
    local bin_dir="$PREFIX/bin"
    local kernel_share_dir="$PREFIX/share/xps17-studio-audio/kernels"
    local menu_share_dir="$PREFIX/share/xps17-studio-audio/menu"
    local pw_conf_dir="$LIVE_USER_HOME/.config/pipewire/omarchy-speaker-engine.conf.d"
    local ctrl_lib_dir="$LIVE_USER_HOME/.local/lib/omarchy_audio"

    # 1. Native AVX2/FMA + Rust LADSPA plugin & CLI (installed to canonical share/lib path used by 92-xps17-studio.conf + standard lib/ladspa)
    atomic_install_file \
        "$REPO_ROOT/dsp-engine/lib/libxps17_studio_dsp.so" \
        "$share_lib_dir/libxps17_studio_dsp.so" \
        0755 \
        "LADSPA plugin (canonical)"

    atomic_install_file \
        "$REPO_ROOT/dsp-engine/lib/libxps17_studio_dsp.so" \
        "$ladspa_dir/libxps17_studio_dsp.so" \
        0755 \
        "LADSPA plugin (LADSPA_PATH)"

    atomic_install_file \
        "$REPO_ROOT/dsp-engine/bin/xps17-studio-cli" \
        "$bin_dir/xps17-studio-cli" \
        0755 \
        "DSP CLI"

    # 2. 4096-tap minimum-phase FIR kernels + mathematical dsp_params.json
    #    In --portable mode on non-0AFF machines, install portable neutral/Harman-tilt FIRs under
    #    the canonical xps17-studio-*.wav runtime names expected by libxps17_studio_dsp.so Stage 3.
    local kname src_kname
    local host_sku=""
    [[ -r "$DMI_DIR/product_sku" ]] && host_sku="$(tr -d '[:space:]' < "$DMI_DIR/product_sku" || true)"
    for kname in xps17-studio-deep.wav xps17-studio-spatial.wav xps17-studio-reference.wav xps17-studio-voice.wav; do
        src_kname="$kname"
        if ((PORTABLE_MODE)) && [[ "$host_sku" != "$EXPECTED_DMI_SKU" ]]; then
            local cand_port="${kname/#xps17-/portable-}"
            if [[ -f "$REPO_ROOT/acoustics-research/kernels/$cand_port" ]]; then
                src_kname="$cand_port"
            fi
        fi
        atomic_install_file \
            "$REPO_ROOT/acoustics-research/kernels/$src_kname" \
            "$kernel_share_dir/$kname" \
            0644 \
            "FIR kernel ($src_kname)"
    done
    if [[ -f "$REPO_ROOT/acoustics-research/dsp_params.json" ]]; then
        atomic_install_file \
            "$REPO_ROOT/acoustics-research/dsp_params.json" \
            "$PREFIX/share/xps17-studio-audio/dsp_params.json" \
            0644 \
            "DSP params"
    elif [[ -f "$REPO_ROOT/acoustics-research/kernels/dsp_params.json" ]]; then
        atomic_install_file \
            "$REPO_ROOT/acoustics-research/kernels/dsp_params.json" \
            "$PREFIX/share/xps17-studio-audio/dsp_params.json" \
            0644 \
            "DSP params"
    else
        log "  [info] dsp_params.json not yet present in acoustics-research/; skipping optional copy"
    fi

    # 3. PipeWire filter-chain configs (rewrite absolute home/prefix paths cleanly for any user)
    local pw_staged="$REPO_ROOT/verify-integrate/staged/pipewire"
    local found_pw=0
    if [[ -d "$pw_staged" ]]; then
        local f
        while IFS= read -r -d '' f; do
            found_pw=1
            local dst_pw
            dst_pw="$pw_conf_dir/$(basename "$f")"
            if ((DRY_RUN)); then
                atomic_install_file "$f" "$dst_pw" 0644 "PipeWire config"
            else
                local rendered_pw
                rendered_pw="$(mktemp)"
                sed \
                    -e "s|/home/akshaybapat/.local/share/xps17-studio-audio|$PREFIX/share/xps17-studio-audio|g" \
                    -e "s|/home/akshaybapat/project/xps17-studio-audio/dsp-engine/lib|$share_lib_dir|g" \
                    -e "s|/home/akshaybapat/project/xps17-studio-audio/acoustics-research/kernels|$kernel_share_dir|g" \
                    "$f" > "$rendered_pw"
                if ((PORTABLE_MODE)) && [[ "$host_sku" != "$EXPECTED_DMI_SKU" ]]; then
                    # Detect host built-in speaker sink on generic Omarchy PCs, or omit hardcoded Dell PCI sink
                    local detected_sink=""
                    if command -v pactl >/dev/null 2>&1; then
                        detected_sink="$(pactl list short sinks 2>/dev/null | awk '
                            $2 ~ /^alsa_output\./ && tolower($2) !~ /hdmi|displayport|headphone|usb/ {
                                if (tolower($2) ~ /speaker/) { print $2; found=1; exit }
                                if (!first) first=$2
                            }
                            END { if (!found && first) print first }
                        ' || true)"
                    fi
                    if [[ -n "$detected_sink" ]]; then
                        sed -i -e "s|alsa_output.pci-0000_00_1f.3-platform-sof_sdw.HiFi__Speaker__sink|$detected_sink|g" "$rendered_pw"
                    else
                        sed -i -e '/target\.object[[:space:]]*=[[:space:]]*"alsa_output\.pci-0000_00_1f\.3-platform-sof_sdw\.HiFi__Speaker__sink"/d' "$rendered_pw"
                    fi
                fi
                atomic_install_file "$rendered_pw" "$dst_pw" 0644 "PipeWire config"
                rm -f "$rendered_pw"
            fi
        done < <(find "$pw_staged" -maxdepth 1 -type f -name '*.conf' -print0 | sort -z)
    fi
    if ((!found_pw)); then
        if ((DRY_RUN)); then
            log "  [dry-run] PENDING STAGED CONFIGS: $pw_staged/*.conf -> $pw_conf_dir/"
        else
            die "No PipeWire .conf files found in $pw_staged"
        fi
    fi

    # 4. Live ISO 226 controller & switcher:
    #    - If the full omarchy_audio controller package is already present on this host (XPS 17),
    #      upgrade model.py, runtime.py, and speaker-profile in place.
    #    - Always install universal studio-audio-profile & studio-audio-volume-sync, and bootstrap
    #      omarchy-speaker-engine.{conf,service} if absent so fresh Omarchy machines work out-of-the-box!
    local has_full_omarchy_audio=0
    if [[ -f "$ctrl_lib_dir/controller.py" && -f "$ctrl_lib_dir/server.py" ]]; then
        has_full_omarchy_audio=1
    fi

    local ctrl_staged="$REPO_ROOT/verify-integrate/staged/controller"
    if ((has_full_omarchy_audio)) && [[ -d "$ctrl_staged" ]]; then
        local f
        while IFS= read -r -d '' f; do
            atomic_install_file "$f" "$ctrl_lib_dir/$(basename "$f")" 0644 "ISO 226 controller"
        done < <(find "$ctrl_staged" -maxdepth 1 -type f -name '*.py' ! -name 'test_*.py' -print0 | sort -z)
    fi

    local bin_staged="$REPO_ROOT/verify-integrate/staged/bin"
    if ((has_full_omarchy_audio)) && [[ -d "$bin_staged" ]]; then
        local f
        while IFS= read -r -d '' f; do
            atomic_install_file "$f" "$bin_dir/$(basename "$f")" 0755 "CLI helper"
        done < <(find "$bin_staged" -maxdepth 1 -type f ! -name '*.diff' ! -name '*.patch' -print0 | sort -z)
    fi

    local portable_staged="$REPO_ROOT/verify-integrate/staged/portable"
    if [[ -d "$portable_staged" ]]; then
        atomic_install_file "$portable_staged/studio-audio-profile" "$bin_dir/studio-audio-profile" 0755 "Universal profile switcher"
        atomic_install_file "$portable_staged/studio-audio-volume-sync" "$bin_dir/studio-audio-volume-sync" 0755 "Universal ISO 226 volume sync"
        if ((!has_full_omarchy_audio)); then
            if [[ ! -f "$bin_dir/speaker-profile" ]]; then
                atomic_install_file "$portable_staged/studio-audio-profile" "$bin_dir/speaker-profile" 0755 "Portable speaker-profile CLI"
            fi
            if [[ ! -f "$LIVE_USER_HOME/.config/pipewire/omarchy-speaker-engine.conf" ]]; then
                atomic_install_file \
                    "$portable_staged/omarchy-speaker-engine.conf" \
                    "$LIVE_USER_HOME/.config/pipewire/omarchy-speaker-engine.conf" \
                    0644 \
                    "PipeWire filter-chain host config"
            fi
            if [[ ! -f "$LIVE_USER_HOME/.config/systemd/user/omarchy-speaker-engine.service" ]]; then
                atomic_install_file \
                    "$portable_staged/omarchy-speaker-engine.service" \
                    "$LIVE_USER_HOME/.config/systemd/user/omarchy-speaker-engine.service" \
                    0644 \
                    "PipeWire filter-chain host service"
            fi
            atomic_install_file \
                "$portable_staged/studio-audio-volume-sync.service" \
                "$LIVE_USER_HOME/.config/systemd/user/studio-audio-volume-sync.service" \
                0644 \
                "Universal ISO 226 volume sync service"
        fi
    fi

    # 6. Optional Omarchy menu assets & omarchy-speaker-engine acoustics-dir drop-in
    local menu_staged="$REPO_ROOT/verify-integrate/staged/menu"
    if [[ -d "$menu_staged" ]]; then
        local f
        while IFS= read -r -d '' f; do
            atomic_install_file "$f" "$menu_share_dir/$(basename "$f")" 0644 "Menu integration"
        done < <(find "$menu_staged" -maxdepth 1 -type f -print0 | sort -z)
    fi
    local systemd_dropin_src="$REPO_ROOT/verify-integrate/staged/systemd/xps17-studio-acoustics-dir.conf"
    if [[ -f "$systemd_dropin_src" ]]; then
        atomic_install_file \
            "$systemd_dropin_src" \
            "$LIVE_USER_HOME/.config/systemd/user/omarchy-speaker-engine.service.d/xps17-studio-acoustics-dir.conf" \
            0644 \
            "Engine acoustics-dir drop-in"
    fi

    # 7. Record active profile & latest backup symlink
    if ((DRY_RUN)); then
        log "  [dry-run] write default profile '$DEFAULT_PROFILE' -> $STATE_DIR/active-profile"
        log "  [dry-run] update symlink $STATE_DIR/latest -> $BACKUP_DIR"
    else
        printf '%s\n' "$DEFAULT_PROFILE" > "$STATE_DIR/active-profile"
        ensure_owned_by_live_user "$STATE_DIR/active-profile"
        ln -sfn "$BACKUP_DIR" "$STATE_DIR/latest"
        ensure_owned_by_live_user "$STATE_DIR/latest"
    fi
}

install_kernel_layer() {
    log "=== Installing Optional Kernel Layer (--with-kernel, GPL-2.0-only) ==="

    if ((!DRY_RUN && EUID != 0)); then
        refuse "--with-kernel requires root privileges (run via 'sudo ./install.sh --with-kernel')."
    fi

    local dkms_script="$REPO_ROOT/kernel-driver/verify/scripts/dkms-install.sh"
    local ucm_dir="$REPO_ROOT/kernel-driver/sof-ipc4"

    if [[ -x "$dkms_script" || -f "$dkms_script" ]]; then
        if ((DRY_RUN)); then
            log "  [dry-run] Would validate & invoke DKMS installer: bash $dkms_script --dry-run"
        else
            log "  Running verified DKMS installer: $dkms_script --confirm-live"
            bash "$dkms_script" --confirm-live
            printf 'KERNEL_DKMS\t%s/%s\t-\t0000\n' "$DKMS_PKG_NAME" "$DKMS_PKG_VER" >> "$MANIFEST_FILE"
        fi
    else
        if ((DRY_RUN)); then
            log "  [dry-run] PENDING KERNEL ARTIFACT: $dkms_script (DKMS $DKMS_PKG_NAME/$DKMS_PKG_VER)"
        else
            die "Kernel DKMS installer not found: $dkms_script"
        fi
    fi

    # Stage SOF IPC4 UCM2 DRC fence (prevents +13.7 dB Post Mixer DRC runaway trap)
    local ucm_target_dir="/etc/alsa/ucm2/conf.d/sof-soundwire"
    if [[ -d "$ucm_dir" ]]; then
        local f found_ucm=0
        while IFS= read -r -d '' f; do
            found_ucm=1
            atomic_install_file "$f" "$ucm_target_dir/$(basename "$f")" 0644 "SOF IPC4 UCM2 DRC fence"
        done < <(find "$ucm_dir" -maxdepth 2 -type f -name '*.conf' -print0 | sort -z)
        if ((!found_ucm)); then
            log "  [info] No UCM2 .conf fence files staged yet in $ucm_dir (SOF remains on safe IPC3 default)."
        fi
    fi
}

reload_user_services() {
    if ((DRY_RUN)); then
        log "[dry-run] Would reload user systemd daemon and restart omarchy-speaker-engine + omarchy-speaker-dsp-controller (skipped in --dry-run)."
        return 0
    fi
    if ((NO_RESTART)); then
        log "Skipping service restarts (--no-restart). Reload manually when ready:"
        log "  systemctl --user daemon-reload && systemctl --user restart omarchy-speaker-engine.service omarchy-speaker-dsp-controller.service"
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
    check_dmi_hardware_guard
    snapshot_privacy_guards
    init_backup_manifest

    install_userspace_dsp
    if ((WITH_KERNEL)); then
        install_kernel_layer
    else
        log "Skipping kernel driver layer (default --dsp-only mode; pass --with-kernel to opt in)."
    fi

    verify_privacy_guards_preserved
    reload_user_services

    if ((DRY_RUN)); then
        log "DRY-RUN COMPLETE: zero files modified, zero services restarted, all safety guards verified."
    else
        log "INSTALL COMPLETE: backup manifest saved to $MANIFEST_FILE"
        log "Switch profiles anytime with: speaker-profile $DEFAULT_PROFILE"
        log "To revert cleanly: $(basename "$SCRIPT_DIR/uninstall.sh")"
    fi
}

main "$@"
