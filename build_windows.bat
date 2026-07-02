@echo off
REM Build Windows : genere dist\Classeur1.exe
cd /d "%~dp0"
python -m PyInstaller --onefile --windowed --icon=icon.ico --add-data "custom_track.mp3;." --name Classeur1 automation_tool.py
echo.
echo Termine. Executable dans dist\Classeur1.exe
