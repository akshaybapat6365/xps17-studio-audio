# Dell XPS 17 9720 Studio Audio — Safety, Mic-Privacy, Hardware-Protection & Thermal Audit

- **Generated (UTC):** `2026-11-24T16:33:48Z`
- **Workspace:** `/home/akshaybapat/project/xps17-studio-audio`
- **Inspected Home (Read-Only):** `/home/akshaybapat`
- **Overall Status:** **`PASS`**

## Executive Summary

| Status | Count | Meaning |
| :--- | ---: | :--- |
| **PASS** | 27 | Verified compliant with safety, privacy, ABI, and thermal contracts |
| **WARN** | 0 | Non-fatal advisory (e.g., elevated CPU/temp within hard limit) |
| **FAIL** | 0 | Hard safety, privacy, ABI, register, or budget violation |
| **PENDING** | 0 | Upstream artifact not yet landed (Lead 1 / Lead 2 / Lead 3 / Integrator) |
| **TOTAL** | 27 | Machine-verifiable audit checks executed |

## Check Group Breakdown

| Group | Status | PASS | WARN | FAIL | PENDING |
| :--- | :---: | ---: | ---: | ---: | ---: |
| Check Group 1 — PipeWire Graph & Config Validator | **PASS** | 7 | 0 | 0 | 0 |
| Check Group 2 — LADSPA ABI, 10-Port Contract & FIR Kernels | **PASS** | 5 | 0 | 0 | 0 |
| Check Group 3 — Live Mic-Privacy, Bluetooth & Loudness Authority Guards | **PASS** | 7 | 0 | 0 | 0 |
| Check Group 4 — Kernel Driver Forbidden-Register & IPC4 Static Audit | **PASS** | 4 | 0 | 0 | 0 |
| Check Group 5 — Thermal & Real-Time CPU Budget Verification | **PASS** | 4 | 0 | 0 | 0 |

## Check Group 1 — PipeWire Graph & Config Validator (`PASS`)

| Check ID | Check | Status | Details |
| :--- | :--- | :---: | :--- |
| `G1.1_spa_json_syntax` | SPA-JSON Balanced Braces/Brackets/Quotes & Parse | **PASS** | 92-xps17-studio.conf parsed cleanly with balanced braces, brackets, and quotes. |
| `G1.2_context_modules` | context.modules Structure | **PASS** | Found 4 libpipewire-module-filter-chain module(s) in context.modules. |
| `G1.3_expected_sinks` | All 4 Studio Sink Nodes Declared | **PASS** | All 4 expected Studio sink nodes present. |
| `G1.4_passive_playback` | Zero Idle CPU (playback.props.node.passive = true) | **PASS** | All filter-chain playback streams set node.passive = true (zero idle CPU). |
| `G1.5_no_mic_capture` | No Unintended Microphone / Capture Source Capture | **PASS** | All modules strictly Audio/Sink playback graphs with zero capture/mic references. |
| `G1.6_dag_acyclicity` | Filter Graph DAG Acyclicity (Topological Sort) | **PASS** | All filter-chain graphs verified acyclic (strict DAGs). |
| `G1.7_port_references` | Valid Node Input/Output Port References | **PASS** | All link, input, and output port references resolve cleanly to declared nodes and valid ports. |

## Check Group 2 — LADSPA ABI, 10-Port Contract & FIR Kernels (`PASS`)

| Check ID | Check | Status | Details |
| :--- | :--- | :---: | :--- |
| `G2.1_ladspa_abi` | LADSPA dlopen & Descriptor ABI (Label, UniqueID, Hard-RT) | **PASS** | dlopen OK: Label='xps17_studio_dsp', UniqueID=597201, Hard-RT=True, PortCount=14. |
| `G2.2_ladspa_10port_contract` | LADSPA 4 Audio + Exact 10 Control Ports Contract | **PASS** | Exact 4 audio ports + 10 control ports verified (optional control outputs: []). |
| `G2.3_cross_contract_parity` | 10-Port Control Parity (LADSPA <-> 92-xps17-studio.conf <-> controller) | **PASS** | Every control port name in 92-xps17-studio.conf and staged controller matches the exact 10-port LADSPA contract. |
| `G2.4_fir_kernels` | 4x 4096-Tap Stereo 48 kHz Float32 FIR Kernels | **PASS** | All 4 FIR kernels verified: 48 kHz, 2-ch, 4096-tap IEEE float32, finite taps, bounded FFT gain. |
| `G2.5_dsp_params_json` | acoustics-research/dsp_params.json Contract | **PASS** | dsp_params.json parsed cleanly (15 top-level keys). |

## Check Group 3 — Live Mic-Privacy, Bluetooth & Loudness Authority Guards (`PASS`)

| Check ID | Check | Status | Details |
| :--- | :--- | :---: | :--- |
| `G3.1_no_mic_monitor_dropin` | Live EasyEffects no-mic-monitor.conf Drop-in Guard | **PASS** | no-mic-monitor.conf exists and enforces listenToMic=false via ExecStartPre. |
| `G3.2_bt_no_phone_audio` | Live WirePlumber 51-bluetooth-no-phone-audio.conf Guard | **PASS** | 51-bluetooth-no-phone-audio.conf verified: excludes a2dp_sink and hfp_hf (roles=['a2dp_source', 'hfp_ag', 'bap_sink', 'bap_source', 'bap_bcast_sink', 'bap_bcast_source', 'asha_sink']). |
| `G3.3_soundwire_mic_default_conf` | Live WirePlumber 60-soundwire-input-default-volume.conf Guard | **PASS** | 60-soundwire-input-default-volume.conf pins device.routes.default-source-volume = 1.0. |
| `G3.4_live_mic_volume` | Live Default Audio Source Volume == 1.00 (100%) | **PASS** | Read-only wpctl query reports @DEFAULT_AUDIO_SOURCE@ Volume: 1.00 (100% unity). |
| `G3.5_easyeffects_listen_to_mic` | Live EasyEffects listenToMic == false | **PASS** | easyeffectsrc sets listenToMic=false (mic loopback disabled). |
| `G3.6_staged_guard_non_interference` | Staged Files Do Not Touch Mic-Privacy or Bluetooth Guards | **PASS** | Scanned 14 staged file(s): zero modifications to mic-privacy or Bluetooth guards. |
| `G3.7_loudness_authority` | Loudness Authority (LOUDNESS_DRIVE_DB & In-Chain Drive/Ceiling) | **PASS** | Loudness authority preserved: LOUDNESS_DRIVE_DB intact in controller/runtime.py and Drive (dB) + brickwall Ceiling active in 92-xps17-studio.conf. |

## Check Group 4 — Kernel Driver Forbidden-Register & IPC4 Static Audit (`PASS`)

| Check ID | Check | Status | Details |
| :--- | :--- | :---: | :--- |
| `G4.1_forbidden_alc1316_regs` | Forbidden ALC1316 Protection Registers Static Audit | **PASS** | Scanned 10 kernel C/H/patch file(s): ZERO writes to forbidden ALC1316 protection registers. |
| `G4.2_high_gain_deadman_gate` | High-Gain (0xC60F=0x05, 0xD002/0xD004=0xA9) Deadman Protection Gate | **PASS** | Conservative safe posture: no 0xC60F=0x05 or 0xD002/0xD004=0xA9 high-gain writes present. |
| `G4.3_ipc4_drc_switch_fence` | IPC4 'Post Mixer Speaker Playback DRC switch' Fenced OFF | **PASS** | IPC4 configs explicitly fence 'Post Mixer Speaker Playback DRC switch' OFF (2 fence layer(s) verified). |
| `G4.4_no_regmap_hang_reads` | No Reads of /sys/kernel/debug/regmap/*/{access,range} | **PASS** | Scanned 36 file(s) under kernel-driver/: zero unsafe regmap access/range reads. |

## Check Group 5 — Thermal & Real-Time CPU Budget Verification (`PASS`)

| Check ID | Check | Status | Details |
| :--- | :--- | :---: | :--- |
| `G5.1_package_temperature` | CPU Package Thermal Envelope (< 82°C) | **PASS** | Package temperature 65.0°C is cool and well under the 82°C safety ceiling. |
| `G5.2_cli_bench` | xps17-studio-cli bench (E-core Pinned) | **PASS** | xps17-studio-cli bench succeeded cleanly (parsed CPU=0.97%). |
| `G5.3_ctypes_rt_cpu_and_nan` | Independent E-Core dlopen RT CPU (< 1.5%) & Zero NaN/Inf Audit | **PASS** | All 4 modes verified on E-cores: worst CPU 0.967% (< 1.0%), zero NaN/Inf samples. |
| `G5.4_post_bench_thermal` | Post-Audit Package Temperature (< 82°C) | **PASS** | Post-audit package temperature remained at 66.0°C (< 82°C). |

## Live Safety Guard SHA256 Fingerprints (Read-Only)

| Guard | Path | SHA256 |
| :--- | :--- | :--- |
| `G3.1_no_mic_monitor_dropin` | `/home/akshaybapat/.config/systemd/user/easyeffects-speaker-dsp.service.d/no-mic-monitor.conf` | `6fff75c913f4cdff8f920e42cc3fb52e6bf7dfeb283e466d11b0eb542d26a1a8` |
| `G3.2_bt_no_phone_audio` | `/home/akshaybapat/.config/wireplumber/wireplumber.conf.d/51-bluetooth-no-phone-audio.conf` | `86b9d38a0d8e271ce7d498b10fb52f381835e0ab5db6da731005eeff70201dd0` |
| `G3.3_soundwire_mic_default_conf` | `/home/akshaybapat/.config/wireplumber/wireplumber.conf.d/60-soundwire-input-default-volume.conf` | `76253e2cb767cef70acccece013278a5b13b004517178e267511c418b5402db4` |

## Standardized Runtime Asset Paths

- LADSPA plugin: `$TARGET_HOME/.local/share/xps17-studio-audio/lib/libxps17_studio_dsp.so`
- FIR kernels: `$TARGET_HOME/.local/share/xps17-studio-audio/kernels/xps17-studio-{deep,spatial,reference,voice}.wav`
- PipeWire graph: `$TARGET_HOME/.config/pipewire/omarchy-speaker-engine.conf.d/92-xps17-studio.conf`
- Pre-deploy backups: `$TARGET_HOME/.local/state/xps17-studio-audio/backups/<timestamp>/`
