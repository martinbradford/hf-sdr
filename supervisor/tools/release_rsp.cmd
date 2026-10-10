@echo off
rem Double-click (or run) to release the RSP so SDRConnect can open it. Add --force to kill a hand-started server.
"C:\Users\MABY\radioconda\python.exe" "%~dp0release_rsp.py" %*
echo.
pause
