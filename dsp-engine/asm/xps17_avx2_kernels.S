/*
 * Dell XPS 17 9720 Studio Audio DSP Engine — Hand-Tuned x86_64 AVX2/FMA Kernels
 *
 * System V AMD64 ABI compliant GNU Assembly (.S), 100% PIC-clean (RIP-relative .rodata),
 * zero callee-saved register spills, zero heap/stack allocations, vzeroupper before every ret.
 *
 * Exported symbols:
 *   1. float xps17_avx2_fir_dot_f32(const float *a, const float *b, size_t n);
 *   2. void  xps17_avx2_cmul_acc_f32(float *acc_re, float *acc_im,
 *                                    const float *x_re, const float *x_im,
 *                                    const float *h_re, const float *h_im, size_t n);
 *   3. void  xps17_avx2_chebyshev_bass_f32(const float *input, float *output, size_t n,
 *                                          float drive, float w2, float w3, float w4);
 *   4. float xps17_avx2_true_peak_4x_f32(const float *padded, float *peaks_out, size_t n);
 *   5. void  xps17_avx2_svf_bank8_f32(const float *in8, float *lp8, float *bp8, float *hp8,
 *                                     const float *coeffs_a1_a2_a3_k,
 *                                     float *state_ic1_ic2, size_t frames);
 */

    .intel_syntax noprefix

    .section .rodata
    .p2align 4
.LC_one:
    .float 1.0
.LC_two:
    .float 2.0
.LC_three:
    .float 3.0
.LC_four:
    .float 4.0
.LC_eight:
    .float 8.0
.LC_tp_a:
    .float 0.0703125
.LC_tp_b:
    .float 0.8671875
.LC_tp_c:
    .float 0.2265625
.LC_tp_d:
    .float 0.0234375
.LC_tp_e:
    .float 0.5625
.LC_tp_f:
    .float 0.0625
.LC_abs_mask:
    .long 0x7fffffff

    .text

/* ============================================================================
 * 1. float xps17_avx2_fir_dot_f32(const float *a, const float *b, size_t n)
 *    Args: rdi = a, rsi = b, rdx = n. Return: xmm0.
 *    4x unrolled (32 floats/iter) with 4 independent YMM FMA accumulators to
 *    hide 4-cycle FMA latency on Intel Alder Lake, plus 8-wide & scalar tails.
 * ============================================================================ */
    .globl xps17_avx2_fir_dot_f32
    .type  xps17_avx2_fir_dot_f32, @function
    .p2align 4
xps17_avx2_fir_dot_f32:
    .cfi_startproc
    endbr64
    vxorps       xmm0, xmm0, xmm0
    test         rdx, rdx
    jz           .Lfir_done

    cmp          rdx, 32
    jb           .Lfir_tail8_check

    vxorps       xmm1, xmm1, xmm1
    vxorps       xmm2, xmm2, xmm2
    vxorps       xmm3, xmm3, xmm3

    .p2align 4
.Lfir_loop32:
    vmovups      ymm4, ymmword ptr [rdi]
    vmovups      ymm5, ymmword ptr [rdi + 32]
    vmovups      ymm6, ymmword ptr [rdi + 64]
    vmovups      ymm7, ymmword ptr [rdi + 96]
    vfmadd231ps  ymm0, ymm4, ymmword ptr [rsi]
    vfmadd231ps  ymm1, ymm5, ymmword ptr [rsi + 32]
    vfmadd231ps  ymm2, ymm6, ymmword ptr [rsi + 64]
    vfmadd231ps  ymm3, ymm7, ymmword ptr [rsi + 96]
    add          rdi, 128
    add          rsi, 128
    sub          rdx, 32
    cmp          rdx, 32
    jae          .Lfir_loop32

    vaddps       ymm0, ymm0, ymm1
    vaddps       ymm2, ymm2, ymm3
    vaddps       ymm0, ymm0, ymm2

.Lfir_tail8_check:
    cmp          rdx, 8
    jb           .Lfir_reduce

    .p2align 4
.Lfir_loop8:
    vmovups      ymm4, ymmword ptr [rdi]
    vfmadd231ps  ymm0, ymm4, ymmword ptr [rsi]
    add          rdi, 32
    add          rsi, 32
    sub          rdx, 8
    cmp          rdx, 8
    jae          .Lfir_loop8

.Lfir_reduce:
    vextractf128 xmm1, ymm0, 1
    vaddps       xmm0, xmm0, xmm1
    vshufps      xmm1, xmm0, xmm0, 0x4e
    vaddps       xmm0, xmm0, xmm1
    vshufps      xmm1, xmm0, xmm0, 0xb1
    vaddss       xmm0, xmm0, xmm1

    test         rdx, rdx
    jz           .Lfir_done

    .p2align 4
.Lfir_tail1:
    vmovss       xmm1, dword ptr [rdi]
    vfmadd231ss  xmm0, xmm1, dword ptr [rsi]
    add          rdi, 4
    add          rsi, 4
    dec          rdx
    jnz          .Lfir_tail1

.Lfir_done:
    vzeroupper
    ret
    .cfi_endproc
    .size xps17_avx2_fir_dot_f32, .-xps17_avx2_fir_dot_f32


/* ============================================================================
 * 2. void xps17_avx2_cmul_acc_f32(float *acc_re, float *acc_im,
 *                                 const float *x_re, const float *x_im,
 *                                 const float *h_re, const float *h_im,
 *                                 size_t n)
 *    Args: rdi=acc_re, rsi=acc_im, rdx=x_re, rcx=x_im, r8=h_re, r9=h_im,
 *          [rsp + 8] = n.
 *    Computes complex multiply-accumulate for partitioned FFT convolution
 *    (8-wide AVX2/FMA main loop + scalar tail for e.g. n = 129 bins).
 * ============================================================================ */
    .globl xps17_avx2_cmul_acc_f32
    .type  xps17_avx2_cmul_acc_f32, @function
    .p2align 4
xps17_avx2_cmul_acc_f32:
    .cfi_startproc
    endbr64
    mov          rax, qword ptr [rsp + 8]
    test         rax, rax
    jz           .Lcmul_done

    xor          r10d, r10d
    cmp          rax, 8
    jb           .Lcmul_tail1_check

    .p2align 4
.Lcmul_loop8:
    vmovups      ymm0, ymmword ptr [rdi + r10]
    vmovups      ymm1, ymmword ptr [rsi + r10]
    vmovups      ymm2, ymmword ptr [rdx + r10]
    vmovups      ymm3, ymmword ptr [rcx + r10]
    vmovups      ymm4, ymmword ptr [r8  + r10]
    vmovups      ymm5, ymmword ptr [r9  + r10]
    vfmadd231ps  ymm0, ymm2, ymm4
    vfmadd231ps  ymm1, ymm2, ymm5
    vfnmadd231ps ymm0, ymm3, ymm5
    vfmadd231ps  ymm1, ymm3, ymm4
    vmovups      ymmword ptr [rdi + r10], ymm0
    vmovups      ymmword ptr [rsi + r10], ymm1
    add          r10, 32
    sub          rax, 8
    cmp          rax, 8
    jae          .Lcmul_loop8

.Lcmul_tail1_check:
    test         rax, rax
    jz           .Lcmul_done

    .p2align 4
.Lcmul_tail1:
    vmovss       xmm0, dword ptr [rdi + r10]
    vmovss       xmm1, dword ptr [rsi + r10]
    vmovss       xmm2, dword ptr [rdx + r10]
    vmovss       xmm3, dword ptr [rcx + r10]
    vmovss       xmm4, dword ptr [r8  + r10]
    vmovss       xmm5, dword ptr [r9  + r10]
    vfmadd231ss  xmm0, xmm2, xmm4
    vfmadd231ss  xmm1, xmm2, xmm5
    vfnmadd231ss xmm0, xmm3, xmm5
    vfmadd231ss  xmm1, xmm3, xmm4
    vmovss       dword ptr [rdi + r10], xmm0
    vmovss       dword ptr [rsi + r10], xmm1
    add          r10, 4
    dec          rax
    jnz          .Lcmul_tail1

.Lcmul_done:
    vzeroupper
    ret
    .cfi_endproc
    .size xps17_avx2_cmul_acc_f32, .-xps17_avx2_cmul_acc_f32


/* ============================================================================
 * 3. void xps17_avx2_chebyshev_bass_f32(const float *input, float *output,
 *                                       size_t n, float drive,
 *                                       float w2, float w3, float w4)
 *    Args: rdi=input, rsi=output, rdx=n, xmm0=drive, xmm1=w2, xmm2=w3, xmm3=w4.
 *    Computes zero-DC 2nd/3rd/4th Chebyshev harmonic synthesis on algebraically
 *    soft-clipped signal s = u / sqrt(1 + u^2) using exact IEEE-754 vsqrtps+vdivps.
 *    2x interleaved 16-wide loop hides sqrt/div latency, + 8-wide & scalar tails.
 * ============================================================================ */
    .globl xps17_avx2_chebyshev_bass_f32
    .type  xps17_avx2_chebyshev_bass_f32, @function
    .p2align 4
xps17_avx2_chebyshev_bass_f32:
    .cfi_startproc
    endbr64
    test         rdx, rdx
    jz           .Lcheb_done

    vbroadcastss ymm0, xmm0
    vbroadcastss ymm1, xmm1
    vbroadcastss ymm2, xmm2
    vbroadcastss ymm3, xmm3
    vbroadcastss ymm4, dword ptr [rip + .LC_one]
    vbroadcastss ymm5, dword ptr [rip + .LC_four]
    vbroadcastss ymm6, dword ptr [rip + .LC_three]
    vbroadcastss ymm7, dword ptr [rip + .LC_eight]

    xor          eax, eax
    cmp          rdx, 16
    jb           .Lcheb_tail8_check

    .p2align 4
.Lcheb_loop16:
    vmulps       ymm8,  ymm0, ymmword ptr [rdi + rax]
    vmulps       ymm12, ymm0, ymmword ptr [rdi + rax + 32]
    vmovaps      ymm9,  ymm4
    vmovaps      ymm13, ymm4
    vfmadd231ps  ymm9,  ymm8,  ymm8
    vfmadd231ps  ymm13, ymm12, ymm12
    vsqrtps      ymm9,  ymm9
    vsqrtps      ymm13, ymm13
    vdivps       ymm8,  ymm8,  ymm9
    vdivps       ymm12, ymm12, ymm13

    vmulps       ymm9,  ymm8,  ymm8
    vmulps       ymm13, ymm12, ymm12

    vaddps       ymm10, ymm9,  ymm9
    vaddps       ymm14, ymm13, ymm13
    vmulps       ymm10, ymm10, ymm1
    vmulps       ymm14, ymm14, ymm1

    vmovaps      ymm11, ymm9
    vmovaps      ymm15, ymm13
    vfmsub213ps  ymm11, ymm5,  ymm6
    vfmsub213ps  ymm15, ymm5,  ymm6
    vmulps       ymm11, ymm11, ymm8
    vmulps       ymm15, ymm15, ymm12
    vfmadd231ps  ymm10, ymm11, ymm2
    vfmadd231ps  ymm14, ymm15, ymm2

    vmovaps      ymm11, ymm9
    vmovaps      ymm15, ymm13
    vfmsub213ps  ymm11, ymm7,  ymm7
    vfmsub213ps  ymm15, ymm7,  ymm7
    vmulps       ymm11, ymm11, ymm9
    vmulps       ymm15, ymm15, ymm13
    vfmadd231ps  ymm10, ymm11, ymm3
    vfmadd231ps  ymm14, ymm15, ymm3

    vmovups      ymmword ptr [rsi + rax],      ymm10
    vmovups      ymmword ptr [rsi + rax + 32], ymm14
    add          rax, 64
    sub          rdx, 16
    cmp          rdx, 16
    jae          .Lcheb_loop16

.Lcheb_tail8_check:
    cmp          rdx, 8
    jb           .Lcheb_tail1_check

    vmulps       ymm8,  ymm0, ymmword ptr [rdi + rax]
    vmovaps      ymm9,  ymm4
    vfmadd231ps  ymm9,  ymm8,  ymm8
    vsqrtps      ymm9,  ymm9
    vdivps       ymm8,  ymm8,  ymm9
    vmulps       ymm9,  ymm8,  ymm8
    vaddps       ymm10, ymm9,  ymm9
    vmulps       ymm10, ymm10, ymm1
    vmovaps      ymm11, ymm9
    vfmsub213ps  ymm11, ymm5,  ymm6
    vmulps       ymm11, ymm11, ymm8
    vfmadd231ps  ymm10, ymm11, ymm2
    vmovaps      ymm11, ymm9
    vfmsub213ps  ymm11, ymm7,  ymm7
    vmulps       ymm11, ymm11, ymm9
    vfmadd231ps  ymm10, ymm11, ymm3
    vmovups      ymmword ptr [rsi + rax], ymm10
    add          rax, 32
    sub          rdx, 8

.Lcheb_tail1_check:
    test         rdx, rdx
    jz           .Lcheb_done

    .p2align 4
.Lcheb_tail1:
    vmovss       xmm8,  dword ptr [rdi + rax]
    vmulss       xmm8,  xmm8,  xmm0
    vmovaps      xmm9,  xmm4
    vfmadd231ss  xmm9,  xmm8,  xmm8
    vsqrtss      xmm9,  xmm9,  xmm9
    vdivss       xmm8,  xmm8,  xmm9
    vmulss       xmm9,  xmm8,  xmm8
    vaddss       xmm10, xmm9,  xmm9
    vmulss       xmm10, xmm10, xmm1
    vmovaps      xmm11, xmm9
    vfmsub213ss  xmm11, xmm5,  xmm6
    vmulss       xmm11, xmm11, xmm8
    vfmadd231ss  xmm10, xmm11, xmm2
    vmovaps      xmm11, xmm9
    vfmsub213ss  xmm11, xmm7,  xmm7
    vmulss       xmm11, xmm11, xmm9
    vfmadd231ss  xmm10, xmm11, xmm3
    vmovss       dword ptr [rsi + rax], xmm10
    add          rax, 4
    dec          rdx
    jnz          .Lcheb_tail1

.Lcheb_done:
    vzeroupper
    ret
    .cfi_endproc
    .size xps17_avx2_chebyshev_bass_f32, .-xps17_avx2_chebyshev_bass_f32


/* ============================================================================
 * 4. float xps17_avx2_true_peak_4x_f32(const float *padded, float *peaks_out,
 *                                      size_t n)
 *    Args: rdi=padded (len >= n+3), rsi=peaks_out (len >= n), rdx=n.
 *    Return: overall max true-peak in xmm0.
 *    4x oversampled Catmull-Rom cubic true-peak detector (8-wide + scalar tail).
 * ============================================================================ */
    .globl xps17_avx2_true_peak_4x_f32
    .type  xps17_avx2_true_peak_4x_f32, @function
    .p2align 4
xps17_avx2_true_peak_4x_f32:
    .cfi_startproc
    endbr64
    vxorps       xmm0, xmm0, xmm0
    test         rdx, rdx
    jz           .Ltp_done

    vbroadcastss ymm1, dword ptr [rip + .LC_abs_mask]
    vbroadcastss ymm2, dword ptr [rip + .LC_tp_a]
    vbroadcastss ymm3, dword ptr [rip + .LC_tp_b]
    vbroadcastss ymm4, dword ptr [rip + .LC_tp_c]
    vbroadcastss ymm5, dword ptr [rip + .LC_tp_d]
    vbroadcastss ymm6, dword ptr [rip + .LC_tp_e]
    vbroadcastss ymm7, dword ptr [rip + .LC_tp_f]

    xor          eax, eax
    cmp          rdx, 8
    jb           .Ltp_tail1_check

    .p2align 4
.Ltp_loop8:
    vmovups      ymm8,  ymmword ptr [rdi + rax]
    vmovups      ymm9,  ymmword ptr [rdi + rax + 4]
    vmovups      ymm10, ymmword ptr [rdi + rax + 8]
    vmovups      ymm11, ymmword ptr [rdi + rax + 12]

    vmulps       ymm12, ymm9,  ymm3
    vfmadd231ps  ymm12, ymm10, ymm4
    vfnmadd231ps ymm12, ymm8,  ymm2
    vfnmadd231ps ymm12, ymm11, ymm5

    vmulps       ymm13, ymm10, ymm3
    vfmadd231ps  ymm13, ymm9,  ymm4
    vfnmadd231ps ymm13, ymm8,  ymm5
    vfnmadd231ps ymm13, ymm11, ymm2

    vaddps       ymm14, ymm9,  ymm10
    vaddps       ymm15, ymm8,  ymm11
    vmulps       ymm14, ymm14, ymm6
    vfnmadd231ps ymm14, ymm15, ymm7

    vandps       ymm9,  ymm9,  ymm1
    vandps       ymm12, ymm12, ymm1
    vandps       ymm13, ymm13, ymm1
    vandps       ymm14, ymm14, ymm1

    vmaxps       ymm9,  ymm9,  ymm12
    vmaxps       ymm13, ymm13, ymm14
    vmaxps       ymm9,  ymm9,  ymm13

    vmovups      ymmword ptr [rsi + rax], ymm9
    vmaxps       ymm0,  ymm0,  ymm9

    add          rax, 32
    sub          rdx, 8
    cmp          rdx, 8
    jae          .Ltp_loop8

    vextractf128 xmm8, ymm0, 1
    vmaxps       xmm0, xmm0, xmm8
    vshufps      xmm8, xmm0, xmm0, 0x4e
    vmaxps       xmm0, xmm0, xmm8
    vshufps      xmm8, xmm0, xmm0, 0xb1
    vmaxss       xmm0, xmm0, xmm8

.Ltp_tail1_check:
    test         rdx, rdx
    jz           .Ltp_done

    .p2align 4
.Ltp_tail1:
    vmovss       xmm8,  dword ptr [rdi + rax]
    vmovss       xmm9,  dword ptr [rdi + rax + 4]
    vmovss       xmm10, dword ptr [rdi + rax + 8]
    vmovss       xmm11, dword ptr [rdi + rax + 12]

    vmulss       xmm12, xmm9,  xmm3
    vfmadd231ss  xmm12, xmm10, xmm4
    vfnmadd231ss xmm12, xmm8,  xmm2
    vfnmadd231ss xmm12, xmm11, xmm5

    vmulss       xmm13, xmm10, xmm3
    vfmadd231ss  xmm13, xmm9,  xmm4
    vfnmadd231ss xmm13, xmm8,  xmm5
    vfnmadd231ss xmm13, xmm11, xmm2

    vaddss       xmm14, xmm9,  xmm10
    vaddss       xmm15, xmm8,  xmm11
    vmulss       xmm14, xmm14, xmm6
    vfnmadd231ss xmm14, xmm15, xmm7

    vandps       xmm9,  xmm9,  xmm1
    vandps       xmm12, xmm12, xmm1
    vandps       xmm13, xmm13, xmm1
    vandps       xmm14, xmm14, xmm1

    vmaxss       xmm9,  xmm9,  xmm12
    vmaxss       xmm13, xmm13, xmm14
    vmaxss       xmm9,  xmm9,  xmm13

    vmovss       dword ptr [rsi + rax], xmm9
    vmaxss       xmm0,  xmm0,  xmm9

    add          rax, 4
    dec          rdx
    jnz          .Ltp_tail1

.Ltp_done:
    vzeroupper
    ret
    .cfi_endproc
    .size xps17_avx2_true_peak_4x_f32, .-xps17_avx2_true_peak_4x_f32


/* ============================================================================
 * 5. void xps17_avx2_svf_bank8_f32(const float *in8, float *lp8,
 *                                  float *bp8, float *hp8,
 *                                  const float *coeffs_a1_a2_a3_k,
 *                                  float *state_ic1_ic2, size_t frames)
 *    Args: rdi=in8, rsi=lp8, rdx=bp8, rcx=hp8, r8=coeffs (32 floats),
 *          r9=state (16 floats), [rsp + 8] = frames.
 *    8-channel parallel Cytomic ZDF State-Variable Filter bank.
 *    Coefficients and integrator states live purely in YMM0..YMM5 across frames.
 * ============================================================================ */
    .globl xps17_avx2_svf_bank8_f32
    .type  xps17_avx2_svf_bank8_f32, @function
    .p2align 4
xps17_avx2_svf_bank8_f32:
    .cfi_startproc
    endbr64
    mov          rax, qword ptr [rsp + 8]
    test         rax, rax
    jz           .Lsvf_done

    vmovups      ymm0, ymmword ptr [r8]
    vmovups      ymm1, ymmword ptr [r8 + 32]
    vmovups      ymm2, ymmword ptr [r8 + 64]
    vmovups      ymm3, ymmword ptr [r8 + 96]
    vmovups      ymm4, ymmword ptr [r9]
    vmovups      ymm5, ymmword ptr [r9 + 32]
    vbroadcastss ymm6, dword ptr [rip + .LC_two]

    xor          r10d, r10d

    .p2align 4
.Lsvf_loop:
    vmovups      ymm7,  ymmword ptr [rdi + r10]
    vsubps       ymm10, ymm7,  ymm5
    vmulps       ymm8,  ymm0,  ymm4
    vmovaps      ymm9,  ymm5
    vfmadd231ps  ymm9,  ymm1,  ymm4
    vfmadd231ps  ymm8,  ymm1,  ymm10
    vfmadd231ps  ymm9,  ymm2,  ymm10
    vfmsub231ps  ymm4,  ymm6,  ymm8
    vfmsub231ps  ymm5,  ymm6,  ymm9
    vfnmadd231ps ymm7,  ymm3,  ymm8
    vsubps       ymm7,  ymm7,  ymm9

    vmovups      ymmword ptr [rsi + r10], ymm9
    vmovups      ymmword ptr [rdx + r10], ymm8
    vmovups      ymmword ptr [rcx + r10], ymm7

    add          r10, 32
    dec          rax
    jnz          .Lsvf_loop

    vmovups      ymmword ptr [r9],      ymm4
    vmovups      ymmword ptr [r9 + 32], ymm5

.Lsvf_done:
    vzeroupper
    ret
    .cfi_endproc
    .size xps17_avx2_svf_bank8_f32, .-xps17_avx2_svf_bank8_f32

    .section .note.GNU-stack,"",@progbits
