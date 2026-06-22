@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo  AlForks update
echo.
echo  IMPORTANT: close AlForks first (close its command window)
echo  so its files aren't in use.
echo ============================================================
echo.
pause

rem --- Find the newest alforks-update-*.zip in this folder ----------------
set "ZIP="
for /f "delims=" %%F in ('dir /b /o-d "alforks-update-*.zip" 2^>nul') do (
    if not defined ZIP set "ZIP=%%F"
)
if not defined ZIP (
    echo No alforks-update-*.zip found in this folder.
    echo Drop the update file you were sent into this folder, then run update.bat again.
    pause
    exit /b 1
)
echo Applying update from: %ZIP%
echo.

rem --- Record the current version and back up app.py + VERSION ------------
set "OLDVER=unknown"
if exist "VERSION" set /p OLDVER=<VERSION
if not exist "backups" mkdir "backups"
if exist "app.py"  copy /Y "app.py"  "backups\app.py.bak"  >nul
if exist "VERSION" copy /Y "VERSION" "backups\VERSION.bak" >nul

rem --- Extract to a temp dir ----------------------------------------------
set "TMP=_update_tmp"
if exist "%TMP%" rmdir /s /q "%TMP%"
mkdir "%TMP%"
powershell -NoProfile -Command "Expand-Archive -LiteralPath '%ZIP%' -DestinationPath '%TMP%' -Force"
if errorlevel 1 ( echo Failed to extract %ZIP%. & rmdir /s /q "%TMP%" & pause & exit /b 1 )

rem --- Don't overwrite THIS running update.bat; stage any newer one -------
if exist "%TMP%\update.bat" (
    copy /Y "%TMP%\update.bat" "update.bat.new" >nul
    del /q "%TMP%\update.bat"
    echo Note: a newer update.bat was saved as update.bat.new ^(replace update.bat with it for next time^).
)

rem --- Copy the new program files over the install (code only) ------------
xcopy "%TMP%\*" "." /E /Y /Q >nul
rmdir /s /q "%TMP%"

set "NEWVER=unknown"
if exist "VERSION" set /p NEWVER=<VERSION

rem --- Update dependencies in case they changed ---------------------------
if exist ".venv\Scripts\python.exe" (
    echo Updating dependencies...
    ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt
)

echo.
echo Updated from version %OLDVER% to %NEWVER%.
echo Your rides, settings, regions, and Strava login were left untouched.
echo Start AlForks again with start.bat.
echo.
pause
endlocal
