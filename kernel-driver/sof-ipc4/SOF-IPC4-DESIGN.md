# Intel SOF IPC4 Topology, Hardware-DSP EQIIR & Thermal P4 Guard Bridge Design
**Target Platform:** Dell XPS 17 9720 (`sys_vendor = Dell Inc.`, `product_name = XPS 17 9720`, `product_sku = 0AFF`)  
**Kernel:** `7.2.15-1-omarchy` (`/lib/modules/7.2.15-1-omarchy/build`)  
**Workspace:** `/home/akshaybapat/project/xps17-studio-audio/kernel-driver/sof-ipc4/`  
**Status:** Strictly staged offline & verified via automated test suites (zero live system modifications).

---

## 1. Verified Firmware, Topology & Signal-Path Inventory

All binaries below were inspected read-only on the target machine:

| Artifact | Path | Size | SHA-256 | Verified Metadata |
|---|---|---|---|---|
| SOF IPC4 Firmware | `/usr/lib/firmware/intel/sof-ipc4/adl/sof-adl.ri` (`-> intel-signed/sof-adl.ri`) | 707,036 B | `351325786e53ca7cd23ffc1462d82913f3e0c08ad78f89053d85e4b6703cf12b` | `SOF:v2.12`, `zephyr:v4.0.0-1916-g7fa8bf17563b` |
| SOF IPC4 Topology | `/usr/lib/firmware/intel/sof-ipc4-tplg/sof-adl-rt711-l0-rt1316-l12-rt714-l3.tplg` | 79,002 B | `4d823c4643cb253c63ab793d18e101668ad55f0f5813e324f2832a837abdfe13` | `CoSA` magic, ABI `3.26.0` |
| Kernel Module | `/lib/modules/7.2.15-1-omarchy/kernel/sound/soc/sof/snd-sof.ko.zst` | — | — | `parm: ipc_type:Force SOF IPC type. 0 - IPC3, 1 - IPC4 (int)` |

### Verified IPC4 Speaker & IV-Sense DAPM Graph (Parsed Directly from `.tplg`)

```
Playback (PCM 2 "Speaker"):
  host-copier.2.playback
    -> gain.20.1 ("Pre Mixer Speaker Playback Volume")
    -> mixin.20.1 -> mixout.21.1
    -> gain.21.1 ("Post Mixer Speaker Playback Volume", 0..45 = -90..0 dB, 2 dB steps)
    -> eqiir.21.1 ("Post Mixer Speaker Playback IIR Eq bytes", access=0x10000030, max=1024 B)
    -> eqfir.21.1 ("Post Mixer Speaker Playback FIR Eq bytes", access=0x10000030, max=1024 B)
    -> drc.21.1   ("Post Mixer Speaker Playback DRC bytes" / "Post Mixer Speaker Playback DRC switch")
    -> module-copier.21.22 -> alh-copier.SDW1-Playback.0  (+ virtual.sdw-amp -> alh-copier.SDW1-Playback.1)

IV-Sense Feedback Capture (PCM 3 "Amp feedback", 4 ch, 48 kHz, S32_LE / 24-in-32):
  alh-copier.SDW1-Capture.0 -> host-copier.3.capture
```

### Critical Findings from Direct Binary Topology Inspection
1. **Default `eqiir.21.1` blob in stock IPC4 topology is ALREADY a 100 Hz 2nd-order HPF:** The 120-byte `priv` payload embedded in `eqiir.21.1` is byte-for-byte identical to `/usr/share/alsa/ucm2/blobs/sof/ipc4/eq_iir/highpass_100hz_0db_48khz.bin[8:]`. Without overriding it, stock IPC4 rolls off physical bass (`-3.0 dB` at 100 Hz, `-5.4 dB` at 80 Hz). Replacing it with `blobs/eqiir_subsonic_80hz_safety.bin` moves the corner down to a steeper 4th-order Butterworth 80 Hz subsonic safety floor (`-0.67 dB` at 100 Hz, `-3.01 dB` at 80 Hz, `-24.10 dB` at 40 Hz).
2. **Dangerous `+13.7 dB` DRC activated by ALSA UCM2 on IPC4 init:** `/usr/share/alsa/ucm2/sof-soundwire/sof-soundwire.conf:258` invokes `Macro.SpeakerDrc.SofDrcBootSetup "endpoint='PostMixerSpeaker' direction='Playback'"` from `/usr/share/alsa/ucm2/Intel/sof-hda-dsp/dsp.conf:162-191`, whose `BootSequence` runs `cset "name='Post Mixer Speaker Playback DRC switch' on"` whenever that control exists (true under IPC4, absent under IPC3).

---

## 2. SOF IPC4 Staging & Multi-Layer DRC Fence (`ipc4-stage/`)

| File | Deploy Target | Role |
|---|---|---|
| `ipc4-stage/ucm2-drc-fence.conf` | `/usr/share/alsa/ucm2/blobs/sof/user_configs/Dell Inc./XPS 17 9720.conf` | **Layer 1 (Preventative Fence):** Included by `dsp.conf:141-149` BEFORE `SofDrcBootSetup` runs. Overrides `Define.PostMixerSpeakerPlaybackDrcSwitch "N/A"` and `Define.PostMixerSpeakerPlaybackDrcBlob ""`, making UCM2's `ControlExists` check evaluate false so the DRC is NEVER switched ON. Also conditionally loads `eqiir_subsonic_80hz_safety.bin` via `Condition { Type Path Mode read ... }` if deployed into `/usr/share/alsa/ucm2/blobs/sof/ipc4/eq_iir/`. |
| `ipc4-stage/90-sof-ipc4-xps17.conf` | `/etc/modprobe.d/90-sof-ipc4-xps17.conf` | Sets `options snd_sof ipc_type=1` (verified via `modinfo snd-sof`). Install **ONLY AFTER** `ucm2-drc-fence.conf` is in place. |
| `ipc4-stage/xps17-sof-ipc4-verify.sh` | `/usr/local/libexec/xps17-studio-audio/xps17-sof-ipc4-verify.sh` | **Layer 2 (Boot-time Verification & Emergency Fence):** Enforces DMI SKU `0AFF`, `ipc_type == 1`, PCM 2/3 presence, EQIIR/EQFIR/DRC kcontrol presence, verifies DRC stays `off` across two reads, auto-fences `off` if found `on`, and if sticky immediately clamps `Post Mixer Speaker Playback Volume` to `38,38` (`-14 dB`, strictly outweighing `+13.7 dB` DRC makeup) and exits `21`. |
| `ipc4-stage/xps17-sof-ipc4-verify.service` | `/etc/systemd/system/xps17-sof-ipc4-verify.service` | Ordered `After=systemd-udev-trigger.service systemd-modules-load.service alsa-restore.service sound.target` and `Before=display-manager.service` (`ConditionPathExists=/etc/modprobe.d/90-sof-ipc4-xps17.conf`). |
| `ipc4-stage/stage-ipc4-check.sh` | *(offline preflight script)* | Strictly READ-ONLY 14-point self-test validating DMI, firmware/topology SHA-256 & binary parse, `modinfo`, UCM2 hook ordering & brace balance, 5 mock `xps17-sof-ipc4-verify.sh` scenarios, systemd unit structure, and EQIIR blob SHA-256s. |

---

## 3. Verified SOF IPC4 `EQIIR` Binary ABI & Generated Blobs (`gen_sof_eqiir_blob.py`, `blobs/`)

### Exact Linux 7.2.15 Kernel ABI Proof
Disassembly of `sof_ipc4_bytes_ext_put` and `sof_ipc4_set_get_bytes_data` inside `/lib/modules/7.2.15-1-omarchy/kernel/sound/soc/sof/snd-sof.ko.zst` together with `include/uapi/sound/sof/{abi.h,header.h}` and `include/sound/sof/control.h` verifies every byte of the wire format:

1. **ALSA Extended Bytes TLV Header (8 bytes, `<2I`, offset `0x00`):**
   - `tlv_type = 3` (`SOF_CTRL_CMD_BINARY`, strictly checked at `sof_ipc4_bytes_ext_put+0x9a`: `cmpl $0x3, (%rsp)`)
   - `tlv_length = 32 + payload_size` (must satisfy `tlv_length <= 1024`, the `max` of `eqiir.21.1`)
2. **`struct sof_abi_hdr` (32 bytes, `<8I`, offset `0x08`):**
   - `magic = 0x34464F53` (`SOF_IPC4_ABI_MAGIC` `"SOF4"`, strictly checked at `sof_ipc4_bytes_ext_put+0xc3`: `cmpl $0x34464f53, 0x8(%rsp)`)
   - `type = 0` (`param_id = 0`, shifted `<< 20` into `SOF_IPC4_MOD_EXT_MSG_PARAM_ID`)
   - `size = payload_size` (bytes of `struct sof_eq_iir_config`)
   - `abi = 0x0301A000` (`SOF_ABI_VER(3, 26, 0)`, `SOF_ABI_MAJOR == 3` compatible with kernel `3.23.1` and identical to topology default)
   - `reserved[4] = {0, 0, 0, 0}`
3. **`struct sof_eq_iir_config` (offset `0x28`):**
   - `size (u32)`, `channels_in_config = 2 (u32)`, `number_of_responses = 1 (u32)`, `reserved[4] = 0 (4×u32)`
   - `assign_response[2] (2×s32)`: `[0, 0]` active stereo, `[-1, -1]` hardware bypass
4. **`struct sof_eq_iir_header_df2t` (24 bytes, offset `0x4C`):**
   - `num_sections (u32)`, `num_sections_in_series = num_sections (u32)`, `reserved[4] = 0 (4×u32)`
5. **`struct sof_eq_iir_biquad_df2t[num_sections]` (28 bytes/section, `<7i`, offset `0x64`):**
   - `[a2, a1, b2, b1, b0, output_shift, output_gain]` where `a2 = round(-a2_std * 2^30)` and `a1 = round(-a1_std * 2^30)` (SOF DF2T feedback sign convention), `b{0,1,2}` are scaled by `1 / (gain_q14 / 2^14)` into Q2.30, `output_shift = 0`, and `output_gain = 32690` (Q2.14 = `1.9952392578125`, providing 6 dB internal accumulator headroom matching upstream SOF).
   - **Ascending-Q Section Ordering:** The 4th-order Butterworth HPF places `Q = 0.5412` first and `Q = 1.3066` second so NO intermediate stage ever exhibits resonant gain > `0.00 dB`, eliminating fixed-point inter-stage clipping.

### Generated Blobs Summary (`fs = 48000 Hz`, Full Details in `blobs/blobs_metadata.json`)

| Blob | Size | Sections | Max `|pole|` | Peak Gain | Inter-Stage Peak | Max Q2.30 Err (20 Hz–20 kHz) | 40 Hz | 80 Hz | 1.5 kHz | SHA-256 |
|---|---|---|---|---|---|---|---|---|---|---|
| `blobs/eqiir_subsonic_80hz_safety.bin` | 156 B | 2 (4th-ord Butt HPF 80 Hz) | `0.996001` | `0.0000 dB` | `0.0000 dB` | `0.002411 dB` | `-24.10 dB` | `-3.01 dB` | `+0.00 dB` | `7f81675241a604f5...50cee` |
| `blobs/eqiir_subsonic_plus_preshape.bin` | 184 B | 3 (80 Hz HPF + 1.5 kHz `-2.5 dB` Q=1.4) | `0.996001` | `-0.0009 dB` | `0.0000 dB` | `0.002411 dB` | `-24.10 dB` | `-3.01 dB` | `-2.50 dB` | `d52d49821711bb52...1b4b5` |
| `blobs/eqiir_passthrough_flat.bin` | 128 B | 1 (bypass `[-1,-1]` + unity) | `0.000000` | `0.0000 dB` | `0.0000 dB` | `0.000000 dB` | `0.00 dB` | `0.00 dB` | `0.00 dB` | `e1f22053a639517d...53e2` |

*Acoustic grounding for `eqiir_subsonic_plus_preshape.bin`*: Measured Dell XPS 17 9720 acoustic baseline (`/home/akshaybapat/project/omarchy-audio-tune/REPORT.md:30`, 455 captures, 0.076 dB repeatability) documents a harsh/shouty `+4.2 dB` cabinet/driver resonance centered at `1.5 kHz`. Applying a gentle `-2.5 dB` Q=1.4 hardware pre-cut preserves strictly `<= 0.00 dB` peak gain everywhere while offloading primary resonance taming to the DSP.

---

## 4. Thermal & IV-Sense Guard Bridge to Kernel P4 (`guard-bridge/`)

`guard-bridge/p4_deadman_bridge.py` (zero external dependencies) bridges `omarchy-speaker-guard`'s two-node voice-coil thermal estimator (`sense/design/guard/speaker_guard/`) and woofer excursion estimator to the patched `snd-soc-rt1316-sdw` kernel driver (`kernel-driver/rt1316-xps17/rt1316-xps17-policy.h`).

### Exact Kernel P4 Contract Alignment
- **Controls:** `rt1316-1 Guard Drive Unlock` (Link 1, Left) and `rt1316-2 Guard Drive Unlock` (Link 2, Right)
- **Unlock Magic:** `0xdec1be15` (`3737239061`, `RT1316_XPS17_GUARD_MAGIC`)
- **Explicit Instant Lock:** `0` (immediately reverts `0xC60F=0x01`, `0xD002/0xD004=0xBF`)
- **Heartbeat Cadence:** `50 ms` default (hard config validation cap `<= 100 ms`), yielding 5 kicks per `250 ms` kernel deadman window (`RT1316_XPS17_GUARD_TIMEOUT_MS = 250`).

### All 7 Fail-Closed Userspace Safety Invariants
1. **Trusted calibration only:** `calibration_trusted == True` (`Model.elevation_allowed()`; rejects `PLACEHOLDER` / `PROVISIONAL`).
2. **Non-colliding 4-ch IV-sense slots:** `IvSenseSlots.non_colliding()` verifies 4 distinct tag bytes and lower-nibble slot indices across Link 1 (`0xA0/0xB1`) and Link 2 (`0xE4/0xF5`), rejecting Linux's stock `0xA0/0xB1` collision.
3. **Fresh, plausible IV-sense frame for BOTH amps:** `amp_present == (True, True)`, `age <= 150 ms < 250 ms`, `plausible == True`, `xrun_count == 0`, sample rate within `48000 Hz +/- 5%`.
4. **DRC fence intact:** `drc_switch_off == True`.
5. **Voice-coil thermal ceiling with hysteresis:** Trips and sends explicit `0` immediately at `T_coil >= 70.0 C` (`t_limit 100 - t_margin 10 - t_window 20`), requires `T_coil <= 67.0 C` (`3.0 C` hysteresis) plus `10.0 s` continuous healthy holdoff and predicted steady-state check to re-unlock, with anti-flapping lockout (`>= 3` fault trips in `600 s` -> `600 s` lockout).
6. **Excursion guard with hysteresis:** Trips immediately at `x_peak >= 0.90 * X_max`, re-enables only at `<= 0.80 * X_max`.
7. **Stream active & symmetric fail-closed writes:** Stream idle immediately sends `0`. If `rt1316-2` write fails after `rt1316-1` succeeded, `rt1316-1` is immediately rolled back to `0`. `SIGTERM`/`SIGINT` handlers, context-manager exit, and `guard-bridge/omarchy-speaker-guard-p4.conf`'s `ExecStopPost` all enforce explicit `0` lock on shutdown.

---

## 5. Atomic Deployment & Rollback Runbook (For Coordinator Review Only)

**Order of operations is safety-critical — ALWAYS install the UCM2 DRC fence BEFORE enabling IPC4:**

1. Deploy UCM2 DRC fence first:
   ```bash
   sudo install -D -m 0644 ipc4-stage/ucm2-drc-fence.conf \
     "/usr/share/alsa/ucm2/blobs/sof/user_configs/Dell Inc./XPS 17 9720.conf"
   ```
2. *(Optional)* Stage hardware 80 Hz subsonic safety HPF blob for automatic UCM2 load:
   ```bash
   sudo install -D -m 0644 blobs/eqiir_subsonic_80hz_safety.bin \
     /usr/share/alsa/ucm2/blobs/sof/ipc4/eq_iir/eqiir_subsonic_80hz_safety.bin
   ```
3. Install boot verifier + enable service:
   ```bash
   sudo install -D -m 0755 ipc4-stage/xps17-sof-ipc4-verify.sh \
     /usr/local/libexec/xps17-studio-audio/xps17-sof-ipc4-verify.sh
   sudo install -D -m 0644 ipc4-stage/xps17-sof-ipc4-verify.service \
     /etc/systemd/system/xps17-sof-ipc4-verify.service
   sudo systemctl daemon-reload && sudo systemctl enable xps17-sof-ipc4-verify.service
   ```
4. Enable SOF IPC4 mode & reboot:
   ```bash
   sudo install -D -m 0644 ipc4-stage/90-sof-ipc4-xps17.conf /etc/modprobe.d/90-sof-ipc4-xps17.conf
   ```
5. *(Only after P4 `rt1316` driver + full speaker calibration are verified `MEASURED`)* Deploy `guard-bridge/omarchy-speaker-guard-p4.conf` into `~/.config/systemd/user/omarchy-speaker-guard.service.d/`.

**Instant Rollback to Stock IPC3:**
```bash
sudo rm -f /etc/modprobe.d/90-sof-ipc4-xps17.conf
sudo systemctl disable xps17-sof-ipc4-verify.service
# (UCM2 DRC fence is harmless under IPC3 and may be kept or removed cleanly)
```

---

## 6. Automated Offline Verification Suite

Run all tests strictly on E-cores (`CPUs 12-19`, zero live system writes):

```bash
taskset -c 12-19 nice -n 19 python3 -m unittest -v \
  /home/akshaybapat/project/xps17-studio-audio/kernel-driver/sof-ipc4/test_sof_eqiir_blob.py
taskset -c 12-19 nice -n 19 python3 -m unittest -v \
  /home/akshaybapat/project/xps17-studio-audio/kernel-driver/sof-ipc4/guard-bridge/test_p4_deadman_bridge.py
taskset -c 12-19 nice -n 19 \
  /home/akshaybapat/project/xps17-studio-audio/kernel-driver/sof-ipc4/ipc4-stage/stage-ipc4-check.sh
```
