@echo off
title SMC Algo Trading System - Delta Demo
color 0A

echo.
echo  ============================================================
echo   SMC ALGO TRADING SYSTEM — STARTUP
echo   Delta India Demo/Testnet mode (BTCUSD)
echo  ============================================================
echo.

:: ----------------------------------------------------------------
:: 1. Set correct project directory
:: ----------------------------------------------------------------
set "PROJECT_DIR=%~dp0"
cd /d "%PROJECT_DIR%"
set "DELTA_ENV=testnet"
echo [1/7] Project folder: %PROJECT_DIR%

:: ----------------------------------------------------------------
:: 2. Check Python
:: ----------------------------------------------------------------
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  ERROR: Python not found!
    echo  Please install Python from: https://www.python.org/downloads/
    echo  Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)
for /f "tokens=*" %%i in ('python --version 2^>^&1') do echo [2/7] %%i found

:: ----------------------------------------------------------------
:: 3. Check for existing engine instance
:: ----------------------------------------------------------------
set "LOCKFILE=%PROJECT_DIR%data\engine.lock"
if exist "%LOCKFILE%" (
    for /f "tokens=*" %%p in ('type "%LOCKFILE%"') do (
        tasklist /FI "PID eq %%p" 2>NUL | find "python" >NUL
        if not errorlevel 1 (
            echo.
            echo  WARNING: Paper Engine already running (PID %%p).
            echo  Close the existing engine window first, then restart.
            echo.
            pause
            exit /b 1
        )
    )
    echo [3/7] Stale lock cleared.
    del "%LOCKFILE%"
) else (
    echo [3/7] No existing engine instance found. OK.
)

:: ----------------------------------------------------------------
:: 4. Create required directories
:: ----------------------------------------------------------------
if not exist "logs" mkdir logs
if not exist "data" mkdir data
if not exist "config" mkdir config
echo [4/7] Directories verified.

:: ----------------------------------------------------------------
:: 5. Install / verify dependencies
:: ----------------------------------------------------------------
echo [5/7] Checking dependencies...
pip install -r requirements.txt --quiet --no-warn-script-location
if errorlevel 1 (
    echo.
    echo  ERROR: Dependency installation failed!
    echo  Try running manually: pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)
echo [5/7] Dependencies OK.

:: ----------------------------------------------------------------
:: 6. Start Paper Engine in a separate window
:: ----------------------------------------------------------------
echo [6/7] Starting Paper Engine...
start "SMC Algo — Paper Engine" cmd /k "cd /d "%PROJECT_DIR%" && python paper_engine.py"

:: Wait for engine to initialize (DB file created)
echo       Waiting for engine to initialize...
timeout /t 4 /nobreak >nul

:: ----------------------------------------------------------------
:: 7. Start Dashboard (headless — no auto browser from streamlit)
:: ----------------------------------------------------------------
echo [7/7] Starting Dashboard...
start "SMC Algo — Dashboard" cmd /k "cd /d "%PROJECT_DIR%" && python -m streamlit run dashboard.py --server.headless true --server.port 8501 --browser.gatherUsageStats false"

:: Wait for Streamlit to start
echo       Waiting for dashboard to load...
timeout /t 5 /nobreak >nul

:: Open browser ONCE
echo       Opening browser...
start "" "http://localhost:8501"

echo.
echo  ============================================================
echo   SYSTEM STARTED SUCCESSFULLY!
echo.
echo   Dashboard:  http://localhost:8501
echo   Engine:     Running in separate window
echo.
echo   To stop: Close both "Paper Engine" and "Dashboard" windows.
echo  ============================================================
echo.
pause
