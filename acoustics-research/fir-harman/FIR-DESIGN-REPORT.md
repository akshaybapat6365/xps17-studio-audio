# Dell XPS 17 9720 Studio Audio Stack — Room Acoustics, Harman Target Curves & Minimum-Phase FIR Inverse Filtering Report

**Date:** 2026-11-24  
**Scope:** Lead 2 (Acoustic Physics & DSP Architecture) — Target Curve Design, Forensic Measurement Analysis, Kirkeby-Nelson Regularized Inversion, and Production 4096-Tap Float32 Minimum-Phase FIR Kernels  
**Hardware Plant:** Dell XPS 17 9720 (`Stradale`, quad `2 × 2 W` woofers + `2 × 2 W` tweeters, dual Realtek `ALC1316`/`ALC1319D`, speaker span `S = 0.340 m`, nominal desk distance `D = 0.550 m`)  
**Empirical Ground Truth:** Blue Yeti X 10 s Exponential Sine Sweep (ESS) measurements in `/home/akshaybapat/project/omarchy-audio-tune/` (`baseline_curve.npy`, 5-sweep averaged, `0.076 dB` in-band repeatability SD, `H2 = -54.7 dB`, `H3 = -54.0 dB`)

---

## 1. Executive Summary & Deliverables Inventory

This report documents the forensic acoustic analysis of the measured Dell XPS 17 9720 speaker-desk transfer function and the complete mathematical synthesis and verification of **four production 4096-tap @ 48 kHz stereo IEEE float32 minimum-phase WAV FIR kernels**.

Every kernel is synthesized deterministically by pure-`numpy` script [`generate_studio_firs.py`](file:///home/akshaybapat/project/xps17-studio-audio/acoustics-research/fir-harman/generate_studio_firs.py), verified bit-for-bit via round-trip WAV readback and `libsndfile` (`sndfile-info`), and passes **100% of mathematical, acoustic, headroom, and minimum-phase validation gates**.

### Verified Production Artifacts

| Artifact | Path | Format / Spec | SHA-256 (prefix) | Status |
|---|---|---|---|---|
| **Studio Deep** (Mode 0) | `/home/akshaybapat/project/xps17-studio-audio/acoustics-research/kernels/xps17-studio-deep.wav` | RIFF WAVE IEEE float32 (`0x0003`), 48 kHz, 2 ch, 4096 frames, `32824 B` | Verified in `fir_metrics.json` | **PASS** |
| **Studio Spatial** (Mode 1) | `/home/akshaybapat/project/xps17-studio-audio/acoustics-research/kernels/xps17-studio-spatial.wav` | RIFF WAVE IEEE float32 (`0x0003`), 48 kHz, 2 ch, 4096 frames, `32824 B` | Verified in `fir_metrics.json` | **PASS** |
| **Studio Reference** (Mode 2) | `/home/akshaybapat/project/xps17-studio-audio/acoustics-research/kernels/xps17-studio-reference.wav` | RIFF WAVE IEEE float32 (`0x0003`), 48 kHz, 2 ch, 4096 frames, `32824 B` | Verified in `fir_metrics.json` | **PASS** |
| **Studio Voice** (Mode 3) | `/home/akshaybapat/project/xps17-studio-audio/acoustics-research/kernels/xps17-studio-voice.wav` | RIFF WAVE IEEE float32 (`0x0003`), 48 kHz, 2 ch, 4096 frames, `32824 B` | Verified in `fir_metrics.json` | **PASS** |
| **Synthesis & Verification Script** | `/home/akshaybapat/project/xps17-studio-audio/acoustics-research/fir-harman/generate_studio_firs.py` | Pure Python 3 stdlib + `numpy` | Reproducible | **PASS** |
| **Machine-Readable Metrics** | `/home/akshaybapat/project/xps17-studio-audio/acoustics-research/fir-harman/fir_metrics.json` | Complete Bode/phase/GD/headroom JSON | Verified | **PASS** |

---

## 2. Primary Peer-Reviewed Literature Foundation

Every architectural choice in this FIR pipeline is directly grounded in verified primary peer-reviewed literature:

1. **Olive, S. E. (2004).** *"A Multiple Regression Model for Predicting Loudspeaker Preference Using Objective Measurements: Part I — Listening Test Results"* (AES 116th Convention, Berlin, Preprint 6113) & *"Part II — Development of the Model"* (AES 117th Convention, San Francisco, Preprint 6194).
   - **Design impact:** Double-blind listening tests across ~70 loudspeakers proved that subjective human preference is governed overwhelmingly by smooth, resonance-free on-axis/listening-window magnitude response (`NBD_ON`, `SM_PIR`) and low-frequency extension (`LFX`) — directly motivating elimination of the XPS 17's `+4.2 dB @ 1.5 kHz` and `+5.7 dB @ 2.5 kHz` upper-mid glare and recovery of its `200–350 Hz` foundation.
2. **Olive, S. E., Welti, T., & McMullin, E. (2013).** *"Listener Preferences for Different Headphone Target Response Curves"* (AES 134th Convention, Rome, Paper 8867) & *"Listener Preferences for In-Room Loudspeaker and Headphone Target Responses"* (AES 135th Convention, New York, Paper 8994); extended by **Olive, S. E., Welti, T., & Khonsaripour, O. (2018)** (AES 144th Convention).
   - **Design impact:** Established that listeners across trained and untrained cohorts prefer a smooth in-room downward spectral tilt of **`-0.8` to `-1.0 dB/octave`** from bass to treble combined with a gentle low-frequency warmth shelf (`+3` to `+5 dB` for warm enjoyment, `+1.5 dB` for neutral mastering reference), matching how commercial recordings are mixed and mastered on neutral monitors in semi-reflective rooms.
3. **Toole, F. E. (2017).** *Sound Reproduction: The Acoustics and Psychoacoustics of Loudspeakers and Rooms* (3rd ed., Routledge / Focal Press).
   - **Design impact:** Demonstrates that above the transition/Schroeder frequency (`~300–400 Hz`), steady-state room measurements exhibit narrow, spatially localized non-minimum-phase comb cancellations (desk bounce and boundary interference). Narrow high-Q boosts to fill these dips fail catastrophically: they waste amplifier/transducer headroom, inject severe ringing, and turn into piercing narrow peaks whenever the listener moves their head by even `2–3 cm`. Equalization above `300 Hz` must therefore target psychoacoustically smoothed (`1/3` to `1/2` octave) broad transducer/enclosure timbre errors rather than micro-positional interference notches.
4. **Kirkeby, O., Nelson, P. A., Hamada, H., & Orduña-Bustamante, F. (1998).** *"Fast Deconvolution of Multichannel Systems Using Regularization."* *IEEE Transactions on Speech and Audio Processing*, Vol. 6, No. 2, pp. 189–194.
   - **Design impact:** Formulates FFT-domain Tikhonov-regularized inverse filtering with frequency-dependent regularization parameter $\beta(\omega)$. Increasing $\beta(\omega)$ outside the physically radiate-able transducer passband pushes inverse poles smoothly inside the unit circle, strictly bounding boost magnitude, curbing impulse tail duration, and guaranteeing causal finite-length FIR realizability.
5. **Mourjopoulos, J. N. (1994).** *"Digital Equalization of Room Acoustics."* *Journal of the Audio Engineering Society (JAES)*, Vol. 42, No. 11, pp. 884–900.
   - **Design impact:** Proves that room/speaker transfer functions factor into an invertible minimum-phase component and a non-minimum-phase excess-phase component (propagation delays + boundary reflections). Inverting excess phase produces audible pre-echoes and severe spatial fragility; perceptually robust equalization must invert only the smoothed minimum-phase magnitude envelope.
6. **Hatziantoniou, P. D., & Mourjopoulos, J. N. (2000).** *"Generalized Fractional-Octave Smoothing of Audio and Acoustic Responses."* *JAES*, Vol. 48, No. 4, pp. 259–280; **Glasberg, B. R., & Moore, B. C. J. (1990).** *"Derivation of auditory filter shapes from notched-noise data."* *Hearing Research*, Vol. 47, Issues 1–2, pp. 103–138 ($\text{ERB}(f) = 24.7(4.37 f_{\text{kHz}} + 1)$).
   - **Design impact:** Motivates continuous log-frequency Gaussian smoothing with frequency-varying fractional-octave bandwidth aligned to cochlear critical bands (`1/5` oct in bass/lower-mid -> `1/3` oct in upper-mid -> `1/2` oct in treble).
7. **Oppenheim, A. V., & Schafer, R. W. (2010).** *Discrete-Time Signal Processing* (3rd ed., Pearson).
   - **Design impact:** Provides the exact real-cepstrum / discrete Hilbert-transform minimum-phase reconstruction guaranteeing minimum group delay, maximum front-loaded impulse energy (`> 99.3%` within `2.0 ms`), zero pre-ringing, and the initial-value relation $h_{\min}[0] = \exp\!\big(\frac{1}{2\pi}\int_{-\pi}^{\pi} \ln|H(e^{j\omega})|\,d\omega\big)$.

---

## 3. Forensic Acoustic Analysis of Real XPS 17 9720 Measurements

### 3.1 Hardware Plant Characterization (`baseline_curve.npy` & Raw `gate0_base0..4.wav`)

Deconvolution and time-window forensic inspection of the raw 5-sweep hardware captures (`EasyEffects` stopped, pure stock hardware speakers, `50%` sink volume, `-12 dBFS` 10 s Farina ESS `80 Hz – 16 kHz`) reveals four critical physical facts:

1. **Ultra-Clean Linear Transducer Headroom:**
   - Second harmonic distortion `H2 = -54.7 dB` (`0.18%`), third harmonic `H3 = -54.0 dB` (`0.20%`) at measurement level, with `0.076 dB` 5-sweep repeatability SD across `300 Hz – 10 kHz`.
2. **Nearfield Direct-Dominated Energy Decay (`gate0_base0.wav`):**
   - `62.2%` of total acoustic energy arrives within **`0.5 ms`** of the direct wavefront, `72.6%` within **`1.0 ms`**, `78.0%` within **`2.0 ms`**, **`85.0%` within `4.0 ms`**, and `95.5%` within `20.0 ms`.
   - Windowing the raw single-capture IR to a quasi-anechoic **`4 ms` direct window** agrees with the full **`200 ms` steady-state curve** within **`0.02 dB` across `600 Hz – 4 kHz`** and within **`0.5–1.8 dB` across `5–14 kHz`**. This proves that the broad `1.5 kHz` peak and the severe `-10` to `-15 dB` rolloff above `5 kHz` are **true nearfield speaker/enclosure acoustic defects**, not room reverberation.
3. **Identified Early Comb-Filter Interference Features (Non-Minimum-Phase):**
   - Early impulse inspection exposes discrete reflections at `t = 0.146 ms` (`ΔL = 5.0 cm`, `-2.5 dB` inverted — woofer/tweeter path difference exacerbated by `0xC202 = 0x00` leaving the hardware crossover disabled so both drivers radiate full-range simultaneously!), `t = 0.292 ms` (`ΔL = 10.0 cm`, `-5.3 dB` — lid/chassis diffraction), and `t = 0.625–0.750 ms` (`ΔL = 21–26 cm`, `-13 dB` — desk bounce).
   - These create spatially sensitive interference notches at **`750 Hz` (`-5.64 dB` raw vs `-2.64 dB` 1/3-oct)**, **`5350 Hz` (`-18.06 dB` raw vs `-11.22 dB` 1/2-oct)**, and **`7130 Hz` (`-13.84 dB` raw vs `-10.48 dB` 1/2-oct)**, plus a sharp local interference peak at **`2500 Hz` (`+5.67 dB` raw vs `+3.22 dB` 1/3-oct)**.
4. **ESS Sweep Stimulus Cutoff Boundaries:**
   - `audiolib.py` defined `F_LO = 80.0 Hz`, `F_HI = 16000.0 Hz` with `20 ms` linear edge tapers. Consequently, `baseline_curve.npy` plunges artificially from `-14.47 dB` (`14 kHz`) to `-48.76 dB` (`16 kHz`) and `-97.63 dB` (`18 kHz`). Any naive fractional-octave smoother run directly on `baseline_curve.npy` without boundary conditioning leaks this `-48.8 dB` cliff down into `12.5–14.5 kHz`, corrupting the smoothed treble response by up to **`10–17 dB`**!

#### Measured Stock XPS 17 9720 Acoustic Profile (`baseline_curve.npy`, rel. 1 kHz)

| Frequency (Hz) | Raw `baseline_curve.npy` (rel. bin 1 kHz) | Conditioned & ERB-Smoothed Plant (rel. robust 1 kHz) | Physical Acoustic Interpretation |
|---:|---:|---:|---|
| 80 | `-42.36 dB` | `-34.76 dB` | Sub-resonance rolloff + ESS 80 Hz fade-in edge |
| 100 | `-33.42 dB` | `-31.24 dB` | Closed-box 2 W micro-woofer physical rolloff |
| 120 | `-25.56 dB` | `-25.80 dB` | Lower excursion boundary (handled with sliding HPF + VB) |
| 150 | `-24.88 dB` | `-23.57 dB` | Steep enclosure rolloff transition |
| 200 | `-14.84 dB` | `-16.72 dB` | **Bass-body cavern** (`-15 dB`; must boost boundedly, NOT +15 dB) |
| 250 | `-12.85 dB` | `-12.83 dB` | Upper-bass chest deficiency |
| 300 | `-6.60 dB` | `-7.08 dB` | **Hollow low-mid dip** (physically radiate-able & recoverable!) |
| 450 | `+3.75 dB` | `+2.13 dB` | Chassis/enclosure lower-mid resonance bump |
| 750 | `-5.64 dB` | `-3.48 dB` | Desk-bounce comb notch (`ΔL ≈ 23 cm`) — do NOT spike-invert |
| 1000 | `0.00 dB` | `0.00 dB` | Passband reference (`raw 1 kHz` sits in a `-0.54 dB` local micro-dip) |
| 1500 | `+4.18 dB` | `+2.94 dB` | **Harsh, shouty transducer/enclosure glare peak** |
| 2500 | `+5.67 dB` | `+2.68 dB` | Upper-mid interference + driver breakup peak |
| 4000 | `-3.55 dB` | `-5.00 dB` | Beginning of top-firing grille / off-axis beaming rolloff |
| 5350 | `-18.06 dB` | `-11.76 dB` | **Deep non-minimum-phase woofer/tweeter comb notch** (`0xC202=0`) |
| 6000 | `-7.82 dB` | `-11.75 dB` | Broad high-frequency grille/beaming attenuation |
| 8000 | `-8.02 dB` | `-10.97 dB` | Dull upper-treble shelf |
| 10000 | `-13.13 dB` | `-12.39 dB` | Missing "air" & harmonic shimmer |
| 14000 | `-14.47 dB` | `-15.36 dB` | Upper valid ESS limit (`16 kHz = -48.76 dB` is stimulus cliff) |

---

### 3.2 Forensic Root-Cause Audit of Legacy `fir_design.py` & `xps17-*.irs`

Inspection of `/home/akshaybapat/project/omarchy-audio-tune/fir_design.py`, `fir_results.json`, `v2_curve_*.npy`, and `~/.local/share/easyeffects/irs/xps17-*.irs` uncovered **seven concrete engineering defects** in the legacy kernels:

| # | Legacy Flaw in `fir_design.py` / `xps17-*.irs` | Measured Evidence | Acoustic / DSP Consequence | Fix in New Studio FIRs |
|---|---|---|---|---|
| 1 | **Catastrophic `+3.0 dB` hard-railing bug** (`corr = np.minimum(corr, 3.0)` AFTER pink-mean shift) | `fir_results.json`: **`railed_frac = 0.641`** (Balanced), `0.580` (Warm), `0.728` (Voice), `0.717` (Flat)! `58%–73%` of the entire `150 Hz – 15 kHz` spectrum slammed flat against `+2.0/+2.2 dB`! | Completely collapsed correction shape across bass (`150–300 Hz`) and treble (`5–14 kHz`), leaving `200 Hz` at `-8.1 dB`, `10 kHz` at `-6.4 dB`, and injecting sharp derivative kinks into the cepstrum. | Kirkeby-Nelson $\beta(f)$ + $C^\infty$ softplus envelope BEFORE loudness normalization (`0.0%` hard railing). |
| 2 | **16-bit integer PCM `.irs` export** (`write_wav` quantizes to `int16`) | `file`: `Microsoft PCM, 16 bit`; forced hard attenuation whenever `max|h| > 0.98`, requiring external `output_gain_db = +1.34..+1.73 dB` in EasyEffects. | `-96 dBFS` quantization floor truncates the minimum-phase tail and couples kernel gain to external config constants (`gain = 1.2589` in `91-spatial-fdtr.conf`). | Native 32-bit IEEE float stereo WAV (`wFormatTag = 0x0003`, `fact` chunk), `max|h| < 1.0` naturally with `convolver gain = 1.0`. |
| 3 | **Single-bin `1000 Hz` reference micro-dip bias** | `baseline_curve.npy` bin `1000 Hz` sits `-0.54 dB` below its surrounding 1/3-octave band. | Shifted the entire equalized midrange `+0.54 dB` off-target. | Anchored plant model to robust smoothed `1 kHz` reference. |
| 4 | **Uniform `1/6`-octave linear-bin boxcar smoothing** | `v2_curve_warm.npy`: cutting `-6.1 dB` at the narrow `+5.7 dB @ 2500 Hz` comb peak carved a **`-4.8 dB` hole** in live re-measurement! | Chased position-dependent comb interference and introduced boxcar derivative discontinuities. | ERB-aligned variable-octave $C^\infty$ log-Gaussian smoothing (`1/5 -> 1/3 -> 1/2 oct`) with exact $d(\ln f)$ measure. |
| 5 | **Unconditioned ESS sweep cliff at `16 kHz` & hard kinks at `150 Hz` / `15 kHz`** | Hard ramp down to `-12 dB` below `150 Hz` (killing `85–150 Hz` woofer output) and linear ramp `15 kHz -> 24 kHz`. | Fought downstream physical bass extension and created phase/group-delay bumps at `150 Hz`. | Smooth physical boundary extrapolation before smoothing; gentle sub-80 Hz taper co-designed with Lead 3's `82 Hz` sliding HPF + 3-band virtual bass. |
| 6 | **Piecewise-linear target curves with zero sub-300 Hz shaping** | `design.py`: `np.interp` clamped flat below `300 Hz` with sharp slope kinks at `300 / 1k / 3k / 10k Hz`; `voice` cut `-2.0 dB` at `300 Hz` on top of `-6.6 dB` speaker dip! | Hollow speech in `voice` and unnatural tonal seams across mid/treble transitions. | $C^\infty$ analytic Harman/Olive/Toole targets with smooth logistic shelves and psychoacoustic voice chest/consonant/de-ess shaping. |
| 7 | **Over-boosted `xps17-warm-rich.irs` + `180 Hz` 48 dB/oct HPF conflict** | `xps17-warm-rich.irs` boosted `+10.0 dB` rel 1 kHz at `200–250 Hz` while `91-spatial-fdtr.conf` immediately followed it with an 8th-order `180 Hz` HPF and `+2.0 dB` convolver gain (`+7.2 dB` peak into limiter!). | Wasted headroom at `180–220 Hz` right into a brickwall HPF, causing limiter pumping and thin, hollow perceived bass. | Strictly bounded `<= +9.4 dB` rel 1 kHz (`<= +5.6 dB` below `120 Hz`, `<= +6.06 dB` absolute peak) with documented headroom for Lead 3's sliding HPF & 4x true-peak limiter. |

---

## 4. Mathematical Formulation & Algorithm

### 4.1 Sweep-Edge Conditioning & ERB-Aligned Variable-Octave Smoothing

Let $M_{\text{raw}}(f)$ denote `baseline_curve.npy` relative to `1000 Hz` on the `NFFT = 32768` (`Δf = 1.4648 Hz`) grid. Outside the valid ESS passband `[100 Hz, 14.2 kHz]`, $M_{\text{cond}}(f)$ smoothly blends via $C^1$ cubic Hermite smoothstep into a `-6 dB/oct` physical ultrasonic continuation above `14 kHz` (anchored to the `12.5–14.2 kHz` mean `-14.1 dB`) and a `+12 dB/oct` closed-box continuation below `105 Hz` (anchored to the `95–112 Hz` mean).

Next, $M_{\text{cond}}(f)$ is resampled onto a uniform `2048`-point $\log_2(f)$ grid (`10 Hz – 24 kHz`, `182 pts/oct`) so convolution integrates strictly against the psychoacoustic measure $d(\ln f) = df/f$. The Gaussian kernel width $\sigma(f) = B_{\text{oct}}(f) / (2\sqrt{2\ln 2})$ varies smoothly with frequency:

$$B_{\text{oct}}(f) = \begin{cases} 0.20\text{ oct}\;(1/5\text{ oct}), & f \le 1200\text{ Hz} \\ \text{smoothstep}\to 0.333\text{ oct}\;(1/3\text{ oct}), & 1200 < f \le 2600\text{ Hz} \\ \text{smoothstep}\to 0.500\text{ oct}\;(1/2\text{ oct}), & 2600 < f \le 4800\text{ Hz} \\ 0.500\text{ oct}\;(1/2\text{ oct}), & f > 4800\text{ Hz} \end{cases}$$

Subtracting the robust smoothed `1000 Hz` anchor $M_{\text{sm}}(1\text{ kHz}) = +0.539\text{ dB}$ yields the zero-anchored plant model $M_{\text{plant}}(f)$ (`0.00 dB` at `1 kHz`).

### 4.2 Analytic Harman / Olive / Toole Target Curves $T_m(f)$

Define the smooth logistic low-shelf $\sigma_{\text{lo}}(f; f_c, s) = [1 + (f/f_c)^s]^{-1}$, log-Gaussian bell $\mathcal{G}(f; f_0, w) = \exp\!\big(-\tfrac{1}{2}[\log_2(f/f_0)/w]^2\big)$, and bounded octave coordinate $u(f) = \log_2(\text{clip}(f, 100, 14000)/1000)$. Each target is normalized so $T_m(1000\text{ Hz}) = 0.0\text{ dB}$:

- **Mode 0 — Studio Deep:**
  $$T_{\text{deep}}(f) = -0.85\,u(f) + 2.20\,\sigma_{\text{lo}}(f; 260, 2.3) - T_0$$
  Delivers a `-0.85 dB/oct` Harman-warm in-room tilt plus a smooth `+4.5 dB` warmth shelf across `85–260 Hz` (`+3.3 dB @ 200 Hz`, `+2.3 dB @ 300 Hz`, `-2.3 dB @ 6 kHz`, `-2.9 dB @ 10 kHz`).
- **Mode 1 — Studio Spatial:**
  $$T_{\text{spatial}}(f) = -0.55\,u(f) + 1.50\,\sigma_{\text{lo}}(f; 240, 2.5) + 0.55\,\mathcal{G}(f; 8000, 0.75) - T_0$$
  Balances cinema warmth (`+3.0 dB` shelf `90–250 Hz`), ultra-smooth midrange (`300 Hz – 3 kHz`) for accurate downstream FDTR-XTC localization, and crisp `4–12 kHz` pinna spatial air.
- **Mode 2 — Studio Reference:**
  $$T_{\text{ref}}(f) = -0.50\,u(f) + 0.35\,\sigma_{\text{lo}}(f; 190, 2.8) - T_0$$
  Harman/Olive mastering-neutral in-room target (`-0.50 dB/oct` tilt + subtle `+1.5 dB` bass foundation across `85–200 Hz`, zero mid/treble coloration bells).
- **Mode 3 — Studio Voice:**
  $$T_{\text{voice}}(f) = -12.0\,\sigma_{\text{lo}}(f; 95, 3.6) + 0.65\,\mathcal{G}(f; 220, 0.55) - 0.55\,\mathcal{G}(f; 500, 0.45) + 1.60\,\mathcal{G}(f; 3300, 0.46) - 3.40\,\mathcal{G}(f; 6900, 0.38) - 5.50\,[1 - \sigma_{\text{lo}}(f; 12800, 3.4)] - T_0$$
  Clean `100 Hz` sub-rumble rolloff, solid `160–280 Hz` vocal chest body, subtle `500 Hz` de-boxiness, `+1.5 dB` `2.5–4.5 kHz` consonant intelligibility lift, strict `6–8 kHz` de-essing, and gentle `> 12 kHz` hiss rolloff.

### 4.3 Kirkeby-Nelson Frequency-Dependent Tikhonov Inversion & $C^\infty$ Gain Envelope

Given linear plant magnitude $G(f) = 10^{M_{\text{plant}}(f)/20}$ and target $D(f) = 10^{T_m(f)/20}$, we solve the frequency-dependent regularized inverse problem (Kirkeby et al., 1998):

$$H_{\text{KN}}(f) = \arg\min_H \Big\{ |G(f)H - D(f)|^2 + \beta(f)|H|^2 \Big\} = \frac{D(f)\,G(f)}{G(f)^2 + \beta(f)}$$

with smooth three-zone Tikhonov regularization:
$$\beta(f) = 0.0018 + 0.042\,w_{\text{lo}}(f)^2 + 0.080\,w_{\text{hi}}(f)^2$$
where $w_{\text{lo}}(f)$ transitions from `0` (`>= 215 Hz`) to `1` (`<= 75 Hz`) to protect micro-woofer excursion, and $w_{\text{hi}}(f)$ transitions from `0` (`<= 13.8 kHz`) to `1` (`>= 19.5 kHz`) to curb ultrasonic noise amplification.

To strictly enforce physical driver ceilings without hard-clipping derivative kinks, $H_{\text{KN,dB}}(f)$ passes through a $C^\infty$ softplus smooth-minimum against the frequency-dependent cap $C_{\text{cap}}(f)$ (`<= +9.25..+9.38 dB` in `205 Hz – 14.2 kHz`, smoothly tapering to `<= +4.5..+5.6 dB` at `120 Hz` and `<= -1.0 dB` at `62 Hz`, or `-0.5 / -9.0 dB` in Voice), followed by a gentle sub-`80 Hz` subsonic taper (`-6 dB` at `24 Hz`, `-14 dB` in Voice) and an ultrasonic taper above `14.2 kHz` toward `-5.2 dB` (`-9.0 dB` Voice) at Nyquist `24 kHz`.

### 4.4 Real-Cepstrum Minimum-Phase Reconstruction & Passband Loudness Normalization

Let $H_{\text{des,dB}}(f) = H_{\text{bounded,dB}}(f) - \mu_{\text{pink}}$ where $\mu_{\text{pink}}$ is the pink-weighted ($1/f$) passband mean across `200 Hz – 12 kHz`, guaranteeing exact **`0.000 dB` loudness-neutral passband gain**. Constructing the even `NFFT = 32768` log-magnitude spectrum $L_{\text{full}}[k] = \ln |H_{\text{des}}[k]|$:

1. Real cepstrum: $c[n] = \text{IFFT}(L_{\text{full}}).\text{real}$.
2. Causal minimum-phase folding window: $w_{\text{mp}}[0] = w_{\text{mp}}[N/2] = 1$, $w_{\text{mp}}[1\dots N/2-1] = 2$, $w_{\text{mp}}[N/2+1\dots N-1] = 0$.
3. Analytic Hilbert reconstruction: $h_{\text{full}}[n] = \text{IFFT}\!\big(\exp(\text{FFT}(c \cdot w_{\text{mp}}))\big).\text{real}$.
4. Causal truncation to `NTAPS = 4096` (`85.333 ms` @ 48 kHz) with a smooth raised-cosine half-Hann taper over the final `256` samples (`n = 3840..4095`), followed by exact post-truncation `0.000 dB` pink-weighted trim and cast to IEEE `float32`.

By Oppenheim & Schafer's minimum-phase initial-value theorem $h_{\min}[0] = \exp(\text{mean}_{\text{lin}}(\ln |H|))$, the ultrasonic Nyquist taper (`14.2–24 kHz`) naturally places `max|h[n]| = h[0] ∈ [0.844, 0.987] < 1.000` for all four kernels **without requiring any post-hoc attenuation or external gain multiplier**.

---

## 5. Comprehensive Verification Results (All 4 Production Kernels)

### 5.1 Master Verification & Headroom Contract Table

All metrics below are measured directly on **round-trip read-back float32 samples** from the finalized `.wav` files in `/home/akshaybapat/project/xps17-studio-audio/acoustics-research/kernels/` and confirmed by `sndfile-info`:

| Metric | Validation Gate | `xps17-studio-deep.wav` | `xps17-studio-spatial.wav` | `xps17-studio-reference.wav` | `xps17-studio-voice.wav` |
|---|---|---:|---:|---:|---:|
| **WAV Format / Bytes** | IEEE f32 `0x0003`, 48k, 2ch, 4096f | `32824 B` (PASS) | `32824 B` (PASS) | `32824 B` (PASS) | `32824 B` (PASS) |
| **NaN / Inf Count** | `0` time & freq | `0` (PASS) | `0` (PASS) | `0` (PASS) | `0` (PASS) |
| **Peak Tap Index** | `<= 5` | **`0`** (PASS) | **`0`** (PASS) | **`0`** (PASS) | **`0`** (PASS) |
| **Time Peak `max\|h[n]\|`** | `< 1.000` | `0.9715` (`-0.25 dB`) | `0.9795` (`-0.18 dB`) | `0.9871` (`-0.11 dB`) | `0.8441` (`-1.47 dB`) |
| **Energy in First `1.0 ms`** | Front-loaded | `98.96%` | `99.05%` | `99.06%` | `99.09%` |
| **Energy in First `2.0 ms`** | **`> 95.0%`** | **`99.38%`** (PASS) | **`99.43%`** (PASS) | **`99.43%`** (PASS) | **`99.51%`** (PASS) |
| **Energy in First `5.0 ms`** | Causal decay | `99.98%` | `99.99%` | `99.99%` | `99.99%` |
| **Tail RMS (Last 256 taps)** | Clean zero decay | `-100.17 dB` | `-103.98 dB` | `-101.79 dB` | `-102.97 dB` |
| **Truncation Err (`50–18k`)** | `< 0.25 dB` | `0.107 dB` | `0.130 dB` | `0.181 dB` | `0.169 dB` |
| **Pink Passband Mean (`rms_gain_db`)** | `0.00 ± 0.02 dB` | **`0.000 dB`** (PASS) | **`0.000 dB`** (PASS) | **`0.000 dB`** (PASS) | **`0.000 dB`** (PASS) |
| **Power RMS Gain (`200–12k`)** | Documented | `+2.22 dB` | `+2.29 dB` | `+2.34 dB` | `+1.60 dB` |
| **`gain_1khz_db`** | Documented | `-3.21 dB` | `-3.38 dB` | `-3.36 dB` | `-3.05 dB` |
| **Peak Magnitude (`peak_mag_db`)** | **`<= +9.5 dB`** | **`+6.06 dB`** @ 206 Hz | **`+5.89 dB`** @ 206 Hz | **`+6.04 dB`** @ 208 Hz | **`+4.77 dB`** @ 208 Hz |
| **Peak Boost rel. `1 kHz`** | **`<= +9.5 dB`** | **`+9.27 dB`** (PASS) | **`+9.27 dB`** (PASS) | **`+9.40 dB`** (PASS) | **`+7.82 dB`** (PASS) |
| **Sub-120 Hz Max (Abs / Rel 1k)** | **`<= +6.5 dB`** | `+2.43 / +5.64 dB` | `+1.84 / +5.22 dB` | `+1.15 / +4.51 dB` | `-3.55 / -0.51 dB` |
| **DC / Nyquist Gain** | `<= -5.0 dB` | `-14.24 / -8.39 dB` | `-14.39 / -8.57 dB` | `-14.35 / -8.54 dB` | `-16.99 / -12.03 dB` |
| **Group Delay RMS (`200–10k`)** | `< 0.35 ms` | **`0.223 ms`** (PASS) | **`0.228 ms`** (PASS) | **`0.234 ms`** (PASS) | **`0.242 ms`** (PASS) |
| **Group Delay Max (`200–10k`)** | `< 3.00 ms` | `1.802 ms` @ 297 Hz | `1.913 ms` @ 287 Hz | `2.184 ms` @ 283 Hz | `2.390 ms` @ 201 Hz |
| **`1.5 kHz` Err (Raw / Smooth)** | `<= ±1.0 / ±0.25 dB` | **`+0.72 / +0.01 dB`** | **`+0.72 / +0.01 dB`** | **`+0.72 / +0.01 dB`** | **`+0.72 / +0.01 dB`** |
| **Pink-RMS Err `300–10k` (Before -> Smooth After)** | Minimize | `5.44 -> 0.20 dB` | `5.90 -> 0.55 dB` | `5.74 -> 0.39 dB` | `5.24 -> 0.56 dB` |
| **PipeWire Convolver `gain`** | Unity float | **`1.0`** (`0.0 dB`) | **`1.0`** (`0.0 dB`) | **`1.0`** (`0.0 dB`) | **`1.0`** (`0.0 dB`) |
| **Digital Headroom Required** | Float32 DSP chain | `6.06 dB` | `5.89 dB` | `6.04 dB` | `4.77 dB` |

> **Headroom Contract for Lead 3 (`libxps17_studio_dsp.so`) & PipeWire:** Load every kernel with `gain = 1.0` (`latency = 0.0`). Because PipeWire and LADSPA process in 32-bit IEEE float (`> +100 dB` internal headroom), the `+4.77` to `+6.06 dB` localized spectral peaks (`200–250 Hz` and `10–14 kHz`) cannot clip internally and feed cleanly into Lead 3's Klippel sliding HPF, 4-band dynamics, and 4× true-peak limiter (`-1.0 dBTP` ceiling) at **exact `0.00 dB` perceived loudness parity** (`rms_gain_db = 0.000 dB`).

> **Studio Reference Flatness Audit:** Across **`280 Hz – 10 kHz`**, `xps17-studio-reference.wav` achieves **`[-1.36, +0.01] dB`** max smoothed deviation (**strictly inside `±1.50 dB` everywhere**, `pink-RMS = 0.385 dB`). Across **`250 Hz – 10 kHz`**, `pink-RMS = 0.655 dB`; only the narrow `250–279 Hz` edge sits below `-1.5 dB` (`-4.72 dB` at `250 Hz`) as a deliberate, physically mandatory consequence of capping boost at `<= +9.4 dB` on the `-13.4 dB @ 250 Hz` micro-woofer rolloff to protect the 2 W drivers.

---

### 5.2 Critical 6-Band Predicted Acoustic Performance (`baseline_curve + H_fir`)

All means below are pink-weighted ($1/f$) band averages relative to the robust `1 kHz` reference:

| Band (Hz) | Stock XPS 17 | **Studio Deep** (Pred / Tgt / ΔStock) | **Studio Spatial** (Pred / Tgt / ΔStock) | **Studio Reference** (Pred / Tgt / ΔStock) | **Studio Voice** (Pred / Tgt / ΔStock) |
|---|---:|---:|---:|---:|---:|
| **`100–200`** | `-23.50 dB` | `-16.66` / `+4.04` (`+6.84`) | `-16.96` / `+2.68` (`+6.54`) | `-17.37` / `+1.65` (`+6.12`) | `-21.30` / `-2.19` (`+2.20`) |
| **`200–350`** | `-10.57 dB` | **`-1.89`** / `+2.62` (**`+8.68`**) | **`-2.12`** / `+1.68` (**`+8.45`**) | **`-2.19`** / `+1.06` (**`+8.38`**) | **`-3.51`** / `+0.15` (**`+7.06`**) |
| **`350–1000`** | `-0.65 dB` | **`+0.90`** / `+0.88` (`+1.55`) | **`+0.56`** / `+0.55` (`+1.21`) | **`+0.41`** / `+0.40` (`+1.06`) | **`-0.25`** / `-0.27` (`+0.40`) |
| **`1000–2500`** | `+1.53 dB` | **`-0.57`** / `-0.62` (`-2.10`) | **`-0.33`** / `-0.38` (`-1.86`) | **`-0.29`** / `-0.33` (`-1.81`) | **`+0.35`** / `+0.30` (`-1.18`) |
| **`2500–6000`** | `-5.40 dB` | **`-2.38`** / `-1.75` (`+3.03`) | **`-1.72`** / `-0.89` (`+3.68`) | **`-1.72`** / `-0.98` (`+3.68`) | **`-0.75`** / `+0.29` (`+4.65`) |
| **`6000–12000`** | `-11.29 dB` | **`-2.72`** / `-2.72` (**`+8.57`**) | **`-2.13`** / `-1.23` (**`+9.16`**) | **`-2.06`** / `-1.55` (**`+9.23`**) | **`-3.91`** / `-3.32` (`+7.38`) |

Notice how cleanly every acoustic design objective is achieved:
- **`200–350 Hz` Cavern Eliminated:** Lifted by **`+8.38` to `+8.68 dB`** across Deep/Spatial/Reference (`+7.06 dB` in Voice) — turning a `-10.57 dB` hollow void into a warm, authoritative low-mid body (`300 Hz` lands at **`+1.74 dB`** in Deep, `+1.18 dB` in Spatial, `+0.75 dB` in Reference, `-0.13 dB` in Voice).
- **`350–2500 Hz` Midrange Precision:** Band means match targets within **`0.01–0.05 dB`** across all four modes, completely eliminating the `+4.2 dB @ 1.5 kHz` shoutiness (`+0.16` to `+0.83 dB` raw, `0.008 dB` smoothed error).
- **`6–12 kHz` Studio Air Restored Without Comb Spikes:** Lifted smoothly by **`+8.57` to `+9.23 dB`** in music/cinema/reference modes (landing `Studio Deep` at `-2.72 dB` — `0.00 dB` error vs target!), while `Studio Voice` applies deliberate **`6–8 kHz` de-essing** (`-3.91 dB` band mean; `-0.68 dB @ 6 kHz`, `-1.64 dB @ 8 kHz` raw) for zero-sibilance dialogue clarity.

---

### 5.3 Detailed Bode, Phase, Group Delay & Predicted Acoustic Tables

#### A. `xps17-studio-deep.wav` (Mode 0 — Flagship Studio Deep)

| Freq (Hz) | Stock (dB) | Target (dB) | FIR `H` Abs (dB) | FIR `H` Rel 1k (dB) | Pred Raw (dB) | Pred Smooth (dB) | Phase (deg) | Group Delay (ms) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 80 | `-42.89` | `+4.79` | `-5.87` | `-2.66` | `-45.55` | `-37.39` | `+92.5°` | `-1.669` |
| 100 | `-33.96` | `+4.71` | `+0.15` | `+3.36` | `-30.60` | `-28.21` | `+93.4°` | `+3.796` |
| 120 | `-26.10` | `+4.39` | `+2.45` | `+5.65` | `-20.44` | `-20.04` | `+62.9°` | `+2.028` |
| 150 | `-25.42` | `+3.96` | `+3.83` | `+7.04` | `-18.38` | `-16.90` | `+50.8°` | `+1.109` |
| 200 | `-15.38` | `+3.29` | `+6.04` | `+9.25` | **`-6.13`** | `-7.35` | `+22.9°` | `+1.555` |
| 250 | `-13.39` | `+2.75` | `+6.06` | `+9.27` | **`-4.13`** | `-3.74` | `+1.3°` | `+1.103` |
| 300 | `-7.14` | `+2.30` | `+5.68` | `+8.89` | **`+1.74`** | `+1.85` | `-23.6°` | `+1.793` |
| 350 | `-3.32` | `+1.93` | `+2.05` | `+5.25` | **`+1.93`** | `+1.91` | `-40.1°` | `+0.376` |
| 500 | `+0.85` | `+1.16` | `-3.19` | `+0.02` | `+0.87` | `+1.16` | `-6.2°` | `-0.256` |
| 750 | `-6.18` | `+0.44` | `+1.42` | `+4.63` | `-1.55` | `+0.41` | `-19.3°` | `+0.913` |
| 1000 | `-0.54` | `0.00` | `-3.21` | `0.00` | `-0.54` | `0.00` | `-21.9°` | `+0.056` |
| 1500 | `+3.65` | `-0.55` | `-6.69` | `-3.48` | **`+0.16`** | **`-0.55`** | `+2.7°` | `-0.296` |
| 2500 | `+5.13` | `-1.21` | `-7.45` | `-4.24` | `+0.89` | `-1.20` | `+23.0°` | `-0.227` |
| 4000 | `-4.09` | `-1.79` | `-0.14` | `+3.07` | `-1.03` | `-1.82` | `+56.8°` | `-0.025` |
| 6000 | `-8.36` | `-2.29` | `+5.66` | `+8.87` | **`+0.52`** | `-2.87` | `+18.2°` | `+0.043` |
| 8000 | `-8.56` | `-2.64` | `+4.87` | `+8.08` | **`-0.48`** | `-2.89` | `+4.2°` | `+0.008` |
| 10000 | `-13.67` | `-2.92` | `+5.65` | `+8.86` | **`-4.81`** | `-3.53` | `-5.1°` | `+0.020` |
| 12000 | `-11.47` | `-3.14` | `+5.99` | `+9.20` | `-2.27` | `-4.70` | `-20.9°` | `+0.024` |
| 14000 | `-15.01` | `-3.33` | `+6.05` | `+9.26` | `-5.75` | `-6.10` | `-43.0°` | `+0.045` |

#### B. `xps17-studio-spatial.wav` (Mode 1 — Studio Spatial / Cinema XTC)

| Freq (Hz) | Stock (dB) | Target (dB) | FIR `H` Abs (dB) | FIR `H` Rel 1k (dB) | Pred Raw (dB) | Pred Smooth (dB) | Phase (deg) | Group Delay (ms) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | `-33.96` | `+3.14` | `-1.47` | `+1.91` | `-32.05` | `-29.66` | `+101.9°` | `+1.900` |
| 120 | `-26.10` | `+2.91` | `+1.87` | `+5.26` | `-20.84` | `-20.44` | `+67.8°` | `+2.256` |
| 200 | `-15.38` | `+2.15` | `+5.86` | `+9.25` | **`-6.14`** | `-7.35` | `+24.8°` | `+1.672` |
| 300 | `-7.14` | `+1.46` | `+4.94` | `+8.32` | **`+1.18`** | `+1.29` | `-26.8°` | `+1.507` |
| 500 | `+0.85` | `+0.72` | `-3.81` | `-0.42` | `+0.43` | `+0.72` | `-3.0°` | `-0.273` |
| 1000 | `-0.54` | `0.00` | `-3.38` | `0.00` | `-0.54` | `0.00` | `-18.1°` | `+0.056` |
| 1500 | `+3.65` | `-0.34` | `-6.66` | `-3.27` | **`+0.37`** | **`-0.34`** | `+6.4°` | `-0.295` |
| 3300 | `-2.39` | `-0.86` | `-2.48` | `+0.90` | `-1.49` | `-0.87` | `+49.9°` | `-0.019` |
| 6000 | `-8.36` | `-0.99` | `+5.83` | `+9.21` | **`+0.86`** | `-2.53` | `+19.2°` | `+0.035` |
| 8000 | `-8.56` | `-1.14` | `+5.66` | `+9.04` | **`+0.48`** | `-1.93` | `+2.6°` | `+0.016` |
| 10000 | `-13.67` | `-1.37` | `+5.85` | `+9.23` | **`-4.44`** | `-3.15` | `-9.8°` | `+0.018` |
| 14000 | `-15.01` | `-1.83` | `+5.88` | `+9.26` | `-5.74` | `-6.09` | `-44.8°` | `+0.044` |

#### C. `xps17-studio-reference.wav` (Mode 2 — Harman/Olive Studio Reference)

| Freq (Hz) | Stock (dB) | Target (dB) | FIR `H` Abs (dB) | FIR `H` Rel 1k (dB) | Pred Raw (dB) | Pred Smooth (dB) | Phase (deg) | Group Delay (ms) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | `-33.96` | `+1.96` | `-2.63` | `+0.72` | `-33.24` | `-30.85` | `+105.6°` | `+0.710` |
| 120 | `-26.10` | `+1.80` | `+1.20` | `+4.56` | `-21.54` | `-21.14` | `+71.3°` | `+2.074` |
| 200 | `-15.38` | `+1.32` | `+6.01` | `+9.37` | `-6.01` | `-7.23` | `+27.2°` | `+1.855` |
| 250 | `-13.39` | `+1.11` | `+6.04` | `+9.39` | `-4.00` | `-3.61` | `+1.5°` | `+1.335` |
| 300 | `-7.14` | `+0.94` | `+4.54` | `+7.89` | **`+0.75`** | **`+0.86`** | `-28.5°` | `+1.184` |
| 350 | `-3.32` | `+0.81` | `+0.77` | `+4.13` | **`+0.81`** | **`+0.79`** | `-38.2°` | `+0.217` |
| 500 | `+0.85` | `+0.52` | `-3.98` | `-0.62` | `+0.24` | `+0.52` | `-1.2°` | `-0.274` |
| 1000 | `-0.54` | `0.00` | `-3.36` | `0.00` | `-0.54` | `0.00` | `-16.9°` | `+0.059` |
| 1500 | `+3.65` | `-0.29` | `-6.58` | `-3.22` | **`+0.42`** | **`-0.29`** | `+7.2°` | `-0.292` |
| 4000 | `-4.09` | `-1.00` | `+0.50` | `+3.85` | `-0.24` | `-1.04` | `+58.3°` | `-0.021` |
| 6000 | `-8.36` | `-1.30` | `+5.97` | `+9.33` | **`+0.97`** | `-2.42` | `+19.5°` | `+0.037` |
| 8000 | `-8.56` | `-1.50` | `+5.67` | `+9.03` | **`+0.47`** | `-1.94` | `+3.1°` | `+0.014` |
| 10000 | `-13.67` | `-1.66` | `+6.00` | `+9.36` | **`-4.32`** | **`-3.03`** | `-9.3°` | `+0.019` |

#### D. `xps17-studio-voice.wav` (Mode 3 — Ultra-Articulate Studio Voice)

| Freq (Hz) | Stock (dB) | Target (dB) | FIR `H` Abs (dB) | FIR `H` Rel 1k (dB) | Pred Raw (dB) | Pred Smooth (dB) | Phase (deg) | Group Delay (ms) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 80 | `-42.89` | `-7.66` | `-13.95` | **`-10.90`** | `-53.80` | `-45.64` | `+71.1°` | `-3.236` |
| 100 | `-33.96` | `-5.37` | `-9.81` | **`-6.76`** | `-40.72` | `-38.34` | `+116.0°` | `-4.813` |
| 120 | `-26.10` | `-3.37` | `-3.52` | `-0.48` | `-26.57` | `-26.17` | `+86.6°` | `+1.030` |
| 200 | `-15.38` | `-0.09` | `+4.73` | `+7.78` | `-7.60` | `-8.82` | `+44.3°` | `+2.390` |
| 300 | `-7.14` | `+0.18` | `+3.96` | `+7.01` | **`-0.13`** | **`-0.03`** | `-13.8°` | `+1.637` |
| 500 | `+0.85` | `-0.47` | `-4.65` | `-1.61` | `-0.75` | `-0.46` | `+8.4°` | `-0.300` |
| 1000 | `-0.54` | `0.00` | `-3.04` | `0.00` | `-0.54` | `0.00` | `-8.0°` | `+0.075` |
| 1500 | `+3.65` | `+0.12` | `-5.86` | `-2.81` | **`+0.83`** | **`+0.13`** | `+14.6°` | `-0.290` |
| 3300 | `-2.39` | `+1.53` | `+0.24` | **`+3.29`** | **`+0.89`** | **`+1.52`** | `+42.0°` | `+0.026` |
| 4000 | `-4.09` | `+0.88` | `+2.68` | `+5.72` | **`+1.63`** | `+0.83` | `+39.2°` | `+0.025` |
| 6000 | `-8.36` | `-3.02` | `+4.63` | `+7.68` | **`-0.68`** | **`-4.07`** | `+4.0°` | `+0.033` |
| 7000 | `-13.79` | `-3.87` | `+3.84` | `+6.89` | **`-6.90`** | **`-4.17`** | `-2.4°` | `+0.004` |
| 8000 | `-8.56` | `-3.75` | `+3.88` | `+6.92` | **`-1.64`** | **`-4.05`** | `-3.4°` | `+0.005` |
| 10000 | `-13.67` | `-2.87` | `+4.73` | `+7.78` | `-5.90` | `-4.61` | `-15.4°` | `+0.020` |
| 14000 | `-15.01` | `-3.21` | `+4.77` | `+7.82` | `-7.19` | `-7.54` | `-52.5°` | `+0.047` |

---

### 5.4 Direct Comparison: Stock vs Legacy Kernels vs New Studio Deep FIR

| Characteristic | Stock XPS 17 9720 | Legacy `xps17-warm.irs` (`fir_design.py`) | Legacy `xps17-warm-rich.irs` (`91-spatial-fdtr.conf`) | **New `xps17-studio-deep.wav`** |
|---|---|---|---|---|
| **WAV Bit Depth / Format** | N/A | 16-bit int PCM (`.irs`) | 16-bit int PCM + `+2.0 dB` external gain | **32-bit IEEE float WAV (`0x0003`)**, `gain=1.0` |
| **Hard-Railed Spectrum Fraction** | N/A | **`58.0%`** (slammed at `+3.0 dB` rail!) | Railed below `260 Hz`, `+10.0 dB` into `180 Hz` 48 dB/oct HPF | **`0.0%`** ($C^\infty$ softplus + Kirkeby-Nelson $\beta(f)$) |
| **`200 Hz` Predicted (rel. 1 kHz)** | `-15.38 dB` | `-8.07 dB` (`+6.8 dB` boost) | `-4.9 dB` (then crushed by `180 Hz` 8th-order HPF!) | **`-6.13 dB`** (`+9.25 dB` clean, into `82 Hz` sliding HPF!) |
| **`300 Hz` Predicted (rel. 1 kHz)** | `-7.14 dB` | `+0.17 dB` | `+2.5 dB` | **`+1.74 dB`** (`+8.89 dB`, smooth Harman body) |
| **`1.5 kHz` Harsh Peak** | `+4.18 dB` (raw) | `+0.02 dB` | `-0.5 dB` | **`+0.16 dB`** raw / **`-0.55 dB`** smooth (`0.01 dB` err!) |
| **`2.5 kHz` Over-Cut Check** | `+5.67 dB` (raw) | `-6.1 dB` cut -> **`-4.8 dB` hole** in `v2_curve_warm`! | `-4.0 dB` cut | **`-4.24 dB`** smooth cut (zero off-axis hole!) |
| **`6–10 kHz` Air Recovery** | `-11.3 dB` mean | `+6.1..+6.8 dB` (capped by rail!) | `+4.1..+6.1 dB` (`-7.0 dB @ 10 kHz`) | **`+8.57 dB`** mean (`-2.72 dB`, exact `0.00 dB` band error!) |
| **`E(< 2.0 ms)` Min-Phase Energy** | `78.0%` (plant) | `99.56%` | `99.17%` | **`99.38%` (`pk_idx = 0`, `max|h| = 0.9715`)** |
| **Note on Hardware Crossover (`0xC202`)** | `0xC202 = 0x00` (OFF) | Measured with `0xC202 = 0x00` | Measured with `0xC202 = 0x00` | Designed conservatively with `1/2`-oct treble smoothing so `5.35 kHz` comb notch won't over-boost even after Lead 1 enables `0xC202 = 0xC0`! |

---

## 6. Post-Hardware-Fix Re-Calibration Note (Coordination with Lead 1)

Because `baseline_curve.npy` was captured while Dell's in-amp biquad crossover (`0xC202 = 0x00`) was disabled on both `rt1316` amplifiers, woofer/tweeter overlap contributed to the `5.35 kHz` (`-18.1 dB`) and `7.13 kHz` (`-13.8 dB`) comb notches. By deliberately applying **`1/2`-octave psychoacoustic Gaussian smoothing** across `4.8–14 kHz` (Toole, 2017) rather than narrow-Q inversion, our kernels restore the true broad treble shelf without introducing any sharp `+15 dB` spikes at `5.35 kHz` that could ring once Lead 1 restores `0xC202 = 0xC0`. Once the Coordinator deploys Lead 1's kernel fix and captures a fresh 10-second Yeti X ESS sweep with hardware crossover active, re-running `generate_studio_firs.py` on the updated `.npy` file takes **`< 0.5 seconds`** and deterministically updates all four kernels with full automated gate verification.
