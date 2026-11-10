@echo off
cd /d "C:\Users\rakes\Desktop\TradingBot"

echo ==========================================
echo       STARTING TRADING SYSTEM
echo ==========================================

echo 1. Launching Trading Engine...
start "Trading Engine" cmd /k "python paper_engine.py"

echo.
echo 2. Launching Dashboard Server...
:: Hum 'python -m streamlit' use karenge taki computer confuse na ho
start "Dashboard Server" cmd /k "python -m streamlit run dashboard.py"

echo.
echo 3. Waiting for Dashboard to load...
timeout /t 5

echo.
echo 4. Opening Google Chrome...
start "" "http://localhost:8501"

echo.
echo DONE! System is running.
echo ==========================================
pause
