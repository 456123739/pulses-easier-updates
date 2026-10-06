@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
rem ============================================================
rem  Pulses Easier 一键打包（Windows / 免安装目录版 + zip）
rem ------------------------------------------------------------
rem  前置：装了 Python 3.11（勾选 Add to PATH）
rem  产物：dist\pulses-easier-<版本>-beta-win64.zip
rem        解压后有 Pulses Easier.exe、assets\（logo）、logs\
rem ============================================================
cd /d "%~dp0"

set VERSION=0.6.1
set OUTNAME=pulses-easier-%VERSION%-beta-win64

echo [1/5] 安装依赖...
python -m pip install --upgrade -r requirements.txt pyinstaller || goto :fail

echo [2/5] 清理旧产物...
if exist build rmdir /s /q build
if exist "dist\Pulses Easier" rmdir /s /q "dist\Pulses Easier"
if exist "dist\%OUTNAME%" rmdir /s /q "dist\%OUTNAME%"

echo [3/5] PyInstaller 打包...
python -m PyInstaller --clean --noconfirm pulses-easier.spec || goto :fail

echo [4/5] 组装发布目录...
mkdir "dist\%OUTNAME%" || goto :fail
xcopy /e /i /q "dist\Pulses Easier" "dist\%OUTNAME%" >nul || goto :fail
rem assets 复制到 exe 同级：程序优先读这一份（玩家能直接换 logo）
xcopy /e /i /q "assets" "dist\%OUTNAME%\assets" >nul || goto :fail
type nul > "dist\%OUTNAME%\logs\.keep"
copy /y "README.md" "dist\%OUTNAME%\README.md" >nul
copy /y "CHANGELOG.md" "dist\%OUTNAME%\CHANGELOG.md" >nul

echo [5/5] 压缩 zip...
powershell -NoProfile -Command "Compress-Archive -Path 'dist\%OUTNAME%\*' -DestinationPath 'dist\%OUTNAME%.zip' -Force" || goto :fail

echo.
echo 完成： dist\%OUTNAME%.zip
echo 目录结构预览：
dir /b "dist\%OUTNAME%"
echo.
echo 提示：本机测试请直接运行 "dist\%OUTNAME%\Pulses Easier.exe"
exit /b 0

:fail
echo.
echo *** 打包失败，看上面的报错 ***
exit /b 1
