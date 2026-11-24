#!/usr/bin/env bash
# Safe staged DKMS installer for Dell XPS 17 9720 (DMI SKU 0AFF) rt1316-sdw driver.
#
# Usage:
#   dkms-install.sh --dry-run [--src-dir <dir>]
#   dkms-install.sh --confirm-live [--src-dir <dir>]
#
# Validates:
#   1. Explicit --dry-run OR --confirm-live (refuses bare invocation with exit 2).
#   2. DMI product_sku == 0AFF (refuses exit 2 on any other machine).
#   3. Kernel release matches expected KVER & kernel headers exist.
#   4. dkms.conf syntax & required fields (PACKAGE_NAME in allowlist,
#      PACKAGE_VERSION=7.2.15.2, BUILT_MODULE_NAME, DEST_MODULE_LOCATION,
#      BUILD_EXCLUSIVE_KERNEL matching KVER, and PRE_INSTALL script if declared).
#   5. Required source files present & SHA256SUMS verified (if present).
#   6. /var/lib/alsa/asound.state does NOT store XU24 Bypass Switch = true
#      (safety gate from REVIEW-kernel-safety.json).
#   7. Thermal ceiling check (< 82C before compile).
#
# Stages into /usr/src/<PACKAGE_NAME>-<PACKAGE_VERSION>/ ONLY with --confirm-live.
set -uo pipefail

SCRIPT_NAME="dkms-install"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

usage() {
	cat <<EOF
Usage: $0 (--dry-run | --confirm-live) [--src-dir <driver-source-dir>]

Safety gates:
  --dry-run       Validate DMI SKU, headers, dkms.conf, checksums, XU24 state, thermal; print plan only.
  --confirm-live  Stage /usr/src/<PACKAGE_NAME>-<PACKAGE_VERSION> and run dkms add/build/install + depmod.
  --src-dir <dir> Override driver source directory (default: $DRIVER_DIR).
EOF
	exit 2
}

SRC_DIR="$DRIVER_DIR"
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
	--src-dir)
		shift
		[[ $# -gt 0 ]] || usage
		SRC_DIR="$1"
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

# Gate 3: Kernel release & headers check.
check_kernel_release
KHEADERS="$R/lib/modules/$KVER/build"
if [[ ! -d "$KHEADERS" ]]; then
	KHEADERS="$R/usr/lib/modules/$KVER/build"
fi
[[ -d "$KHEADERS" ]] || refuse "missing-headers" "kernel headers directory not found for $KVER"
say "kernel headers verified: $KHEADERS"

# Gate 4: Validate source directory and dkms.conf, deriving exact PACKAGE_NAME/VERSION/PRE_INSTALL.
[[ -d "$SRC_DIR" ]] || refuse "missing-src-dir" "driver source directory not found: $SRC_DIR"
DKMS_CONF="$SRC_DIR/dkms.conf"
[[ -f "$DKMS_CONF" ]] || refuse "missing-dkms-conf" "dkms.conf not found in $SRC_DIR"

PARSED_DKMS="$(python3 -I - "$DKMS_CONF" "$DKMS_PKG_VER" "$KVER" <<'PY'
import re, sys

conf_path, want_ver, kver = sys.argv[1:4]
text = open(conf_path, errors="replace").read()

def get_val(key):
    m = re.search(rf'^\s*{re.escape(key)}\s*=\s*"?([^"\n#]+)"?', text, flags=re.M)
    return m.group(1).strip() if m else None

errs = []
pkg = get_val("PACKAGE_NAME")
ver = get_val("PACKAGE_VERSION")
mod = get_val("BUILT_MODULE_NAME[0]")
dest = get_val("DEST_MODULE_LOCATION[0]")
excl = get_val("BUILD_EXCLUSIVE_KERNEL")
pre_install = get_val("PRE_INSTALL") or ""

ALLOWED_PKGS = ("snd-soc-rt1316-sdw", "snd-soc-rt1316-sdw-xps17")
if pkg not in ALLOWED_PKGS:
    errs.append(f"PACKAGE_NAME='{pkg}' not in allowed {ALLOWED_PKGS}")
if ver != want_ver:
    errs.append(f"PACKAGE_VERSION='{ver}' != '{want_ver}'")
if mod not in ("snd-soc-rt1316-sdw", "snd_soc_rt1316_sdw"):
    errs.append(f"BUILT_MODULE_NAME[0]='{mod}' unexpected")
if not dest or not dest.startswith("/"):
    errs.append(f"DEST_MODULE_LOCATION[0]='{dest}' missing or relative")
if not excl:
    errs.append("BUILD_EXCLUSIVE_KERNEL is missing (required to guard future kernel upgrades)")
elif not re.search(excl, kver):
    errs.append(f"BUILD_EXCLUSIVE_KERNEL='{excl}' does not match running kernel '{kver}'")
if pre_install and ("/" in pre_install or ".." in pre_install):
    errs.append(f"PRE_INSTALL='{pre_install}' must be a local filename inside source dir")

if errs:
    for e in errs:
        print("dkms.conf error:", e, file=sys.stderr)
    sys.exit(1)

print(f"{pkg}|{ver}|{pre_install}|{mod}|{dest}|{excl}")
PY
)" || refuse "invalid-dkms-conf" "dkms.conf validation failed"

IFS='|' read -r DKMS_PKG_NAME DKMS_PKG_VER DKMS_PRE_INSTALL DKMS_MOD_NAME DKMS_DEST DKMS_EXCL <<<"$PARSED_DKMS"
DKMS_USR_SRC="$R/usr/src/${DKMS_PKG_NAME}-${DKMS_PKG_VER}"
say "dkms.conf validated: ${DKMS_PKG_NAME}/${DKMS_PKG_VER}, module=${DKMS_MOD_NAME}, dest=${DKMS_DEST}, exclusive=${DKMS_EXCL}${DKMS_PRE_INSTALL:+, pre_install=${DKMS_PRE_INSTALL}}"

# Validate PRE_INSTALL hook if declared in dkms.conf
if [[ -n "$DKMS_PRE_INSTALL" ]]; then
	[[ -f "$SRC_DIR/$DKMS_PRE_INSTALL" && -x "$SRC_DIR/$DKMS_PRE_INSTALL" ]] ||
		refuse "missing-pre-install" "PRE_INSTALL script missing or not executable: $SRC_DIR/$DKMS_PRE_INSTALL"
	sh -n "$SRC_DIR/$DKMS_PRE_INSTALL" ||
		refuse "invalid-pre-install" "PRE_INSTALL script failed syntax check: $SRC_DIR/$DKMS_PRE_INSTALL"
	say "PRE_INSTALL hook validated: $DKMS_PRE_INSTALL"
fi

# Gate 5: Required driver source files & SHA256SUMS integrity.
REQUIRED_FILES=(dkms.conf rt1316-sdw.c rt1316-sdw.h rt1316-xps17-policy.h)
for f in "${REQUIRED_FILES[@]}"; do
	[[ -f "$SRC_DIR/$f" ]] || refuse "missing-source" "required source file missing: $SRC_DIR/$f"
done
if [[ ! -f "$SRC_DIR/Makefile" && ! -f "$SRC_DIR/Kbuild" ]]; then
	refuse "missing-build-file" "neither Makefile nor Kbuild found in $SRC_DIR"
fi
if [[ -f "$SRC_DIR/SHA256SUMS" ]]; then
	(cd "$SRC_DIR" && sha256sum -c --quiet SHA256SUMS >/dev/null 2>&1) ||
		refuse "checksum-mismatch" "SHA256SUMS verification failed in $SRC_DIR"
	say "SHA256SUMS verified cleanly"
else
	say "SHA256SUMS not present in $SRC_DIR; will compute manifest on staging"
fi

# Gate 6: XU24 saved ALSA state safety check (from REVIEW-kernel-safety.json).
check_asound_state_xu24 || refuse "xu24-saved-on" "$ALSA_STATE_FILE stores XU24 Bypass Switch = true; set XU24 off and run 'sudo alsactl store' first"

# Gate 7: Thermal ceiling check (< 82C).
check_thermal_ceiling

# Gate 8: Exclusive lock.
init_run_lock "dkms-install"

# Collect files to stage (sources + headers + build files + dkms.conf + PRE_INSTALL hook)
STAGE_FILES=()
for cand in dkms.conf Makefile Kbuild SHA256SUMS rt1316-sdw.c rt1316-sdw.h rt1316-xps17-policy.h rt1316-xps17-bq-tables.h; do
	[[ -f "$SRC_DIR/$cand" ]] && STAGE_FILES+=("$cand")
done
if [[ -n "$DKMS_PRE_INSTALL" && -f "$SRC_DIR/$DKMS_PRE_INSTALL" ]]; then
	STAGE_FILES+=("$DKMS_PRE_INSTALL")
fi

if ((DRY_RUN)); then
	say "=== DRY-RUN DKMS INSTALL PLAN (zero system changes) ==="
	say "  [dry-run] would stage files (${STAGE_FILES[*]}) into $DKMS_USR_SRC"
	run_priv 60 mkdir -p "$DKMS_USR_SRC"
	run_priv 120 dkms add -m "$DKMS_PKG_NAME" -v "$DKMS_PKG_VER"
	run_priv 600 taskset -c 12-19 nice -n 19 dkms build -m "$DKMS_PKG_NAME" -v "$DKMS_PKG_VER" -k "$KVER"
	run_priv 300 dkms install -m "$DKMS_PKG_NAME" -v "$DKMS_PKG_VER" -k "$KVER"
	run_priv 120 depmod -a "$KVER"
	say "  [dry-run] would verify modinfo -n $MOD_KO_BASE points to updates/dkms"
	printf 'OUTCOME=dry-run-ok\n'
	exit 0
fi

# Live execution (--confirm-live)
if [[ -e "$DKMS_USR_SRC" ]]; then
	refuse "already-staged" "$DKMS_USR_SRC already exists; remove old DKMS version first"
fi

say "Step 1/5: staging source tree into $DKMS_USR_SRC"
run_priv 60 mkdir -p "$DKMS_USR_SRC" || { fail "stage-mkdir" "mkdir $DKMS_USR_SRC failed"; exit 4; }
for f in "${STAGE_FILES[@]}"; do
	run_priv 60 cp -p "$SRC_DIR/$f" "$DKMS_USR_SRC/$f" || { fail "stage-copy" "copy $f failed"; exit 4; }
done

say "Step 2/5: dkms add $DKMS_PKG_NAME/$DKMS_PKG_VER"
run_priv 120 dkms add -m "$DKMS_PKG_NAME" -v "$DKMS_PKG_VER" || { fail "dkms-add" "dkms add failed"; exit 4; }

say "Step 3/5: dkms build $DKMS_PKG_NAME/$DKMS_PKG_VER (E-cores, nice 19)"
check_thermal_ceiling
run_priv 600 taskset -c 12-19 nice -n 19 dkms build -m "$DKMS_PKG_NAME" -v "$DKMS_PKG_VER" -k "$KVER" || {
	fail "dkms-build" "dkms build failed"
	exit 4
}

say "Step 4/5: dkms install $DKMS_PKG_NAME/$DKMS_PKG_VER"
run_priv 300 dkms install -m "$DKMS_PKG_NAME" -v "$DKMS_PKG_VER" -k "$KVER" || {
	fail "dkms-install" "dkms install failed"
	exit 4
}

say "Step 5/5: depmod & modinfo verification"
run_priv 120 depmod -a "$KVER" || { fail "depmod" "depmod failed"; exit 4; }
modpath="$(tq modinfo -n "$MOD_KO_BASE" 2>/dev/null || true)"
say "modinfo -n $MOD_KO_BASE -> ${modpath:-<empty>}"
if [[ "$modpath" != *updates* && "$modpath" != *dkms* ]]; then
	warn "modinfo -n returned '$modpath' (expected updates/dkms override path)"
fi

printf 'OUTCOME=dkms-installed\n'
exit 0
