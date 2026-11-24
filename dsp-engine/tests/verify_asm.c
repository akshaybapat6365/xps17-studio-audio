/*
 * Standalone C Verification & Benchmark Harness for xps17_avx2_kernels.S
 *
 * Verifies all 5 hand-tuned System V AMD64 AVX2/FMA GNU Assembly kernels against
 * strict non-vectorized C scalar references across edge-case lengths, unaligned
 * buffers, denormal values, full-scale audio, and multi-block IIR state continuity.
 *
 * Build & run on E-cores:
 *   gcc -O3 -mavx2 -mfma -fPIC -Wall -Wextra -Werror \
 *       tests/verify_asm.c asm/xps17_avx2_kernels.S -lm -o /tmp/verify_asm
 *   taskset -c 12-19 nice -n 19 /tmp/verify_asm
 */

#define _POSIX_C_SOURCE 200809L

#include <immintrin.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* ---- External AVX2/FMA Assembly Kernels ---- */
extern float xps17_avx2_fir_dot_f32(const float *a, const float *b, size_t n);

extern void xps17_avx2_cmul_acc_f32(
    float *acc_re, float *acc_im,
    const float *x_re, const float *x_im,
    const float *h_re, const float *h_im,
    size_t n);

extern void xps17_avx2_chebyshev_bass_f32(
    const float *input, float *output, size_t n,
    float drive, float w2, float w3, float w4);

extern float xps17_avx2_true_peak_4x_f32(
    const float *padded, float *peaks_out, size_t n);

extern void xps17_avx2_svf_bank8_f32(
    const float *in8, float *lp8, float *bp8, float *hp8,
    const float *coeffs_a1_a2_a3_k, float *state_ic1_ic2, size_t frames);

/* ---- Strict Non-Vectorized C Scalar Reference Implementations ---- */
__attribute__((noinline, optimize("no-tree-vectorize")))
static float scalar_fir_dot_f32(const float *a, const float *b, size_t n) {
    float acc = 0.0f;
    for (size_t i = 0; i < n; ++i) {
        acc += a[i] * b[i];
    }
    return acc;
}

__attribute__((noinline, optimize("no-tree-vectorize")))
static void scalar_cmul_acc_f32(
    float *acc_re, float *acc_im,
    const float *x_re, const float *x_im,
    const float *h_re, const float *h_im,
    size_t n)
{
    for (size_t k = 0; k < n; ++k) {
        float xr = x_re[k];
        float xi = x_im[k];
        float hr = h_re[k];
        float hi = h_im[k];
        acc_re[k] += xr * hr - xi * hi;
        acc_im[k] += xr * hi + xi * hr;
    }
}

__attribute__((noinline, optimize("no-tree-vectorize")))
static void scalar_chebyshev_bass_f32(
    const float *input, float *output, size_t n,
    float drive, float w2, float w3, float w4)
{
    for (size_t i = 0; i < n; ++i) {
        float u = input[i] * drive;
        float s = u / sqrtf(1.0f + u * u);
        float s2 = s * s;
        float t2_ac = 2.0f * s2;
        float t3 = s * (4.0f * s2 - 3.0f);
        float t4_ac = s2 * (8.0f * s2 - 8.0f);
        output[i] = w2 * t2_ac + w3 * t3 + w4 * t4_ac;
    }
}

__attribute__((noinline, optimize("no-tree-vectorize")))
static float scalar_true_peak_4x_f32(
    const float *padded, float *peaks_out, size_t n)
{
    float max_peak = 0.0f;
    for (size_t i = 0; i < n; ++i) {
        float p0 = padded[i];
        float p1 = padded[i + 1];
        float p2 = padded[i + 2];
        float p3 = padded[i + 3];

        float y0 = p1;
        float y1 = -0.0703125f * p0 + 0.8671875f * p1 + 0.2265625f * p2 - 0.0234375f * p3;
        float y2 = -0.0625f * p0    + 0.5625f * p1    + 0.5625f * p2    - 0.0625f * p3;
        float y3 = -0.0234375f * p0 + 0.2265625f * p1 + 0.8671875f * p2 - 0.0703125f * p3;

        float a0 = fabsf(y0);
        float a1 = fabsf(y1);
        float a2 = fabsf(y2);
        float a3 = fabsf(y3);
        float peak = a0 > a1 ? a0 : a1;
        if (a2 > peak) peak = a2;
        if (a3 > peak) peak = a3;

        peaks_out[i] = peak;
        if (peak > max_peak) max_peak = peak;
    }
    return max_peak;
}

__attribute__((noinline, optimize("no-tree-vectorize")))
static void scalar_svf_bank8_f32(
    const float *in8, float *lp8, float *bp8, float *hp8,
    const float *coeffs, float *state, size_t frames)
{
    for (size_t ch = 0; ch < 8; ++ch) {
        float a1 = coeffs[0 * 8 + ch];
        float a2 = coeffs[1 * 8 + ch];
        float a3 = coeffs[2 * 8 + ch];
        float k  = coeffs[3 * 8 + ch];
        float ic1 = state[0 * 8 + ch];
        float ic2 = state[1 * 8 + ch];
        for (size_t t = 0; t < frames; ++t) {
            size_t idx = t * 8 + ch;
            float v0 = in8[idx];
            float v3 = v0 - ic2;
            float v1 = a1 * ic1 + a2 * v3;
            float v2 = ic2 + a2 * ic1 + a3 * v3;
            ic1 = 2.0f * v1 - ic1;
            ic2 = 2.0f * v2 - ic2;
            float hp = v0 - k * v1 - v2;
            lp8[idx] = v2;
            bp8[idx] = v1;
            hp8[idx] = hp;
        }
        state[0 * 8 + ch] = ic1;
        state[1 * 8 + ch] = ic2;
    }
}

/* ---- Helpers ---- */
static uint64_t rng_state = 0x9e3779b97f4a7c15ULL;

static float rng_f32(void) {
    uint64_t x = rng_state;
    x ^= x >> 12;
    x ^= x << 25;
    x ^= x >> 27;
    rng_state = x;
    uint64_t v = x * 0x2545f4914f6cdd1dULL;
    float u = (float)(v >> 40) * (1.0f / 16777216.0f);
    return u * 2.0f - 1.0f;
}

static void fill_rand(float *buf, size_t n, float scale) {
    for (size_t i = 0; i < n; ++i) {
        buf[i] = rng_f32() * scale;
    }
}

static double now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec * 1e9 + (double)ts.tv_nsec;
}

static void make_svf_coeffs(float coeffs[32]) {
    const float freqs[8] = {65.0f, 110.0f, 220.0f, 500.0f, 1200.0f, 2800.0f, 6160.0f, 12000.0f};
    const float qs[8]    = {0.707f, 1.414f, 0.5f, 2.0f, 0.707f, 1.5f, 0.707f, 0.9f};
    const float fs = 48000.0f;
    for (int ch = 0; ch < 8; ++ch) {
        float g = tanf(3.14159265358979323846f * freqs[ch] / fs);
        float k = 1.0f / qs[ch];
        float a1 = 1.0f / (1.0f + g * (g + k));
        float a2 = g * a1;
        float a3 = g * a2;
        coeffs[0 * 8 + ch] = a1;
        coeffs[1 * 8 + ch] = a2;
        coeffs[2 * 8 + ch] = a3;
        coeffs[3 * 8 + ch] = k;
    }
}

#define CHECK_ERR(label, err) do { \
    if (!isfinite(err) || (err) >= 1e-5f) { \
        fprintf(stderr, "FAIL [%s]: max_abs_err = %.9g (>= 1e-5)\n", (label), (double)(err)); \
        return 1; \
    } \
} while (0)

int main(void) {
    const size_t edge_lens[] = {
        0, 1, 2, 3, 7, 8, 9, 15, 16, 17, 31, 32, 33, 63, 64, 65, 128, 129, 256, 513
    };
    const size_t num_edges = sizeof(edge_lens) / sizeof(edge_lens[0]);

    /* Enable FTZ/DAZ just as the real-time audio engine does */
    unsigned int prev_mxcsr = _mm_getcsr();
    _mm_setcsr(prev_mxcsr | 0x8040u);

    float max_err_fir  = 0.0f;
    float max_err_cmul = 0.0f;
    float max_err_cheb = 0.0f;
    float max_err_tp   = 0.0f;
    float max_err_svf  = 0.0f;

    /* ========================================================================
     * CORRECTNESS VERIFICATION (edge lengths, unaligned ptrs, denormals, full-scale)
     * ======================================================================== */

    /* 1. fir_dot_f32 */
    {
        float a[600], b[600];
        fill_rand(a, 600, 1.0f);
        fill_rand(b, 600, 1.0f);
        for (size_t e = 0; e < num_edges; ++e) {
            size_t n = edge_lens[e];
            for (size_t off = 0; off < 4; ++off) {
                float got = xps17_avx2_fir_dot_f32(a + off, b + off + 1, n);
                float ref = scalar_fir_dot_f32(a + off, b + off + 1, n);
                float err = fabsf(got - ref);
                if (err > max_err_fir) max_err_fir = err;
            }
        }
        /* Denormal & full-scale */
        for (size_t i = 0; i < 131; ++i) {
            a[i] = (i & 1) ? 1e-40f : -1e-42f;
            b[i] = 1e-40f;
        }
        float d_err = fabsf(xps17_avx2_fir_dot_f32(a, b, 131) - scalar_fir_dot_f32(a, b, 131));
        if (d_err > max_err_fir) max_err_fir = d_err;

        for (size_t i = 0; i < 513; ++i) {
            a[i] = 1.0f;
            b[i] = (i & 1) ? -1.0f : 1.0f;
        }
        float fs_err = fabsf(xps17_avx2_fir_dot_f32(a, b, 513) - 1.0f);
        if (fs_err > max_err_fir) max_err_fir = fs_err;
        CHECK_ERR("fir_dot_f32", max_err_fir);
    }

    /* 2. cmul_acc_f32 */
    {
        float xr[600], xi[600], hr[600], hi[600], init_r[600], init_i[600];
        float ar_avx[600], ai_avx[600], ar_ref[600], ai_ref[600];
        fill_rand(xr, 600, 1.0f);
        fill_rand(xi, 600, 1.0f);
        fill_rand(hr, 600, 1.0f);
        fill_rand(hi, 600, 1.0f);
        fill_rand(init_r, 600, 1.0f);
        fill_rand(init_i, 600, 1.0f);

        for (size_t e = 0; e < num_edges; ++e) {
            size_t n = edge_lens[e];
            for (size_t off = 0; off < 4; ++off) {
                memcpy(ar_avx, init_r, sizeof(init_r));
                memcpy(ai_avx, init_i, sizeof(init_i));
                memcpy(ar_ref, init_r, sizeof(init_r));
                memcpy(ai_ref, init_i, sizeof(init_i));

                xps17_avx2_cmul_acc_f32(
                    ar_avx + off, ai_avx + off,
                    xr + off, xi + off, hr + off, hi + off, n);
                scalar_cmul_acc_f32(
                    ar_ref + off, ai_ref + off,
                    xr + off, xi + off, hr + off, hi + off, n);

                for (size_t k = 0; k < n; ++k) {
                    float er = fabsf(ar_avx[off + k] - ar_ref[off + k]);
                    float ei = fabsf(ai_avx[off + k] - ai_ref[off + k]);
                    if (er > max_err_cmul) max_err_cmul = er;
                    if (ei > max_err_cmul) max_err_cmul = ei;
                }
            }
        }
        CHECK_ERR("cmul_acc_f32", max_err_cmul);
    }

    /* 3. chebyshev_bass_f32 */
    {
        float inp[600], out_avx[600], out_ref[600];
        fill_rand(inp, 600, 2.0f);
        const float drive = 3.2f, w2 = 0.6f, w3 = 0.35f, w4 = 0.15f;
        for (size_t e = 0; e < num_edges; ++e) {
            size_t n = edge_lens[e];
            for (size_t off = 0; off < 4; ++off) {
                xps17_avx2_chebyshev_bass_f32(inp + off, out_avx + off, n, drive, w2, w3, w4);
                scalar_chebyshev_bass_f32(inp + off, out_ref + off, n, drive, w2, w3, w4);
                for (size_t i = 0; i < n; ++i) {
                    float err = fabsf(out_avx[off + i] - out_ref[off + i]);
                    if (err > max_err_cheb) max_err_cheb = err;
                }
            }
        }
        /* Zero-DC + denormal + full-scale checks */
        const float special[17] = {
            0.0f, -0.0f, 1e-42f, -1e-40f, 1e-20f, -1e-20f, 0.5f, -0.5f,
            1.0f, -1.0f, 4.0f, -4.0f, 0.0f, 0.25f, -0.25f, 0.75f, -0.75f
        };
        xps17_avx2_chebyshev_bass_f32(special, out_avx, 17, drive, w2, w3, w4);
        scalar_chebyshev_bass_f32(special, out_ref, 17, drive, w2, w3, w4);
        if (out_avx[0] != 0.0f) {
            fprintf(stderr, "FAIL [chebyshev_bass_f32]: non-zero DC at silence: %g\n", out_avx[0]);
            return 1;
        }
        for (size_t i = 0; i < 17; ++i) {
            float err = fabsf(out_avx[i] - out_ref[i]);
            if (err > max_err_cheb) max_err_cheb = err;
        }
        CHECK_ERR("chebyshev_bass_f32", max_err_cheb);
    }

    /* 4. true_peak_4x_f32 */
    {
        float padded[610], pk_avx[600], pk_ref[600];
        fill_rand(padded, 610, 1.5f);
        for (size_t e = 0; e < num_edges; ++e) {
            size_t n = edge_lens[e];
            for (size_t off = 0; off < 4; ++off) {
                float m_avx = xps17_avx2_true_peak_4x_f32(padded + off, pk_avx + off, n);
                float m_ref = scalar_true_peak_4x_f32(padded + off, pk_ref + off, n);
                float merr = fabsf(m_avx - m_ref);
                if (merr > max_err_tp) max_err_tp = merr;
                for (size_t i = 0; i < n; ++i) {
                    float err = fabsf(pk_avx[off + i] - pk_ref[off + i]);
                    if (err > max_err_tp) max_err_tp = err;
                }
            }
        }
        /* Inter-sample overshoot check: [-1, +1, +1, -1] => exact midpoint 1.25 */
        const float isp[4] = {-1.0f, 1.0f, 1.0f, -1.0f};
        float p_one = 0.0f;
        float m_isp = xps17_avx2_true_peak_4x_f32(isp, &p_one, 1);
        float isp_err = fabsf(m_isp - 1.25f);
        if (isp_err > max_err_tp) max_err_tp = isp_err;
        CHECK_ERR("true_peak_4x_f32", max_err_tp);
    }

    /* 5. svf_bank8_f32 */
    {
        float coeffs[32];
        make_svf_coeffs(coeffs);
        float *in8 = calloc(600 * 8, sizeof(float));
        float *lp_avx = calloc(600 * 8, sizeof(float));
        float *bp_avx = calloc(600 * 8, sizeof(float));
        float *hp_avx = calloc(600 * 8, sizeof(float));
        float *lp_ref = calloc(600 * 8, sizeof(float));
        float *bp_ref = calloc(600 * 8, sizeof(float));
        float *hp_ref = calloc(600 * 8, sizeof(float));
        fill_rand(in8, 600 * 8, 1.0f);

        for (size_t e = 0; e < num_edges; ++e) {
            size_t frames = edge_lens[e];
            float st_avx[16], st_ref[16];
            for (int i = 0; i < 16; ++i) {
                st_avx[i] = (i < 8) ? 0.01f : -0.02f;
                st_ref[i] = st_avx[i];
            }
            xps17_avx2_svf_bank8_f32(in8, lp_avx, bp_avx, hp_avx, coeffs, st_avx, frames);
            scalar_svf_bank8_f32(in8, lp_ref, bp_ref, hp_ref, coeffs, st_ref, frames);

            for (size_t i = 0; i < frames * 8; ++i) {
                float el = fabsf(lp_avx[i] - lp_ref[i]);
                float eb = fabsf(bp_avx[i] - bp_ref[i]);
                float eh = fabsf(hp_avx[i] - hp_ref[i]);
                if (el > max_err_svf) max_err_svf = el;
                if (eb > max_err_svf) max_err_svf = eb;
                if (eh > max_err_svf) max_err_svf = eh;
            }
            for (int i = 0; i < 16; ++i) {
                float es = fabsf(st_avx[i] - st_ref[i]);
                if (es > max_err_svf) max_err_svf = es;
            }
        }
        free(in8);
        free(lp_avx); free(bp_avx); free(hp_avx);
        free(lp_ref); free(bp_ref); free(hp_ref);
        CHECK_ERR("svf_bank8_f32", max_err_svf);
    }

    /* ========================================================================
     * PERFORMANCE BENCHMARKS (AVX2/FMA Assembly vs Strict Scalar C)
     * ======================================================================== */
    printf("=== XPS 17 9720 x86_64 AVX2/FMA Kernel Verification & Benchmark ===\n");
    printf("%-28s | %10s | %10s | %8s | %12s\n",
           "Kernel", "AVX2 (ns)", "Scalar(ns)", "Speedup", "Max Abs Err");
    printf("-----------------------------+------------+------------+----------+--------------\n");

    volatile float sink = 0.0f;

    /* Bench 1: fir_dot_f32 (n = 1024) */
    {
        enum { N = 1024, ITERS = 100000 };
        float a[N], b[N];
        fill_rand(a, N, 1.0f);
        fill_rand(b, N, 1.0f);
        for (int i = 0; i < 5000; ++i) sink += xps17_avx2_fir_dot_f32(a, b, N);
        double t0 = now_ns();
        for (int i = 0; i < ITERS; ++i) {
            sink = xps17_avx2_fir_dot_f32(a, b, N);
            __asm__ volatile("" : "+m"(a), "+m"(b) :: "memory");
        }
        double avx_ns = (now_ns() - t0) / ITERS;

        for (int i = 0; i < 5000; ++i) sink += scalar_fir_dot_f32(a, b, N);
        double t1 = now_ns();
        for (int i = 0; i < ITERS; ++i) {
            sink = scalar_fir_dot_f32(a, b, N);
            __asm__ volatile("" : "+m"(a), "+m"(b) :: "memory");
        }
        double sca_ns = (now_ns() - t1) / ITERS;
        printf("%-28s | %10.2f | %10.2f | %7.2fx | %12.3e\n",
               "fir_dot_f32 (n=1024)", avx_ns, sca_ns, sca_ns / avx_ns, (double)max_err_fir);
    }

    /* Bench 2: cmul_acc_f32 (n = 129) */
    {
        enum { N = 129, ITERS = 200000 };
        float ar[N], ai[N], xr[N], xi[N], hr[N], hi[N];
        memset(ar, 0, sizeof(ar));
        memset(ai, 0, sizeof(ai));
        fill_rand(xr, N, 0.01f);
        fill_rand(xi, N, 0.01f);
        fill_rand(hr, N, 0.01f);
        fill_rand(hi, N, 0.01f);

        for (int i = 0; i < 5000; ++i) xps17_avx2_cmul_acc_f32(ar, ai, xr, xi, hr, hi, N);
        memset(ar, 0, sizeof(ar)); memset(ai, 0, sizeof(ai));
        double t0 = now_ns();
        for (int i = 0; i < ITERS; ++i) {
            xps17_avx2_cmul_acc_f32(ar, ai, xr, xi, hr, hi, N);
            __asm__ volatile("" : "+m"(ar), "+m"(ai) :: "memory");
        }
        double avx_ns = (now_ns() - t0) / ITERS;
        sink += ar[0] + ai[0];

        memset(ar, 0, sizeof(ar)); memset(ai, 0, sizeof(ai));
        for (int i = 0; i < 5000; ++i) scalar_cmul_acc_f32(ar, ai, xr, xi, hr, hi, N);
        memset(ar, 0, sizeof(ar)); memset(ai, 0, sizeof(ai));
        double t1 = now_ns();
        for (int i = 0; i < ITERS; ++i) {
            scalar_cmul_acc_f32(ar, ai, xr, xi, hr, hi, N);
            __asm__ volatile("" : "+m"(ar), "+m"(ai) :: "memory");
        }
        double sca_ns = (now_ns() - t1) / ITERS;
        sink += ar[0] + ai[0];
        printf("%-28s | %10.2f | %10.2f | %7.2fx | %12.3e\n",
               "cmul_acc_f32 (n=129)", avx_ns, sca_ns, sca_ns / avx_ns, (double)max_err_cmul);
    }

    /* Bench 3: chebyshev_bass_f32 (n = 256) */
    {
        enum { N = 256, ITERS = 100000 };
        float inp[N], out[N];
        fill_rand(inp, N, 1.0f);
        const float drive = 2.8f, w2 = 0.55f, w3 = 0.28f, w4 = 0.12f;

        for (int i = 0; i < 5000; ++i) xps17_avx2_chebyshev_bass_f32(inp, out, N, drive, w2, w3, w4);
        double t0 = now_ns();
        for (int i = 0; i < ITERS; ++i) {
            xps17_avx2_chebyshev_bass_f32(inp, out, N, drive, w2, w3, w4);
            __asm__ volatile("" : "+m"(out) :: "memory");
        }
        double avx_ns = (now_ns() - t0) / ITERS;
        sink += out[0];

        for (int i = 0; i < 5000; ++i) scalar_chebyshev_bass_f32(inp, out, N, drive, w2, w3, w4);
        double t1 = now_ns();
        for (int i = 0; i < ITERS; ++i) {
            scalar_chebyshev_bass_f32(inp, out, N, drive, w2, w3, w4);
            __asm__ volatile("" : "+m"(out) :: "memory");
        }
        double sca_ns = (now_ns() - t1) / ITERS;
        sink += out[0];
        printf("%-28s | %10.2f | %10.2f | %7.2fx | %12.3e\n",
               "chebyshev_bass_f32 (n=256)", avx_ns, sca_ns, sca_ns / avx_ns, (double)max_err_cheb);
    }

    /* Bench 4: true_peak_4x_f32 (n = 256) */
    {
        enum { N = 256, ITERS = 150000 };
        float padded[N + 3], peaks[N];
        fill_rand(padded, N + 3, 1.0f);

        for (int i = 0; i < 5000; ++i) sink = xps17_avx2_true_peak_4x_f32(padded, peaks, N);
        double t0 = now_ns();
        for (int i = 0; i < ITERS; ++i) {
            sink = xps17_avx2_true_peak_4x_f32(padded, peaks, N);
            __asm__ volatile("" : "+m"(peaks) :: "memory");
        }
        double avx_ns = (now_ns() - t0) / ITERS;

        for (int i = 0; i < 5000; ++i) sink = scalar_true_peak_4x_f32(padded, peaks, N);
        double t1 = now_ns();
        for (int i = 0; i < ITERS; ++i) {
            sink = scalar_true_peak_4x_f32(padded, peaks, N);
            __asm__ volatile("" : "+m"(peaks) :: "memory");
        }
        double sca_ns = (now_ns() - t1) / ITERS;
        printf("%-28s | %10.2f | %10.2f | %7.2fx | %12.3e\n",
               "true_peak_4x_f32 (n=256)", avx_ns, sca_ns, sca_ns / avx_ns, (double)max_err_tp);
    }

    /* Bench 5: svf_bank8_f32 (frames = 256, 8 ch) */
    {
        enum { FRAMES = 256, ITERS = 40000 };
        float coeffs[32], st[16] = {0};
        make_svf_coeffs(coeffs);
        float in8[FRAMES * 8], lp8[FRAMES * 8], bp8[FRAMES * 8], hp8[FRAMES * 8];
        fill_rand(in8, FRAMES * 8, 1.0f);

        for (int i = 0; i < 2000; ++i) xps17_avx2_svf_bank8_f32(in8, lp8, bp8, hp8, coeffs, st, FRAMES);
        double t0 = now_ns();
        for (int i = 0; i < ITERS; ++i) {
            xps17_avx2_svf_bank8_f32(in8, lp8, bp8, hp8, coeffs, st, FRAMES);
            __asm__ volatile("" : "+m"(lp8), "+m"(st) :: "memory");
        }
        double avx_ns = (now_ns() - t0) / ITERS;
        sink += lp8[0] + st[0];

        memset(st, 0, sizeof(st));
        for (int i = 0; i < 2000; ++i) scalar_svf_bank8_f32(in8, lp8, bp8, hp8, coeffs, st, FRAMES);
        double t1 = now_ns();
        for (int i = 0; i < ITERS; ++i) {
            scalar_svf_bank8_f32(in8, lp8, bp8, hp8, coeffs, st, FRAMES);
            __asm__ volatile("" : "+m"(lp8), "+m"(st) :: "memory");
        }
        double sca_ns = (now_ns() - t1) / ITERS;
        sink += lp8[0] + st[0];
        printf("%-28s | %10.2f | %10.2f | %7.2fx | %12.3e\n",
               "svf_bank8_f32 (256x8)", avx_ns, sca_ns, sca_ns / avx_ns, (double)max_err_svf);
    }

    (void)sink;
    _mm_setcsr(prev_mxcsr);
    printf("ALL 5 AVX2/FMA KERNELS PASSED (< 1e-5 max abs error).\n");
    return 0;
}
