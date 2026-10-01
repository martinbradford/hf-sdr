# Launches the HF SDR headless server (control 5555, stream 5556, audio 5557).
# Run with PowerShell 7:  pwsh .\RunServer.ps1
# Windows PowerShell 5.1 refuses it ("running scripts is disabled on this
# system") because its LocalMachine execution policy here is Restricted; pwsh 7
# is RemoteSigned, which allows local unsigned scripts. Right-click "Run with
# PowerShell" uses 5.1, so it fails too.
C:\Users\MABY\radioconda\python.exe "$PSScriptRoot\server\python\headless\server.py" --center 7.15e6
