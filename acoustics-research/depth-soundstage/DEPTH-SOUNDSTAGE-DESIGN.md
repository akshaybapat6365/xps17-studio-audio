# Dell XPS 17 9720 Studio Audio — Spatial Psychoacoustics, Depth & Soundstage DSP Architecture

**Author:** Senior Spatial Psychoacoustics & Soundstage DSP Researcher (Lead 2 — Acoustic Physics & DSP Architecture)  
**Target Platform:** Dell XPS 17 9720 (*Stradale*) — Quad speakers (`2×` woofers + `2×` tweeters), top-firing flank span $S = 0.340\text{ m}$, nominal desk listening distance $D = 0.550\text{ m}$, sample rate $f_s = 48\,000\text{ Hz}$  
**Companion Deliverables:**
- Machine-readable parameters & 256-tap float32 Shuffler FIR: [`spatial_spec.json`](./spatial_spec.json)
- Pure-NumPy offline verification suite: [`verify_spatial_depth.py`](./verify_spatial_depth.py)

---

## 1. Executive Summary

Forensic inspection of the existing PipeWire spatial graph (`~/.config/pipewire/omarchy-speaker-engine.conf.d/91-spatial-fdtr.conf`) and Rust DSP engine (`/home/akshaybapat/project/omarchy-spatial-audio/src/{fdtr.rs,dsp.rs,ladspa.rs}`) proves that the hollow, thin, flat, and lifeless sound was **not** caused by a single filter bug, but by **six compounding psychoacoustic and gain-staging failures**:

1. **Pre-Limiter Side-Boost Pumping (Center Vocal & Bass Ducking):** The 2×2 FDTR-XTC matrix boosts the stereo Side signal $S = \frac{1}{2}(L - R)$ by up to **`+3.56 dB`** (`+3.40 dB` after Kaiser windowing at $668\text{ Hz}$) **before** a `+7.0 dB` broadband 100% stereo-linked fast-release (`20 ms`) brickwall limiter (`lim`). On a mastered `-1.0 dBFS` stereo mix, this Side surge forces an **extra `-1.21 dB` of direct Mid-channel vocal and bass ducking** during stereo choruses (up to `-3.4 dB` worst-case on hard-panned peaks) while shifting the Side-to-Mid balance upward by `+3.4 dB` — collapsing center body and making music sound hollow.
2. **8th-Order (`48 dB/oct`) Static `180 Hz` Brickwall HPF (`91-spatial-fdtr.conf:42–43`):** Obliterates real physical bass by **`-18.51 dB`** at `80 Hz` (`-17.67 dB` vs input), creating an extreme **`-6.69 dB`** verse-to-chorus limiter pumping swing on centered vocals because the bass-stripped verse barely hits the limiter while the XTC-boosted chorus slams it.
3. **Lack of Bass Mono-Anchoring (`< 180 Hz`):** `fdtr.rs` sets strict unity bypass below `200 Hz`, leaving stereo phase differences intact where closely spaced woofers ($S = 0.340\text{ m}$, $kS = 1.121\text{ rad} < \pi/2$ at `180 Hz`) suffer **`-9.6` to `-16.6 dB`** mutual dipole cancellation while burning full cone excursion $X_{\max}$.
4. **Zero Natural Early-Reflection Depth Engine:** Hardwiring 100% transaural XTC widens only pre-existing L–R differences and narrows head-position tolerance without creating front-to-back studio depth or Apparent Source Width (ASW) on dry vocals or narrow studio recordings.
5. **Broken Missing-Fundamental Bass & Frozen ISO 226 Contour:** `dsp.rs:430–459` attenuates Chebyshev harmonics by `0.42 × 0.55 = 0.231`, injects DC/envelope artifacts via $T_2 = 2u^2 - 1$, chokes bass during singing via `speech_gate`, and freezes `Loudness Contour = 0.25` (`+1.25 dB`) disconnected from `omarchy-speaker-dsp-controller`.
6. **4× Computational & 5.8× Latency Overhead:** Running a symmetric 2×2 512-tap FIR (`2048 MACs/sample`, `5.333 ms` causal delay) when Cooper–Bauck diagonalization reduces symmetric XTC to a single 1D Side filter.

Our redesigned **Studio Depth & Soundstage DSP Architecture** resolves every single failure mathematically and is offline-verified via pure `numpy` simulation (`verify_spatial_depth.py`, all assertions passing):

| Metric | Old `91-spatial-fdtr.conf` | New `Studio Deep` *(Mode 0, Flagship)* | New `Studio Spatial` *(Mode 1, Wide)* |
|---|---|---|---|
| **Spatial Extra Vocal Ducking (Mid Loss)** | `-1.21 dB` *(XTC alone)* / `-6.69 dB` chorus swing | **`+0.03 dB`** *(zero pumping)* | **`-0.04 dB`** *(30× reduction)* |
| **Spatial Extra Bass Ducking / `80 Hz` Retention** | `-18.51 dB` loss (`-17.67 dB` vs in) | **`-0.00 dB`** loss (`+0.05 dB` vs in) | **`-0.07 dB`** loss (`-0.02 dB` vs in) |
| **Bass Mono-Anchor (`< 180 Hz`)** | None (`H_ll = 1, H_lr = 0`) | **Exact 3-biquad LR4** (`+5.6..+5.9 dB` coupling) | **Exact 3-biquad LR4** (`+5.6..+5.9 dB` coupling) |
| **Studio Depth Early Reflections** | None | **2-stage Schroeder AP + 4.4–13.7 ms comb-free ER** (`-15.5 dB` wet) | **Same ER network** (`-17.8 dB` wet) |
| **Mono-Sum Comb Ripple (`200 Hz – 8 kHz`)** | `0.00 dB` (no ER) | **`±0.375 dB` raw** (`±0.046 dB` 1/3-oct) mono; **`±0.839 dB`** hard-L | **`±0.287 dB` raw** mono |
| **Acoustic Ear `IACC_E3` (Mono Vocal / Studio Mix)** | Unanchored off-axis | **`0.9855` (>0.92)** / **`0.8195`** *(from `0.8548`)* | **`0.9914`** / **`0.5565`** *(diffuse from `0.5908`)* |
| **Shuffler XTC Max Side Boost / Mean Mid XTC** | `+3.40 dB` (`+3.56 dB` ideal) / `13.8 dB` | OFF (`0.0` default; zero phase artifacts) | **`+2.28 dB`** (`<= +2.8 dB`) / **`12.55 dB`** (`11.1 dB` @ 3 cm) |
| **Spatial Latency / MACs per sample** | `5.333 ms` / `2048 MACs` | **`0.000 ms`** / `~24 FLOPs` | **`0.917 ms`** (`44` smp) / **`256 MACs`** (**8× faster**) |

---

## 2. Primary Peer-Reviewed Psychoacoustic & Acoustic Literature Foundation

Every architectural decision in this specification is grounded in primary peer-reviewed literature:

1. **Early Lateral Reflections, Apparent Source Width (ASW), and Foreground/Background Segregation:**
   - **David Griesinger (1997)**, *"The Psychoacoustics of Apparent Source Width, Spaciousness and Envelopment in Performance Spaces,"* *Acta Acustica united with Acustica*, Vol. 83, No. 4, pp. 721–731.
   - **David Griesinger (1996)**, *"Spaciousness and Envelopment in Musical Acoustics,"* *Audio Engineering Society (AES) 101st Convention*, Los Angeles, Preprint 4401.
   - **David Griesinger (2000)**, *"The Theory and Practice of Perceptual Modeling — How to Use Electronic Reverberation to Add Depth and Envelopment Without Reducing Clarity,"* *21st Tonmeistertagung*, Hanover; and *"General Overview of Spatial Impression, Envelopment, Localization, and Externalization"* (2000).
   - **Takayuki Hidaka, Leo L. Beranek, and Toshiyuki Okano (1995)**, *"Interaural cross-correlation, lateral fraction, and low- and high-frequency sound levels as measures of acoustical quality in concert halls,"* *Journal of the Acoustical Society of America (JASA)*, Vol. 98, No. 2, pp. 988–1007.
   - **Design Consequence:** Griesinger (1996, 1997, 2000) proves that **early lateral reflections arriving within `< 80 ms`** (specifically `4–15 ms` for nearfield studio depth) broaden **Apparent Source Width (ASW)** and externalize the soundstage during note onsets without smearing foreground vocal clarity, whereas long diffuse late tails (`> 100–300 ms`) blur articulation and degrade speech intelligibility. Hidaka, Beranek & Okano (1995) formalize early interaural cross-correlation **`IACC_E3`** (averaged across the `500`, `1000`, and `2000 Hz` octave bands over `0–80 ms`, $|\tau| \le 1\text{ ms}$) as the primary objective correlate of spaciousness (`1 - IACC_E3`).

2. **Frequency-Dependent Regularized Crosstalk Cancellation (FDTR / BACCH) & Stereo Dipole Physics:**
   - **Edgar Y. Choueiri (2010)**, *"Optimal Crosstalk Cancellation for Binaural Audio with Two Loudspeakers,"* Princeton University 3D3A Lab Technical Report (theoretical foundation of BACCH™ filters).
   - **Takashi Takeuchi and Philip A. Nelson (2002)**, *"Optimal source distribution for binaural synthesis over loudspeakers,"* *Journal of the Acoustical Society of America (JASA)*, Vol. 112, No. 6, pp. 2786–2797.
   - **Darren B. Ward and Gary W. Elko (1999)**, *"Effect of loudspeaker position on the robustness of acoustic crosstalk cancellation,"* *IEEE Signal Processing Letters*, Vol. 6, No. 5, pp. 106–108.
   - **Duane H. Cooper and Jerald L. Bauck (1989)**, *"Prospects for Transaural Recording,"* *Journal of the Audio Engineering Society (JAES)*, Vol. 37, No. 1/2, pp. 3–19.
   - **Design Consequence:** Naive inversion of the 2×2 loudspeaker-to-ear transfer matrix blows up at acoustic cancellation singularities ($f \to 0$, $f_{M,1} = c/(2\Delta r) \approx 3.29\text{ kHz}$, $f_{S,1} = c/\Delta r \approx 6.59\text{ kHz}$), producing severe spectral coloration and massive Side-channel gain surges. Choueiri (2010) proves that **frequency-dependent Tikhonov regularization $\beta(f)$** eliminates coloration peaks while maximizing sweet-spot separation and off-axis head robustness. Cooper & Bauck (1989) prove that any symmetric 2×2 transaural matrix diagonalizes algebraically into a **1D Mid/Side Shuffler** ($H_M, H_S$), enabling bit-exact vocal center preservation ($H_M \equiv 1$) and drastic CPU reduction.

3. **Precedence / Haas Localization Dominance, Duplex Theory, and Auditory Distance Cues:**
   - **Jens Blauert (1997, revised ed.)**, *Spatial Hearing: The Psychophysics of Human Sound Localization*, The MIT Press, Cambridge, MA.
   - **Helmut Haas (1951 / 1972)**, *"The Influence of a Single Echo on the Audibility of Speech"* (*Über den Einfluss eines Einfachechos auf die Hörsamkeit von Sprache*, Göttingen, 1951), translated in *Journal of the Audio Engineering Society (JAES)*, Vol. 20, No. 2, pp. 146–159, March 1972.
   - **C. P. Brown and R. O. Duda (1998)**, *"A Structural Model for Binaural Sound Synthesis,"* *IEEE Transactions on Speech and Audio Processing*, Vol. 6, No. 5, pp. 476–488.
   - **George F. Kuhn (1977)**, *"Model for the interaural time differences in the azimuthal plane,"* *Journal of the Acoustical Society of America (JASA)*, Vol. 62, No. 1, pp. 157–167.
   - **Design Consequence:** Under the **Law of the First Wavefront (Haas/Precedence Effect)**, reflections delayed by **`4–14 ms`** and attenuated `-14` to `-18 dB` below the direct wavefront fuse seamlessly into a single perceived event whose direction remains **100% locked** to the direct sound, while the **Direct-to-Reverberant Ratio (DRR)** and **high-frequency wall/air absorption (`6.5 kHz` LPF)** provide vivid front-to-back distance depth. Furthermore, per Duplex Theory (Rayleigh / Blauert 1997), ITDs dominate localization below `~1.5 kHz` while ILDs and pinna cues dominate above `~1.5 kHz`; Kuhn (1977) shows spherical head diffraction lengthens low-frequency ITDs by `~1.5×` below `500 Hz` compared to high-frequency Woodworth ray paths.

4. **Bass Mono-Anchoring, Linkwitz–Riley Phase-Matched Crossovers, and Mutual Acoustic Coupling:**
   - **Michael A. Gerzon (1992 / 1994)**, *"Applications of Blumlein Shuffling to Stereo Microphone Techniques,"* *AES 93rd Convention* (1992) / *JAES*, Vol. 42, No. 6, pp. 435–453 (June 1994).
   - **Siegfried H. Linkwitz (1976)**, *"Active Crossover Networks for Noncoincident Drivers,"* *Journal of the Audio Engineering Society (JAES)*, Vol. 24, No. 1, pp. 2–8.
   - **Lawrence E. Kinsler, Austin R. Frey, Alan B. Coppens, and James V. Sanders (2000)**, *Fundamentals of Acoustics*, 4th ed., John Wiley & Sons (mutual radiation resistance of closely spaced monopoles $R_{12} = R_{11}\sin(kS)/(kS)$).
   - **Design Consequence:** Two woofers separated by $S = 0.340\text{ m}$ satisfy $kS < \pi/2$ across the entire bass band up to `252 Hz`, meaning in-phase mono bass adds up to **`+5.93 dB` acoustic power** vs a single woofer (`+2.92 dB` free power vs uncorrelated stereo) while out-of-phase Side bass suffers **`-9.6` to `-16.6 dB` dipole cancellation**. An **LR4 (`24 dB/oct`) crossover** satisfies $H_{\text{LP,LR4}}(z) + H_{\text{HP,LR4}}(z) \equiv H_{\text{AP2}}(z)$ with exact unity magnitude and `0.0000°` phase error between Mid and Side.

5. **Colorless All-Pass Decorrelation & Comb-Free Early Reflection Synthesis:**
   - **Manfred R. Schroeder (1962)**, *"Natural Sounding Artificial Reverberation,"* *Journal of the Audio Engineering Society (JAES)*, Vol. 10, No. 3, pp. 219–223.
   - **Jean-Marc Jot and Antoine Chaigne (1991)**, *"Digital Delay Networks for Designing Artificial Reverberators,"* *AES 90th Convention*, Paris, Preprint 3030.
   - **Gary S. Kendall (1995)**, *"The Decorrelation of Audio Signals and Its Impact on Spatial Imagery,"* *Computer Music Journal*, Vol. 19, No. 4, pp. 71–87.
   - **Jon Dattorro (1997)**, *"Effect Design, Part 1: Reverberator and Other Filters,"* *Journal of the Audio Engineering Society (JAES)*, Vol. 45, No. 9, pp. 660–684.
   - **Design Consequence:** Cascaded coprime-length Schroeder all-pass filters achieve strictly unity magnitude ($|H_{\text{AP}}(e^{j\omega})| \equiv 1$, zero timbral coloration) while scrambling inter-channel phase; pairing them with prime-spaced, polarity-alternated cross-coupled early taps and a comb-cancelling output scattering matrix delivers spacious decorrelation with `< ±0.4 dB` mono-sum coloration.

---

## 3. Forensic Mathematical Diagnosis of `91-spatial-fdtr.conf` + `fdtr.rs` + `dsp.rs`

### 3.1 Exact Dell XPS 17 9720 Nearfield Acoustic Geometry

With top-firing speaker flank span $S = 0.340\text{ m}$ ($S/2 = 0.170\text{ m}$), nominal desk listening distance $D = 0.550\text{ m}$, anthropometric head radius $a = 0.0875\text{ m}$ ($d_{\text{ear}} = 2a = 0.175\text{ m}$; human range $0.17\text{–}0.18\text{ m}$), and speed of sound $c = 343.0\text{ m/s}$:

$$\theta = \arctan\!\left(\frac{S/2}{D}\right) = \arctan\!\left(\frac{0.170}{0.550}\right) = 17.1759^\circ \implies 2\theta = \mathbf{34.3518^\circ}$$

The XPS 17 forms a narrow stereo dipole — roughly half the standard ITU-R BS.775 $60^\circ$ studio monitor triangle. Its acoustic path-length difference $\Delta r = r_c - r_i$, interaural time delay $\tau = \Delta r / c$, and crosstalk matrix eigenvalues $\lambda_{\pm}(\omega) = 1 \pm g(\omega)e^{-j\omega\tau}$ yield the exact critical frequencies below:

| Acoustic Model | $r_i$ (m) | $r_c$ (m) | $\Delta r$ (cm) | ITD $\tau$ (µs) | Optimal OSD $f_{\text{opt}} = \frac{1}{4\tau}$ | 1st Mid Comb $f_{M,1} = \frac{1}{2\tau}$ | 1st Side Singularity $f_{S,1} = \frac{1}{\tau}$ |
|---|---:|---:|---:|---:|---:|---:|---:|
| Free-field geometric ($d_{\text{ear}} = 17.0\text{ cm}$) | `0.55653` | `0.60624` | `4.971` | `144.92` | `1725.0 Hz` | `3450.1 Hz` | `6900.2 Hz` |
| **Free-field geometric ($d_{\text{ear}} = 17.5\text{ cm}$)** | **`0.55615`** | **`0.60729`** | **`5.114`** | **`149.10`** | **`1676.7 Hz`** | **`3353.5 Hz`** | **`6706.9 Hz`** |
| Free-field geometric ($d_{\text{ear}} = 18.0\text{ cm}$) | `0.55579` | `0.60836` | `5.257` | `153.27` | `1631.1 Hz` | `3262.3 Hz` | `6524.5 Hz` |
| **Woodworth HF rigid sphere ($a = 8.75\text{ cm}$)** | — | — | **`5.207`** | **`151.81`** | **`1646.8 Hz`** | **`3293.7 Hz`** | **`6587.3 Hz`** |
| Kuhn LF diffraction limit ($f < 500\text{ Hz}$) | — | — | `7.752` | `226.00` | — | — | — |

Geometric attenuation ratio $g_{\text{geom}} = r_i / r_c = 0.91579$ (`-0.764 dB`). Note the physical significance of the two distinct singularities of $\mathbf{C}(\omega)$:
- **Side Singularity ($\lambda_- = 1 - g e^{-j\omega\tau} \to \text{min}$):** Occurs at **$f = 0\text{ Hz}$** (where $|\lambda_-| \to 1 - g_{\text{geom}} = 0.0842 = -21.5\text{ dB}$, causing massive low-mid Side boost under naive inversion!) and at **$f_{S,1} = c/\Delta r \approx 6587\text{ Hz}$** (where high-frequency XTC becomes hypersensitive to head motion).
- **Mid Comb Singularity ($\lambda_+ = 1 + g e^{-j\omega\tau} \to \text{min}$):** Occurs at **$f_{M,1} = c/(2\Delta r) \approx 3294\text{ Hz}$** (right inside the human vocal presence band `2.5–4.0 kHz`), requiring elevated regularization $\beta(f)$ across `2.4–3.3 kHz`.
- **Optimal Conditioning Frequency:** $f_{\text{opt}} = c/(4\Delta r) \approx 1647\text{ Hz}$ ($\omega\tau = \pi/2$, $|\lambda_+| = |\lambda_-|$, condition number $\kappa = 1$).

### 3.2 Mathematical Proof of Pre-Limiter Side-Boost Mid-Channel Ducking

In `91-spatial-fdtr.conf` (lines 89–110) and `dsp.rs` (lines 507–625), the signal chain executes:

$$\text{Stereo Signal } (L, R) \xrightarrow{\text{2×2 FDTR-XTC } (\max |H_S| = +3.56\text{ dB})} (L_{\text{xtc}}, R_{\text{xtc}}) \xrightarrow{+7.0\text{ dB Drive}} \text{Broadband Fast Limiter } (-1.0\text{ dBFS},\ t_{\text{rel}} = 20\text{ ms},\ \text{slink} = 100\%)$$

Writing the signals in the Mid/Side basis $M = \frac{1}{2}(L + R)$, $S = \frac{1}{2}(L - R)$, `fdtr.rs:149–151` sets $H_{ll} = 1 - H_{lr}$ so $H_M(\omega) = H_{ll} + H_{lr} = 1$ and $H_S(\omega) = 1 - 2 H_{lr}(\omega)$, reaching $|H_S| = +3.563\text{ dB}$ (`1.507×` linear) at `668 Hz` (`+3.402 dB` after windowing in `kernels_meta.json`).

The peak instantaneous stereo amplitude entering the stereo-linked limiter satisfies the exact identity:

$$\max\big(|L_{\text{xtc}}(t)|,\, |R_{\text{xtc}}(t)|\big) = \max\big(|M(t) + (h_S * S)(t)|,\, |M(t) - (h_S * S)(t)|\big) = |M(t)| + |(h_S * S)(t)|$$

When driven by $G = 10^{7/20} = 2.2387$ into ceiling $C = 10^{-1/20} = 0.8913$, the 100% stereo-linked broadband limiter computes a single scalar gain reduction factor:

$$g_{\text{lim}}(t) = \min\!\left(1,\ \frac{C}{G \cdot \big(|M(t)| + |(h_S * S)(t)|\big)}\right)$$

Because $g_{\text{lim}}(t)$ multiplies **both** channels identically:

$$M_{\text{out}}(t) = g_{\text{lim}}(t)\, G\, M(t), \qquad S_{\text{out}}(t) = g_{\text{lim}}(t)\, G\, (h_S * S)(t)$$

Every extra decibel of XTC Side boost ($|h_S * S| \approx 1.48 |S|$ in `400–1000 Hz`) **directly steals limiter headroom from the Mid channel $M(t)$** (centered vocals, kick, bass, snare body):

$$\Delta M_{\text{duck}}(t) = 20\log_{10}\!\left(\frac{|M(t)| + |S(t)|}{|M(t)| + |H_S|\cdot|S(t)|}\right) < 0$$

- In the limit $|S| \gg |M|$ during wide stereo guitar/synth transients, $\Delta M_{\text{duck}} \to -20\log_{10}|H_S| = \mathbf{-3.40\text{ dB}}$!
- Simultaneously, the output Side-to-Mid ratio remains inflated by $S_{\text{out}}/M_{\text{out}} = (H_S * S)/M$ (`+3.40 dB`), so **centered vocals and bass are actively pushed backward every time wide stereo accompaniment enters**, creating the exact hollow, thin, pumping sound reported on the XPS 17.
- Compounding this, `91-spatial-fdtr.conf:42–43` inserts an **8th-order (`48 dB/oct`) `180 Hz` HPF** (`lsp filter_stereo`, `s=3`) upstream of the limiter: stripping sub-180 Hz bass out of verses leaves verses untouched by the limiter (`0.00 dB GR`), so when the XTC-boosted chorus hits, the limiter slams down by **`-6.69 dB`**, visibly ducking the lead vocal on every chorus entry!

### 3.3 Verified `numpy` Forensic Simulation Results (`verify_spatial_depth.py`)

Test signal: Mastered `-1.0 dBFS` peak (`-10.98 dBFS` chorus RMS) 48 kHz stereo mix consisting of centered `80 Hz + 160 Hz` bass and centered harmonic lead vocal (`220–2420 Hz`) held identical across Verse (`0.2–0.9 s`) and Chorus (`1.2–1.9 s`), with wide stereo guitars/synths (`515–1115 Hz` pure Side) entering in the Chorus:

| Pipeline Under Test | Chorus RMS (dBFS) | Peak (dBFS) | Limiter GR (Verse → Chorus) | Spatial Extra Mid Vocal Loss | Spatial Extra Mid Bass Loss | `80 Hz` Bass Retention vs Input |
|---|---:|---:|---:|---:|---:|---:|
| `0.` Baseline (`No XTC → +7 dB 20 ms Lim`) | `-10.19` | `-1.00` | `-1.54 → -6.18 dB` | `0.00 dB` | `0.00 dB` | `+0.84 dB` |
| `1A.` **OLD `fdtr.rs` XTC → `+7 dB` 20 ms Lim** | `-9.67` | `-1.00` | `-1.54 → -7.40 dB` | **`-1.21 dB`** | **`-1.21 dB`** | `-0.37 dB` |
| `1B.` **OLD FULL `91-spatial-fdtr.conf`** | `-9.97` | `-1.01` | `+0.00 → -6.69 dB` | `-0.58 dB` *(total chorus duck **`-6.69 dB`**)* | **`-18.51 dB`** | **`-17.67 dB`** *(obliterated)* |
| `2A.` NEW 4-Band MBC (Spatial OFF) | `-11.18` | `-1.40` | `0.00 → 0.00 dB` | `0.00 dB` | `0.00 dB` | `+0.05 dB` |
| `2B.` **NEW `Studio Deep` (`Depth 0.85, XTC 0.0`)** | `-11.02` | `-1.21` | `0.00 → 0.00 dB` | **`+0.03 dB`** | **`-0.00 dB`** | **`+0.05 dB`** (`+17.7 dB` vs old) |
| `2C.` **NEW `Studio Spatial` (`Depth 0.65, XTC 0.75`)** | `-10.83` | `-1.00` | `0.00 → -0.07 dB` | **`-0.04 dB`** | **`-0.07 dB`** | **`-0.02 dB`** (`+17.7 dB` vs old) |

---

## 4. Studio Depth & Soundstage DSP Architecture

### 4.1 Block A — Strict Bass Mono-Anchoring (`< 180 Hz`, 3-Biquad Phase-Matched Bilinear LR4)

#### Mutual Acoustic Coupling Physics ($S = 0.340\text{ m}$)
Per Kinsler et al. (2000), two closely spaced monopoles of volume velocities $U_L, U_R$ separated by $S$ share mutual radiation resistance $R_{12} = R_{11}\frac{\sin(kS)}{kS}$, where $k = 2\pi f / c$. Total radiated acoustic power is:

$$W = \frac{1}{2} R_{11} \left( |U_L|^2 + |U_R|^2 + 2\operatorname{Re}(U_L U_R^*) \frac{\sin(kS)}{kS} \right)$$

For the Dell XPS 17 9720 ($S = 0.340\text{ m}$), $kS < \pi/2$ holds for all frequencies $f < c/(4S) = 252.2\text{ Hz}$:

| Frequency | Wavelength $\lambda$ | $kS$ (rad) | $\operatorname{sinc}(kS)$ | In-Phase Mono vs 1 Woofer | In-Phase Mono vs Uncorrelated | Out-of-Phase Side Dipole vs Uncorrelated | **Mono Advantage over Dipole** |
|---:|---:|---:|---:|---:|---:|---:|---:|
| `65 Hz` | `5.28 m` | `0.405` | `0.9729` | **`+5.96 dB`** | `+2.95 dB` | `-15.67 dB` | **`+18.62 dB`** |
| **`82 Hz`** *(HPF floor)* | `4.18 m` | `0.511` | `0.9571` | **`+5.93 dB`** | **`+2.92 dB`** | `-13.67 dB` | **`+16.59 dB`** |
| `100 Hz` | `3.43 m` | `0.623` | `0.9366` | **`+5.88 dB`** | `+2.87 dB` | `-11.98 dB` | **`+14.85 dB`** |
| `120 Hz` | `2.86 m` | `0.747` | `0.9095` | **`+5.82 dB`** | `+2.81 dB` | `-10.43 dB` | **`+13.24 dB`** |
| `150 Hz` | `2.29 m` | `0.934` | `0.8608` | **`+5.71 dB`** | `+2.70 dB` | `-8.56 dB` | **`+11.26 dB`** |
| **`180 Hz`** *(LR4 $f_c$)* | `1.91 m` | **`1.121`** | `0.8033` | **`+5.57 dB`** | **`+2.56 dB`** | `-7.06 dB` | **`+9.62 dB`** |
| `260 Hz` | `1.32 m` | `1.619` | `0.6168` | `+5.10 dB` | `+2.09 dB` | `-4.17 dB` | `+6.25 dB` |

Forcing bass below `180 Hz` to in-phase mono recovers up to **`+2.92 dB` of free acoustic bass output over uncorrelated stereo** and **`+16.59 dB` over out-of-phase bass** at zero additional woofer excursion $X_{\max}$!

#### Exact 3-Biquad Bilinear LR4 M/S Topology (Linkwitz 1976)
Splitting $(L, R)$ via identical 4th-order Linkwitz–Riley crossovers ($f_c = 180\text{ Hz}$, `150 Hz` in Studio Reference), summing $B_{\text{mono}} = \frac{1}{2}(L_{\text{low}} + R_{\text{low}}) = H_{\text{LP,LR4}} M$, and recombining $L' = B_{\text{mono}} + L_{\text{high}}$, $R' = B_{\text{mono}} + R_{\text{high}}$ simplifies **algebraically** in the Mid/Side domain via the exact bilinear-transform rational identity:

$$H_{\text{LP,LR4}}(z) + H_{\text{HP,LR4}}(z) \equiv H_{\text{AP2}}(z; f_c, Q = 1/\sqrt{2})$$

$$M' = H_{\text{AP2}}(f_c) \cdot M, \qquad S' = \big(H_{\text{HP,BW2}}(f_c)\big)^2 \cdot S, \qquad L' = M' + S',\quad R' = M' - S'$$

- **CPU Cost:** Only **3 biquads total** (vs 8 biquads for two stereo LR4 crossovers — **62.5% lower CPU**!).
- **Verified Exactness (`verify_spatial_depth.py`):**
  - Bilinear identity max error $|H_{\text{LP4}} + H_{\text{HP4}} - H_{\text{AP2}}| = 9.24 \times 10^{-13}$.
  - Mid magnitude flatness (`20 Hz – 20 kHz`): **`±9.70e-12 dB`** (bit-exact all-pass unity).
  - Inter-channel M/S phase error across crossover (`20 Hz – 20 kHz`): **`0.0000°`** (zero transition-band comb filtering!).
  - Side attenuation: `60 Hz: -38.28 dB`, `90 Hz: -24.61 dB`, `120 Hz: -15.65 dB`, `150 Hz: -9.75 dB`, **`180 Hz: -6.02 dB`**, `260 Hz: -1.80 dB`, `360 Hz: -0.53 dB`.

---

### 4.2 Block B — Natural Studio Depth Early-Reflection Engine (`Stage Depth`, Flagship `Studio Deep`)

Designed around Griesinger (1997), Blauert (1997), Haas (1951/1972), Schroeder (1962), and Kendall (1995), the Studio Depth Engine synthesizes lush, three-dimensional front-to-back nearfield studio monitor depth **without XTC phase artifacts, without vocal hollowness, and without comb filtering**:

#### Signal Flow (`Studio Deep`, default `Stage Depth = 0.85`, `Spatial XTC = 0.0`)
1. **Band-Limited Mid/Side Spatial Send (`300 Hz – 6.5 kHz`):**
   - Form Side-weighted stereo sends: $F_L = \text{BPF}(\alpha_M M + \alpha_S S)$, $F_R = \text{BPF}(\alpha_M M - \alpha_S S)$ with $\alpha_M = 0.60$, $\alpha_S = 1.20$ (`+6.0 dB` Side weighting so stereo accompaniment blooms while centered vocals retain delicate studio air).
   - Band-limiting via 2nd-order Butterworth HPF (`300 Hz`, keeps bass & vocal chest 100% dry and tight) cascaded with 2nd-order Butterworth LPF (`6.5 kHz`, emulates natural studio wall absorption and air attenuation; eliminates sibilant splash).
2. **2-Stage Cascaded Coprime Schroeder All-Pass Micro-Diffuser (`0.7 – 2.9 ms`):**
   - Cross-coupled input (`D_L` fed from $F_R$; `D_R` fed from $F_L$).
   - Each Schroeder stage implements $y[n] = -g x[n] + x[n - m] + g y[n - m]$ with exact unity magnitude $|H_{\text{AP}}(e^{j\omega})| \equiv 1$:
     * **Left channel:** Stage 1: $m_{L1} = 43$ samples (`0.896 ms`), $g_{L1} = +0.52$; Stage 2: $m_{L2} = 107$ samples (`2.229 ms`), $g_{L2} = -0.48$.
     * **Right channel:** Stage 1: $m_{R1} = 59$ samples (`1.229 ms`), $g_{R1} = -0.52$; Stage 2: $m_{R2} = 131$ samples (`2.729 ms`), $g_{R2} = +0.48$.
   - Verified magnitude flatness: **`±5.79e-15 dB`** (`<< ±0.001 dB`).
3. **4-Tap Asymmetric Prime-Spaced Comb-Free Early Reflection Bank (`4.4 – 13.7 ms`, Haas Fusion Zone):**
   - Polarity-alternated exponentially weighted prime taps (RMS-normalized to unity):
     * **Left (`D_L`):** `211` (`4.396 ms`, `+0.58`), `331` (`6.896 ms`, `-0.46`), `487` (`10.146 ms`, `+0.36`), `641` (`13.354 ms`, `-0.28`).
     * **Right (`D_R`):** `229` (`4.771 ms`, `-0.58`), `359` (`7.479 ms`, `+0.46`), `521` (`10.854 ms`, `-0.36`), `659` (`13.729 ms`, `+0.28`).
4. **Unit-Norm Comb-Cancelling Lateral Scattering Output Matrix:**
   $$W_L = g_{\text{wet}}\big(\alpha_{\text{same}} D_L + \alpha_{\text{cross}} D_R\big), \qquad W_R = g_{\text{wet}}\big(\alpha_{\text{cross}} D_L + \alpha_{\text{same}} D_R\big)$$
   with $\alpha_{\text{same}} = +0.848528$, $\alpha_{\text{cross}} = -0.529150$ ($\alpha_{\text{same}}^2 + \alpha_{\text{cross}}^2 = 1.000000$). Because $\alpha_{\text{same}} + \alpha_{\text{cross}} = +0.319378$ (`-9.91 dB`), mono-sum comb artifacts $W_L + W_R$ are suppressed by **`-9.91 dB`** while preserving distinct asymmetric L/R wall arrival times!
5. **Wet Gain Scaling & Subtle High-Band Side Air Polish:**
   - $g_{\text{wet}} = \operatorname{clamp}(\text{Stage Depth}, 0.0, 1.5) \times 0.1975$:
     * `Stage Depth = 0.85` (`Studio Deep` default) $\to$ **`-15.50 dB` wet** (`0.1679`)
     * `Stage Depth = 0.65` (`Studio Spatial` default) $\to$ **`-17.83 dB` wet** (`0.1284`)
     * `Stage Depth = 1.00` $\to$ **`-14.09 dB` wet** (`0.1975`)
   - In `Studio Deep` (when `Spatial XTC < 0.05`), a gentle **`+0.8 dB` Side high-shelf at `600 Hz`** ($S = 0.707$) adds airy studio width above the vocal chest band with zero XTC phase sensitivity.

#### Verified Acoustic Performance (`verify_spatial_depth.py`)
- **Mono-Sum Comb Coloration (`200 Hz – 8 kHz` at `Stage Depth = 0.85`):**
  - Pure Mono Vocal input: **`±0.375 dB` raw** (`±0.046 dB` 1/3-octave smoothed) — strictly `< ±0.8 dB`!
  - Hard-Panned Left input: **`±0.839 dB` raw** (`±0.163 dB` 1/3-octave smoothed) — strictly `< ±1.0 dB`!
- **Binaural Acoustic Ear `IACC_E3` (`500–2000 Hz` through XPS 17 Transfer Plant):**
  - Pure Mono Vocal (`corr = 1.00`): Dry `1.0000` $\to$ **`Studio Deep = 0.9855`** (`> 0.92` rock-solid center vocal lock!).
  - Intimate Acoustic (`corr = 0.90`): Dry `0.9478` $\to$ **`Studio Deep = 0.9246`**.
  - Typical Studio Mix (`corr = 0.72`): Dry `0.8548` $\to$ **`Studio Deep = 0.8195`** (squarely inside `0.75–0.85` Griesinger ASW target!).
  - Wide Stereo Mix (`corr = 0.45`): Dry `0.7203` $\to$ **`Studio Deep = 0.6898`**.

---

### 4.3 Block C — Post-Dynamics Cooper–Bauck / Choueiri 1D M/S Shuffler XTC (`Spatial XTC`)

#### Algebraic Proof: Symmetric 2×2 FDTR-XTC $\equiv$ 1D Mid/Side Shuffler (Cooper & Bauck 1989; Choueiri 2010)
For any symmetric 2×2 acoustic plant $\mathbf{C}(\omega) = C_{ii}(\omega)\begin{bmatrix} 1 & A(\omega) \\ A(\omega) & 1 \end{bmatrix}$ and symmetric cancellation filter $\mathbf{H}(\omega) = \begin{bmatrix} H_{ll}(\omega) & H_{lr}(\omega) \\ H_{lr}(\omega) & H_{ll}(\omega) \end{bmatrix}$, conjugation by the unitary Mid/Side Hadamard matrix $\mathbf{U} = \frac{1}{\sqrt{2}}\begin{bmatrix} 1 & 1 \\ 1 & -1 \end{bmatrix}$ diagonalizes both matrices simultaneously:

$$\mathbf{U}\mathbf{C}(\omega)\mathbf{U}^{-1} = C_{ii}(\omega)\begin{bmatrix} \lambda_+(\omega) & 0 \\ 0 & \lambda_-(\omega) \end{bmatrix}, \qquad \mathbf{U}\mathbf{H}(\omega)\mathbf{U}^{-1} = \begin{bmatrix} H_M(\omega) & 0 \\ 0 & H_S(\omega) \end{bmatrix}$$

where $\lambda_{\pm}(\omega) = 1 \pm A(\omega)$, $H_M(\omega) = H_{ll}(\omega) + H_{lr}(\omega)$, and $H_S(\omega) = H_{ll}(\omega) - H_{lr}(\omega)$. Imposing the **exact Mono-Sum Anchor Constraint** $H_M(\omega) \equiv 1$ (zero Mid vocal/bass coloration) and minimizing Choueiri's frequency-dependent Tikhonov regularized objective:

$$J(H_S) = \big|\lambda_-(\omega) H_S(\omega) - \lambda_+(\omega)\big|^2 + \beta(\omega)\big|H_S(\omega) - 1\big|^2$$

yields the exact closed-form 1D Shuffler solution (verified to `2.22e-16` machine epsilon against the 2×2 matrix formulation):

$$H_S(\omega) = 1 + \frac{2\, W(\omega)\, A(\omega)\, (1 - A(\omega))^*}{|1 - A(\omega)|^2 + \beta(\omega)}, \qquad H_M(\omega) = 1$$

with band window $W(f) = \operatorname{ss}\!\left(\frac{f - 230}{140}\right)\!\left[1 - \operatorname{ss}\!\left(\frac{f - 5300}{1200}\right)\right]$ (active `250 Hz – 6.5 kHz`) and regularization schedule:

$$\beta(f) = 0.58 + 0.56\!\left[1 - \operatorname{ss}\!\left(\frac{f - 230}{610}\right)\right] + 1.50\,\operatorname{ss}\!\left(\frac{f - 2350}{950}\right)$$

#### Ultra-Low-Latency (`0.917 ms`), AVX2-Ready 256-Tap Realization
Because the physical crosstalk plant $A(\omega) = g(\omega)e^{-j\omega\tau}$ is causal ($\tau = 151.81\ \mu\text{s} \approx 7.3$ samples @ 48 kHz), the causal post-cursor of $h_S[n]$ carries $3.2\times$ more energy than its anti-causal precursor. Centering the impulse peak at **`D = 44` samples (`0.9167 ms < 1.0 ms`!)** inside a power-of-two **`N = 256`-tap float32 FIR** with an asymmetric Kaiser window ($\beta_K = 6.5$) gives:
- **Mid Path:** Pure `44`-sample integer delay ring buffer (`0` MACs/sample, **`0.000000 dB`** vocal/bass coloration!).
- **Side Path:** Single 1D 256-tap float32 FIR (**`32` AVX2 `_mm256_fmadd_ps` ops/sample** — **8× lower CPU** than old `dsp.rs`'s `2048 MACs/sample` and **5.8× lower latency** than `5.333 ms`!).
- **Dry/Wet Control (`Spatial XTC` $\in [0, 1]$):** $S_{\text{out}}[n] = (1 - \alpha_{\text{xtc}}) S[n - 44] + \alpha_{\text{xtc}} (h_S * S)[n]$.

#### Verified Float32 Shuffler Metrics (`verify_spatial_depth.py`)
- **Max Side Boost $|H_S(f)|$:** **`+2.278 dB`** at `632.8 Hz` (strictly `<= +2.8 dB`; combined with 180 Hz LR4 Bass Anchor: `+2.225 dB`).
- **Midband Crosstalk Cancellation (`300–2600 Hz`):** **`12.55 dB`** on-axis, **`11.14 dB`** at `3 cm` lateral head shift, **`8.74 dB`** at `5 cm` lateral shift.
- **Singularity Band (`2600–5500 Hz`, spanning $f_{M,1} = 3.29\text{ kHz}$):** **`11.22 dB`** smooth cancellation (zero vocal comb notch).
- **Sub-150 Hz Cross-Feed Isolation:** $\max_{f \le 150\text{ Hz}} |H_{lr}(f)| = \mathbf{-27.36\text{ dB}}$ (`< -24 dB`, zero bass cancellation).
- **Tweeter Air Rolloff (`>= 6.8 kHz`):** $|H_S(f)|$ unity within **`±0.0229 dB`** ($|H_{lr}| \le -53.9\text{ dB}$).
- **Pink-Noise Energy Conservation (`20 Hz – 20 kHz`):** Side delta `+0.219 dB`, Total `(L+R)` energy delta **`+0.111 dB`** (well inside `±0.5 dB`).
- **Diffuse Stereo Opening (`corr = 0.00`):** Acoustic ear `IACC_E3` drops from `0.5908` (dry speakers) $\to$ **`0.5565`** at `Spatial XTC = 0.75`.

---

### 4.4 Block D — Unified 7-Stage Pipeline Ordering & 4-Mode Control Contract

Unifying all four Lead-2 acoustics researchers' stages cleanly behind Lead 3's exact 10 LADSPA control ports (`libxps17_studio_dsp.so`, `UniqueID = 597201`):

```
Stereo Input (In L, In R) @ 48 kHz
  │
  ├─► [Stage 1] Klippel Excursion Observer + Sliding HPF (65–180 Hz, resting 82 Hz; tracks Volume dB & Excursion Guard)
  │
  ├─► [Stage 2] Hybrid 3-Band Psychoacoustic Bass + Warmth Body + Live ISO 226:2023 Dynamic Loudness Shelves
  │
  ├─► [Stage 3] Strict Bass Mono-Anchoring (3-Biquad Phase-Matched Bilinear LR4 24 dB/oct @ 180 Hz [150 Hz Ref])
  │             └─ Coherent +5.6..+5.9 dB woofer coupling; 0.0000° M/S phase error; unified bass energy into dynamics
  │
  ├─► [Stage 4] Minimum-Phase Room / Harman-Target Speaker Correction FIR (4096-tap @ 48 kHz per mode)
  │             └─ Removes raw +4.2 dB @ 1.5 kHz speaker shout BEFORE multiband dynamics react to spectrum
  │
  ├─► [Stage 5] Phase-Aligned 4-Band LR4 Multiband Dynamics (160 Hz / 750 Hz / 3500 Hz, exact AP unity sum)
  │             ├─ Primary loudness drive (`Drive (dB)`, default +7.0 dB) built independently per band
  │             ├─ Band 0 (<160 Hz) & Band 1 (160–750 Hz) completely isolated from Band 2 (750–3500 Hz) stereo guitars
  │             └─ Pre-Spatial Mastering Headroom Bus (-2.2 dBFS ceiling -> reserves clean post-dynamics spatial margin)
  │
  ├─► [Stage 6] POST-DYNAMICS SPATIAL & DEPTH STAGE (Zero pre-limiter pumping!)
  │             ├─ 6A: Studio Depth Engine (`Stage Depth`: 300–6.5k BPF -> 2x Schroeder AP -> 4.4–13.7 ms comb-free ER)
  │             └─ 6B: Cooper-Bauck / Choueiri 1D M/S Shuffler XTC (`Spatial XTC`: 256-tap float32, 0.917 ms, <= +2.28 dB)
  │
  └─► [Stage 7] Final 4× Oversampled True-Peak Safety Guard (+0.8 dB trim -> -1.0 dBTP ceiling, 10 ms lk, 80 ms rel)
                └─► Hardware Dual Realtek ALC1316 (with restored 0xC202=0xC0 2-way woofer/tweeter crossover)
```

#### Per-Mode Spatial, Dynamics & Limiter Matrix

| Mode ID | Mode Name | Bass Mono-Anchor | `Stage Depth` Default | Wet ER Level | Side Air (`>600 Hz`) | `Spatial XTC` Default | `Drive` / `Ceiling` | Pre-Spatial Bus | Spatial + Limiter Latency |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **`0`** | **`Studio Deep`** *(Flagship)* | `180 Hz` LR4 | **`0.85`** | `-15.50 dB` | **`+0.8 dB`** | **`0.00`** *(OFF)* | `7.0 dB` / `-1.0 dBTP` | `-2.2 dBFS` | `10.000 ms` |
| **`1`** | **`Studio Spatial`** *(Wide/Cinema)* | `180 Hz` LR4 | **`0.65`** | `-17.83 dB` | `0.0 dB` | **`0.75`** *(Shuffler)* | `7.0 dB` / `-1.0 dBTP` | `-2.2 dBFS` | `10.917 ms` |
| **`2`** | **`Studio Reference`** *(Neutral)* | `150 Hz` LR4 | **`0.20`** | `-28.07 dB` | `0.0 dB` | **`0.00`** *(OFF)* | `5.0 dB` / `-1.0 dBTP` | `-2.0 dBFS` | `10.000 ms` |
| **`3`** | **`Studio Voice`** *(Podcast/Calls)* | `180 Hz` LR4 | **`0.00`** *(OFF)* | `−∞ dB` | `0.0 dB` | **`0.00`** *(OFF)* | `6.0 dB` / `-1.0 dBTP` | `-2.0 dBFS` | `10.000 ms` |

---

## 5. Offline Reproducibility & Verification Summary

Run the complete pure-`numpy` verification suite on Efficiency Cores (`CPUs 12–19`):

```bash
taskset -c 12-19 nice -n 19 python3 \
  /home/akshaybapat/project/xps17-studio-audio/acoustics-research/depth-soundstage/verify_spatial_depth.py \
  --write-spec /home/akshaybapat/project/xps17-studio-audio/acoustics-research/depth-soundstage/spatial_spec.json
```

All 24 quantitative assertions pass with zero errors:
- XPS 17 geometry (`2θ = 34.352°`, geometric `Δr = 5.114 cm` / `149.10 µs`, Woodworth `151.81 µs`, Kuhn LF `226.00 µs`, $f_{M,1} = 3293.7\text{ Hz}$, $f_{S,1} = 6587.3\text{ Hz}$, $f_{\text{opt}} = 1646.8\text{ Hz}$) and mutual woofer coupling (`+5.93 dB` @ `82 Hz`, `+5.57 dB` @ `180 Hz` vs single woofer).
- Exact 3-biquad bilinear LR4 `180 Hz` Bass Mono-Anchor (`9.24e-13` identity error, `±9.70e-12 dB` Mid flatness, `0.0000°` M/S phase error, `-6.02 dB` @ `180 Hz`, `-24.61 dB` @ `90 Hz`).
- Float32 256-tap (`0.9167 ms`) Cooper–Bauck / Choueiri M/S Shuffler (`2.22e-16` 2×2 equivalence, `+2.278 dB <= +2.8 dB` max Side boost, `12.55 dB` midband XTC, `-27.36 dB < -24 dB` cross-feed `<= 150 Hz`, `±0.0229 dB` unity `>= 6.8 kHz`, `+0.111 dB` pink-noise total energy delta).
- Studio Depth Early-Reflection Network (`±5.79e-15 dB` all-pass flatness, `±0.375 dB` mono / `±0.839 dB` hard-left raw mono-sum comb ripple, `0.9855 > 0.92` mono vocal `IACC_E3`, `0.8195` studio mix `IACC_E3`).
- Forensic Pre-Limiter vs Post-Dynamics Ducking: Old pre-limiter FDTR-XTC inflicts `-1.21 dB` extra Mid vocal/bass ducking (and `91-spatial-fdtr.conf` inflicts `-6.69 dB` chorus vocal swing + `-18.51 dB` bass loss), reduced to **`+0.03 dB`** (`Studio Deep`) and **`-0.04 / -0.07 dB`** (`Studio Spatial`), strictly `< 0.30 dB` with peak `<= -1.00 dBTP`.
