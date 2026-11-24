#!/usr/bin/env bash
# Master Offline Kernel Safety Verification Runner
# Dell XPS 17 9720 Studio Audio Engineering Stack
#
# Runs (strictly read-only on live system, strictly on E-cores 12-19, nice 19):
#   1. Exhaustive C policy, crossover math, IV-slot, and P4 deadman gate suite (ASan+UBSan)
#   2. Static source audit + ELF/vermagic/modinfo/symbol audit of snd-soc-rt1316-sdw.ko
#   3. Adversarial isolated mock-harness test suite for swap-in/swap-out/dkms-install scripts
#   4. Real dkms.conf + real built .ko preflight inside an isolated temporary mock sandbox
set -uo pipefail

VERIFY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRIVER_DIR="$(cd "$VERIFY_DIR/.." && pwd)/rt1316-xps17"
ECORE=(taskset -c 12-19 nice -n 19)

OVERALL_FAIL=0
STEP_RESULTS=()

run_step() {
	local name="$1"; shift
	echo ""
	echo "========================================================================"
	echo "STEP: $name"
	echo "========================================================================"
	if "${ECORE[@]}" "$@"; then
		STEP_RESULTS+=("PASS  $name")
	else
		STEP_RESULTS+=("FAIL  $name")
		OVERALL_FAIL=1
	fi
}

# Check thermal headroom before compile (per COORDINATOR-NOTES.md)
check_temp() {
	local temp
	temp="$(sensors 2>/dev/null | awk '/Package id 0:/ {gsub(/[+°C]/, "", $4); printf "%d", $4; exit}' || true)"
	if [[ -n "$temp" ]]; then
		echo "Thermal check: Package id 0 = ${temp}C (limit < 82C)"
		if ((temp >= 82)); then
			echo "WARNING: Package id 0 >= 82C; waiting 15s..."
			sleep 15
		fi
	fi
}

check_temp

# 1. C policy & state-machine test suite (ASan + UBSan)
if [[ -f "$DRIVER_DIR/rt1316-xps17-policy.h" && -f "$VERIFY_DIR/test_rt1316_policy.c" ]]; then
	run_step "C Policy, Crossover Math, IV-Slot & P4 Deadman Suite (ASan+UBSan)" \
		make -C "$VERIFY_DIR" clean test
else
	echo "PENDING: Missing $DRIVER_DIR/rt1316-xps17-policy.h"
	STEP_RESULTS+=("FAIL  C Policy Suite (driver policy header not yet ready)")
	OVERALL_FAIL=1
fi

# 2. Static Source + ELF/Vermagic/Symbol Audit
if [[ -f "$DRIVER_DIR/snd-soc-rt1316-sdw.ko" ]]; then
	run_step "Static Source & ELF/Vermagic/Symbol Audit (snd-soc-rt1316-sdw.ko)" \
		"$VERIFY_DIR/verify_ko_elf.sh" --driver-dir "$DRIVER_DIR"
else
	echo "PENDING: Compiled module not yet present at $DRIVER_DIR/snd-soc-rt1316-sdw.ko"
	STEP_RESULTS+=("FAIL  ELF/Vermagic/Symbol Audit (snd-soc-rt1316-sdw.ko not yet ready)")
	OVERALL_FAIL=1
fi

# 3. Adversarial Mock-Harness Script Tests (isolated mktemp -d sandbox)
run_step "Adversarial Mock-Harness Deployment & Rollback Script Suite" \
	"$VERIFY_DIR/test_scripts_mock.sh"

# 4. Adversarial Mutation Suite (13 single-point fault injections)
run_step "Adversarial Mutation Self-Test Suite (13 Fault Injections)" \
	"$VERIFY_DIR/test_mutants.sh"

# 5. Real dkms.conf + real built .ko validation strictly inside isolated mock sandbox
if [[ -f "$DRIVER_DIR/dkms.conf" && -f "$DRIVER_DIR/snd-soc-rt1316-sdw.ko" ]]; then
	validate_real_artifacts_in_sandbox() {
		local tmp kver stock_real
		kver="$(uname -r)"
		tmp="$(mktemp -d /tmp/xps17-real-sandbox.XXXXXX)"
		trap 'rm -rf "$tmp"' RETURN

		mkdir -p "$tmp/bin" "$tmp/runs" \
			"$tmp/root/sys/class/dmi/id" \
			"$tmp/root/lib/modules/$kver/kernel/sound/soc/codecs" \
			"$tmp/root/lib/modules/$kver/build" \
			"$tmp/root/var/lib/alsa" \
			"$tmp/root/sys/class/hwmon/hwmon0"
		echo "0AFF" > "$tmp/root/sys/class/dmi/id/product_sku"
		echo "coretemp" > "$tmp/root/sys/class/hwmon/hwmon0/name"
		echo "Package id 0" > "$tmp/root/sys/class/hwmon/hwmon0/temp1_label"
		echo "60000" > "$tmp/root/sys/class/hwmon/hwmon0/temp1_input"

		# Link read-only real stock module into fake root so real modinfo verifies it
		for stock_real in "/lib/modules/$kver/kernel/sound/soc/codecs/snd-soc-rt1316-sdw.ko"*; do
			if [[ -f "$stock_real" ]]; then
				ln -s "$stock_real" "$tmp/root/lib/modules/$kver/kernel/sound/soc/codecs/$(basename "$stock_real")"
				break
			fi
		done

		# Stub systemctl & sudo so live systemd/root is NEVER queried or touched
		cat > "$tmp/bin/systemctl" <<'STUB'
#!/usr/bin/env bash
echo "active"
exit 0
STUB
		cat > "$tmp/bin/sudo" <<'STUB'
#!/usr/bin/env bash
echo "SAFETY: sudo must never be called during dry-run!" >&2
exit 99
STUB
		chmod +x "$tmp/bin/systemctl" "$tmp/bin/sudo"

		# Validate real dkms.conf & real source tree via dkms-install.sh --dry-run
		PATH="$tmp/bin:$PATH" \
		XPS17_ROOT="$tmp/root" \
		SYS_DMI_SKU="$tmp/root/sys/class/dmi/id/product_sku" \
		XPS17_DRIVER_DIR="$DRIVER_DIR" \
		XPS17_RUNS_DIR="$tmp/runs" \
			"$VERIFY_DIR/scripts/dkms-install.sh" --dry-run --src-dir "$DRIVER_DIR" || return 1

		# Validate real compiled .ko vermagic & real stock .ko.zst via swap-in.sh --dry-run
		PATH="$tmp/bin:$PATH" \
		XPS17_ROOT="$tmp/root" \
		SYS_DMI_SKU="$tmp/root/sys/class/dmi/id/product_sku" \
		XPS17_DRIVER_DIR="$DRIVER_DIR" \
		XPS17_RUNS_DIR="$tmp/runs" \
			"$VERIFY_DIR/scripts/swap-in.sh" --dry-run --ko "$DRIVER_DIR/snd-soc-rt1316-sdw.ko" || return 1

		# Validate stock rollback discovery via swap-out.sh --dry-run
		PATH="$tmp/bin:$PATH" \
		XPS17_ROOT="$tmp/root" \
		SYS_DMI_SKU="$tmp/root/sys/class/dmi/id/product_sku" \
		XPS17_DRIVER_DIR="$DRIVER_DIR" \
		XPS17_RUNS_DIR="$tmp/runs" \
			"$VERIFY_DIR/scripts/swap-out.sh" --dry-run || return 1

		# Validate real dkms-pre-install.sh against isolated mock asound.state files
		if [[ -x "$DRIVER_DIR/dkms-pre-install.sh" ]]; then
			cat > "$tmp/asound_ok.state" <<'ALSA'
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
			cat > "$tmp/asound_bad.state" <<'ALSA'
state.sofsoundwire {
	control.1 {
		iface MIXER
		name 'rt1316-1 XU24 Bypass Switch'
		value false
	}
	control.2 {
		iface MIXER
		name 'rt1316-2 XU24 Bypass Switch'
		value true
	}
}
ALSA
			RT1316_ALSA_STATE="$tmp/asound_ok.state" "$DRIVER_DIR/dkms-pre-install.sh" || {
				echo "FAIL: dkms-pre-install.sh rejected valid XU24=false state" >&2
				return 1
			}
			if RT1316_ALSA_STATE="$tmp/asound_bad.state" "$DRIVER_DIR/dkms-pre-install.sh" 2>/dev/null; then
				echo "FAIL: dkms-pre-install.sh accepted unsafe XU24=true state!" >&2
				return 1
			fi
			if RT1316_ALSA_STATE="$tmp/nonexistent.state" "$DRIVER_DIR/dkms-pre-install.sh" 2>/dev/null; then
				echo "FAIL: dkms-pre-install.sh accepted unreadable state file!" >&2
				return 1
			fi
			echo "real dkms-pre-install.sh validated: accepts both-false, rejects true, rejects unreadable"
		fi
	}
	run_step "Real Driver Artifacts Sandbox Dry-Run (dkms.conf + built .ko + stock .ko.zst)" \
		bash -c "$(declare -f validate_real_artifacts_in_sandbox); VERIFY_DIR='$VERIFY_DIR' DRIVER_DIR='$DRIVER_DIR' validate_real_artifacts_in_sandbox"
else
	STEP_RESULTS+=("FAIL  Real Driver Artifacts Sandbox Dry-Run (artifacts not yet ready)")
	OVERALL_FAIL=1
fi

echo ""
echo "========================================================================"
echo "MASTER VERIFICATION SUMMARY"
echo "========================================================================"
for r in "${STEP_RESULTS[@]}"; do
	echo "  $r"
done
echo "========================================================================"
if ((OVERALL_FAIL)); then
	echo "OVERALL VERDICT: FAIL"
	exit 1
else
	echo "OVERALL VERDICT: PASS (ALL CHECKS GREEN)"
	exit 0
fi
