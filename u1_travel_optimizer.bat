@echo off
rem Starts u1_travel_optimizer.py with the installed Python. Snapmaker Orca runs .bat files directly but not .py files.
py -3 --version >nul 2>nul
if errorlevel 1 goto try_python
py -3 "%~dp0u1_travel_optimizer.py" %*
exit /b

:try_python
python --version >nul 2>nul
if errorlevel 1 goto no_python
python "%~dp0u1_travel_optimizer.py" %*
exit /b

:no_python
echo U1 Travel Optimizer: Python 3 was not found. Install it from python.org. 1>&2
exit /b 1
