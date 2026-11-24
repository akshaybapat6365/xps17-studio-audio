//! Runtime AVX2/FMA dispatch layer, MXCSR FTZ/DAZ guard, scalar references,
//! and micro-benchmark harness for the Dell XPS 17 9720 Studio Audio DSP Engine.
//!
//! All five hot-path DSP kernels dispatch dynamically to the hand-tuned x86_64
//! AVX2/FMA GNU Assembly kernels in `asm/xps17_avx2_kernels.S` with zero heap
//! allocations and safe scalar fallbacks for non-AVX2 hosts.

use std::hint::black_box;
use std::time::Instant;

#[cfg(target_arch = "x86_64")]
extern "C" {
    fn xps17_avx2_fir_dot_f32(a: *const f32, b: *const f32, n: usize) -> f32;

    fn xps17_avx2_cmul_acc_f32(
        acc_re: *mut f32,
        acc_im: *mut f32,
        x_re: *const f32,
        x_im: *const f32,
        h_re: *const f32,
        h_im: *const f32,
        n: usize,
    );

    fn xps17_avx2_chebyshev_bass_f32(
        input: *const f32,
        output: *mut f32,
        n: usize,
        drive: f32,
        w2: f32,
        w3: f32,
        w4: f32,
    );

    fn xps17_avx2_true_peak_4x_f32(
        padded: *const f32,
        peaks_out: *mut f32,
        n: usize,
    ) -> f32;

    fn xps17_avx2_svf_bank8_f32(
        in8: *const f32,
        lp8: *mut f32,
        bp8: *mut f32,
        hp8: *mut f32,
        coeffs_a1_a2_a3_k: *const f32,
        state_ic1_ic2: *mut f32,
        frames: usize,
    );
}

/// Returns `true` if the host CPU supports both x86_64 AVX2 and FMA3 instructions.
#[inline]
pub fn has_avx2_fma() -> bool {
    #[cfg(target_arch = "x86_64")]
    {
        std::arch::is_x86_feature_detected!("avx2")
            && std::arch::is_x86_feature_detected!("fma")
    }
    #[cfg(not(target_arch = "x86_64"))]
    {
        false
    }
}

/// RAII guard enabling x86_64 MXCSR Flush-To-Zero (FTZ, bit 15 = `0x8000`) and
/// Denormals-Are-Zero (DAZ, bit 6 = `0x0040`) for hard-real-time audio callbacks,
/// restoring the caller's exact MXCSR state on drop.
pub struct FtzDazGuard {
    prev_mxcsr: u32,
}

impl FtzDazGuard {
    const FTZ_BIT: u32 = 0x8000;
    const DAZ_BIT: u32 = 0x0040;

    /// Saves the current MXCSR register and enables FTZ + DAZ.
    #[inline]
    pub fn enter() -> Self {
        #[cfg(target_arch = "x86_64")]
        {
            let prev = read_mxcsr();
            write_mxcsr(prev | Self::FTZ_BIT | Self::DAZ_BIT);
            Self { prev_mxcsr: prev }
        }
        #[cfg(not(target_arch = "x86_64"))]
        {
            Self { prev_mxcsr: 0 }
        }
    }
}

impl Drop for FtzDazGuard {
    #[inline]
    fn drop(&mut self) {
        #[cfg(target_arch = "x86_64")]
        {
            write_mxcsr(self.prev_mxcsr);
        }
    }
}

#[cfg(target_arch = "x86_64")]
#[inline(always)]
fn read_mxcsr() -> u32 {
    let mut mxcsr: u32 = 0;
    // SAFETY: `stmxcsr` writes the 32-bit SSE/AVX control-status register to `&mut mxcsr`.
    // Omitting `readonly`/`nomem` prevents the compiler from reordering FP ops across it.
    unsafe {
        core::arch::asm!(
            "stmxcsr [{0}]",
            in(reg) &mut mxcsr,
            options(nostack, preserves_flags),
        );
    }
    mxcsr
}

#[cfg(target_arch = "x86_64")]
#[inline(always)]
fn write_mxcsr(mxcsr: u32) {
    // SAFETY: `ldmxcsr` loads a valid 32-bit MXCSR control word.
    // Omitting `readonly`/`nomem` acts as an FP reordering barrier around the guard scope.
    unsafe {
        core::arch::asm!(
            "ldmxcsr [{0}]",
            in(reg) &mxcsr,
            options(nostack, preserves_flags),
        );
    }
}

#[cfg(target_arch = "x86_64")]
#[inline(always)]
fn no_vectorize_f32(mut v: f32) -> f32 {
    // SAFETY: Empty inline assembly on an `xmm` register emits zero instructions
    // and zero stack traffic while keeping scalar reference loops genuinely scalar.
    unsafe {
        core::arch::asm!(
            "/* {0} */",
            inlateout(xmm_reg) v,
            options(pure, nomem, nostack, preserves_flags),
        );
    }
    v
}

#[cfg(not(target_arch = "x86_64"))]
#[inline(always)]
fn no_vectorize_f32(v: f32) -> f32 {
    v
}

// ============================================================================
// 1. FIR Dot Product
// ============================================================================

/// Computes the dot product `sum_i a[i] * b[i]` over `0..a.len().min(b.len())`
/// using the 4x-unrolled AVX2/FMA assembly kernel when available.
#[inline]
pub fn fir_dot_f32(a: &[f32], b: &[f32]) -> f32 {
    let n = a.len().min(b.len());
    if n == 0 {
        return 0.0;
    }
    #[cfg(target_arch = "x86_64")]
    {
        if has_avx2_fma() {
            // SAFETY: Both slices have at least `n` readable `f32` elements and AVX2+FMA is verified.
            return unsafe { xps17_avx2_fir_dot_f32(a.as_ptr(), b.as_ptr(), n) };
        }
    }
    fir_dot_scalar_f32(a, b)
}

/// Scalar reference implementation of the FIR dot product.
#[inline(never)]
pub fn fir_dot_scalar_f32(a: &[f32], b: &[f32]) -> f32 {
    let n = a.len().min(b.len());
    let mut acc = 0.0f32;
    for i in 0..n {
        acc += no_vectorize_f32(a[i]) * b[i];
    }
    acc
}

// ============================================================================
// 2. Partitioned-FFT Complex Multiply-Accumulate
// ============================================================================

/// Complex multiply-accumulate for partitioned overlap-save FFT convolution:
/// - `acc_re[k] += x_re[k] * h_re[k] - x_im[k] * h_im[k]`
/// - `acc_im[k] += x_re[k] * h_im[k] + x_im[k] * h_re[k]`
#[inline]
pub fn cmul_acc_f32(
    acc_re: &mut [f32],
    acc_im: &mut [f32],
    x_re: &[f32],
    x_im: &[f32],
    h_re: &[f32],
    h_im: &[f32],
) {
    let n = acc_re
        .len()
        .min(acc_im.len())
        .min(x_re.len())
        .min(x_im.len())
        .min(h_re.len())
        .min(h_im.len());
    if n == 0 {
        return;
    }
    #[cfg(target_arch = "x86_64")]
    {
        if has_avx2_fma() {
            // SAFETY: All 6 slices have length >= `n`, `acc_re` and `acc_im` are exclusively
            // borrowed (`&mut`), and AVX2+FMA support has been verified at runtime.
            unsafe {
                xps17_avx2_cmul_acc_f32(
                    acc_re.as_mut_ptr(),
                    acc_im.as_mut_ptr(),
                    x_re.as_ptr(),
                    x_im.as_ptr(),
                    h_re.as_ptr(),
                    h_im.as_ptr(),
                    n,
                );
            }
            return;
        }
    }
    cmul_acc_scalar_f32(acc_re, acc_im, x_re, x_im, h_re, h_im);
}

/// Scalar reference implementation of complex multiply-accumulate.
#[inline(never)]
pub fn cmul_acc_scalar_f32(
    acc_re: &mut [f32],
    acc_im: &mut [f32],
    x_re: &[f32],
    x_im: &[f32],
    h_re: &[f32],
    h_im: &[f32],
) {
    let n = acc_re
        .len()
        .min(acc_im.len())
        .min(x_re.len())
        .min(x_im.len())
        .min(h_re.len())
        .min(h_im.len());
    for k in 0..n {
        let xr = no_vectorize_f32(x_re[k]);
        let xi = x_im[k];
        let hr = h_re[k];
        let hi = h_im[k];
        acc_re[k] += xr * hr - xi * hi;
        acc_im[k] += xr * hi + xi * hr;
    }
}

// ============================================================================
// 3. Zero-DC Chebyshev Missing-Fundamental Bass Synthesizer
// ============================================================================

/// Synthesizes zero-DC 2nd/3rd/4th Chebyshev harmonics on algebraically soft-clipped
/// input `s = u / sqrt(1 + u^2)` where `u = input[i] * drive`:
/// - `t2_ac = 2 * s^2`
/// - `t3    = s * (4 * s^2 - 3)`
/// - `t4_ac = s^2 * (8 * s^2 - 8)`
/// - `output[i] = w2 * t2_ac + w3 * t3 + w4 * t4_ac`
#[inline]
pub fn chebyshev_bass_f32(
    input: &[f32],
    output: &mut [f32],
    drive: f32,
    w2: f32,
    w3: f32,
    w4: f32,
) {
    let n = input.len().min(output.len());
    if n == 0 {
        return;
    }
    #[cfg(target_arch = "x86_64")]
    {
        if has_avx2_fma() {
            // SAFETY: Both slices have length >= `n`, `output` is uniquely borrowed,
            // and AVX2+FMA support has been verified.
            unsafe {
                xps17_avx2_chebyshev_bass_f32(
                    input.as_ptr(),
                    output.as_mut_ptr(),
                    n,
                    drive,
                    w2,
                    w3,
                    w4,
                );
            }
            return;
        }
    }
    chebyshev_bass_scalar_f32(input, output, drive, w2, w3, w4);
}

/// Scalar reference implementation of the zero-DC Chebyshev harmonic generator.
#[inline(never)]
pub fn chebyshev_bass_scalar_f32(
    input: &[f32],
    output: &mut [f32],
    drive: f32,
    w2: f32,
    w3: f32,
    w4: f32,
) {
    let n = input.len().min(output.len());
    for i in 0..n {
        let u = no_vectorize_f32(input[i]) * drive;
        let s = u / (1.0f32 + u * u).sqrt();
        let s2 = s * s;
        let t2_ac = 2.0f32 * s2;
        let t3 = s * (4.0f32 * s2 - 3.0f32);
        let t4_ac = s2 * (8.0f32 * s2 - 8.0f32);
        output[i] = w2 * t2_ac + w3 * t3 + w4 * t4_ac;
    }
}

// ============================================================================
// 4. 4x Oversampled Catmull-Rom True-Peak Detector
// ============================================================================

/// Computes per-sample 4x Catmull-Rom inter-sample true peaks for `i in 0..n`
/// where `n = peaks_out.len().min(padded.len().saturating_sub(3))`, storing each
/// sample's peak into `peaks_out[i]` and returning the overall maximum peak.
#[inline]
pub fn true_peak_4x_f32(padded: &[f32], peaks_out: &mut [f32]) -> f32 {
    if padded.len() < 4 || peaks_out.is_empty() {
        return 0.0;
    }
    let n = peaks_out.len().min(padded.len() - 3);
    #[cfg(target_arch = "x86_64")]
    {
        if has_avx2_fma() {
            // SAFETY: `padded.len() >= n + 3` and `peaks_out.len() >= n` are guaranteed above,
            // and AVX2+FMA support has been verified.
            return unsafe {
                xps17_avx2_true_peak_4x_f32(padded.as_ptr(), peaks_out.as_mut_ptr(), n)
            };
        }
    }
    true_peak_4x_scalar_f32(padded, peaks_out)
}

/// Scalar reference implementation of the 4x Catmull-Rom true-peak detector.
#[inline(never)]
pub fn true_peak_4x_scalar_f32(padded: &[f32], peaks_out: &mut [f32]) -> f32 {
    if padded.len() < 4 || peaks_out.is_empty() {
        return 0.0;
    }
    let n = peaks_out.len().min(padded.len() - 3);
    let mut max_peak = 0.0f32;
    for i in 0..n {
        let p0 = no_vectorize_f32(padded[i]);
        let p1 = padded[i + 1];
        let p2 = padded[i + 2];
        let p3 = padded[i + 3];

        let y0 = p1;
        let y1 = -0.0703125 * p0 + 0.8671875 * p1 + 0.2265625 * p2 - 0.0234375 * p3;
        let y2 = -0.0625 * p0 + 0.5625 * p1 + 0.5625 * p2 - 0.0625 * p3;
        let y3 = -0.0234375 * p0 + 0.2265625 * p1 + 0.8671875 * p2 - 0.0703125 * p3;

        let peak = y0.abs().max(y1.abs()).max(y2.abs()).max(y3.abs());
        peaks_out[i] = peak;
        if peak > max_peak {
            max_peak = peak;
        }
    }
    max_peak
}

// ============================================================================
// 5. 8-Channel Parallel Cytomic ZDF State-Variable Filter Bank
// ============================================================================

/// Processes 8 parallel Cytomic Zero-Delay-Feedback State-Variable Filters
/// across `frames = in8.len().min(lp8.len()).min(bp8.len()).min(hp8.len())`.
///
/// - `coeffs`: `[a1, a2, a3, k]`, each an 8-lane array.
/// - `state`: `[ic1eq, ic2eq]`, updated in-place on completion.
#[inline]
pub fn svf_bank8_f32(
    in8: &[[f32; 8]],
    lp8: &mut [[f32; 8]],
    bp8: &mut [[f32; 8]],
    hp8: &mut [[f32; 8]],
    coeffs: &[[f32; 8]; 4],
    state: &mut [[f32; 8]; 2],
) {
    let frames = in8
        .len()
        .min(lp8.len())
        .min(bp8.len())
        .min(hp8.len());
    if frames == 0 {
        return;
    }
    #[cfg(target_arch = "x86_64")]
    {
        if has_avx2_fma() {
            // SAFETY: `[f32; 8]` has identical contiguous layout to 8 `f32`s with no padding;
            // `coeffs` holds 32 contiguous `f32`s, `state` holds 16 contiguous `f32`s,
            // and every slice has at least `frames` 8-float entries.
            unsafe {
                xps17_avx2_svf_bank8_f32(
                    in8.as_ptr() as *const f32,
                    lp8.as_mut_ptr() as *mut f32,
                    bp8.as_mut_ptr() as *mut f32,
                    hp8.as_mut_ptr() as *mut f32,
                    coeffs.as_ptr() as *const f32,
                    state.as_mut_ptr() as *mut f32,
                    frames,
                );
            }
            return;
        }
    }
    svf_bank8_scalar_f32(in8, lp8, bp8, hp8, coeffs, state);
}

/// Scalar reference implementation of the 8-channel Cytomic ZDF SVF bank.
#[inline(never)]
pub fn svf_bank8_scalar_f32(
    in8: &[[f32; 8]],
    lp8: &mut [[f32; 8]],
    bp8: &mut [[f32; 8]],
    hp8: &mut [[f32; 8]],
    coeffs: &[[f32; 8]; 4],
    state: &mut [[f32; 8]; 2],
) {
    let frames = in8
        .len()
        .min(lp8.len())
        .min(bp8.len())
        .min(hp8.len());
    for ch in 0..8 {
        let a1 = coeffs[0][ch];
        let a2 = coeffs[1][ch];
        let a3 = coeffs[2][ch];
        let k = coeffs[3][ch];
        let mut ic1 = state[0][ch];
        let mut ic2 = state[1][ch];
        for t in 0..frames {
            let v0 = no_vectorize_f32(in8[t][ch]);
            let v3 = v0 - ic2;
            let v1 = a1 * ic1 + a2 * v3;
            let v2 = ic2 + a2 * ic1 + a3 * v3;
            ic1 = 2.0 * v1 - ic1;
            ic2 = 2.0 * v2 - ic2;
            let hp = v0 - k * v1 - v2;
            lp8[t][ch] = v2;
            bp8[t][ch] = v1;
            hp8[t][ch] = hp;
        }
        state[0][ch] = ic1;
        state[1][ch] = ic2;
    }
}

// ============================================================================
// Offline SIMD vs Scalar Benchmark Suite
// ============================================================================

/// Measured performance and numerical parity for a single SIMD kernel.
#[derive(Debug, Clone)]
pub struct KernelBenchResult {
    pub name: &'static str,
    pub avx2_ns: f64,
    pub scalar_ns: f64,
    pub speedup: f64,
    pub max_abs_err: f32,
}

/// Deterministic xorshift64* PRNG producing floats in `[-1.0, 1.0]`.
fn gen_signal(len: usize, seed: u64) -> Vec<f32> {
    let mut s = seed.max(1);
    let mut out = Vec::with_capacity(len);
    for _ in 0..len {
        s ^= s >> 12;
        s ^= s << 25;
        s ^= s >> 27;
        let v = s.wrapping_mul(0x2545_f491_4f6c_dd1d);
        let unit = ((v >> 40) as f32) * (1.0 / 16_777_216.0);
        out.push(unit * 2.0 - 1.0);
    }
    out
}

/// Computes Cytomic ZDF SVF coefficients `(a1, a2, a3, k)` for cutoff `fc` and damping `q`.
fn make_svf_coeffs(freqs: [f32; 8], qs: [f32; 8], sample_rate: f32) -> [[f32; 8]; 4] {
    let mut c = [[0.0f32; 8]; 4];
    for ch in 0..8 {
        let g = (std::f32::consts::PI * freqs[ch] / sample_rate).tan();
        let k = 1.0 / qs[ch];
        let a1 = 1.0 / (1.0 + g * (g + k));
        let a2 = g * a1;
        let a3 = g * a2;
        c[0][ch] = a1;
        c[1][ch] = a2;
        c[2][ch] = a3;
        c[3][ch] = k;
    }
    c
}

/// Benchmarks all 5 hand-tuned AVX2/FMA kernels against their scalar references
/// and computes exact numerical error on realistic XPS 17 studio DSP block sizes.
pub fn run_simd_benchmarks() -> Vec<KernelBenchResult> {
    let _guard = FtzDazGuard::enter();
    let mut results = Vec::with_capacity(5);

    // 1. FIR dot product (n = 1024 taps)
    {
        let n = 1024;
        let a = gen_signal(n, 0x101);
        let b = gen_signal(n, 0x202);
        let ref_val = fir_dot_scalar_f32(&a, &b);
        let avx_val = fir_dot_f32(&a, &b);
        let max_abs_err = (avx_val - ref_val).abs();

        let iters: u32 = 40_000;
        for _ in 0..2_000 {
            black_box(fir_dot_f32(black_box(&a), black_box(&b)));
            black_box(fir_dot_scalar_f32(black_box(&a), black_box(&b)));
        }

        let t0 = Instant::now();
        for _ in 0..iters {
            black_box(fir_dot_f32(black_box(&a), black_box(&b)));
        }
        let avx2_ns = t0.elapsed().as_nanos() as f64 / iters as f64;

        let t1 = Instant::now();
        for _ in 0..iters {
            black_box(fir_dot_scalar_f32(black_box(&a), black_box(&b)));
        }
        let scalar_ns = t1.elapsed().as_nanos() as f64 / iters as f64;

        results.push(KernelBenchResult {
            name: "fir_dot_f32 (n=1024)",
            avx2_ns,
            scalar_ns,
            speedup: scalar_ns / avx2_ns.max(1e-9),
            max_abs_err,
        });
    }

    // 2. Partitioned FFT complex multiply-accumulate (n = 129 bins, 256-pt R2C FFT)
    {
        let n = 129;
        let x_re: Vec<f32> = gen_signal(n, 0x303).into_iter().map(|v| v * 0.01).collect();
        let x_im: Vec<f32> = gen_signal(n, 0x404).into_iter().map(|v| v * 0.01).collect();
        let h_re: Vec<f32> = gen_signal(n, 0x505).into_iter().map(|v| v * 0.01).collect();
        let h_im: Vec<f32> = gen_signal(n, 0x606).into_iter().map(|v| v * 0.01).collect();
        let init_re = gen_signal(n, 0x707);
        let init_im = gen_signal(n, 0x808);

        let mut acc_re_avx = init_re.clone();
        let mut acc_im_avx = init_im.clone();
        let mut acc_re_ref = init_re;
        let mut acc_im_ref = init_im;

        cmul_acc_f32(&mut acc_re_avx, &mut acc_im_avx, &x_re, &x_im, &h_re, &h_im);
        cmul_acc_scalar_f32(&mut acc_re_ref, &mut acc_im_ref, &x_re, &x_im, &h_re, &h_im);
        let max_abs_err = acc_re_avx
            .iter()
            .zip(acc_re_ref.iter())
            .chain(acc_im_avx.iter().zip(acc_im_ref.iter()))
            .map(|(a, b)| (a - b).abs())
            .fold(0.0f32, f32::max);

        let iters: u32 = 100_000;
        for _ in 0..5_000 {
            cmul_acc_f32(
                black_box(&mut acc_re_avx),
                black_box(&mut acc_im_avx),
                black_box(&x_re),
                black_box(&x_im),
                black_box(&h_re),
                black_box(&h_im),
            );
            cmul_acc_scalar_f32(
                black_box(&mut acc_re_ref),
                black_box(&mut acc_im_ref),
                black_box(&x_re),
                black_box(&x_im),
                black_box(&h_re),
                black_box(&h_im),
            );
        }

        acc_re_avx.fill(0.0);
        acc_im_avx.fill(0.0);
        let t0 = Instant::now();
        for _ in 0..iters {
            cmul_acc_f32(
                black_box(&mut acc_re_avx),
                black_box(&mut acc_im_avx),
                black_box(&x_re),
                black_box(&x_im),
                black_box(&h_re),
                black_box(&h_im),
            );
        }
        black_box(&acc_re_avx);
        black_box(&acc_im_avx);
        let avx2_ns = t0.elapsed().as_nanos() as f64 / iters as f64;

        acc_re_ref.fill(0.0);
        acc_im_ref.fill(0.0);
        let t1 = Instant::now();
        for _ in 0..iters {
            cmul_acc_scalar_f32(
                black_box(&mut acc_re_ref),
                black_box(&mut acc_im_ref),
                black_box(&x_re),
                black_box(&x_im),
                black_box(&h_re),
                black_box(&h_im),
            );
        }
        black_box(&acc_re_ref);
        black_box(&acc_im_ref);
        let scalar_ns = t1.elapsed().as_nanos() as f64 / iters as f64;

        results.push(KernelBenchResult {
            name: "cmul_acc_f32 (n=129)",
            avx2_ns,
            scalar_ns,
            speedup: scalar_ns / avx2_ns.max(1e-9),
            max_abs_err,
        });
    }

    // 3. Chebyshev bass harmonic synthesizer (n = 256 samples)
    {
        let n = 256;
        let input = gen_signal(n, 0x909);
        let mut out_avx = vec![0.0f32; n];
        let mut out_ref = vec![0.0f32; n];
        let (drive, w2, w3, w4) = (2.8f32, 0.55f32, 0.28f32, 0.12f32);

        chebyshev_bass_f32(&input, &mut out_avx, drive, w2, w3, w4);
        chebyshev_bass_scalar_f32(&input, &mut out_ref, drive, w2, w3, w4);
        let max_abs_err = out_avx
            .iter()
            .zip(out_ref.iter())
            .map(|(a, b)| (a - b).abs())
            .fold(0.0f32, f32::max);

        let iters: u32 = 40_000;
        for _ in 0..2_000 {
            chebyshev_bass_f32(black_box(&input), black_box(&mut out_avx), drive, w2, w3, w4);
            chebyshev_bass_scalar_f32(
                black_box(&input),
                black_box(&mut out_ref),
                drive,
                w2,
                w3,
                w4,
            );
        }

        let t0 = Instant::now();
        for _ in 0..iters {
            chebyshev_bass_f32(black_box(&input), black_box(&mut out_avx), drive, w2, w3, w4);
        }
        black_box(&out_avx);
        let avx2_ns = t0.elapsed().as_nanos() as f64 / iters as f64;

        let t1 = Instant::now();
        for _ in 0..iters {
            chebyshev_bass_scalar_f32(
                black_box(&input),
                black_box(&mut out_ref),
                drive,
                w2,
                w3,
                w4,
            );
        }
        black_box(&out_ref);
        let scalar_ns = t1.elapsed().as_nanos() as f64 / iters as f64;

        results.push(KernelBenchResult {
            name: "chebyshev_bass_f32 (n=256)",
            avx2_ns,
            scalar_ns,
            speedup: scalar_ns / avx2_ns.max(1e-9),
            max_abs_err,
        });
    }

    // 4. 4x Catmull-Rom true-peak detector (n = 256 samples)
    {
        let n = 256;
        let padded = gen_signal(n + 3, 0xa0a);
        let mut peaks_avx = vec![0.0f32; n];
        let mut peaks_ref = vec![0.0f32; n];

        let pmax_avx = true_peak_4x_f32(&padded, &mut peaks_avx);
        let pmax_ref = true_peak_4x_scalar_f32(&padded, &mut peaks_ref);
        let max_abs_err = peaks_avx
            .iter()
            .zip(peaks_ref.iter())
            .map(|(a, b)| (a - b).abs())
            .fold((pmax_avx - pmax_ref).abs(), f32::max);

        let iters: u32 = 60_000;
        for _ in 0..3_000 {
            black_box(true_peak_4x_f32(black_box(&padded), black_box(&mut peaks_avx)));
            black_box(true_peak_4x_scalar_f32(
                black_box(&padded),
                black_box(&mut peaks_ref),
            ));
        }

        let t0 = Instant::now();
        for _ in 0..iters {
            black_box(true_peak_4x_f32(black_box(&padded), black_box(&mut peaks_avx)));
        }
        let avx2_ns = t0.elapsed().as_nanos() as f64 / iters as f64;

        let t1 = Instant::now();
        for _ in 0..iters {
            black_box(true_peak_4x_scalar_f32(
                black_box(&padded),
                black_box(&mut peaks_ref),
            ));
        }
        let scalar_ns = t1.elapsed().as_nanos() as f64 / iters as f64;

        results.push(KernelBenchResult {
            name: "true_peak_4x_f32 (n=256)",
            avx2_ns,
            scalar_ns,
            speedup: scalar_ns / avx2_ns.max(1e-9),
            max_abs_err,
        });
    }

    // 5. 8-channel Cytomic ZDF SVF bank (frames = 256)
    {
        let frames = 256;
        let raw = gen_signal(frames * 8, 0xb0b);
        let in8: Vec<[f32; 8]> = raw
            .chunks_exact(8)
            .map(|c| [c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7]])
            .collect();
        let coeffs = make_svf_coeffs(
            [82.0, 140.0, 260.0, 620.0, 1500.0, 3200.0, 6400.0, 10500.0],
            [0.707, 0.707, 0.707, 0.85, 1.0, 1.2, 0.707, 0.707],
            48_000.0,
        );

        let mut lp_avx = vec![[0.0f32; 8]; frames];
        let mut bp_avx = vec![[0.0f32; 8]; frames];
        let mut hp_avx = vec![[0.0f32; 8]; frames];
        let mut st_avx = [[0.0f32; 8]; 2];

        let mut lp_ref = vec![[0.0f32; 8]; frames];
        let mut bp_ref = vec![[0.0f32; 8]; frames];
        let mut hp_ref = vec![[0.0f32; 8]; frames];
        let mut st_ref = [[0.0f32; 8]; 2];

        svf_bank8_f32(&in8, &mut lp_avx, &mut bp_avx, &mut hp_avx, &coeffs, &mut st_avx);
        svf_bank8_scalar_f32(&in8, &mut lp_ref, &mut bp_ref, &mut hp_ref, &coeffs, &mut st_ref);

        let mut max_abs_err = 0.0f32;
        for t in 0..frames {
            for ch in 0..8 {
                max_abs_err = max_abs_err
                    .max((lp_avx[t][ch] - lp_ref[t][ch]).abs())
                    .max((bp_avx[t][ch] - bp_ref[t][ch]).abs())
                    .max((hp_avx[t][ch] - hp_ref[t][ch]).abs());
            }
        }
        for s in 0..2 {
            for ch in 0..8 {
                max_abs_err = max_abs_err.max((st_avx[s][ch] - st_ref[s][ch]).abs());
            }
        }

        let iters: u32 = 15_000;
        for _ in 0..1_000 {
            svf_bank8_f32(
                black_box(&in8),
                black_box(&mut lp_avx),
                black_box(&mut bp_avx),
                black_box(&mut hp_avx),
                black_box(&coeffs),
                black_box(&mut st_avx),
            );
            svf_bank8_scalar_f32(
                black_box(&in8),
                black_box(&mut lp_ref),
                black_box(&mut bp_ref),
                black_box(&mut hp_ref),
                black_box(&coeffs),
                black_box(&mut st_ref),
            );
        }

        let t0 = Instant::now();
        for _ in 0..iters {
            svf_bank8_f32(
                black_box(&in8),
                black_box(&mut lp_avx),
                black_box(&mut bp_avx),
                black_box(&mut hp_avx),
                black_box(&coeffs),
                black_box(&mut st_avx),
            );
        }
        black_box(&st_avx);
        let avx2_ns = t0.elapsed().as_nanos() as f64 / iters as f64;

        let t1 = Instant::now();
        for _ in 0..iters {
            svf_bank8_scalar_f32(
                black_box(&in8),
                black_box(&mut lp_ref),
                black_box(&mut bp_ref),
                black_box(&mut hp_ref),
                black_box(&coeffs),
                black_box(&mut st_ref),
            );
        }
        black_box(&st_ref);
        let scalar_ns = t1.elapsed().as_nanos() as f64 / iters as f64;

        results.push(KernelBenchResult {
            name: "svf_bank8_f32 (256x8)",
            avx2_ns,
            scalar_ns,
            speedup: scalar_ns / avx2_ns.max(1e-9),
            max_abs_err,
        });
    }

    results
}

#[cfg(test)]
mod tests {
    use super::*;

    const EDGE_LENS: &[usize] = &[0, 1, 2, 7, 8, 9, 15, 16, 17, 31, 32, 33, 63, 64, 65, 129, 513];

    #[test]
    fn test_ftz_daz_guard() {
        #[cfg(target_arch = "x86_64")]
        {
            let before = read_mxcsr();
            {
                let _guard = FtzDazGuard::enter();
                let inside = read_mxcsr();
                assert_eq!(
                    inside & (FtzDazGuard::FTZ_BIT | FtzDazGuard::DAZ_BIT),
                    FtzDazGuard::FTZ_BIT | FtzDazGuard::DAZ_BIT,
                    "FTZ and DAZ bits must be set inside guard"
                );
                let subnormal = black_box(f32::from_bits(1)); // ~1.4e-45
                let flushed = black_box(subnormal) * black_box(1.0f32);
                assert_eq!(flushed, 0.0, "subnormal must flush to zero under FTZ+DAZ");
            }
            let after = read_mxcsr();
            assert_eq!(after, before, "MXCSR must be restored identically on Drop");
        }
    }

    #[test]
    fn test_fir_dot_accuracy_and_edges() {
        for &n in EDGE_LENS {
            let a = gen_signal(n + 5, 0x1111 + n as u64);
            let b = gen_signal(n + 7, 0x2222 + n as u64);
            // Exercise unaligned starting offsets as well as base alignment
            for off in 0..4 {
                let sa = &a[off..off + n];
                let sb = &b[off + 1..off + 1 + n];
                let got = fir_dot_f32(sa, sb);
                let expected = fir_dot_scalar_f32(sa, sb);
                let err = (got - expected).abs();
                assert!(
                    err < 1e-5,
                    "fir_dot mismatch n={n} off={off}: got={got}, expected={expected}, err={err}"
                );
            }
        }

        // Denormal & full-scale checks
        let denorm_a = vec![1e-40f32; 67];
        let denorm_b = vec![-1e-40f32; 67];
        assert!((fir_dot_f32(&denorm_a, &denorm_b) - fir_dot_scalar_f32(&denorm_a, &denorm_b)).abs() < 1e-5);

        let full_a = vec![1.0f32; 513];
        let full_b: Vec<f32> = (0..513).map(|i| if i % 2 == 0 { 1.0 } else { -1.0 }).collect();
        let got = fir_dot_f32(&full_a, &full_b);
        assert!((got - 1.0).abs() < 1e-5, "alternating full-scale dot expected 1.0, got {got}");
    }

    #[test]
    fn test_cmul_acc_accuracy_and_edges() {
        for &n in EDGE_LENS {
            let x_re = gen_signal(n + 3, 0x3000 + n as u64);
            let x_im = gen_signal(n + 3, 0x3100 + n as u64);
            let h_re = gen_signal(n + 3, 0x3200 + n as u64);
            let h_im = gen_signal(n + 3, 0x3300 + n as u64);
            let init_re = gen_signal(n + 3, 0x3400 + n as u64);
            let init_im = gen_signal(n + 3, 0x3500 + n as u64);

            for off in [0usize, 1, 3] {
                let mut re_avx = init_re[off..off + n].to_vec();
                let mut im_avx = init_im[off..off + n].to_vec();
                let mut re_ref = re_avx.clone();
                let mut im_ref = im_avx.clone();

                cmul_acc_f32(
                    &mut re_avx,
                    &mut im_avx,
                    &x_re[off..off + n],
                    &x_im[off..off + n],
                    &h_re[off..off + n],
                    &h_im[off..off + n],
                );
                cmul_acc_scalar_f32(
                    &mut re_ref,
                    &mut im_ref,
                    &x_re[off..off + n],
                    &x_im[off..off + n],
                    &h_re[off..off + n],
                    &h_im[off..off + n],
                );

                for k in 0..n {
                    let e_re = (re_avx[k] - re_ref[k]).abs();
                    let e_im = (im_avx[k] - im_ref[k]).abs();
                    assert!(
                        e_re < 1e-5 && e_im < 1e-5,
                        "cmul_acc mismatch n={n} off={off} k={k}: e_re={e_re} e_im={e_im}"
                    );
                }
            }
        }

        // Denormal & full-scale test
        let mut re_avx = vec![1e-40f32; 129];
        let mut im_avx = vec![-1e-41f32; 129];
        let mut re_ref = re_avx.clone();
        let mut im_ref = im_avx.clone();
        let xr = vec![1.0f32; 129];
        let xi = vec![-1.0f32; 129];
        let hr = vec![1e-40f32; 129];
        let hi = vec![-1e-40f32; 129];
        cmul_acc_f32(&mut re_avx, &mut im_avx, &xr, &xi, &hr, &hi);
        cmul_acc_scalar_f32(&mut re_ref, &mut im_ref, &xr, &xi, &hr, &hi);
        for k in 0..129 {
            assert!((re_avx[k] - re_ref[k]).abs() < 1e-5);
            assert!((im_avx[k] - im_ref[k]).abs() < 1e-5);
        }
    }

    #[test]
    fn test_chebyshev_bass_accuracy_and_edges() {
        let (drive, w2, w3, w4) = (3.2f32, 0.6f32, 0.35f32, 0.15f32);
        for &n in EDGE_LENS {
            let inp = gen_signal(n + 3, 0x4000 + n as u64);
            for off in [0usize, 1, 2] {
                let mut out_avx = vec![99.0f32; n];
                let mut out_ref = vec![99.0f32; n];
                chebyshev_bass_f32(&inp[off..off + n], &mut out_avx, drive, w2, w3, w4);
                chebyshev_bass_scalar_f32(&inp[off..off + n], &mut out_ref, drive, w2, w3, w4);
                for i in 0..n {
                    let err = (out_avx[i] - out_ref[i]).abs();
                    assert!(
                        err < 1e-5,
                        "chebyshev_bass mismatch n={n} off={off} i={i}: got={} ref={} err={err}",
                        out_avx[i],
                        out_ref[i]
                    );
                }
            }
        }

        // Zero-DC property at silence + denormal + full-scale (+/-4.0)
        let special = [
            0.0f32, -0.0, 1e-42, -1e-40, 1e-20, -1e-20, 0.5, -0.5, 1.0, -1.0, 4.0, -4.0, 0.0,
            0.25, -0.25, 0.75, -0.75,
        ];
        let mut out_avx = vec![0.0f32; special.len()];
        let mut out_ref = vec![0.0f32; special.len()];
        chebyshev_bass_f32(&special, &mut out_avx, drive, w2, w3, w4);
        chebyshev_bass_scalar_f32(&special, &mut out_ref, drive, w2, w3, w4);
        assert_eq!(out_avx[0], 0.0, "output at 0.0 must have zero DC offset");
        for i in 0..special.len() {
            assert!((out_avx[i] - out_ref[i]).abs() < 1e-5);
        }
    }

    #[test]
    fn test_true_peak_4x_accuracy_and_edges() {
        for &n in EDGE_LENS {
            let padded = gen_signal(n + 6, 0x5000 + n as u64);
            for off in [0usize, 1, 3] {
                let slice = &padded[off..off + n + 3];
                let mut peaks_avx = vec![0.0f32; n];
                let mut peaks_ref = vec![0.0f32; n];
                let max_avx = true_peak_4x_f32(slice, &mut peaks_avx);
                let max_ref = true_peak_4x_scalar_f32(slice, &mut peaks_ref);
                assert!(
                    (max_avx - max_ref).abs() < 1e-5,
                    "true_peak_4x max mismatch n={n} off={off}: got={max_avx} ref={max_ref}"
                );
                for i in 0..n {
                    let err = (peaks_avx[i] - peaks_ref[i]).abs();
                    assert!(err < 1e-5, "true_peak_4x sample mismatch n={n} i={i}: err={err}");
                }
            }
        }

        // Classic inter-sample peak > 0 dBFS: [-1, +1, +1, -1] => midpoint y2 = 1.25
        let isp = [-1.0f32, 1.0, 1.0, -1.0];
        let mut peak_out = [0.0f32; 1];
        let max_p = true_peak_4x_f32(&isp, &mut peak_out);
        assert!(
            (max_p - 1.25).abs() < 1e-6 && (peak_out[0] - 1.25).abs() < 1e-6,
            "expected Catmull-Rom inter-sample peak 1.25, got {max_p}"
        );

        // Short padded guard
        let mut dummy = [0.0f32; 4];
        assert_eq!(true_peak_4x_f32(&[1.0, 2.0, 3.0], &mut dummy), 0.0);
    }

    #[test]
    fn test_svf_bank8_accuracy_and_edges() {
        let coeffs = make_svf_coeffs(
            [65.0, 110.0, 220.0, 500.0, 1200.0, 2800.0, 6160.0, 12000.0],
            [0.707, 1.414, 0.5, 2.0, 0.707, 1.5, 0.707, 0.9],
            48_000.0,
        );

        for &frames in EDGE_LENS {
            let raw = gen_signal(frames * 8, 0x6000 + frames as u64);
            let in8: Vec<[f32; 8]> = raw
                .chunks_exact(8)
                .map(|c| [c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7]])
                .collect();

            let mut lp_avx = vec![[0.0f32; 8]; frames];
            let mut bp_avx = vec![[0.0f32; 8]; frames];
            let mut hp_avx = vec![[0.0f32; 8]; frames];
            let mut st_avx = [[0.01f32; 8], [-0.02f32; 8]];

            let mut lp_ref = vec![[0.0f32; 8]; frames];
            let mut bp_ref = vec![[0.0f32; 8]; frames];
            let mut hp_ref = vec![[0.0f32; 8]; frames];
            let mut st_ref = st_avx;

            svf_bank8_f32(&in8, &mut lp_avx, &mut bp_avx, &mut hp_avx, &coeffs, &mut st_avx);
            svf_bank8_scalar_f32(&in8, &mut lp_ref, &mut bp_ref, &mut hp_ref, &coeffs, &mut st_ref);

            for t in 0..frames {
                for ch in 0..8 {
                    assert!((lp_avx[t][ch] - lp_ref[t][ch]).abs() < 1e-5);
                    assert!((bp_avx[t][ch] - bp_ref[t][ch]).abs() < 1e-5);
                    assert!((hp_avx[t][ch] - hp_ref[t][ch]).abs() < 1e-5);
                }
            }
            for s in 0..2 {
                for ch in 0..8 {
                    assert!((st_avx[s][ch] - st_ref[s][ch]).abs() < 1e-5);
                }
            }
        }

        // State continuity across split blocks (100 + 156 == 256)
        let raw = gen_signal(256 * 8, 0x7777);
        let in8: Vec<[f32; 8]> = raw
            .chunks_exact(8)
            .map(|c| [c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7]])
            .collect();
        let mut lp_all = vec![[0.0f32; 8]; 256];
        let mut bp_all = vec![[0.0f32; 8]; 256];
        let mut hp_all = vec![[0.0f32; 8]; 256];
        let mut st_all = [[0.0f32; 8]; 2];
        svf_bank8_f32(&in8, &mut lp_all, &mut bp_all, &mut hp_all, &coeffs, &mut st_all);

        let mut lp_sp = vec![[0.0f32; 8]; 256];
        let mut bp_sp = vec![[0.0f32; 8]; 256];
        let mut hp_sp = vec![[0.0f32; 8]; 256];
        let mut st_sp = [[0.0f32; 8]; 2];
        svf_bank8_f32(
            &in8[..100],
            &mut lp_sp[..100],
            &mut bp_sp[..100],
            &mut hp_sp[..100],
            &coeffs,
            &mut st_sp,
        );
        svf_bank8_f32(
            &in8[100..],
            &mut lp_sp[100..],
            &mut bp_sp[100..],
            &mut hp_sp[100..],
            &coeffs,
            &mut st_sp,
        );
        for t in 0..256 {
            for ch in 0..8 {
                assert_eq!(lp_all[t][ch], lp_sp[t][ch]);
                assert_eq!(bp_all[t][ch], bp_sp[t][ch]);
                assert_eq!(hp_all[t][ch], hp_sp[t][ch]);
            }
        }
        assert_eq!(st_all, st_sp);
    }

    #[test]
    fn test_run_simd_benchmarks() {
        let results = run_simd_benchmarks();
        assert_eq!(results.len(), 5);
        for r in &results {
            assert!(
                r.max_abs_err < 1e-5,
                "benchmark {} exceeded error tolerance: {}",
                r.name,
                r.max_abs_err
            );
            assert!(r.avx2_ns > 0.0 && r.scalar_ns > 0.0 && r.speedup > 0.0);
        }
    }
}
