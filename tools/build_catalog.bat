@echo off
setlocal
cd /d "%~dp0"

echo Home Medicine Cabinet v0.3.4 - build local catalog
py -3 convert_rls.py ^
  --rls "C:\ProgramData\ENC2026\DB\rls.sqlite" ^
  --config "C:\ProgramData\ENC2026\DB\rls_config.db" ^
  --out "%~dp0medicine_catalog.sqlite"

if errorlevel 1 (
  echo.
  echo ERROR: catalog was not created.
  pause
  exit /b 1
)

echo.
echo Ready: %~dp0medicine_catalog.sqlite
echo Copy it to: /config/medicine_cabinet/medicine_catalog.sqlite
pause
