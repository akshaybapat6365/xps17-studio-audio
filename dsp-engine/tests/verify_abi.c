/* verify_abi.c — C11 dlopen LADSPA v1.1 ABI & Stress Verifier for libxps17_studio_dsp.so
 *
 * Build:
 *   gcc -std=c11 -O2 -Wall -Wextra -Werror tests/verify_abi.c -ldl -lm -o /tmp/verify_abi
 * Run:
 *   /tmp/verify_abi lib/libxps17_studio_dsp.so
 */

#include <dlfcn.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ladspa.h"

#define EXPECTED_UNIQUE_ID 597201UL
#define EXPECTED_LABEL     "xps17_studio_dsp"
#define EXPECTED_PORTS     14UL
#define SAMPLE_RATE        48000UL
#define BLOCK_SIZE         128UL
#define TWO_PI_F           6.28318530717958647692f

static int fail(const char *msg) {
    fprintf(stderr, "ABI VERIFY FAIL: %s\n", msg);
    return 1;
}

typedef struct {
    const char *name;
    LADSPA_PortDescriptor desc;
    int check_bounds;
    float lo;
    float hi;
    int require_int;
} ExpectedPort;

static const ExpectedPort EXPECTED_SCHEMA[EXPECTED_PORTS] = {
    { "In L",             LADSPA_PORT_INPUT  | LADSPA_PORT_AUDIO,   0,   0.0f,   0.0f, 0 },
    { "In R",             LADSPA_PORT_INPUT  | LADSPA_PORT_AUDIO,   0,   0.0f,   0.0f, 0 },
    { "Out L",            LADSPA_PORT_OUTPUT | LADSPA_PORT_AUDIO,   0,   0.0f,   0.0f, 0 },
    { "Out R",            LADSPA_PORT_OUTPUT | LADSPA_PORT_AUDIO,   0,   0.0f,   0.0f, 0 },
    { "Mode",             LADSPA_PORT_INPUT  | LADSPA_PORT_CONTROL, 1,   0.0f,   3.0f, 1 },
    { "Volume (dB)",      LADSPA_PORT_INPUT  | LADSPA_PORT_CONTROL, 1, -45.0f,   0.0f, 0 },
    { "Bass Depth",       LADSPA_PORT_INPUT  | LADSPA_PORT_CONTROL, 1,   0.0f,   1.5f, 0 },
    { "Physical Bass Hz", LADSPA_PORT_INPUT  | LADSPA_PORT_CONTROL, 1,  65.0f, 180.0f, 0 },
    { "Warmth Body",      LADSPA_PORT_INPUT  | LADSPA_PORT_CONTROL, 1,   0.0f,   1.5f, 0 },
    { "Stage Depth",      LADSPA_PORT_INPUT  | LADSPA_PORT_CONTROL, 1,   0.0f,   1.5f, 0 },
    { "Spatial XTC",      LADSPA_PORT_INPUT  | LADSPA_PORT_CONTROL, 1,   0.0f,   1.0f, 0 },
    { "Drive (dB)",       LADSPA_PORT_INPUT  | LADSPA_PORT_CONTROL, 1,   0.0f,  12.0f, 0 },
    { "Ceiling (dBFS)",   LADSPA_PORT_INPUT  | LADSPA_PORT_CONTROL, 1,  -9.0f,  -0.3f, 0 },
    { "Excursion Guard",  LADSPA_PORT_INPUT  | LADSPA_PORT_CONTROL, 1,   0.0f,   1.0f, 0 },
};

int main(int argc, char **argv) {
    const char *so_path = (argc > 1) ? argv[1] : "lib/libxps17_studio_dsp.so";

    printf("=== LADSPA v1.1 C ABI Verifier ===\n");
    printf("Loading plugin: %s\n", so_path);

    dlerror();
    void *handle = dlopen(so_path, RTLD_NOW | RTLD_LOCAL);
    if (!handle) {
        fprintf(stderr, "dlopen failed: %s\n", dlerror());
        return 1;
    }

    /* POSIX-clean function pointer load avoiding -Wpedantic object-to-fn cast warning */
    LADSPA_Descriptor_Function desc_fn = NULL;
    void *sym = dlsym(handle, "ladspa_descriptor");
    const char *dlerr = dlerror();
    if (dlerr != NULL || sym == NULL) {
        fprintf(stderr, "dlsym(ladspa_descriptor) failed: %s\n", dlerr ? dlerr : "NULL");
        dlclose(handle);
        return 1;
    }
    memcpy(&desc_fn, &sym, sizeof(desc_fn));

    const LADSPA_Descriptor *desc = desc_fn(0);
    if (!desc) {
        dlclose(handle);
        return fail("ladspa_descriptor(0) returned NULL");
    }
    if (desc_fn(1) != NULL) {
        dlclose(handle);
        return fail("ladspa_descriptor(1) must return NULL");
    }
    if (desc_fn(99) != NULL) {
        dlclose(handle);
        return fail("ladspa_descriptor(99) must return NULL");
    }

    /* 1. Verify descriptor metadata */
    if (desc->UniqueID != EXPECTED_UNIQUE_ID) {
        fprintf(stderr, "UniqueID mismatch: got %lu, expected %lu\n",
                desc->UniqueID, EXPECTED_UNIQUE_ID);
        dlclose(handle);
        return 1;
    }
    if (!desc->Label || strcmp(desc->Label, EXPECTED_LABEL) != 0) {
        fprintf(stderr, "Label mismatch: got '%s', expected '%s'\n",
                desc->Label ? desc->Label : "(null)", EXPECTED_LABEL);
        dlclose(handle);
        return 1;
    }
    if (!desc->Name || strlen(desc->Name) == 0 ||
        !desc->Maker || strlen(desc->Maker) == 0 ||
        !desc->Copyright || strlen(desc->Copyright) == 0) {
        dlclose(handle);
        return fail("Name, Maker, or Copyright missing");
    }
    if (!LADSPA_IS_HARD_RT_CAPABLE(desc->Properties)) {
        dlclose(handle);
        return fail("LADSPA_PROPERTY_HARD_RT_CAPABLE flag not set");
    }
    if (LADSPA_IS_INPLACE_BROKEN(desc->Properties)) {
        dlclose(handle);
        return fail("LADSPA_PROPERTY_INPLACE_BROKEN must NOT be set");
    }
    if (desc->PortCount != EXPECTED_PORTS) {
        fprintf(stderr, "PortCount mismatch: got %lu, expected %lu\n",
                desc->PortCount, EXPECTED_PORTS);
        dlclose(handle);
        return 1;
    }
    if (!desc->instantiate || !desc->connect_port || !desc->run || !desc->cleanup) {
        dlclose(handle);
        return fail("Required LADSPA lifecycle function pointer is NULL");
    }

    /* 2. Verify all 14 port names, descriptors, and range hints */
    for (unsigned long p = 0; p < EXPECTED_PORTS; ++p) {
        const ExpectedPort *exp = &EXPECTED_SCHEMA[p];
        const char *got_name = desc->PortNames[p];
        LADSPA_PortDescriptor got_desc = desc->PortDescriptors[p];
        LADSPA_PortRangeHint hint = desc->PortRangeHints[p];

        if (!got_name || strcmp(got_name, exp->name) != 0) {
            fprintf(stderr, "Port %lu name mismatch: got '%s', expected '%s'\n",
                    p, got_name ? got_name : "(null)", exp->name);
            dlclose(handle);
            return 1;
        }
        if (got_desc != exp->desc) {
            fprintf(stderr, "Port %lu ('%s') descriptor mismatch: got 0x%x, expected 0x%x\n",
                    p, exp->name, got_desc, exp->desc);
            dlclose(handle);
            return 1;
        }
        if (exp->check_bounds) {
            if (!LADSPA_IS_HINT_BOUNDED_BELOW(hint.HintDescriptor) ||
                !LADSPA_IS_HINT_BOUNDED_ABOVE(hint.HintDescriptor)) {
                fprintf(stderr, "Control port %lu ('%s') missing BOUNDED hints\n", p, exp->name);
                dlclose(handle);
                return 1;
            }
            if (fabsf(hint.LowerBound - exp->lo) > 1e-4f ||
                fabsf(hint.UpperBound - exp->hi) > 1e-4f) {
                fprintf(stderr, "Control port %lu ('%s') bounds mismatch: [%.3f, %.3f] != [%.3f, %.3f]\n",
                        p, exp->name, hint.LowerBound, hint.UpperBound, exp->lo, exp->hi);
                dlclose(handle);
                return 1;
            }
            if (exp->require_int && !LADSPA_IS_HINT_INTEGER(hint.HintDescriptor)) {
                fprintf(stderr, "Control port %lu ('%s') missing INTEGER hint\n", p, exp->name);
                dlclose(handle);
                return 1;
            }
        }
    }
    printf("[PASS] Descriptor metadata & all 14 port contracts verified (ID=%lu, Label=%s)\n",
           desc->UniqueID, desc->Label);

    /* 3. Instantiate and connect all 14 ports */
    LADSPA_Handle inst = desc->instantiate(desc, SAMPLE_RATE);
    if (!inst) {
        dlclose(handle);
        return fail("instantiate(48000) returned NULL");
    }

    LADSPA_Data in_l[BLOCK_SIZE];
    LADSPA_Data in_r[BLOCK_SIZE];
    LADSPA_Data out_l[BLOCK_SIZE];
    LADSPA_Data out_r[BLOCK_SIZE];

    LADSPA_Data ctrl_mode = 0.0f;
    LADSPA_Data ctrl_volume_db = -12.0f;
    LADSPA_Data ctrl_bass_depth = 1.0f;
    LADSPA_Data ctrl_phys_bass_hz = 82.0f;
    LADSPA_Data ctrl_warmth = 1.0f;
    LADSPA_Data ctrl_stage = 0.85f;
    LADSPA_Data ctrl_xtc = 0.0f;
    LADSPA_Data ctrl_drive = 7.0f;
    LADSPA_Data ctrl_ceiling = -1.0f;
    LADSPA_Data ctrl_excursion = 1.0f;

    desc->connect_port(inst, 0, in_l);
    desc->connect_port(inst, 1, in_r);
    desc->connect_port(inst, 2, out_l);
    desc->connect_port(inst, 3, out_r);
    desc->connect_port(inst, 4, &ctrl_mode);
    desc->connect_port(inst, 5, &ctrl_volume_db);
    desc->connect_port(inst, 6, &ctrl_bass_depth);
    desc->connect_port(inst, 7, &ctrl_phys_bass_hz);
    desc->connect_port(inst, 8, &ctrl_warmth);
    desc->connect_port(inst, 9, &ctrl_stage);
    desc->connect_port(inst, 10, &ctrl_xtc);
    desc->connect_port(inst, 11, &ctrl_drive);
    desc->connect_port(inst, 12, &ctrl_ceiling);
    desc->connect_port(inst, 13, &ctrl_excursion);

    if (desc->activate) {
        desc->activate(inst);
    }

    const float ceiling_lin = powf(10.0f, ctrl_ceiling / 20.0f);
    const float ceiling_tol = ceiling_lin + 1e-4f;

    /* 4. Out-of-place 100,000+ frame stress test across all 4 modes */
    const unsigned long total_blocks = 782UL; /* 782 * 128 = 100,096 frames */
    unsigned long total_frames = 0;
    unsigned long bad_count = 0;
    float max_abs = 0.0f;
    double energy_sum = 0.0;

    for (unsigned long b = 0; b < total_blocks; ++b) {
        /* Switch through all 4 studio modes across the 100k frames */
        unsigned long mode_idx = (b * 4UL) / total_blocks;
        ctrl_mode = (float)mode_idx;
        ctrl_xtc = (mode_idx == 1UL) ? 0.75f : 0.0f;

        for (unsigned long i = 0; i < BLOCK_SIZE; ++i) {
            float t = (float)(total_frames + i) / (float)SAMPLE_RATE;
            float burst = ((total_frames + i) % 4096UL < 8UL) ? 0.85f : 0.0f;
            in_l[i] = 0.35f * sinf(TWO_PI_F * 65.0f * t)
                    + 0.25f * sinf(TWO_PI_F * 440.0f * t)
                    + 0.15f * sinf(TWO_PI_F * 3200.0f * t)
                    + burst;
            in_r[i] = 0.35f * sinf(TWO_PI_F * 65.0f * t)
                    + 0.25f * cosf(TWO_PI_F * 554.37f * t)
                    + 0.15f * sinf(TWO_PI_F * 4800.0f * t)
                    - burst;
        }

        desc->run(inst, BLOCK_SIZE);

        for (unsigned long i = 0; i < BLOCK_SIZE; ++i) {
            float l = out_l[i];
            float r = out_r[i];
            if (!isfinite(l) || !isfinite(r)) {
                bad_count++;
            } else {
                float al = fabsf(l);
                float ar = fabsf(r);
                if (al > max_abs) max_abs = al;
                if (ar > max_abs) max_abs = ar;
                energy_sum += (double)l * (double)l + (double)r * (double)r;
            }
        }
        total_frames += BLOCK_SIZE;
    }

    double rms = sqrt(energy_sum / (2.0 * (double)total_frames));
    if (bad_count > 0) {
        fprintf(stderr, "Out-of-place run produced %lu NaN/Inf samples\n", bad_count);
        if (desc->deactivate) desc->deactivate(inst);
        desc->cleanup(inst);
        dlclose(handle);
        return 1;
    }
    if (max_abs > ceiling_tol) {
        fprintf(stderr, "Ceiling violated: max_abs=%.6f > ceiling=%.6f\n", max_abs, ceiling_tol);
        if (desc->deactivate) desc->deactivate(inst);
        desc->cleanup(inst);
        dlclose(handle);
        return 1;
    }
    if (rms < 0.01) {
        fprintf(stderr, "Signal energy too low (silent output?): rms=%.6f\n", rms);
        if (desc->deactivate) desc->deactivate(inst);
        desc->cleanup(inst);
        dlclose(handle);
        return 1;
    }
    printf("[PASS] Out-of-place %lu frames across 4 modes: NaN=0, max=%.5f (<=%.5f), rms=%.4f\n",
           total_frames, max_abs, ceiling_lin, rms);

    /* 5. In-place buffer aliasing test (Out L == In L, Out R == In R) */
    desc->connect_port(inst, 2, in_l);
    desc->connect_port(inst, 3, in_r);
    ctrl_mode = 1.0f;
    ctrl_xtc = 0.75f;

    float alias_max = 0.0f;
    double alias_energy = 0.0;
    unsigned long alias_frames = 0;
    for (unsigned long b = 0; b < 128UL; ++b) {
        for (unsigned long i = 0; i < BLOCK_SIZE; ++i) {
            float t = (float)(alias_frames + i) / (float)SAMPLE_RATE;
            in_l[i] = 0.45f * sinf(TWO_PI_F * 110.0f * t) + 0.20f * sinf(TWO_PI_F * 1500.0f * t);
            in_r[i] = 0.45f * sinf(TWO_PI_F * 110.0f * t) + 0.20f * cosf(TWO_PI_F * 2200.0f * t);
        }
        desc->run(inst, BLOCK_SIZE);
        for (unsigned long i = 0; i < BLOCK_SIZE; ++i) {
            if (!isfinite(in_l[i]) || !isfinite(in_r[i])) {
                bad_count++;
            } else {
                float al = fabsf(in_l[i]);
                float ar = fabsf(in_r[i]);
                if (al > alias_max) alias_max = al;
                if (ar > alias_max) alias_max = ar;
                alias_energy += (double)in_l[i] * (double)in_l[i] + (double)in_r[i] * (double)in_r[i];
            }
        }
        alias_frames += BLOCK_SIZE;
    }
    double alias_rms = sqrt(alias_energy / (2.0 * (double)alias_frames));
    if (bad_count > 0 || alias_max > ceiling_tol || alias_rms < 0.01) {
        fprintf(stderr, "In-place aliasing test failed: bad=%lu max=%.5f rms=%.5f\n",
                bad_count, alias_max, alias_rms);
        if (desc->deactivate) desc->deactivate(inst);
        desc->cleanup(inst);
        dlclose(handle);
        return 1;
    }
    printf("[PASS] In-place aliasing %lu frames: NaN=0, max=%.5f (<=%.5f), rms=%.4f\n",
           alias_frames, alias_max, ceiling_lin, alias_rms);

    /* 6. NaN / +Inf / -Inf / Denormal injection + NULL control port resilience */
    desc->connect_port(inst, 2, out_l);
    desc->connect_port(inst, 3, out_r);
    desc->connect_port(inst, 5, NULL); /* Disconnect Volume (dB) -> must fall back safely */

    for (unsigned long b = 0; b < 32UL; ++b) {
        for (unsigned long i = 0; i < BLOCK_SIZE; ++i) {
            float t = (float)(b * BLOCK_SIZE + i) / (float)SAMPLE_RATE;
            in_l[i] = 0.2f * sinf(TWO_PI_F * 440.0f * t);
            in_r[i] = 0.2f * cosf(TWO_PI_F * 440.0f * t);
            if (b == 1 && i == 5) {
                in_l[i] = NAN;
                in_r[i] = INFINITY;
            } else if (b == 2 && i == 7) {
                in_l[i] = -INFINITY;
                in_r[i] = 1.0e-40f; /* denormal */
            } else if (b >= 4 && b < 12) {
                in_l[i] = 1.0e-40f;
                in_r[i] = -1.0e-41f;
            }
        }
        desc->run(inst, BLOCK_SIZE);
        for (unsigned long i = 0; i < BLOCK_SIZE; ++i) {
            if (!isfinite(out_l[i]) || !isfinite(out_r[i]) ||
                fabsf(out_l[i]) > ceiling_tol || fabsf(out_r[i]) > ceiling_tol) {
                bad_count++;
            }
        }
    }
    if (bad_count > 0) {
        fprintf(stderr, "Hostile NaN/Inf/denormal injection test failed: bad=%lu\n", bad_count);
        if (desc->deactivate) desc->deactivate(inst);
        desc->cleanup(inst);
        dlclose(handle);
        return 1;
    }
    printf("[PASS] Hostile NaN/Inf/denormal injection + NULL control port test: bad=0\n");

    /* 7. Clean teardown */
    if (desc->deactivate) {
        desc->deactivate(inst);
    }
    desc->cleanup(inst);
    dlclose(handle);

    printf("ALL LADSPA v1.1 C ABI CHECKS PASSED.\n");
    return 0;
}
