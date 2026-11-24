# Lead 1 Report — Hardware, Kernel Driver, SoundWire & SOF IPC4 Systems Architecture

**Platform:** Dell XPS 17 9720 (`DMI product_sku = 0AFF`, `SUBSYS 0AFF1028`, codename `Stradale`)
**Target Kernel:** `7.2.15-1-omarchy` (`x86_64`, Alder Lake SOF + dual Realtek `ALC1316`/`ALC1319D` on SoundWire Links 1 & 2)
**Scope:** `/home/akshaybapat/project/xps17-studio-audio/kernel-driver/` (100% offline/read-only engineering; zero live kernel/system/audio mutations; stock module `srcversion=4899F687DBDD6FA29B41276` untouched)
**Build Environment:** E-cores only (`taskset -c 12-19 nice -n 19`), shared compile lock (`/tmp/xps17-audio-build.lock`), `systemd-run --user --scope -p CPUQuota=140% -p MemoryMax=4G`, CPU package temperature `53°C` (< 82°C ceiling).

---

## 1. Executive Summary & Overall Verification Matrix

All 4 specialized sub-agent workstreams completed and were independently rebuilt and re-verified by Lead 1 on E-cores:

| Subsystem | Sub-Agent | Primary Artifacts | Fresh Lead 1 Verification |
|---|---|---|---|
| **Silicon & Windows Audit** | `rt1316-silicon-auditor` | `audit/SILICON-AND-WINDOWS-AUDIT.md`, `audit/waves_stradale_summary.json`, `audit/analyze_windows_tables.py` | **PASS** — `analyze_windows_tables.py` exit `0`, 100% assertions green (40 EFX / 181 MFX / 520 SFX BPAM blocks, 0 trailing bytes) |
| **Upgraded RT1316 Kernel Driver (P1–P4)** | `kernel-driver-builder` | `rt1316-xps17/{rt1316-sdw.{c,h},rt1316-xps17-policy.h,rt1316-xps17-bq-tables.h,gen_bq_tables.py,Makefile,Kbuild,dkms.conf,dkms-pre-install.sh,patches/*,snd-soc-rt1316-sdw.ko}` | **PASS** — Clean `W=1` rebuild exit `0`, **0 warnings / 0 errors**, clean BTF, exact `vermagic`, bit-reproducible `sha256=49fff38d...`, `gen_bq_tables.py check` PASS |
| **SOF IPC4, UCM2 DRC Fence, Hardware EQIIR & P4 Guard Bridge** | `sof-ipc4-topology-engineer` | `sof-ipc4/{SOF-IPC4-DESIGN.md,gen_sof_eqiir_blob.py,test_sof_eqiir_blob.py,blobs/*,ipc4-stage/*,guard-bridge/*}` | **PASS** — `stage-ipc4-check.sh`: **14/14 PASS**; `unittest`: **20/20 PASS** (`0.297s`) |
| **Kernel Safety, ELF/Symbol & Staged Script Verification** | `kernel-safety-verifier` | `verify/{VERIFY-REPORT.md,test_rt1316_policy.c,verify_ko_elf.sh,test_scripts_mock.sh,test_mutants.sh,run_all_verify.sh,scripts/*}` | **PASS** — **346,226 C checks (ASan+UBSan, 0 fails)**, **18/18 ELF/static checks**, **27/27 mock-harness tests**, **13/13 mutants killed (100%)**, real-artifact sandbox dry-runs PASS |

---

## 2. Key Technical Findings & Verified Architecture

### 2.1 Silicon & Windows Reverse-Engineering (`kernel-driver/audit/`)
1. **In-Amp Biquad Crossover (`0xC202` + `0xC200`) & 134-Write Table (`0xC203..0xC252`):**
   - Windows `EP001_Open/OpenCtl.txt:3-4` activates the crossover with `0xC202 = 0xC0` (enable BQ1/BQ2) followed by `0xC200 = 0xC0` (update pulse), and closes cleanly via `CloseCtl.txt:8-9` (`0xC202 = 0x00`, `0xC200 = 0xC0`).
   - Live Linux dumps prove `0xC202 = 0x00` on **both** amplifiers (`amp{1,2}.registers.txt:538`), and Amp 2 missed the 134-write table (`34/134` accidental matches, reset identity coefficients `0x08000000`).
   - All 4 filters in `dell_0b00_bq_params` (134 writes / 402 bytes across `0xC203..0xC252`, `0xC00B=0x30`, `0xC380..0xC3A3`, `0xC261..0xC270`, `0xC200=0xC0`) re-derive to **0 LSB error** from `0xC380..0xC3A3` via RBJ at `44.1 kHz` while the hardware runs at `48 kHz`:
     - `SPOL` woofer LPF `-3 dB` at `6164.0 Hz` (`5674.1 Hz` @ 48 kHz-corrected) + subsonic HPF `17.11 Hz` (`+1.253 dB @ 30.8 Hz`).
     - `SPOR` tweeter HPF `-3 dB` at `838.4 Hz` (`770.2 Hz` @ 48 kHz-corrected, `+1.557 dB @ 1467 Hz`, `-41.26 dB @ 100 Hz`, `-29.25 dB @ 200 Hz`).
   - **Critical regcache bug prevented:** `0xC200`, `0xC202`, and `0xC60F` are all marked volatile in `rt1316_volatile_register()` so `regcache_sync()` can never replay `0xC200 = 0xC0` before `0xC203..0xC252` or replay unlocked HV gain.
2. **Amp 2 IV-Sense Slot De-Collision (`0x3004` / `0x3005`):**
   - Stock Linux writes `0x3004=0xA0`, `0x3005=0xB1` on both amps (colliding on aggregated SoundWire capture). Windows writes `0xA0/0xB1` on LL (Link 1) and **`0xE4/0xF5`** on RR (Link 2).
   - Sense clock note: Windows also sets `0xC007/0xC008=0x22` (`24 kHz`), `0x2F0A=0x01`, `PDE24=0x00` vs Linux `0x11`, `0x00`, `0x03`; nibble-order slot polarity (I vs V) must be confirmed once live IPC4 PCM 3 capture runs.
3. **Gain Staging & Forbidden Protection Registers:**
   - Linux stock `0xC60F=0x01` + `0xD002/4=0xBF` vs Windows Basic `0xC60F=0x05` + `0xD002/4=0xA9`. Because `Basic_cmd.txt` vs `Advance_cmd.txt` comments disagree on digital gain direction, **both** `0xC60F=0x05` and `0xD002/4=0xA9` stay strictly gated behind P4's 250 ms deadman watchdog (Advance `0xCA` permanently forbidden).
   - All 9 forbidden protection/test/reset registers (`0xC000`, `0xC050`, `0xC082`, `0xC093`, `0xC09C`, `0xC09D`, `0xC0A3`, `0xC102`, `0xC60E`) are permanently blocked by policy and untouched across every new code path.

### 2.2 Upgraded `snd-soc-rt1316-sdw` v7.2.15.2 Driver (`kernel-driver/rt1316-xps17/`)
Clean 4-patch quilt series verified byte-for-byte against untouched `7.2.15-1-omarchy` base source:
- **P1 (`0001-p1-bq-in-io-init.patch`):** Applies the 134-write BQ table in both `rt1316_io_init()` and `rt1316_sdw_component_probe()` under `init_lock`; hardens DT parsing (`devm_krealloc`, `cnt % 3` check); clean runtime PM refcounting.
- **P2 (`0002-p2-xu24-truthful.patch`):** Fixes `XU24 Bypass` (`0x41001208`) `reg_default = 0x00` to match hardware init.
- **P3 (`0003-p3-crossover-enable-and-iv-slot-decollision.patch`):** Dell `0AFF` DMI-gated (`dmi_match(DMI_PRODUCT_SKU, "0AFF")`):
  - Enables hardware BQ crossover (`0xC202=0xC0` + `0xC200=0xC0`) in `SND_SOC_DAPM_POST_PMU` and cleanly disables (`0xC202=0x00` + `0xC200=0xC0`) in `PRE_PMD` / suspend.
  - Exposes `"BQ Crossover Switch"` (R/W bool, default ON on `0AFF`, live `0xC202` chip readback when powered) and `"BQ Crossover Profile"` (`"Stock Dell"` default vs `"Precision 48kHz LR2/BW2"` at `Fc=2400 Hz` BW2 + `20 Hz` BW2 HPF: peak `-0.0006 dB` SPOL / `0.0000 dB` SPOR, acoustic power sum `[-0.0070, 0.0000] dB`, `r_max=0.998151`).
  - Sets Amp 2 (`slave->bus->link_id == 2`) IV-sense tags to `0x3004=0xE4`, `0x3005=0xF5` (Amp 1 stays `0xA0/0xB1`).
- **P4 (`0004-p4-deadman-guard-drive-gate.patch`):** Asahi `macaudio.c`-style 250 ms deadman kernel gate:
  - Exposes `"Guard Drive Unlock"` (R/W volatile integer: write `0xdec1be15` while playback stream is active to unlock/extend 250 ms watchdog; write `0` or any invalid magic to immediately force-lock) and `"Guard Drive Active"` (R/O volatile bool).
  - Uses `struct timer_list` (`timer_container_of`, `timer_delete_sync`) + `struct work_struct` (`cancel_work_sync`) + leaf `spinlock_t` (zero sleeping in softirq; strict lock ordering).
  - Glitch-safe ordering (lowers digital before raising HV on unlock; drops HV before restoring digital on lock), bypasses regcache on gain writes, and atomically reverts to stock (`0xC60F=0x01`, `0xD002/0xD004=0xBF`) on 250 ms timeout, bad magic, PCM `shutdown`, Class-D `PRE_PMD`, PM `suspend`, SoundWire re-init, or driver `remove`.
- **Compiled Module Evidence:**
  - `W=1 clean modules`: **0 warnings, 0 errors**, clean BTF.
  - `vermagic: 7.2.15-1-omarchy SMP preempt mod_unload ` (exact match to stock `.ko.zst` and `uname -r`).
  - `srcversion: 6B1D5DCE07931F20A0763FD`, `sha256: 49fff38defb223acd145572dd59c48d23ade879e2bf2df02d67c5a3b5786155b`.
  - `dkms.conf` (`PACKAGE_NAME="snd-soc-rt1316-sdw"`, `PACKAGE_VERSION="7.2.15.2"`, `BUILD_EXCLUSIVE_KERNEL="^7\.2\.15-1-omarchy$"`, `PRE_INSTALL="dkms-pre-install.sh"` checking XU24 safety state).

### 2.3 Intel SOF IPC4 Topology, UCM2 DRC Fence, Hardware EQIIR & P4 Guard Bridge (`kernel-driver/sof-ipc4/`)
- **Firmware & Topology Verified on Disk (Read-Only):**
  - `/usr/lib/firmware/intel/sof-ipc4/adl/sof-adl.ri` (`707036 B`, `sha256=351325786e53ca7c...`)
  - `/usr/lib/firmware/intel/sof-ipc4-tplg/sof-adl-rt711-l0-rt1316-l12-rt714-l3.tplg` (`79002 B`, `sha256=4d823c4643cb253c...`) — exposes PCM 2 (`Speaker`), PCM 3 (`Amp feedback`, 4-ch IV-sense), `eqiir.21.1` (`max=1024`), `eqfir.21.1`, `drc.21.1`.
  - **Notable discovery:** Stock IPC4 topology defaults `eqiir.21.1` to a 100 Hz 2nd-order HPF; deploying our `eqiir_subsonic_80hz_safety.bin` both lowers the hardware floor cleanly to 80 Hz (matching Lead 2/3's `Physical Bass Hz = 82 Hz`) and steepens protection to 4th-order Butterworth (`-24.10 dB @ 40 Hz`).
- **UCM2 DRC Fence & Boot Verifier (`ipc4-stage/`):**
  - `90-sof-ipc4-xps17.conf`: `options snd_sof ipc_type=1` (validated against `modinfo snd-sof`).
  - `ucm2-drc-fence.conf`: Targets `/usr/share/alsa/ucm2/blobs/sof/user_configs/Dell Inc./XPS 17 9720.conf` (included before `sof-soundwire.conf:258`) setting `Define.PostMixerSpeakerPlaybackDrcSwitch "N/A"` + `Define.PostMixerSpeakerPlaybackDrcBlob ""`, completely preventing the dangerous `+13.7 dB` makeup DRC from ever turning on.
  - `xps17-sof-ipc4-verify.{sh,service}`: Double-reads DRC state on boot, auto-fences OFF, and clamps speaker volume to `38,38` (`-14 dB`, exit `21`) if sticky.
- **Bit-Exact SOF IPC4 Hardware-DSP `EQIIR` Blobs (`blobs/`):**
  - Verified against Linux `7.2.15-1-omarchy` headers, `snd-sof.ko.zst` disassembly (`tlv_type == 3`, `magic == "SOF4"`), upstream `alsa-ucm-conf` blobs, and 64-bit integer DF2T time-domain simulation (zero clipping, `< 0.01 dB` RMS error):
    - `eqiir_subsonic_80hz_safety.bin` (156 B): 4th-order Butterworth 80 Hz HPF (`40 Hz: -24.10 dB`, `80 Hz: -3.01 dB`, `peak: +0.0000 dB`, `max|p|=0.996001`, Q2.30 err `0.0024 dB`).
    - `eqiir_subsonic_plus_preshape.bin` (184 B): 80 Hz 4th-order HPF + `-2.5 dB` Q=1.4 cut at `1.5 kHz` (`peak: -0.0009 dB`).
    - `eqiir_passthrough_flat.bin` (128 B): unity bypass for A/B rollback.
- **Thermal/IV-Sense P4 Deadman Bridge (`guard-bridge/`):**
  - `p4_deadman_bridge.py` connects `speaker_guard.Guard` to `rt1316-1 Guard Drive Unlock` and `rt1316-2 Guard Drive Unlock` (`0xdec1be15`, 50 ms heartbeat <= 100 ms vs 250 ms kernel watchdog), enforcing 7 simultaneous fail-closed invariants (trusted calibration, non-colliding IV slots, fresh dual-amp frames with `xrun_count==0`, DRC off, `T_coil < 70°C` + hysteresis/holdoff/flap-lockout, `excursion < 0.90 Xmax`, stream active), symmetric rollback on partial ALSA failure, and instant `0` lock on `SIGTERM`/`SIGINT`/trip/idle.

### 2.4 Verification, ELF/Symbol Audit & Safe Staged Scripts (`kernel-driver/verify/`)
- **`test_rt1316_policy.c` (ASan+UBSan, `-Wall -Wextra -Werror -Wpedantic`):** `346,226 checks, 0 failures` (full 16-bit + SDCA forbidden/allowlist disjointness, 2000-pt 20 Hz–20 kHz crossover Jury stability & gain bounds, IV-slot pairwise uniqueness, exhaustive P4 250 ms deadman state machine including all 32 single-bit magic flips, wraparound, suspend, close, and NULL guards).
- **`verify_ko_elf.sh`:** `18/18 PASS, 0 warnings` (static diff scan of 786 added lines -> 0 forbidden writes, 0 debugfs `access`/`range`, exact `vermagic`, GPL license, 0 `EXPORT_SYMBOL` leaks, `66/66` undefined symbols in `Module.symvers`, `dmi_match` + `mod_timer` + 3 `objdump` call sites to `timer_delete_sync`, all ALSA control strings verified).
- **Hardened Staged Scripts (`verify/scripts/{swap-in.sh,swap-out.sh,dkms-install.sh,route-timer-guard.sh,lib.sh}`):** Mandatory `0AFF` DMI SKU check, mandatory `--dry-run` vs `--confirm-live` gate, `omarchy-audio-route-recovery.timer` pause/restore `EXIT` trap, automatic mid-swap rollback, XU24 state & thermal gates — verified with **27/27 mock-harness tests**, **13/13 mutants killed (100%)**, and real-artifact isolated sandbox dry-runs.

---

## 3. Cross-Team Handoffs (For Lead 2, Lead 3, Lead 4 & Coordinator)

1. **For Lead 2 (Acoustics) & Lead 3 (DSP/ASM) — Newly Decoded Dell/Waves `StradaleMLK` Benchmarks (`audit/waves_stradale_summary.json`):**
   - `MFX[0] LINKLAWS` (432 B decoded, 6 volume-adaptive curves `0..100%`): `64 Hz` EQ holds `-0.2 dB` up to 75% volume then ducks to `-4.5 dB` at 100%; `MaxxBass` center `103.2 Hz` (`88 Hz` in NX subtype) scales intensity ratio `1.61` (≤45% vol) -> `0.46` (≥75% vol); `MV2` woofer excursion limiter tracks `82–93 Hz` with `1 ms` attack.
   - `SFX[100] Nx Speakers` XTC geometry: `S = 34 cm`, `D = 72 cm`, **`XTC_LF_SPLIT_FREQ = 351 Hz` (`Q = 1.6`)**, `N360_BASS_CUT = 80 Hz (+3.0 dB)`.
   - `EFX[0]` asymmetric L/R chassis resonance notch clusters: `344–382 Hz`, `778–928 Hz`, `1.0–1.4 kHz`, `1.73–1.88 kHz`, `3.11–3.14 kHz` (down to `-9.5 dB`, `-1.0 dB` output trim).
   - `RTSPKPT` candidate thermal model constants: `Re0 = 4.0 Ω`, `T0 = 25°C`, `Tmax = 100°C`, staged knees `80 / 70 / 60°C`.
   - **Important acoustic interplay:** Once our P3 driver activates `0xC202=0xC0`, stock Dell tweeter HPF (`838 Hz`) + woofer LPF (`6164 Hz`) immediately cleans bass out of the tweeters (and optional `"Precision 48kHz LR2/BW2"` aligns both drivers cleanly at `2400 Hz` @ 48 kHz with 0 dB peaking). Also remember stock IPC4 `eqiir.21.1` defaults to a 100 Hz HPF — deploy `blobs/eqiir_subsonic_80hz_safety.bin` so the hardware DSP does not cut 80–100 Hz bass!
2. **For Coordinator / Lead 4 — Safe Staged Deployment Order (Offline Today; Atomic Deploy When Authorized):**
   1. Stage `sof-ipc4/ipc4-stage/ucm2-drc-fence.conf` -> `/usr/share/alsa/ucm2/blobs/sof/user_configs/Dell Inc./XPS 17 9720.conf` **BEFORE** enabling IPC4 (guarantees `+13.7 dB` DRC never activates).
   2. Stage `verify/scripts/dkms-install.sh --dry-run` -> `--confirm-live` (or hot-test with `swap-in.sh --dry-run` -> `--confirm-live`, instant rollback via `swap-out.sh --confirm-live`).
   3. Stage `sof-ipc4/ipc4-stage/{90-sof-ipc4-xps17.conf,xps17-sof-ipc4-verify.{sh,service}}` and `blobs/eqiir_subsonic_80hz_safety.bin` for IPC4 boot + PCM 3 4-ch IV-sense verification.
   4. Only after live PCM 3 4-ch IV-sense calibration is proven, enable `guard-bridge/omarchy-speaker-guard-p4.conf` to feed the P4 250 ms deadman watchdog (`0xdec1be15`).
