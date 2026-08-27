@echo off
REM  MT5-Trader - double-click this to start trading.
REM
REM  It does the whole start: finds a Python, checks the two terminals
REM  are open, brings the dependencies up to date, runs the safety
REM  tests, starts the engine and opens the ladders in your browser.
REM
REM  If anything is wrong it stops and says what to do, in words. It
REM  never starts the engine on a failing test suite - that rule is
REM  what keeps a bad build away from a live account.
REM
REM  Plain ASCII on purpose: a console running the default code page
REM  turns anything else into mojibake in the one message that matters.

title MT5-Trader
cd /d "%~dp0"
color 0F

echo.
echo   =====================================================
echo     MT5-Trader - starting up
echo   =====================================================
echo.

REM --- 1. Python -------------------------------------------------------
REM  Three ways a working Python turns up on these boxes, and all three
REM  are normal: the py launcher from a python.org install, a plain
REM  "python" on PATH (a conda or venv prompt has this and NO launcher),
REM  or nothing at all. Assuming the launcher is what tells a machine
REM  that already has Python that it has none.
set "PY="
py -3.11 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3.11"
if not defined PY (
  python -c "import sys; assert sys.version_info[0] == 3" >nul 2>&1
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo   [X] No Python was found on this machine.
  echo.
  echo       Install Python 3.11, 64-bit, from python.org and tick
  echo       "Add python.exe to PATH" during the install. If you use
  echo       conda, open the prompt that has your environment active
  echo       and run this file from there.
  echo.
  pause
  exit /b 1
)
echo   Using Python: %PY%

REM --- 2. The two MetaTrader 5 terminals -------------------------------
tasklist /fi "imagename eq terminal64.exe" 2>nul | find /i "terminal64.exe" >nul
if errorlevel 1 (
  echo   [!] No MetaTrader 5 terminal is running.
  echo.
  echo       Open BOTH terminals, log each into its own account, and
  echo       press the Algo Trading button in each so it turns green.
  echo       Then run this again.
  echo.
  pause
  exit /b 1
)

REM --- 3. Configuration -------------------------------------------------
if not exist config.json (
  echo   [i] First run: creating config.json from the example.
  copy /y config.example.json config.json >nul
)
if not exist .env (
  copy /y .env.example .env >nul
)

REM --- 4. Dependencies --------------------------------------------------
echo   Checking dependencies...
%PY% -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo   [X] The dependencies could not be installed. Check the internet
  echo       connection on this machine and run this again.
  pause
  exit /b 1
)

REM --- 5. The safety tests ---------------------------------------------
echo   Running the safety tests (about 20 seconds)...
%PY% -m pytest tests -q
if errorlevel 1 (
  echo.
  echo   [X] THE SAFETY TESTS FAILED. The engine has NOT been started.
  echo.
  echo       Do not trade on this build. Send the lines above to whoever
  echo       maintains it.
  echo.
  pause
  exit /b 1
)

REM --- 6. Go ------------------------------------------------------------
echo.
echo   All checks passed. Starting the engine and opening the ladders.
echo   Leave this window open - closing it stops trading.
echo.
%PY% start.py --config config.json

echo.
echo   The engine has stopped. Any positions you had are still at the
echo   broker; open the terminals to see them.
pause
