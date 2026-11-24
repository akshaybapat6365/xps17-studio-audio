#!/usr/bin/env bash
# Safe staged swap-in script for Dell XPS 17 9720 (DMI SKU 0AFF) rt1316-sdw driver.
#
# Usage:
#   swap-in.sh --dry-run [--ko <path>]
#   swap-in.sh --confirm-live [--ko <path>]
#
# Mandatory safety gates:
#   1. Explicit --dry-run OR --confirm-live (accidental bare call refuses exit 2).
#   2. DMI product_sku MUST equal 0AFF (refuses exit 2 on any other machine).
#   3. Running kernel release MUST match built module vermagic.
#   4. Stock module MUST exist for guaranteed automatic rollback.
#   5. omarchy-audio-route-recovery.timer is paused if active and ALWAYS restored
#      via EXIT trap (both normal exit and mid-swap rollback).
#   6. Mid-swap failure automatically rolls back to stock snd-soc-rt1316-sdw.
set -uo pipefail

SCRIPT_NAME="swap-in"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

usage() {
	cat <<EOF
Usage: $0 (--dry-run | --confirm-live) [--ko <path-to-ko>]

Safety gates:
  --dry-run       Validate DMI SKU, kernel, vermagic, stock module, and print exact plan (no changes).
  --confirm-live  Execute live swap with route-timer guard and automatic rollback on failure.
  --ko <path>     Override path to built snd-soc-rt1316-sdw.ko (default: $DRIVER_DIR/snd-soc-rt1316-sdw.ko).
EOF
	exit 2
}

KO_PATH="$DRIVER_DIR/${MOD_KO_BASE}.ko"
while (($# > 0)); do
	case "$1" in
	--dry-run)
		[[ -z "$MODE" ]] || refuse "conflicting-flags" "pass only one of --dry-run or --confirm-live"
		MODE="dry-run"
		;;
	--confirm-live)
		[[ -z "$MODE" ]] || refuse "conflicting-flags" "pass only one of --dry-run or --confirm-live"
		MODE="live"
		;;
	--ko)
		shift
		[[ $# -gt 0 ]] || usage
		KO_PATH="$1"
		;;
	-h|--help)
		usage
		;;
	*)
		err "unknown argument: $1"
		usage
		;;
	esac
	shift
done

# Gate 1 & 2: Mode gate + DMI hardware identity gate BEFORE any mutation or lock.
enforce_mode_gate
check_dmi_sku

# Gate 3 & 4: Kernel release, target module vermagic, and stock rollback module.
check_kernel_release
check_ko_vermagic "$KO_PATH"
STOCK_KO="$(find_stock_ko)" || refuse "no-stock-module" "cannot locate stock ${MOD_KO_BASE}.ko* for rollback"
say "stock rollback module verified: $STOCK_KO"

# Gate 5: Acquire exclusive run lock (prevents overlapping swaps).
init_run_lock "swap-in"

ARMED=0
SWAPPED_OUT_STOCK=0
FINISHED_OK=0

rollback_to_stock() {
	local WAIT_END=""
	local why="${REASON:-swap-failed}"
	trap 'warn "signal received during rollback; finishing rollback first"' INT TERM HUP
	warn "automatic rollback to stock module triggered (reason=$why)"

	if card_bound; then
		sysfs_write "$MACHINE_DRV/unbind" "$MACHINE_DEV" || warn "rollback unbind returned error"
		wait_for 15 "card absent during rollback" card_absent || warn "card still present during rollback"
	fi

	if module_loaded; then
		wait_for 10 "$MOD refcnt == 0 (rollback)" refcnt_zero || warn "$MOD refcnt non-zero during rollback"
		run_priv "$TMO_MODULE" rmmod "$MOD" || warn "rollback rmmod $MOD failed"
	fi

	if [[ "$STOCK_KO" == *.ko ]]; then
		run_priv "$TMO_MODULE" insmod "$STOCK_KO" || run_priv "$TMO_MODULE" modprobe "$MOD_KO_BASE" || warn "rollback load stock failed"
	else
		run_priv "$TMO_MODULE" modprobe "$MOD_KO_BASE" || warn "rollback modprobe $MOD_KO_BASE failed"
	fi

	if ! card_bound; then
		sysfs_write "$MACHINE_DRV/bind" "$MACHINE_DEV" || {
			warn "rollback bind returned error; waiting 5s for deferred bind"
			wait_for 5 "card bound (rollback)" card_bound || warn "card failed to bind during rollback"
		}
	fi
	unpin_links
	say "rollback to stock module completed"
}

on_exit() {
	local rc=$?
	local WAIT_END=""
	if ((ARMED && !FINISHED_OK)); then
		ARMED=0
		rollback_to_stock
		route_timer_restore
		printf 'OUTCOME=rolled-back-stock reason=%s\n' "${REASON:-exit-$rc}"
		exit 3
	fi
	((LINKS_PINNED)) && unpin_links
	route_timer_restore
	if ((FINISHED_OK)); then
		exit 0
	fi
	exit "$rc"
}

on_signal() {
	[[ -n "$REASON" ]] || REASON="signal-$1"
	warn "received signal $1"
	exit 130
}

trap on_exit EXIT
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM

# Step 0: Pause omarchy-audio-route-recovery.timer if active (dry-run only prints).
route_timer_pause || exit 2

if ((DRY_RUN)); then
	say "=== DRY-RUN PLAN (zero system changes) ==="
	say "Step 1/7: pin SoundWire links power/control=on"
	pin_links
	say "Step 2/7: unbind $MACHINE_DEV via $MACHINE_DRV/unbind"
	sysfs_write "$MACHINE_DRV/unbind" "$MACHINE_DEV"
	say "Step 3/7: wait for $MOD refcnt==0, then rmmod $MOD"
	run_priv "$TMO_MODULE" rmmod "$MOD"
	say "Step 4/7: insmod $KO_PATH (deadman gate starts LOCKED: 0xC60F=0x01, 0xD002/0xD004=0xBF)"
	run_priv "$TMO_MODULE" insmod "$KO_PATH"
	say "Step 5/7: bind $MACHINE_DEV via $MACHINE_DRV/bind"
	sysfs_write "$MACHINE_DRV/bind" "$MACHINE_DEV"
	say "Step 6/7: verify both SoundWire amps attached & card ready"
	say "  [dry-run] would verify amps (${AMPS[*]}) and card ($CARD)"
	say "Step 7/7: unpin SoundWire links & restore $ROUTE_TIMER on EXIT"
	FINISHED_OK=1
	printf 'OUTCOME=dry-run-ok\n'
	exit 0
fi

# Live execution (--confirm-live)
ARMED=1

say "Step 1/7: pin SoundWire links awake"
pin_links || exit 1

say "Step 2/7: unbind machine driver $MACHINE_DEV"
if card_bound; then
	sysfs_write "$MACHINE_DRV/unbind" "$MACHINE_DEV" || { fail "unbind-failed" "unbind $MACHINE_DEV failed"; exit 1; }
	wait_for 15 "card $CARD absent" card_absent || { fail "card-still-present" "card did not vanish after unbind"; exit 1; }
else
	say "  $MACHINE_DEV already unbound"
fi

say "Step 3/7: unload stock $MOD"
if module_loaded; then
	wait_for 10 "$MOD refcnt == 0" refcnt_zero || { fail "module-busy" "$MOD refcnt != 0"; exit 1; }
	run_priv "$TMO_MODULE" rmmod "$MOD" || { fail "rmmod-failed" "rmmod $MOD failed"; exit 1; }
	SWAPPED_OUT_STOCK=1
else
	say "  $MOD not currently loaded"
fi

say "Step 4/7: load staged driver $KO_PATH"
run_priv "$TMO_MODULE" insmod "$KO_PATH" || { fail "insmod-failed" "insmod $KO_PATH failed"; exit 1; }
wait_for 10 "both rt1316 amps bound and attached" amps_bound_and_attached || {
	fail "amps-missing" "amplifiers failed to bind/attach after insmod"
	exit 1
}

say "Step 5/7: rebind machine driver $MACHINE_DEV"
if ! sysfs_write "$MACHINE_DRV/bind" "$MACHINE_DEV"; then
	warn "bind returned error (possible -EPROBE_DEFER); waiting up to 10s"
	wait_for 10 "card bound" card_bound || { fail "bind-failed" "failed to rebind $MACHINE_DEV"; exit 1; }
fi

say "Step 6/7: verify card present and module loaded"
wait_for 15 "card $CARD present" card_present || { fail "card-missing" "card $CARD did not reappear"; exit 1; }
module_loaded || { fail "module-missing" "$MOD not visible in sysfs"; exit 1; }

say "Step 7/7: unpin SoundWire links (non-fatal on warning)"
unpin_links

ARMED=0
FINISHED_OK=1
say "swap-in complete (P4 deadman gate remains LOCKED at 0xC60F=0x01 until userspace magic heartbeat)"
printf 'OUTCOME=swap-in-ok\n'
exit 0
