@echo off
cd /d "%~dp0"
python app.py
if errorlevel 1 (
  echo The widget could not start. Check Python 3, Tkinter, and Pillow.
  pause
)
