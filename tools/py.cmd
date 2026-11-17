@echo off
setlocal
rem The project's Python: the virtual environment at .venv (built from requirements\core.lock.txt), with src on PYTHONPATH. Override with QUANT_PYTHON.
set "PYTHONPATH=%~dp0..\src;%PYTHONPATH%"
if defined QUANT_PYTHON ( "%QUANT_PYTHON%" %* ) else ( "%~dp0..\.venv\Scripts\python.exe" %* )
