@echo off
REM Builds dist\GPUZ-Nothing-Monitor.exe (needs Windows and Python 3 with Tkinter)
python -m pip install --upgrade pyinstaller
if errorlevel 1 goto :fail
python -m PyInstaller --noconsole --onefile --clean --name GPUZ-Nothing-Monitor gpuz_nothing.pyw
if errorlevel 1 goto :fail
echo.
echo Done. The exe is in the dist folder.
pause
exit /b 0
:fail
echo.
echo Build failed. See the messages above.
pause
exit /b 1
