# Kernel Safety Verification Report — Dell XPS 17 9720 (`0AFF`) `rt1316-xps17`

**Verdict:** **PASS (ALL 5 VERIFICATION SUITES GREEN)**
**Verified Driver Directory:** `/home/akshaybapat/project/xps17-studio-audio/kernel-driver/rt1316-xps17/`
**Verifier Directory:** `/home/akshaybapat/project/xps17-studio-audio/kernel-driver/verify/`
**Execution Mode:** Strictly offline & read-only on live system; all compiles and tests executed on E-cores (`taskset -c 12-19 nice -n 19`, thermal `58°C < 82°C`). Zero live module loads/unloads, zero live sysfs/register writes, zero reads of `/sys/kernel/debug/regmap/*/access` or `range`.

---

## 1. Audited Driver Artifacts (SHA-256 Fingerprints)

| Artifact | SHA-256 |
|---|---|
| `rt1316-xps17/snd-soc-rt1316-sdw.ko` | `49fff38defb223acd145572dd59c48d23ade879e2bf2df02d67c5a3b5786155b` |
| `rt1316-xps17/dkms.conf` | `9851b98efbb4a9e083e62e91e226df84f0d37fab92fa4c95d67528140891859e` |
| `rt1316-xps17/rt1316-xps17-policy.h` | `3a907b3db5d7b7b8cad336679a5d4a7a1544c9b13aadf917b2139351f3572823` |
| `rt1316-xps17/rt1316-xps17-bq-tables.h` | `0307d810220eee4eaa88ddb9f2b79516137f498a7c4cf34b61089da0a2fc8eaf` |
| `rt1316-xps17/rt1316-sdw.c` | `8e1c3ed56c60cd0852b115a230288517a672c95c3c24517428e954fb7bf86000` |

---

## 2. Master Verification Summary (`run_all_verify.sh`, exit code `0`)

| # | Suite | Result | Exact Counts / Evidence |
|---|---|---|---|
| 1 | Exhaustive C Policy, Crossover Math, IV-Slot & P4 Deadman Suite (`make clean test`, ASan+UBSan) | **PASS** | `346,226 checks, 0 failures`, 0 compiler warnings, 0 ASan/UBSan issues |
| 2 | Static Source & ELF / Vermagic / Modinfo / Symbol Audit (`verify_ko_elf.sh`) | **PASS** | `18 passed, 0 failed, 0 warnings` (786 added/modified lines + compiled `.ko`) |
| 3 | Adversarial Mock-Harness Deployment & Rollback Script Suite (`test_scripts_mock.sh`) | **PASS** | `27 passed, 0 failed` (10/10 privileged/system commands stubbed in `mktemp -d`) |
| 4 | Adversarial Mutation Self-Test Suite (`test_mutants.sh`) | **PASS** | `13 killed, 0 missed` (100% mutant kill rate) |
| 5 | Real Driver Artifacts Isolated Sandbox Dry-Run (`dkms.conf` + built `.ko` + stock `.ko.zst` + `dkms-pre-install.sh`) | **PASS** | All 3 staged scripts dry-run clean + `dkms-pre-install.sh` XU24 gate verified |

---

## 3. Detailed Verification Evidence

### 3.1 Exhaustive Offline C Policy & State-Machine Suite (`test_rt1316_policy.c`)
- **Build Command:** `taskset -c 12-19 nice -n 19 gcc -O2 -std=c11 -Wall -Wextra -Werror -Wpedantic -fsanitize=address,undefined -I../rt1316-xps17 test_rt1316_policy.c -o test_rt1316_policy -fsanitize=address,undefined -lm`
- **Result:** `346,226 checks, 0 failures`
- **Verified Safety Invariants:**
  1. **Forbidden Register Invariant & Full Space Sweep:** All 9 forbidden registers (`0xC000`, `0xC050`, `0xC082`, `0xC093`, `0xC09C`, `0xC09D`, `0xC0A3`, `0xC102`, `0xC60E`) return `rt1316_xps17_reg_is_forbidden == true`, `rt1316_xps17_reg_is_allowed_write == false`, and `rt1316_xps17_write_val_is_safe == false` across all `0x00..0x1FF`. Full `0x0000..0xFFFF` sweep (65,536 addresses) + 65,537 SDCA addresses (`0x41000000..0x41010000` + key SDCA endpoints): **9 forbidden, 140 allowlisted, 0 overlap**.
  2. **134-Write BQ Table Integrity:** Both `RT1316_BQ_PROFILE_STOCK_DELL` and `RT1316_BQ_PROFILE_PRECISION_48K` contain `0` forbidden writes, `134/134` allowlisted safe writes, exact `0x08000000` (`1.000000`, `0.000 dB`) pre/post volume words (`0xC261..0xC270`), and terminal `0xC200=0xC0` pulse.
  3. **P3 Crossover Mathematical Safety (2000 log-spaced points, 20 Hz–20 kHz, `fs = 48 kHz`):**
     - All 8 biquad sections satisfy strict Jury BIBO stability (`|a2| < 1` and `|a1| < 1 + a2`) and pole radius `< 1.0`.
     - `STOCK_DELL SPOL (woofer)`: max `+1.2527 dB @ 30.8 Hz` (`<= +2.0 dB`), `20kHz = -40.03 dB`.
     - `STOCK_DELL SPOR (tweeter)`: max `+1.5570 dB @ 1467.1 Hz` (`<= +2.0 dB`), `20Hz = -70.32 dB`, `100Hz = -41.26 dB` (sub-100 Hz bass high-pass protection verified).
     - `PRECISION_48K SPOL (woofer)`: max `-0.0006 dB @ 220.8 Hz` (`<= +0.1 dB`), `20kHz = -54.89 dB`.
     - `PRECISION_48K SPOR (tweeter)`: max `-0.0000 dB @ 20000.0 Hz` (`<= +0.1 dB`), `20Hz = -86.31 dB`, `100Hz = -55.36 dB`.
     - `PRECISION_48K` 2-way power sum (100 Hz–20 kHz): `min = -0.0070 dB`, `max = +0.0000 dB`.
     - All 4 mixed-profile transient pairs verified `<= +2.0 dB`.
  4. **P3 IV-Sense Slot De-Collision:** `Amp1=(0xA0, 0xB1)`, `Amp2=(0xE4, 0xF5)` — all 4 bytes, high tag nibbles, and low slot indices pairwise distinct.
  5. **P4 250ms Deadman Gate State Machine:** Starts locked (`0xC60F=0x01`, `0xD002=0xBF`, `0xD004=0xBF`); rejects valid magic when `!stream_active` or `suspended`; rejects all 8 adversarial magics + all 32 single-bit flips of `0xdec1be15` (immediately forcing lock if unlocked); continuous heartbeats (`0, 100, 200, 300, 400 ms`) stay unlocked (`0x05, 0xA9, 0xA9`); silence stays unlocked at `t+249 ms` and ALWAYS transitions to locked at `t+250 ms` and `t+251 ms`; safe under `UINT64_MAX` saturation, timestamp wraparound to `0`, clock rollback, `force_lock`, PM suspend/resume (no auto-unlock), stream close, forged states, and NULL pointers.

### 3.2 Static Source & Compiled `.ko` ELF / Vermagic / Symbol Audit (`verify_ko_elf.sh`)
- **Result:** `18 passed, 0 failed, 0 warnings`
- **Part A (Static Source Audit — 786 added/modified lines vs stock `base/rt1316-sdw.c`):**
  - Zero direct `regmap_write` / `regmap_update_bits` / `regmap_write_bits` / `snd_soc_component_*` writes to any of the 9 forbidden registers in added code across `rt1316-sdw.c`, `rt1316-sdw.h`, `rt1316-xps17-bq-tables.h`, `rt1316-xps17-policy.h`.
  - Zero debugfs regmap `access` / `range` references.
  - Stock Realtek protection entries `{0xC60E, 0x05}`, `{0xC102, 0x00}`, `{0xC050, 0x83}` preserved intact.
  - `rt1316-sdw.c` includes `rt1316-xps17-policy.h` (via `rt1316-sdw.h`) and enforces policy register guard helpers.
- **Part B (ELF / Modinfo / Symbol Audit of `snd-soc-rt1316-sdw.ko`):**
  - Valid ELF64 x86_64 relocatable object (`REL`).
  - Exact `vermagic` match with stock `/lib/modules/7.2.15-1-omarchy/kernel/sound/soc/codecs/snd-soc-rt1316-sdw.ko.zst` AND `uname -r`: `'7.2.15-1-omarchy SMP preempt mod_unload '`.
  - `license: GPL`, zero staging/taint flags, `retpoline: Y` matching stock, `srcversion: 6B1D5DCE07931F20A0763FD` (stock: `4899F687DBDD6FA29B41276`).
  - Zero `EXPORT_SYMBOL` leaks (no `__ksymtab*` or `__export_symbol_*`).
  - `66/66` undefined symbols resolved cleanly inside `/lib/modules/7.2.15-1-omarchy/build/Module.symvers` (`0` outside).
  - Safety symbols linked & verified via `nm` + `objdump -dr`: `dmi_match`, `timer_delete_sync` (**3** relocation call sites in `.text`), `mod_timer`.
  - Required ALSA kcontrol & platform strings verified in binary: `"BQ Crossover Switch"`, `"BQ Crossover Profile"`, `"Guard Drive Unlock"`, `"0AFF"`.
  - Zero debugfs regmap `access` / `range` strings in binary.

### 3.3 Staged Scripts, Mock-Harness Suite (`27/27`), Mutation Suite (`13/13`), & Real-Artifact Dry-Run
- **Staged Scripts (`scripts/{lib.sh,swap-in.sh,swap-out.sh,dkms-install.sh,route-timer-guard.sh}`):**
  - Hard DMI `0AFF` guard (`${SYS_DMI_SKU:-/sys/class/dmi/id/product_sku}`), mandatory `--dry-run` vs `--confirm-live` gate, `omarchy-audio-route-recovery.timer` pause + `trap on_exit EXIT` restore + idempotent `route-timer-guard.sh`, automatic mid-swap rollback to stock, `swap-out.sh` `srcversion` stock verification, and `dkms-install.sh` validation/staging of `dkms.conf` + `PRE_INSTALL="dkms-pre-install.sh"` + `BUILD_EXCLUSIVE_KERNEL` + XU24 `asound.state` + `< 82°C` thermal ceiling.
- **Integration Finding Caught & Fixed During Final Run:**
  - Step 5 initially caught that real `rt1316-xps17/dkms.conf` uses `PACKAGE_NAME="snd-soc-rt1316-sdw"` (with `PRE_INSTALL="dkms-pre-install.sh"`) rather than `snd-soc-rt1316-sdw-xps17`. Updated `scripts/dkms-install.sh` to allowlist both package names, derive `/usr/src/<PACKAGE_NAME>-<PACKAGE_VERSION>` from validated `dkms.conf`, validate & stage `PRE_INSTALL="dkms-pre-install.sh"`, and added `srcversion` verification to `scripts/swap-out.sh` plus 3 new mock cases (`27/27` total) and real `dkms-pre-install.sh` XU24 tests — all re-run cleanly to `PASS (ALL CHECKS GREEN)`.
