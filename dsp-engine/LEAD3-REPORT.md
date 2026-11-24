# Lead 3 Engineering Report — Dell XPS 17 9720 Hard-RT Studio Audio DSP Engine

**Team:** Lead 3 (`audio-lead-dsp-asm` — Low-Level Rust + C + x86_64 AVX2/FMA Assembly Hard-RT Audio Engine)
**Owned Directory:** `/home/akshaybapat/project/xps17-studio-audio/dsp-engine/`
**Target Hardware:** Dell XPS 17 9720 (`0AFF`, Intel Alder Lake AVX2+FMA, Dual Realtek ALC1316 SoundWire quad-speaker array, flank span `S = 0.340 m`, desk listening distance `D = 0.550 m`)

---

## 1. Executive Verification Summary

| Gate / Requirement | Target | Measured Result (Fresh Post-Build on E-Cores `12–19`) | Verdict |
|---|---|---|---|
| **LADSPA v1.1 Binary & ID** | `lib/libxps17_studio_dsp.so`, `ID=597201`, `xps17_studio_dsp`, 14 exact ports | `dlopen` verified via `tests/verify_abi.c` (`NaN=0`, in-place + out-of-place + hostile inputs) | **PASS** |
| **Hard-RT Heap Allocations** | `0` allocations/deallocations in `process_block()` & `ladspa::run()` | `0` allocs across `6,000+` blocks (`tests/zero_alloc.rs` + `xps17-studio-cli bench`) | **PASS** |
| **AVX2/FMA `.S` Parity** | `< 1e-5` max abs error vs scalar reference | `5.96e-8` to `5.72e-6` max abs error (`2.15×` to `11.58×` speedup on E-cores) | **PASS** |
| **E-Core Real-Time CPU %** | Strictly `< 1.50%` (`< 1.00%` zero-warn gate) @ `48 kHz / 256` frames | **`0.859% – 0.927%`** in `LadspaHost` (`45.82–49.44 µs/block`, `107.9×–116.4×` RT); **`0.98%`** in CLI `bench` (`51.80 µs` p50) | **PASS** |
| **4× True-Peak Brickwall** | `<= -1.00 dBTP` (both Catmull-Rom & ITU-R BS.1770-4 Kaiser-sinc) | **`-1.19 dBTP`** across Deep, Spatial, Reference (`-2.71 dBTP` Voice; `-1.19 dBTP` burst across all 4 modes) | **PASS** |
| **Klippel `X_max` Safety (`0.45 mm`)** | Positive margin (`>= +0.50 dB`) on hot kicks; rests at `82 Hz` on normal program | Deep: **`0.4028 mm` (`+0.96 dB`)**, Spatial: **`0.3993 mm` (`+1.04 dB`)**, Ref: **`0.3969 mm` (`+1.09 dB`)**, Voice: **`0.1456 mm` (`+9.80 dB`)** | **PASS** |
| **Physical & Psychoacoustic Bass** | Recover `< 180 Hz` bass starved by old `180 Hz` 8th-order HPF | Deep `Phys 70–180 Hz = -41.31 dB` (**`+7.41 dB` vs OLD**), `PBI = -34.07 dB` | **PASS** |
| **Vocal + Bass Non-Choking** | `Vocal+Bass Δ` within `-0.20 .. +0.50 dB` (`choking=False`) | Deep: **`-0.14 dB`**, Spatial: **`-0.12 dB`**, Reference: **`+0.08 dB`**, Voice: **`+0.04 dB`** | **PASS** |
| **`< 180 Hz` Stereo Correlation** | `r(< 180 Hz) >= +0.990` on uncorrelated stereo pink noise | Deep: **`0.99960`**, Spatial: **`0.99960`**, Reference: **`0.99960`**, Voice: **`0.99940`** | **PASS** |
| **Lead 2 Acoustics Bundle** | Auto-load `acoustics-research/dsp_params.json` + 4 FIR `.wav` kernels | `kernels_from_disk=[true, true, true, true]`, `dsp_params_json=true` | **PASS** |
| **27-Check Safety & Privacy Audit** | `run_safety_audit.py --strict` -> `27/27 PASS` | **`overall=PASS, PASS=27, WARN=0, FAIL=0, PENDING=0`** | **PASS** |

---

## 2. 7-Stage Scientific Signal Flow (`src/engine.rs`)

The engine processes stereo `f32` audio under an RAII MXCSR `FTZ` (`bit 15`) + `DAZ` (`bit 6`) denormal-immunity guard (`FtzDazGuard`) in the following scientifically ordered 7-stage pipeline:

1. **Stage 1 — 3-Band Missing-Fundamental Psychoacoustic Bass & Warmth Engine (`src/bass.rs`)**
   - Operates directly on the raw full-range input **before** mechanical high-pass filtering so `32–175 Hz` fundamentals drive phase-locked harmonic synthesis (`110–380 Hz`) before sub-resonance energy is attenuated.
   - Enforces a phase-coherent `StereoLr4F32` mono crossover (`>= 215 Hz`) on incoming sub-bass, decimates the 3 sub-bass detector/exciter bands (`32–68 Hz`, `68–115 Hz`, `115–175 Hz`) by `4×` (`12 kHz` internal rate, Nyquist `6 kHz >> 175 Hz`) for ultra-low E-core overhead (`10.94 µs/block`), and evaluates zero-DC Chebyshev polynomials (`T2(u) - T2(0) = 2u^2`, `T3(u) = 4u^3 - 3u`, `T4(u) - T4(0) = 8u^4 - 8u^2`) normalized by smooth envelope tracking so harmonics scale linearly with musical dynamics rather than collapsing on `-14 dBFS` basslines.
   - Replaces the legacy `speech_gate` with a vocal-safe sub-bass crest/spectral music gate and isolates the `195 Hz` dynamic warmth detector strictly to `< 215 Hz` `mono_low`, completely preventing `680–2650 Hz` vocals from ducking bass warmth (`Vocal+Bass Δ = -0.14 dB` in `Studio Deep`).

2. **Stage 2 — Continuous Volume-Coupled ISO 226:2023 Equal-Loudness Contour (`src/loudness.rs`)**
   - Maps live `Volume (dB)` (`-45.0 .. 0.0 dB`, reference `-12.0 dB`) continuously onto dual cascaded `StereoSvfF32` low-shelves (`105 Hz` + `210 Hz`), presence compensation (`3.2 kHz`), and air shelf (`9.5 kHz`) with per-sample exponential smoothing (`zero zipper noise`).

3. **Stage 3 — Uniformly Partitioned Overlap-Save (UPOLS) 4096-Tap Minimum-Phase FIR Convolver (`src/convolver.rs`)**
   - Uses `B = 256` (`N = 512` FFT, `16` partitions = `4096` taps, `256` samples algorithmic latency) and exploits Hermitian symmetry `Z[k] = X_L[k] + j X_R[k]` to transform **both** stereo channels simultaneously in a **single** 512-point radix-2/4 SIMD FFT and a **single** IFFT per 256-sample block (`5.99 µs/block`).
   - Accumulates frequency-domain partitions in Structure-of-Arrays (`re[]`, `im[]`) layout via `xps17_avx2_cmul_acc_f32` (`asm/xps17_avx2_kernels.S`), with energy-aware active partition trimming (`< -125 dB` tail threshold) while retaining all `4096` taps in memory.

4. **Stage 4 — Phase-Coherent 4-Band Linkwitz-Riley (LR4) Mastering Dynamics (`src/dynamics.rs`)**
   - Splits audio at `430 Hz / 1650 Hz / 5800 Hz` via `StereoLr4F32` (`__m128` register-resident Cytomic ZDF-SVF cascades with complementary 2nd-order Butterworth all-pass phase alignment -> `< 0.05 dB` flat unity recombination).
   - Placing `XOVER_LOW_HZ = 430 Hz` keeps `100%` of physical bass and `110–380 Hz` missing-fundamental harmonics inside Band 0 while isolating `620–3000 Hz` vocal formants in Bands 1–2, eliminating cross-band vocal-induced bass modulation (`8.66 µs/block`).

5. **Stage 5 — Post-Dynamics Nearfield Depth & Mono-Anchored FDTR-XTC (`src/spatial.rs`)**
   - Synthesizes subtle micro-early reflections (`5.3 ms` / `8.9 ms` Haas-zone taps via `4×` decimated Schroeder all-pass diffusion + `90°` quadrature stereo decorrelation) for `Stage Depth`, and post-dynamics mono-anchored recursive `FDTR-XTC` (`Δτ = 94 µs` for XPS 17 `S = 0.340 m, D = 0.550 m` via 3rd-order Lagrange fractional delay, head-shadow LPF at `2.3 kHz`, Side boost strictly capped `<= +2.8 dB`, and 4th-order `215 Hz` M/S Side HPF; `9.17 µs/block`).

6. **Stage 6 — Post-Spatial Mono-Bass Anchor & Klippel 2nd-Order Excursion Guard (`src/excursion.rs`)**
   - Placed **after** `bass`, `loudness`, `convolver`, `dynamics`, and `spatial` (and scaled by Stage 7's `drive_db` linear gain) so the Klippel 2nd-order mechanical displacement estimator (`f0 = 145 Hz, Qm = 0.85, X_max = 0.45 mm`, matched to `acoustic_suite.py`) observes the exact electrical drive entering the speakers (`7.18 µs/block`).
   - Enforces a post-spatial `215 Hz` `StereoLr4F32` mono-bass anchor (`r(< 180 Hz) = 0.99940 – 0.99960` across all 4 modes on uncorrelated stereo pink noise), rests its 4th-order ZDF-SVF sliding HPF cleanly at `82.0 Hz` on normal desk listening program, and glides upward smoothly (`up to 165 Hz`) alongside a proportional `210 Hz` dynamic sub-shelf whenever predicted cone excursion approaches `X_max`.

7. **Stage 7 — 4× Oversampled Dual-Detector True-Peak Brickwall Limiter (`src/limiter.rs`)**
   - Combines `Drive (dB)` (`0..12 dB`) with a continuous `C^2` soft-knee shaper, an `8.0 ms` (`384`-sample) inline power-of-two ring-buffered lookahead window (`O(1)` monotonic deque + 64-sample triangle-cascade boxcar), program-dependent dual-stage release (`25 ms` transient / `160 ms` sustained), and a **hybrid dual-4× true-peak detector**:
     - Fast `xps17_avx2_true_peak_4x_f32` Catmull-Rom 4× cubic lower-bound pre-screen across every sample;
     - Exact **12-tap/phase (48-tap) Kaiser-windowed (`β = 8.5`) ITU-R BS.1770-4 polyphase sinc** evaluation (`__m128` SIMD dot product across the 3 fractional phases) whenever a sample approaches within `-3.5 dB` of ceiling, plus a zero-overshoot post-lookahead safety clamp (`9.72 µs/block`).

---

## 3. x86_64 AVX2 + FMA Assembly Kernels (`asm/xps17_avx2_kernels.S` & `src/simd.rs`)

All 5 assembly kernels follow the System V AMD64 ABI (`asm/xps17_avx2_kernels.S`, `.note.GNU-stack` non-executable stack, `vzeroupper` before every `ret`, runtime `is_x86_feature_detected!("avx2") && is_x86_feature_detected!("fma")` dispatch with bit-accurate scalar fallback):

| Kernel (`asm/xps17_avx2_kernels.S`) | AVX2 + FMA (ns) | Scalar (ns) | Speedup (E-Core) | Max Abs Error vs Scalar |
|---|---:|---:|---:|---:|
| `xps17_avx2_fir_dot_f32` (`n = 1024`, 4×8-wide unrolled FMA) | `69.73` | `807.61` | **`11.58×`** | `1.431e-06` |
| `xps17_avx2_cmul_acc_f32` (`n = 129`, SOA `vfmsub231ps` + `vfmadd231ps`) | `26.70` | `132.36` | **`4.96×`** | `2.384e-07` |
| `xps17_avx2_chebyshev_bass_f32` (`n = 256`, zero-DC `T2/T3/T4` + `tanh` Padé `[3/3]`) | `419.47` | `902.25` | **`2.15×`** | `1.192e-07` |
| `xps17_avx2_true_peak_4x_f32` (`n = 256`, 4-phase Catmull-Rom polyphase max) | `181.87` | `833.04` | **`4.58×`** | `2.384e-07` |
| `xps17_avx2_svf_bank8_f32` (`256 × 8` parallel Cytomic ZDF-SVF channels) | `1527.80` | `10230.41` | **`6.70×`** | `2.384e-07` |

In addition, `src/svf.rs` keeps stereo (`[L, R, 0, 0]`) and 4-channel (`[ch0..ch3]`) Cytomic ZDF-SVF integrators (`ic1eq`, `ic2eq`) resident in 128-bit `__m128` XMM registers across cascaded `StereoLr4F32` and `QuadSvfF32` stages, eliminating store-to-load forwarding stalls on Intel Alder Lake Gracemont E-cores.

---

## 4. Full Offline Acoustic & Mastering Benchmark Results (`RealNewStudioChain`)

Measured via `/home/akshaybapat/project/xps17-studio-audio/verify-integrate/bench/acoustic_suite.py` loading real `lib/libxps17_studio_dsp.so`, `acoustics-research/dsp_params.json`, and all 4 Lead 2 `acoustics-research/kernels/xps17-studio-*.wav` minimum-phase FIR kernels against measured XPS 17 Blue Yeti X calibration (`baseline_curve.npy`):

| Studio Mode | Phys Bass `70–180 Hz` | Perceived Bass Index (`PBI`) | Integrated Loudness | Kick / Burst True Peak | Klippel Peak (`X_max = 0.45 mm`) | `Vocal+Bass Δ` (`70–260 Hz`) | `< 180 Hz` Corr `r` | `1 kHz` THD+N | SMPTE IMD | E-Core CPU (`256f`) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **Studio Deep** | **`-41.31 dB`** | **`-34.07 dB`** | `-9.20 LUFS` | **`-1.19 / -1.19 dBTP`** | **`0.4028 mm` (`+0.96 dB`)** | **`-0.14 dB`** (`OK`) | **`0.99960`** | `0.0041%` | `0.3538%` | **`0.877%`** (`46.8 µs`) |
| **Studio Spatial** | **`-41.47 dB`** | **`-34.30 dB`** | `-9.37 LUFS` | **`-1.19 / -1.19 dBTP`** | **`0.3993 mm` (`+1.04 dB`)** | **`-0.12 dB`** (`OK`) | **`0.99960`** | `0.0043%` | `0.5144%` | **`0.927%`** (`49.4 µs`) |
| **Studio Reference** | **`-41.93 dB`** | `-41.78 dB` | `-9.08 LUFS` | **`-1.19 / -1.19 dBTP`** | **`0.3969 mm` (`+1.09 dB`)** | **`+0.08 dB`** (`OK`) | **`0.99960`** | `0.0000%` | `0.0000%` | **`0.859%`** (`45.8 µs`) |
| **Studio Voice** | `-57.30 dB` | `-57.30 dB` | `-16.81 LUFS` | **`-2.71 / -1.19 dBTP`** | **`0.1456 mm` (`+9.80 dB`)** | **`+0.04 dB`** (`OK`) | **`0.99940`** | `0.0035%` | `0.0000%` | **`0.875%`** (`46.7 µs`) |

---

## 5. Build, Test & Verification Commands Executed

All builds and tests were executed offline on Efficiency Cores (`CPUs 12–19`) under `/tmp/xps17-audio-build.lock` and `CPUQuota=140%`:
1. `cargo test --release` -> **47/47** unit tests PASS + **3/3** `zero_alloc` hard-RT global-allocator-hook tests PASS (`0` compiler warnings).
2. `tests/verify_abi.c` -> **PASS** (`ID=597201`, `Label=xps17_studio_dsp`, all 14 port descriptors/hints, out-of-place, in-place buffer aliasing, NULL control port fallback, NaN/Inf/denormal immunity).
3. `tests/verify_asm.c` -> **PASS** (`< 1e-5` max abs error across all 5 AVX2/FMA `.S` kernels).
4. `./bin/xps17-studio-cli bench` -> **PASS** (`0.98%` E-core CPU, `51.80 µs` p50, `102.2×` realtime, `0` RT heap allocations).
5. `./bin/xps17-studio-cli verify` -> **16/16 PASS** across `deep`, `spatial`, `reference`, `voice`.
6. `./bin/xps17-studio-cli impulse` & `./bin/xps17-studio-cli render` -> **PASS** (`640` samples = `13.33 ms` total algorithmic latency: `256` FIR + `384` limiter).
7. `verify-integrate/audit/run_safety_audit.py --strict` -> **`overall=PASS, PASS=27, WARN=0, FAIL=0, PENDING=0`**.

---

## 6. Primary Scientific & Engineering References

1. **Simper, A. (2013).** *Linear Trapezoidal Integrated State Variable Filter (SVF)*. Cytomic Technical Papers.
2. **Linkwitz, S. H. (1976).** Active crossover networks for noncoincident drivers. *Journal of the Audio Engineering Society (JAES)*, 24(1), 2–8.
3. **ISO 226:2023.** *Acoustics — Normal equal-loudness-level contours*. International Organization for Standardization.
4. **Klippel, W. (2006).** Tutorial: Loudspeaker Nonlinearities — Causes, Parameters, Symptoms. *Journal of the Audio Engineering Society (JAES)*, 54(10), 907–939.
5. **Terhardt, E. (1974).** Pitch, consonance, and harmony. *Journal of the Acoustical Society of America (JASA)*, 55(5), 1061–1069.
6. **Larsen, E., & Aarts, R. M. (2004).** *Audio Bandwidth Extension: Application of Psychoacoustics, Signal Processing and Loudspeaker Design*. John Wiley & Sons.
7. **Choueiri, E. Y. (2011).** Optimal Crosstalk Cancellation for Binaural Audio with Two Loudspeakers (*BACCH / FDTR*). Princeton University 3D Audio and Applied Acoustics Laboratory.
8. **ITU-R BS.1770-4 (2015).** *Algorithms to measure audio programme loudness and true-peak audio level*. International Telecommunication Union.
