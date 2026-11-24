# Technical Architecture — Dell XPS 17 9720 Studio Audio Stack

This document specifies all four layers of the `xps17-studio-audio` engineering stack for the **Dell XPS 17 9720** (`DMI product_sku = 0AFF`, PCI Subsystem `0AFF:1028`, codename *Stradale*, Intel Alder Lake + SoundWire quad-speaker subsystem).

---

## 1. Physical Hardware & Acoustic Baseline

| Parameter | Verified Value | Evidence / Source |
| :--- | :--- | :--- |
| **Platform** | Dell XPS 17 9720 (`DMI product_sku = 0AFF`) | `/sys/class/dmi/id/product_sku` |
| **CPU ISA** | Intel Alder Lake (`x86-64`, AVX2 + FMA + BMI2 + SSE4.2) | `/proc/cpuinfo` |
| **Speaker Transducers** | Quad: `2 × 2 W` avg (`2.5 W` peak) woofers + `2 × 2 W` avg (`2.5 W` peak) tweeters | Dell hardware spec & RT1316 crossover topology |
| **Acoustic Geometry** | Flank span $S = 0.340\text{ m}$, desk distance $D = 0.550\text{ m}$, full span angle $2\theta = 34.35^\circ$ | Physical chassis measurement |
| **Woodworth ITD & Comb** | $\tau = 152.0\,\mu\text{s}$, first comb-filter singularity $f_1 = \frac{1}{2\tau} = 3289.47\text{ Hz}$ | Geometric acoustics model |
| **SoundWire Link 0** | Realtek `RT711` headset/jack codec | ALSA/SOF topology |
| **SoundWire Link 1** | `sdw:0:1:025d:1316:01` (`rt1316-1`, Left pair `L,L` — woofer SPOL + tweeter SPOR) | Verified regmap dump & Windows INI `AggrLL` |
| **SoundWire Link 2** | `sdw:0:2:025d:1316:01` (`rt1316-2`, Right pair `R,R` — woofer SPOL + tweeter SPOR) | Verified regmap dump & Windows INI `AggrRR` |
| **SoundWire Link 3** | Realtek `RT714` DMIC array (untouched by speaker stack) | ALSA/SOF topology |
| **Raw Measured Response** | `200 Hz: -14.8 dB`, `300 Hz: -6.6 dB`, `1.5 kHz: +4.2 dB`, `6–8 kHz: -8.0 dB`, `10 kHz: -13.1 dB` | 455 physical Blue Yeti X sweeps (`0.076 dB` repeatability) |

---

## 2. Layer 1 — Kernel Driver (`rt1316-sdca` DKMS) & SOF IPC4 Fence

Directory: `kernel-driver/` (Licensed **`GPL-2.0-only`**)

### 2.1 Proven `ALC1316` / `rt1316-sdw` Silicon Register Findings

Comparative static analysis of live Linux `rt1316-sdw` register dumps (`v7.2.15`) against Dell/Realtek Windows SDCA scripts (`SDCA_06_Alc1316_Aggr{LL,RR}_SM_0AFE1028`, mapped to `0AFF`) proved five concrete Linux driver defects:

| Register | Name / Function | Stock Linux (`v7.2.15`) | Windows Reference | Root Cause & Kernel Fix |
| :--- | :--- | :--- | :--- | :--- |
| **`0xC202`** | Biquad Enable (`bit 7 = BQ1`, `bit 6 = BQ2`) | `0x00` (OFF on **both** amps) | `0xC0` on stream open | **BUG 3 (PROVEN):** `dell_0b00_bq_params` omits `0xC202`. Both woofers & tweeters run full-band. Fix: program `0xC202 = 0xC0` in `rt1316_io_init` after loading coefficients. |
| **`0xC200`** | BQ Update Pulse (`bit 7 = W1C pulse`, `bit 6 = 1`) | Cached `0xC0` on amp 1 only; `0x40` on amp 2 | `0xC0` written *after* `0xC202` | **BUG 1 & Sync Order:** Cached `0xC200` replays *before* coefficient registers (`0xC204+`) on `regcache_sync`. Fix: mark `0xC200` volatile and emit explicit `0xC200 = 0xC0` update pulse after coefficient load on *every* attach. |
| **`0xC204–0xC27B`** | 4× In-Amp Biquad Coefficients | Loaded on amp 1 only (44.1 kHz math); reset identity on amp 2 | Loaded on both amps | **BUG 1 (PROVEN):** `rt1316_sdw_component_probe` races against SoundWire enumeration. Fix: load table inside `rt1316_io_init` (matching `rt1308-sdw.c`) and support 48 kHz-exact RBJ biquads (woofer LPF `5000 Hz Q=0.833` + `20 Hz HPF Q=1.0`; tweeter HPF `1000 Hz Q=1.053` + `20 Hz HPF Q=1.0`). |
| **`0x41001208`** | SDCA `XU24 Bypass` | Blind init `0x00`, `reg_default = 0x01`, ALSA cache `1` | `0x00` (active) | **BUG 2 (INFERRED, high):** Wrong `reg_default` causes `regcache_sync` to skip syncing `0x01` after runtime PM re-attach. Fix: truthful default / explicit post-sync write. |
| **`0x3004 / 0x3005`** | IV-Sense SoundWire Slot Tags | Both amps `0xA0 / 0xB1` (collision) | Amp 1 `0xA0/0xB1`; Amp 2 `0xE4/0xF5` | Enables collision-free 4-channel IV-sense capture (`V0/I2, V1/I3, V4/I6, V5/I7`) on aggregated SOF IPC4 PCM 3 `"Amp feedback"` with PDE24 powered during playback. |
| **`0xC60F` & `0xD001–0xD004`** | Class-D HV Gain & 10-bit Digital Gain | `0xC60F = 0x01` (+6.6 dB), `0x3BF` (-6.00 dB) | Basic: `0xC60F = 0x05` (+9.0 dB), `0x3A9` (-8.06 dB) | Net gain already matches Windows Basic within `±0.5 dB`. Default stays at safe stock gain (`0x01` / `0x3BF`). Any opt-in to `0x05` / `0x3A9` requires the Asahi `macaudio.c`-pattern **250 ms deadman heartbeat** gate. |

### 2.2 Strictly Forbidden Protection Registers (Zero-Write Invariant)

The following nine `ALC1316` registers govern hardware DC protection, HV mute, output-stage speaker protection (`HV_POW_SPK_PRO`), or test tone generators and **must never be written** by any module, patch, or script:

```text
0xC000   0xC050 (DC detect)   0xC082 (sine gen)
0xC093   0xC09C               0xC09D
0xC0A3   0xC102 (HV mute)     0xC60E (HV_POW_SPK_PRO)
```

GitHub Actions CI (`kernel-safety-audit`) statically scans every file under `kernel-driver/` to enforce this invariant on every commit.

### 2.3 SOF IPC3 vs IPC4 & The UCM2 DRC Trap

- Default runtime uses proven SOF IPC3 (`intel/sof/sof-adl.ri` + `sof-adl-rt711-l0-rt1316-l12-rt714-l3.tplg`).
- On-disk SOF IPC4 (`intel/sof-ipc4/adl/sof-adl.ri` + `sof-ipc4-tplg/sof-adl-rt711-l0-rt1316-l12-rt714-l3.tplg`) exposes hardware `eqiir.21.1`, `eqfir.21.1`, `drc.21.1`, and 4-channel `"Amp feedback"` PCM 3.
- **Critical Hazard:** Upstream ALSA UCM2 (`sof-soundwire.conf:258`) enables `Post Mixer Speaker Playback DRC switch` on IPC4 init, injecting **+13.7 dB** uncontrolled makeup gain into the 2 W speakers. `kernel-driver/sof-ipc4/` installs an explicit UCM2 override fencing this control strictly **OFF**.

---

## 3. Layer 2 — Acoustic Research & 4096-Tap Minimum-Phase FIR

Directory: `acoustics-research/`

### 3.1 Minimum-Phase Regularized Inverse FIR Design

Using the 455-sweep baseline capture (`baseline_curve.npy`, `0.076 dB` repeatability via Farina swept-sine deconvolution):

1. **Acoustic Smoothing & Chassis Resonance Trimming:** Psychoacoustic fractional-octave smoothing preserves broad driver imbalance corrections (`+8 dB` at `300 Hz`, `-4 dB` notch at `1.5 kHz`, `+6.5 dB` treble recovery across `6–10 kHz`) while avoiding narrow high-Q boosts that ring or waste headroom.
2. **Excursion-Safe Inverse Regularization:** Low-frequency boost below the woofer roll-off is strictly bounded so static FIR correction never forces mechanical over-excursion; dynamic sub-200 Hz extension is delegated to Layer 3's Klippel excursion-tracking sliding HPF and harmonic synthesizer.
3. **Cepstral Minimum-Phase Reconstruction:** Computes the real cepstrum via $\mathcal{F}^{-1}\{\ln |H_{\text{target}}(e^{j\omega})|\}$, applies the causal Hilbert window (`1` at $n=0$, `2` for $0 < n < N/2$, `0` for $n > N/2$), and transforms back to a **4096-tap @ 48 kHz** stereo `float32` WAV kernel (`85.33 ms` length, peak impulse energy concentrated in the first milliseconds for near-zero perceived latency).

### 3.2 Standardized Kernel & Parameter Artifacts

Stored under `acoustics-research/kernels/`:
- `xps17-studio-deep.wav` — Flagship warm, deep, lush tonal balance (gentle low-end tilt, smooth presence, airy top end)
- `xps17-studio-spatial.wav` — Cinema/spatial balanced response tuned to pair with post-dynamics FDTR-XTC
- `xps17-studio-reference.wav` — Neutral Harman/Olive studio monitoring curve
- `xps17-studio-voice.wav` — Speech-optimized curve reducing desk/chest mud (`150–350 Hz`) and clarifying `2–4 kHz` consonants
- `dsp_params.json` — Machine-readable source of truth for Layer 3 defaults (sliding HPF range $f_{\min}..f_{\max}$, Klippel $f_0, Q_m, X_{\max}$, harmonic weights per band, ISO 226:2023 shelf tables vs host volume dB, 4-band LR4 crossover frequencies & compressor ratios, nearfield early-reflection delays, true-peak drive & ceiling).

---

## 4. Layer 3 — Real-Time AVX2/FMA + Rust DSP Engine

Directory: `dsp-engine/`

### 4.1 Artifacts & Hard Real-Time Contract

- **LADSPA v1.1 Shared Library:** `dsp-engine/lib/libxps17_studio_dsp.so`
  - Symbol: `ladspa_descriptor(unsigned long Index)`
  - Label: `xps17_studio_dsp`
  - UniqueID: `597201`
  - Hard-RT flags: `LADSPA_PROPERTY_HARD_RT_CAPABLE`
- **Standalone Verification & Benchmark CLI:** `dsp-engine/bin/xps17-studio-cli`
  - `render --in <in.wav> --out <out.wav> --mode <deep|spatial|reference|voice> [--volume-db -12] [--fir <kernel.wav>]`
  - `bench` — Measures AVX2/FMA vs scalar speedup, numerical accuracy delta (ULP / dB), p50/p99 per-quantum latency in µs, CPU % at 48 kHz, and verifies zero heap allocations on the audio thread.
  - `impulse --out <ir.wav> --mode <deep|spatial|reference|voice>` — Emits linear impulse response for FFT inspection.
- **Audio-Thread Safety Invariants:**
  - Zero heap allocations (`malloc`/`free`/`Vec::push`) in `run()`
  - Zero locks, mutexes, atomic contention, or blocking syscalls in `run()`
  - IEEE-754 denormal suppression (FTZ/DAZ or anti-denormal offset) across all IIR feedback states
  - Deterministic block processing for any PipeWire quantum (`64`–`2048` samples @ 48 kHz)

### 4.2 Internal DSP Chain Ordering (Why Order Matters)

```text
In L/R
  │
  ├─► [1] Klippel Excursion-Tracking Sliding HPF (65–180 Hz, 24 dB/oct Butterworth/LR)
  │       Tracks instantaneous displacement via 2nd-order mechanical model (f0, Qm, Xmax)
  │       and smoothly glides cutoff upward only on extreme low-bass transients.
  │
  ├─► [2] Hybrid Physical Bass + Missing-Fundamental Harmonic Synthesizer
  │       DC-free band-limited even/odd harmonic projection (2f, 3f, 4f) from 40–130 Hz
  │       into the audible 120–320 Hz woofer passband (Terhardt 1974 / Larsen & Aarts 2004).
  │       NO speech-gate bass choking!
  │
  ├─► [3] Live ISO 226:2023 Equal-Loudness Contour + Warmth Body Stage
  │       Continuously adapts LF/HF shelving from `Volume (dB)` (-45..0 dB) so quiet
  │       late-night listening retains full bass warmth and clarity, plus subtle low-mid
  │       harmonic richness (`Warmth Body`).
  │
  ├─► [4] 4-Band Linkwitz-Riley LR4 Multiband Dynamics + Anchored Nearfield Stage Depth
  │       Phase-coherent 4-band split (Sub/Bass, Low-Mid, Presence, Air) with independent
  │       gentle RMS compression preventing bass peaks from ducking midrange vocals,
  │       paired with subtle mono-compatible nearfield early reflections (`Stage Depth`).
  │
  ├─► [5] Mastering Loudness Drive + 4x Oversampled True-Peak Brickwall Limiter
  │       Applies `Drive (dB)` into an ITU-R BS.1770-4 4x polyphase oversampled lookahead
  │       true-peak limiter clamped strictly to `Ceiling (dBFS)` (-1.0 dBTP default).
  │       Loudness stays INSIDE the DSP chain—never lost to uncompensated gain trims.
  │
  └─► [6] Post-Dynamics Mono-Anchored Choueiri FDTR-XTC (Active when `Spatial XTC > 0`)
          Crucially placed AFTER the limiter (fixing `91-spatial-fdtr.conf` Bug #4) so
          Side-channel spatial boost NEVER triggers bass/vocal pumping!
          Strict unity < 200 Hz, 3.29 kHz comb regularization, 0.000 dB mono-sum ripple.
  │
  ▼
Out L/R
```

### 4.3 SIMD Microarchitecture (`x86-64` AVX2 + FMA + SSE4.2)

Hot audio-thread kernels leverage hand-tuned x86-64 SIMD (AVX2 256-bit `vfmadd231ps`, SSE4.2 fallback, and bit-verified scalar reference):
- **Parallel Stereo Biquad / LR4 Cascade:** Processes L/R channels and multi-band states simultaneously in SIMD lanes with FMA precision.
- **4× Polyphase True-Peak Upsampler:** Vectorized FIR dot product scanning inter-sample peaks with zero heap buffers.
- **FDTR-XTC Stereo Convolution:** Short-block SIMD convolution for the 512-tap causal crosstalk-cancellation matrix ($H_{ll}, H_{lr}, H_{rl}, H_{rr}$) with runtime bypass when `Spatial XTC == 0.0`.
- **Fast Transcendental / Soft-Clip Kernels:** Vectorized rational `tanh` / envelope approximations eliminating slow `libm` calls per sample.

### 4.4 Exact 10-Port LADSPA v1.1 ABI Contract

**DO NOT rename or reorder these ports** — PipeWire filter-chain configs, `omarchy-speaker-dsp-controller`, and CI `ladspa-abi-verify` bind directly to these exact port names and indices:

| Port Index | Direction | Type | Exact Port Name | Min | Default | Max | Live Semantics |
| :---: | :---: | :---: | :--- | :---: | :---: | :---: | :--- |
| `0` | Input | Audio | `In L` | — | — | — | Left stereo input (`float32`) |
| `1` | Input | Audio | `In R` | — | — | — | Right stereo input (`float32`) |
| `2` | Output | Audio | `Out L` | — | — | — | Left stereo output (`float32`) |
| `3` | Output | Audio | `Out R` | — | — | — | Right stereo output (`float32`) |
| `4` | Input | Control | `Mode` | `0` | `0` | `3` | `0` = Studio Deep, `1` = Studio Spatial, `2` = Studio Reference, `3` = Studio Voice |
| `5` | Input | Control | `Volume (dB)` | `-45.0` | `-12.0` | `0.0` | Driven live by `omarchy-speaker-dsp-controller` for ISO 226:2023 loudness & Klippel excursion scaling |
| `6` | Input | Control | `Bass Depth` | `0.0` | `1.0` | `1.5` | Hybrid physical bass extension + missing-fundamental harmonic weight |
| `7` | Input | Control | `Physical Bass Hz` | `65.0` | `82.0` | `180.0` | Resting cutoff floor of the Klippel excursion-tracking sliding HPF |
| `8` | Input | Control | `Warmth Body` | `0.0` | `1.0` | `1.5` | Low-mid chest richness & subtle transformer/tube-style even-order warmth |
| `9` | Input | Control | `Stage Depth` | `0.0` | `0.85` | `1.5` | Anchored nearfield early-reflection depth & spaciousness |
| `10` | Input | Control | `Spatial XTC` | `0.0` | `0.0` | `1.0` | Post-dynamics mono-anchored FDTR-XTC blend (`0.0` in Studio Deep, `0.75` in Studio Spatial) |
| `11` | Input | Control | `Drive (dB)` | `0.0` | `7.0` | `12.0` | Mastering loudness drive into the 4× oversampled true-peak limiter |
| `12` | Input | Control | `Ceiling (dBFS)` | `-9.0` | `-1.0` | `-0.3` | Brickwall true-peak protection ceiling |
| `13` | Input | Control | `Excursion Guard` | `0.0` | `1.0` | `1.0` | Klippel nonlinear displacement limiter & dynamic HPF engagement |

---

## 5. Layer 4 — PipeWire Graph, Live Controller & Verification

Directory: `verify-integrate/`

### 5.1 PipeWire Filter-Chain Topology

`omarchy-speaker-engine.service` launches an isolated PipeWire filter-chain client (`pipewire -c omarchy-speaker-engine.conf`) loading `~/.config/pipewire/omarchy-speaker-engine.conf.d/`:
- Captures desktop playback on the virtual default sink (`node.name = "omarchy_speaker_engine"` when internal speakers are active).
- Routes stereo audio through:
  1. Builtin `convolver` node (`xps17_fir_convolver`) running the active 4096-tap minimum-phase FIR kernel (`xps17-studio-*.wav`).
  2. LADSPA plugin node (`xps17_studio_dsp`, `plugin = ~/.local/lib/ladspa/libxps17_studio_dsp.so`, `label = xps17_studio_dsp`).
- Pins playback directly to the internal SoundWire speaker sink so WirePlumber never accidentally routes laptop DSP to Bluetooth headphones or USB DACs.
- **Fail-Closed Resilience:** `StartLimitBurst=4` in 60 s triggers `OnFailure=omarchy-speaker-engine-fallback.service`, guaranteeing audio never drops silently if a plugin fails to initialize.

### 5.2 Live ISO 226 Controller Coupling

`omarchy-speaker-dsp-controller.service` monitors the active PipeWire sink volume and translates hardware/desktop volume percentage into exact attenuation (`-45.0 .. 0.0 dB`), pushing real-time parameter updates to the `Volume (dB)` port of `xps17_studio_dsp` via `pw-cli` / PipeWire control metadata. This eliminates the frozen-loudness bug of `91-spatial-fdtr.conf` while keeping Klippel excursion thresholds accurately scaled to actual acoustic SPL.

### 5.3 Privacy, Bluetooth & Rollback Invariants

- **Mic Privacy:** Never touches `~/.config/systemd/user/easyeffects-speaker-dsp.service.d/no-mic-monitor.conf` or `listenToMic=false` in `~/.config/easyeffects/db/easyeffectsrc`.
- **Bluetooth Protection:** Never modifies `~/.config/wireplumber/wireplumber.conf.d/51-bluetooth-no-phone-audio.conf` (`a2dp_sink` / `hfp_hf` remain excluded so paired phones never hijack the laptop as a Bluetooth speaker).
- **Atomic Staging & Rollback:** `install.sh` writes all target files via temporary same-filesystem staging + `mv -f`, maintains a timestamped pre-install backup manifest under `~/.local/state/xps17-studio-audio/backups/`, and enables one-command restoration via `uninstall.sh`.

### 5.4 Continuous Offline Verification (`verify-integrate/bench/` & CI)

All automated tests run strictly offline without playing audible sound or opening the microphone:
1. Synthetic logarithmic sweeps, pink noise, music transients, and vocal+bass superposition stems rendered through `xps17-studio-cli`.
2. Convolved with the measured physical XPS 17 9720 speaker impulse response (`baseline_curve.npy`) to compute predicted in-room SPL, RMSE vs target curve, ITU-R BS.1770-4 LUFS, 4× true peak, Klippel peak excursion, THD, and mono-sum ripple.
3. Four publication-grade charts exported to `docs/charts/` and structured metrics exported to `benchmark_results.json`, synced into `README.md` via `sync_repo_bundle.py --update-readme`.
