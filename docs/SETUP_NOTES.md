# Shack PC — Environment Setup Notes

Corrections and machine-specific gotchas discovered while setting up this PC.
These supersede the setup steps in [SDR_PROJECT_CONTEXT.md](SDR_PROJECT_CONTEXT.md),
which was written from the design session before touching the hardware.

## Confirmed environment

| Item | Value |
|------|-------|
| radioconda / GNU Radio | 3.10.12.0 (already installed — no reinstall needed) |
| Python (radioconda) | 3.12.9 |
| SDRPlay API service | `SDRplayAPIService` — running |
| gr-sdrplay3 | `gnuradio-sdrplay3` **3.11.0.8** (py312) |

## Corrections to SDR_PROJECT_CONTEXT.md

1. **Step 4 (reinstall radioconda) was unnecessary** — GNU Radio 3.10.12 was
   already installed and healthy at `C:\Users\MABY\radioconda`.

2. **Step 5 (SDRPlay API)** was already installed and the service running.

3. **Step 6 package name/method was wrong.** `conda install -c conda-forge gr-sdrplay3`
   does not work — there is no such package on conda-forge (nor `gnuradio-sdrplay3`).
   The correct method (per fventuri's WINDOWS.md) is to download the prebuilt
   `.conda` from GitHub releases and install the local file:

   ```
   # matched to Python 3.12 / radioconda 2025.03.14 build
   conda install gnuradio-sdrplay3-3.11.0.8-py312h702a0ab_0.conda
   ```
   Release: https://github.com/fventuri/gr-sdrplay3/releases/tag/v3.11.0.8

## Gotcha: Norton breaks conda TLS (SSL CERTIFICATE_VERIFY_FAILED)

Norton (`nllbIDSAgent` / `aswidsagent.exe`, filter `nllMonFltProxy`) does
TLS interception. conda ships its own CA bundle and does not consult the
Windows trust store, so it cannot verify Norton's injected certificate and
every download fails with:

```
CondaSSLError ... CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate
```

**Fix (keeps verification on — do NOT set `ssl_verify: false`):** export the
Windows trusted roots (which include Norton's root) to a PEM and point conda
at it.

```powershell
# Export Windows root + CA stores to a PEM
$out = "C:\Users\MABY\radioconda\win-trusted-ca.pem"
$lines = foreach ($st in 'Cert:\LocalMachine\Root','Cert:\CurrentUser\Root','Cert:\LocalMachine\CA','Cert:\CurrentUser\CA') {
  Get-ChildItem $st -EA SilentlyContinue | ForEach-Object {
    "-----BEGIN CERTIFICATE-----`n" +
    [Convert]::ToBase64String($_.RawData,'InsertLineBreaks') +
    "`n-----END CERTIFICATE-----"
  }
}
Set-Content $out ($lines -join "`n") -Encoding ascii
```
```
conda config --set ssl_verify C:\Users\MABY\radioconda\win-trusted-ca.pem
```

This is set persistently in `.condarc`. The same PEM can be exported to
`SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` for pip if needed. Re-export if Norton
rotates its root CA.

## Gotcha: gr-sdrplay3 gain is gain *reduction*, and negative values overload

`set_gain(value, "IF"|"RF")` takes **gain reduction in dB** (higher = less gain),
not gain: IF is `[20-59]` (default 40), RF is `[0..dBMax]` (default 0). Passing
negative values (e.g. `-40`) is out of range and clamps to the *minimum*
reduction = *maximum* gain, which overloads the ADC on strong signals
(`rspduo :warning: overload corrected`).

For SSB listening, enabling IF AGC (`set_gain_mode(True)` + `set_agc_setpoint(-30)`)
auto-manages IF reduction and prevents ADC overload; raise RF gain reduction if
strong signals still overload with AGC on. `ssb_demod.py` exposes `--rf-gr`,
`--if-gr`, and `--agc/--no-agc` for this.

## GitHub remote + git TLS (Norton again)

Remote: https://github.com/martinbradford/hf-sdr (private). Auth is via Git
Credential Manager (browser flow, no PAT needed).

Norton's TLS interception can also break `git push` if git uses its bundled
OpenSSL CA store. Fix: use the Windows-native TLS backend, which trusts the
Windows cert store (incl. Norton's root):

```
git config http.sslBackend schannel
```
(Set on this repo; set `--global` if other repos hit the same SSL error.)

## Stage 3 — dual-tuner / diversity (gr-sdrplay3) bring-up

`rspduo_mode` options: `Single Tuner`, `Dual Tuner (diversity reception)`,
`Dual Tuner (independent RX)`, `Master`, `Master (SR=8Mhz)`, `Slave`.
Independent two-tuner use (two separate frequencies) = **`Dual Tuner
(independent RX)`** with `set_center_freq(freq_A, freq_B)`.

Working **diversity** source config (see `server/python/poc/diversity_rx.py`):
- `rspduo_mode="Dual Tuner (diversity reception)"`, `antenna="Both Tuners"`,
  `stream_args(output_type="fc32", channels_size=2)`.
- `set_sample_rate(2_000_000)` — dual-tuner runs the ADC at 2 MHz. In the GRC
  example this is the `sample_rate_non_single_tuner` field; the `sample_rate`
  (62.5e3) field is used **only** in Single Tuner mode — a red herring.
- `set_center_freq(freq)` **single-form** (both tuners locked to same freq),
  `set_bandwidth(1_536_000)` (dual-tuner is Low-IF, fixed BW), single-form gains.

**Gotchas:**
1. In diversity mode use **single-form** setters. The per-tuner
   `set_center_freq(freq_A, freq_B)` / `set_gain(gA, gB, name)` forms are for
   *independent RX* mode and **segfault** in diversity mode ("device is not in
   independent RX mode").
2. gr-sdrplay3 dual-tuner **init can fail** (`sdrplay_api_Init() Error:
   sdrplay_api_Fail`) on Windows + API 3.15 (gr-sdrplay3 issues #48, #54).
   Fix: launch and close **SDRConnect** once to reset the API state, then run.
   A failed init **wedges the device** ("device not found") — recover with
   `Restart-Service SDRplayAPIService`. SDRConnect confirms the hardware/API
   fully support diversity, so this is a gr-sdrplay3-layer issue, not hardware.

**Result:** streams confirmed phase-coherent (correction phase stable ~-23°
over the run); combiner estimates a stable complex correction and the combined
power sits above both branches. Combiner algorithm unit-tested in
`test_combiner.py` (phase error <0.01 rad, within ~0.15 dB of ideal MRC).

## Stage 1 result

Headless smoke test (RSPduo single tuner, 40 m, 2 MS/s, 400k samples):
`RESULT: OK  rms_dBFS=-60.8`, with an `overload corrected` warning (strong
signals present — normal on a live antenna; add gain reduction if persistent).
Visual confirmation via `server/python/poc/hardware_verify.py` (Qt waterfall)
should be run interactively.
