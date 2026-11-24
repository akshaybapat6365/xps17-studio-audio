# shellcheck shell=bash
# Shared safety library for staged Dell XPS 17 9720 (0AFF) rt1316-sdw scripts.
#
# STRICTLY STAGED: never modifies live hardware unless invoked explicitly with
# --confirm-live AND running on verified DMI product_sku=0AFF.
#
# Test isolation variables (used ONLY by test_scripts_mock.sh):
#   XPS17_ROOT        Fake filesystem root prefix (default: empty = live root)
#   SYS_DMI_SKU       Override path to DMI product_sku file
#   XPS17_DRIVER_DIR  Override path to built driver/source directory
#   XPS17_RUNS_DIR    Override path to per-run state/lock/log directory
#   XPS17_KVER        Override expected kernel release (default: 7.2.15-1-omarchy)
#   XPS17_TMO_*       Override timeouts in seconds for fast offline tests
#   XPS17_POLL_SLEEP  Override wait_for poll sleep interval (default: 0.25)

R="${XPS17_ROOT:-}"
VERIFY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS_DIR="$VERIFY_DIR/scripts"
DRIVER_DIR="${XPS17_DRIVER_DIR:-$(cd "$VERIFY_DIR/.." && pwd)/rt1316-xps17}"
RUNS_DIR="${XPS17_RUNS_DIR:-$VERIFY_DIR/.runs}"

EXPECTED_DMI_SKU="0AFF"
DMI_SKU_PATH="${SYS_DMI_SKU:-${R}/sys/class/dmi/id/product_sku}"

KVER="${XPS17_KVER:-7.2.15-1-omarchy}"
MOD="snd_soc_rt1316_sdw"
MOD_KO_BASE="snd-soc-rt1316-sdw"
MOD_SYS="$R/sys/module/$MOD"

DKMS_PKG_NAME="snd-soc-rt1316-sdw-xps17"
DKMS_PKG_VER="7.2.15.2"
DKMS_USR_SRC="$R/usr/src/${DKMS_PKG_NAME}-${DKMS_PKG_VER}"
DKMS_TREE="$R/var/lib/dkms"

CARD="sofsoundwire"
CARD_DIR="$R/proc/asound/$CARD"
MACHINE_DRV="$R/sys/bus/platform/drivers/sof_sdw"
MACHINE_DEV="sof_sdw"
SDW_DEVICES="$R/sys/bus/soundwire/devices"
AMPS=(sdw:0:1:025d:1316:01 sdw:0:2:025d:1316:01)
ALSA_STATE_FILE="$R/var/lib/alsa/asound.state"

ROUTE_TIMER="omarchy-audio-route-recovery.timer"
ROUTE_SERVICE="omarchy-audio-route-recovery.service"

MAX_TEMP_C="${XPS17_MAX_TEMP_C:-82}"

TMO_QUERY="${XPS17_TMO_QUERY:-10}"
TMO_MODULE="${XPS17_TMO_MODULE:-30}"
TMO_SYSFS="${XPS17_TMO_SYSFS:-30}"
TMO_KILL="${XPS17_TMO_KILL:-5}"
POLL_SLEEP="${XPS17_POLL_SLEEP:-0.25}"

MODE=""          # "dry-run" or "live"
DRY_RUN=0
RUN_DIR=""
REASON=""
ROUTE_TIMER_STOPPED=0
ROUTE_TIMER_WAS=""
LINKS_PINNED=0
LINKS_NOT_RESTORED=0

# Ignore SIGPIPE so a closed console pipe cannot abort mid-swap or mid-rollback.
trap '' PIPE

say()  { printf '%s: %s\n' "${SCRIPT_NAME:-rt1316-xps17}" "$*"; }
warn() { printf '%s: WARNING: %s\n' "${SCRIPT_NAME:-rt1316-xps17}" "$*" >&2; }
err()  { printf '%s: ERROR: %s\n' "${SCRIPT_NAME:-rt1316-xps17}" "$*" >&2; }

fail() {
	local code="$1"; shift
	[[ -n "$REASON" ]] || REASON="$code"
	err "$*"
	return 1
}

refuse() {
	local code="$1"; shift
	REASON="$code"
	err "REFUSED ($code): $*"
	printf 'OUTCOME=refused reason=%s\n' "$code"
	exit 2
}

# Bounded query helper; clamps to remaining WAIT_END budget when inside wait_for.
tq() {
	local t="$TMO_QUERY" left
	if [[ -n "${WAIT_END:-}" ]]; then
		left=$((WAIT_END - SECONDS))
		((left < 1)) && left=1
		((left < t)) && t="$left"
	fi
	timeout -k "$TMO_KILL" "$t" "$@"
}

# Privileged command wrapper with mandatory hard timeout.
run_priv() {
	local limit="$1"; shift
	if ((DRY_RUN)); then
		say "  [dry-run] would run (priv, limit ${limit}s): $*"
		return 0
	fi
	say "  run (priv): $*"
	if [[ "$EUID" -eq 0 && "${XPS17_FORCE_SUDO:-0}" != "1" ]]; then
		timeout -k "$TMO_KILL" "$limit" "$@"
	else
		sudo -n timeout -k "$TMO_KILL" "$limit" "$@"
	fi
}

run_user() {
	if ((DRY_RUN)); then
		say "  [dry-run] would run (user, limit ${TMO_QUERY}s): $*"
		return 0
	fi
	say "  run (user): $*"
	tq "$@"
}

sysfs_write() {
	local path="$1" value="$2"
	if ((DRY_RUN)); then
		say "  [dry-run] would write '$value' -> $path"
		return 0
	fi
	say "  write '$value' -> $path"
	if [[ "$EUID" -eq 0 && "${XPS17_FORCE_SUDO:-0}" != "1" ]]; then
		printf '%s' "$value" | timeout -k "$TMO_KILL" "$TMO_SYSFS" tee "$path" >/dev/null
	else
		printf '%s' "$value" | sudo -n timeout -k "$TMO_KILL" "$TMO_SYSFS" tee "$path" >/dev/null
	fi
}

# Poll predicate until true or timeout (both wall-clock and iteration capped).
wait_for() {
	local secs="$1" what="$2"
	shift 2
	if ((DRY_RUN)); then
		say "  [dry-run] would wait up to ${secs}s for: $what"
		return 0
	fi
	local n=$((secs * 4))
	local WAIT_END=$((SECONDS + secs))
	while :; do
		"$@" && return 0
		((n-- > 0 && SECONDS < WAIT_END)) || break
		sleep "$POLL_SLEEP"
	done
	warn "timed out after ${secs}s waiting for: $what"
	return 1
}

# ------------------------------------------------------------------ DMI Guard
# Hard hardware identity gate: refuses to touch anything unless product_sku==0AFF.
check_dmi_sku() {
	local sku=""
	if [[ ! -r "$DMI_SKU_PATH" ]]; then
		refuse "dmi-unreadable" "cannot read DMI product_sku at $DMI_SKU_PATH (expected $EXPECTED_DMI_SKU)"
	fi
	sku="$(tr -d ' \t\r\n' < "$DMI_SKU_PATH" 2>/dev/null || true)"
	if [[ "$sku" != "$EXPECTED_DMI_SKU" ]]; then
		refuse "wrong-dmi-sku" "DMI product_sku='$sku' != '$EXPECTED_DMI_SKU' (Dell XPS 17 9720 Stradale required)"
	fi
	say "DMI guard verified: product_sku=$sku ($DMI_SKU_PATH)"
}

# ------------------------------------------------------------------ Mode Gate
# Require explicit --dry-run OR --confirm-live so accidental calls cannot mutate.
enforce_mode_gate() {
	if [[ -z "$MODE" ]]; then
		refuse "no-confirm-live" "refusing to run without explicit --dry-run or --confirm-live"
	fi
	if [[ "$MODE" == "dry-run" ]]; then
		DRY_RUN=1
	else
		DRY_RUN=0
	fi
}

# ------------------------------------------------------------------ Run Lock
init_run_lock() {
	local label="$1"
	mkdir -p "$RUNS_DIR" || refuse "run-dir-failed" "cannot create $RUNS_DIR"
	exec 9>>"$RUNS_DIR/.lock" || refuse "lock-open-failed" "cannot open $RUNS_DIR/.lock"
	flock -n 9 || refuse "another-run" "another swap/install holds $RUNS_DIR/.lock"
	RUN_DIR="$RUNS_DIR/$(date +%Y%m%d-%H%M%S)-$$-${label}"
	mkdir -p "$RUN_DIR" || refuse "run-dir-failed" "cannot create $RUN_DIR"
	ln -sfn "$RUN_DIR" "$RUNS_DIR/last"
}

# ------------------------------------------------------------------ Kernel & Stock KO
check_kernel_release() {
	local cur
	cur="$(uname -r 2>/dev/null || true)"
	if [[ "$cur" != "$KVER" ]]; then
		refuse "wrong-kernel" "running kernel '$cur' != expected '$KVER'"
	fi
	say "kernel release verified: $cur"
}

find_stock_ko() {
	local cand
	for cand in \
		"$R/lib/modules/$KVER/kernel/sound/soc/codecs/${MOD_KO_BASE}.ko.zst" \
		"$R/lib/modules/$KVER/kernel/sound/soc/codecs/${MOD_KO_BASE}.ko.xz" \
		"$R/lib/modules/$KVER/kernel/sound/soc/codecs/${MOD_KO_BASE}.ko" \
		"$R/usr/lib/modules/$KVER/kernel/sound/soc/codecs/${MOD_KO_BASE}.ko.zst" \
		"$R/usr/lib/modules/$KVER/kernel/sound/soc/codecs/${MOD_KO_BASE}.ko.xz" \
		"$R/usr/lib/modules/$KVER/kernel/sound/soc/codecs/${MOD_KO_BASE}.ko" \
		"$DKMS_TREE"/*/original_module/"$KVER"/*/"${MOD_KO_BASE}".ko*; do
		if [[ -f "$cand" ]]; then
			printf '%s\n' "$cand"
			return 0
		fi
	done
	return 1
}

check_ko_vermagic() {
	local ko="$1"
	local vm
	[[ -f "$ko" ]] || refuse "missing-module" "module file not found: $ko"
	vm="$(tq modinfo -F vermagic "$ko" 2>/dev/null || true)"
	if [[ "$vm" != "$KVER "* && "$vm" != "$KVER" ]]; then
		refuse "vermagic-mismatch" "vermagic of $ko is '$vm', expected prefix '$KVER'"
	fi
	say "vermagic verified: $vm"
}

# ------------------------------------------------------------------ Thermal Check
package_temp_c() {
	local h l
	local hwmon_base="${XPS17_HWMON_DIR:-$R/sys/class/hwmon}"
	for h in "$hwmon_base"/hwmon*; do
		[[ -r "$h/name" ]] || continue
		[[ "$(cat "$h/name" 2>/dev/null)" == coretemp ]] || continue
		for l in "$h"/temp*_label; do
			[[ -r "$l" ]] || continue
			if [[ "$(cat "$l" 2>/dev/null)" == "Package id 0" ]]; then
				local inp="${l%_label}_input"
				if [[ -r "$inp" ]]; then
					echo $(($(cat "$inp") / 1000))
					return 0
				fi
			fi
		done
	done
	return 1
}

check_thermal_ceiling() {
	local t
	if t="$(package_temp_c)"; then
		if ((t >= MAX_TEMP_C)); then
			refuse "thermal-hot" "CPU package ${t}C >= ceiling ${MAX_TEMP_C}C; wait until cool (< 78C)"
		fi
		say "thermal guard verified: Package id 0 = ${t}C (< ${MAX_TEMP_C}C)"
	else
		warn "could not read coretemp Package id 0; proceeding cautiously"
	fi
}

# ------------------------------------------------------------------ Route Recovery Timer
route_service_idle() {
	local st
	st="$(tq systemctl --user is-active "$ROUTE_SERVICE" 2>/dev/null || true)"
	[[ "$st" == "inactive" || "$st" == "failed" || -z "$st" ]]
}

route_timer_pause() {
	local was
	was="$(tq systemctl --user is-active "$ROUTE_TIMER" 2>/dev/null || true)"
	# systemctl is-active prints "inactive"/"failed"/"unknown" with non-zero rc
	[[ -n "$was" ]] || was="unknown"
	ROUTE_TIMER_WAS="$was"
	if [[ -n "$RUN_DIR" ]]; then
		printf 'was=%s\n' "$was" > "$RUN_DIR/route-timer" 2>/dev/null || true
	fi
	if [[ "$was" != "active" ]]; then
		say "route timer $ROUTE_TIMER is '$was' before run; leaving untouched"
		return 0
	fi
	if ((DRY_RUN)); then
		say "  [dry-run] would stop $ROUTE_TIMER (currently active), wait for $ROUTE_SERVICE idle, and restore on EXIT trap"
		return 0
	fi
	if [[ -n "$RUN_DIR" ]]; then
		printf 'stopping\n' >> "$RUN_DIR/route-timer" 2>/dev/null || true
	fi
	ROUTE_TIMER_STOPPED=1
	say "pausing active $ROUTE_TIMER before driver swap (guaranteed EXIT trap restore)"
	run_user systemctl --user stop "$ROUTE_TIMER" || {
		fail "route-timer-stop" "failed to stop $ROUTE_TIMER"
		return 1
	}
	wait_for 30 "$ROUTE_SERVICE idle" route_service_idle || {
		fail "route-service-busy" "$ROUTE_SERVICE still active after stopping timer"
		return 1
	}
	return 0
}

route_timer_restore() {
	# Clear WAIT_END so an interrupted wait_for never truncates timer restore!
	local WAIT_END=""
	((ROUTE_TIMER_STOPPED)) || return 0
	ROUTE_TIMER_STOPPED=0
	say "restoring $ROUTE_TIMER (was active before run)"
	if run_user systemctl --user start "$ROUTE_TIMER"; then
		if [[ -n "$RUN_DIR" ]]; then
			printf 'restored\n' >> "$RUN_DIR/route-timer" 2>/dev/null || true
		fi
		say "$ROUTE_TIMER restored"
	else
		warn "failed to restore $ROUTE_TIMER — run: systemctl --user start $ROUTE_TIMER"
	fi
	return 0
}

# ------------------------------------------------------------------ Hardware / Sysfs Helpers
module_loaded() { [[ -d "$MOD_SYS" ]]; }
module_refcnt() { cat "$MOD_SYS/refcnt" 2>/dev/null || echo 0; }
refcnt_zero()   { [[ "$(module_refcnt)" == "0" ]]; }

card_bound()    { [[ -e "$MACHINE_DRV/$MACHINE_DEV" ]]; }
card_present()  { [[ -d "$CARD_DIR" ]]; }
card_absent()   { ! card_present; }

amp_status()    { cat "$SDW_DEVICES/$1/status" 2>/dev/null || echo missing; }
amp_driver()    { basename "$(readlink "$SDW_DEVICES/$1/driver" 2>/dev/null)" 2>/dev/null || echo none; }

amps_bound_and_attached() {
	local a
	for a in "${AMPS[@]}"; do
		[[ "$(amp_status "$a")" == "Attached" ]] || return 1
		[[ "$(amp_driver "$a")" == "rt1316-sdca" ]] || return 1
	done
	return 0
}

amp_link_dev() {
	local p="$SDW_DEVICES/$1"
	[[ -e "$p" ]] || return 1
	dirname "$(dirname "$(readlink -f "$p")")"
}

pin_links() {
	local a l seen=" " cur
	for a in "${AMPS[@]}"; do
		l="$(amp_link_dev "$a" 2>/dev/null)" || continue
		[[ -f "$l/power/control" ]] || continue
		[[ "$seen" == *" $l "* ]] && continue
		seen+="$l "
		cur="$(cat "$l/power/control" 2>/dev/null || echo auto)"
		if [[ -n "$RUN_DIR" ]] && ! grep -qs "^$l " "$RUN_DIR/links.saved" 2>/dev/null; then
			printf '%s %s\n' "$l" "$cur" >> "$RUN_DIR/links.saved"
		fi
		sysfs_write "$l/power/control" "on" || {
			fail "link-pin" "failed to pin link ${l##*/} power/control=on"
			return 1
		}
	done
	LINKS_PINNED=1
	return 0
}

unpin_links() {
	local WAIT_END=""
	local l val
	[[ -n "$RUN_DIR" && -f "$RUN_DIR/links.saved" ]] || { LINKS_PINNED=0; return 0; }
	while read -r l val; do
		[[ -n "$l" && -n "$val" ]] || continue
		if ! sysfs_write "$l/power/control" "$val"; then
			warn "failed to restore ${l##*/}/power/control=$val (non-fatal)"
			LINKS_NOT_RESTORED=1
		fi
	done < "$RUN_DIR/links.saved"
	LINKS_PINNED=0
	return 0
}

# Check /var/lib/alsa/asound.state does not hold XU24 Bypass Switch = true
check_asound_state_xu24() {
	local f="$ALSA_STATE_FILE"
	[[ -r "$f" ]] || return 0
	python3 -I - "$f" <<'PY'
import re, sys
text = open(sys.argv[1], errors='replace').read()
blocks = re.findall(r"control\.\d+\s*\{[^}]*\}", text, flags=re.S)
bad = []
for b in blocks:
    m_name = re.search(r"name\s+'([^']*XU24 Bypass Switch[^']*)'", b)
    if m_name:
        m_val = re.search(r"value\s+(true|false|1|0)", b)
        val = m_val.group(1) if m_val else "unknown"
        if val in ("true", "1"):
            bad.append(f"{m_name.group(1)}={val}")
if bad:
    print("UNSAFE XU24 saved state: " + ", ".join(bad), file=sys.stderr)
    sys.exit(1)
sys.exit(0)
PY
}
