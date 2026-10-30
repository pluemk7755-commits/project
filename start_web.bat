@echo off
cd /d "%~dp0"
REM Web using the owner's configured QR and bank payee details (manual slip review).
REM Local computer only. Do NOT port-forward this plain HTTP development server.
set RNGD_TEST_MODE=
set RNGD_PAYMENT_LIVE=1
python run.py --host 127.0.0.1 --port 8000
pause
