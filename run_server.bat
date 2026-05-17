@echo off
cd /d "C:\Users\Asuss\OneDrive\Desktop\opti-project\Signalor-BE-main"

:: Kill any existing Django server on port 8000
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr " :8000 "') do (
    taskkill /PID %%a /F >nul 2>&1
)
timeout /t 1 /nobreak >nul

set DJANGO_SETTINGS_MODULE=config.settings.development
echo Starting Django server at http://127.0.0.1:8000
echo Press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" manage.py runserver 0.0.0.0:8000
