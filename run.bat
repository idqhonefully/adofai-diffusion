@echo off
setlocal
cd /d %~dp0

REM Set threading env vars
set PYTHONNOUSERSITE=1
set MKL_THREADING_LAYER=sequential
set MKL_NUM_THREADS=1
set OMP_NUM_THREADS=1
set KMP_AFFINITY=disabled
set OPENBLAS_NUM_THREADS=1
set NUMEXPR_MAX_THREADS=1

REM Create venv if missing
if not exist venv\Scripts\python.exe (
    echo [setup] Creating virtual environment...
    python -m venv venv
    call venv\Scripts\activate.bat
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
) else (
    call venv\Scripts\activate.bat
)

start "ADOFAI Maker 128" venv\Scripts\python.exe app\web_server.py --port 8081
timeout /t 3 >nul
start "" http://127.0.0.1:8081

echo ============================================================
echo   ADOFAI Maker 128 Started
echo   URL: http://127.0.0.1:8081
echo.
echo   First run: go to "Train Model" tab, select train folder, click "Train All"
echo   Need onset_net.pt / vae.pt / ddpm.pt before generating charts
echo ============================================================
pause