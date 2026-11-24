#!/usr/bin/env bash
# Adversarial mutation tests: deliberately inject single-point safety faults into
# scratch copies of scripts and mock driver sources, and prove our verifiers
# CATCH every single fault (100% mutant kill rate).
#
# Operates purely on temporary scratch copies in /tmp; NEVER touches originals
# or the live system.
set -uo pipefail

VERIFY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SANDBOX="$(mktemp -d /tmp/xps17-mutants.XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT

CAUGHT=0
MISSED=0
MISSED_LIST=()

# 1. Script mutants: break one safety guard in a scratch copy of verify/ and prove test_scripts_mock.sh FAILS
run_script_mutant() {
	local name="$1" rel_file="$2" old="$3" new="$4"
	local mdir="$SANDBOX/script-$name"
	mkdir -p "$mdir"
	cp -a "$VERIFY_DIR/scripts" "$VERIFY_DIR/test_scripts_mock.sh" "$mdir/"
	python3 -I - "$mdir/$rel_file" "$old" "$new" <<'PY' || { echo "  [ERR]  mutant $name failed to apply"; MISSED=$((MISSED + 1)); MISSED_LIST+=("$name"); return; }
import sys
p, old, new = sys.argv[1:4]
txt = open(p).read()
if txt.count(old) != 1:
    sys.exit(f"pattern matched {txt.count(old)} times in {p}")
open(p, "w").write(txt.replace(old, new, 1))
PY
	if "$mdir/test_scripts_mock.sh" >"$mdir/out.log" 2>&1; then
		MISSED=$((MISSED + 1))
		MISSED_LIST+=("$name")
		printf '  [MISS] script mutant %-30s NOT caught!\n' "$name"
	else
		CAUGHT=$((CAUGHT + 1))
		local caught_by
		caught_by="$(grep '\[FAIL\]' "$mdir/out.log" | awk '{print $2}' | tr '\n' ' ')"
		printf '  [KILL] script mutant %-30s caught by: %s\n' "$name" "$caught_by"
	fi
}

# 2. Static-auditor mutants: inject forbidden reg writes / debugfs refs into a scratch driver copy and prove verify_ko_elf.sh --static-only FAILS
run_static_mutant() {
	local name="$1" injected_line="$2"
	local mdir="$SANDBOX/static-$name"
	mkdir -p "$mdir"
	cat > "$mdir/rt1316-sdw.c" <<EOF
#include "rt1316-xps17-policy.h"
static const struct reg_sequence rt1316_blind_write[] = {
	{ 0xc60e, 0x05 },
	{ 0xc102, 0x00 },
	{ 0xc050, 0x83 },
};
static void dummy(void *regmap) {
	if (rt1316_xps17_reg_is_forbidden(0xc60e)) return;
	$injected_line
}
EOF
	if "$VERIFY_DIR/verify_ko_elf.sh" --driver-dir "$mdir" --static-only >"$mdir/out.log" 2>&1; then
		MISSED=$((MISSED + 1))
		MISSED_LIST+=("$name")
		printf '  [MISS] static mutant %-30s NOT caught!\n' "$name"
	else
		CAUGHT=$((CAUGHT + 1))
		local reason
		reason="$(grep '\[FAIL\]' "$mdir/out.log" | head -n1 | sed 's/^[[:space:]]*//')"
		printf '  [KILL] static mutant %-30s caught: %s\n' "$name" "$reason"
	fi
}

echo "=== Running Adversarial Mutation Tests (Proving Verifier Sensitivity) ==="

run_script_mutant "drop-dmi-guard" \
	"scripts/lib.sh" \
	'if [[ "$sku" != "$EXPECTED_DMI_SKU" ]]; then' \
	'if false; then'

run_script_mutant "drop-confirm-live-gate" \
	"scripts/lib.sh" \
	'if [[ -z "$MODE" ]]; then' \
	'if false; then'

run_script_mutant "drop-route-timer-restore" \
	"scripts/lib.sh" \
	'run_user systemctl --user start "$ROUTE_TIMER"' \
	'true'

run_script_mutant "drop-mid-swap-rollback" \
	"scripts/swap-in.sh" \
	'if ((ARMED && !FINISHED_OK)); then' \
	'if false; then'

run_script_mutant "drop-vermagic-check" \
	"scripts/lib.sh" \
	'if [[ "$vm" != "$KVER "* && "$vm" != "$KVER" ]]; then' \
	'if false; then'

run_script_mutant "drop-xu24-dkms-gate" \
	"scripts/dkms-install.sh" \
	'check_asound_state_xu24 || refuse "xu24-saved-on"' \
	'true || refuse "xu24-saved-on"'

run_script_mutant "drop-thermal-dkms-gate" \
	"scripts/lib.sh" \
	'if ((t >= MAX_TEMP_C)); then' \
	'if false; then'

run_script_mutant "drop-flock-lock" \
	"scripts/lib.sh" \
	'flock -n 9 || refuse "another-run"' \
	'true || refuse "another-run"'

run_static_mutant "forbidden-write-0xC60E" \
	'regmap_write(regmap, 0xC60E, 0x00);'

run_static_mutant "forbidden-write-0xC050" \
	'regmap_update_bits(regmap, 0xc050, 0xff, 0x00);'

run_static_mutant "forbidden-write-0xC102" \
	'regmap_write(regmap, 0xC102, 0x01);'

run_static_mutant "forbidden-write-0xC082" \
	'regmap_write(regmap, 0xC082, 0x01);'

run_static_mutant "debugfs-regmap-access" \
	'const char *bad = "/sys/kernel/debug/regmap/sdw/access"; (void)bad;'

echo "=== Mutation Test Summary: $CAUGHT killed, $MISSED missed ==="
if ((MISSED > 0)); then
	echo "Missed mutants: ${MISSED_LIST[*]}" >&2
	exit 1
fi
exit 0
