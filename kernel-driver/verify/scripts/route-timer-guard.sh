#!/usr/bin/env bash
# Emergency ExecStopPost guard for swap units.
# If a swap script recorded pausing omarchy-audio-route-recovery.timer and died
# before restoring it (e.g., SIGKILL), this guard restores the timer.
set -uo pipefail

SCRIPT_NAME="route-timer-guard"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

run_dir="${1:-}"
[[ -n "$run_dir" ]] || { err "usage: route-timer-guard.sh <run-dir>"; exit 2; }
rec="$run_dir/route-timer"

[[ -f "$rec" ]] || exit 0
grep -qx 'was=active' "$rec" && grep -qx 'stopping' "$rec" || exit 0
grep -qx 'restored' "$rec" && exit 0

say "run ended without restoring $ROUTE_TIMER; starting it now"
if tq systemctl --user start "$ROUTE_TIMER"; then
	printf 'restored\n' >> "$rec" 2>/dev/null || true
	say "$ROUTE_TIMER restored by emergency guard"
	exit 0
else
	err "failed to restore $ROUTE_TIMER; run manually: systemctl --user start $ROUTE_TIMER"
	exit 1
fi
