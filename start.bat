@echo off
chcp 65001 >nul
title LLM4OR 医疗平台
cd /d "%~dp0"
"C:\Users\Administrator\.workbuddy\binaries\python\envs\token-monitor\Scripts\python.exe" -X utf8 app.py
pause
