#!/usr/bin/env bash
# Dell XPS 17 9720 (SKU 0AFF) — Boot-time & Post-Init SOF IPC4 Verifier & Emergency DRC Fence.
#
# Target deployment path (staged only):
#   /usr/local/libexec/xps17-studio-audio/xps17-sof-ipc4-verify.sh
#
# Verifies:
#   1. Hardware DMI matches Dell XPS 17 9720 (product_sku == 0AFF).
#   2. snd_sof ipc_type == 1 (IPC4 active).
#   3. ALSA card `sofsoundwire` exposes PCM 2 (Speaker) and PCM 3 (Amp feedback).
#   4. IPC4 speaker DSP controls exist: Volume, EQIIR, EQFIR, DRC switch.
#   5. `Post Mixer Speaker Playback DRC switch` is OFF (and stays OFF); if found ON,
#      immediately fences it OFF; if sticky/unreadable, clamps Post Mixer volume to
#      38,38 (-14 dB, strictly outweighing the +13.7 dB DRC makeup gain) and fails loudly.
#   6. `Post Mixer Speaker Playback Volume` <= 45,45 (0 dB ceiling).
#
# Offline testing seam:
#   --root <dir>     Fake sysfs/procfs root (never touches live /sys or /proc)
#   --amixer <path>  Mock amixer binary
#   --dry-run        Read-only inspection; never executes `amixer cset`
#   --no-sleep       Skip settle delays (offline unit tests)
set -euo pipefail

ROOT=""
AMIXER_BIN="amixer"
CARD="sofsoundwire"
DRY_RUN=0
NO_SLEEP=0
WAIT_SEC=30

CTL_VOL="Post Mixer Speaker Playback Volume"
CTL_EQIIR="Post Mixer Speaker Playback IIR Eq bytes"
CTL_EQFIR="Post Mixer Speaker Playback FIR Eq bytes"
CTL_DRC="Post Mixer Speaker Playback DRC switch"
DRC_CLAMP_VAL="38,38"   # -14 dB (step = 2 dB; 45 = 0 dB; outweighs +13.7 dB DRC makeup)
MAX_SAFE_VOL=45         # 0 dB ceiling on IPC4 gain.21.1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root)     ROOT="$2"; shift 2 ;;
    --amixer)   AMIXER_BIN="$2"; shift 2 ;;
    --card)     CARD="$2"; shift 2 ;;
    --wait-sec) WAIT_SEC="$2"; shift 2 ;;
    --dry-run)  DRY_RUN=1; shift ;;
    --no-sleep) NO_SLEEP=1; shift ;;
    -h|--help)
      sed -n '2,22p' "$0"
      exit 0
      ;;
    *)
      echo "XPS17_IPC4_VERIFY status=FAIL reason=unknown_arg arg=$1" >&2
      exit 64
      ;;
  esac
done

pause_s() {
  (( NO_SLEEP == 0 )) && sleep "$1" || true
}

read_trimmed() {
  local f="$1"
  [[ -r "$f" ]] || return 1
  tr -d '\r\n' < "$f"
}

amixer_cget() {
  local ctl="$1"
  timeout -k 2 5 "$AMIXER_BIN" -c "$CARD" cget "name=${ctl}" 2>/dev/null
}

amixer_values() {
  local ctl="$1" out
  out=$(amixer_cget "$ctl") || return 1
  printf '%s\n' "$out" | awk -F'=' '/: values=/{print $2; exit}'
}

amixer_cset() {
  local ctl="$1" val="$2"
  if (( DRY_RUN == 1 )); then
    echo "XPS17_IPC4_VERIFY dry_run_skip_cset ctl='$ctl' val='$val'" >&2
    return 1
  fi
  timeout -k 2 5 "$AMIXER_BIN" -c "$CARD" -q cset "name=${ctl}" "$val" 2>/dev/null
}

fail() {
  local code="$1" reason="$2" detail="${3:-}"
  echo "XPS17_IPC4_VERIFY status=FAIL reason=${reason} detail=${detail}" >&2
  exit "$code"
}

# 1. Verify exact hardware DMI identity (Dell XPS 17 9720, SKU 0AFF)
DMI_DIR="${ROOT}/sys/class/dmi/id"
SKU=$(read_trimmed "${DMI_DIR}/product_sku") || fail 10 "dmi_unreadable" "product_sku"
VENDOR=$(read_trimmed "${DMI_DIR}/sys_vendor") || fail 10 "dmi_unreadable" "sys_vendor"
PRODUCT=$(read_trimmed "${DMI_DIR}/product_name") || fail 10 "dmi_unreadable" "product_name"

if [[ "$SKU" != "0AFF" || "$VENDOR" != "Dell Inc." || "$PRODUCT" != "XPS 17 9720" ]]; then
  fail 11 "wrong_hardware" "sku=${SKU},vendor=${VENDOR},product=${PRODUCT}"
fi

# 2. Verify snd_sof ipc_type == 1 (IPC4)
IPC_PARAM="${ROOT}/sys/module/snd_sof/parameters/ipc_type"
IPC_TYPE=$(read_trimmed "$IPC_PARAM") || fail 12 "ipc_type_unreadable" "$IPC_PARAM"
if [[ "$IPC_TYPE" != "1" ]]; then
  fail 13 "ipc_type_not_ipc4" "ipc_type=${IPC_TYPE}"
fi

# 3. Wait for ALSA card & IPC4 PCMs (PCM 2 Speaker playback + PCM 3 Amp feedback capture)
PCM2="${ROOT}/proc/asound/${CARD}/pcm2p"
PCM3="${ROOT}/proc/asound/${CARD}/pcm3c"
elapsed=0
while [[ ! -e "$PCM2" || ! -e "$PCM3" ]]; do
  if (( elapsed >= WAIT_SEC )); then
    fail 14 "ipc4_pcm_missing" "pcm2p=$([[ -e $PCM2 ]] && echo ok || echo missing),pcm3c=$([[ -e $PCM3 ]] && echo ok || echo missing)"
  fi
  pause_s 1
  elapsed=$((elapsed + 1))
done

# 4. Verify required IPC4 DSP kcontrols exist
for ctl in "$CTL_VOL" "$CTL_EQIIR" "$CTL_EQFIR" "$CTL_DRC"; do
  if ! amixer_cget "$ctl" >/dev/null; then
    fail 15 "missing_kcontrol" "ctl=${ctl// /_}"
  fi
done

# 5. Emergency DRC fence verification (must be OFF and stay OFF)
drc_state=$(amixer_values "$CTL_DRC" || echo "unreadable")
drc_fenced_now=0
if [[ "$drc_state" != "off" ]]; then
  echo "XPS17_IPC4_VERIFY warn=drc_found_${drc_state} action=forcing_off" >&2
  if (( DRY_RUN == 1 )); then
    fail 20 "drc_not_off_dry_run" "drc=${drc_state}"
  fi
  amixer_cset "$CTL_DRC" "off" || true
  drc_fenced_now=1
fi

# Confirm DRC reads off across two consecutive checks (catches anything flipping it back on)
drc_ok=1
for _ in 1 2; do
  drc_check=$(amixer_values "$CTL_DRC" || echo "unreadable")
  if [[ "$drc_check" != "off" ]]; then
    drc_ok=0
    break
  fi
  pause_s 0.5
done

if (( drc_ok == 0 )); then
  # Sticky DRC hazard (+13.7 dB makeup!): clamp Post Mixer volume to -14 dB immediately!
  clamp_ok=0
  if amixer_cset "$CTL_VOL" "$DRC_CLAMP_VAL"; then
    v_after=$(amixer_values "$CTL_VOL" || echo "")
    if [[ "$v_after" =~ ^([0-9]+),([0-9]+)$ ]] && (( BASH_REMATCH[1] <= 38 && BASH_REMATCH[2] <= 38 )); then
      clamp_ok=1
    fi
  fi
  if (( clamp_ok == 1 )); then
    fail 21 "drc_sticky_clamped_minus14db" "drc=${drc_check},vol_clamped=${v_after}"
  else
    fail 22 "drc_sticky_clamp_failed" "drc=${drc_check}"
  fi
fi

# 6. Verify Post Mixer Speaker Playback Volume is <= 0 dB (45,45)
vol_raw=$(amixer_values "$CTL_VOL" || echo "")
if [[ ! "$vol_raw" =~ ^([0-9]+),([0-9]+)$ ]]; then
  fail 30 "volume_unreadable" "vol=${vol_raw}"
fi
vol_l="${BASH_REMATCH[1]}"
vol_r="${BASH_REMATCH[2]}"
if (( vol_l > MAX_SAFE_VOL || vol_r > MAX_SAFE_VOL )); then
  fail 31 "volume_above_0db" "vol=${vol_raw},max=${MAX_SAFE_VOL}"
fi

echo "XPS17_IPC4_VERIFY status=OK sku=${SKU} ipc_type=${IPC_TYPE} card=${CARD} pcm2p=ok pcm3c=ok eqiir=ok eqfir=ok drc=off drc_fenced=${drc_fenced_now} vol=${vol_raw}"
exit 0
