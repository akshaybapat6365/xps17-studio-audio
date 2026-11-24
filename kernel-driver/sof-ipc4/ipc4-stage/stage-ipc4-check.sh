#!/usr/bin/env bash
# Offline, strictly READ-ONLY self-test & preflight validator for the XPS 17 9720 SOF IPC4 staging stack.
# NEVER modifies /etc, /usr, ~/.config, ALSA mixer state, or kernel modules.
set -euo pipefail

STAGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOF_DIR="$(cd "${STAGE_DIR}/.." && pwd)"

FW_PATH="/usr/lib/firmware/intel/sof-ipc4/adl/sof-adl.ri"
FW_SHA256_EXPECTED="351325786e53ca7cd23ffc1462d82913f3e0c08ad78f89053d85e4b6703cf12b"
TPLG_PATH="/usr/lib/firmware/intel/sof-ipc4-tplg/sof-adl-rt711-l0-rt1316-l12-rt714-l3.tplg"
TPLG_SHA256_EXPECTED="4d823c4643cb253c63ab793d18e101668ad55f0f5813e324f2832a837abdfe13"
UCM_SDW="/usr/share/alsa/ucm2/sof-soundwire/sof-soundwire.conf"
UCM_DSP="/usr/share/alsa/ucm2/Intel/sof-hda-dsp/dsp.conf"

PASS_COUNT=0
FAIL_COUNT=0

ok()   { echo "  [PASS] $*"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "  [FAIL] $*" >&2; FAIL_COUNT=$((FAIL_COUNT + 1)); }

echo "=== XPS 17 9720 SOF IPC4 Staging Offline Self-Test (READ-ONLY) ==="

# 1. Hardware DMI verification (read-only)
VENDOR=$(tr -d '\r\n' < /sys/class/dmi/id/sys_vendor)
PRODUCT=$(tr -d '\r\n' < /sys/class/dmi/id/product_name)
SKU=$(tr -d '\r\n' < /sys/class/dmi/id/product_sku)
if [[ "$VENDOR" == "Dell Inc." && "$PRODUCT" == "XPS 17 9720" && "$SKU" == "0AFF" ]]; then
  ok "DMI identity verified: vendor='${VENDOR}', product='${PRODUCT}', sku='${SKU}'"
else
  fail "Unexpected DMI: vendor='${VENDOR}', product='${PRODUCT}', sku='${SKU}'"
fi

# 2. IPC4 firmware binary presence & SHA256
if [[ -r "$FW_PATH" ]]; then
  fw_sha=$(sha256sum "$FW_PATH" | awk '{print $1}')
  fw_sz=$(stat -L -c '%s' "$FW_PATH")
  if [[ "$fw_sha" == "$FW_SHA256_EXPECTED" && "$fw_sz" -gt 100000 ]]; then
    ok "IPC4 firmware verified: ${FW_PATH} (${fw_sz} B, sha256=${fw_sha:0:16}...)"
  else
    fail "IPC4 firmware mismatch: size=${fw_sz}, sha256=${fw_sha}"
  fi
else
  fail "IPC4 firmware missing: ${FW_PATH}"
fi

# 3. IPC4 topology presence, SHA256 & binary structure verification
if [[ -r "$TPLG_PATH" ]]; then
  tplg_sha=$(sha256sum "$TPLG_PATH" | awk '{print $1}')
  if [[ "$tplg_sha" == "$TPLG_SHA256_EXPECTED" ]]; then
    ok "IPC4 topology checksum verified: ${TPLG_PATH} (sha256=${tplg_sha:0:16}...)"
  else
    fail "IPC4 topology checksum mismatch: ${tplg_sha}"
  fi
else
  fail "IPC4 topology missing: ${TPLG_PATH}"
fi

if python3 - "$TPLG_PATH" <<'PY'
import struct, sys
data = open(sys.argv[1], "rb").read()
pcms, kctls = set(), {}
off = 0
while off + 36 <= len(data):
    magic, _a, _v, typ, hdr_sz, _vt, pay_sz, _idx, cnt = struct.unpack_from("<9I", data, off)
    assert magic == 0x41536F43, f"Bad tplg magic 0x{magic:08x}"
    pay = data[off + hdr_sz : off + hdr_sz + pay_sz]
    if typ == 7:
        po = 0
        for _ in range(cnt):
            esz = struct.unpack_from("<I", pay, po)[0]
            name = pay[po + 4 : po + 48].split(b"\x00")[0].decode("ascii", "replace").strip()
            pid = struct.unpack_from("<I", pay, po + 92)[0]
            if name:
                pcms.add((pid, name))
            po += esz
    elif typ == 5:
        po = 0
        for _ in range(cnt):
            wsz = struct.unpack_from("<I", pay, po)[0]
            nk = struct.unpack_from("<I", pay, po + wsz - 8)[0]
            psz = struct.unpack_from("<I", pay, po + wsz - 4)[0]
            po += wsz + psz
            for _ in range(nk):
                khsz, ktype = struct.unpack_from("<2I", pay, po)
                kname = pay[po + 8 : po + 52].split(b"\x00")[0].decode("ascii", "replace")
                csz = struct.unpack_from("<I", pay, po + khsz)[0]
                kpsz = struct.unpack_from("<I", pay, po + csz - 4)[0]
                cmax = struct.unpack_from("<I", pay, po + khsz + 4)[0] if ktype == 2 else 0
                kctls[kname] = (ktype, cmax)
                po += csz + kpsz
    off += hdr_sz + pay_sz

assert (2, "Speaker") in pcms, f"Missing PCM 2 Speaker: {pcms}"
assert (3, "Amp feedback") in pcms, f"Missing PCM 3 Amp feedback: {pcms}"
for req in (
    "Post Mixer Speaker Playback Volume",
    "Post Mixer Speaker Playback IIR Eq bytes",
    "Post Mixer Speaker Playback FIR Eq bytes",
    "Post Mixer Speaker Playback DRC bytes",
    "Post Mixer Speaker Playback DRC switch",
):
    assert req in kctls, f"Missing kcontrol '{req}'"
assert kctls["Post Mixer Speaker Playback IIR Eq bytes"] == (2, 1024), kctls["Post Mixer Speaker Playback IIR Eq bytes"]
PY
then
  ok "IPC4 topology parsed cleanly: PCM 2 (Speaker), PCM 3 (Amp feedback), EQIIR(max=1024)/EQFIR/DRC confirmed"
else
  fail "IPC4 topology parse check failed"
fi

# 4. Modprobe drop-in validated against live `modinfo`
MODCONF="${STAGE_DIR}/90-sof-ipc4-xps17.conf"
if grep -Eq '^[[:space:]]*options[[:space:]]+snd[_-]sof[[:space:]]+ipc_type=1([[:space:]]|$)' "$MODCONF"; then
  if modinfo snd-sof 2>/dev/null | grep -Eq '^parm:[[:space:]]+ipc_type:.*IPC4.*\(int\)'; then
    ok "Modprobe drop-in '${MODCONF##*/}' validated against modinfo snd-sof (ipc_type int = 1)"
  else
    fail "modinfo snd-sof does not report ipc_type (int) parameter"
  fi
else
  fail "Modprobe drop-in missing valid 'options snd_sof ipc_type=1' line"
fi

# 5. UCM2 DRC fence & target path verification (read-only)
UCM_FENCE="${STAGE_DIR}/ucm2-drc-fence.conf"
if python3 - "$UCM_SDW" "$UCM_DSP" "$UCM_FENCE" "$VENDOR" "$PRODUCT" <<'PY'
import re, sys
sdw = open(sys.argv[1], encoding="utf-8").read()
dsp = open(sys.argv[2], encoding="utf-8").read()
fence = open(sys.argv[3], encoding="utf-8").read()
vendor, product = sys.argv[4], sys.argv[5]

# Confirm sof-soundwire.conf includes dsp.conf BEFORE calling SofDrcBootSetup
idx_inc = sdw.find('Include.dsp-variables.File "/Intel/sof-hda-dsp/dsp.conf"')
idx_drc = sdw.find("Macro.SpeakerDrc.SofDrcBootSetup")
assert idx_inc != -1 and idx_drc != -1 and idx_inc < idx_drc, "UCM2 include ordering changed"

# Confirm dsp.conf includes user_configs/<vendor>/<product>.conf BEFORE DefineMacro.SofDrcBootSetup
idx_user = dsp.find('SOFUserConfig "/blobs/sof/user_configs/${var:ConfPathFromDMI}"')
idx_macro = dsp.find("DefineMacro.SofDrcBootSetup")
assert idx_user != -1 and idx_macro != -1 and idx_user < idx_macro, "dsp.conf user_configs lookup changed"

# Validate ucm2-drc-fence.conf balanced braces and required directives
depth = 0
for ln in fence.splitlines():
    s = ln.split("#", 1)[0]
    depth += s.count("{") - s.count("}")
    assert depth >= 0, f"Unbalanced closing brace on line: {ln}"
assert depth == 0, f"Unclosed braces in ucm2-drc-fence.conf (depth={depth})"
assert re.search(r'^\s*Define\.PostMixerSpeakerPlaybackDrcSwitch\s+"N/A"\s*$', fence, re.M), "Missing DRC switch fence"
assert re.search(r'^\s*Define\.PostMixerSpeakerPlaybackDrcBlob\s+""\s*$', fence, re.M), "Missing DRC blob fence"
assert "Type Path" in fence and "Mode read" in fence, "Optional EQIIR blob must be guarded by Path condition"

target = f"/usr/share/alsa/ucm2/blobs/sof/user_configs/{vendor}/{product}.conf"
print(f"    resolved UCM2 deploy target: {target}")
PY
then
  ok "UCM2 DRC fence syntax, brace balance, and dsp.conf hook ordering verified"
else
  fail "UCM2 DRC fence verification failed"
fi

# 6. Boot verification script syntax + 5 offline mock execution scenarios (ZERO live system access)
VERIFY_SH="${STAGE_DIR}/xps17-sof-ipc4-verify.sh"
if bash -n "$VERIFY_SH"; then
  ok "Syntax check passed: ${VERIFY_SH##*/}"
else
  fail "Syntax error in ${VERIFY_SH##*/}"
fi

TMPDIR_MOCK=$(mktemp -d)
trap 'rm -rf "$TMPDIR_MOCK"' EXIT

setup_mock_env() {
  local d="$1" sku="$2" ipc="$3" drc_init="$4" drc_sticky="$5" vol_init="$6"
  rm -rf "$d"
  mkdir -p "$d/sys/class/dmi/id" "$d/sys/module/snd_sof/parameters" "$d/proc/asound/sofsoundwire" "$d/state"
  printf '%s\n' "Dell Inc." > "$d/sys/class/dmi/id/sys_vendor"
  printf '%s\n' "XPS 17 9720" > "$d/sys/class/dmi/id/product_name"
  printf '%s\n' "$sku" > "$d/sys/class/dmi/id/product_sku"
  printf '%s\n' "$ipc" > "$d/sys/module/snd_sof/parameters/ipc_type"
  touch "$d/proc/asound/sofsoundwire/pcm2p" "$d/proc/asound/sofsoundwire/pcm3c"
  printf '%s\n' "$drc_init" > "$d/state/drc"
  printf '%s\n' "$drc_sticky" > "$d/state/drc_sticky"
  printf '%s\n' "$vol_init" > "$d/state/vol"

  cat > "$d/mock-amixer" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
STATE_DIR="$(dirname "$0")/state"
cmd="" ctl="" val=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -c) shift 2 ;;
    -q) shift ;;
    cget|cset) cmd="$1"; ctl="${2#name=}"; shift 2; [[ $# -gt 0 ]] && { val="$1"; shift; } ;;
    *) shift ;;
  esac
done
case "$cmd:$ctl" in
  "cget:Post Mixer Speaker Playback DRC switch")
    echo "numid=99,iface=MIXER,name='${ctl}'"
    echo "  : values=$(cat "$STATE_DIR/drc")"
    ;;
  "cset:Post Mixer Speaker Playback DRC switch")
    if [[ "$(cat "$STATE_DIR/drc_sticky")" != "1" ]]; then
      printf '%s\n' "$val" > "$STATE_DIR/drc"
    fi
    ;;
  "cget:Post Mixer Speaker Playback Volume")
    echo "numid=98,iface=MIXER,name='${ctl}'"
    echo "  : values=$(cat "$STATE_DIR/vol")"
    ;;
  "cset:Post Mixer Speaker Playback Volume")
    printf '%s\n' "$val" > "$STATE_DIR/vol"
    ;;
  "cget:Post Mixer Speaker Playback IIR Eq bytes"|"cget:Post Mixer Speaker Playback FIR Eq bytes")
    echo "numid=97,iface=MIXER,name='${ctl}'"
    ;;
  *)
    exit 1
    ;;
esac
MOCK
  chmod +x "$d/mock-amixer"
}

# Case A: Clean IPC4 boot (DRC=off, vol=45,45)
setup_mock_env "$TMPDIR_MOCK/caseA" "0AFF" "1" "off" "0" "45,45"
outA=$("$VERIFY_SH" --root "$TMPDIR_MOCK/caseA" --amixer "$TMPDIR_MOCK/caseA/mock-amixer" --no-sleep --wait-sec 0 2>&1) && rcA=0 || rcA=$?
if [[ $rcA -eq 0 && "$outA" == *"status=OK"* && "$outA" == *"drc=off drc_fenced=0"* ]]; then
  ok "Mock verify Case A (clean IPC4 boot, DRC=off): rc=0"
else
  fail "Mock verify Case A failed: rc=${rcA} out=${outA}"
fi

# Case B: DRC accidentally ON -> emergency fence switches it OFF
setup_mock_env "$TMPDIR_MOCK/caseB" "0AFF" "1" "on" "0" "45,45"
outB=$("$VERIFY_SH" --root "$TMPDIR_MOCK/caseB" --amixer "$TMPDIR_MOCK/caseB/mock-amixer" --no-sleep --wait-sec 0 2>&1) && rcB=0 || rcB=$?
if [[ $rcB -eq 0 && "$outB" == *"status=OK"* && "$outB" == *"drc=off drc_fenced=1"* && "$(cat "$TMPDIR_MOCK/caseB/state/drc")" == "off" ]]; then
  ok "Mock verify Case B (DRC=on auto-fenced to off): rc=0"
else
  fail "Mock verify Case B failed: rc=${rcB} out=${outB}"
fi

# Case C: Sticky DRC refuses to turn OFF -> clamps volume to 38,38 (-14 dB) and exits 21
setup_mock_env "$TMPDIR_MOCK/caseC" "0AFF" "1" "on" "1" "45,45"
outC=$("$VERIFY_SH" --root "$TMPDIR_MOCK/caseC" --amixer "$TMPDIR_MOCK/caseC/mock-amixer" --no-sleep --wait-sec 0 2>&1) && rcC=0 || rcC=$?
if [[ $rcC -eq 21 && "$outC" == *"reason=drc_sticky_clamped_minus14db"* && "$(cat "$TMPDIR_MOCK/caseC/state/vol")" == "38,38" ]]; then
  ok "Mock verify Case C (sticky DRC -> emergency -14 dB volume clamp to 38,38): rc=21"
else
  fail "Mock verify Case C failed: rc=${rcC} out=${outC} vol=$(cat "$TMPDIR_MOCK/caseC/state/vol")"
fi

# Case D: Wrong DMI SKU -> refused immediately (rc=11)
setup_mock_env "$TMPDIR_MOCK/caseD" "0BAD" "1" "off" "0" "45,45"
outD=$("$VERIFY_SH" --root "$TMPDIR_MOCK/caseD" --amixer "$TMPDIR_MOCK/caseD/mock-amixer" --no-sleep --wait-sec 0 2>&1) && rcD=0 || rcD=$?
if [[ $rcD -eq 11 && "$outD" == *"reason=wrong_hardware"* ]]; then
  ok "Mock verify Case D (wrong DMI SKU refused): rc=11"
else
  fail "Mock verify Case D failed: rc=${rcD} out=${outD}"
fi

# Case E: IPC3 active (ipc_type=0) -> fails cleanly (rc=13)
setup_mock_env "$TMPDIR_MOCK/caseE" "0AFF" "0" "off" "0" "45,45"
outE=$("$VERIFY_SH" --root "$TMPDIR_MOCK/caseE" --amixer "$TMPDIR_MOCK/caseE/mock-amixer" --no-sleep --wait-sec 0 2>&1) && rcE=0 || rcE=$?
if [[ $rcE -eq 13 && "$outE" == *"reason=ipc_type_not_ipc4"* ]]; then
  ok "Mock verify Case E (IPC3 detected -> fails ipc_type_not_ipc4): rc=13"
else
  fail "Mock verify Case E failed: rc=${rcE} out=${outE}"
fi

# 7. Systemd service unit structural verification
UNIT_FILE="${STAGE_DIR}/xps17-sof-ipc4-verify.service"
if python3 - "$UNIT_FILE" <<'PY'
import configparser, sys
cp = configparser.ConfigParser(interpolation=None)
cp.optionxform = str
cp.read(sys.argv[1], encoding="utf-8")
for sec in ("Unit", "Service", "Install"):
    assert sec in cp, f"Missing [{sec}]"
assert "sound.target" in cp["Unit"]["After"]
assert "display-manager.service" in cp["Unit"]["Before"]
assert cp["Unit"]["ConditionPathExists"] == "/etc/modprobe.d/90-sof-ipc4-xps17.conf"
assert cp["Service"]["Type"] == "oneshot"
assert "xps17-sof-ipc4-verify.sh" in cp["Service"]["ExecStart"]
assert cp["Install"]["WantedBy"] == "multi-user.target"
PY
then
  ok "Systemd unit '${UNIT_FILE##*/}' structure & boot ordering verified"
else
  fail "Systemd unit verification failed"
fi

# 8. Staged EQIIR blobs & metadata SHA256 integrity
if python3 - "${SOF_DIR}/blobs" <<'PY'
import hashlib, json, sys
from pathlib import Path
bdir = Path(sys.argv[1])
meta = json.loads((bdir / "blobs_metadata.json").read_text(encoding="utf-8"))
for fname, info in meta["blobs"].items():
    raw = (bdir / fname).read_bytes()
    assert len(raw) == info["total_size_bytes"], f"{fname} size mismatch"
    assert hashlib.sha256(raw).hexdigest() == info["sha256"], f"{fname} sha256 mismatch"
assert len(meta["blobs"]) == 3
PY
then
  ok "Staged SOF IPC4 EQIIR blobs (3 files) verified against blobs_metadata.json SHA256s"
else
  fail "Staged EQIIR blob integrity check failed"
fi

echo "=== Summary: ${PASS_COUNT} passed, ${FAIL_COUNT} failed ==="
[[ $FAIL_COUNT -eq 0 ]]
