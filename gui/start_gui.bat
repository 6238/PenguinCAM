@echo off
REM FRC CAM Post-Processor GUI Launcher
REM For Windows

echo ==========================================
echo FRC CAM Post-Processor GUI
echo ==========================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Python is not installed
    echo Please install Python 3.8 or higher from python.org
    pause
    exit /b 1
)

REM Check if pip is installed
pip --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: pip is not installed
    echo Please install pip
    pause
    exit /b 1
)

REM Check if dependencies are installed
echo Checking dependencies...
python -c "import flask" >nul 2>&1
if %errorlevel% neq 0 (
    echo Installing dependencies...
    pip install -r requirements_gui.txt
    if %errorlevel% neq 0 (
        echo Failed to install dependencies
        pause
        exit /b 1
    )
)

REM Check if post-processor exists
if not exist "frc_cam_postprocessor.py" (
    echo Error: frc_cam_postprocessor.py not found
    echo Please make sure it's in the same directory as this script
    pause
    exit /b 1
)

REM Check if templates directory exists
if not exist "templates" (
    echo Error: templates directory not found
    echo.
    echo You need this structure:
    echo   your-directory\
    echo   ├── frc_cam_gui_app.py
    echo   ├── frc_cam_postprocessor.py
    echo   └── templates\
    echo       └── index.html
    echo.
    echo Create templates directory and put index.html inside it:
    echo   mkdir templates
    echo   move index.html templates\
    pause
    exit /b 1
)

REM Check if index.html exists in templates
if not exist "templates\index.html" (
    echo Error: templates\index.html not found
    echo.
    echo index.html must be inside the templates\ directory
    echo.
    if exist "index.html" (
        echo Found index.html in current directory. Moving it...
        move index.html templates\
        echo Fixed! index.html moved to templates\
    ) else (
        echo index.html not found. Please download it.
        pause
        exit /b 1
    )
)

echo All dependencies ready
echo.
echo Starting server...
echo The browser will open automatically
echo.
echo Press Ctrl+C to stop the server
echo.
echo ==========================================
echo.

REM Start the server
start /B python frc_cam_gui_app.py

REM Wait a moment for server to start
timeout /t 2 /nobreak >nul

REM Open browser
start http://localhost:6238

REM Keep window open
pause
