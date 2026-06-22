@echo off
setlocal
cd /d "%~dp0"

rem === AlForks launcher ====================================================
rem Self-contained: keep this copy's Strava login inside its own .alforks
rem folder (so copies are portable and don't read another install's login).
set "ALFORKS_HOME=%~dp0.alforks"

echo ============================================================
echo   AlForks - starting up
echo ============================================================
echo.
echo This window shows each step. Your data + Strava login live in:
echo   %ALFORKS_HOME%
echo.

rem --- Check GitHub for updates (only when launched from a git clone) -----
call :check_updates

rem --- [1/4] Detect Python (py launcher first, then python) ---------------
echo [1 of 4] Checking that Python is installed...
set "PYCMD="
py -3 --version >nul 2>&1 && set "PYCMD=py -3"
if not defined PYCMD (
    python --version >nul 2>&1 && set "PYCMD=python"
)
if not defined PYCMD (
    echo          Python was NOT found on this computer.
    echo.
    echo   Please install Python 3.10 or newer from:
    echo       https://www.python.org/downloads/
    echo   During install, tick "Add Python to PATH", then run start.bat again.
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%V in ('%PYCMD% --version 2^>^&1') do set "PYVER=%%V"
echo          Found %PYVER%.
echo.

rem --- [2/4] Create the virtual environment on first run -----------------
echo [2 of 4] Preparing the Python environment...
if not exist ".venv" (
    echo          First run - building a private environment ^(one-time, about 30-60 seconds^)...
    %PYCMD% -m venv .venv
    if errorlevel 1 ( echo          FAILED to create the environment. & echo. & pause & exit /b 1 )
    echo          Environment created.
) else (
    echo          Environment already set up - reusing it.
)
echo.

rem --- [3/4] Install / update dependencies -------------------------------
echo [3 of 4] Installing / updating components...
echo          ^(The first time this can take a minute. Please wait - it is not stuck.^)
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 ( echo          FAILED to install the required components. & echo. & pause & exit /b 1 )
echo          Components are ready.
echo.

rem --- First run: bootstrap config.json from the example -----------------
set "FIRSTRUN="
if not exist "config.json" (
    set "FIRSTRUN=1"
    if exist "config.example.json" copy /Y "config.example.json" "config.json" >nul
    echo          First run detected - created your settings file config.json.
    echo.
)

rem --- [4/4] Start the server + open the browser -------------------------
set "OPENURL=http://localhost:5000/"
if defined FIRSTRUN set "OPENURL=http://localhost:5000/guide"
echo [4 of 4] Starting AlForks...
echo          Your browser will open at %OPENURL% in a few seconds.
echo          If it does not, open that address yourself.
echo.
echo   ============================================================
echo     KEEP THIS WINDOW OPEN while you use AlForks.
echo     Close it when you are done to stop the app.
echo   ============================================================
echo.
start "" cmd /c "timeout /t 6 /nobreak >nul & start """" %OPENURL%"

".venv\Scripts\python.exe" app.py

echo.
echo ============================================================
echo   AlForks has shut down - the server is no longer running.
echo.
echo   This is expected when you close this window or press Ctrl+C.
echo   To use AlForks again, just run start.bat.
echo.
echo   If it closed on its own unexpectedly, scroll up for any
echo   error message - the most common cause is another copy of
echo   AlForks already using http://localhost:5000.
echo ============================================================
echo.
pause
endlocal
exit /b 0

rem === Subroutine: auto-update from GitHub ================================
rem Only acts when launched from a git clone; never blocks the app launch.
:check_updates
if not exist ".git\HEAD" goto :eof
git --version >nul 2>&1 || goto :eof
echo Checking GitHub for updates...
for /f %%H in ('git rev-parse HEAD 2^>nul') do set "OLDREV=%%H"
git -c http.lowSpeedLimit=1 -c http.lowSpeedTime=15 pull --ff-only --quiet
if errorlevel 1 (
    echo   Could not check for updates ^(offline, or you have local changes^) - using the current version.
    echo.
    goto :eof
)
for /f %%H in ('git rev-parse HEAD 2^>nul') do set "NEWREV=%%H"
if not "%OLDREV%"=="%NEWREV%" (
    echo   Updated to the latest version - restarting...
    start "" "%~f0"
    exit
)
echo   You're on the latest version.
echo.
goto :eof
