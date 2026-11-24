// SPDX-License-Identifier: GPL-2.0-only
/*
 * Exhaustive offline adversarial verification suite for Dell XPS 17 9720 (0AFF)
 * rt1316-sdw kernel policy, crossover math, IV-sense slot mapping, and Asahi-style
 * 250ms deadman guard state machine.
 *
 * Directly compiles against the exact kernel policy header:
 *   #include "../rt1316-xps17/rt1316-xps17-policy.h"
 *
 * Build & run on E-cores with AddressSanitizer + UndefinedBehaviorSanitizer:
 *   taskset -c 12-19 nice -n 19 gcc -O2 -std=c11 -Wall -Wextra -Werror -Wpedantic \
 *       -fsanitize=address,undefined -I../rt1316-xps17 test_rt1316_policy.c -lm \
 *       -o test_rt1316_policy && taskset -c 12-19 nice -n 19 ./test_rt1316_policy
 */

#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../rt1316-xps17/rt1316-xps17-policy.h"

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

static uint64_t g_checks = 0;
static uint64_t g_failures = 0;

#define EXPECT(cond, ...) do {                                               \
	g_checks++;                                                          \
	if (!(cond)) {                                                       \
		g_failures++;                                                \
		if (g_failures <= 60) {                                      \
			fprintf(stderr, "FAIL %s:%d: ", __FILE__, __LINE__); \
			fprintf(stderr, __VA_ARGS__);                        \
			fprintf(stderr, "\n");                               \
		}                                                            \
	}                                                                    \
} while (0)

/* ------------------------------------------------------------------------- */
/* 1. Forbidden Register Invariant & Exhaustive Register-Space Sweep         */
/* ------------------------------------------------------------------------- */
static void test_forbidden_and_allowlist_sweep(void)
{
	static const unsigned int forbidden_regs[] = {
		0xC000U, 0xC050U, 0xC082U, 0xC093U, 0xC09CU,
		0xC09DU, 0xC0A3U, 0xC102U, 0xC60EU,
	};
	static const unsigned int sdca_probe_regs[] = {
		0x40000000U, 0x41000000U, 0x41001208U, /* XU24 Bypass */
		0x41080100U, /* UDMPU21 cluster */
		0x41080101U, 0x41080200U, 0x41090000U,
		0x7FFFFFFFU, 0x80000000U, 0xFFFFFFFFU,
	};
	size_t i;
	unsigned int reg, val;
	unsigned int forbidden_cnt = 0, allowed_cnt = 0;

	printf("[1/5] Forbidden register invariant & 16-bit + SDCA register-space sweep...\n");

	/* Every mandatory forbidden register must be forbidden AND disallowed */
	for (i = 0; i < sizeof(forbidden_regs) / sizeof(forbidden_regs[0]); i++) {
		reg = forbidden_regs[i];
		EXPECT(rt1316_xps17_reg_is_forbidden(reg) == true,
		       "0x%04X must be forbidden", reg);
		EXPECT(rt1316_xps17_reg_is_allowed_write(reg) == false,
		       "0x%04X must NOT be allowed_write", reg);
		for (val = 0U; val <= 0x1FFU; val++) {
			EXPECT(rt1316_xps17_write_val_is_safe(reg, val) == false,
			       "forbidden reg 0x%04X val 0x%X must never be safe", reg, val);
		}
	}

	/* Sweep ENTIRE 16-bit register space 0x0000..0xFFFF: ZERO overlap */
	for (reg = 0x0000U; reg <= 0xFFFFU; reg++) {
		bool is_f = rt1316_xps17_reg_is_forbidden(reg);
		bool is_a = rt1316_xps17_reg_is_allowed_write(reg);

		EXPECT(!(is_f && is_a),
		       "OVERLAP at 0x%04X: both forbidden and allowed_write!", reg);
		if (is_f)
			forbidden_cnt++;
		if (is_a) {
			allowed_cnt++;
			/* Value > 0xFF must always be rejected even on allowlisted regs */
			EXPECT(!rt1316_xps17_write_val_is_safe(reg, 0x100U),
			       "0x%04X accepted out-of-byte value 0x100", reg);
			EXPECT(!rt1316_xps17_write_val_is_safe(reg, 0xFFFFFFFFU),
			       "0x%04X accepted 0xFFFFFFFF", reg);
		} else {
			EXPECT(!rt1316_xps17_write_val_is_safe(reg, 0x00U),
			       "disallowed reg 0x%04X accepted 0x00", reg);
			EXPECT(!rt1316_xps17_write_val_is_safe(reg, 0xFFU),
			       "disallowed reg 0x%04X accepted 0xFF", reg);
		}
	}

	/* Sweep SDCA register space (including XU24 0x41001208 & boundary values) */
	for (i = 0; i < sizeof(sdca_probe_regs) / sizeof(sdca_probe_regs[0]); i++) {
		reg = sdca_probe_regs[i];
		EXPECT(!(rt1316_xps17_reg_is_forbidden(reg) &&
			 rt1316_xps17_reg_is_allowed_write(reg)),
		       "SDCA overlap at 0x%08X", reg);
		EXPECT(!rt1316_xps17_reg_is_allowed_write(reg),
		       "SDCA reg 0x%08X must not be in P1/P3/P4 raw reg allowlist", reg);
		EXPECT(!rt1316_xps17_write_val_is_safe(reg, 0x00U),
		       "SDCA reg 0x%08X must fail write_val_is_safe", reg);
	}
	/* Also sweep a contiguous SDCA window 0x41000000..0x41010000 */
	for (reg = 0x41000000U; reg <= 0x41010000U; reg++) {
		EXPECT(!(rt1316_xps17_reg_is_forbidden(reg) &&
			 rt1316_xps17_reg_is_allowed_write(reg)),
		       "SDCA window overlap at 0x%08X", reg);
		EXPECT(!rt1316_xps17_reg_is_allowed_write(reg),
		       "SDCA window reg 0x%08X unexpectedly allowlisted", reg);
	}

	/* Exhaustive byte-value gate tests for every control/gain/IV register */
	for (val = 0U; val <= 0x1FFU; val++) {
		EXPECT(rt1316_xps17_write_val_is_safe(0xC00BU, val) == (val == 0x30U),
		       "0xC00B val 0x%X", val);
		EXPECT(rt1316_xps17_write_val_is_safe(0xC200U, val) == (val == 0xC0U),
		       "0xC200 val 0x%X", val);
		EXPECT(rt1316_xps17_write_val_is_safe(0xC202U, val) ==
		       (val == 0x00U || val == 0xC0U),
		       "0xC202 val 0x%X", val);
		EXPECT(rt1316_xps17_write_val_is_safe(0x3004U, val) ==
		       (val == 0xA0U || val == 0xE4U),
		       "0x3004 val 0x%X", val);
		EXPECT(rt1316_xps17_write_val_is_safe(0x3005U, val) ==
		       (val == 0xB1U || val == 0xF5U),
		       "0x3005 val 0x%X", val);
		EXPECT(rt1316_xps17_write_val_is_safe(0xC60FU, val) ==
		       (val == 0x01U || val == 0x05U),
		       "0xC60F val 0x%X", val);
		EXPECT(rt1316_xps17_write_val_is_safe(0xD002U, val) ==
		       (val == 0xBFU || val == 0xA9U),
		       "0xD002 val 0x%X", val);
		EXPECT(rt1316_xps17_write_val_is_safe(0xD004U, val) ==
		       (val == 0xBFU || val == 0xA9U),
		       "0xD004 val 0x%X", val);
	}

	EXPECT(forbidden_cnt >= 9U, "expected >= 9 forbidden regs, got %u", forbidden_cnt);
	EXPECT(allowed_cnt == 140U, "expected exactly 140 allowlisted regs, got %u", allowed_cnt);
	printf("      16-bit sweep complete: %u forbidden, %u allowlisted, 0 overlap (+65537 SDCA regs verified)\n",
	       forbidden_cnt, allowed_cnt);
}

/* ------------------------------------------------------------------------- */
/* 2. Compiled 134-Write BQ Tables Structural & Register-Safety Verification */
/* ------------------------------------------------------------------------- */
static void test_bq_tables_integrity(void)
{
	unsigned int p, i, len;
	const unsigned char *tbl;

	printf("[2/5] Compiled 134-write BQ table structural & safety audit...\n");

	/* Invalid profile bounds check */
	len = 999U;
	EXPECT(rt1316_xps17_bq_profile_table(RT1316_BQ_PROFILE_COUNT, &len) == NULL && len == 0U,
	       "out-of-range profile must return NULL/0");
	EXPECT(rt1316_xps17_bq_profile_table((enum rt1316_xps17_bq_profile)99, &len) == NULL && len == 0U,
	       "profile 99 must return NULL/0");

	for (p = 0; p < (unsigned int)RT1316_BQ_PROFILE_COUNT; p++) {
		uint8_t vol_bytes[16];
		bool vol_seen[16];
		unsigned int last_reg = 0U, last_val = 0U;

		memset(vol_bytes, 0, sizeof(vol_bytes));
		memset(vol_seen, 0, sizeof(vol_seen));

		len = 0U;
		tbl = rt1316_xps17_bq_profile_table((enum rt1316_xps17_bq_profile)p, &len);
		EXPECT(tbl != NULL, "profile %u table NULL", p);
		EXPECT(len == RT1316_XPS17_BQ_TABLE_BYTES, "profile %u len %u != 402", p, len);
		EXPECT(len % 3U == 0U && len / 3U == 134U, "profile %u not 134 writes", p);

		for (i = 0U; i < len; i += 3U) {
			unsigned int reg = (unsigned int)tbl[i] | ((unsigned int)tbl[i + 1U] << 8);
			unsigned int val = (unsigned int)tbl[i + 2U];

			EXPECT(!rt1316_xps17_reg_is_forbidden(reg),
			       "profile %u entry %u writes forbidden reg 0x%04X", p, i / 3U, reg);
			EXPECT(rt1316_xps17_reg_is_allowed_write(reg),
			       "profile %u entry %u writes unallowlisted reg 0x%04X", p, i / 3U, reg);
			EXPECT(rt1316_xps17_write_val_is_safe(reg, val),
			       "profile %u entry %u unsafe val 0x%04X=0x%02X", p, i / 3U, reg, val);

			if (reg >= 0xC261U && reg <= 0xC270U) {
				vol_bytes[reg - 0xC261U] = (uint8_t)val;
				vol_seen[reg - 0xC261U] = true;
			}
			last_reg = reg;
			last_val = val;
		}

		/* Final write must pulse 0xC200 = 0xC0 */
		EXPECT(last_reg == 0xC200U && last_val == 0xC0U,
		       "profile %u last write 0x%04X=0x%02X != 0xC200=0xC0", p, last_reg, last_val);

		/* All 4 pre/post volume words 0xC261..0xC270 must decode to exact 1.0 (0 dB) */
		for (i = 0U; i < 16U; i++)
			EXPECT(vol_seen[i], "profile %u missing volume byte 0x%04X", p, 0xC261U + i);
		for (i = 0U; i < 4U; i++) {
			uint32_t w = ((uint32_t)vol_bytes[i * 4U] << 24) |
				     ((uint32_t)vol_bytes[i * 4U + 1U] << 16) |
				     ((uint32_t)vol_bytes[i * 4U + 2U] << 8) |
				     (uint32_t)vol_bytes[i * 4U + 3U];
			double gain = (double)(int32_t)w / 134217728.0;
			EXPECT(w == 0x08000000U && fabs(gain - 1.0) < 1e-12,
			       "profile %u volume word %u = 0x%08X (%.6f != 1.0)", p, i, w, gain);
		}
	}
	printf("      Both 134-write profiles verified: 0 forbidden, 134/134 allowlisted, 0.000 dB volume words\n");
}

/* ------------------------------------------------------------------------- */
/* 3. P3 Crossover Mathematical Safety Test (Independent Q5.27 Evaluator)    */
/* ------------------------------------------------------------------------- */
static double indep_bq_mag_db(const int32_t q[5], double freq_hz, double fs_hz)
{
	double b0 = (double)q[0] / 134217728.0;
	double b1 = (double)q[1] / 134217728.0;
	double b2 = (double)q[2] / 134217728.0;
	double a1 = (double)q[3] / 134217728.0;
	double a2 = (double)q[4] / 134217728.0;
	double w = 2.0 * M_PI * freq_hz / fs_hz;
	double cw = cos(w), sw = sin(w);
	double c2w = cos(2.0 * w), s2w = sin(2.0 * w);
	/* H(e^{jw}) = (b0 + b1*e^{-jw} + b2*e^{-2jw}) / (1 + a1*e^{-jw} + a2*e^{-2jw}) */
	double nr = b0 + b1 * cw + b2 * c2w;
	double ni = -b1 * sw - b2 * s2w;
	double dr = 1.0 + a1 * cw + a2 * c2w;
	double di = -a1 * sw - a2 * s2w;
	double mag2 = (nr * nr + ni * ni) / (dr * dr + di * di);

	return (mag2 <= 1e-30) ? -300.0 : 10.0 * log10(mag2);
}

static void test_crossover_math_safety(void)
{
	static const char *const prof_names[2] = { "STOCK_DELL", "PRECISION_48K" };
	static const char *const chan_names[2] = { "SPOL (woofer)", "SPOR (tweeter)" };
	const double fs = 48000.0;
	const unsigned int n_pts = 2000U; /* >= 1000 log-spaced points across 20..20000 Hz */
	int32_t all_q[2][4][5];
	unsigned int p, ch, bq, k;

	printf("[3/5] P3 Crossover BIBO stability & 2000-point 20Hz-20kHz gain ceiling audit...\n");

	/* Negative extraction tests */
	{
		int32_t dummy[5];
		EXPECT(!rt1316_xps17_bq_extract_q527(RT1316_BQ_PROFILE_STOCK_DELL, 4U, dummy),
		       "bq_idx=4 must fail");
		EXPECT(!rt1316_xps17_bq_extract_q527(RT1316_BQ_PROFILE_STOCK_DELL, 0U, NULL),
		       "NULL output array must fail");
		EXPECT(!rt1316_xps17_bq_extract_q527(RT1316_BQ_PROFILE_COUNT, 0U, dummy),
		       "invalid profile must fail");
	}

	for (p = 0U; p < 2U; p++) {
		enum rt1316_xps17_bq_profile prof = (enum rt1316_xps17_bq_profile)p;

		/* Extract and check Jury stability + pole radius for all 4 sections */
		for (bq = 0U; bq < 4U; bq++) {
			double a1, a2, r_hdr, r_indep, disc;

			EXPECT(rt1316_xps17_bq_extract_q527(prof, bq, all_q[p][bq]),
			       "failed to extract profile %s bq %u", prof_names[p], bq);

			a1 = (double)all_q[p][bq][3] / 134217728.0;
			a2 = (double)all_q[p][bq][4] / 134217728.0;

			/* Jury BIBO stability conditions for 2nd-order IIR: |a2| < 1 && |a1| < 1 + a2 */
			EXPECT(fabs(a2) < 1.0,
			       "%s BQ%u violates |a2| < 1 (a2=%.9f)", prof_names[p], bq, a2);
			EXPECT(fabs(a1) < 1.0 + a2,
			       "%s BQ%u violates |a1| < 1 + a2 (a1=%.9f, 1+a2=%.9f)",
			       prof_names[p], bq, a1, 1.0 + a2);

			disc = a1 * a1 - 4.0 * a2;
			if (disc < 0.0)
				r_indep = sqrt(a2);
			else
				r_indep = fmax(fabs((-a1 + sqrt(disc)) * 0.5),
					       fabs((-a1 - sqrt(disc)) * 0.5));
			r_hdr = rt1316_xps17_bq_pole_radius(all_q[p][bq]);
			EXPECT(r_indep < 1.0 && r_hdr < 1.0,
			       "%s BQ%u pole radius >= 1 (indep=%.9f, hdr=%.9f)",
			       prof_names[p], bq, r_indep, r_hdr);
			EXPECT(fabs(r_indep - r_hdr) < 1e-12,
			       "%s BQ%u pole radius mismatch", prof_names[p], bq);
		}

		/* Sweep 20 Hz .. 20,000 Hz (2000 log-spaced points) on SPOL and SPOR */
		for (ch = 0U; ch < 2U; ch++) {
			double max_db = -1e9, max_freq = 0.0;
			double max_sub100_db = -1e9;
			double mag_20hz = 0.0, mag_100hz = 0.0, mag_20khz = 0.0;

			for (k = 0U; k < n_pts; k++) {
				double frac = (double)k / (double)(n_pts - 1U);
				double f = 20.0 * pow(20000.0 / 20.0, frac);
				double g_indep = indep_bq_mag_db(all_q[p][ch * 2U], f, fs) +
						 indep_bq_mag_db(all_q[p][ch * 2U + 1U], f, fs);
				double g_hdr = rt1316_xps17_bq_channel_mag_db(prof, ch, f, fs);

				EXPECT(fabs(g_indep - g_hdr) < 1e-9,
				       "%s %s @ %.2fHz math mismatch (%.9f vs %.9f)",
				       prof_names[p], chan_names[ch], f, g_indep, g_hdr);

				if (g_indep > max_db) {
					max_db = g_indep;
					max_freq = f;
				}
				if (f <= 100.0 && g_indep > max_sub100_db)
					max_sub100_db = g_indep;
			}

			mag_20hz = indep_bq_mag_db(all_q[p][ch * 2U], 20.0, fs) +
				   indep_bq_mag_db(all_q[p][ch * 2U + 1U], 20.0, fs);
			mag_100hz = indep_bq_mag_db(all_q[p][ch * 2U], 100.0, fs) +
				    indep_bq_mag_db(all_q[p][ch * 2U + 1U], 100.0, fs);
			mag_20khz = indep_bq_mag_db(all_q[p][ch * 2U], 20000.0, fs) +
				    indep_bq_mag_db(all_q[p][ch * 2U + 1U], 20000.0, fs);

			/* Hard safety ceiling: <= +2.0 dB across 20 Hz - 20 kHz */
			EXPECT(max_db <= 2.0,
			       "%s %s exceeds +2.0 dB ceiling: %+.4f dB @ %.1f Hz",
			       prof_names[p], chan_names[ch], max_db, max_freq);

			if (prof == RT1316_BQ_PROFILE_PRECISION_48K) {
				EXPECT(max_db <= 0.1,
				       "PRECISION_48K %s exceeds +0.1 dB target: %+.4f dB @ %.1f Hz",
				       chan_names[ch], max_db, max_freq);
			}

			/* Tweeter SPOR sub-100 Hz bass protection (HPF verified!) */
			if (ch == 1U) {
				EXPECT(max_sub100_db < -35.0,
				       "%s SPOR sub-100Hz bass NOT attenuated enough: %.2f dB",
				       prof_names[p], max_sub100_db);
				EXPECT(mag_20hz < -65.0,
				       "%s SPOR 20Hz bass NOT attenuated enough: %.2f dB",
				       prof_names[p], mag_20hz);
			} else {
				/* Woofer SPOL high-frequency low-pass rolloff */
				EXPECT(mag_20khz < -30.0,
				       "%s SPOL 20kHz LPF rolloff insufficient: %.2f dB",
				       prof_names[p], mag_20khz);
			}

			printf("      %-13s %-14s: max=%+.4f dB @ %7.1f Hz | 20Hz=%+6.2f dB, 100Hz=%+6.2f dB, 20kHz=%+6.2f dB\n",
			       prof_names[p], chan_names[ch], max_db, max_freq,
			       mag_20hz, mag_100hz, mag_20khz);
		}
	}

	/* Verify precision_48k two-way power sum flatness (100 Hz .. 20 kHz) */
	{
		double psum_min = 1e9, psum_max = -1e9;
		for (k = 0U; k < 1000U; k++) {
			double f = 100.0 * pow(20000.0 / 100.0, (double)k / 999.0);
			double gl = rt1316_xps17_bq_channel_mag_db(RT1316_BQ_PROFILE_PRECISION_48K, 0U, f, fs);
			double gr = rt1316_xps17_bq_channel_mag_db(RT1316_BQ_PROFILE_PRECISION_48K, 1U, f, fs);
			double psum = 10.0 * log10(pow(10.0, gl / 10.0) + pow(10.0, gr / 10.0));
			if (psum < psum_min)
				psum_min = psum;
			if (psum > psum_max)
				psum_max = psum;
		}
		EXPECT(psum_min >= -0.25 && psum_max <= 0.1,
		       "PRECISION_48K power-sum out of bounds: [%.4f, %.4f] dB", psum_min, psum_max);
		printf("      PRECISION_48K power-sum (100Hz-20kHz): min=%+.4f dB, max=%+.4f dB\n",
		       psum_min, psum_max);
	}

	/* Adversarial mixed-profile cross-check (transient safety if BQ0/BQ1 differ) */
	for (ch = 0U; ch < 2U; ch++) {
		unsigned int s0, s1;
		for (s0 = 0U; s0 < 2U; s0++) {
			for (s1 = 0U; s1 < 2U; s1++) {
				double worst = -1e9;
				for (k = 0U; k < 500U; k++) {
					double f = 20.0 * pow(20000.0 / 20.0, (double)k / 499.0);
					double g = indep_bq_mag_db(all_q[s0][ch * 2U], f, fs) +
						   indep_bq_mag_db(all_q[s1][ch * 2U + 1U], f, fs);
					if (g > worst)
						worst = g;
				}
				EXPECT(worst <= 2.0,
				       "mixed profile (%u,%u) ch %u peak %+.3f dB > +2.0 dB",
				       s0, s1, ch, worst);
			}
		}
	}
}

/* ------------------------------------------------------------------------- */
/* 4. P3 Amp 2 IV-Sense Slot De-Collision Test                               */
/* ------------------------------------------------------------------------- */
static void test_iv_sense_slot_decollision(void)
{
	uint8_t a1_0 = 0, a1_1 = 0, a2_0 = 0, a2_1 = 0;
	uint8_t tags[4];
	size_t i, j;

	printf("[4/5] P3 Amp 1 & Amp 2 IV-sense slot de-collision audit...\n");

	rt1316_xps17_iv_slot_tags(1U, &a1_0, &a1_1);
	rt1316_xps17_iv_slot_tags(2U, &a2_0, &a2_1);

	EXPECT(a1_0 == 0xA0U, "Amp 1 reg 0x3004 = 0x%02X, want 0xA0", a1_0);
	EXPECT(a1_1 == 0xB1U, "Amp 1 reg 0x3005 = 0x%02X, want 0xB1", a1_1);
	EXPECT(a2_0 == 0xE4U, "Amp 2 reg 0x3004 = 0x%02X, want 0xE4", a2_0);
	EXPECT(a2_1 == 0xF5U, "Amp 2 reg 0x3005 = 0x%02X, want 0xF5", a2_1);

	tags[0] = a1_0;
	tags[1] = a1_1;
	tags[2] = a2_0;
	tags[3] = a2_1;

	/* Pairwise distinct full bytes, high nibbles (tag IDs), and low nibbles (slot indices) */
	for (i = 0; i < 4; i++) {
		for (j = i + 1; j < 4; j++) {
			EXPECT(tags[i] != tags[j],
			       "IV slot tag collision: tags[%zu]=0x%02X == tags[%zu]=0x%02X",
			       i, tags[i], j, tags[j]);
			EXPECT((tags[i] & 0x0FU) != (tags[j] & 0x0FU),
			       "IV slot index collision (low nibble): 0x%02X vs 0x%02X",
			       tags[i], tags[j]);
			EXPECT((tags[i] & 0xF0U) != (tags[j] & 0xF0U),
			       "IV tag nibble collision (high nibble): 0x%02X vs 0x%02X",
			       tags[i], tags[j]);
		}
	}

	/* Verify safe values pass policy write_val_is_safe */
	EXPECT(rt1316_xps17_write_val_is_safe(0x3004U, a1_0), "Amp1 0x3004 safe");
	EXPECT(rt1316_xps17_write_val_is_safe(0x3005U, a1_1), "Amp1 0x3005 safe");
	EXPECT(rt1316_xps17_write_val_is_safe(0x3004U, a2_0), "Amp2 0x3004 safe");
	EXPECT(rt1316_xps17_write_val_is_safe(0x3005U, a2_1), "Amp2 0x3005 safe");

	/* NULL pointer safety (ASan/UBSan verified) */
	rt1316_xps17_iv_slot_tags(1U, NULL, &a1_1);
	rt1316_xps17_iv_slot_tags(2U, &a2_0, NULL);
	rt1316_xps17_iv_slot_tags(2U, NULL, NULL);

	printf("      Amp1=(0x%02X, 0x%02X), Amp2=(0x%02X, 0x%02X): all 4 tags & slot indices pairwise distinct\n",
	       a1_0, a1_1, a2_0, a2_1);
}

/* ------------------------------------------------------------------------- */
/* 5. P4 250ms Deadman Gate State-Machine Exhaustive Tests                   */
/* ------------------------------------------------------------------------- */
static void assert_guard_regs(const struct rt1316_xps17_guard_state *st,
			      bool expect_unlocked, const char *ctx)
{
	uint8_t c60f = 0xFFU, d002 = 0x00U, d004 = 0x00U;

	rt1316_xps17_guard_target_regs(st, &c60f, &d002, &d004);
	if (expect_unlocked) {
		EXPECT(c60f == 0x05U && d002 == 0xA9U && d004 == 0xA9U,
		       "%s: expected UNLOCKED (0x05,0xA9,0xA9), got (0x%02X,0x%02X,0x%02X)",
		       ctx, c60f, d002, d004);
	} else {
		EXPECT(c60f == 0x01U && d002 == 0xBFU && d004 == 0xBFU,
		       "%s: expected LOCKED (0x01,0xBF,0xBF), got (0x%02X,0x%02X,0x%02X)",
		       ctx, c60f, d002, d004);
	}
}

static void test_deadman_guard_state_machine(void)
{
	struct rt1316_xps17_guard_state st;
	static const uint32_t bad_magics[] = {
		0x00000000U, 0x00000001U, 0xdec1be14U, 0xdec1be16U,
		0xFFFFFFFFU, 0x15bec1deU, 0xdeadbeefU, 0x213e41eaU,
	};
	uint64_t t;
	uint32_t bit, gen_before;
	size_t i;

	printf("[5/5] P4 250ms deadman guard state-machine exhaustive audit...\n");

	/* Constants check */
	EXPECT(RT1316_XPS17_GUARD_MAGIC == 0xdec1be15U, "guard magic mismatch");
	EXPECT(RT1316_XPS17_GUARD_TIMEOUT_MS == 250U, "guard timeout mismatch");

	/* Starts locked (0xC60F=0x01, 0xD002=0xBF, 0xD004=0xBF) */
	rt1316_xps17_guard_init(&st);
	EXPECT(!st.unlocked && !st.stream_active && !st.suspended && st.generation == 0U,
	       "initial guard state must be locked/inactive");
	assert_guard_regs(&st, false, "init");

	/* Even exact valid magic MUST NOT unlock when stream_active == false */
	EXPECT(!rt1316_xps17_guard_kick(&st, 0xdec1be15U, 1000ULL),
	       "valid magic while stream_active==false must be rejected");
	EXPECT(!st.unlocked, "must remain locked when stream inactive");
	assert_guard_regs(&st, false, "valid magic while stream inactive");

	/* Opening stream must NOT auto-unlock */
	EXPECT(!rt1316_xps17_guard_set_stream_active(&st, true),
	       "stream open should not report unlocked->locked transition");
	EXPECT(!st.unlocked, "stream open must not auto-unlock");
	assert_guard_regs(&st, false, "stream open before kick");

	/* Bad magics (list + all 32 single-bit flips of 0xdec1be15) NEVER unlock */
	for (i = 0; i < sizeof(bad_magics) / sizeof(bad_magics[0]); i++) {
		EXPECT(!rt1316_xps17_guard_kick(&st, bad_magics[i], 1000ULL + i),
		       "bad magic 0x%08X unlocked guard!", bad_magics[i]);
		EXPECT(!st.unlocked, "still locked after bad magic 0x%08X", bad_magics[i]);
		assert_guard_regs(&st, false, "bad magic from locked");
	}
	for (bit = 0U; bit < 32U; bit++) {
		uint32_t flipped = RT1316_XPS17_GUARD_MAGIC ^ (1U << bit);
		EXPECT(!rt1316_xps17_guard_kick(&st, flipped, 2000ULL + bit),
		       "1-bit-flipped magic (bit %u: 0x%08X) unlocked guard!", bit, flipped);
		EXPECT(!st.unlocked, "locked after 1-bit flip %u", bit);
	}

	/* Valid magic unlocks ONLY when stream_active && !suspended */
	EXPECT(rt1316_xps17_guard_kick(&st, 0xdec1be15U, 0ULL),
	       "valid magic failed to unlock active unsuspended stream");
	EXPECT(st.unlocked && st.generation == 1U, "unlocked & generation=1");
	assert_guard_regs(&st, true, "unlocked at t=0");

	/* Bad magic while ALREADY unlocked MUST IMMEDIATELY force lock! */
	for (i = 0; i < sizeof(bad_magics) / sizeof(bad_magics[0]); i++) {
		EXPECT(rt1316_xps17_guard_kick(&st, 0xdec1be15U, 5000ULL + i * 10ULL),
		       "re-unlock before bad magic test");
		EXPECT(st.unlocked, "unlocked before bad magic");
		gen_before = st.generation;
		EXPECT(!rt1316_xps17_guard_kick(&st, bad_magics[i], 5001ULL + i * 10ULL),
		       "bad magic 0x%08X must fail when previously unlocked", bad_magics[i]);
		EXPECT(!st.unlocked && st.generation == gen_before + 1U,
		       "bad magic 0x%08X failed to force lock immediately", bad_magics[i]);
		assert_guard_regs(&st, false, "bad magic force-locked");
	}
	/* Also test all 32 single-bit flips force-lock when previously unlocked */
	for (bit = 0U; bit < 32U; bit++) {
		uint32_t flipped = RT1316_XPS17_GUARD_MAGIC ^ (1U << bit);
		EXPECT(rt1316_xps17_guard_kick(&st, 0xdec1be15U, 10000ULL + bit * 10ULL),
		       "re-unlock before bit-flip test");
		EXPECT(!rt1316_xps17_guard_kick(&st, flipped, 10001ULL + bit * 10ULL),
		       "bit-flip %u must force lock", bit);
		EXPECT(!st.unlocked, "locked after bit-flip %u from unlocked", bit);
		assert_guard_regs(&st, false, "bit-flip force-locked");
	}

	/* Continuous heartbeats at t = 0, 100, 200, 300, 400 ms stay unlocked */
	rt1316_xps17_guard_init(&st);
	rt1316_xps17_guard_set_stream_active(&st, true);
	for (t = 0ULL; t <= 400ULL; t += 100ULL) {
		if (t > 0ULL) {
			EXPECT(!rt1316_xps17_guard_tick(&st, t - 1ULL),
			       "spurious expiry at t=%llu", (unsigned long long)(t - 1ULL));
			EXPECT(!rt1316_xps17_guard_tick(&st, t),
			       "spurious expiry at t=%llu", (unsigned long long)t);
		}
		EXPECT(rt1316_xps17_guard_kick(&st, 0xdec1be15U, t),
		       "heartbeat kick at t=%llu failed", (unsigned long long)t);
		EXPECT(st.unlocked, "must stay unlocked at t=%llu", (unsigned long long)t);
		assert_guard_regs(&st, true, "continuous heartbeat");
	}

	/* Silence after t=400: t+0..t+249 stays unlocked; t+250 ALWAYS transitions to locked! */
	for (t = 400ULL; t <= 400ULL + 249ULL; t++) {
		EXPECT(!rt1316_xps17_guard_tick(&st, t),
		       "premature lock at t=%llu (delta=%llu < 250)",
		       (unsigned long long)t, (unsigned long long)(t - 400ULL));
		EXPECT(st.unlocked, "must stay unlocked at delta=%llu",
		       (unsigned long long)(t - 400ULL));
	}
	assert_guard_regs(&st, true, "t+249ms still unlocked");

	gen_before = st.generation;
	EXPECT(rt1316_xps17_guard_tick(&st, 400ULL + 250ULL) == true,
	       "MUST transition unlocked->locked at exact t+250ms!");
	EXPECT(!st.unlocked && st.generation == gen_before + 1U,
	       "must be locked with generation++ at t+250ms");
	assert_guard_regs(&st, false, "t+250ms locked");

	/* Subsequent ticks while already locked return false and stay locked */
	EXPECT(!rt1316_xps17_guard_tick(&st, 400ULL + 251ULL),
	       "already-locked tick at t+251ms should return false");
	assert_guard_regs(&st, false, "already locked at t+251ms");

	/* Direct jump to t+251ms from unlocked state MUST lock */
	EXPECT(rt1316_xps17_guard_kick(&st, 0xdec1be15U, 1000ULL), "re-unlock at t=1000");
	EXPECT(rt1316_xps17_guard_tick(&st, 1251ULL) == true,
	       "direct silence jump to t+251ms must lock!");
	EXPECT(!st.unlocked, "locked at t+251ms");
	assert_guard_regs(&st, false, "t+251ms locked");

	/* Wraparound / monotonic timestamp boundary handling (NO integer overflow/underflow!) */
	/* Case A: Clock rollback (now_ms < last_kick_ms) MUST immediately lock */
	EXPECT(rt1316_xps17_guard_kick(&st, 0xdec1be15U, 5000ULL), "unlock at t=5000");
	EXPECT(rt1316_xps17_guard_tick(&st, 4999ULL) == true,
	       "clock rollback (now_ms < last_kick_ms) must force lock!");
	EXPECT(!st.unlocked, "locked after clock rollback");
	assert_guard_regs(&st, false, "clock rollback locked");

	/* Case B: Near UINT64_MAX saturation (no wraparound to small expire_ms!) */
	EXPECT(rt1316_xps17_guard_kick(&st, 0xdec1be15U, UINT64_MAX - 100ULL),
	       "unlock near UINT64_MAX");
	EXPECT(st.expire_ms == UINT64_MAX,
	       "expire_ms must saturate to UINT64_MAX (got %llu)",
	       (unsigned long long)st.expire_ms);
	EXPECT(!rt1316_xps17_guard_tick(&st, UINT64_MAX - 1ULL),
	       "UINT64_MAX-1 still before saturated expiry");
	EXPECT(st.unlocked, "unlocked at UINT64_MAX-1");
	EXPECT(rt1316_xps17_guard_tick(&st, UINT64_MAX) == true,
	       "UINT64_MAX must expire and lock!");
	EXPECT(!st.unlocked, "locked at UINT64_MAX");
	assert_guard_regs(&st, false, "UINT64_MAX locked");

	/* Case C: Wrapped timestamp back to 0 after kick near UINT64_MAX MUST lock */
	EXPECT(rt1316_xps17_guard_kick(&st, 0xdec1be15U, UINT64_MAX - 50ULL),
	       "unlock at UINT64_MAX-50");
	EXPECT(rt1316_xps17_guard_tick(&st, 0ULL) == true,
	       "wrapped now_ms=0 must force lock (not underflow!)");
	EXPECT(!st.unlocked, "locked on timestamp wraparound");
	assert_guard_regs(&st, false, "wraparound locked");

	/* force_lock (stream close / PM suspend / remove) ALWAYS immediately locks */
	EXPECT(rt1316_xps17_guard_kick(&st, 0xdec1be15U, 20000ULL), "unlock before force_lock");
	EXPECT(rt1316_xps17_guard_force_lock(&st) == true,
	       "force_lock from unlocked must return true");
	EXPECT(!st.unlocked, "locked after force_lock");
	assert_guard_regs(&st, false, "after force_lock");
	EXPECT(rt1316_xps17_guard_force_lock(&st) == false,
	       "second force_lock from locked must return false");

	/* PM suspend: immediately locks, REJECTS kicks while suspended, resume does NOT auto-unlock */
	EXPECT(rt1316_xps17_guard_kick(&st, 0xdec1be15U, 30000ULL), "unlock before suspend");
	EXPECT(rt1316_xps17_guard_set_suspended(&st, true) == true,
	       "set_suspended(true) must immediately transition unlocked->locked");
	EXPECT(!st.unlocked && st.suspended, "locked & suspended");
	assert_guard_regs(&st, false, "suspended");
	EXPECT(!rt1316_xps17_guard_kick(&st, 0xdec1be15U, 30010ULL),
	       "valid kick while suspended==true MUST be rejected!");
	EXPECT(!st.unlocked, "remains locked after kick while suspended");
	assert_guard_regs(&st, false, "kick rejected during suspend");
	EXPECT(!rt1316_xps17_guard_set_suspended(&st, false),
	       "resume must not report lock transition");
	EXPECT(!st.unlocked, "resume MUST NOT auto-unlock!");
	assert_guard_regs(&st, false, "after resume before fresh kick");
	EXPECT(rt1316_xps17_guard_kick(&st, 0xdec1be15U, 30050ULL),
	       "fresh kick after resume unlocks");
	assert_guard_regs(&st, true, "fresh kick after resume");

	/* Stream close: immediately locks, rejects kicks until stream reopens & fresh kick */
	EXPECT(rt1316_xps17_guard_set_stream_active(&st, false) == true,
	       "stream close must immediately transition unlocked->locked");
	EXPECT(!st.unlocked, "locked on stream close");
	assert_guard_regs(&st, false, "stream closed");
	EXPECT(!rt1316_xps17_guard_kick(&st, 0xdec1be15U, 30060ULL),
	       "kick after stream close must be rejected");

	/* Defensive invariant check on guard_target_regs even under forged struct state */
	{
		struct rt1316_xps17_guard_state forged;
		forged.unlocked = true;
		forged.stream_active = true;
		forged.suspended = true;
		forged.generation = 1U;
		forged.last_kick_ms = 0ULL;
		forged.expire_ms = 250ULL;
		assert_guard_regs(&forged, false, "forged unlocked+suspended");

		forged.suspended = false;
		forged.stream_active = false;
		assert_guard_regs(&forged, false, "forged unlocked+inactive");

		assert_guard_regs(NULL, false, "NULL guard state");
	}

	/* NULL resilience across all guard helpers */
	rt1316_xps17_guard_init(NULL);
	EXPECT(!rt1316_xps17_guard_force_lock(NULL), "force_lock(NULL)");
	EXPECT(!rt1316_xps17_guard_kick(NULL, 0xdec1be15U, 0ULL), "kick(NULL)");
	EXPECT(!rt1316_xps17_guard_tick(NULL, 0ULL), "tick(NULL)");
	EXPECT(!rt1316_xps17_guard_set_suspended(NULL, true), "set_suspended(NULL)");
	EXPECT(!rt1316_xps17_guard_set_stream_active(NULL, true), "set_stream_active(NULL)");
	rt1316_xps17_guard_target_regs(&st, NULL, NULL, NULL);

	printf("      All P4 deadman invariants verified (init, 40 bad/bitflip magics, 249/250/251ms, U64 wrap, suspend, close, NULL)\n");
}

int main(void)
{
	printf("=== Dell XPS 17 9720 rt1316-xps17 Exhaustive Offline C Safety Suite ===\n");
	test_forbidden_and_allowlist_sweep();
	test_bq_tables_integrity();
	test_crossover_math_safety();
	test_iv_sense_slot_decollision();
	test_deadman_guard_state_machine();
	printf("=== C Policy Suite Summary: %llu checks, %llu failures ===\n",
	       (unsigned long long)g_checks, (unsigned long long)g_failures);
	return g_failures ? 1 : 0;
}
