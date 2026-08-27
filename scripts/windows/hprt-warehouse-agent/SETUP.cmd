@echo off
setlocal DisableDelayedExpansion
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-WarehouseHprtAgent.ps1" -BaseUrl "https://warehouse-full-ui-staging-characterization.up.railway.app"
if errorlevel 1 exit /b 1
echo.
pause
exit /b 0
