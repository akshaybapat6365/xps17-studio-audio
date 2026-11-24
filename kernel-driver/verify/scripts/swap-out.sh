#!/usr/bin/env bash
# Atomic rollback script to restore the stock kernel snd-soc-rt1316-sdw module
# on Dell XPS 17 9720 (DMI SKU 0AFF).
#
# Usage:
#   swap-out.sh --dry-run
#   swap-out.sh --confirm-live
#
# Guarantees:
#   - Refuses without --dry-run or --confirm-live (exit 2).
#   - Refuses on any machine whose DMI product_sku != 0AFF (exit 2).
#   - Pauses omarchy-audio-route-recovery.timer if active and ALWAYS restores on EXIT.
#   - Unbinds sof_sdw, unloads custom module, reloads stock kernel module (which
#     leaves Class-D HV gain at hardware power-on safe default 0xC60F=0x01),
#     rebinds sof_sdw, verifies stock loaded & amps attached.
set -uo pipefail

SCRIPT_NAME="swap-out"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

usage() {
	cat <<EOF
Usage: $0 (--dry-run | --confirm-live)

Safety gates:
  --dry-run       Validate DMI SKU, kernel, stock module, and print rollback plan (no changes).
  --confirm-live  Atomically restore stock kernel snd-soc-rt1316-sdw module and verify safe state.
EOF
	exit 2
}

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

# Gate 1 & 2: Mode gate + DMI hardware identity gate BEFORE any mutation.
enforce_mode_gate
check_dmi_sku

# Gate 3: Kernel release & stock module presence.
check_kernel_release
STOCK_KO="$(find_stock_ko)" || refuse "no-stock-module" "cannot locate stock ${MOD_KO_BASE}.ko* in kernel tree or DKMS archive"
check_ko_vermagic "$STOCK_KO"
say "stock module confirmed: $STOCK_KO"

# Gate 4: Exclusive lock.
init_run_lock "swap-out"

FINISHED_OK=0

on_exit() {
	local rc=$?
	local WAIT_END=""
	((LINKS_PINNED)) && unpin_links
	route_timer_restore
	if ((FINISHED_OK)); then
		exit 0
	fi
	printf 'OUTCOME=swap-out-failed reason=%s\n' "${REASON:-exit-$rc}"
	exit "${rc:-4}"
}

trap on_exit EXIT
trap 'warn "signal received during swap-out; finishing stock restore"' INT TERM

route_timer_pause || exit 2

if ((DRY_RUN)); then
	say "=== DRY-RUN ROLLBACK PLAN (zero system changes) ==="
	say "Step 1/6: pin SoundWire links power/control=on"
	pin_links
	say "Step 2/6: unbind $MACHINE_DEV (if bound)"
	sysfs_write "$MACHINE_DRV/unbind" "$MACHINE_DEV"
	say "Step 3/6: rmmod $MOD (if loaded)"
	run_priv "$TMO_MODULE" rmmod "$MOD"
	say "Step 4/6: reload stock module ($STOCK_KO via modprobe/insmod -> power-on safe 0xC60F=0x01)"
	if [[ "$STOCK_KO" == *.ko ]]; then
		run_priv "$TMO_MODULE" insmod "$STOCK_KO"
	else
		run_priv "$TMO_MODULE" modprobe "$MOD_KO_BASE"
	fi
	say "Step 5/6: bind $MACHINE_DEV"
	sysfs_write "$MACHINE_DRV/bind" "$MACHINE_DEV"
	say "Step 6/6: verify stock module loaded, amps attached, unpin links, restore $ROUTE_TIMER"
	FINISHED_OK=1
	printf 'OUTCOME=dry-run-ok\n'
	exit 0
fi

say "Step 1/6: pin SoundWire links awake"
pin_links || exit 4

say "Step 2/6: unbind machine driver $MACHINE_DEV (if bound)"
if card_bound; then
	sysfs_write "$MACHINE_DRV/unbind" "$MACHINE_DEV" || { fail "unbind-failed" "failed to unbind $MACHINE_DEV"; exit 4; }
	wait_for 15 "card $CARD absent" card_absent || { fail "card-still-present" "card did not disappear"; exit 4; }
else
	say "  $MACHINE_DEV already unbound"
fi

say "Step 3/6: unload custom $MOD (if loaded)"
if module_loaded; then
	wait_for 10 "$MOD refcnt == 0" refcnt_zero || { fail "module-busy" "$MOD refcnt != 0"; exit 4; }
	run_priv "$TMO_MODULE" rmmod "$MOD" || { fail "rmmod-failed" "rmmod $MOD failed"; exit 4; }
else
	say "  $MOD not currently loaded"
fi

say "Step 4/6: load stock kernel module ($STOCK_KO)"
if [[ "$STOCK_KO" == *.ko ]]; then
	run_priv "$TMO_MODULE" insmod "$STOCK_KO" || { fail "stock-insmod-failed" "insmod $STOCK_KO failed"; exit 4; }
else
	run_priv "$TMO_MODULE" modprobe "$MOD_KO_BASE" || { fail "stock-modprobe-failed" "modprobe $MOD_KO_BASE failed"; exit 4; }
fi
wait_for 10 "both amps bound to stock rt1316-sdca" amps_bound_and_attached || {
	fail "amps-missing" "amps did not attach to stock driver"
	exit 4
}

say "Step 5/6: rebind machine driver $MACHINE_DEV"
if ! sysfs_write "$MACHINE_DRV/bind" "$MACHINE_DEV"; then
	warn "bind returned error; waiting 10s for deferred probe"
	wait_for 10 "card bound" card_bound || { fail "bind-failed" "failed to bind $MACHINE_DEV"; exit 4; }
fi

say "Step 6/6: verify stock driver loaded and card active (safe baseline 0xC60F=0x01)"
wait_for 15 "card $CARD present" card_present || { fail "card-missing" "card $CARD not present after rollback"; exit 4; }
module_loaded || { fail "module-missing" "$MOD not loaded after rollback"; exit 4; }
if [[ -r "$MOD_SYS/parameters/xps17_guard_state" ]]; then
	fail "custom-still-loaded" "xps17 custom parameter still visible; stock module did not replace it"
	exit 4
fi
stock_srcv="$(tq modinfo -F srcversion "$STOCK_KO" 2>/dev/null || true)"
loaded_srcv="$(cat "$MOD_SYS/srcversion" 2>/dev/null || true)"
if [[ -n "$stock_srcv" && -n "$loaded_srcv" && "$loaded_srcv" != "$stock_srcv" ]]; then
	fail "srcversion-mismatch" "loaded srcversion '$loaded_srcv' != stock '$stock_srcv'"
	exit 4
fi
unpin_links

FINISHED_OK=1
say "stock module restored cleanly (hardware Class-D HV gain at safe default 0xC60F=0x01)"
printf 'OUTCOME=stock-restored\n'
exit 0
