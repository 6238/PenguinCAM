@echo off
REM Batch process multiple DXF files on Windows
REM Usage: batch_process.bat [thickness] [tabs]
REM Example: batch_process.bat 0.25 4

setlocal enabledelayedexpansion

set THICKNESS=%1
if "%THICKNESS%"=="" set THICKNESS=0.25

set TABS=%2
if "%TABS%"=="" set TABS=4

echo =========================================
echo FRC CAM Batch Processor
echo =========================================
echo Material thickness: %THICKNESS%"
echo Number of tabs: %TABS%
echo.

set count=0

for %%f in (*.dxf) do (
    set "dxf_file=%%f"
    set "gcode_file=%%~nf.gcode"
    
    echo Processing: !dxf_file! -^> !gcode_file!
    python frc_cam_postprocessor.py "!dxf_file!" "!gcode_file!" --thickness %THICKNESS% --tabs %TABS%
    
    if errorlevel 1 (
        echo   X Failed
    ) else (
        echo   V Success
        set /a count+=1
    )
    echo.
)

echo =========================================
echo Processed %count% files
echo =========================================
pause
