//! Uniformly Partitioned Overlap-Save (UPOLS) Stereo 4096-Tap FIR Convolver.
//!
//! High-Efficiency Hard-RT Architecture:
//! - Block size `B = 256` samples (`5.333 ms` @ 48 kHz, matching PipeWire's native quantum),
//!   FFT size `N = 512` (`2 * B`).
//! - Up to `CONVOLVER_MAX_TAPS = 4096` taps per stereo channel (`16` uniform partitions —
//!   4x fewer FFT executions per second and 2x fewer partitions than `B = 128`).
//! - **Stereo-Packed AVX2/FMA 512-Point FFT/IFFT**:
//!   Packs `z[n] = x_L[n] + j * x_R[n]` to transform BOTH Left and Right channels in a
//!   SINGLE 512-point AVX2-vectorized Cooley-Tukey FFT (`1` forward + `1` inverse FFT per
//!   256 stereo frames instead of 8 FFTs), unpacking exact Hermitian half-spectra (`257` bins
//!   padded to `264 = 33 * 8` floats, 32-byte aligned).
//! - **Dual-Channel Symmetric-IR & General Stereo AVX2 Complex MAC**:
//!   Uses `crate::simd::cmul_acc_f32` (`asm/xps17_avx2_kernels.S`) for arbitrary asymmetric
//!   stereo IRs and a fused single-load dual-channel AVX2 accumulator when `h_L == h_R`
//!   (true for all 4 studio speaker-correction kernels).
//! - 4 preloaded IR slots (`Deep`, `Spatial`, `Reference`, `Voice`) for zero-allocation,
//!   click-free live mode switching on the hard-RT audio thread.
//!
//! Reference:
//! - Wefers, F. (2015), *Partitioned Convolution Algorithms for Real-Time Auralization*,
//!   Aachener Beiträge zur Technischen Akustik, RWTH Aachen University.

use core::f64::consts::PI;

use crate::simd::{cmul_acc_f32, has_avx2_fma};

/// Uniform partition block size in samples (`5.333 ms` at 48 kHz).
pub const CONVOLVER_BLOCK_SIZE: usize = 256;

/// Overlap-save FFT size (`2 * CONVOLVER_BLOCK_SIZE`).
pub const CONVOLVER_FFT_SIZE: usize = 512;

/// Maximum supported FIR impulse length in taps per channel.
pub const CONVOLVER_MAX_TAPS: usize = 4096;

/// Maximum number of uniform frequency-domain partitions (`4096 / 256 = 16`).
pub const CONVOLVER_MAX_PARTITIONS: usize = 16;

/// Number of unique Hermitian frequency bins (`0..=256`).
const N_BINS: usize = CONVOLVER_FFT_SIZE / 2 + 1;

/// Padded bin count (`33 * 8 = 264` floats = 32-byte aligned for full-width AVX2 with zero tail).
/// Bins `257..264` are always held at `0.0`.
const PADDED_BINS: usize = 264;

/// Number of preallocated IR slots for live mode switching (`Deep`, `Spatial`, `Reference`, `Voice`).
const NUM_SLOTS: usize = 4;

/// 32-byte aligned split real/imaginary half-spectrum (`0..=256`, padded to `264`).
#[repr(C, align(32))]
#[derive(Debug, Clone)]
struct AlignedSpectrum {
    re: [f32; PADDED_BINS],
    im: [f32; PADDED_BINS],
}

impl AlignedSpectrum {
    #[inline]
    fn zeroed() -> Self {
        Self {
            re: [0.0; PADDED_BINS],
            im: [0.0; PADDED_BINS],
        }
    }

    #[inline]
    fn clear(&mut self) {
        self.re.fill(0.0);
        self.im.fill(0.0);
    }
}

/// Pre-twiddled, zero-allocation 512-point AVX2-accelerated Radix-2/4 FFT/IFFT engine
/// with stereo `L + j*R` complex packing.
#[derive(Debug, Clone)]
struct Fft512 {
    bitrev: [u16; CONVOLVER_FFT_SIZE],
    /// Contiguous per-stage `cos(2*pi*k / len)` table (`1 + 2 + 4 + ... + 256 = 511`).
    tw_cos: [f32; 511],
    /// Contiguous per-stage `sin(2*pi*k / len)` table (`forward` uses `-tw_sin`, `inverse` `+tw_sin`).
    tw_sin: [f32; 511],
    use_avx2: bool,
}

impl Fft512 {
    fn new() -> Self {
        let mut bitrev = [0u16; CONVOLVER_FFT_SIZE];
        for (i, dst) in bitrev.iter_mut().enumerate() {
            // Reverse low 9 bits (`512 = 2^9`, `16 - 9 = 7`).
            *dst = (i as u16).reverse_bits() >> 7;
        }

        let mut tw_cos = [0.0f32; 511];
        let mut tw_sin = [0.0f32; 511];
        for s in 0..9usize {
            let half = 1usize << s;
            let len = half << 1;
            let off = half - 1;
            let inv_len = 2.0 * PI / (len as f64);
            for k in 0..half {
                let angle = (k as f64) * inv_len;
                tw_cos[off + k] = angle.cos() as f32;
                tw_sin[off + k] = angle.sin() as f32;
            }
        }

        Self {
            bitrev,
            tw_cos,
            tw_sin,
            use_avx2: has_avx2_fma(),
        }
    }

    #[cfg(target_arch = "x86_64")]
    #[target_feature(enable = "avx2,fma")]
    unsafe fn butterflies_avx2(
        &self,
        re: &mut [f32; CONVOLVER_FFT_SIZE],
        im: &mut [f32; CONVOLVER_FFT_SIZE],
        inverse: bool,
    ) {
        use core::arch::x86_64::*;

        // Fused stages 0 & 1 (radix-4 butterfly, zero trig multiplications)
        let mut base = 0usize;
        while base < CONVOLVER_FFT_SIZE {
            let r0 = re[base];
            let i0 = im[base];
            let r1 = re[base + 1];
            let i1 = im[base + 1];
            let r2 = re[base + 2];
            let i2 = im[base + 2];
            let r3 = re[base + 3];
            let i3 = im[base + 3];

            let s0r = r0 + r1;
            let s0i = i0 + i1;
            let d0r = r0 - r1;
            let d0i = i0 - i1;
            let s1r = r2 + r3;
            let s1i = i2 + i3;
            let d1r = r2 - r3;
            let d1i = i2 - i3;

            re[base] = s0r + s1r;
            im[base] = s0i + s1i;
            re[base + 2] = s0r - s1r;
            im[base + 2] = s0i - s1i;

            if inverse {
                // w = +j: (d1r + j*d1i) * j = -d1i + j*d1r
                re[base + 1] = d0r - d1i;
                im[base + 1] = d0i + d1r;
                re[base + 3] = d0r + d1i;
                im[base + 3] = d0i - d1r;
            } else {
                // w = -j: (d1r + j*d1i) * (-j) = d1i - j*d1r
                re[base + 1] = d0r + d1i;
                im[base + 1] = d0i - d1r;
                re[base + 3] = d0r - d1i;
                im[base + 3] = d0i + d1r;
            }
            base += 4;
        }

        // Stage 2: half = 4, step = 8 (128-bit SSE/AVX 4-wide)
        {
            let tw_off = 3usize;
            let w_re = _mm_loadu_ps(self.tw_cos.as_ptr().add(tw_off));
            let w_sin_raw = _mm_loadu_ps(self.tw_sin.as_ptr().add(tw_off));
            let w_im = if inverse {
                w_sin_raw
            } else {
                _mm_xor_ps(w_sin_raw, _mm_set1_ps(-0.0))
            };
            let re_ptr = re.as_mut_ptr();
            let im_ptr = im.as_mut_ptr();
            let mut b = 0usize;
            while b < CONVOLVER_FFT_SIZE {
                let a_re = _mm_loadu_ps(re_ptr.add(b));
                let a_im = _mm_loadu_ps(im_ptr.add(b));
                let b_re = _mm_loadu_ps(re_ptr.add(b + 4));
                let b_im = _mm_loadu_ps(im_ptr.add(b + 4));

                let t_re = _mm_fmsub_ps(b_re, w_re, _mm_mul_ps(b_im, w_im));
                let t_im = _mm_fmadd_ps(b_re, w_im, _mm_mul_ps(b_im, w_re));

                _mm_storeu_ps(re_ptr.add(b), _mm_add_ps(a_re, t_re));
                _mm_storeu_ps(im_ptr.add(b), _mm_add_ps(a_im, t_im));
                _mm_storeu_ps(re_ptr.add(b + 4), _mm_sub_ps(a_re, t_re));
                _mm_storeu_ps(im_ptr.add(b + 4), _mm_sub_ps(a_im, t_im));
                b += 8;
            }
        }

        // Stages 3..9: half = 8, 16, 32, 64, 128, 256 (full 256-bit 8-wide AVX2/FMA!)
        let sign_mask = _mm256_set1_ps(-0.0);
        let re_ptr = re.as_mut_ptr();
        let im_ptr = im.as_mut_ptr();
        let cos_ptr = self.tw_cos.as_ptr();
        let sin_ptr = self.tw_sin.as_ptr();

        for s in 3..9usize {
            let half = 1usize << s;
            let step = half << 1;
            let tw_off = half - 1;
            let mut b = 0usize;
            while b < CONVOLVER_FFT_SIZE {
                let mut k = 0usize;
                while k < half {
                    let w_re = _mm256_loadu_ps(cos_ptr.add(tw_off + k));
                    let w_sin = _mm256_loadu_ps(sin_ptr.add(tw_off + k));
                    let w_im = if inverse {
                        w_sin
                    } else {
                        _mm256_xor_ps(w_sin, sign_mask)
                    };

                    let i0 = b + k;
                    let i1 = i0 + half;
                    let a_re = _mm256_loadu_ps(re_ptr.add(i0));
                    let a_im = _mm256_loadu_ps(im_ptr.add(i0));
                    let b_re = _mm256_loadu_ps(re_ptr.add(i1));
                    let b_im = _mm256_loadu_ps(im_ptr.add(i1));

                    let t_re = _mm256_fmsub_ps(b_re, w_re, _mm256_mul_ps(b_im, w_im));
                    let t_im = _mm256_fmadd_ps(b_re, w_im, _mm256_mul_ps(b_im, w_re));

                    _mm256_storeu_ps(re_ptr.add(i0), _mm256_add_ps(a_re, t_re));
                    _mm256_storeu_ps(im_ptr.add(i0), _mm256_add_ps(a_im, t_im));
                    _mm256_storeu_ps(re_ptr.add(i1), _mm256_sub_ps(a_re, t_re));
                    _mm256_storeu_ps(im_ptr.add(i1), _mm256_sub_ps(a_im, t_im));

                    k += 8;
                }
                b += step;
            }
        }
    }

    #[inline]
    fn butterflies_scalar(
        &self,
        re: &mut [f32; CONVOLVER_FFT_SIZE],
        im: &mut [f32; CONVOLVER_FFT_SIZE],
        inverse: bool,
    ) {
        for s in 0..9usize {
            let half = 1usize << s;
            let step = half << 1;
            let tw_off = half - 1;
            let mut base = 0usize;
            while base < CONVOLVER_FFT_SIZE {
                for k in 0..half {
                    let w_re = self.tw_cos[tw_off + k];
                    let w_im = if inverse {
                        self.tw_sin[tw_off + k]
                    } else {
                        -self.tw_sin[tw_off + k]
                    };
                    let i0 = base + k;
                    let i1 = i0 + half;
                    let a_re = re[i0];
                    let a_im = im[i0];
                    let b_re = re[i1];
                    let b_im = im[i1];
                    let t_re = b_re * w_re - b_im * w_im;
                    let t_im = b_re * w_im + b_im * w_re;
                    re[i0] = a_re + t_re;
                    im[i0] = a_im + t_im;
                    re[i1] = a_re - t_re;
                    im[i1] = a_im - t_im;
                }
                base += step;
            }
        }
    }

    #[inline]
    fn butterflies(
        &self,
        re: &mut [f32; CONVOLVER_FFT_SIZE],
        im: &mut [f32; CONVOLVER_FFT_SIZE],
        inverse: bool,
    ) {
        #[cfg(target_arch = "x86_64")]
        {
            if self.use_avx2 {
                // SAFETY: `self.use_avx2` verified AVX2 + FMA at construction.
                unsafe {
                    self.butterflies_avx2(re, im, inverse);
                }
                return;
            }
        }
        self.butterflies_scalar(re, im, inverse);
    }

    /// Single-channel forward 512-point real FFT (used during offline IR slot loading & tests).
    #[inline]
    fn forward_real(
        &self,
        input: &[f32; CONVOLVER_FFT_SIZE],
        out: &mut AlignedSpectrum,
        scratch_re: &mut [f32; CONVOLVER_FFT_SIZE],
        scratch_im: &mut [f32; CONVOLVER_FFT_SIZE],
    ) {
        for i in 0..CONVOLVER_FFT_SIZE {
            scratch_re[self.bitrev[i] as usize] = input[i];
        }
        scratch_im.fill(0.0);

        self.butterflies(scratch_re, scratch_im, false);

        out.re[..N_BINS].copy_from_slice(&scratch_re[..N_BINS]);
        out.im[..N_BINS].copy_from_slice(&scratch_im[..N_BINS]);
        out.im[0] = 0.0;
        out.im[N_BINS - 1] = 0.0;
        for k in N_BINS..PADDED_BINS {
            out.re[k] = 0.0;
            out.im[k] = 0.0;
        }
    }

    /// Stereo-packed forward 512-point FFT: transforms BOTH `in_l` and `in_r` in ONE complex FFT
    /// via `z[n] = in_l[n] + j * in_r[n]`.
    #[inline]
    fn forward_stereo(
        &self,
        in_l: &[f32; CONVOLVER_FFT_SIZE],
        in_r: &[f32; CONVOLVER_FFT_SIZE],
        out_l: &mut AlignedSpectrum,
        out_r: &mut AlignedSpectrum,
        z_re: &mut [f32; CONVOLVER_FFT_SIZE],
        z_im: &mut [f32; CONVOLVER_FFT_SIZE],
    ) {
        for i in 0..CONVOLVER_FFT_SIZE {
            let br = self.bitrev[i] as usize;
            z_re[br] = in_l[i];
            z_im[br] = in_r[i];
        }

        self.butterflies(z_re, z_im, false);

        out_l.re[0] = z_re[0];
        out_l.im[0] = 0.0;
        out_r.re[0] = z_im[0];
        out_r.im[0] = 0.0;

        let nyq = CONVOLVER_FFT_SIZE / 2;
        out_l.re[nyq] = z_re[nyq];
        out_l.im[nyq] = 0.0;
        out_r.re[nyq] = z_im[nyq];
        out_r.im[nyq] = 0.0;

        for k in 1..nyq {
            let m = CONVOLVER_FFT_SIZE - k;
            let zr_k = z_re[k];
            let zi_k = z_im[k];
            let zr_m = z_re[m];
            let zi_m = z_im[m];
            out_l.re[k] = 0.5 * (zr_k + zr_m);
            out_l.im[k] = 0.5 * (zi_k - zi_m);
            out_r.re[k] = 0.5 * (zi_k + zi_m);
            out_r.im[k] = 0.5 * (zr_m - zr_k);
        }

        for k in N_BINS..PADDED_BINS {
            out_l.re[k] = 0.0;
            out_l.im[k] = 0.0;
            out_r.re[k] = 0.0;
            out_r.im[k] = 0.0;
        }
    }

    /// Stereo-packed inverse 512-point FFT: transforms BOTH `spec_l` and `spec_r` in ONE complex
    /// IFFT via `W[k] = Y_L[k] + j * Y_R[k]`, writing the non-aliased second half `[256..512]`
    /// directly into `out_l` and `out_r`.
    #[inline]
    fn inverse_stereo_second_half(
        &self,
        spec_l: &AlignedSpectrum,
        spec_r: &AlignedSpectrum,
        out_l: &mut [f32; CONVOLVER_BLOCK_SIZE],
        out_r: &mut [f32; CONVOLVER_BLOCK_SIZE],
        w_re: &mut [f32; CONVOLVER_FFT_SIZE],
        w_im: &mut [f32; CONVOLVER_FFT_SIZE],
    ) {
        let b0 = self.bitrev[0] as usize;
        w_re[b0] = spec_l.re[0];
        w_im[b0] = spec_r.re[0];

        let nyq = CONVOLVER_FFT_SIZE / 2;
        let bn = self.bitrev[nyq] as usize;
        w_re[bn] = spec_l.re[nyq];
        w_im[bn] = spec_r.re[nyq];

        for k in 1..nyq {
            let m = CONVOLVER_FFT_SIZE - k;
            let lr = spec_l.re[k];
            let li = spec_l.im[k];
            let rr = spec_r.re[k];
            let ri = spec_r.im[k];

            let bk = self.bitrev[k] as usize;
            let bm = self.bitrev[m] as usize;
            w_re[bk] = lr - ri;
            w_im[bk] = li + rr;
            w_re[bm] = lr + ri;
            w_im[bm] = rr - li;
        }

        self.butterflies(w_re, w_im, true);

        const INV_N: f32 = 1.0 / (CONVOLVER_FFT_SIZE as f32);
        for i in 0..CONVOLVER_BLOCK_SIZE {
            out_l[i] = w_re[CONVOLVER_BLOCK_SIZE + i] * INV_N;
            out_r[i] = w_im[CONVOLVER_BLOCK_SIZE + i] * INV_N;
        }
    }

    #[cfg(test)]
    fn inverse_real(
        &self,
        spec: &AlignedSpectrum,
        output: &mut [f32; CONVOLVER_FFT_SIZE],
        scratch_re: &mut [f32; CONVOLVER_FFT_SIZE],
        scratch_im: &mut [f32; CONVOLVER_FFT_SIZE],
    ) {
        let b0 = self.bitrev[0] as usize;
        scratch_re[b0] = spec.re[0];
        scratch_im[b0] = 0.0;

        let bn = self.bitrev[CONVOLVER_FFT_SIZE / 2] as usize;
        scratch_re[bn] = spec.re[N_BINS - 1];
        scratch_im[bn] = 0.0;

        for k in 1..(CONVOLVER_FFT_SIZE / 2) {
            let re = spec.re[k];
            let im = spec.im[k];
            let b_pos = self.bitrev[k] as usize;
            scratch_re[b_pos] = re;
            scratch_im[b_pos] = im;
            let b_neg = self.bitrev[CONVOLVER_FFT_SIZE - k] as usize;
            scratch_re[b_neg] = re;
            scratch_im[b_neg] = -im;
        }

        self.butterflies(scratch_re, scratch_im, true);

        const INV_N: f32 = 1.0 / (CONVOLVER_FFT_SIZE as f32);
        for i in 0..CONVOLVER_FFT_SIZE {
            output[i] = scratch_re[i] * INV_N;
        }
    }
}

/// Fused AVX2 dual-channel complex multiply-accumulate when Left & Right share the same IR (`h_L == h_R`).
/// Loads `h_re, h_im` once into YMM registers and updates both `acc_l` and `acc_r` simultaneously.
#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2,fma")]
unsafe fn cmul_acc_dual_sym_avx2(
    acc_l: &mut AlignedSpectrum,
    acc_r: &mut AlignedSpectrum,
    xl: &AlignedSpectrum,
    xr: &AlignedSpectrum,
    h: &AlignedSpectrum,
) {
    use core::arch::x86_64::*;

    let al_re = acc_l.re.as_mut_ptr();
    let al_im = acc_l.im.as_mut_ptr();
    let ar_re = acc_r.re.as_mut_ptr();
    let ar_im = acc_r.im.as_mut_ptr();
    let xl_re = xl.re.as_ptr();
    let xl_im = xl.im.as_ptr();
    let xr_re = xr.re.as_ptr();
    let xr_im = xr.im.as_ptr();
    let h_re = h.re.as_ptr();
    let h_im = h.im.as_ptr();

    let mut k = 0usize;
    while k < PADDED_BINS {
        let hr = _mm256_load_ps(h_re.add(k));
        let hi = _mm256_load_ps(h_im.add(k));

        let xlr = _mm256_load_ps(xl_re.add(k));
        let xli = _mm256_load_ps(xl_im.add(k));
        let mut lr = _mm256_load_ps(al_re.add(k));
        let mut li = _mm256_load_ps(al_im.add(k));
        lr = _mm256_fmadd_ps(xlr, hr, lr);
        li = _mm256_fmadd_ps(xlr, hi, li);
        lr = _mm256_fnmadd_ps(xli, hi, lr);
        li = _mm256_fmadd_ps(xli, hr, li);
        _mm256_store_ps(al_re.add(k), lr);
        _mm256_store_ps(al_im.add(k), li);

        let xrr = _mm256_load_ps(xr_re.add(k));
        let xri = _mm256_load_ps(xr_im.add(k));
        let mut rr = _mm256_load_ps(ar_re.add(k));
        let mut ri = _mm256_load_ps(ar_im.add(k));
        rr = _mm256_fmadd_ps(xrr, hr, rr);
        ri = _mm256_fmadd_ps(xrr, hi, ri);
        rr = _mm256_fnmadd_ps(xri, hi, rr);
        ri = _mm256_fmadd_ps(xri, hr, ri);
        _mm256_store_ps(ar_re.add(k), rr);
        _mm256_store_ps(ar_im.add(k), ri);

        k += 8;
    }
}

/// Pre-transformed stereo impulse response slot (`<= 16` active partitions).
#[derive(Debug, Clone)]
struct IrSlot {
    h_l: Vec<AlignedSpectrum>,
    h_r: Vec<AlignedSpectrum>,
    active_partitions: usize,
    symmetric_lr: bool,
}

impl IrSlot {
    fn new_preallocated() -> Self {
        Self {
            h_l: vec![AlignedSpectrum::zeroed(); CONVOLVER_MAX_PARTITIONS],
            h_r: vec![AlignedSpectrum::zeroed(); CONVOLVER_MAX_PARTITIONS],
            active_partitions: 0,
            symmetric_lr: true,
        }
    }
}

/// Heap-boxed workspace allocated once inside `PartitionedConvolver::new()`.
#[derive(Debug, Clone)]
struct ConvolverWorkspace {
    fft: Fft512,
    slots: Vec<IrSlot>,
    fdl_l: Vec<AlignedSpectrum>,
    fdl_r: Vec<AlignedSpectrum>,
    acc_l: AlignedSpectrum,
    acc_r: AlignedSpectrum,
    overlap_l: [f32; CONVOLVER_BLOCK_SIZE],
    overlap_r: [f32; CONVOLVER_BLOCK_SIZE],
    in_buf_l: [f32; CONVOLVER_BLOCK_SIZE],
    in_buf_r: [f32; CONVOLVER_BLOCK_SIZE],
    out_buf_l: [f32; CONVOLVER_BLOCK_SIZE],
    out_buf_r: [f32; CONVOLVER_BLOCK_SIZE],
    xfade_l: [f32; CONVOLVER_BLOCK_SIZE],
    xfade_r: [f32; CONVOLVER_BLOCK_SIZE],
    fft_in_l: [f32; CONVOLVER_FFT_SIZE],
    fft_in_r: [f32; CONVOLVER_FFT_SIZE],
    scratch_re: [f32; CONVOLVER_FFT_SIZE],
    scratch_im: [f32; CONVOLVER_FFT_SIZE],
}

/// Hard-RT Uniformly Partitioned Overlap-Save (UPOLS) stereo FIR convolver.
#[derive(Debug, Clone)]
pub struct PartitionedConvolver {
    ws: Box<ConvolverWorkspace>,
    active_slot: usize,
    xfade_from_slot: Option<usize>,
    fdl_pos: usize,
    fifo_pos: usize,
    bypass: bool,
    has_processed: bool,
}

impl Default for PartitionedConvolver {
    fn default() -> Self {
        Self::new()
    }
}

impl PartitionedConvolver {
    /// Creates a new stereo UPOLS convolver preallocated for 4 IR slots × 16 partitions.
    ///
    /// Initializes all 4 slots with an identity Dirac impulse `[1.0, 0.0, ...]`.
    /// All memory is allocated here; `process_block`, `reset`, and `select_slot`
    /// perform strictly ZERO heap allocations.
    pub fn new() -> Self {
        let mut slots = Vec::with_capacity(NUM_SLOTS);
        for _ in 0..NUM_SLOTS {
            slots.push(IrSlot::new_preallocated());
        }

        let ws = Box::new(ConvolverWorkspace {
            fft: Fft512::new(),
            slots,
            fdl_l: vec![AlignedSpectrum::zeroed(); CONVOLVER_MAX_PARTITIONS],
            fdl_r: vec![AlignedSpectrum::zeroed(); CONVOLVER_MAX_PARTITIONS],
            acc_l: AlignedSpectrum::zeroed(),
            acc_r: AlignedSpectrum::zeroed(),
            overlap_l: [0.0; CONVOLVER_BLOCK_SIZE],
            overlap_r: [0.0; CONVOLVER_BLOCK_SIZE],
            in_buf_l: [0.0; CONVOLVER_BLOCK_SIZE],
            in_buf_r: [0.0; CONVOLVER_BLOCK_SIZE],
            out_buf_l: [0.0; CONVOLVER_BLOCK_SIZE],
            out_buf_r: [0.0; CONVOLVER_BLOCK_SIZE],
            xfade_l: [0.0; CONVOLVER_BLOCK_SIZE],
            xfade_r: [0.0; CONVOLVER_BLOCK_SIZE],
            fft_in_l: [0.0; CONVOLVER_FFT_SIZE],
            fft_in_r: [0.0; CONVOLVER_FFT_SIZE],
            scratch_re: [0.0; CONVOLVER_FFT_SIZE],
            scratch_im: [0.0; CONVOLVER_FFT_SIZE],
        });

        let mut conv = Self {
            ws,
            active_slot: 0,
            xfade_from_slot: None,
            fdl_pos: 0,
            fifo_pos: 0,
            bypass: false,
            has_processed: false,
        };

        let identity = [1.0f32];
        for s in 0..NUM_SLOTS {
            conv.load_impulse_slot(s, &identity, &identity);
        }
        conv
    }

    /// Clears the Frequency-Domain Delay Line (FDL) and input/output FIFOs without
    /// altering any loaded impulse responses.
    pub fn reset(&mut self) {
        for spec in self.ws.fdl_l.iter_mut() {
            spec.clear();
        }
        for spec in self.ws.fdl_r.iter_mut() {
            spec.clear();
        }
        self.ws.overlap_l.fill(0.0);
        self.ws.overlap_r.fill(0.0);
        self.ws.in_buf_l.fill(0.0);
        self.ws.in_buf_r.fill(0.0);
        self.ws.out_buf_l.fill(0.0);
        self.ws.out_buf_r.fill(0.0);
        self.ws.xfade_l.fill(0.0);
        self.ws.xfade_r.fill(0.0);
        self.fdl_pos = 0;
        self.fifo_pos = 0;
        self.xfade_from_slot = None;
        self.has_processed = false;
    }

    /// Loads a stereo impulse response into slot `0` and immediately selects slot `0`.
    pub fn load_impulse(&mut self, ir_l: &[f32], ir_r: &[f32]) {
        self.load_impulse_slot(0, ir_l, ir_r);
        self.active_slot = 0;
        self.xfade_from_slot = None;
    }

    /// Loads and pre-transforms a stereo impulse response into the specified slot (`0..4`).
    ///
    /// Truncates beyond `CONVOLVER_MAX_TAPS` (`4096`) and skips trailing near-zero
    /// partitions (`<= 1e-12`) to minimize MAC work on shorter impulses. Zero heap allocations.
    pub fn load_impulse_slot(&mut self, slot: usize, ir_l: &[f32], ir_r: &[f32]) {
        let s = slot.min(NUM_SLOTS - 1);
        let len_l = ir_l.len().min(CONVOLVER_MAX_TAPS);
        let len_r = ir_r.len().min(CONVOLVER_MAX_TAPS);
        let max_len = len_l.max(len_r);

        let symmetric_lr = len_l == len_r
            && ir_l[..len_l]
                .iter()
                .zip(&ir_r[..len_r])
                .all(|(&a, &b)| {
                    let ca = if a.is_finite() { a } else { 0.0 };
                    let cb = if b.is_finite() { b } else { 0.0 };
                    (ca - cb).abs() <= 1.0e-9
                });

        let mut active_parts = 0usize;
        let max_parts = (max_len + CONVOLVER_BLOCK_SIZE - 1) / CONVOLVER_BLOCK_SIZE;
        for p in 0..max_parts.min(CONVOLVER_MAX_PARTITIONS) {
            let start = p * CONVOLVER_BLOCK_SIZE;
            let end_l = (start + CONVOLVER_BLOCK_SIZE).min(len_l);
            let end_r = (start + CONVOLVER_BLOCK_SIZE).min(len_r);

            let mut non_zero = false;
            if start < end_l {
                for &v in &ir_l[start..end_l] {
                    if v.is_finite() && v.abs() > 1e-12 {
                        non_zero = true;
                        break;
                    }
                }
            }
            if !non_zero && start < end_r {
                for &v in &ir_r[start..end_r] {
                    if v.is_finite() && v.abs() > 1e-12 {
                        non_zero = true;
                        break;
                    }
                }
            }
            if non_zero {
                active_parts = p + 1;
            }
        }

        let ws = &mut *self.ws;
        let target_slot = &mut ws.slots[s];
        target_slot.active_partitions = active_parts;
        target_slot.symmetric_lr = symmetric_lr;

        for p in 0..active_parts {
            let start = p * CONVOLVER_BLOCK_SIZE;

            ws.fft_in_l.fill(0.0);
            if start < len_l {
                let end_l = (start + CONVOLVER_BLOCK_SIZE).min(len_l);
                for (dst, &src) in ws.fft_in_l[..(end_l - start)]
                    .iter_mut()
                    .zip(&ir_l[start..end_l])
                {
                    *dst = if src.is_finite() { src } else { 0.0 };
                }
            }
            ws.fft.forward_real(
                &ws.fft_in_l,
                &mut target_slot.h_l[p],
                &mut ws.scratch_re,
                &mut ws.scratch_im,
            );

            if symmetric_lr {
                target_slot.h_r[p].re.copy_from_slice(&target_slot.h_l[p].re);
                target_slot.h_r[p].im.copy_from_slice(&target_slot.h_l[p].im);
            } else {
                ws.fft_in_r.fill(0.0);
                if start < len_r {
                    let end_r = (start + CONVOLVER_BLOCK_SIZE).min(len_r);
                    for (dst, &src) in ws.fft_in_r[..(end_r - start)]
                        .iter_mut()
                        .zip(&ir_r[start..end_r])
                    {
                        *dst = if src.is_finite() { src } else { 0.0 };
                    }
                }
                ws.fft.forward_real(
                    &ws.fft_in_r,
                    &mut target_slot.h_r[p],
                    &mut ws.scratch_re,
                    &mut ws.scratch_im,
                );
            }
        }

        for p in active_parts..CONVOLVER_MAX_PARTITIONS {
            target_slot.h_l[p].clear();
            target_slot.h_r[p].clear();
        }
    }

    /// Selects a preloaded IR slot (`0..4`) on the RT thread with zero allocations.
    ///
    /// If audio is actively streaming and the slot changes, applies a seamless 1-block
    /// (`256`-sample / `5.33 ms`) linear crossfade on the next partition step.
    #[inline]
    pub fn select_slot(&mut self, slot: usize) {
        let s = slot.min(NUM_SLOTS - 1);
        if s == self.active_slot {
            return;
        }
        if self.has_processed {
            self.xfade_from_slot = Some(self.active_slot);
        }
        self.active_slot = s;
    }

    /// Enables or disables convolver bypass without clearing loaded IRs.
    #[inline]
    pub fn set_bypass(&mut self, bypass: bool) {
        self.bypass = bypass;
    }

    /// Returns `true` if the convolver is currently bypassed.
    #[inline]
    pub fn is_bypassed(&self) -> bool {
        self.bypass
    }

    /// Returns the number of active uniform partitions (`0..=16`) in the currently selected slot.
    #[inline]
    pub fn active_partitions(&self) -> usize {
        self.ws.slots[self.active_slot].active_partitions
    }

    /// Returns exact algorithmic latency in samples (`0` when bypassed, `256` when active).
    #[inline]
    pub fn latency_samples(&self) -> usize {
        if self.bypass {
            0
        } else {
            CONVOLVER_BLOCK_SIZE
        }
    }

    /// Convolves a stereo audio block of arbitrary length in-place with strictly ZERO allocations.
    pub fn process_block(&mut self, left: &mut [f32], right: &mut [f32]) {
        if self.bypass {
            return;
        }
        let len = left.len().min(right.len());
        if len == 0 {
            return;
        }
        self.has_processed = true;

        let mut offset = 0usize;
        while offset < len {
            let space = CONVOLVER_BLOCK_SIZE - self.fifo_pos;
            let take = (len - offset).min(space);
            let p = self.fifo_pos;

            self.ws.in_buf_l[p..p + take].copy_from_slice(&left[offset..offset + take]);
            self.ws.in_buf_r[p..p + take].copy_from_slice(&right[offset..offset + take]);
            left[offset..offset + take].copy_from_slice(&self.ws.out_buf_l[p..p + take]);
            right[offset..offset + take].copy_from_slice(&self.ws.out_buf_r[p..p + take]);

            self.fifo_pos += take;
            offset += take;

            if self.fifo_pos == CONVOLVER_BLOCK_SIZE {
                self.process_partition_block();
                self.fifo_pos = 0;
            }
        }
    }

    /// Evaluates one full 256-sample stereo overlap-save step using stereo-packed AVX2 FFT/IFFT
    /// and AVX2 complex multiply-accumulate.
    fn process_partition_block(&mut self) {
        let ws = &mut *self.ws;
        let fdl_cur = self.fdl_pos;

        // 1. Assemble overlap-save frames `[prev_256, cur_256]` for Left & Right and transform
        //    BOTH channels simultaneously in a SINGLE stereo-packed 512-point AVX2 FFT!
        ws.fft_in_l[..CONVOLVER_BLOCK_SIZE].copy_from_slice(&ws.overlap_l);
        ws.fft_in_l[CONVOLVER_BLOCK_SIZE..].copy_from_slice(&ws.in_buf_l);
        ws.overlap_l.copy_from_slice(&ws.in_buf_l);

        ws.fft_in_r[..CONVOLVER_BLOCK_SIZE].copy_from_slice(&ws.overlap_r);
        ws.fft_in_r[CONVOLVER_BLOCK_SIZE..].copy_from_slice(&ws.in_buf_r);
        ws.overlap_r.copy_from_slice(&ws.in_buf_r);

        ws.fft.forward_stereo(
            &ws.fft_in_l,
            &ws.fft_in_r,
            &mut ws.fdl_l[fdl_cur],
            &mut ws.fdl_r[fdl_cur],
            &mut ws.scratch_re,
            &mut ws.scratch_im,
        );

        // 2. Evaluate active slot (and optional 1-block crossfade from previous slot)
        Self::convolve_slot_into(ws, fdl_cur, self.active_slot, true);

        if let Some(prev_slot) = self.xfade_from_slot.take() {
            Self::convolve_slot_into(ws, fdl_cur, prev_slot, false);
            const INV_B: f32 = 1.0 / (CONVOLVER_BLOCK_SIZE as f32);
            for i in 0..CONVOLVER_BLOCK_SIZE {
                let alpha = (i as f32 + 0.5) * INV_B;
                let beta = 1.0 - alpha;
                ws.out_buf_l[i] = ws.xfade_l[i] * beta + ws.out_buf_l[i] * alpha;
                ws.out_buf_r[i] = ws.xfade_r[i] * beta + ws.out_buf_r[i] * alpha;
            }
        }

        // 3. Advance FDL ring pointer (`CONVOLVER_MAX_PARTITIONS == 16` is power of two)
        self.fdl_pos = (fdl_cur + 1) & (CONVOLVER_MAX_PARTITIONS - 1);
    }

    #[inline]
    fn convolve_slot_into(
        ws: &mut ConvolverWorkspace,
        fdl_cur: usize,
        slot_idx: usize,
        to_out_buf: bool,
    ) {
        let active_parts = ws.slots[slot_idx].active_partitions;
        if active_parts == 0 {
            if to_out_buf {
                ws.out_buf_l.fill(0.0);
                ws.out_buf_r.fill(0.0);
            } else {
                ws.xfade_l.fill(0.0);
                ws.xfade_r.fill(0.0);
            }
            return;
        }

        ws.acc_l.clear();
        ws.acc_r.clear();

        let slot = &ws.slots[slot_idx];
        let sym_fast = slot.symmetric_lr && ws.fft.use_avx2;

        #[cfg(target_arch = "x86_64")]
        if sym_fast {
            for p in 0..active_parts {
                let fdl_idx = fdl_cur.wrapping_sub(p) & (CONVOLVER_MAX_PARTITIONS - 1);
                // SAFETY: `ws.fft.use_avx2` verified AVX2+FMA and all `AlignedSpectrum` buffers
                // are `#[repr(C, align(32))]` with length `PADDED_BINS == 264` (multiple of 8).
                unsafe {
                    cmul_acc_dual_sym_avx2(
                        &mut ws.acc_l,
                        &mut ws.acc_r,
                        &ws.fdl_l[fdl_idx],
                        &ws.fdl_r[fdl_idx],
                        &slot.h_l[p],
                    );
                }
            }
        } else {
            for p in 0..active_parts {
                let fdl_idx = fdl_cur.wrapping_sub(p) & (CONVOLVER_MAX_PARTITIONS - 1);
                let xl = &ws.fdl_l[fdl_idx];
                let hl = &slot.h_l[p];
                cmul_acc_f32(
                    &mut ws.acc_l.re,
                    &mut ws.acc_l.im,
                    &xl.re,
                    &xl.im,
                    &hl.re,
                    &hl.im,
                );

                let xr = &ws.fdl_r[fdl_idx];
                let hr = &slot.h_r[p];
                cmul_acc_f32(
                    &mut ws.acc_r.re,
                    &mut ws.acc_r.im,
                    &xr.re,
                    &xr.im,
                    &hr.re,
                    &hr.im,
                );
            }
        }

        #[cfg(not(target_arch = "x86_64"))]
        {
            let _ = sym_fast;
            for p in 0..active_parts {
                let fdl_idx = fdl_cur.wrapping_sub(p) & (CONVOLVER_MAX_PARTITIONS - 1);
                let xl = &ws.fdl_l[fdl_idx];
                let hl = &slot.h_l[p];
                cmul_acc_f32(
                    &mut ws.acc_l.re,
                    &mut ws.acc_l.im,
                    &xl.re,
                    &xl.im,
                    &hl.re,
                    &hl.im,
                );

                let xr = &ws.fdl_r[fdl_idx];
                let hr = &slot.h_r[p];
                cmul_acc_f32(
                    &mut ws.acc_r.re,
                    &mut ws.acc_r.im,
                    &xr.re,
                    &xr.im,
                    &hr.re,
                    &hr.im,
                );
            }
        }

        // Single stereo-packed 512-point IFFT recovers BOTH Left and Right valid halves `[256..512]`
        if to_out_buf {
            ws.fft.inverse_stereo_second_half(
                &ws.acc_l,
                &ws.acc_r,
                &mut ws.out_buf_l,
                &mut ws.out_buf_r,
                &mut ws.scratch_re,
                &mut ws.scratch_im,
            );
        } else {
            ws.fft.inverse_stereo_second_half(
                &ws.acc_l,
                &ws.acc_r,
                &mut ws.xfade_l,
                &mut ws.xfade_r,
                &mut ws.scratch_re,
                &mut ws.scratch_im,
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Deterministic xorshift64* PRNG for reproducible offline unit tests.
    struct Rng(u64);
    impl Rng {
        fn next_f32(&mut self) -> f32 {
            let mut x = self.0;
            x ^= x >> 12;
            x ^= x << 25;
            x ^= x >> 27;
            self.0 = x;
            let v = x.wrapping_mul(0x2545_F491_4F6C_DD1D);
            ((v >> 40) as f32 / ((1u32 << 24) as f32)) * 2.0 - 1.0
        }
    }

    fn direct_convolve(x: &[f32], h: &[f32]) -> Vec<f32> {
        let mut y = vec![0.0f32; x.len()];
        for n in 0..x.len() {
            let mut sum = 0.0f64;
            let k_max = h.len().min(n + 1);
            for k in 0..k_max {
                sum += (x[n - k] as f64) * (h[k] as f64);
            }
            y[n] = sum as f32;
        }
        y
    }

    fn run_parity_check(ir_len: usize, chunk_sizes: &[usize], symmetric_ir: bool) {
        let mut rng = Rng(0x9E37_79B9_7F4A_7C15 ^ (ir_len as u64) ^ (symmetric_ir as u64));
        let mut ir_l = vec![0.0f32; ir_len];
        let mut ir_r = vec![0.0f32; ir_len];
        for i in 0..ir_len {
            let decay = (-(i as f32) / (ir_len as f32 * 0.35)).exp();
            ir_l[i] = rng.next_f32() * decay * 0.25;
            ir_r[i] = if symmetric_ir {
                ir_l[i]
            } else {
                rng.next_f32() * decay * 0.25
            };
        }
        // Ensure final tap is strictly non-zero so all expected partitions stay active.
        ir_l[ir_len - 1] = 0.05;
        ir_r[ir_len - 1] = if symmetric_ir { 0.05 } else { -0.04 };

        let total_samples = ir_len + 2048;
        let mut in_l = vec![0.0f32; total_samples];
        let mut in_r = vec![0.0f32; total_samples];
        for i in 0..total_samples {
            let t = i as f32 / 48000.0;
            in_l[i] = 0.5 * (2.0 * core::f32::consts::PI * 211.0 * t).sin() + 0.3 * rng.next_f32();
            in_r[i] = 0.5 * (2.0 * core::f32::consts::PI * 997.0 * t).cos() + 0.3 * rng.next_f32();
        }

        let ref_l = direct_convolve(&in_l, &ir_l);
        let ref_r = direct_convolve(&in_r, &ir_r);

        for &chunk in chunk_sizes {
            let mut conv = PartitionedConvolver::new();
            conv.load_impulse(&ir_l, &ir_r);
            assert_eq!(
                conv.active_partitions(),
                (ir_len + CONVOLVER_BLOCK_SIZE - 1) / CONVOLVER_BLOCK_SIZE
            );
            assert_eq!(conv.latency_samples(), CONVOLVER_BLOCK_SIZE);

            let mut out_l = in_l.clone();
            let mut out_r = in_r.clone();
            let mut pos = 0usize;
            while pos < total_samples {
                let n = (total_samples - pos).min(chunk);
                conv.process_block(&mut out_l[pos..pos + n], &mut out_r[pos..pos + n]);
                pos += n;
            }

            let lat = conv.latency_samples();
            let mut max_err_l = 0.0f32;
            let mut max_err_r = 0.0f32;
            for n in 0..(total_samples - lat) {
                let err_l = (out_l[n + lat] - ref_l[n]).abs();
                let err_r = (out_r[n + lat] - ref_r[n]).abs();
                max_err_l = max_err_l.max(err_l);
                max_err_r = max_err_r.max(err_r);
            }
            assert!(
                max_err_l < 1e-4,
                "ir_len={ir_len}, sym={symmetric_ir}, chunk={chunk}: Left max error {max_err_l} >= 1e-4"
            );
            assert!(
                max_err_r < 1e-4,
                "ir_len={ir_len}, sym={symmetric_ir}, chunk={chunk}: Right max error {max_err_r} >= 1e-4"
            );
        }
    }

    #[test]
    fn test_fft512_roundtrip_and_parseval() {
        let fft = Fft512::new();
        let mut rng = Rng(0xDEAD_BEEF_CAFE_BABEu64);
        let mut x = [0.0f32; CONVOLVER_FFT_SIZE];
        let mut time_energy = 0.0f64;
        for v in x.iter_mut() {
            *v = rng.next_f32();
            time_energy += (*v as f64) * (*v as f64);
        }

        let mut spec = AlignedSpectrum::zeroed();
        let mut y = [0.0f32; CONVOLVER_FFT_SIZE];
        let mut sr = [0.0f32; CONVOLVER_FFT_SIZE];
        let mut si = [0.0f32; CONVOLVER_FFT_SIZE];

        fft.forward_real(&x, &mut spec, &mut sr, &mut si);

        let mut freq_energy = (spec.re[0] as f64).powi(2) + (spec.re[N_BINS - 1] as f64).powi(2);
        for k in 1..(N_BINS - 1) {
            freq_energy += 2.0 * ((spec.re[k] as f64).powi(2) + (spec.im[k] as f64).powi(2));
        }
        freq_energy /= CONVOLVER_FFT_SIZE as f64;
        assert!(
            (time_energy - freq_energy).abs() < 1e-3,
            "Parseval mismatch: time={time_energy}, freq={freq_energy}"
        );

        fft.inverse_real(&spec, &mut y, &mut sr, &mut si);
        let max_err = x
            .iter()
            .zip(y.iter())
            .map(|(a, b)| (a - b).abs())
            .fold(0.0f32, f32::max);
        assert!(max_err < 1e-5, "FFT roundtrip error too high: {max_err}");
    }

    #[test]
    fn test_upols_parity_512_tap_arbitrary_chunks() {
        run_parity_check(512, &[37, 64, 128, 256, 257], false);
        run_parity_check(512, &[128, 256], true);
    }

    #[test]
    fn test_upols_parity_4096_tap_arbitrary_chunks() {
        run_parity_check(4096, &[37, 64, 128, 256, 257], false);
        run_parity_check(4096, &[128, 256, 257], true);
    }

    #[test]
    fn test_identity_bypass_slots_and_reset() {
        let mut conv = PartitionedConvolver::new();
        assert_eq!(conv.active_partitions(), 1);
        assert_eq!(conv.latency_samples(), CONVOLVER_BLOCK_SIZE);

        conv.set_bypass(true);
        assert!(conv.is_bypassed());
        assert_eq!(conv.latency_samples(), 0);
        let mut l = vec![0.25f32; 64];
        let mut r = vec![-0.75f32; 64];
        conv.process_block(&mut l, &mut r);
        assert_eq!(l[0], 0.25);
        assert_eq!(r[0], -0.75);
        conv.set_bypass(false);

        conv.load_impulse_slot(1, &[-2.0], &[0.5]);
        conv.load_impulse_slot(2, &[0.0, 0.0, 0.0, 1.0], &[0.0, 1.0]);
        conv.select_slot(1);
        assert_eq!(conv.active_partitions(), 1);

        let n_imp = CONVOLVER_BLOCK_SIZE * 2;
        let mut imp_l = vec![0.0f32; n_imp];
        let mut imp_r = vec![0.0f32; n_imp];
        imp_l[0] = 1.0;
        imp_r[0] = 1.0;
        conv.process_block(&mut imp_l, &mut imp_r);
        assert!((imp_l[CONVOLVER_BLOCK_SIZE] - (-2.0)).abs() < 1e-5);
        assert!((imp_r[CONVOLVER_BLOCK_SIZE] - 0.5).abs() < 1e-5);

        conv.reset();
        let mut z_l = vec![0.0f32; n_imp];
        let mut z_r = vec![0.0f32; n_imp];
        z_l[5] = 1.0;
        z_r[5] = 1.0;
        conv.process_block(&mut z_l, &mut z_r);
        assert!((z_l[CONVOLVER_BLOCK_SIZE + 5] - (-2.0)).abs() < 1e-5);
        assert!((z_r[CONVOLVER_BLOCK_SIZE + 5] - 0.5).abs() < 1e-5);
    }
}
