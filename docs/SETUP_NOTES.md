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

## Stage 1 result

Headless smoke test (RSPduo single tuner, 40 m, 2 MS/s, 400k samples):
`RESULT: OK  rms_dBFS=-60.8`, with an `overload corrected` warning (strong
signals present — normal on a live antenna; add gain reduction if persistent).
Visual confirmation via `server/python/poc/hardware_verify.py` (Qt waterfall)
should be run interactively.
