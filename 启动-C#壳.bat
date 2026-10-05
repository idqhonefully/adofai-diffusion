@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title ADOFAI Studio (C# 壳)

set "ROOT=%~dp0"
cd /d "%ROOT%"

rem ---- D 盘那套 .NET（与 C 盘的系统运行时完全隔离）----
set "DOTNET_ROOT=D:\dotnet\sdk10"
set "DOTNET_HOST=D:\dotnet\sdk10\dotnet.exe"

set "SELF=%ROOT%out\Release\net10.0-windows\win-x64\publish\AdofaiStudio.exe"
set "DEV=%ROOT%out\Debug\net10.0-windows\AdofaiStudio.exe"
set "DEVDLL=%ROOT%out\Debug\net10.0-windows\AdofaiStudio.dll"

if /i "%~1"=="--help" goto :help

set "SELFTIME=（没有这个构建）"
set "DEVTIME=（没有这个构建）"
for %%F in ("%SELF%") do set "SELFTIME=%%~tF"
for %%F in ("%DEVDLL%") do set "DEVTIME=%%~tF"

set "PICKKIND="
for /f "usebackq delims=" %%T in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=Get-Item -Force -EA 0 '%SELF%';$d=Get-Item -Force -EA 0 '%DEVDLL%';if($d -and ((-not $s) -or ($d.LastWriteTime -gt $s.LastWriteTime))){'DEV'}else{'SELF'}"`) do set "PICKKIND=%%T"

if /i "%~1"=="--pick" goto :pick

if /i "%PICKKIND%"=="DEV" goto :rundev
if /i "%PICKKIND%"=="SELF" goto :runself
goto :nobuild

:runself
if not exist "%SELF%" goto :nobuild
echo [启动] 自包含版  （%SELFTIME%）
echo.
"%SELF%"
goto :report

:rundev
if not exist "%DEVDLL%" if exist "%SELF%" goto :runself
if not exist "%DEVDLL%" goto :nobuild
if not exist "%DOTNET_HOST%" (
  echo.
  echo [错误] 找不到 D:\dotnet\sdk10\dotnet.exe
  echo         开发版需要它；或改用自包含版：
  echo         dotnet publish -c Release -r win-x64 --self-contained true
  echo.
  pause
  exit /b 2
)
echo [启动] 开发版  （%DEVTIME%，比自包含版新^）
echo        需要 D:\dotnet\sdk10
echo        如果窗口一闪而过，看日志：%ROOT%logs\app.log
echo.
"%DOTNET_HOST%" "%DEVDLL%"
goto :report

:pick
echo.
echo   自包含版  %SELFTIME%   %SELF%
echo   开发版    %DEVTIME%   %DEVDLL%
echo.
echo   --^> 这次会跑：%PICKKIND%
exit /b 0

:nobuild
echo.
echo [错误] 还没编译过，找不到可执行文件。
echo.
echo   编译命令：
echo     cd /d "%ROOT%src\AdofaiStudio.App"
echo     D:\dotnet\sdk10\dotnet.exe publish -c Release -r win-x64 --self-contained true
echo.
pause
exit /b 1

:report
echo.
echo 程序已退出，返回码 = %ERRORLEVEL%
echo 日志：%ROOT%logs\app.log
echo.
pause
exit /b 0

:help
echo.
echo ADOFAI Studio ^(C# 壳^) 启动器
echo.
echo   双击    = 启动界面
echo   --pick  = 只说这次会跑哪一份构建（不启动，排查用）
echo   --help  = 显示这段说明
echo.
echo 两个构建：
echo   自包含版 %SELF%
echo             ^(自带运行时，拷到别的机器也能跑^)
echo   开发版   %DEV%
echo             ^(需 D:\dotnet\sdk10^)
echo.
echo 挑哪一份：按文件时间跑**更新的那个** —— 免得改了代码却跑着旧发布版。
echo.
echo 调试用环境变量：
echo   ADOFAI_SHELL_URL       覆盖页面地址（如指向本地 file:// 验证桥）
echo   ADOFAI_SHELL_CDP=9222  开壳内调试口（tools\shellcdp.mjs 用）
echo   ADOFAI_SHELL_CHROME=frameless  退回"无边框 + 页面自绘标题栏"形态
echo   ADOFAI_SHELL_NC=0      关掉 WebView2 非客户区支持（仅无边框模式有意义）
echo.
echo 日志：%ROOT%logs\app.log
echo.
pause
exit /b 0
