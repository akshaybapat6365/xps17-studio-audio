#!/usr/bin/env bash
# Read-only ELF, vermagic, symbol, modinfo, and static source safety auditor
# for the Dell XPS 17 9720 rt1316-sdw kernel module.
#
# Usage:
#   verify_ko_elf.sh [--driver-dir <dir>] [--ko <path>] [--static-only]
#
# Strictly read-only: NEVER loads/unloads modules, NEVER writes sysfs/debugfs,
# NEVER reads /sys/kernel/debug/regmap/*/access or range.
set -uo pipefail

VERIFY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRIVER_DIR="$(cd "$VERIFY_DIR/.." && pwd)/rt1316-xps17"
KO_PATH=""
STATIC_ONLY=0
KVER="$(uname -r)"
BASE_STOCK_C="/home/akshaybapat/project/xps17-speaker-stack/module/base/rt1316-sdw.c"

while (($# > 0)); do
	case "$1" in
	--driver-dir)
		shift
		DRIVER_DIR="${1:?missing --driver-dir argument}"
		;;
	--ko)
		shift
		KO_PATH="${1:?missing --ko argument}"
		;;
	--static-only)
		STATIC_ONLY=1
		;;
	*)
		echo "Usage: $0 [--driver-dir <dir>] [--ko <path>] [--static-only]" >&2
		exit 2
		;;
	esac
	shift
done

[[ -n "$KO_PATH" ]] || KO_PATH="$DRIVER_DIR/snd-soc-rt1316-sdw.ko"

PASS=0
FAIL=0
WARN=0

pass() { PASS=$((PASS + 1)); printf '  [PASS] %s\n' "$*"; }
fail() { FAIL=$((FAIL + 1)); printf '  [FAIL] %s\n' "$*"; }
warn() { WARN=$((WARN + 1)); printf '  [WARN] %s\n' "$*"; }

echo "=== Part A: Static Source Safety Audit ($DRIVER_DIR) ==="
if [[ ! -f "$DRIVER_DIR/rt1316-sdw.c" ]]; then
	fail "driver source missing: $DRIVER_DIR/rt1316-sdw.c"
else
	python3 -I - "$DRIVER_DIR" "$BASE_STOCK_C" <<'PY'
import difflib, glob, os, re, sys

driver_dir = sys.argv[1]
base_c_path = sys.argv[2]

c_path = os.path.join(driver_dir, "rt1316-sdw.c")
h_path = os.path.join(driver_dir, "rt1316-sdw.h")
hdr_paths = sorted(glob.glob(os.path.join(driver_dir, "rt1316-xps17*.h")))
all_files = [c_path] + ([h_path] if os.path.isfile(h_path) else []) + hdr_paths

BASE_FORBIDDEN = {
    0xC000, 0xC050, 0xC082, 0xC093, 0xC09C,
    0xC09D, 0xC0A3, 0xC102, 0xC60E,
}

def strip_comments(src: str) -> str:
    # Remove /* ... */ and // ... while preserving newlines so line numbers stay accurate
    def repl(m):
        s = m.group(0)
        if s.startswith(('"', "'")):
            return s
        return re.sub(r'[^\n]', ' ', s)
    pattern = re.compile(
        r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*|/\*.*?\*/',
        re.S,
    )
    return pattern.sub(repl, src)

# Dynamically discover any extra forbidden regs in rt1316-xps17-policy.h
forbidden = set(BASE_FORBIDDEN)
policy_h = os.path.join(driver_dir, "rt1316-xps17-policy.h")
if os.path.isfile(policy_h):
    ptext = strip_comments(open(policy_h, errors="replace").read())
    # Look inside rt1316_xps17_reg_is_forbidden or forbidden array
    m = re.search(r'reg_is_forbidden\s*\([^)]*\)\s*\{(.*?)\n\}', ptext, flags=re.S)
    if m:
        for hx in re.findall(r'0[xX]([0-9a-fA-F]+)', m.group(1)):
            forbidden.add(int(hx, 16))
    for m_arr in re.finditer(r'forbidden[^{;]*=\s*\{(.*?)\}', ptext, flags=re.S | re.I):
        for hx in re.findall(r'0[xX]([0-9a-fA-F]+)', m_arr.group(1)):
            forbidden.add(int(hx, 16))

print(f"  [INFO] Auditing against {len(forbidden)} forbidden registers: " +
      ", ".join(f"0x{r:04X}" for r in sorted(forbidden)))

failures = 0

WRITE_CALL_RE = re.compile(
    r'\b(regmap_write|regmap_update_bits|regmap_write_bits|'
    r'snd_soc_component_write|snd_soc_component_update_bits)\s*\(([^;]*?)\)',
    re.S,
)

# 1. Diff-based audit of ONLY added lines in rt1316-sdw.c (zero forbidden writes allowed, even 0xC000!)
added_lines = set()
if os.path.isfile(base_c_path):
    base_lines = open(base_c_path, errors="replace").read().splitlines()
    new_lines = open(c_path, errors="replace").read().splitlines()
    sm = difflib.SequenceMatcher(None, base_lines, new_lines)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            for ln in range(j1 + 1, j2 + 1):
                added_lines.add(ln)
    print(f"  [INFO] Diff against stock base/rt1316-sdw.c: {len(added_lines)} added/modified lines in rt1316-sdw.c")
else:
    print("  [WARN] Stock base/rt1316-sdw.c not found; auditing entire rt1316-sdw.c")

for fpath in all_files:
    raw = open(fpath, errors="replace").read()
    clean = strip_comments(raw)
    fname = os.path.basename(fpath)

    # Check direct regmap/component write calls to any forbidden register literal
    bad_calls = []
    for m in WRITE_CALL_RE.finditer(clean):
        fn_name = m.group(1)
        args = m.group(2)
        lineno = clean[:m.start()].count("\n") + 1
        # Split top-level arguments by comma (ignoring nested parens)
        parts = []
        depth = 0
        cur = []
        for ch in args:
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            if ch == ',' and depth == 0:
                parts.append("".join(cur).strip())
                cur = []
            else:
                cur.append(ch)
        if cur:
            parts.append("".join(cur).strip())
        if len(parts) < 2:
            continue
        reg_arg = parts[1]
        for hx in re.findall(r'\b0[xX]([0-9a-fA-F]+)[uUlL]*\b', reg_arg):
            val = int(hx, 16)
            if val in forbidden:
                # Allow ONLY unmodified upstream chip software reset regmap_write(..., 0xc000, 0x02)
                is_upstream_reset = (
                    fname == "rt1316-sdw.c"
                    and val == 0xC000
                    and fn_name == "regmap_write"
                    and len(parts) >= 3
                    and re.fullmatch(r'0[xX]0*2[uUlL]*', parts[2].strip()) is not None
                    and (not added_lines or lineno not in added_lines)
                )
                if not is_upstream_reset:
                    bad_calls.append((lineno, fn_name, f"0x{val:04X}", m.group(0).replace("\n", " ").strip()))

    if bad_calls:
        failures += 1
        for ln, fn, hx, snippet in bad_calls:
            print(f"  [FAIL] {fname}:{ln}: forbidden register {hx} in {fn}(): {snippet}")
    else:
        print(f"  [PASS] {fname}: zero direct regmap/component writes to forbidden registers in added code")

    # Check zero debugfs regmap access/range references in source
    dbg_hits = re.findall(r'debug/regmap|regmap[^\n"]*/(?:access|range)\b', raw)
    if dbg_hits:
        failures += 1
        print(f"  [FAIL] {fname}: forbidden debugfs regmap access/range reference found: {dbg_hits}")
    else:
        print(f"  [PASS] {fname}: zero debugfs regmap access/range references")

# Verify upstream rt1316_blind_write[] safety entries were NOT altered/weakened
c_raw = open(c_path, errors="replace").read()
required_blind = [
    (0xC60E, 0x05),
    (0xC102, 0x00),
    (0xC050, 0x83),
]
for reg, val in required_blind:
    pat = re.compile(rf'\{{\s*0[xX]0*{reg:x}\s*,\s*0[xX]0*{val:x}\s*\}}', re.I)
    if pat.search(c_raw):
        print(f"  [PASS] rt1316-sdw.c: stock protection entry {{ 0x{reg:04X}, 0x{val:02X} }} preserved")
    else:
        failures += 1
        print(f"  [FAIL] rt1316-sdw.c: stock protection entry {{ 0x{reg:04X}, 0x{val:02X} }} missing or altered!")

# Verify rt1316-sdw.c (directly or via rt1316-sdw.h) includes rt1316-xps17-policy.h
h_raw = open(h_path, errors="replace").read() if os.path.isfile(h_path) else ""
incl_direct = bool(re.search(r'#\s*include\s*"rt1316-xps17-policy\.h"', c_raw))
incl_via_h = (bool(re.search(r'#\s*include\s*"rt1316-sdw\.h"', c_raw)) and
              bool(re.search(r'#\s*include\s*"rt1316-xps17-policy\.h"', h_raw)))
if incl_direct or incl_via_h:
    via = "directly" if incl_direct else "via rt1316-sdw.h"
    print(f"  [PASS] rt1316-sdw.c: includes rt1316-xps17-policy.h ({via})")
else:
    failures += 1
    print("  [FAIL] rt1316-sdw.c: does not include rt1316-xps17-policy.h (directly or via rt1316-sdw.h)")

if "rt1316_xps17_reg_is_forbidden" in c_raw or "rt1316_xps17_reg_is_allowed_write" in c_raw:
    print("  [PASS] rt1316-sdw.c: enforces policy register guard helpers")
else:
    failures += 1
    print("  [FAIL] rt1316-sdw.c: does not reference policy register guard helpers")

sys.exit(1 if failures else 0)
PY
	if [[ $? -eq 0 ]]; then
		pass "static source safety audit passed"
	else
		fail "static source safety audit failed"
	fi
fi

if ((STATIC_ONLY)); then
	echo "=== Summary (static-only): $PASS passed, $FAIL failed, $WARN warnings ==="
	exit $((FAIL > 0 ? 1 : 0))
fi

echo ""
echo "=== Part B: ELF / Vermagic / Modinfo / Symbol Audit ($KO_PATH) ==="

if [[ ! -f "$KO_PATH" ]]; then
	fail "compiled module not found: $KO_PATH"
	echo "=== Summary: $PASS passed, $FAIL failed, $WARN warnings ==="
	exit 1
fi

# 1. Valid ELF64 x86_64 relocatable object
ELF_HDR="$(readelf -h "$KO_PATH" 2>&1)"
if grep -q 'Class:.*ELF64' <<<"$ELF_HDR" &&
   grep -q 'Type:.*REL (Relocatable file)' <<<"$ELF_HDR" &&
   grep -q 'Machine:.*Advanced Micro Devices X86-64' <<<"$ELF_HDR"; then
	pass "valid ELF64 x86_64 relocatable kernel object"
else
	fail "ELF header invalid for x86_64 kernel module"
fi

# 2. Exact vermagic match against stock kernel module AND uname -r
STOCK_KO=""
for cand in \
	"/lib/modules/$KVER/kernel/sound/soc/codecs/snd-soc-rt1316-sdw.ko.zst" \
	"/lib/modules/$KVER/kernel/sound/soc/codecs/snd-soc-rt1316-sdw.ko.xz" \
	"/lib/modules/$KVER/kernel/sound/soc/codecs/snd-soc-rt1316-sdw.ko"; do
	[[ -f "$cand" ]] && { STOCK_KO="$cand"; break; }
done

KO_VERMAGIC="$(modinfo -F vermagic "$KO_PATH" 2>/dev/null || true)"
if [[ -z "$STOCK_KO" ]]; then
	fail "stock module not found under /lib/modules/$KVER/kernel/sound/soc/codecs/"
else
	STOCK_VERMAGIC="$(modinfo -F vermagic "$STOCK_KO" 2>/dev/null || true)"
	if [[ -n "$KO_VERMAGIC" && "$KO_VERMAGIC" == "$STOCK_VERMAGIC" ]]; then
		pass "vermagic exact match with stock module: '$KO_VERMAGIC'"
	else
		fail "vermagic mismatch: built='$KO_VERMAGIC' vs stock='$STOCK_VERMAGIC'"
	fi
fi

if [[ "$KO_VERMAGIC" == "$KVER "* ]]; then
	pass "vermagic prefix matches running uname -r ($KVER)"
else
	fail "vermagic '$KO_VERMAGIC' does not start with uname -r '$KVER '"
fi

# 3. License, retpoline, and zero taint flags
KO_LICENSE="$(modinfo -F license "$KO_PATH" 2>/dev/null || true)"
if [[ "$KO_LICENSE" == "GPL" || "$KO_LICENSE" == "GPL v2" || "$KO_LICENSE" == "Dual BSD/GPL" ]]; then
	pass "clean GPL-compatible license: '$KO_LICENSE'"
else
	fail "unexpected/tainting license: '$KO_LICENSE'"
fi

KO_STAGING="$(modinfo -F staging "$KO_PATH" 2>/dev/null || true)"
if [[ -z "$KO_STAGING" ]]; then
	pass "no staging taint flag in modinfo"
else
	fail "unexpected staging taint flag: '$KO_STAGING'"
fi

KO_RETPOLINE="$(modinfo -F retpoline "$KO_PATH" 2>/dev/null || true)"
STOCK_RETPOLINE="$(modinfo -F retpoline "$STOCK_KO" 2>/dev/null || true)"
if [[ "$KO_RETPOLINE" == "$STOCK_RETPOLINE" ]]; then
	pass "retpoline matches stock ('$KO_RETPOLINE')"
else
	fail "retpoline mismatch: built='$KO_RETPOLINE' vs stock='$STOCK_RETPOLINE'"
fi

# 4. Zero EXPORT_SYMBOL leaks
SEC_HDRS="$(readelf -S -W "$KO_PATH" 2>/dev/null || true)"
ALL_SYMS="$(nm "$KO_PATH" 2>/dev/null || true)"
if grep -q '__ksymtab' <<<"$SEC_HDRS" || grep -qE '__ksymtab_|__export_symbol_' <<<"$ALL_SYMS"; then
	fail "unexpected EXPORT_SYMBOL leak detected in $KO_PATH"
else
	pass "zero EXPORT_SYMBOL leaks (no __ksymtab / __export_symbol_ entries)"
fi

# 5. Undefined symbols strictly within kernel Module.symvers
SYMVERS="/lib/modules/$KVER/build/Module.symvers"
if [[ ! -r "$SYMVERS" ]]; then
	fail "kernel Module.symvers unreadable at $SYMVERS"
else
	python3 -I - "$KO_PATH" "$SYMVERS" <<'PY'
import subprocess, sys

ko, symvers = sys.argv[1:3]
known = set()
with open(symvers, errors="replace") as f:
    for line in f:
        parts = line.split()
        if len(parts) >= 2:
            known.add(parts[1])

out = subprocess.check_output(["nm", "-u", ko], text=True)
undef = [l.split()[-1] for l in out.splitlines() if l.strip()]
missing = [s for s in undef if s not in known]
print(f"  [INFO] Undefined symbols: {len(undef)} total, {len(missing)} outside Module.symvers")
if missing:
    print("  [FAIL] Unexpected undefined symbols outside Module.symvers:", ", ".join(missing))
    sys.exit(1)
sys.exit(0)
PY
	if [[ $? -eq 0 ]]; then
		pass "all undefined symbols resolved in kernel Module.symvers"
	else
		fail "undefined symbols found outside kernel Module.symvers"
	fi
fi

# 6. Required safety symbols present (DMI check + synchronous timer/workqueue cleanup)
UNDEF_SYMS="$(nm -u "$KO_PATH" 2>/dev/null | awk '{print $NF}')"
if grep -qE '^(dmi_check_system|dmi_match|dmi_get_system_info|dmi_first_match)$' <<<"$UNDEF_SYMS"; then
	DMI_SYMS="$(grep -E '^(dmi_check_system|dmi_match|dmi_get_system_info|dmi_first_match)$' <<<"$UNDEF_SYMS" | tr '\n' ' ')"
	pass "DMI check symbol(s) linked: $DMI_SYMS"
else
	fail "no DMI check symbol found in undefined symbols (DMI 0AFF guard missing!)"
fi

if grep -qE '^(cancel_delayed_work_sync|disable_delayed_work_sync|timer_delete_sync|del_timer_sync|timer_shutdown_sync)$' <<<"$UNDEF_SYMS"; then
	CLEANUP_SYMS="$(grep -E '^(cancel_delayed_work_sync|disable_delayed_work_sync|timer_delete_sync|del_timer_sync|timer_shutdown_sync)$' <<<"$UNDEF_SYMS" | tr '\n' ' ')"
	pass "synchronous timer/workqueue teardown symbol(s) linked: $CLEANUP_SYMS"
else
	fail "no synchronous timer/workqueue cleanup symbol (cancel_delayed_work_sync / timer_delete_sync) found!"
fi

if grep -qE '^(queue_delayed_work_on|mod_delayed_work_on|mod_timer|schedule_delayed_work|__queue_delayed_work)$' <<<"$UNDEF_SYMS"; then
	SCHED_SYMS="$(grep -E '^(queue_delayed_work_on|mod_delayed_work_on|mod_timer|schedule_delayed_work|__queue_delayed_work)$' <<<"$UNDEF_SYMS" | tr '\n' ' ')"
	pass "deadman timer/workqueue scheduling symbol(s) linked: $SCHED_SYMS"
else
	fail "no deadman timer/workqueue scheduling symbol found!"
fi

# Verify objdump confirms teardown call sites actually exist in .text
OBJDUMP_CALLS="$(objdump -dr "$KO_PATH" 2>/dev/null | grep -E 'cancel_delayed_work_sync|disable_delayed_work_sync|timer_delete_sync|del_timer_sync|timer_shutdown_sync' | wc -l)"
if ((OBJDUMP_CALLS > 0)); then
	pass "objdump confirms $OBJDUMP_CALLS relocation call site(s) to synchronous watchdog teardown"
else
	fail "objdump found zero call sites to synchronous watchdog teardown"
fi

# 7. Required ALSA kcontrol strings & DMI SKU string in .rodata / strings
KO_STRINGS="$(strings -a "$KO_PATH")"
for ctl in "BQ Crossover Switch" "BQ Crossover Profile" "Guard Drive Unlock"; do
	if grep -qF "$ctl" <<<"$KO_STRINGS"; then
		pass "ALSA control string present: \"$ctl\""
	else
		fail "missing expected ALSA control string: \"$ctl\""
	fi
done

if grep -qF "0AFF" <<<"$KO_STRINGS"; then
	pass "DMI product SKU literal \"0AFF\" present in module binary"
else
	fail "DMI product SKU literal \"0AFF\" not found in module binary"
fi

# 8. Zero debugfs regmap access/range references in binary
if grep -qE 'debug/regmap|regmap/.*/(access|range)|/regmap/[^ ]*/(access|range)' <<<"$KO_STRINGS"; then
	fail "forbidden debugfs regmap access/range string found in $KO_PATH!"
else
	pass "zero debugfs regmap access/range strings in module binary"
fi

echo ""
echo "=== ELF/Static Audit Summary: $PASS passed, $FAIL failed, $WARN warnings ==="
if ((FAIL > 0)); then
	exit 1
fi
exit 0
