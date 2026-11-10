@echo off
chcp 65001 >nul
rem Перетащите файл .m3u на этот .bat или запустите без аргументов (проверит IPTV_work.m3u рядом)
cd /d "%~dp0"
set "PL=%~1"
if "%PL%"=="" set "PL=IPTV_work.m3u"
where py >nul 2>nul && (py -3 iptv_checker.py "%PL%") || (python iptv_checker.py "%PL%")
if exist "%~dpn1_report.html" start "" "%~dpn1_report.html"
if "%~1"=="" if exist "IPTV_work_report.html" start "" "IPTV_work_report.html"
pause
