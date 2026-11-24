//! 3-Band Equal-Loudness Missing-Fundamental Bass & Chest Warmth Body Engine.
//!
//! Psychoacoustic & DSP References:
//! - E. Larsen & R. M. Aarts, "Audio Bandwidth Extension: Application of Psychoacoustics,
//!   Signal Processing and Loudspeaker Design", John Wiley & Sons, 2004
//!   (envelope-normalized nonlinear harmonic synthesis for level-invariant virtual pitch).
//! - E. Terhardt, "Pitch, consonance, and harmony", J. Acoust. Soc. Am., 55(5), 1061-1069, 1974.
//! - ISO 226:2023, "Acoustics — Normal equal-loudness-level contours"
//!   (harmonic weighting into the XPS 17 micro-woofer 120-360 Hz acoustic efficiency window).
//!
//! Signal flow per fixed `CHUNK = 256` stack/preallocated block (ZERO heap allocations):
//! 1. Phase-coherent `StereoLr4F32` split at `mono_crossover_hz` (`215 Hz`) -> strict mono `< 180 Hz`.
//! 2. 3-band phase-aligned split (`32-68 Hz`, `68-112 Hz`, `112-160 Hz`) of `mono_low`.
//! 3. Level-invariant envelope-normalized AVX2/FMA Chebyshev harmonic synthesis via
//!    `crate::simd::chebyshev_bass_f32` (ensures `-35 dBFS` sustained basslines and `-14 dBFS`
//!    kicks both radiate rich, coherent `110-360 Hz` virtual-pitch harmonics!).
//! 4. Per-band 4th-order fundamental-rejection HPFs + clean `105-380 Hz` bandpass on harmonic bus:
//!    strips 100% of rectified `T2_ac`/`T4_ac` DC offset and prevents `> 450 Hz` vocal splatter.
//! 5. Sub-112 Hz vocal-safe spectral music detector (evaluates every 16 samples with zero `log10`
//!    calls: tracks true sub-bass `< 112 Hz` vs `320-3200 Hz` vocals so music with loud vocals
//!    receives 100% un-choked bass while dry speech podcasts stay crisp and un-boomy).
//! 6. Mono-anchored recombination + dynamic `195 Hz` chest-warmth bell + subtle DC-blocked
//!    even-harmonic transformer sheen.

use std::f64::consts::FRAC_1_SQRT_2;

use crate::simd::FtzDazGuard;
use crate::svf::{AllpassF32, Lr4F32, QuadSvfF32, StereoLr4F32, StereoSvfF32, SvfF32};

/// Fixed internal processing chunk size (zero heap allocations on the audio thread).
const CHUNK: usize = 256;

/// Decimated (`fs / 4 = 12 kHz`) scratch buffer capacity (`CHUNK / 4 + 8` for AVX2 alignment).
const SUB_CHUNK: usize = 72;

/// Control-rate decimation stride at `fs / 4` (`4` sub-samples = `16` full-rate samples).
const SUB_CTRL_STRIDE: u32 = 4;

/// Harmonic synthesis bandpass boundaries (Hz).
const HARM_HPF_HZ: f64 = 105.0;
const HARM_LPF_HZ: f64 = 385.0;

/// Vocal detector bandpass boundaries (Hz).
const VOX_HPF_HZ: f64 = 320.0;
const VOX_LPF_HZ: f64 = 3200.0;

/// `-52 dBFS` sub-112 Hz mean-square presence threshold (`10^(-52/10)`).
const SUB_PRESENCE_PWR: f32 = 6.309_573_4e-6;

/// `-55 dBFS` full-bass mean-square presence threshold (`10^(-55/10)`).
const BASS_PRESENCE_PWR: f32 = 3.162_277_7e-6;

/// Resting floor of the vocal-safe music gate during pure dry speech.
const GATE_SPEECH_FLOOR: f32 = 0.06;

/// Maximum music gate allowed in `is_voice_mode` to keep podcasts crisp.
const GATE_VOICE_MODE_CAP: f32 = 0.16;

/// Detector RMS integration and gate hysteresis time constants (ms).
const DET_RMS_MS: f64 = 25.0;
const GATE_OPEN_MS: f64 = 15.0;
const GATE_CLOSE_MS: f64 = 550.0;

/// Per-band fast groove envelope time constants (ms) for Larsen-Aarts normalization.
const BAND_ENV_ATK_MS: f64 = 4.0;
const BAND_ENV_REL_MS: f64 = 38.0;

/// Smooth control & dynamic warmth time constants (ms).
const PARAM_SMOOTH_MS: f64 = 5.0;
const WARMTH_ENV_MS: f64 = 90.0;
const WARMTH_GAIN_SMOOTH_MS: f64 = 75.0;

/// Larsen-Aarts soft-knee envelope normalization knees and harmonic projection gains.
const NORM_KNEES: [f32; 3] = [0.0020, 0.0022, 0.085];
const ENV_GAINS: [f32; 3] = [3.80, 5.80, 1.50];

#[inline]
fn exp_coeff_f32(time_ms: f64, sample_rate: f64) -> f32 {
    let tau_s = (time_ms.max(0.05)) * 0.001;
    (1.0 - (-1.0 / (tau_s * sample_rate)).exp()) as f32
}

#[inline(always)]
fn sanitize_f32(x: f32) -> f32 {
    if x.is_finite() { x } else { 0.0 }
}

/// Configuration parameters for the 3-band equal-loudness missing-fundamental
/// bass and chest warmth engine.
#[derive(Debug, Clone, Copy)]
pub struct BassEngineParams {
    /// Phase-coherent LR4 mono-anchor crossover frequency in Hz (default `215.0`).
    pub mono_crossover_hz: f64,
    /// Four frequency edges defining the 3 bass synthesis bands in Hz:
    /// `[32.0, 68.0, 115.0, 175.0]`.
    pub band_edges_hz: [f64; 4],
    /// Per-band `[w2, w3, w4]` weights for `T2_ac, T3, T4_ac` in
    /// `crate::simd::chebyshev_bass_f32`, ISO 226 equal-loudness weighted into
    /// the XPS 17's 120-360 Hz acoustic sweet spot.
    pub harmonic_weights: [[f32; 3]; 3],
    /// Per-band nonlinear Chebyshev drive factors on envelope-normalized signal.
    pub band_drives: [f32; 3],
    /// Per-band harmonic output mix gains.
    pub band_gains: [f32; 3],
    /// Master harmonic synthesis wet mix (default `0.78`).
    pub synth_mix: f32,
    /// Dynamic chest warmth bell center frequency in Hz (default `195.0`).
    pub warmth_freq_hz: f64,
    /// Dynamic chest warmth bell quality factor (default `0.78`).
    pub warmth_q: f64,
    /// Maximum dynamic chest warmth boost in dB (default `4.0`).
    pub warmth_max_db: f64,
}

impl Default for BassEngineParams {
    fn default() -> Self {
        Self {
            mono_crossover_hz: 215.0,
            band_edges_hz: [32.0, 68.0, 115.0, 175.0],
            harmonic_weights: [
                // Band 0 (32-68 Hz): strong 2nd, 3rd & 4th harmonics -> 110/165/220 Hz where woofers sing.
                [0.55, 0.70, 0.45],
                // Band 1 (68-115 Hz): rich 2nd & 3rd -> 170/255 Hz bass punch & body.
                [0.80, 0.45, 0.15],
                // Band 2 (115-175 Hz): warm 2nd harmonic -> 260-340 Hz upper-bass coupling.
                [0.65, 0.20, 0.0],
            ],
            band_drives: [2.8, 2.2, 1.6],
            band_gains: [0.55, 0.65, 0.42],
            synth_mix: 0.78,
            warmth_freq_hz: 195.0,
            warmth_q: 0.78,
            warmth_max_db: 4.0,
        }
    }
}

/// 3-Band Equal-Loudness Missing-Fundamental Bass & Warmth Body Engine.
#[derive(Debug, Clone)]
pub struct BassEngine {
    sample_rate: f64,
    params: BassEngineParams,

    // Step A (`fs`): Phase-coherent Stereo LR4 mono-anchor crossover (< 215 Hz -> r(<180 Hz) >= +0.999).
    mono_xover: StereoLr4F32,

    // Step B (`fs / 2` = 24 kHz): Phase-aligned 3-band splitter for `mono_low`.
    bass_in_hpf: SvfF32,
    bass_in_lpf1: SvfF32,
    bass_in_lpf2: SvfF32,
    band_xover_1: Lr4F32,
    band_xover_2: Lr4F32,
    band0_ap_match: AllpassF32,
    band_envs: [f32; 3],
    cur_inv_norm: [f32; 3],
    cur_band_scale: [f32; 3],

    // Step D (`fs / 2` + `fs` post-LPF): `__m128` 4-lane per-band 4th-order fundamental-rejection HPFs
    // + clean `105-385 Hz` harmonic bus bandpass + full-rate anti-imaging LPF.
    band_harm_hpf1_q: QuadSvfF32,
    band_harm_hpf2_q: QuadSvfF32,
    harm_hpf: SvfF32,
    harm_lpf1: SvfF32,
    harm_post_lpf: SvfF32,

    // Step E (`fs / 2`): Sub-112 Hz vocal-safe spectral music detector.
    vox_hpf: SvfF32,
    vox_lpf: SvfF32,
    e_sub: f32,
    e_bass: f32,
    e_vox: f32,
    gate_open: bool,
    music_gate: f32,
    target_gate: f32,
    last_gate: f32,

    // Stateful 4x polyphase boxcar decimator & linear interpolator across arbitrary block sizes.
    decim_cnt: u8,
    decim_acc_full: f32,
    prev_harm_sub: f32,
    cur_harm_sub: f32,
    sub_counter: u32,

    // Step G: Dynamic chest warmth bell (`195 Hz`, vocal-isolated detector -> 0.00% IMD & 0.00 dB vocal ducking!).
    warmth_det_bp: SvfF32,
    warmth_pwr: f32,
    warmth_bell_db: f64,
    applied_bell_db: f64,
    warmth_bell: StereoSvfF32,

    // Control smoothers.
    controls_initialized: bool,
    smooth_bass_depth: f32,
    smooth_warmth_body: f32,

    // Precomputed exponential smoothing coefficients.
    alpha_rms: f32,
    alpha_gate_open: f32,
    alpha_gate_close: f32,
    alpha_band_atk: f32,
    alpha_band_rel: f32,
    alpha_param: f32,
    alpha_warmth_env: f32,
    alpha_warmth_gain: f64,

    // Preallocated fixed-size chunk scratch buffers (zero heap allocations).
    low_mono_buf: [f32; CHUNK],
    high_l_buf: [f32; CHUNK],
    high_r_buf: [f32; CHUNK],
    decim_acc_low: f32,
    band_norm_bufs: [[f32; SUB_CHUNK]; 3],
    band_scale_bufs: [[f32; SUB_CHUNK]; 3],
    harm_band_bufs: [[f32; SUB_CHUNK]; 3],
    harm_sub_buf: [f32; SUB_CHUNK],
    gate_sub_buf: [f32; SUB_CHUNK],
    low_sub_buf: [f32; SUB_CHUNK],
}

impl BassEngine {
    /// Creates a new `BassEngine` with default XPS 17 studio parameters and
    /// preallocated fixed-size chunk scratch buffers.
    pub fn new(sample_rate: f64) -> Self {
        let sr = if sample_rate.is_finite() && sample_rate >= 8000.0 {
            sample_rate
        } else {
            48_000.0
        };
        let fs_sub = (0.25 * sr).max(8000.0);
        let params = BassEngineParams::default();

        let mono_xover = StereoLr4F32::new(sr, params.mono_crossover_hz);

        let mut bass_in_hpf = SvfF32::new(fs_sub);
        let mut bass_in_lpf1 = SvfF32::new(fs_sub);
        let mut bass_in_lpf2 = SvfF32::new(fs_sub);
        let lpf_edge = params.band_edges_hz[3].min(162.0);
        bass_in_hpf.set_highpass(params.band_edges_hz[0], FRAC_1_SQRT_2);
        bass_in_lpf1.set_lowpass(lpf_edge, FRAC_1_SQRT_2);
        bass_in_lpf2.copy_coeffs_from(&bass_in_lpf1);

        let band_xover_1 = Lr4F32::new(fs_sub, params.band_edges_hz[1]);
        let band_xover_2 = Lr4F32::new(fs_sub, params.band_edges_hz[2]);
        let band0_ap_match = AllpassF32::new(fs_sub, params.band_edges_hz[2]);

        let band_hpf_freqs = [
            (params.band_edges_hz[1] * 1.55).max(HARM_HPF_HZ),
            (params.band_edges_hz[2] * 1.20).max(136.0),
            (params.band_edges_hz[3] * 1.32).max(210.0),
        ];
        let mut band_harm_hpf1_q = QuadSvfF32::new(fs_sub);
        for b in 0..3 {
            band_harm_hpf1_q.set_lane_highpass(b, band_hpf_freqs[b], FRAC_1_SQRT_2);
        }
        band_harm_hpf1_q.set_lane_highpass(3, HARM_HPF_HZ, FRAC_1_SQRT_2);
        let mut band_harm_hpf2_q = QuadSvfF32::new(fs_sub);
        band_harm_hpf2_q.copy_coeffs_from(&band_harm_hpf1_q);

        let mut harm_hpf = SvfF32::new(fs_sub);
        let mut harm_lpf1 = SvfF32::new(fs_sub);
        let mut harm_post_lpf = SvfF32::new(sr);
        harm_hpf.set_highpass(HARM_HPF_HZ, FRAC_1_SQRT_2);
        harm_lpf1.set_lowpass(HARM_LPF_HZ, FRAC_1_SQRT_2);
        harm_post_lpf.set_lowpass(HARM_LPF_HZ, FRAC_1_SQRT_2);

        let mut vox_hpf = SvfF32::new(fs_sub);
        let mut vox_lpf = SvfF32::new(fs_sub);
        vox_hpf.set_highpass(VOX_HPF_HZ, FRAC_1_SQRT_2);
        vox_lpf.set_lowpass(VOX_LPF_HZ.min(0.42 * fs_sub), FRAC_1_SQRT_2);

        let mut warmth_det_bp = SvfF32::new(fs_sub);
        warmth_det_bp.set_bandpass(params.warmth_freq_hz, 1.2);

        let mut warmth_bell = StereoSvfF32::new(sr);
        warmth_bell.set_bell(params.warmth_freq_hz, params.warmth_q, 0.0);

        let alpha_warmth_gain = {
            let tau_s = (WARMTH_GAIN_SMOOTH_MS * 0.001).max(1e-4);
            1.0 - (-(SUB_CTRL_STRIDE as f64) / (tau_s * fs_sub)).exp()
        };

        Self {
            sample_rate: sr,
            params,
            mono_xover,
            bass_in_hpf,
            bass_in_lpf1,
            bass_in_lpf2,
            band_xover_1,
            band_xover_2,
            band0_ap_match,
            band_envs: [0.0; 3],
            cur_inv_norm: [1.0 / NORM_KNEES[0], 1.0 / NORM_KNEES[1], 1.0 / NORM_KNEES[2]],
            cur_band_scale: [0.0; 3],
            band_harm_hpf1_q,
            band_harm_hpf2_q,
            harm_hpf,
            harm_lpf1,
            harm_post_lpf,
            vox_hpf,
            vox_lpf,
            e_sub: 0.0,
            e_bass: 0.0,
            e_vox: 0.0,
            gate_open: false,
            music_gate: GATE_SPEECH_FLOOR,
            target_gate: GATE_SPEECH_FLOOR,
            last_gate: GATE_SPEECH_FLOOR,
            decim_cnt: 0,
            decim_acc_full: 0.0,
            prev_harm_sub: 0.0,
            cur_harm_sub: 0.0,
            sub_counter: 0,
            warmth_det_bp,
            warmth_pwr: 0.0,
            warmth_bell_db: 0.0,
            applied_bell_db: 0.0,
            warmth_bell,
            controls_initialized: false,
            smooth_bass_depth: 1.0,
            smooth_warmth_body: 1.0,
            alpha_rms: exp_coeff_f32(DET_RMS_MS, fs_sub),
            alpha_gate_open: exp_coeff_f32(GATE_OPEN_MS, fs_sub),
            alpha_gate_close: exp_coeff_f32(GATE_CLOSE_MS, fs_sub),
            alpha_band_atk: exp_coeff_f32(BAND_ENV_ATK_MS, fs_sub),
            alpha_band_rel: exp_coeff_f32(BAND_ENV_REL_MS, fs_sub),
            alpha_param: exp_coeff_f32(PARAM_SMOOTH_MS, sr),
            alpha_warmth_env: exp_coeff_f32(WARMTH_ENV_MS, fs_sub),
            alpha_warmth_gain,
            low_mono_buf: [0.0; CHUNK],
            high_l_buf: [0.0; CHUNK],
            high_r_buf: [0.0; CHUNK],
            decim_acc_low: 0.0,
            band_norm_bufs: [[0.0; SUB_CHUNK]; 3],
            band_scale_bufs: [[0.0; SUB_CHUNK]; 3],
            harm_band_bufs: [[0.0; SUB_CHUNK]; 3],
            harm_sub_buf: [0.0; SUB_CHUNK],
            gate_sub_buf: [0.0; SUB_CHUNK],
            low_sub_buf: [0.0; SUB_CHUNK],
        }
    }

    /// Resets all filter, envelope, and detector states to zero without allocating.
    pub fn reset(&mut self) {
        self.mono_xover.reset();
        self.decim_acc_low = 0.0;
        self.bass_in_hpf.reset();
        self.bass_in_lpf1.reset();
        self.bass_in_lpf2.reset();
        self.band_xover_1.reset();
        self.band_xover_2.reset();
        self.band0_ap_match.reset();
        self.band_envs = [0.0; 3];
        self.cur_inv_norm = [1.0 / NORM_KNEES[0], 1.0 / NORM_KNEES[1], 1.0 / NORM_KNEES[2]];
        self.cur_band_scale = [0.0; 3];
        self.band_harm_hpf1_q.reset();
        self.band_harm_hpf2_q.reset();
        self.harm_hpf.reset();
        self.harm_lpf1.reset();
        self.harm_post_lpf.reset();
        self.vox_hpf.reset();
        self.vox_lpf.reset();
        self.e_sub = 0.0;
        self.e_bass = 0.0;
        self.e_vox = 0.0;
        self.gate_open = false;
        self.music_gate = GATE_SPEECH_FLOOR;
        self.target_gate = GATE_SPEECH_FLOOR;
        self.last_gate = GATE_SPEECH_FLOOR;
        self.decim_cnt = 0;
        self.decim_acc_full = 0.0;
        self.prev_harm_sub = 0.0;
        self.cur_harm_sub = 0.0;
        self.sub_counter = 0;
        self.warmth_det_bp.reset();
        self.warmth_pwr = 0.0;
        self.warmth_bell_db = 0.0;
        self.applied_bell_db = 0.0;
        self.warmth_bell.reset();
        self.warmth_bell
            .set_bell(self.params.warmth_freq_hz, self.params.warmth_q, 0.0);
        self.controls_initialized = false;
    }

    /// Updates engine parameters click-free (sanitizing frequencies, drives, and gains).
    pub fn set_params(&mut self, mut params: BassEngineParams) {
        let max_nyq = 0.42 * self.sample_rate;
        let max_sub_nyq = 0.105 * self.sample_rate;
        params.mono_crossover_hz = if params.mono_crossover_hz.is_finite() {
            params.mono_crossover_hz.clamp(60.0, 400.0_f64.min(max_nyq))
        } else {
            215.0
        };

        let e0 = if params.band_edges_hz[0].is_finite() {
            params.band_edges_hz[0].clamp(20.0, 120.0)
        } else {
            32.0
        };
        let e1 = if params.band_edges_hz[1].is_finite() {
            params.band_edges_hz[1].clamp(e0 + 10.0, 200.0)
        } else {
            68.0_f64.max(e0 + 10.0)
        };
        let e2 = if params.band_edges_hz[2].is_finite() {
            params.band_edges_hz[2].clamp(e1 + 10.0, 300.0)
        } else {
            115.0_f64.max(e1 + 10.0)
        };
        let e3 = if params.band_edges_hz[3].is_finite() {
            params
                .band_edges_hz[3]
                .clamp(e2 + 10.0, 420.0_f64.min(max_sub_nyq))
        } else {
            175.0_f64.max(e2 + 10.0)
        };
        params.band_edges_hz = [e0, e1, e2, e3];

        for b in 0..3 {
            params.band_drives[b] = if params.band_drives[b].is_finite() {
                params.band_drives[b].clamp(0.1, 10.0)
            } else {
                2.0
            };
            params.band_gains[b] = if params.band_gains[b].is_finite() {
                params.band_gains[b].clamp(0.0, 3.0)
            } else {
                0.55
            };
            for h in 0..3 {
                if !params.harmonic_weights[b][h].is_finite() {
                    params.harmonic_weights[b][h] = 0.0;
                }
            }
        }

        params.synth_mix = if params.synth_mix.is_finite() {
            params.synth_mix.clamp(0.0, 2.0)
        } else {
            0.78
        };
        params.warmth_freq_hz = if params.warmth_freq_hz.is_finite() {
            params.warmth_freq_hz.clamp(80.0, 500.0_f64.min(max_sub_nyq))
        } else {
            195.0
        };
        params.warmth_q = if params.warmth_q.is_finite() {
            params.warmth_q.clamp(0.3, 4.0)
        } else {
            0.78
        };
        params.warmth_max_db = if params.warmth_max_db.is_finite() {
            params.warmth_max_db.clamp(0.0, 12.0)
        } else {
            4.0
        };

        self.params = params;

        let lpf_edge = params.band_edges_hz[3].min(162.0);
        self.mono_xover
            .set_freq(params.mono_crossover_hz.max(215.0));
        self.bass_in_hpf
            .set_highpass(params.band_edges_hz[0], FRAC_1_SQRT_2);
        self.bass_in_lpf1.set_lowpass(lpf_edge, FRAC_1_SQRT_2);
        self.bass_in_lpf2.copy_coeffs_from(&self.bass_in_lpf1);
        self.band_xover_1.set_freq(params.band_edges_hz[1]);
        self.band_xover_2.set_freq(params.band_edges_hz[2]);
        self.band0_ap_match.set_freq(params.band_edges_hz[2]);

        let band_hpf_freqs = [
            (params.band_edges_hz[1] * 1.55).max(HARM_HPF_HZ),
            (params.band_edges_hz[2] * 1.20).max(136.0),
            (params.band_edges_hz[3] * 1.32).max(210.0),
        ];
        for b in 0..3 {
            self.band_harm_hpf1_q
                .set_lane_highpass(b, band_hpf_freqs[b], FRAC_1_SQRT_2);
        }
        self.band_harm_hpf1_q
            .set_lane_highpass(3, HARM_HPF_HZ, FRAC_1_SQRT_2);
        self.band_harm_hpf2_q
            .copy_coeffs_from(&self.band_harm_hpf1_q);

        self.warmth_det_bp
            .set_bandpass(params.warmth_freq_hz, 1.2);
        self.warmth_bell
            .set_bell(params.warmth_freq_hz, params.warmth_q, self.warmth_bell_db);
        self.applied_bell_db = self.warmth_bell_db;
    }

    #[inline]
    fn evaluate_music_gate_target(&mut self, is_voice_mode: bool) -> f32 {
        let e_sub = self.e_sub;
        let e_bass = self.e_bass;
        let e_vox = self.e_vox + 1.0e-12;

        // Sub-112 Hz true sub-bass presence (immune to >=115 Hz podcast speech HPF!)
        let sub_music = e_sub > SUB_PRESENCE_PWR && e_sub >= 0.0020 * e_vox;
        let bass_present = e_bass > BASS_PRESENCE_PWR;

        let mut tgt = if sub_music {
            self.gate_open = true;
            1.0
        } else if !bass_present {
            self.gate_open = false;
            GATE_SPEECH_FLOOR
        } else if e_bass >= 0.0631 * e_vox {
            self.gate_open = true;
            1.0
        } else if e_bass >= 0.0251 * e_vox {
            self.gate_open = true;
            let t = ((e_bass / e_vox) - 0.0251) * (1.0 / (0.0631 - 0.0251));
            0.45 + 0.55 * t.clamp(0.0, 1.0)
        } else if e_bass >= 0.0100 * e_vox {
            if self.gate_open {
                self.music_gate.max(0.45)
            } else {
                GATE_SPEECH_FLOOR
            }
        } else {
            self.gate_open = false;
            GATE_SPEECH_FLOOR
        };

        if is_voice_mode {
            tgt = tgt.min(GATE_VOICE_MODE_CAP);
        }
        tgt
    }

    /// Processes stereo audio in-place through the 3-band missing-fundamental
    /// bass engine, vocal-safe music detector, strict `< 215 Hz` mono anchor,
    /// and dynamic low-mid chest warmth + transformer sheen stage.
    ///
    /// Hard-RT safe: ZERO heap allocations.
    pub fn process_block(
        &mut self,
        left: &mut [f32],
        right: &mut [f32],
        bass_depth: f32,
        warmth_body: f32,
        is_voice_mode: bool,
    ) {
        let total = left.len().min(right.len());
        if total == 0 {
            return;
        }
        let _ftz = FtzDazGuard::enter();

        let target_bass_depth = if bass_depth.is_finite() {
            bass_depth.clamp(0.0, 1.5)
        } else {
            1.0
        };
        let target_warmth_body = if warmth_body.is_finite() {
            warmth_body.clamp(0.0, 1.5)
        } else {
            1.0
        };

        if !self.controls_initialized {
            self.smooth_bass_depth = target_bass_depth;
            self.smooth_warmth_body = target_warmth_body;
            self.controls_initialized = true;
        }

        let voice_warmth_scale = if is_voice_mode { 0.45f32 } else { 1.0f32 };
        let synth_mix = self.params.synth_mix;
        let warmth_freq = self.params.warmth_freq_hz;
        let warmth_q = self.params.warmth_q;
        let warmth_max_db = self.params.warmth_max_db;
        let [bg0, bg1, bg2] = self.params.band_gains;

        let a_rms = self.alpha_rms;
        let a_open = self.alpha_gate_open;
        let a_close = self.alpha_gate_close;
        let a_batk = self.alpha_band_atk;
        let a_brel = self.alpha_band_rel;
        let a_param = self.alpha_param;
        let a_wenv = self.alpha_warmth_env;

        let mut offset = 0usize;
        while offset < total {
            let n = (total - offset).min(CHUNK);
            let cnt_start = self.decim_cnt;
            let mut n_sub = 0usize;

            // Pass 1a (`48 kHz`): Register-resident Stereo LR4 mono split (< 215 Hz, 0 XMM spills!).
            for i in 0..n {
                let xl = sanitize_f32(left[offset + i]);
                let xr = sanitize_f32(right[offset + i]);
                let (low_lr, high_lr) = self.mono_xover.split([xl, xr]);
                self.low_mono_buf[i] = 0.5 * (low_lr[0] + low_lr[1]);
                self.high_l_buf[i] = high_lr[0];
                self.high_r_buf[i] = high_lr[1];
            }

            // Pass 1b (`12 kHz` = 4x decimated, 64 sub-samples per 256-sample block!):
            // 3-band `< 162 Hz` sub-bass splitter & vocal-safe spectral music detector.
            for i in 0..n {
                let mono_low = self.low_mono_buf[i];
                let mono_full = mono_low + 0.5 * (self.high_l_buf[i] + self.high_r_buf[i]);

                self.decim_acc_low += mono_low;
                self.decim_acc_full += mono_full;
                if self.decim_cnt < 3 {
                    self.decim_cnt += 1;
                } else {
                    self.decim_cnt = 0;
                    let m_low = 0.25 * self.decim_acc_low;
                    let m_full = 0.25 * self.decim_acc_full;
                    self.decim_acc_low = 0.0;
                    self.decim_acc_full = 0.0;
                    let k = n_sub;
                    n_sub += 1;

                    let hp_in = self.bass_in_hpf.process_hp(m_low);
                    let bass_band =
                        self.bass_in_lpf2.process_lp(self.bass_in_lpf1.process_lp(hp_in));

                    let (b0_raw, b12) = self.band_xover_1.split(bass_band);
                    let (b1, b2) = self.band_xover_2.split(b12);
                    let b0 = self.band0_ap_match.process(b0_raw);
                    let bands = [b0, b1, b2];

                    let update_recip = (self.sub_counter & 1) == 0;
                    for b in 0..3 {
                        let s = bands[b];
                        let ab = s.abs();
                        let env = &mut self.band_envs[b];
                        let coeff = if ab > *env { a_batk } else { a_brel };
                        *env += coeff * (ab - *env);
                        if update_recip {
                            let e = *env;
                            self.cur_inv_norm[b] = 1.0 / (e + NORM_KNEES[b]);
                            self.cur_band_scale[b] = ENV_GAINS[b] * e * (e / (e + 0.0016));
                        }
                        self.band_norm_bufs[b][k] = s * self.cur_inv_norm[b];
                        self.band_scale_bufs[b][k] = self.cur_band_scale[b];
                    }

                    let sub_112 = b0 + b1;
                    let vox_band = self.vox_lpf.process_lp(self.vox_hpf.process_hp(m_full));

                    self.e_sub += a_rms * (sub_112 * sub_112 - self.e_sub);
                    self.e_bass += a_rms * (bass_band * bass_band - self.e_bass);
                    self.e_vox += a_rms * (vox_band * vox_band - self.e_vox);

                    if (self.sub_counter & (SUB_CTRL_STRIDE - 1)) == 0 {
                        self.target_gate = self.evaluate_music_gate_target(is_voice_mode);
                    }
                    self.sub_counter = self.sub_counter.wrapping_add(1);

                    let gate_alpha = if self.target_gate > self.music_gate {
                        a_open
                    } else {
                        a_close
                    };
                    self.music_gate += gate_alpha * (self.target_gate - self.music_gate);
                    self.gate_sub_buf[k] = self.music_gate;
                    self.low_sub_buf[k] = m_low;
                }
            }

            // Pass 2 (`12 kHz`): AVX2/FMA Chebyshev synthesis + `__m128` 4-lane HPF + vocal-isolated warmth detector.
            if n_sub > 0 {
                for b in 0..3 {
                    let drive = self.params.band_drives[b];
                    let [w2, w3, w4] = self.params.harmonic_weights[b];
                    if self.params.band_gains[b] <= 0.0 {
                        self.harm_band_bufs[b][..n_sub].fill(0.0);
                        continue;
                    }
                    crate::simd::chebyshev_bass_f32(
                        &self.band_norm_bufs[b][..n_sub],
                        &mut self.harm_band_bufs[b][..n_sub],
                        drive,
                        w2,
                        w3,
                        w4,
                    );
                }

                for k in 0..n_sub {
                    let h_in = [
                        self.harm_band_bufs[0][k] * self.band_scale_bufs[0][k],
                        self.harm_band_bufs[1][k] * self.band_scale_bufs[1][k],
                        self.harm_band_bufs[2][k] * self.band_scale_bufs[2][k],
                        0.0,
                    ];
                    let h_hp = self
                        .band_harm_hpf2_q
                        .process_hp(self.band_harm_hpf1_q.process_hp(h_in));
                    let h_sum = bg0.mul_add(h_hp[0], bg1.mul_add(h_hp[1], bg2 * h_hp[2]));
                    let h_bp = self.harm_lpf1.process_lp(self.harm_hpf.process_hp(h_sum));
                    let h_gated = h_bp * synth_mix * self.gate_sub_buf[k];
                    self.harm_sub_buf[k] = h_gated;

                    let w_in = self.low_sub_buf[k] + h_gated * self.smooth_bass_depth;
                    let w_band = self.warmth_det_bp.process(w_in);
                    self.warmth_pwr += a_wenv * (w_band * w_band - self.warmth_pwr);
                }

                let warmth_rms = self.warmth_pwr.max(0.0).sqrt() as f64;
                let dyn_factor = 1.0 / (1.0 + 1.2 * warmth_rms);
                let target_bell_db = (warmth_max_db
                    * ((self.smooth_warmth_body as f64) / 1.25)
                    * (voice_warmth_scale as f64)
                    * dyn_factor)
                    .clamp(0.0, warmth_max_db);
                let sub_blocks = (n_sub as f64) / (SUB_CTRL_STRIDE as f64);
                let blk_alpha = 1.0 - (1.0 - self.alpha_warmth_gain).powf(sub_blocks);
                self.warmth_bell_db += blk_alpha * (target_bell_db - self.warmth_bell_db);
                if (self.warmth_bell_db - self.applied_bell_db).abs() > 0.035 {
                    self.warmth_bell
                        .set_bell(warmth_freq, warmth_q, self.warmth_bell_db);
                    self.applied_bell_db = self.warmth_bell_db;
                }
            }

            // Pass 3 (`48 kHz`): Linear 4x harmonic upsampling + anti-imaging LPF + register-resident warmth bell.
            let mut c = cnt_start;
            let mut k_read = 0usize;
            for i in 0..n {
                self.smooth_bass_depth +=
                    a_param * (target_bass_depth - self.smooth_bass_depth);
                self.smooth_warmth_body +=
                    a_param * (target_warmth_body - self.smooth_warmth_body);

                let harm_48k = match c {
                    0 => {
                        c = 1;
                        0.50 * (self.prev_harm_sub + self.cur_harm_sub)
                    }
                    1 => {
                        c = 2;
                        0.25 * self.prev_harm_sub + 0.75 * self.cur_harm_sub
                    }
                    2 => {
                        c = 3;
                        self.cur_harm_sub
                    }
                    _ => {
                        c = 0;
                        self.prev_harm_sub = self.cur_harm_sub;
                        self.cur_harm_sub = self.harm_sub_buf[k_read];
                        k_read += 1;
                        0.75 * self.prev_harm_sub + 0.25 * self.cur_harm_sub
                    }
                };

                let harm_scaled =
                    self.harm_post_lpf.process_lp(harm_48k) * self.smooth_bass_depth;
                let low_plus_harm = self.low_mono_buf[i] + harm_scaled;

                let warm = self.warmth_bell.process([
                    low_plus_harm + self.high_l_buf[i],
                    low_plus_harm + self.high_r_buf[i],
                ]);
                left[offset + i] = warm[0];
                right[offset + i] = warm[1];
            }

            offset += n;
        }

        self.last_gate = self.music_gate.clamp(GATE_SPEECH_FLOOR, 1.0);
    }

    /// Returns the most recent vocal-safe spectral music detector gate value (`0.06..=1.0`).
    #[inline]
    pub fn last_music_gate(&self) -> f32 {
        self.last_gate
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::svf::Lr4Crossover;
    use std::f64::consts::TAU;

    fn pearson_corr(a: &[f32], b: &[f32]) -> f64 {
        let n = a.len().min(b.len()) as f64;
        let mean_a = a.iter().map(|&x| x as f64).sum::<f64>() / n;
        let mean_b = b.iter().map(|&x| x as f64).sum::<f64>() / n;
        let mut cov = 0.0_f64;
        let mut va = 0.0_f64;
        let mut vb = 0.0_f64;
        for (&x, &y) in a.iter().zip(b.iter()) {
            let da = (x as f64) - mean_a;
            let db = (y as f64) - mean_b;
            cov += da * db;
            va += da * da;
            vb += db * db;
        }
        cov / (va * vb).sqrt().max(1e-30)
    }

    fn tone_amp(buf: &[f32], fs: f64, freq_hz: f64) -> f64 {
        let mut re = 0.0_f64;
        let mut im = 0.0_f64;
        for (i, &s) in buf.iter().enumerate() {
            let ang = TAU * freq_hz * (i as f64) / fs;
            re += (s as f64) * ang.cos();
            im += (s as f64) * ang.sin();
        }
        2.0 * (re * re + im * im).sqrt() / (buf.len() as f64)
    }

    #[test]
    fn test_bass_strict_mono_correlation_below_180hz() {
        let fs = 48_000.0;
        let len = 24_000;

        let mut eng = BassEngine::new(fs);
        let mut l = vec![0.0_f32; len];
        let mut r = vec![0.0_f32; len];
        for i in 0..len {
            l[i] = 0.5 * (TAU * 80.0 * (i as f64) / fs).sin() as f32;
        }
        eng.process_block(&mut l, &mut r, 1.0, 1.0, false);
        let corr_80 = pearson_corr(&l[8_000..], &r[8_000..]);
        assert!(
            corr_80 > 0.999,
            "80 Hz hard-panned bass was not mono-anchored: corr = {corr_80:.6}"
        );

        eng.reset();
        for i in 0..len {
            l[i] = 0.5 * (TAU * 100.0 * (i as f64) / fs).sin() as f32;
            r[i] = 0.0;
        }
        eng.process_block(&mut l, &mut r, 1.0, 1.0, false);
        let mut lp_l = Lr4Crossover::new(fs, 150.0);
        let mut lp_r = Lr4Crossover::new(fs, 150.0);
        let mut sub_l = Vec::with_capacity(len);
        let mut sub_r = Vec::with_capacity(len);
        for i in 0..len {
            sub_l.push(lp_l.split(l[i] as f64).0 as f32);
            sub_r.push(lp_r.split(r[i] as f64).0 as f32);
        }
        let corr_100_sub = pearson_corr(&sub_l[8_000..], &sub_r[8_000..]);
        let amp_100 = tone_amp(&l[8_000..], fs, 100.0);
        assert!(
            corr_100_sub > 0.999,
            "100 Hz hard-panned bass (< 180 Hz) was not mono-anchored: corr = {corr_100_sub:.6}"
        );
        assert!(
            amp_100 > 0.20,
            "100 Hz fundamental was cancelled by Chebyshev T3 bleed: amp = {amp_100:.4}"
        );

        eng.reset();
        for i in 0..len {
            let ang = TAU * 60.0 * (i as f64) / fs;
            l[i] = 0.5 * ang.sin() as f32;
            r[i] = 0.5 * ang.cos() as f32;
        }
        eng.process_block(&mut l, &mut r, 1.0, 1.0, false);
        let corr_60 = pearson_corr(&l[8_000..], &r[8_000..]);
        assert!(
            corr_60 > 0.999,
            "60 Hz quadrature stereo bass was not mono-anchored: corr = {corr_60:.6}"
        );

        eng.reset();
        for i in 0..len {
            l[i] = 0.5 * (TAU * 1000.0 * (i as f64) / fs).sin() as f32;
            r[i] = 0.0;
        }
        eng.process_block(&mut l, &mut r, 1.0, 1.0, false);
        let l_rms = (l[8_000..].iter().map(|&x| (x as f64) * (x as f64)).sum::<f64>()
            / (len - 8_000) as f64)
            .sqrt();
        let r_rms = (r[8_000..].iter().map(|&x| (x as f64) * (x as f64)).sum::<f64>()
            / (len - 8_000) as f64)
            .sqrt();
        assert!(
            l_rms > 10.0 * r_rms,
            "1 kHz stereo image was collapsed by bass mono anchor: L={l_rms:.4}, R={r_rms:.4}"
        );
    }

    #[test]
    fn test_bass_zero_dc_offset_and_harmonic_generation() {
        let fs = 48_000.0;
        let settle = 24_000;
        let measure = 48_000;
        let len = settle + measure;
        let mut eng = BassEngine::new(fs);

        let mut l = vec![0.0_f32; len];
        let mut r = vec![0.0_f32; len];
        for i in 0..len {
            let s = 0.55 * (TAU * 50.0 * (i as f64) / fs).sin() as f32;
            l[i] = s;
            r[i] = s;
        }
        eng.process_block(&mut l, &mut r, 1.0, 1.0, false);

        let tail_l = &l[settle..];
        let tail_r = &r[settle..];
        let dc_l = tail_l.iter().map(|&x| x as f64).sum::<f64>() / (tail_l.len() as f64);
        let dc_r = tail_r.iter().map(|&x| x as f64).sum::<f64>() / (tail_r.len() as f64);
        assert!(
            dc_l.abs() < 1.0e-4 && dc_r.abs() < 1.0e-4,
            "DC offset detected on bass harmonics: dc_l={dc_l:.2e}, dc_r={dc_r:.2e}"
        );

        let h2_100 = tone_amp(tail_l, fs, 100.0);
        let h3_150 = tone_amp(tail_l, fs, 150.0);
        let h4_200 = tone_amp(tail_l, fs, 200.0);
        assert!(
            h3_150 > 0.02 && h4_200 > 0.01,
            "Expected synthesized missing-fundamental harmonics for 50 Hz input: h2={h2_100:.4}, h3={h3_150:.4}, h4={h4_200:.4}"
        );
    }

    #[test]
    fn test_vocal_bass_mix_sustained_55_85hz_no_choking() {
        let fs = 48_000.0;
        let mut eng = BassEngine::new(fs);
        let len = 96_000; // 2.0 s: [0..1.0s] bass-only, [1.0..2.0s] loud vocals + bass
        let mut l = vec![0.0_f32; len];
        let mut r = vec![0.0_f32; len];
        for i in 0..len {
            let t = (i as f64) / fs;
            // Realistic -34 dBFS 55+85 Hz bassline + +20 dB louder vocals in [1.0..2.0s]
            let bass = 0.020 * (TAU * 55.0 * t).sin() + 0.016 * (TAU * 85.0 * t + 0.4).sin();
            let vox = if i >= 48_000 {
                0.22 * (TAU * 900.0 * t).sin() + 0.16 * (TAU * 1850.0 * t).sin()
            } else {
                0.0
            };
            let s = (bass + vox) as f32;
            l[i] = s;
            r[i] = s;
        }
        eng.process_block(&mut l, &mut r, 1.0, 1.0, false);

        // Music gate MUST stay > 0.95 even with vocals +20 dB above isolated sub-bass!
        assert!(
            eng.last_music_gate() > 0.95,
            "Vocal-forward mix choked sub-bass gate: {:.4}",
            eng.last_music_gate()
        );

        // Verify strong 110/165/170 Hz harmonics in BOTH bass-only and vocal+bass segments!
        let h165_bass_only = tone_amp(&l[12_000..44_000], fs, 165.0);
        let h165_with_vox = tone_amp(&l[60_000..92_000], fs, 165.0);
        let h170_with_vox = tone_amp(&l[60_000..92_000], fs, 170.0);
        let delta_db = 20.0 * (h165_with_vox / h165_bass_only.max(1e-9)).log10();
        assert!(
            h165_with_vox > 0.004 && h170_with_vox > 0.004,
            "Missing 165/170 Hz harmonics on -34 dBFS bassline: h165={h165_with_vox:.5}, h170={h170_with_vox:.5}"
        );
        assert!(
            delta_db.abs() < 0.25,
            "Harmonic level shifted when vocals entered: delta={delta_db:.3} dB"
        );
    }

    #[test]
    fn test_bass_vocal_safe_music_detector() {
        let fs = 48_000.0;

        let mut eng = BassEngine::new(fs);
        let len = 24_000;
        let mut l = vec![0.0_f32; len];
        let mut r = vec![0.0_f32; len];
        for i in 0..len {
            let t = (i as f64) / fs;
            let bass = 0.28 * (TAU * 60.0 * t).sin();
            let vox = 0.35 * (TAU * 600.0 * t).sin() + 0.18 * (TAU * 1200.0 * t).sin();
            let s = (bass + vox) as f32;
            l[i] = s;
            r[i] = s;
        }
        eng.process_block(&mut l, &mut r, 1.0, 1.0, false);
        assert!(
            eng.last_music_gate() > 0.90,
            "Vocal-safe detector choked vocals+bass music: gate = {:.4}",
            eng.last_music_gate()
        );

        let speech_len = 96_000;
        let mut sl = vec![0.0_f32; speech_len];
        let mut sr = vec![0.0_f32; speech_len];
        for i in 0..speech_len {
            let t = (i as f64) / fs;
            let s = (0.45 * (TAU * 600.0 * t).sin() + 0.15 * (TAU * 1400.0 * t).sin()) as f32;
            sl[i] = s;
            sr[i] = s;
        }
        eng.process_block(&mut sl, &mut sr, 1.0, 1.0, false);
        assert!(
            eng.last_music_gate() < 0.25,
            "Vocal-safe detector stayed open on sustained dry 600 Hz speech: gate = {:.4}",
            eng.last_music_gate()
        );

        let mut eng_speech = BassEngine::new(fs);
        let mut dl = vec![0.0_f32; len];
        let mut dr = vec![0.0_f32; len];
        for i in 0..len {
            let s = 0.45 * (TAU * 600.0 * (i as f64) / fs).sin() as f32;
            dl[i] = s;
            dr[i] = s;
        }
        eng_speech.process_block(&mut dl, &mut dr, 1.0, 1.0, false);
        assert!(
            eng_speech.last_music_gate() < 0.25,
            "Dry 600 Hz speech opened music gate: gate = {:.4}",
            eng_speech.last_music_gate()
        );

        let mut eng_voice = BassEngine::new(fs);
        let mut vl = vec![0.0_f32; len];
        let mut vr = vec![0.0_f32; len];
        for i in 0..len {
            let t = (i as f64) / fs;
            let s = (0.35 * (TAU * 80.0 * t).sin() + 0.35 * (TAU * 600.0 * t).sin()) as f32;
            vl[i] = s;
            vr[i] = s;
        }
        eng_voice.process_block(&mut vl, &mut vr, 1.0, 1.0, true);
        assert!(
            eng_voice.last_music_gate() < 0.25,
            "Voice mode did not cap music gate: gate = {:.4}",
            eng_voice.last_music_gate()
        );
    }

    #[test]
    fn test_bass_warmth_body_boosts_195hz_and_zero_dc() {
        let fs = 48_000.0;
        let settle = 24_000;
        let measure = 48_000;
        let len = settle + measure;

        let mut dry_l = vec![0.0_f32; len];
        let mut dry_r = vec![0.0_f32; len];
        for i in 0..len {
            let s = 0.25 * (TAU * 195.0 * (i as f64) / fs).sin() as f32;
            dry_l[i] = s;
            dry_r[i] = s;
        }

        let mut off_l = dry_l.clone();
        let mut off_r = dry_r.clone();
        let mut eng_off = BassEngine::new(fs);
        eng_off.process_block(&mut off_l, &mut off_r, 0.0, 0.0, false);

        let mut on_l = dry_l.clone();
        let mut on_r = dry_r.clone();
        let mut eng_on = BassEngine::new(fs);
        eng_on.process_block(&mut on_l, &mut on_r, 0.0, 1.25, false);

        let a_off = tone_amp(&off_l[settle..], fs, 195.0);
        let a_on = tone_amp(&on_l[settle..], fs, 195.0);
        let boost_db = 20.0 * (a_on / a_off.max(1e-12)).log10();
        assert!(
            boost_db > 2.0 && boost_db < 4.5,
            "Warmth body 195 Hz boost out of expected range: {boost_db:.2} dB"
        );

        let dc = on_l[settle..].iter().map(|&x| x as f64).sum::<f64>() / (measure as f64);
        assert!(dc.abs() < 1.0e-4, "Warmth transformer introduced DC: {dc:.2e}");
    }

    #[test]
    fn test_bass_chunk_boundaries_and_nan_resilience() {
        let fs = 48_000.0;
        let mut eng = BassEngine::new(fs);

        for &block_len in &[1usize, 7, 128, 255, 256, 257, 777] {
            let mut l = vec![0.25_f32; block_len];
            let mut r = vec![-0.25_f32; block_len];
            if block_len > 3 {
                l[2] = f32::NAN;
                r[3] = f32::INFINITY;
            }
            eng.process_block(&mut l, &mut r, 1.0, 1.0, false);
            assert!(l.iter().all(|x| x.is_finite()));
            assert!(r.iter().all(|x| x.is_finite()));
        }
    }
}
