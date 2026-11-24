//! Andrew Simper Cytomic Zero-Delay-Feedback State-Variable Filter (ZDF-SVF)
//! and phase-coherent 4th-order Linkwitz-Riley (LR4) crossover primitives (`f64`).
//!
//! Reference:
//! - Andrew Simper, "Linear Trapezoidal Integrated State Variable Filter (SVF)",
//!   Cytomic Technical Paper, 2013/2014.
//! - Siegfried H. Linkwitz, "Active Crossover Networks for Noncoincident Drivers",
//!   J. Audio Eng. Soc., Vol. 24, No. 1, pp. 2-8, 1976.
//!
//! Hard-real-time guarantees:
//! - Zero heap allocations (`Copy`, stack-only state).
//! - Unconditionally stable under per-sample cutoff/Q modulation (trapezoidal integration).
//! - NaN/Inf/denormal-safe internal state updates.

use std::f64::consts::{FRAC_1_SQRT_2, PI};

/// Denormal flush threshold (~ -600 dBFS in `f64`, 278 decades above `f64::MIN_POSITIVE`).
const DENORMAL_FLUSH: f64 = 1.0e-30;

/// Sanitizes an `f64` sample or filter state against NaN, Inf, and subnormals.
#[inline(always)]
fn sanitize_f64(v: f64) -> f64 {
    if !v.is_finite() || v.abs() < DENORMAL_FLUSH {
        0.0
    } else {
        v
    }
}

/// Clamps frequency safely to `[5.0, 0.48 * sample_rate]`.
#[inline(always)]
fn clamp_freq(freq_hz: f64, sample_rate: f64) -> f64 {
    let f = if freq_hz.is_finite() { freq_hz } else { 1000.0 };
    f.clamp(5.0, 0.48 * sample_rate)
}

/// Clamps filter Q safely to `[0.1, 20.0]`.
#[inline(always)]
fn clamp_q(q: f64) -> f64 {
    let q_clean = if q.is_finite() { q } else { FRAC_1_SQRT_2 };
    q_clean.clamp(0.1, 20.0)
}

/// Clamps EQ gain safely to `[-36.0, 36.0]` dB.
#[inline(always)]
fn clamp_gain_db(gain_db: f64) -> f64 {
    let g = if gain_db.is_finite() { gain_db } else { 0.0 };
    g.clamp(-36.0, 36.0)
}

/// Internal mode tag allowing `set_cutoff_q_fast` to preserve the active
/// topology without recomputing `powf`/`sqrt` per sample.
#[derive(Debug, Clone, Copy)]
enum SvfMode {
    Bypass,
    Lowpass,
    Highpass,
    Bandpass,
    Notch,
    Bell {
        a: f64,
        a_sq_minus_1: f64,
    },
    LowShelf {
        inv_sqrt_a: f64,
        a_minus_1: f64,
    },
    HighShelf {
        sqrt_a: f64,
        one_minus_a_times_a: f64,
    },
    Allpass,
}

/// Double-precision Andrew Simper Cytomic Zero-Delay-Feedback State-Variable Filter.
///
/// Implements linear trapezoidal integration of the canonical 2-pole SVF topology:
/// ```text
/// v3 = x - ic2eq
/// v1 = a1 * ic1eq + a2 * v3
/// v2 = ic2eq + a2 * ic1eq + a3 * v3
/// ic1eq = 2 * v1 - ic1eq
/// ic2eq = 2 * v2 - ic2eq
/// y  = m0 * x + m1 * v1 + m2 * v2
/// ```
/// Because state energy is preserved across arbitrary coefficient updates,
/// modulating `freq_hz` per sample via [`CytomicSvf::set_cutoff_q_fast`] is
/// click-free and unconditionally stable.
#[derive(Debug, Clone, Copy)]
pub struct CytomicSvf {
    sample_rate: f64,
    g: f64,
    k: f64,
    a1: f64,
    a2: f64,
    a3: f64,
    m0: f64,
    m1: f64,
    m2: f64,
    ic1eq: f64,
    ic2eq: f64,
    mode: SvfMode,
}

impl CytomicSvf {
    /// Creates a new Cytomic ZDF-SVF initialized in flat unity bypass mode.
    pub fn new(sample_rate: f64) -> Self {
        let sr = if sample_rate.is_finite() && sample_rate >= 8000.0 {
            sample_rate
        } else {
            48_000.0
        };
        let mut svf = Self {
            sample_rate: sr,
            g: 0.0,
            k: 1.0 / FRAC_1_SQRT_2,
            a1: 0.0,
            a2: 0.0,
            a3: 0.0,
            m0: 1.0,
            m1: 0.0,
            m2: 0.0,
            ic1eq: 0.0,
            ic2eq: 0.0,
            mode: SvfMode::Bypass,
        };
        svf.update_core_coeffs((PI * 1000.0 / sr).tan(), 1.0 / FRAC_1_SQRT_2);
        svf
    }

    /// Clears both trapezoidal integrator capacitor states to zero.
    #[inline]
    pub fn reset(&mut self) {
        self.ic1eq = 0.0;
        self.ic2eq = 0.0;
    }

    /// Updates `(g, k, a1, a2, a3)` from positive `g` and `k`.
    #[inline(always)]
    fn update_core_coeffs(&mut self, g: f64, k: f64) {
        self.g = g;
        self.k = k;
        let denom = 1.0 + g * (g + k);
        self.a1 = if denom > 1.0e-18 && denom.is_finite() {
            1.0 / denom
        } else {
            0.0
        };
        self.a2 = g * self.a1;
        self.a3 = g * self.a2;
    }

    /// Copies all computed filter coefficients (`g, k, a1, a2, a3, m0, m1, m2, mode`)
    /// from another `CytomicSvf` without touching this filter's integrator states.
    /// Useful for stereo pairs and cascaded Butterworth/LR4 stages sharing identical cutoff.
    #[inline(always)]
    pub(crate) fn copy_coeffs_from(&mut self, other: &Self) {
        self.g = other.g;
        self.k = other.k;
        self.a1 = other.a1;
        self.a2 = other.a2;
        self.a3 = other.a3;
        self.m0 = other.m0;
        self.m1 = other.m1;
        self.m2 = other.m2;
        self.mode = other.mode;
    }

    /// Configures a 2nd-order low-pass filter (`H(0) = 1`, `-12 dB/oct` above `freq_hz`).
    pub fn set_lowpass(&mut self, freq_hz: f64, q: f64) {
        let fc = clamp_freq(freq_hz, self.sample_rate);
        let q = clamp_q(q);
        let g = (PI * fc / self.sample_rate).tan();
        let k = 1.0 / q;
        self.update_core_coeffs(g, k);
        self.m0 = 0.0;
        self.m1 = 0.0;
        self.m2 = 1.0;
        self.mode = SvfMode::Lowpass;
    }

    /// Configures a 2nd-order high-pass filter (`H(0) = 0`, `-12 dB/oct` below `freq_hz`).
    pub fn set_highpass(&mut self, freq_hz: f64, q: f64) {
        let fc = clamp_freq(freq_hz, self.sample_rate);
        let q = clamp_q(q);
        let g = (PI * fc / self.sample_rate).tan();
        let k = 1.0 / q;
        self.update_core_coeffs(g, k);
        self.m0 = 1.0;
        self.m1 = -k;
        self.m2 = -1.0;
        self.mode = SvfMode::Highpass;
    }

    /// Configures a 2nd-order constant-0 dB-peak bandpass filter (`y = k * v1`).
    pub fn set_bandpass(&mut self, freq_hz: f64, q: f64) {
        let fc = clamp_freq(freq_hz, self.sample_rate);
        let q = clamp_q(q);
        let g = (PI * fc / self.sample_rate).tan();
        let k = 1.0 / q;
        self.update_core_coeffs(g, k);
        self.m0 = 0.0;
        self.m1 = k;
        self.m2 = 0.0;
        self.mode = SvfMode::Bandpass;
    }

    /// Configures a 2nd-order band-reject (notch) filter (`H(fc) = 0`).
    pub fn set_notch(&mut self, freq_hz: f64, q: f64) {
        let fc = clamp_freq(freq_hz, self.sample_rate);
        let q = clamp_q(q);
        let g = (PI * fc / self.sample_rate).tan();
        let k = 1.0 / q;
        self.update_core_coeffs(g, k);
        self.m0 = 1.0;
        self.m1 = -k;
        self.m2 = 0.0;
        self.mode = SvfMode::Notch;
    }

    /// Configures an exact Cytomic symmetrical peaking (bell) EQ filter:
    /// `A = 10^(gain_db / 40)`, `k = 1 / (Q * A)`, `m0 = 1`, `m1 = k * (A^2 - 1)`, `m2 = 0`.
    pub fn set_bell(&mut self, freq_hz: f64, q: f64, gain_db: f64) {
        let fc = clamp_freq(freq_hz, self.sample_rate);
        let q = clamp_q(q);
        let gdb = clamp_gain_db(gain_db);
        let a = 10.0_f64.powf(gdb / 40.0);
        let g = (PI * fc / self.sample_rate).tan();
        let k = 1.0 / (q * a);
        let a_sq_minus_1 = a * a - 1.0;
        self.update_core_coeffs(g, k);
        self.m0 = 1.0;
        self.m1 = k * a_sq_minus_1;
        self.m2 = 0.0;
        self.mode = SvfMode::Bell { a, a_sq_minus_1 };
    }

    /// Configures an exact Cytomic low-shelf filter:
    /// `A = 10^(gain_db / 40)`, `g = tan(pi * fc / fs) / sqrt(A)`, `k = 1 / Q`,
    /// `m0 = 1`, `m1 = k * (A - 1)`, `m2 = A^2 - 1`.
    pub fn set_lowshelf(&mut self, freq_hz: f64, q: f64, gain_db: f64) {
        let fc = clamp_freq(freq_hz, self.sample_rate);
        let q = clamp_q(q);
        let gdb = clamp_gain_db(gain_db);
        let a = 10.0_f64.powf(gdb / 40.0);
        let inv_sqrt_a = 1.0 / a.sqrt();
        let g = (PI * fc / self.sample_rate).tan() * inv_sqrt_a;
        let k = 1.0 / q;
        let a_minus_1 = a - 1.0;
        self.update_core_coeffs(g, k);
        self.m0 = 1.0;
        self.m1 = k * a_minus_1;
        self.m2 = a * a - 1.0;
        self.mode = SvfMode::LowShelf {
            inv_sqrt_a,
            a_minus_1,
        };
    }

    /// Configures an exact Cytomic high-shelf filter:
    /// `A = 10^(gain_db / 40)`, `g = tan(pi * fc / fs) * sqrt(A)`, `k = 1 / Q`,
    /// `m0 = A^2`, `m1 = k * (1 - A) * A`, `m2 = 1 - A^2`.
    pub fn set_highshelf(&mut self, freq_hz: f64, q: f64, gain_db: f64) {
        let fc = clamp_freq(freq_hz, self.sample_rate);
        let q = clamp_q(q);
        let gdb = clamp_gain_db(gain_db);
        let a = 10.0_f64.powf(gdb / 40.0);
        let sqrt_a = a.sqrt();
        let g = (PI * fc / self.sample_rate).tan() * sqrt_a;
        let k = 1.0 / q;
        let a_sq = a * a;
        let one_minus_a_times_a = (1.0 - a) * a;
        self.update_core_coeffs(g, k);
        self.m0 = a_sq;
        self.m1 = k * one_minus_a_times_a;
        self.m2 = 1.0 - a_sq;
        self.mode = SvfMode::HighShelf {
            sqrt_a,
            one_minus_a_times_a,
        };
    }

    /// Configures a 2nd-order unity-magnitude all-pass filter (`m0 = 1`, `m1 = -2*k`, `m2 = 0`).
    /// At `freq_hz`, phase shift is `-180` degrees.
    pub fn set_allpass(&mut self, freq_hz: f64, q: f64) {
        let fc = clamp_freq(freq_hz, self.sample_rate);
        let q = clamp_q(q);
        let g = (PI * fc / self.sample_rate).tan();
        let k = 1.0 / q;
        self.update_core_coeffs(g, k);
        self.m0 = 1.0;
        self.m1 = -2.0 * k;
        self.m2 = 0.0;
        self.mode = SvfMode::Allpass;
    }

    /// Fast per-sample cutoff and Q update (`g, k, a1, a2, a3`) preserving the
    /// active filter mix mode (`m0, m1, m2`) with zero `powf`/`sqrt` overhead.
    /// Ideal for click-free audio-rate sliding HPF and dynamic filters.
    #[inline]
    pub fn set_cutoff_q_fast(&mut self, freq_hz: f64, q: f64) {
        let fc = clamp_freq(freq_hz, self.sample_rate);
        let q = clamp_q(q);
        let g0 = (PI * fc / self.sample_rate).tan();
        match self.mode {
            SvfMode::Bypass | SvfMode::Lowpass => {
                let k = 1.0 / q;
                self.update_core_coeffs(g0, k);
            }
            SvfMode::Highpass | SvfMode::Notch => {
                let k = 1.0 / q;
                self.update_core_coeffs(g0, k);
                self.m1 = -k;
            }
            SvfMode::Bandpass => {
                let k = 1.0 / q;
                self.update_core_coeffs(g0, k);
                self.m1 = k;
            }
            SvfMode::Allpass => {
                let k = 1.0 / q;
                self.update_core_coeffs(g0, k);
                self.m1 = -2.0 * k;
            }
            SvfMode::Bell { a, a_sq_minus_1 } => {
                let k = 1.0 / (q * a);
                self.update_core_coeffs(g0, k);
                self.m1 = k * a_sq_minus_1;
            }
            SvfMode::LowShelf {
                inv_sqrt_a,
                a_minus_1,
            } => {
                let k = 1.0 / q;
                self.update_core_coeffs(g0 * inv_sqrt_a, k);
                self.m1 = k * a_minus_1;
            }
            SvfMode::HighShelf {
                sqrt_a,
                one_minus_a_times_a,
            } => {
                let k = 1.0 / q;
                self.update_core_coeffs(g0 * sqrt_a, k);
                self.m1 = k * one_minus_a_times_a;
            }
        }
    }

    /// Processes one sample through the configured filter mode.
    #[inline]
    pub fn process(&mut self, x: f64) -> f64 {
        let v0 = if x.is_finite() { x } else { 0.0 };
        let v3 = v0 - self.ic2eq;
        let v1 = self.a1 * self.ic1eq + self.a2 * v3;
        let v2 = self.ic2eq + self.a2 * self.ic1eq + self.a3 * v3;
        self.ic1eq = sanitize_f64(2.0 * v1 - self.ic1eq);
        self.ic2eq = sanitize_f64(2.0 * v2 - self.ic2eq);
        let y = self.m0 * v0 + self.m1 * v1 + self.m2 * v2;
        if y.is_finite() { y } else { 0.0 }
    }

    /// Processes one sample and simultaneously returns `(lp, bp, hp)` equal to
    /// `(v2, v1, x - k * v1 - v2)`.
    #[inline]
    pub fn process_outputs(&mut self, x: f64) -> (f64, f64, f64) {
        let v0 = if x.is_finite() { x } else { 0.0 };
        let v3 = v0 - self.ic2eq;
        let v1 = self.a1 * self.ic1eq + self.a2 * v3;
        let v2 = self.ic2eq + self.a2 * self.ic1eq + self.a3 * v3;
        self.ic1eq = sanitize_f64(2.0 * v1 - self.ic1eq);
        self.ic2eq = sanitize_f64(2.0 * v2 - self.ic2eq);
        let lp = if v2.is_finite() { v2 } else { 0.0 };
        let bp = if v1.is_finite() { v1 } else { 0.0 };
        let hp_raw = v0 - self.k * v1 - v2;
        let hp = if hp_raw.is_finite() { hp_raw } else { 0.0 };
        (lp, bp, hp)
    }
}

/// Phase-coherent 4th-order Linkwitz-Riley (`24 dB/oct`) crossover implemented
/// as cascaded 2nd-order Butterworth (`Q = 1/sqrt(2)`) Cytomic ZDF-SVF stages.
///
/// Mathematical identity:
/// `H_LP4(s') + H_HP4(s') = (1 + s'^4) / (s'^2 + sqrt(2)*s' + 1)^2`
/// `                      = (s'^2 - sqrt(2)*s' + 1) / (s'^2 + sqrt(2)*s' + 1)`
/// `                      = H_AP2_Butterworth(s')`
///
/// Therefore `low + high` is strictly in-phase with exact `0.000 dB` flat
/// unity magnitude across the entire spectrum (`0 Hz .. Nyquist`), and matches
/// the exact phase response of [`AllpassMatch`] at the same frequency.
#[derive(Debug, Clone, Copy)]
pub struct Lr4Crossover {
    /// Shared first Butterworth stage extracting both LP2 and HP2 simultaneously.
    stage1: CytomicSvf,
    /// Second Butterworth LP2 stage (`LP2 * LP2 = LP4`).
    lp2: CytomicSvf,
    /// Second Butterworth HP2 stage (`HP2 * HP2 = HP4`).
    hp2: CytomicSvf,
}

impl Lr4Crossover {
    /// Creates an LR4 crossover at `freq_hz`.
    pub fn new(sample_rate: f64, freq_hz: f64) -> Self {
        let mut stage1 = CytomicSvf::new(sample_rate);
        let mut lp2 = CytomicSvf::new(sample_rate);
        let mut hp2 = CytomicSvf::new(sample_rate);
        stage1.set_lowpass(freq_hz, FRAC_1_SQRT_2);
        lp2.set_lowpass(freq_hz, FRAC_1_SQRT_2);
        hp2.set_highpass(freq_hz, FRAC_1_SQRT_2);
        Self { stage1, lp2, hp2 }
    }

    /// Updates the crossover frequency click-free (preserving filter states).
    #[inline]
    pub fn set_freq(&mut self, freq_hz: f64) {
        self.stage1.set_cutoff_q_fast(freq_hz, FRAC_1_SQRT_2);
        self.lp2.copy_coeffs_from(&self.stage1);
        self.hp2.set_cutoff_q_fast(freq_hz, FRAC_1_SQRT_2);
    }

    /// Resets all internal filter states to zero.
    #[inline]
    pub fn reset(&mut self) {
        self.stage1.reset();
        self.lp2.reset();
        self.hp2.reset();
    }

    /// Splits input `x` into `(low, high)` such that `low + high` has exact
    /// unity magnitude (`0.00 dB`) across `20 Hz .. 20 kHz`.
    #[inline]
    pub fn split(&mut self, x: f64) -> (f64, f64) {
        let (lp_mid, _bp_mid, hp_mid) = self.stage1.process_outputs(x);
        let low = self.lp2.process(lp_mid);
        let high = self.hp2.process(hp_mid);
        (low, high)
    }
}

/// 2nd-order Butterworth all-pass filter (`Q = 1/sqrt(2)`) whose phase shift
/// exactly matches the recombined sum `low + high` of [`Lr4Crossover`] at the
/// same frequency.
///
/// Inserting `AllpassMatch` on non-split parallel branches ensures multi-way
/// (3-band, 4-band) LR4 crossover networks recombine to exact `0.00 dB` flat
/// magnitude across the entire audio spectrum.
#[derive(Debug, Clone, Copy)]
pub struct AllpassMatch {
    ap: CytomicSvf,
}

impl AllpassMatch {
    /// Creates a Butterworth phase-matching all-pass at `freq_hz`.
    pub fn new(sample_rate: f64, freq_hz: f64) -> Self {
        let mut ap = CytomicSvf::new(sample_rate);
        ap.set_allpass(freq_hz, FRAC_1_SQRT_2);
        Self { ap }
    }

    /// Updates the all-pass match frequency click-free.
    #[inline]
    pub fn set_freq(&mut self, freq_hz: f64) {
        self.ap.set_cutoff_q_fast(freq_hz, FRAC_1_SQRT_2);
    }

    /// Clears internal filter state to zero.
    #[inline]
    pub fn reset(&mut self) {
        self.ap.reset();
    }

    /// Processes one sample through the unity-magnitude phase-matching all-pass.
    #[inline]
    pub fn process(&mut self, x: f64) -> f64 {
        self.ap.process(x)
    }
}

// ============================================================================
// High-Throughput Branchless `f32` & Stereo `[f32; 2]` Cytomic ZDF-SVF Primitives
// (Coefficients computed in `f64`, inner audio loop branchless FMA in `f32` under FTZ/DAZ)
// ============================================================================

/// Fast mono `f32` Cytomic ZDF-SVF with `f64` coefficient design and branchless `f32` state updates.
#[derive(Debug, Clone, Copy)]
pub(crate) struct SvfF32 {
    sample_rate: f64,
    a1: f32,
    a2: f32,
    a3: f32,
    k: f32,
    m0: f32,
    m1: f32,
    m2: f32,
    ic1: f32,
    ic2: f32,
}

impl SvfF32 {
    #[inline]
    pub fn new(sample_rate: f64) -> Self {
        let sr = if sample_rate.is_finite() && sample_rate >= 8000.0 {
            sample_rate
        } else {
            48_000.0
        };
        let mut s = Self {
            sample_rate: sr,
            a1: 0.0,
            a2: 0.0,
            a3: 0.0,
            k: (1.0 / FRAC_1_SQRT_2) as f32,
            m0: 1.0,
            m1: 0.0,
            m2: 0.0,
            ic1: 0.0,
            ic2: 0.0,
        };
        s.set_core_gk((PI * 1000.0 / sr).tan(), 1.0 / FRAC_1_SQRT_2);
        s
    }

    #[inline(always)]
    pub fn reset(&mut self) {
        self.ic1 = 0.0;
        self.ic2 = 0.0;
    }

    #[inline(always)]
    fn set_core_gk(&mut self, g: f64, k: f64) {
        let denom = 1.0 + g * (g + k);
        let a1 = if denom > 1.0e-18 && denom.is_finite() {
            1.0 / denom
        } else {
            0.0
        };
        let a2 = g * a1;
        let a3 = g * a2;
        self.a1 = a1 as f32;
        self.a2 = a2 as f32;
        self.a3 = a3 as f32;
        self.k = k as f32;
    }

    #[inline(always)]
    pub fn copy_coeffs_from(&mut self, other: &Self) {
        self.a1 = other.a1;
        self.a2 = other.a2;
        self.a3 = other.a3;
        self.k = other.k;
        self.m0 = other.m0;
        self.m1 = other.m1;
        self.m2 = other.m2;
    }

    #[inline]
    pub fn set_lowpass(&mut self, freq_hz: f64, q: f64) {
        let fc = clamp_freq(freq_hz, self.sample_rate);
        let q = clamp_q(q);
        self.set_core_gk((PI * fc / self.sample_rate).tan(), 1.0 / q);
        self.m0 = 0.0;
        self.m1 = 0.0;
        self.m2 = 1.0;
    }

    #[inline]
    pub fn set_highpass(&mut self, freq_hz: f64, q: f64) {
        let fc = clamp_freq(freq_hz, self.sample_rate);
        let q = clamp_q(q);
        let k = 1.0 / q;
        self.set_core_gk((PI * fc / self.sample_rate).tan(), k);
        self.m0 = 1.0;
        self.m1 = -(k as f32);
        self.m2 = -1.0;
    }

    #[inline]
    pub fn set_bandpass(&mut self, freq_hz: f64, q: f64) {
        let fc = clamp_freq(freq_hz, self.sample_rate);
        let q = clamp_q(q);
        let k = 1.0 / q;
        self.set_core_gk((PI * fc / self.sample_rate).tan(), k);
        self.m0 = 0.0;
        self.m1 = k as f32;
        self.m2 = 0.0;
    }

    #[inline]
    pub fn set_allpass(&mut self, freq_hz: f64, q: f64) {
        let fc = clamp_freq(freq_hz, self.sample_rate);
        let q = clamp_q(q);
        let k = 1.0 / q;
        self.set_core_gk((PI * fc / self.sample_rate).tan(), k);
        self.m0 = 1.0;
        self.m1 = (-2.0 * k) as f32;
        self.m2 = 0.0;
    }

    #[inline]
    pub fn set_highshelf(&mut self, freq_hz: f64, q: f64, gain_db: f64) {
        let fc = clamp_freq(freq_hz, self.sample_rate);
        let q = clamp_q(q);
        let gdb = clamp_gain_db(gain_db);
        let a = 10.0_f64.powf(gdb / 40.0);
        let g = (PI * fc / self.sample_rate).tan() * a.sqrt();
        let k = 1.0 / q;
        let a_sq = a * a;
        self.set_core_gk(g, k);
        self.m0 = a_sq as f32;
        self.m1 = (k * (1.0 - a) * a) as f32;
        self.m2 = (1.0 - a_sq) as f32;
    }

    #[inline(always)]
    pub fn process(&mut self, x: f32) -> f32 {
        let v3 = x - self.ic2;
        let v1 = self.a1.mul_add(self.ic1, self.a2 * v3);
        let v2 = self.ic2 + self.a2.mul_add(self.ic1, self.a3 * v3);
        self.ic1 = 2.0f32.mul_add(v1, -self.ic1);
        self.ic2 = 2.0f32.mul_add(v2, -self.ic2);
        self.m0.mul_add(x, self.m1.mul_add(v1, self.m2 * v2))
    }

    #[inline(always)]
    pub fn process_lp(&mut self, x: f32) -> f32 {
        let v3 = x - self.ic2;
        let v1 = self.a1.mul_add(self.ic1, self.a2 * v3);
        let v2 = self.ic2 + self.a2.mul_add(self.ic1, self.a3 * v3);
        self.ic1 = 2.0f32.mul_add(v1, -self.ic1);
        self.ic2 = 2.0f32.mul_add(v2, -self.ic2);
        v2
    }

    #[inline(always)]
    pub fn process_hp(&mut self, x: f32) -> f32 {
        let v3 = x - self.ic2;
        let v1 = self.a1.mul_add(self.ic1, self.a2 * v3);
        let v2 = self.ic2 + self.a2.mul_add(self.ic1, self.a3 * v3);
        self.ic1 = 2.0f32.mul_add(v1, -self.ic1);
        self.ic2 = 2.0f32.mul_add(v2, -self.ic2);
        x - self.k.mul_add(v1, v2)
    }

    #[inline(always)]
    pub fn process_lp_hp(&mut self, x: f32) -> (f32, f32) {
        let v3 = x - self.ic2;
        let v1 = self.a1.mul_add(self.ic1, self.a2 * v3);
        let v2 = self.ic2 + self.a2.mul_add(self.ic1, self.a3 * v3);
        self.ic1 = 2.0f32.mul_add(v1, -self.ic1);
        self.ic2 = 2.0f32.mul_add(v2, -self.ic2);
        (v2, x - self.k.mul_add(v1, v2))
    }
}

/// Fast stereo `[L, R]` `__m128` register-resident Cytomic ZDF-SVF (zero stack store-forwarding stalls).
#[derive(Debug, Clone, Copy)]
pub(crate) struct StereoSvfF32 {
    sample_rate: f64,
    a1: f32,
    a2: f32,
    a3: f32,
    k: f32,
    m0: f32,
    m1: f32,
    m2: f32,
    #[cfg(target_arch = "x86_64")]
    ic1: core::arch::x86_64::__m128,
    #[cfg(target_arch = "x86_64")]
    ic2: core::arch::x86_64::__m128,
    #[cfg(not(target_arch = "x86_64"))]
    ic1: [f32; 2],
    #[cfg(not(target_arch = "x86_64"))]
    ic2: [f32; 2],
}

impl StereoSvfF32 {
    #[inline]
    pub fn new(sample_rate: f64) -> Self {
        let sr = if sample_rate.is_finite() && sample_rate >= 8000.0 {
            sample_rate
        } else {
            48_000.0
        };
        let mut s = Self {
            sample_rate: sr,
            a1: 0.0,
            a2: 0.0,
            a3: 0.0,
            k: (1.0 / FRAC_1_SQRT_2) as f32,
            m0: 1.0,
            m1: 0.0,
            m2: 0.0,
            #[cfg(target_arch = "x86_64")]
            ic1: unsafe { core::arch::x86_64::_mm_setzero_ps() },
            #[cfg(target_arch = "x86_64")]
            ic2: unsafe { core::arch::x86_64::_mm_setzero_ps() },
            #[cfg(not(target_arch = "x86_64"))]
            ic1: [0.0; 2],
            #[cfg(not(target_arch = "x86_64"))]
            ic2: [0.0; 2],
        };
        s.set_core_gk((PI * 1000.0 / sr).tan(), 1.0 / FRAC_1_SQRT_2);
        s
    }

    #[inline(always)]
    pub fn reset(&mut self) {
        #[cfg(target_arch = "x86_64")]
        unsafe {
            self.ic1 = core::arch::x86_64::_mm_setzero_ps();
            self.ic2 = core::arch::x86_64::_mm_setzero_ps();
        }
        #[cfg(not(target_arch = "x86_64"))]
        {
            self.ic1 = [0.0; 2];
            self.ic2 = [0.0; 2];
        }
    }

    #[inline(always)]
    fn set_core_gk(&mut self, g: f64, k: f64) {
        let denom = 1.0 + g * (g + k);
        let a1 = if denom > 1.0e-18 && denom.is_finite() {
            1.0 / denom
        } else {
            0.0
        };
        let a2 = g * a1;
        let a3 = g * a2;
        self.a1 = a1 as f32;
        self.a2 = a2 as f32;
        self.a3 = a3 as f32;
        self.k = k as f32;
    }

    #[inline(always)]
    pub fn copy_coeffs_from(&mut self, other: &Self) {
        self.a1 = other.a1;
        self.a2 = other.a2;
        self.a3 = other.a3;
        self.k = other.k;
        self.m0 = other.m0;
        self.m1 = other.m1;
        self.m2 = other.m2;
    }

    #[inline]
    pub fn set_lowpass(&mut self, freq_hz: f64, q: f64) {
        let fc = clamp_freq(freq_hz, self.sample_rate);
        let q = clamp_q(q);
        self.set_core_gk((PI * fc / self.sample_rate).tan(), 1.0 / q);
        self.m0 = 0.0;
        self.m1 = 0.0;
        self.m2 = 1.0;
    }

    #[inline]
    pub fn set_highpass(&mut self, freq_hz: f64, q: f64) {
        let fc = clamp_freq(freq_hz, self.sample_rate);
        let q = clamp_q(q);
        let k = 1.0 / q;
        self.set_core_gk((PI * fc / self.sample_rate).tan(), k);
        self.m0 = 1.0;
        self.m1 = -(k as f32);
        self.m2 = -1.0;
    }

    #[inline]
    pub fn set_bell(&mut self, freq_hz: f64, q: f64, gain_db: f64) {
        let fc = clamp_freq(freq_hz, self.sample_rate);
        let q = clamp_q(q);
        let gdb = clamp_gain_db(gain_db);
        let a = 10.0_f64.powf(gdb / 40.0);
        let g = (PI * fc / self.sample_rate).tan();
        let k = 1.0 / (q * a);
        self.set_core_gk(g, k);
        self.m0 = 1.0;
        self.m1 = (k * (a * a - 1.0)) as f32;
        self.m2 = 0.0;
    }

    #[inline]
    pub fn set_lowshelf(&mut self, freq_hz: f64, q: f64, gain_db: f64) {
        let fc = clamp_freq(freq_hz, self.sample_rate);
        let q = clamp_q(q);
        let gdb = clamp_gain_db(gain_db);
        let a = 10.0_f64.powf(gdb / 40.0);
        let g = (PI * fc / self.sample_rate).tan() / a.sqrt();
        let k = 1.0 / q;
        self.set_core_gk(g, k);
        self.m0 = 1.0;
        self.m1 = (k * (a - 1.0)) as f32;
        self.m2 = (a * a - 1.0) as f32;
    }

    #[inline]
    pub fn set_highshelf(&mut self, freq_hz: f64, q: f64, gain_db: f64) {
        let fc = clamp_freq(freq_hz, self.sample_rate);
        let q = clamp_q(q);
        let gdb = clamp_gain_db(gain_db);
        let a = 10.0_f64.powf(gdb / 40.0);
        let g = (PI * fc / self.sample_rate).tan() * a.sqrt();
        let k = 1.0 / q;
        let a_sq = a * a;
        self.set_core_gk(g, k);
        self.m0 = a_sq as f32;
        self.m1 = (k * (1.0 - a) * a) as f32;
        self.m2 = (1.0 - a_sq) as f32;
    }

    #[cfg(target_arch = "x86_64")]
    #[inline(always)]
    pub(crate) unsafe fn pack2(x: [f32; 2]) -> core::arch::x86_64::__m128 {
        use core::arch::x86_64::*;
        _mm_unpacklo_ps(_mm_set_ss(x[0]), _mm_set_ss(x[1]))
    }

    #[cfg(target_arch = "x86_64")]
    #[inline(always)]
    pub(crate) unsafe fn unpack2(v: core::arch::x86_64::__m128) -> [f32; 2] {
        use core::arch::x86_64::*;
        [_mm_cvtss_f32(v), _mm_cvtss_f32(_mm_shuffle_ps::<0x55>(v, v))]
    }

    #[inline(always)]
    pub fn process(&mut self, x: [f32; 2]) -> [f32; 2] {
        #[cfg(target_arch = "x86_64")]
        unsafe {
            use core::arch::x86_64::*;
            let vx = Self::pack2(x);
            let va1 = _mm_set1_ps(self.a1);
            let va2 = _mm_set1_ps(self.a2);
            let va3 = _mm_set1_ps(self.a3);
            let vtwo = _mm_set1_ps(2.0);
            let v3 = _mm_sub_ps(vx, self.ic2);
            let v1 = _mm_fmadd_ps(va1, self.ic1, _mm_mul_ps(va2, v3));
            let v2 = _mm_add_ps(self.ic2, _mm_fmadd_ps(va2, self.ic1, _mm_mul_ps(va3, v3)));
            self.ic1 = _mm_fmsub_ps(vtwo, v1, self.ic1);
            self.ic2 = _mm_fmsub_ps(vtwo, v2, self.ic2);
            let vy = _mm_fmadd_ps(
                _mm_set1_ps(self.m0),
                vx,
                _mm_fmadd_ps(_mm_set1_ps(self.m1), v1, _mm_mul_ps(_mm_set1_ps(self.m2), v2)),
            );
            return Self::unpack2(vy);
        }
        #[cfg(not(target_arch = "x86_64"))]
        {
            let v3_l = x[0] - self.ic2[0];
            let v3_r = x[1] - self.ic2[1];
            let v1_l = self.a1.mul_add(self.ic1[0], self.a2 * v3_l);
            let v1_r = self.a1.mul_add(self.ic1[1], self.a2 * v3_r);
            let v2_l = self.ic2[0] + self.a2.mul_add(self.ic1[0], self.a3 * v3_l);
            let v2_r = self.ic2[1] + self.a2.mul_add(self.ic1[1], self.a3 * v3_r);
            self.ic1[0] = 2.0f32.mul_add(v1_l, -self.ic1[0]);
            self.ic1[1] = 2.0f32.mul_add(v1_r, -self.ic1[1]);
            self.ic2[0] = 2.0f32.mul_add(v2_l, -self.ic2[0]);
            self.ic2[1] = 2.0f32.mul_add(v2_r, -self.ic2[1]);
            [
                self.m0.mul_add(x[0], self.m1.mul_add(v1_l, self.m2 * v2_l)),
                self.m0.mul_add(x[1], self.m1.mul_add(v1_r, self.m2 * v2_r)),
            ]
        }
    }

    #[inline(always)]
    pub fn process_lp(&mut self, x: [f32; 2]) -> [f32; 2] {
        #[cfg(target_arch = "x86_64")]
        unsafe {
            use core::arch::x86_64::*;
            let vx = Self::pack2(x);
            let va1 = _mm_set1_ps(self.a1);
            let va2 = _mm_set1_ps(self.a2);
            let va3 = _mm_set1_ps(self.a3);
            let vtwo = _mm_set1_ps(2.0);
            let v3 = _mm_sub_ps(vx, self.ic2);
            let v1 = _mm_fmadd_ps(va1, self.ic1, _mm_mul_ps(va2, v3));
            let v2 = _mm_add_ps(self.ic2, _mm_fmadd_ps(va2, self.ic1, _mm_mul_ps(va3, v3)));
            self.ic1 = _mm_fmsub_ps(vtwo, v1, self.ic1);
            self.ic2 = _mm_fmsub_ps(vtwo, v2, self.ic2);
            return Self::unpack2(v2);
        }
        #[cfg(not(target_arch = "x86_64"))]
        {
            let v3_l = x[0] - self.ic2[0];
            let v3_r = x[1] - self.ic2[1];
            let v1_l = self.a1.mul_add(self.ic1[0], self.a2 * v3_l);
            let v1_r = self.a1.mul_add(self.ic1[1], self.a2 * v3_r);
            let v2_l = self.ic2[0] + self.a2.mul_add(self.ic1[0], self.a3 * v3_l);
            let v2_r = self.ic2[1] + self.a2.mul_add(self.ic1[1], self.a3 * v3_r);
            self.ic1[0] = 2.0f32.mul_add(v1_l, -self.ic1[0]);
            self.ic1[1] = 2.0f32.mul_add(v1_r, -self.ic1[1]);
            self.ic2[0] = 2.0f32.mul_add(v2_l, -self.ic2[0]);
            self.ic2[1] = 2.0f32.mul_add(v2_r, -self.ic2[1]);
            [v2_l, v2_r]
        }
    }

    #[cfg(target_arch = "x86_64")]
    #[inline(always)]
    pub(crate) unsafe fn process_lp_v(
        &mut self,
        vx: core::arch::x86_64::__m128,
    ) -> core::arch::x86_64::__m128 {
        use core::arch::x86_64::*;
        let va1 = _mm_set1_ps(self.a1);
        let va2 = _mm_set1_ps(self.a2);
        let va3 = _mm_set1_ps(self.a3);
        let vtwo = _mm_set1_ps(2.0);
        let v3 = _mm_sub_ps(vx, self.ic2);
        let v1 = _mm_fmadd_ps(va1, self.ic1, _mm_mul_ps(va2, v3));
        let v2 = _mm_add_ps(self.ic2, _mm_fmadd_ps(va2, self.ic1, _mm_mul_ps(va3, v3)));
        self.ic1 = _mm_fmsub_ps(vtwo, v1, self.ic1);
        self.ic2 = _mm_fmsub_ps(vtwo, v2, self.ic2);
        v2
    }

    #[cfg(target_arch = "x86_64")]
    #[inline(always)]
    pub(crate) unsafe fn process_hp_v(
        &mut self,
        vx: core::arch::x86_64::__m128,
    ) -> core::arch::x86_64::__m128 {
        use core::arch::x86_64::*;
        let va1 = _mm_set1_ps(self.a1);
        let va2 = _mm_set1_ps(self.a2);
        let va3 = _mm_set1_ps(self.a3);
        let vk = _mm_set1_ps(self.k);
        let vtwo = _mm_set1_ps(2.0);
        let v3 = _mm_sub_ps(vx, self.ic2);
        let v1 = _mm_fmadd_ps(va1, self.ic1, _mm_mul_ps(va2, v3));
        let v2 = _mm_add_ps(self.ic2, _mm_fmadd_ps(va2, self.ic1, _mm_mul_ps(va3, v3)));
        self.ic1 = _mm_fmsub_ps(vtwo, v1, self.ic1);
        self.ic2 = _mm_fmsub_ps(vtwo, v2, self.ic2);
        _mm_sub_ps(vx, _mm_fmadd_ps(vk, v1, v2))
    }

    #[cfg(target_arch = "x86_64")]
    #[inline(always)]
    pub(crate) unsafe fn process_lp_hp_v(
        &mut self,
        vx: core::arch::x86_64::__m128,
    ) -> (core::arch::x86_64::__m128, core::arch::x86_64::__m128) {
        use core::arch::x86_64::*;
        let va1 = _mm_set1_ps(self.a1);
        let va2 = _mm_set1_ps(self.a2);
        let va3 = _mm_set1_ps(self.a3);
        let vk = _mm_set1_ps(self.k);
        let vtwo = _mm_set1_ps(2.0);
        let v3 = _mm_sub_ps(vx, self.ic2);
        let v1 = _mm_fmadd_ps(va1, self.ic1, _mm_mul_ps(va2, v3));
        let v2 = _mm_add_ps(self.ic2, _mm_fmadd_ps(va2, self.ic1, _mm_mul_ps(va3, v3)));
        self.ic1 = _mm_fmsub_ps(vtwo, v1, self.ic1);
        self.ic2 = _mm_fmsub_ps(vtwo, v2, self.ic2);
        (v2, _mm_sub_ps(vx, _mm_fmadd_ps(vk, v1, v2)))
    }

    #[inline(always)]
    pub fn process_hp(&mut self, x: [f32; 2]) -> [f32; 2] {
        #[cfg(target_arch = "x86_64")]
        unsafe {
            use core::arch::x86_64::*;
            let vx = Self::pack2(x);
            let va1 = _mm_set1_ps(self.a1);
            let va2 = _mm_set1_ps(self.a2);
            let va3 = _mm_set1_ps(self.a3);
            let vk = _mm_set1_ps(self.k);
            let vtwo = _mm_set1_ps(2.0);
            let v3 = _mm_sub_ps(vx, self.ic2);
            let v1 = _mm_fmadd_ps(va1, self.ic1, _mm_mul_ps(va2, v3));
            let v2 = _mm_add_ps(self.ic2, _mm_fmadd_ps(va2, self.ic1, _mm_mul_ps(va3, v3)));
            self.ic1 = _mm_fmsub_ps(vtwo, v1, self.ic1);
            self.ic2 = _mm_fmsub_ps(vtwo, v2, self.ic2);
            let hp = _mm_sub_ps(vx, _mm_fmadd_ps(vk, v1, v2));
            return Self::unpack2(hp);
        }
        #[cfg(not(target_arch = "x86_64"))]
        {
            let v3_l = x[0] - self.ic2[0];
            let v3_r = x[1] - self.ic2[1];
            let v1_l = self.a1.mul_add(self.ic1[0], self.a2 * v3_l);
            let v1_r = self.a1.mul_add(self.ic1[1], self.a2 * v3_r);
            let v2_l = self.ic2[0] + self.a2.mul_add(self.ic1[0], self.a3 * v3_l);
            let v2_r = self.ic2[1] + self.a2.mul_add(self.ic1[1], self.a3 * v3_r);
            self.ic1[0] = 2.0f32.mul_add(v1_l, -self.ic1[0]);
            self.ic1[1] = 2.0f32.mul_add(v1_r, -self.ic1[1]);
            self.ic2[0] = 2.0f32.mul_add(v2_l, -self.ic2[0]);
            self.ic2[1] = 2.0f32.mul_add(v2_r, -self.ic2[1]);
            [
                x[0] - self.k.mul_add(v1_l, v2_l),
                x[1] - self.k.mul_add(v1_r, v2_r),
            ]
        }
    }

    #[cfg(not(target_arch = "x86_64"))]
    #[inline(always)]
    pub fn process_lp_hp(&mut self, x: [f32; 2]) -> ([f32; 2], [f32; 2]) {
        let v3_l = x[0] - self.ic2[0];
        let v3_r = x[1] - self.ic2[1];
        let v1_l = self.a1.mul_add(self.ic1[0], self.a2 * v3_l);
        let v1_r = self.a1.mul_add(self.ic1[1], self.a2 * v3_r);
        let v2_l = self.ic2[0] + self.a2.mul_add(self.ic1[0], self.a3 * v3_l);
        let v2_r = self.ic2[1] + self.a2.mul_add(self.ic1[1], self.a3 * v3_r);
        self.ic1[0] = 2.0f32.mul_add(v1_l, -self.ic1[0]);
        self.ic1[1] = 2.0f32.mul_add(v1_r, -self.ic1[1]);
        self.ic2[0] = 2.0f32.mul_add(v2_l, -self.ic2[0]);
        self.ic2[1] = 2.0f32.mul_add(v2_r, -self.ic2[1]);
        (
            [v2_l, v2_r],
            [
                x[0] - self.k.mul_add(v1_l, v2_l),
                x[1] - self.k.mul_add(v1_r, v2_r),
            ],
        )
    }
}

#[repr(C, align(16))]
#[derive(Debug, Clone, Copy)]
struct Aligned4([f32; 4]);

/// 4-lane parallel `__m128` register-resident Cytomic ZDF-SVF (zero stack store-forwarding stalls).
#[derive(Debug, Clone, Copy)]
pub(crate) struct QuadSvfF32 {
    sample_rate: f64,
    a1: Aligned4,
    a2: Aligned4,
    a3: Aligned4,
    k: Aligned4,
    m0: Aligned4,
    m1: Aligned4,
    m2: Aligned4,
    #[cfg(target_arch = "x86_64")]
    ic1: core::arch::x86_64::__m128,
    #[cfg(target_arch = "x86_64")]
    ic2: core::arch::x86_64::__m128,
    #[cfg(not(target_arch = "x86_64"))]
    ic1: [f32; 4],
    #[cfg(not(target_arch = "x86_64"))]
    ic2: [f32; 4],
}

impl QuadSvfF32 {
    #[inline]
    pub fn new(sample_rate: f64) -> Self {
        let sr = if sample_rate.is_finite() && sample_rate >= 8000.0 {
            sample_rate
        } else {
            48_000.0
        };
        Self {
            sample_rate: sr,
            a1: Aligned4([0.0; 4]),
            a2: Aligned4([0.0; 4]),
            a3: Aligned4([0.0; 4]),
            k: Aligned4([(1.0 / FRAC_1_SQRT_2) as f32; 4]),
            m0: Aligned4([1.0; 4]),
            m1: Aligned4([0.0; 4]),
            m2: Aligned4([0.0; 4]),
            #[cfg(target_arch = "x86_64")]
            ic1: unsafe { core::arch::x86_64::_mm_setzero_ps() },
            #[cfg(target_arch = "x86_64")]
            ic2: unsafe { core::arch::x86_64::_mm_setzero_ps() },
            #[cfg(not(target_arch = "x86_64"))]
            ic1: [0.0; 4],
            #[cfg(not(target_arch = "x86_64"))]
            ic2: [0.0; 4],
        }
    }

    #[inline(always)]
    pub fn reset(&mut self) {
        #[cfg(target_arch = "x86_64")]
        unsafe {
            self.ic1 = core::arch::x86_64::_mm_setzero_ps();
            self.ic2 = core::arch::x86_64::_mm_setzero_ps();
        }
        #[cfg(not(target_arch = "x86_64"))]
        {
            self.ic1 = [0.0; 4];
            self.ic2 = [0.0; 4];
        }
    }

    #[inline]
    pub fn copy_coeffs_from(&mut self, other: &Self) {
        self.a1 = other.a1;
        self.a2 = other.a2;
        self.a3 = other.a3;
        self.k = other.k;
        self.m0 = other.m0;
        self.m1 = other.m1;
        self.m2 = other.m2;
    }

    #[inline]
    pub fn set_lane_lowpass(&mut self, lane: usize, freq_hz: f64, q: f64) {
        let fc = clamp_freq(freq_hz, self.sample_rate);
        let q = clamp_q(q);
        let g = (PI * fc / self.sample_rate).tan();
        let k = 1.0 / q;
        let a1 = 1.0 / (1.0 + g * (g + k));
        let a2 = g * a1;
        let a3 = g * a2;
        self.a1.0[lane] = a1 as f32;
        self.a2.0[lane] = a2 as f32;
        self.a3.0[lane] = a3 as f32;
        self.k.0[lane] = k as f32;
        self.m0.0[lane] = 0.0;
        self.m1.0[lane] = 0.0;
        self.m2.0[lane] = 1.0;
    }

    #[inline]
    pub fn set_lane_highpass(&mut self, lane: usize, freq_hz: f64, q: f64) {
        let fc = clamp_freq(freq_hz, self.sample_rate);
        let q = clamp_q(q);
        let g = (PI * fc / self.sample_rate).tan();
        let k = 1.0 / q;
        let a1 = 1.0 / (1.0 + g * (g + k));
        let a2 = g * a1;
        let a3 = g * a2;
        self.a1.0[lane] = a1 as f32;
        self.a2.0[lane] = a2 as f32;
        self.a3.0[lane] = a3 as f32;
        self.k.0[lane] = k as f32;
        self.m0.0[lane] = 1.0;
        self.m1.0[lane] = -(k as f32);
        self.m2.0[lane] = -1.0;
    }

    #[inline]
    pub fn set_lane_allpass(&mut self, lane: usize, freq_hz: f64, q: f64) {
        let fc = clamp_freq(freq_hz, self.sample_rate);
        let q = clamp_q(q);
        let g = (PI * fc / self.sample_rate).tan();
        let k = 1.0 / q;
        let a1 = 1.0 / (1.0 + g * (g + k));
        let a2 = g * a1;
        let a3 = g * a2;
        self.a1.0[lane] = a1 as f32;
        self.a2.0[lane] = a2 as f32;
        self.a3.0[lane] = a3 as f32;
        self.k.0[lane] = k as f32;
        self.m0.0[lane] = 1.0;
        self.m1.0[lane] = (-2.0 * k) as f32;
        self.m2.0[lane] = 0.0;
    }

    #[cfg(target_arch = "x86_64")]
    #[inline(always)]
    pub(crate) unsafe fn pack4(x: [f32; 4]) -> core::arch::x86_64::__m128 {
        use core::arch::x86_64::*;
        let p02 = _mm_unpacklo_ps(_mm_set_ss(x[0]), _mm_set_ss(x[2]));
        let p13 = _mm_unpacklo_ps(_mm_set_ss(x[1]), _mm_set_ss(x[3]));
        _mm_unpacklo_ps(p02, p13)
    }

    #[cfg(target_arch = "x86_64")]
    #[inline(always)]
    pub(crate) unsafe fn unpack4(v: core::arch::x86_64::__m128) -> [f32; 4] {
        use core::arch::x86_64::*;
        [
            _mm_cvtss_f32(v),
            _mm_cvtss_f32(_mm_shuffle_ps::<0x55>(v, v)),
            _mm_cvtss_f32(_mm_movehl_ps(v, v)),
            _mm_cvtss_f32(_mm_shuffle_ps::<0xFF>(v, v)),
        ]
    }

    #[cfg(target_arch = "x86_64")]
    #[inline(always)]
    pub(crate) unsafe fn process_v(
        &mut self,
        vx: core::arch::x86_64::__m128,
    ) -> core::arch::x86_64::__m128 {
        use core::arch::x86_64::*;
        let va1 = _mm_load_ps(self.a1.0.as_ptr());
        let va2 = _mm_load_ps(self.a2.0.as_ptr());
        let va3 = _mm_load_ps(self.a3.0.as_ptr());
        let vtwo = _mm_set1_ps(2.0);
        let v3 = _mm_sub_ps(vx, self.ic2);
        let v1 = _mm_fmadd_ps(va1, self.ic1, _mm_mul_ps(va2, v3));
        let v2 = _mm_add_ps(self.ic2, _mm_fmadd_ps(va2, self.ic1, _mm_mul_ps(va3, v3)));
        self.ic1 = _mm_fmsub_ps(vtwo, v1, self.ic1);
        self.ic2 = _mm_fmsub_ps(vtwo, v2, self.ic2);
        _mm_fmadd_ps(
            _mm_load_ps(self.m0.0.as_ptr()),
            vx,
            _mm_fmadd_ps(
                _mm_load_ps(self.m1.0.as_ptr()),
                v1,
                _mm_mul_ps(_mm_load_ps(self.m2.0.as_ptr()), v2),
            ),
        )
    }

    #[cfg(target_arch = "x86_64")]
    #[inline(always)]
    pub(crate) unsafe fn process_lp_v(
        &mut self,
        vx: core::arch::x86_64::__m128,
    ) -> core::arch::x86_64::__m128 {
        use core::arch::x86_64::*;
        let va1 = _mm_load_ps(self.a1.0.as_ptr());
        let va2 = _mm_load_ps(self.a2.0.as_ptr());
        let va3 = _mm_load_ps(self.a3.0.as_ptr());
        let vtwo = _mm_set1_ps(2.0);
        let v3 = _mm_sub_ps(vx, self.ic2);
        let v1 = _mm_fmadd_ps(va1, self.ic1, _mm_mul_ps(va2, v3));
        let v2 = _mm_add_ps(self.ic2, _mm_fmadd_ps(va2, self.ic1, _mm_mul_ps(va3, v3)));
        self.ic1 = _mm_fmsub_ps(vtwo, v1, self.ic1);
        self.ic2 = _mm_fmsub_ps(vtwo, v2, self.ic2);
        v2
    }

    #[cfg(target_arch = "x86_64")]
    #[inline(always)]
    pub(crate) unsafe fn process_hp_v(
        &mut self,
        vx: core::arch::x86_64::__m128,
    ) -> core::arch::x86_64::__m128 {
        use core::arch::x86_64::*;
        let va1 = _mm_load_ps(self.a1.0.as_ptr());
        let va2 = _mm_load_ps(self.a2.0.as_ptr());
        let va3 = _mm_load_ps(self.a3.0.as_ptr());
        let vk = _mm_load_ps(self.k.0.as_ptr());
        let vtwo = _mm_set1_ps(2.0);
        let v3 = _mm_sub_ps(vx, self.ic2);
        let v1 = _mm_fmadd_ps(va1, self.ic1, _mm_mul_ps(va2, v3));
        let v2 = _mm_add_ps(self.ic2, _mm_fmadd_ps(va2, self.ic1, _mm_mul_ps(va3, v3)));
        self.ic1 = _mm_fmsub_ps(vtwo, v1, self.ic1);
        self.ic2 = _mm_fmsub_ps(vtwo, v2, self.ic2);
        _mm_sub_ps(vx, _mm_fmadd_ps(vk, v1, v2))
    }

    #[cfg(target_arch = "x86_64")]
    #[inline(always)]
    pub(crate) unsafe fn process_lp_hp_v(
        &mut self,
        vx: core::arch::x86_64::__m128,
    ) -> (core::arch::x86_64::__m128, core::arch::x86_64::__m128) {
        use core::arch::x86_64::*;
        let va1 = _mm_load_ps(self.a1.0.as_ptr());
        let va2 = _mm_load_ps(self.a2.0.as_ptr());
        let va3 = _mm_load_ps(self.a3.0.as_ptr());
        let vk = _mm_load_ps(self.k.0.as_ptr());
        let vtwo = _mm_set1_ps(2.0);
        let v3 = _mm_sub_ps(vx, self.ic2);
        let v1 = _mm_fmadd_ps(va1, self.ic1, _mm_mul_ps(va2, v3));
        let v2 = _mm_add_ps(self.ic2, _mm_fmadd_ps(va2, self.ic1, _mm_mul_ps(va3, v3)));
        self.ic1 = _mm_fmsub_ps(vtwo, v1, self.ic1);
        self.ic2 = _mm_fmsub_ps(vtwo, v2, self.ic2);
        (v2, _mm_sub_ps(vx, _mm_fmadd_ps(vk, v1, v2)))
    }

    #[cfg(test)]
    #[inline(always)]
    pub fn process(&mut self, x: [f32; 4]) -> [f32; 4] {
        #[cfg(target_arch = "x86_64")]
        unsafe {
            use core::arch::x86_64::*;
            let vx = Self::pack4(x);
            let va1 = _mm_load_ps(self.a1.0.as_ptr());
            let va2 = _mm_load_ps(self.a2.0.as_ptr());
            let va3 = _mm_load_ps(self.a3.0.as_ptr());
            let vtwo = _mm_set1_ps(2.0);
            let v3 = _mm_sub_ps(vx, self.ic2);
            let v1 = _mm_fmadd_ps(va1, self.ic1, _mm_mul_ps(va2, v3));
            let v2 = _mm_add_ps(self.ic2, _mm_fmadd_ps(va2, self.ic1, _mm_mul_ps(va3, v3)));
            self.ic1 = _mm_fmsub_ps(vtwo, v1, self.ic1);
            self.ic2 = _mm_fmsub_ps(vtwo, v2, self.ic2);
            let vy = _mm_fmadd_ps(
                _mm_load_ps(self.m0.0.as_ptr()),
                vx,
                _mm_fmadd_ps(
                    _mm_load_ps(self.m1.0.as_ptr()),
                    v1,
                    _mm_mul_ps(_mm_load_ps(self.m2.0.as_ptr()), v2),
                ),
            );
            return Self::unpack4(vy);
        }
        #[cfg(not(target_arch = "x86_64"))]
        {
            let mut out = [0.0f32; 4];
            for c in 0..4 {
                let v3 = x[c] - self.ic2[c];
                let v1 = self.a1.0[c].mul_add(self.ic1[c], self.a2.0[c] * v3);
                let v2 = self.ic2[c] + self.a2.0[c].mul_add(self.ic1[c], self.a3.0[c] * v3);
                self.ic1[c] = 2.0f32.mul_add(v1, -self.ic1[c]);
                self.ic2[c] = 2.0f32.mul_add(v2, -self.ic2[c]);
                out[c] = self.m0.0[c].mul_add(x[c], self.m1.0[c].mul_add(v1, self.m2.0[c] * v2));
            }
            out
        }
    }

    #[cfg(test)]
    #[inline(always)]
    pub fn process_lp(&mut self, x: [f32; 4]) -> [f32; 4] {
        #[cfg(target_arch = "x86_64")]
        unsafe {
            use core::arch::x86_64::*;
            let vx = Self::pack4(x);
            let va1 = _mm_load_ps(self.a1.0.as_ptr());
            let va2 = _mm_load_ps(self.a2.0.as_ptr());
            let va3 = _mm_load_ps(self.a3.0.as_ptr());
            let vtwo = _mm_set1_ps(2.0);
            let v3 = _mm_sub_ps(vx, self.ic2);
            let v1 = _mm_fmadd_ps(va1, self.ic1, _mm_mul_ps(va2, v3));
            let v2 = _mm_add_ps(self.ic2, _mm_fmadd_ps(va2, self.ic1, _mm_mul_ps(va3, v3)));
            self.ic1 = _mm_fmsub_ps(vtwo, v1, self.ic1);
            self.ic2 = _mm_fmsub_ps(vtwo, v2, self.ic2);
            return Self::unpack4(v2);
        }
        #[cfg(not(target_arch = "x86_64"))]
        {
            let mut out = [0.0f32; 4];
            for c in 0..4 {
                let v3 = x[c] - self.ic2[c];
                let v1 = self.a1.0[c].mul_add(self.ic1[c], self.a2.0[c] * v3);
                let v2 = self.ic2[c] + self.a2.0[c].mul_add(self.ic1[c], self.a3.0[c] * v3);
                self.ic1[c] = 2.0f32.mul_add(v1, -self.ic1[c]);
                self.ic2[c] = 2.0f32.mul_add(v2, -self.ic2[c]);
                out[c] = v2;
            }
            out
        }
    }

    #[inline(always)]
    pub fn process_hp(&mut self, x: [f32; 4]) -> [f32; 4] {
        #[cfg(target_arch = "x86_64")]
        unsafe {
            use core::arch::x86_64::*;
            let vx = Self::pack4(x);
            let va1 = _mm_load_ps(self.a1.0.as_ptr());
            let va2 = _mm_load_ps(self.a2.0.as_ptr());
            let va3 = _mm_load_ps(self.a3.0.as_ptr());
            let vk = _mm_load_ps(self.k.0.as_ptr());
            let vtwo = _mm_set1_ps(2.0);
            let v3 = _mm_sub_ps(vx, self.ic2);
            let v1 = _mm_fmadd_ps(va1, self.ic1, _mm_mul_ps(va2, v3));
            let v2 = _mm_add_ps(self.ic2, _mm_fmadd_ps(va2, self.ic1, _mm_mul_ps(va3, v3)));
            self.ic1 = _mm_fmsub_ps(vtwo, v1, self.ic1);
            self.ic2 = _mm_fmsub_ps(vtwo, v2, self.ic2);
            let hp = _mm_sub_ps(vx, _mm_fmadd_ps(vk, v1, v2));
            return Self::unpack4(hp);
        }
        #[cfg(not(target_arch = "x86_64"))]
        {
            let mut out = [0.0f32; 4];
            for c in 0..4 {
                let v3 = x[c] - self.ic2[c];
                let v1 = self.a1.0[c].mul_add(self.ic1[c], self.a2.0[c] * v3);
                let v2 = self.ic2[c] + self.a2.0[c].mul_add(self.ic1[c], self.a3.0[c] * v3);
                self.ic1[c] = 2.0f32.mul_add(v1, -self.ic1[c]);
                self.ic2[c] = 2.0f32.mul_add(v2, -self.ic2[c]);
                out[c] = x[c] - self.k.0[c].mul_add(v1, v2);
            }
            out
        }
    }

    #[cfg(test)]
    #[inline(always)]
    pub fn process_lp_hp(&mut self, x: [f32; 4]) -> ([f32; 4], [f32; 4]) {
        #[cfg(target_arch = "x86_64")]
        unsafe {
            use core::arch::x86_64::*;
            let vx = Self::pack4(x);
            let va1 = _mm_load_ps(self.a1.0.as_ptr());
            let va2 = _mm_load_ps(self.a2.0.as_ptr());
            let va3 = _mm_load_ps(self.a3.0.as_ptr());
            let vk = _mm_load_ps(self.k.0.as_ptr());
            let vtwo = _mm_set1_ps(2.0);
            let v3 = _mm_sub_ps(vx, self.ic2);
            let v1 = _mm_fmadd_ps(va1, self.ic1, _mm_mul_ps(va2, v3));
            let v2 = _mm_add_ps(self.ic2, _mm_fmadd_ps(va2, self.ic1, _mm_mul_ps(va3, v3)));
            self.ic1 = _mm_fmsub_ps(vtwo, v1, self.ic1);
            self.ic2 = _mm_fmsub_ps(vtwo, v2, self.ic2);
            let hp = _mm_sub_ps(vx, _mm_fmadd_ps(vk, v1, v2));
            return (Self::unpack4(v2), Self::unpack4(hp));
        }
        #[cfg(not(target_arch = "x86_64"))]
        {
            let mut lp_out = [0.0f32; 4];
            let mut hp_out = [0.0f32; 4];
            for c in 0..4 {
                let v3 = x[c] - self.ic2[c];
                let v1 = self.a1.0[c].mul_add(self.ic1[c], self.a2.0[c] * v3);
                let v2 = self.ic2[c] + self.a2.0[c].mul_add(self.ic1[c], self.a3.0[c] * v3);
                self.ic1[c] = 2.0f32.mul_add(v1, -self.ic1[c]);
                self.ic2[c] = 2.0f32.mul_add(v2, -self.ic2[c]);
                lp_out[c] = v2;
                hp_out[c] = x[c] - self.k.0[c].mul_add(v1, v2);
            }
            (lp_out, hp_out)
        }
    }
}

/// Fast mono `f32` phase-coherent LR4 crossover.
#[derive(Debug, Clone, Copy)]
pub(crate) struct Lr4F32 {
    stage1: SvfF32,
    lp2: SvfF32,
    hp2: SvfF32,
}

impl Lr4F32 {
    #[inline]
    pub fn new(sample_rate: f64, freq_hz: f64) -> Self {
        let mut stage1 = SvfF32::new(sample_rate);
        let mut lp2 = SvfF32::new(sample_rate);
        let mut hp2 = SvfF32::new(sample_rate);
        stage1.set_lowpass(freq_hz, FRAC_1_SQRT_2);
        lp2.copy_coeffs_from(&stage1);
        hp2.copy_coeffs_from(&stage1);
        Self { stage1, lp2, hp2 }
    }

    #[inline]
    pub fn set_freq(&mut self, freq_hz: f64) {
        self.stage1.set_lowpass(freq_hz, FRAC_1_SQRT_2);
        self.lp2.copy_coeffs_from(&self.stage1);
        self.hp2.copy_coeffs_from(&self.stage1);
    }

    #[inline(always)]
    pub fn reset(&mut self) {
        self.stage1.reset();
        self.lp2.reset();
        self.hp2.reset();
    }

    #[inline(always)]
    pub fn split(&mut self, x: f32) -> (f32, f32) {
        let (lp_m, hp_m) = self.stage1.process_lp_hp(x);
        (self.lp2.process_lp(lp_m), self.hp2.process_hp(hp_m))
    }
}

/// Fast stereo `[L, R]` phase-coherent LR4 crossover.
#[derive(Debug, Clone, Copy)]
pub(crate) struct StereoLr4F32 {
    stage1: StereoSvfF32,
    lp2: StereoSvfF32,
    hp2: StereoSvfF32,
}

impl StereoLr4F32 {
    #[inline]
    pub fn new(sample_rate: f64, freq_hz: f64) -> Self {
        let mut stage1 = StereoSvfF32::new(sample_rate);
        let mut lp2 = StereoSvfF32::new(sample_rate);
        let mut hp2 = StereoSvfF32::new(sample_rate);
        stage1.set_lowpass(freq_hz, FRAC_1_SQRT_2);
        lp2.copy_coeffs_from(&stage1);
        hp2.copy_coeffs_from(&stage1);
        Self { stage1, lp2, hp2 }
    }

    #[inline]
    pub fn set_freq(&mut self, freq_hz: f64) {
        self.stage1.set_lowpass(freq_hz, FRAC_1_SQRT_2);
        self.lp2.copy_coeffs_from(&self.stage1);
        self.hp2.copy_coeffs_from(&self.stage1);
    }

    #[inline(always)]
    pub fn reset(&mut self) {
        self.stage1.reset();
        self.lp2.reset();
        self.hp2.reset();
    }

    #[inline(always)]
    pub fn split(&mut self, x: [f32; 2]) -> ([f32; 2], [f32; 2]) {
        #[cfg(target_arch = "x86_64")]
        unsafe {
            let vx = StereoSvfF32::pack2(x);
            let (lp_m, hp_m) = self.stage1.process_lp_hp_v(vx);
            let lp2 = self.lp2.process_lp_v(lp_m);
            let hp2 = self.hp2.process_hp_v(hp_m);
            return (StereoSvfF32::unpack2(lp2), StereoSvfF32::unpack2(hp2));
        }
        #[cfg(not(target_arch = "x86_64"))]
        {
            let (lp_m, hp_m) = self.stage1.process_lp_hp(x);
            (self.lp2.process_lp(lp_m), self.hp2.process_hp(hp_m))
        }
    }
}

/// Fast mono `f32` Butterworth all-pass phase matcher for LR4 networks.
#[derive(Debug, Clone, Copy)]
pub(crate) struct AllpassF32 {
    ap: SvfF32,
}

impl AllpassF32 {
    #[inline]
    pub fn new(sample_rate: f64, freq_hz: f64) -> Self {
        let mut ap = SvfF32::new(sample_rate);
        ap.set_allpass(freq_hz, FRAC_1_SQRT_2);
        Self { ap }
    }

    #[inline]
    pub fn set_freq(&mut self, freq_hz: f64) {
        self.ap.set_allpass(freq_hz, FRAC_1_SQRT_2);
    }

    #[inline(always)]
    pub fn reset(&mut self) {
        self.ap.reset();
    }

    #[inline(always)]
    pub fn process(&mut self, x: f32) -> f32 {
        self.ap.process(x)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::f64::consts::TAU;

    /// Measures steady-state RMS gain in dB of a closure `f(x)` at `freq_hz`
    /// over an exact integer number of sine cycles to eliminate window leakage.
    fn measure_gain_db(fs: f64, freq_hz: f64, mut f: impl FnMut(f64) -> f64) -> f64 {
        let settle = (fs * 0.35) as usize;
        let cycles = (0.25 * freq_hz).round().max(6.0);
        let measure = (cycles * fs / freq_hz).round() as usize;
        let mut in_sq = 0.0_f64;
        let mut out_sq = 0.0_f64;
        for n in 0..(settle + measure) {
            let x = (TAU * freq_hz * (n as f64) / fs).sin();
            let y = f(x);
            if n >= settle {
                in_sq += x * x;
                out_sq += y * y;
            }
        }
        10.0 * ((out_sq + 1e-30) / (in_sq + 1e-30)).log10()
    }

    #[test]
    fn test_lr4_unity_magnitude_recombination() {
        let fs = 48_000.0;
        let fc = 180.0;
        let test_freqs = [
            20.0, 30.0, 50.0, 80.0, 120.0, 160.0, 180.0, 200.0, 250.0, 350.0, 500.0, 750.0,
            1000.0, 1500.0, 2400.0, 4000.0, 6000.0, 8000.0, 12000.0, 16000.0, 20000.0,
        ];

        for &f in &test_freqs {
            let mut xover = Lr4Crossover::new(fs, fc);
            let gain_db = measure_gain_db(fs, f, |x| {
                let (lo, hi) = xover.split(x);
                lo + hi
            });
            assert!(
                gain_db.abs() < 0.05,
                "LR4 sum not flat at {f} Hz: {gain_db:.5} dB"
            );
        }

        // At crossover frequency fc, low and high must each be -6.02 dB and strictly in-phase.
        let mut xover = Lr4Crossover::new(fs, fc);
        let settle = 24_000;
        let measure = 24_000;
        let mut lo_sq = 0.0;
        let mut hi_sq = 0.0;
        let mut dot = 0.0;
        let mut in_sq = 0.0;
        for n in 0..(settle + measure) {
            let x = (TAU * fc * (n as f64) / fs).sin();
            let (lo, hi) = xover.split(x);
            if n >= settle {
                in_sq += x * x;
                lo_sq += lo * lo;
                hi_sq += hi * hi;
                dot += lo * hi;
            }
        }
        let lo_db = 10.0 * (lo_sq / in_sq).log10();
        let hi_db = 10.0 * (hi_sq / in_sq).log10();
        let corr = dot / (lo_sq * hi_sq).sqrt();
        assert!((lo_db + 6.0206).abs() < 0.05, "LR4 low at fc = {lo_db:.3} dB");
        assert!((hi_db + 6.0206).abs() < 0.05, "LR4 high at fc = {hi_db:.3} dB");
        assert!(corr > 0.9999, "LR4 low/high not in-phase at fc: corr = {corr:.6}");
    }

    #[test]
    fn test_lr4_3way_with_allpass_match_flat() {
        let fs = 48_000.0;
        let f1 = 180.0;
        let f2 = 2400.0;
        let freqs = [
            25.0, 60.0, 120.0, 180.0, 300.0, 700.0, 1500.0, 2400.0, 4000.0, 9000.0, 18000.0,
        ];
        for &f in &freqs {
            let mut x1 = Lr4Crossover::new(fs, f1);
            let mut x2 = Lr4Crossover::new(fs, f2);
            let mut ap_match = AllpassMatch::new(fs, f2);
            let gain_db = measure_gain_db(fs, f, |x| {
                let (lo, mid_hi) = x1.split(x);
                let lo_aligned = ap_match.process(lo);
                let (mid, hi) = x2.split(mid_hi);
                lo_aligned + mid + hi
            });
            assert!(
                gain_db.abs() < 0.05,
                "3-way LR4 + AllpassMatch not flat at {f} Hz: {gain_db:.5} dB"
            );
        }
    }

    #[test]
    fn test_svf_rapid_cutoff_modulation_click_free() {
        let fs = 48_000.0;
        let mut hpf = CytomicSvf::new(fs);
        hpf.set_highpass(80.0, FRAC_1_SQRT_2);

        let total = 48_000;
        let mut prev = 0.0_f64;
        let mut max_abs = 0.0_f64;
        let mut max_step = 0.0_f64;

        for n in 0..total {
            let t = (n as f64) / fs;
            // Rapid 12 Hz logarithmic sweep between 65 Hz and 1600 Hz every sample.
            let lfo = 0.5 + 0.5 * (TAU * 12.0 * t).sin();
            let fc = 65.0 * (1600.0 / 65.0_f64).powf(lfo);
            hpf.set_cutoff_q_fast(fc, FRAC_1_SQRT_2);

            let x = (TAU * 440.0 * t).sin();
            let y = hpf.process(x);
            assert!(y.is_finite(), "SVF blew up under rapid modulation at sample {n}");
            max_abs = max_abs.max(y.abs());
            if n > 0 {
                max_step = max_step.max((y - prev).abs());
            }
            prev = y;
        }

        // For a 440 Hz unity sine at 48 kHz, max input step is 2*pi*440/48000 = 0.0576.
        // Smooth ZDF-SVF modulation must stay strictly bounded and click-free.
        assert!(max_abs < 1.5, "Modulated SVF overshot: max_abs = {max_abs:.4}");
        assert!(max_step < 0.20, "Click detected during rapid modulation: max_step = {max_step:.4}");
    }

    #[test]
    fn test_svf_all_filter_modes_accuracy() {
        let fs = 48_000.0;

        // Lowpass: -3.01 dB at fc, ~0 dB well below.
        let mut lp = CytomicSvf::new(fs);
        lp.set_lowpass(1000.0, FRAC_1_SQRT_2);
        let lp_fc = measure_gain_db(fs, 1000.0, |x| lp.process(x));
        lp.reset();
        let lp_lo = measure_gain_db(fs, 60.0, |x| lp.process(x));
        assert!((lp_fc + 3.0103).abs() < 0.08, "LP at fc = {lp_fc:.3} dB");
        assert!(lp_lo.abs() < 0.05, "LP passband = {lp_lo:.3} dB");

        // Highpass: -3.01 dB at fc, ~0 dB well above.
        let mut hp = CytomicSvf::new(fs);
        hp.set_highpass(1000.0, FRAC_1_SQRT_2);
        let hp_fc = measure_gain_db(fs, 1000.0, |x| hp.process(x));
        hp.reset();
        let hp_hi = measure_gain_db(fs, 12000.0, |x| hp.process(x));
        assert!((hp_fc + 3.0103).abs() < 0.08, "HP at fc = {hp_fc:.3} dB");
        assert!(hp_hi.abs() < 0.05, "HP passband = {hp_hi:.3} dB");

        // Bandpass: 0 dB peak at fc.
        let mut bp = CytomicSvf::new(fs);
        bp.set_bandpass(1000.0, 2.0);
        let bp_fc = measure_gain_db(fs, 1000.0, |x| bp.process(x));
        bp.reset();
        let bp_off = measure_gain_db(fs, 100.0, |x| bp.process(x));
        assert!(bp_fc.abs() < 0.05, "BP peak gain = {bp_fc:.3} dB");
        assert!(bp_off < -20.0, "BP stopband = {bp_off:.3} dB");

        // Notch: deep null at fc, 0 dB away.
        let mut notch = CytomicSvf::new(fs);
        notch.set_notch(1000.0, 4.0);
        let n_fc = measure_gain_db(fs, 1000.0, |x| notch.process(x));
        notch.reset();
        let n_off = measure_gain_db(fs, 100.0, |x| notch.process(x));
        assert!(n_fc < -60.0, "Notch depth = {n_fc:.3} dB");
        assert!(n_off.abs() < 0.05, "Notch passband = {n_off:.3} dB");

        // Bell: +6 dB and -6 dB at fc.
        let mut bell = CytomicSvf::new(fs);
        bell.set_bell(1000.0, 1.5, 6.0);
        let b_plus = measure_gain_db(fs, 1000.0, |x| bell.process(x));
        bell.reset();
        bell.set_bell(1000.0, 1.5, -6.0);
        let b_minus = measure_gain_db(fs, 1000.0, |x| bell.process(x));
        assert!((b_plus - 6.0).abs() < 0.08, "Bell +6 = {b_plus:.3} dB");
        assert!((b_minus + 6.0).abs() < 0.08, "Bell -6 = {b_minus:.3} dB");

        // Lowshelf: +6 dB at 40 Hz, +3 dB at 500 Hz, ~0 dB at 10 kHz.
        let mut ls = CytomicSvf::new(fs);
        ls.set_lowshelf(500.0, FRAC_1_SQRT_2, 6.0);
        let ls_lo = measure_gain_db(fs, 40.0, |x| ls.process(x));
        ls.reset();
        let ls_mid = measure_gain_db(fs, 500.0, |x| ls.process(x));
        ls.reset();
        let ls_hi = measure_gain_db(fs, 10000.0, |x| ls.process(x));
        assert!((ls_lo - 6.0).abs() < 0.15, "Lowshelf low = {ls_lo:.3} dB");
        assert!((ls_mid - 3.0).abs() < 0.10, "Lowshelf fc = {ls_mid:.3} dB");
        assert!(ls_hi.abs() < 0.10, "Lowshelf high = {ls_hi:.3} dB");

        // Highshelf: ~0 dB at 50 Hz, +3 dB at 2000 Hz, +6 dB at 16 kHz.
        let mut hs = CytomicSvf::new(fs);
        hs.set_highshelf(2000.0, FRAC_1_SQRT_2, 6.0);
        let hs_lo = measure_gain_db(fs, 50.0, |x| hs.process(x));
        hs.reset();
        let hs_mid = measure_gain_db(fs, 2000.0, |x| hs.process(x));
        hs.reset();
        let hs_hi = measure_gain_db(fs, 16000.0, |x| hs.process(x));
        assert!(hs_lo.abs() < 0.10, "Highshelf low = {hs_lo:.3} dB");
        assert!((hs_mid - 3.0).abs() < 0.10, "Highshelf fc = {hs_mid:.3} dB");
        assert!((hs_hi - 6.0).abs() < 0.15, "Highshelf high = {hs_hi:.3} dB");

        // Allpass: unity magnitude everywhere.
        let mut ap = CytomicSvf::new(fs);
        ap.set_allpass(800.0, FRAC_1_SQRT_2);
        for &f in &[50.0, 400.0, 800.0, 2000.0, 12000.0] {
            ap.reset();
            let g = measure_gain_db(fs, f, |x| ap.process(x));
            assert!(g.abs() < 0.05, "Allpass gain at {f} Hz = {g:.4} dB");
        }
    }

    #[test]
    fn test_svf_nan_and_extreme_bounds_safe() {
        let mut svf = CytomicSvf::new(48_000.0);
        svf.set_lowpass(f64::NAN, f64::INFINITY);
        svf.set_bell(-50.0, 0.0, 500.0);
        svf.set_highpass(1.0e9, -2.0);
        for &x in &[f64::NAN, f64::INFINITY, f64::NEG_INFINITY, 0.5, -0.5, 0.0] {
            let y = svf.process(x);
            assert!(y.is_finite(), "Non-finite output from SVF: {y}");
            let (lp, bp, hp) = svf.process_outputs(x);
            assert!(lp.is_finite() && bp.is_finite() && hp.is_finite());
        }
    }
}
