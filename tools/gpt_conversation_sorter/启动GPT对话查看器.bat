@echo off
chcp 65001 >nul
rem 启动「GPT对话记录查看器」:依次尝试可用的 pythonw(无控制台黑窗口)
rem 注意:PATH 中 hermes venv 的 pythonw 运行本程序会挂起,故不直接用 "pythonw"
cd /d "%~dp0"
setlocal
set "PYW="
if exist "%USERPROFILE%\Anaconda3\pythonw.exe" set "PYW=%USERPROFILE%\Anaconda3\pythonw.exe"
if not defined PYW if exist "%USERPROFILE%\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\pythonw.exe" set "PYW=%USERPROFILE%\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\pythonw.exe"
if defined PYW (
    start "" "%PYW%" gpt_conversation_viewer.py
) else (
    start "" python gpt_conversation_viewer.py
)
endlocal
