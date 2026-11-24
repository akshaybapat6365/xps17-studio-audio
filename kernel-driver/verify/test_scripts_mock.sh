#!/usr/bin/env bash
# Adversarial offline mock-harness test suite for staged deployment & rollback scripts.
#
# Creates an isolated temporary sandbox (mktemp -d) with fake sysfs/procfs,
# fake SYS_DMI_SKU, and fake PATH binaries (sudo, systemctl, modprobe, rmmod,
# insmod, dkms, uname, modinfo, depmod, tee).
# NEVER touches live sysfs, modules, systemd, or hardware!
set -uo pipefail

VERIFY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$VERIFY_DIR/scripts"

SANDBOX="$(mktemp -d /tmp/xps17-verify-mock.XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT

PASS=0
FAIL=0
FAIL_LIST=()
ORIG_PATH="$PATH"

# Build fake command stubs in $SANDBOX/bin
mkdir -p "$SANDBOX/bin"

cat > "$SANDBOX/bin/sudo" <<'STUB'
#!/usr/bin/env bash
# Mock sudo: strips -n, logs invocation, executes command inside mock PATH.
if [[ "${1:-}" == "-n" ]]; then shift; fi
printf 'sudo %s\n' "$*" >> "$MOCK_STATE/calls.log"
exec "$@"
STUB

cat > "$SANDBOX/bin/uname" <<'STUB'
#!/usr/bin/env bash
if [[ "${1:-}" == "-r" ]]; then
    printf '%s\n' "${MOCK_KVER:-7.2.15-1-omarchy}"
else
    /usr/bin/uname "$@"
fi
STUB

cat > "$SANDBOX/bin/systemctl" <<'STUB'
#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >> "$MOCK_STATE/calls.log"
# Expect: --user <cmd> <unit>
if [[ "${1:-}" == "--user" ]]; then shift; fi
cmd="${1:-}"; shift || true
quiet=0
if [[ "${1:-}" == "--quiet" ]]; then quiet=1; shift; fi
unit="${1:-}"
case "$cmd" in
is-active)
    if [[ "$unit" == "omarchy-audio-route-recovery.timer" ]]; then
        st="$(cat "$MOCK_STATE/timer_state" 2>/dev/null || echo active)"
        ((quiet)) || printf '%s\n' "$st"
        [[ "$st" == "active" ]] && exit 0 || exit 3
    elif [[ "$unit" == "omarchy-audio-route-recovery.service" ]]; then
        st="$(cat "$MOCK_STATE/service_state" 2>/dev/null || echo inactive)"
        ((quiet)) || printf '%s\n' "$st"
        [[ "$st" == "active" ]] && exit 0 || exit 3
    else
        ((quiet)) || printf 'inactive\n'
        exit 3
    fi
    ;;
stop)
    if [[ "$unit" == "omarchy-audio-route-recovery.timer" ]]; then
        printf 'stop\n' >> "$MOCK_STATE/timer_ops.log"
        printf 'inactive\n' > "$MOCK_STATE/timer_state"
    fi
    exit 0
    ;;
start)
    if [[ "$unit" == "omarchy-audio-route-recovery.timer" ]]; then
        printf 'start\n' >> "$MOCK_STATE/timer_ops.log"
        printf 'active\n' > "$MOCK_STATE/timer_state"
    fi
    exit 0
    ;;
*)
    exit 0
    ;;
esac
STUB

cat > "$SANDBOX/bin/modinfo" <<'STUB'
#!/usr/bin/env bash
printf 'modinfo %s\n' "$*" >> "$MOCK_STATE/calls.log"
if [[ "${1:-}" == "-F" && "${2:-}" == "vermagic" ]]; then
    f="${3:-}"
    if [[ -f "$f" ]] && grep -q '^VERMAGIC=' "$f" 2>/dev/null; then
        sed -n 's/^VERMAGIC=//p' "$f" | head -n1
    else
        printf '%s SMP preempt mod_unload \n' "${MOCK_KVER:-7.2.15-1-omarchy}"
    fi
    exit 0
elif [[ "${1:-}" == "-F" && "${2:-}" == "srcversion" ]]; then
    f="${3:-}"
    if [[ -f "$f" ]] && grep -q '^SRCVERSION=' "$f" 2>/dev/null; then
        sed -n 's/^SRCVERSION=//p' "$f" | head -n1
    else
        printf 'STOCKSRCV00000000000000\n'
    fi
    exit 0
elif [[ "${1:-}" == "-n" ]]; then
    printf '%s/lib/modules/%s/updates/dkms/snd-soc-rt1316-sdw.ko.zst\n' "$XPS17_ROOT" "${MOCK_KVER:-7.2.15-1-omarchy}"
    exit 0
fi
exit 0
STUB

cat > "$SANDBOX/bin/rmmod" <<'STUB'
#!/usr/bin/env bash
printf 'rmmod %s\n' "$*" >> "$MOCK_STATE/calls.log"
printf 'rmmod %s\n' "$*" >> "$MOCK_STATE/mutations.log"
if [[ "${MOCK_FAIL_RMMOD:-0}" == "1" ]]; then
    echo "rmmod: simulated failure" >&2
    exit 1
fi
mod="${1:-}"
rm -rf "$XPS17_ROOT/sys/module/$mod"
for a in sdw:0:1:025d:1316:01 sdw:0:2:025d:1316:01; do
    rm -f "$XPS17_ROOT/sys/bus/soundwire/devices/$a/driver"
done
exit 0
STUB

cat > "$SANDBOX/bin/insmod" <<'STUB'
#!/usr/bin/env bash
printf 'insmod %s\n' "$*" >> "$MOCK_STATE/calls.log"
printf 'insmod %s\n' "$*" >> "$MOCK_STATE/mutations.log"
ko="${1:-}"
if [[ "${MOCK_FAIL_INSMOD:-0}" == "1" && "$ko" != *stock* && "$ko" != */kernel/sound/soc/codecs/* ]]; then
    echo "insmod: simulated failure for $ko" >&2
    exit 1
fi
mkdir -p "$XPS17_ROOT/sys/module/snd_soc_rt1316_sdw/parameters"
echo 0 > "$XPS17_ROOT/sys/module/snd_soc_rt1316_sdw/refcnt"
if [[ "$ko" != *stock* && "$ko" != */kernel/sound/soc/codecs/* ]]; then
    echo "locked" > "$XPS17_ROOT/sys/module/snd_soc_rt1316_sdw/parameters/xps17_guard_state"
else
    rm -f "$XPS17_ROOT/sys/module/snd_soc_rt1316_sdw/parameters/xps17_guard_state"
fi
for a in sdw:0:1:025d:1316:01 sdw:0:2:025d:1316:01; do
    ln -sfn "$XPS17_ROOT/sys/bus/soundwire/drivers/rt1316-sdca" "$XPS17_ROOT/sys/bus/soundwire/devices/$a/driver"
    echo "Attached" > "$XPS17_ROOT/sys/bus/soundwire/devices/$a/status"
done
exit 0
STUB

cat > "$SANDBOX/bin/modprobe" <<'STUB'
#!/usr/bin/env bash
printf 'modprobe %s\n' "$*" >> "$MOCK_STATE/calls.log"
printf 'modprobe %s\n' "$*" >> "$MOCK_STATE/mutations.log"
if [[ "${MOCK_FAIL_MODPROBE:-0}" == "1" ]]; then
    echo "modprobe: simulated failure" >&2
    exit 1
fi
mkdir -p "$XPS17_ROOT/sys/module/snd_soc_rt1316_sdw/parameters"
echo 0 > "$XPS17_ROOT/sys/module/snd_soc_rt1316_sdw/refcnt"
if [[ "${MOCK_STUCK_CUSTOM_PARAM:-0}" == "1" ]]; then
    echo "locked" > "$XPS17_ROOT/sys/module/snd_soc_rt1316_sdw/parameters/xps17_guard_state"
    echo "XPS17SRCV00000000000000" > "$XPS17_ROOT/sys/module/snd_soc_rt1316_sdw/srcversion"
elif [[ "${MOCK_WRONG_SRCV:-0}" == "1" ]]; then
    rm -f "$XPS17_ROOT/sys/module/snd_soc_rt1316_sdw/parameters/xps17_guard_state"
    echo "XPS17SRCV00000000000000" > "$XPS17_ROOT/sys/module/snd_soc_rt1316_sdw/srcversion"
else
    rm -f "$XPS17_ROOT/sys/module/snd_soc_rt1316_sdw/parameters/xps17_guard_state"
    echo "STOCKSRCV00000000000000" > "$XPS17_ROOT/sys/module/snd_soc_rt1316_sdw/srcversion"
fi
for a in sdw:0:1:025d:1316:01 sdw:0:2:025d:1316:01; do
    ln -sfn "$XPS17_ROOT/sys/bus/soundwire/drivers/rt1316-sdca" "$XPS17_ROOT/sys/bus/soundwire/devices/$a/driver"
    echo "Attached" > "$XPS17_ROOT/sys/bus/soundwire/devices/$a/status"
done
exit 0
STUB

cat > "$SANDBOX/bin/dkms" <<'STUB'
#!/usr/bin/env bash
printf 'dkms %s\n' "$*" >> "$MOCK_STATE/calls.log"
printf 'dkms %s\n' "$*" >> "$MOCK_STATE/mutations.log"
if [[ "${MOCK_FAIL_DKMS:-0}" == "1" ]]; then
    echo "dkms: simulated failure" >&2
    exit 1
fi
exit 0
STUB

cat > "$SANDBOX/bin/depmod" <<'STUB'
#!/usr/bin/env bash
printf 'depmod %s\n' "$*" >> "$MOCK_STATE/calls.log"
printf 'depmod %s\n' "$*" >> "$MOCK_STATE/mutations.log"
exit 0
STUB

cat > "$SANDBOX/bin/tee" <<'STUB'
#!/usr/bin/env bash
target="${1:-}"
val="$(cat)"
if [[ "$target" == "$XPS17_ROOT/sys/"* ]]; then
    rel="${target#"$XPS17_ROOT"}"
    printf 'sysfs_write %s=%s\n' "$rel" "$val" >> "$MOCK_STATE/calls.log"
    printf 'sysfs_write %s=%s\n' "$rel" "$val" >> "$MOCK_STATE/mutations.log"
    case "$rel" in
    /sys/bus/platform/drivers/sof_sdw/unbind)
        rm -rf "$XPS17_ROOT/sys/bus/platform/drivers/sof_sdw/sof_sdw"
        rm -rf "$XPS17_ROOT/proc/asound/sofsoundwire"
        echo 0 > "$XPS17_ROOT/sys/module/snd_soc_rt1316_sdw/refcnt" 2>/dev/null || true
        ;;
    /sys/bus/platform/drivers/sof_sdw/bind)
        if [[ "${MOCK_FAIL_BIND_ONCE:-0}" == "1" && ! -f "$MOCK_STATE/bind_failed_once" ]]; then
            touch "$MOCK_STATE/bind_failed_once"
            echo "tee: simulated bind failure" >&2
            exit 1
        fi
        mkdir -p "$XPS17_ROOT/sys/bus/platform/drivers/sof_sdw/sof_sdw"
        mkdir -p "$XPS17_ROOT/proc/asound/sofsoundwire"
        ;;
    */power/control)
        printf '%s\n' "$val" > "$target"
        ;;
    *)
        printf '%s\n' "$val" > "$target"
        ;;
    esac
    printf '%s' "$val"
    exit 0
fi
printf '%s' "$val" | /usr/bin/tee "$@"
STUB

chmod +x "$SANDBOX/bin/"*

# SAFETY GATE: Verify every state-changing binary resolves to our sandbox stub!
for bin in sudo systemctl modprobe rmmod insmod dkms uname modinfo depmod tee; do
    resolved="$(PATH="$SANDBOX/bin:$ORIG_PATH" command -v "$bin")"
    if [[ "$resolved" != "$SANDBOX/bin/$bin" ]]; then
        echo "SAFETY ABORT: $bin resolved to '$resolved', expected '$SANDBOX/bin/$bin'" >&2
        exit 99
    fi
done
echo "SAFETY GATE OK: all 10 privileged/system commands intercepted by sandbox stubs"

# Helper to build a fresh isolated mock environment per test case
setup_case() {
    local name="$1"
    CASE_NAME="$name"
    CASE_DIR="$SANDBOX/$name"
    rm -rf "$CASE_DIR"
    export XPS17_ROOT="$CASE_DIR/root"
    export MOCK_STATE="$CASE_DIR/state"
    export XPS17_DRIVER_DIR="$CASE_DIR/driver"
    export XPS17_RUNS_DIR="$CASE_DIR/runs"
    export SYS_DMI_SKU="$XPS17_ROOT/sys/class/dmi/id/product_sku"
    export MOCK_KVER="7.2.15-1-omarchy"
    export XPS17_KVER="$MOCK_KVER"
    export XPS17_FORCE_SUDO=1
    export XPS17_POLL_SLEEP=0.01
    export XPS17_TMO_QUERY=5
    export XPS17_TMO_MODULE=5
    export XPS17_TMO_SYSFS=5
    export XPS17_TMO_KILL=1
    export MOCK_FAIL_RMMOD=0
    export MOCK_FAIL_INSMOD=0
    export MOCK_FAIL_MODPROBE=0
    export MOCK_FAIL_DKMS=0
    export MOCK_FAIL_BIND_ONCE=0
    export MOCK_STUCK_CUSTOM_PARAM=0
    export MOCK_WRONG_SRCV=0

    mkdir -p "$MOCK_STATE" "$XPS17_DRIVER_DIR" "$XPS17_RUNS_DIR" \
        "$XPS17_ROOT/sys/class/dmi/id" \
        "$XPS17_ROOT/sys/module/snd_soc_rt1316_sdw/parameters" \
        "$XPS17_ROOT/sys/bus/platform/drivers/sof_sdw/sof_sdw" \
        "$XPS17_ROOT/sys/bus/soundwire/drivers/rt1316-sdca" \
        "$XPS17_ROOT/sys/bus/soundwire/devices" \
        "$XPS17_ROOT/sys/devices/sdw-link1/sub/sdw:0:1:025d:1316:01" \
        "$XPS17_ROOT/sys/devices/sdw-link1/power" \
        "$XPS17_ROOT/sys/devices/sdw-link2/sub/sdw:0:2:025d:1316:01" \
        "$XPS17_ROOT/sys/devices/sdw-link2/power" \
        "$XPS17_ROOT/proc/asound/sofsoundwire" \
        "$XPS17_ROOT/lib/modules/$MOCK_KVER/kernel/sound/soc/codecs" \
        "$XPS17_ROOT/lib/modules/$MOCK_KVER/build" \
        "$XPS17_ROOT/var/lib/alsa" \
        "$XPS17_ROOT/usr/src" \
        "$XPS17_ROOT/sys/class/hwmon/hwmon0"

    echo "0AFF" > "$SYS_DMI_SKU"
    echo "1" > "$XPS17_ROOT/sys/module/snd_soc_rt1316_sdw/refcnt"
    echo "auto" > "$XPS17_ROOT/sys/devices/sdw-link1/power/control"
    echo "auto" > "$XPS17_ROOT/sys/devices/sdw-link2/power/control"
    ln -sfn "$XPS17_ROOT/sys/devices/sdw-link1/sub/sdw:0:1:025d:1316:01" "$XPS17_ROOT/sys/bus/soundwire/devices/sdw:0:1:025d:1316:01"
    ln -sfn "$XPS17_ROOT/sys/devices/sdw-link2/sub/sdw:0:2:025d:1316:01" "$XPS17_ROOT/sys/bus/soundwire/devices/sdw:0:2:025d:1316:01"
    for a in sdw:0:1:025d:1316:01 sdw:0:2:025d:1316:01; do
        echo "Attached" > "$XPS17_ROOT/sys/bus/soundwire/devices/$a/status"
        ln -sfn "$XPS17_ROOT/sys/bus/soundwire/drivers/rt1316-sdca" "$XPS17_ROOT/sys/bus/soundwire/devices/$a/driver"
    done

    # Fake thermal sensor (62C)
    echo "coretemp" > "$XPS17_ROOT/sys/class/hwmon/hwmon0/name"
    echo "Package id 0" > "$XPS17_ROOT/sys/class/hwmon/hwmon0/temp1_label"
    echo "62000" > "$XPS17_ROOT/sys/class/hwmon/hwmon0/temp1_input"

    # Fake clean ALSA saved state (XU24 Bypass Switch = false)
    cat > "$XPS17_ROOT/var/lib/alsa/asound.state" <<'ALSA'
state.sofsoundwire {
    control.1 {
        iface MIXER
        name 'rt1316-1 XU24 Bypass Switch'
        value false
    }
    control.2 {
        iface MIXER
        name 'rt1316-2 XU24 Bypass Switch'
        value false
    }
}
ALSA

    # Fake stock .ko.zst & staged custom .ko + source files + valid dkms.conf
    printf 'VERMAGIC=%s SMP preempt mod_unload \n' "$MOCK_KVER" \
        > "$XPS17_ROOT/lib/modules/$MOCK_KVER/kernel/sound/soc/codecs/snd-soc-rt1316-sdw.ko.zst"
    printf 'VERMAGIC=%s SMP preempt mod_unload \n' "$MOCK_KVER" \
        > "$XPS17_DRIVER_DIR/snd-soc-rt1316-sdw.ko"
    cat > "$XPS17_DRIVER_DIR/dkms.conf" <<EOF
PACKAGE_NAME="snd-soc-rt1316-sdw-xps17"
PACKAGE_VERSION="7.2.15.2"
BUILT_MODULE_NAME[0]="snd-soc-rt1316-sdw"
DEST_MODULE_LOCATION[0]="/updates/dkms"
AUTOINSTALL="yes"
BUILD_EXCLUSIVE_KERNEL="^7\.2\.15-1-omarchy$"
EOF
    echo "/* mock c */" > "$XPS17_DRIVER_DIR/rt1316-sdw.c"
    echo "/* mock h */" > "$XPS17_DRIVER_DIR/rt1316-sdw.h"
    echo "/* mock policy */" > "$XPS17_DRIVER_DIR/rt1316-xps17-policy.h"
    echo "obj-m += snd-soc-rt1316-sdw.o" > "$XPS17_DRIVER_DIR/Makefile"
    (cd "$XPS17_DRIVER_DIR" && sha256sum dkms.conf Makefile rt1316-sdw.c rt1316-sdw.h rt1316-xps17-policy.h > SHA256SUMS)

    echo "active" > "$MOCK_STATE/timer_state"
    echo "inactive" > "$MOCK_STATE/service_state"
    : > "$MOCK_STATE/calls.log"
    : > "$MOCK_STATE/mutations.log"
    : > "$MOCK_STATE/timer_ops.log"
}

run_mock() {
    PATH="$SANDBOX/bin:$ORIG_PATH" "$@" > "$CASE_DIR/stdout" 2> "$CASE_DIR/stderr"
    LAST_RC=$?
}

CASE_OK=1
assert_eq() {
    local desc="$1" got="$2" want="$3"
    if [[ "$got" != "$want" ]]; then
        CASE_OK=0
        printf '    FAIL [%s]: %s (got=%q want=%q)\n' "$CASE_NAME" "$desc" "$got" "$want"
    fi
}

assert_grep() {
    local desc="$1" pat="$2" file="$3"
    if ! grep -Eq "$pat" "$file" 2>/dev/null; then
        CASE_OK=0
        printf '    FAIL [%s]: %s (pattern %q not found in %s)\n' "$CASE_NAME" "$desc" "$pat" "$file"
    fi
}

assert_zero_mutations() {
    local muts timer_ops
    muts="$(wc -l < "$MOCK_STATE/mutations.log")"
    timer_ops="$(wc -l < "$MOCK_STATE/timer_ops.log")"
    assert_eq "zero module/sysfs/dkms mutations" "$muts" "0"
    assert_eq "zero route-timer mutations" "$timer_ops" "0"
}

finish_case() {
    if ((CASE_OK)); then
        PASS=$((PASS + 1))
        printf '  [PASS] %s\n' "$CASE_NAME"
    else
        FAIL=$((FAIL + 1))
        FAIL_LIST+=("$CASE_NAME")
        printf '  [FAIL] %s\n' "$CASE_NAME"
        printf '    --- stdout ---\n'; sed 's/^/      /' "$CASE_DIR/stdout" || true
        printf '    --- stderr ---\n'; sed 's/^/      /' "$CASE_DIR/stderr" || true
        printf '    --- mutations ---\n'; sed 's/^/      /' "$MOCK_STATE/mutations.log" || true
    fi
    CASE_OK=1
}

echo "=== Running Adversarial Mock-Harness Script Tests ==="

# 1. Wrong DMI SKU (0BEE) -> hard abort with zero mutations across all 3 scripts
for script in swap-in.sh swap-out.sh dkms-install.sh; do
    setup_case "wrong-dmi-$script"
    echo "0BEE" > "$SYS_DMI_SKU"
    run_mock "$SCRIPTS_DIR/$script" --confirm-live
    assert_eq "exit code 2" "$LAST_RC" "2"
    assert_grep "refused reason=wrong-dmi-sku" "OUTCOME=refused reason=wrong-dmi-sku" "$CASE_DIR/stdout"
    assert_zero_mutations
    finish_case
done

# 2. Unreadable/missing DMI SKU file -> hard abort with zero mutations
setup_case "missing-dmi-file"
rm -f "$SYS_DMI_SKU"
run_mock "$SCRIPTS_DIR/swap-in.sh" --confirm-live
assert_eq "exit code 2" "$LAST_RC" "2"
assert_grep "refused reason=dmi-unreadable" "OUTCOME=refused reason=dmi-unreadable" "$CASE_DIR/stdout"
assert_zero_mutations
finish_case

# 3. Missing --confirm-live -> refuses live mutation across all 3 scripts
for script in swap-in.sh swap-out.sh dkms-install.sh; do
    setup_case "missing-confirm-$script"
    run_mock "$SCRIPTS_DIR/$script"
    assert_eq "exit code 2" "$LAST_RC" "2"
    assert_grep "refused reason=no-confirm-live" "OUTCOME=refused reason=no-confirm-live" "$CASE_DIR/stdout"
    assert_zero_mutations
    finish_case
done

# 4. Conflicting --dry-run and --confirm-live -> refuses exit 2
setup_case "conflicting-mode-flags"
run_mock "$SCRIPTS_DIR/swap-in.sh" --dry-run --confirm-live
assert_eq "exit code 2" "$LAST_RC" "2"
assert_grep "conflicting-flags" "OUTCOME=refused reason=conflicting-flags" "$CASE_DIR/stdout"
assert_zero_mutations
finish_case

# 5. swap-in.sh --dry-run -> validates and prints plan with ZERO mutations
setup_case "swap-in-dry-run"
run_mock "$SCRIPTS_DIR/swap-in.sh" --dry-run
assert_eq "exit code 0" "$LAST_RC" "0"
assert_grep "dry-run-ok" "OUTCOME=dry-run-ok" "$CASE_DIR/stdout"
assert_grep "mentions deadman locked" "0xC60F=0x01" "$CASE_DIR/stdout"
assert_zero_mutations
finish_case

# 6. swap-in.sh --confirm-live (happy path) -> exact safe order & timer pause/restore
setup_case "swap-in-confirm-live-happy"
run_mock "$SCRIPTS_DIR/swap-in.sh" --confirm-live
assert_eq "exit code 0" "$LAST_RC" "0"
assert_grep "swap-in-ok" "OUTCOME=swap-in-ok" "$CASE_DIR/stdout"
assert_eq "timer ops exact stop then start" "$(tr '\n' ',' < "$MOCK_STATE/timer_ops.log")" "stop,start,"
assert_eq "timer active at end" "$(cat "$MOCK_STATE/timer_state")" "active"
assert_eq "link1 restored to auto" "$(cat "$XPS17_ROOT/sys/devices/sdw-link1/power/control")" "auto"
assert_eq "link2 restored to auto" "$(cat "$XPS17_ROOT/sys/devices/sdw-link2/power/control")" "auto"
# Verify exact mutation sequence: pin -> unbind -> rmmod -> insmod -> bind -> unpin
MUT_ORDER="$(awk '
    /power\/control=on/ && !seen_pin {print "pin"; seen_pin=1}
    /sof_sdw\/unbind/ {print "unbind"}
    /^rmmod / {print "rmmod"}
    /^insmod / {print "insmod"}
    /sof_sdw\/bind/ {print "bind"}
    /power\/control=auto/ && !seen_unpin {print "unpin"; seen_unpin=1}
' "$MOCK_STATE/mutations.log" | tr '\n' ',')"
assert_eq "exact safe order" "$MUT_ORDER" "pin,unbind,rmmod,insmod,bind,unpin,"
finish_case

# 7. swap-in.sh --confirm-live when route timer originally inactive -> untouched
setup_case "swap-in-timer-was-inactive"
echo "inactive" > "$MOCK_STATE/timer_state"
run_mock "$SCRIPTS_DIR/swap-in.sh" --confirm-live
assert_eq "exit code 0" "$LAST_RC" "0"
assert_eq "zero timer ops" "$(wc -l < "$MOCK_STATE/timer_ops.log")" "0"
assert_eq "timer still inactive" "$(cat "$MOCK_STATE/timer_state")" "inactive"
finish_case

# 8. swap-in.sh --confirm-live simulated mid-swap insmod failure -> auto-rollback + timer restored!
setup_case "swap-in-mid-failure-insmod-rollback"
export MOCK_FAIL_INSMOD=1
run_mock "$SCRIPTS_DIR/swap-in.sh" --confirm-live
assert_eq "exit code 3 (rolled back)" "$LAST_RC" "3"
assert_grep "rolled-back-stock" "OUTCOME=rolled-back-stock reason=insmod-failed" "$CASE_DIR/stdout"
assert_grep "modprobe stock executed on rollback" "^modprobe snd-soc-rt1316-sdw$" "$MOCK_STATE/mutations.log"
assert_eq "timer paused then restored despite failure" "$(tr '\n' ',' < "$MOCK_STATE/timer_ops.log")" "stop,start,"
assert_eq "timer active after rollback" "$(cat "$MOCK_STATE/timer_state")" "active"
assert_eq "card re-bound after rollback" "$( [[ -d "$XPS17_ROOT/proc/asound/sofsoundwire" ]] && echo yes || echo no )" "yes"
assert_eq "link1 unpinned after rollback" "$(cat "$XPS17_ROOT/sys/devices/sdw-link1/power/control")" "auto"
finish_case

# 9. swap-in.sh --confirm-live simulated mid-swap bind failure -> auto-rollback + timer restored!
setup_case "swap-in-mid-failure-bind-rollback"
export MOCK_FAIL_BIND_ONCE=1
run_mock "$SCRIPTS_DIR/swap-in.sh" --confirm-live
assert_eq "exit code 3 (rolled back)" "$LAST_RC" "3"
assert_grep "rolled-back-stock bind-failed" "OUTCOME=rolled-back-stock reason=bind-failed" "$CASE_DIR/stdout"
assert_grep "modprobe stock executed on rollback" "^modprobe snd-soc-rt1316-sdw$" "$MOCK_STATE/mutations.log"
assert_eq "timer paused then restored despite bind failure" "$(tr '\n' ',' < "$MOCK_STATE/timer_ops.log")" "stop,start,"
assert_eq "timer active after bind rollback" "$(cat "$MOCK_STATE/timer_state")" "active"
finish_case

# 10. swap-in.sh missing module file -> refuses exit 2, zero mutations
setup_case "swap-in-missing-module"
rm -f "$XPS17_DRIVER_DIR/snd-soc-rt1316-sdw.ko"
run_mock "$SCRIPTS_DIR/swap-in.sh" --confirm-live
assert_eq "exit code 2" "$LAST_RC" "2"
assert_grep "missing-module" "OUTCOME=refused reason=missing-module" "$CASE_DIR/stdout"
assert_zero_mutations
finish_case

# 11. swap-in.sh vermagic mismatch -> refuses exit 2, zero mutations
setup_case "swap-in-vermagic-mismatch"
printf 'VERMAGIC=6.12.0-wrong SMP preempt mod_unload \n' > "$XPS17_DRIVER_DIR/snd-soc-rt1316-sdw.ko"
run_mock "$SCRIPTS_DIR/swap-in.sh" --confirm-live
assert_eq "exit code 2" "$LAST_RC" "2"
assert_grep "vermagic-mismatch" "OUTCOME=refused reason=vermagic-mismatch" "$CASE_DIR/stdout"
assert_zero_mutations
finish_case

# 12. swap-out.sh --confirm-live -> cleanly restores stock module & timer
setup_case "swap-out-confirm-live-happy"
echo "locked" > "$XPS17_ROOT/sys/module/snd_soc_rt1316_sdw/parameters/xps17_guard_state"
run_mock "$SCRIPTS_DIR/swap-out.sh" --confirm-live
assert_eq "exit code 0" "$LAST_RC" "0"
assert_grep "stock-restored" "OUTCOME=stock-restored" "$CASE_DIR/stdout"
assert_grep "rmmod executed" "^rmmod snd_soc_rt1316_sdw$" "$MOCK_STATE/mutations.log"
assert_grep "modprobe stock executed" "^modprobe snd-soc-rt1316-sdw$" "$MOCK_STATE/mutations.log"
assert_eq "custom param gone" "$( [[ ! -e "$XPS17_ROOT/sys/module/snd_soc_rt1316_sdw/parameters/xps17_guard_state" ]] && echo yes || echo no )" "yes"
assert_eq "timer paused and restored" "$(tr '\n' ',' < "$MOCK_STATE/timer_ops.log")" "stop,start,"
assert_eq "timer active at end" "$(cat "$MOCK_STATE/timer_state")" "active"
finish_case

# 13. swap-out.sh when custom module refuses to unload/remains -> fails exit 4 AND restores timer
setup_case "swap-out-detects-custom-still-loaded"
export MOCK_STUCK_CUSTOM_PARAM=1
run_mock "$SCRIPTS_DIR/swap-out.sh" --confirm-live
assert_eq "exit code 4" "$LAST_RC" "4"
assert_grep "custom-still-loaded" "OUTCOME=swap-out-failed reason=custom-still-loaded" "$CASE_DIR/stdout"
assert_eq "timer still restored via trap" "$(cat "$MOCK_STATE/timer_state")" "active"
finish_case

# 14. dkms-install.sh --dry-run -> validates dkms.conf + headers + checksums + XU24 + thermal, zero mutations
setup_case "dkms-install-dry-run"
run_mock "$SCRIPTS_DIR/dkms-install.sh" --dry-run
assert_eq "exit code 0" "$LAST_RC" "0"
assert_grep "dry-run-ok" "OUTCOME=dry-run-ok" "$CASE_DIR/stdout"
assert_grep "dkms.conf validated" "dkms.conf validated: snd-soc-rt1316-sdw-xps17/7.2.15.2" "$CASE_DIR/stdout"
assert_zero_mutations
assert_eq "usr/src not staged in dry-run" "$( [[ ! -e "$XPS17_ROOT/usr/src/snd-soc-rt1316-sdw-xps17-7.2.15.2" ]] && echo yes || echo no )" "yes"
finish_case

# 15. dkms-install.sh --confirm-live -> stages exact /usr/src/snd-soc-rt1316-sdw-xps17-7.2.15.2/ and invokes exact commands
setup_case "dkms-install-confirm-live"
run_mock "$SCRIPTS_DIR/dkms-install.sh" --confirm-live
assert_eq "exit code 0" "$LAST_RC" "0"
assert_grep "dkms-installed" "OUTCOME=dkms-installed" "$CASE_DIR/stdout"
assert_eq "staged dkms.conf exists" "$( [[ -f "$XPS17_ROOT/usr/src/snd-soc-rt1316-sdw-xps17-7.2.15.2/dkms.conf" ]] && echo yes || echo no )" "yes"
assert_eq "staged policy header exists" "$( [[ -f "$XPS17_ROOT/usr/src/snd-soc-rt1316-sdw-xps17-7.2.15.2/rt1316-xps17-policy.h" ]] && echo yes || echo no )" "yes"
assert_grep "dkms add invoked" "^dkms add -m snd-soc-rt1316-sdw-xps17 -v 7.2.15.2$" "$MOCK_STATE/mutations.log"
assert_grep "dkms build invoked" "^dkms build -m snd-soc-rt1316-sdw-xps17 -v 7.2.15.2 -k 7.2.15-1-omarchy$" "$MOCK_STATE/mutations.log"
assert_grep "dkms install invoked" "^dkms install -m snd-soc-rt1316-sdw-xps17 -v 7.2.15.2 -k 7.2.15-1-omarchy$" "$MOCK_STATE/mutations.log"
assert_grep "depmod invoked" "^depmod -a 7.2.15-1-omarchy$" "$MOCK_STATE/mutations.log"
finish_case

# 16. dkms-install.sh rejects bad dkms.conf (missing BUILD_EXCLUSIVE_KERNEL)
setup_case "dkms-install-bad-conf"
grep -v '^BUILD_EXCLUSIVE_KERNEL=' "$XPS17_DRIVER_DIR/dkms.conf" > "$XPS17_DRIVER_DIR/dkms.conf.tmp"
mv "$XPS17_DRIVER_DIR/dkms.conf.tmp" "$XPS17_DRIVER_DIR/dkms.conf"
run_mock "$SCRIPTS_DIR/dkms-install.sh" --confirm-live
assert_eq "exit code 2" "$LAST_RC" "2"
assert_grep "invalid-dkms-conf" "OUTCOME=refused reason=invalid-dkms-conf" "$CASE_DIR/stdout"
assert_zero_mutations
finish_case

# 17. dkms-install.sh refuses unsafe XU24=true saved in asound.state
setup_case "dkms-install-xu24-unsafe"
sed -i 's/value false/value true/' "$XPS17_ROOT/var/lib/alsa/asound.state"
run_mock "$SCRIPTS_DIR/dkms-install.sh" --confirm-live
assert_eq "exit code 2" "$LAST_RC" "2"
assert_grep "xu24-saved-on" "OUTCOME=refused reason=xu24-saved-on" "$CASE_DIR/stdout"
assert_zero_mutations
finish_case

# 18. dkms-install.sh refuses thermal >= 82C
setup_case "dkms-install-thermal-hot"
echo "84000" > "$XPS17_ROOT/sys/class/hwmon/hwmon0/temp1_input"
run_mock "$SCRIPTS_DIR/dkms-install.sh" --confirm-live
assert_eq "exit code 2" "$LAST_RC" "2"
assert_grep "thermal-hot" "OUTCOME=refused reason=thermal-hot" "$CASE_DIR/stdout"
assert_zero_mutations
finish_case

# 19. route-timer-guard.sh recovers timer after simulated SIGKILL (and is idempotent)
setup_case "route-timer-guard-sigkill-recovery"
mkdir -p "$CASE_DIR/fake-run"
printf 'was=active\nstopping\n' > "$CASE_DIR/fake-run/route-timer"
echo "inactive" > "$MOCK_STATE/timer_state"
run_mock "$SCRIPTS_DIR/route-timer-guard.sh" "$CASE_DIR/fake-run"
assert_eq "guard exit 0" "$LAST_RC" "0"
assert_eq "timer restored to active" "$(cat "$MOCK_STATE/timer_state")" "active"
assert_eq "one start call" "$(tr '\n' ',' < "$MOCK_STATE/timer_ops.log")" "start,"
# Second call must be a no-op (already restored)
run_mock "$SCRIPTS_DIR/route-timer-guard.sh" "$CASE_DIR/fake-run"
assert_eq "guard second call exit 0" "$LAST_RC" "0"
assert_eq "still only one start call" "$(tr '\n' ',' < "$MOCK_STATE/timer_ops.log")" "start,"
finish_case

# 20. Concurrent run lock (flock) refuses second swap-in
setup_case "concurrent-lock-refused"
(
    exec 9>"$XPS17_RUNS_DIR/.lock"
    flock -x 9
    run_mock "$SCRIPTS_DIR/swap-in.sh" --confirm-live
    echo "$LAST_RC" > "$CASE_DIR/lock_rc"
)
LAST_RC="$(cat "$CASE_DIR/lock_rc")"
assert_eq "exit code 2" "$LAST_RC" "2"
assert_grep "another-run" "OUTCOME=refused reason=another-run" "$CASE_DIR/stdout"
assert_zero_mutations
finish_case

# 21. swap-out.sh detects srcversion mismatch (custom driver remained loaded without extra params)
setup_case "swap-out-detects-srcversion-mismatch"
export MOCK_WRONG_SRCV=1
run_mock "$SCRIPTS_DIR/swap-out.sh" --confirm-live
assert_eq "exit code 4" "$LAST_RC" "4"
assert_grep "srcversion-mismatch" "OUTCOME=swap-out-failed reason=srcversion-mismatch" "$CASE_DIR/stdout"
assert_eq "timer still restored via trap" "$(cat "$MOCK_STATE/timer_state")" "active"
finish_case

# 22. dkms-install.sh with real PACKAGE_NAME=snd-soc-rt1316-sdw + PRE_INSTALL=dkms-pre-install.sh
setup_case "dkms-install-real-pkg-and-preinstall"
cat > "$XPS17_DRIVER_DIR/dkms.conf" <<EOF
PACKAGE_NAME="snd-soc-rt1316-sdw"
PACKAGE_VERSION="7.2.15.2"
BUILT_MODULE_NAME[0]="snd-soc-rt1316-sdw"
DEST_MODULE_LOCATION[0]="/updates/dkms"
AUTOINSTALL="yes"
PRE_INSTALL="dkms-pre-install.sh"
BUILD_EXCLUSIVE_KERNEL="^7\.2\.15-1-omarchy$"
EOF
printf '#!/bin/sh\nexit 0\n' > "$XPS17_DRIVER_DIR/dkms-pre-install.sh"
chmod +x "$XPS17_DRIVER_DIR/dkms-pre-install.sh"
rm -f "$XPS17_DRIVER_DIR/SHA256SUMS"
run_mock "$SCRIPTS_DIR/dkms-install.sh" --confirm-live
assert_eq "exit code 0" "$LAST_RC" "0"
assert_grep "dkms-installed" "OUTCOME=dkms-installed" "$CASE_DIR/stdout"
assert_eq "staged PRE_INSTALL executable" "$( [[ -x "$XPS17_ROOT/usr/src/snd-soc-rt1316-sdw-7.2.15.2/dkms-pre-install.sh" ]] && echo yes || echo no )" "yes"
assert_grep "dkms add snd-soc-rt1316-sdw" "^dkms add -m snd-soc-rt1316-sdw -v 7.2.15.2$" "$MOCK_STATE/mutations.log"
finish_case

# 23. dkms-install.sh refuses missing PRE_INSTALL script declared in dkms.conf
setup_case "dkms-install-missing-preinstall"
cat > "$XPS17_DRIVER_DIR/dkms.conf" <<EOF
PACKAGE_NAME="snd-soc-rt1316-sdw"
PACKAGE_VERSION="7.2.15.2"
BUILT_MODULE_NAME[0]="snd-soc-rt1316-sdw"
DEST_MODULE_LOCATION[0]="/updates/dkms"
AUTOINSTALL="yes"
PRE_INSTALL="dkms-pre-install.sh"
BUILD_EXCLUSIVE_KERNEL="^7\.2\.15-1-omarchy$"
EOF
rm -f "$XPS17_DRIVER_DIR/dkms-pre-install.sh" "$XPS17_DRIVER_DIR/SHA256SUMS"
run_mock "$SCRIPTS_DIR/dkms-install.sh" --confirm-live
assert_eq "exit code 2" "$LAST_RC" "2"
assert_grep "missing-pre-install" "OUTCOME=refused reason=missing-pre-install" "$CASE_DIR/stdout"
assert_zero_mutations
finish_case

echo "=== Mock-Harness Summary: $PASS passed, $FAIL failed ==="
if ((FAIL > 0)); then
    echo "Failed cases: ${FAIL_LIST[*]}" >&2
    exit 1
fi
exit 0
