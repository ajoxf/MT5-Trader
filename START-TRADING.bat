@echo off
REM  MT5-Trader — double-click this to start trading.
REM
REM  It does the whole start: checks the two terminals are open, brings
REM  the dependencies up to date, runs the safety tests, starts the
REM  engine and opens the ladders in your browser.
REM
REM  If anything is wrong it stops and says what to do, in words. It
REM  never starts the engine on a failing test suite — that rule is
REM  what keeps a bad build away from a live account.

title MT5-Trader
cd /d "%~dp0"
color 0F

echo.
echo   =====================================================
echo     MT5-Trader — starting up
echo   =====================================================
echo.

REM --- 1. Python -------------------------------------------------------
where py >nul 2>&1
if errorlevel 1 (
  echo   [X] Python is not installed on this machine.
  echo.
  echo       Install Python 3.11, 64-bit, from python.org and tick
  echo       "Add python.exe to PATH" during the install.
  echo.
  pause
  exit /b 1
)

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
py -3.11 -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo   [X] The dependencies could not be installed. Check the internet
  echo       connection on this machine and run this again.
  pause
  exit /b 1
)

REM --- 5. The safety tests ---------------------------------------------
echo   Running the safety tests (about 20 seconds)...
py -3.11 -m pytest tests -q
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
echo   Leave this window open — closing it stops trading.
echo.
py -3.11 start.py --config config.json --open-browser

echo.
echo   The engine has stopped. Any positions you had are still at the
echo   broker; open the terminals to see them.
pause
