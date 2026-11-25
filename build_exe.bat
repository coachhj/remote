@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo [1/2] 필요한 부품 설치 중...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 (echo 설치에 실패했습니다. Python 설치 여부를 확인하세요. & pause & exit /b 1)
echo [2/2] 실행 파일 만드는 중...
python -m PyInstaller --noconfirm --onefile --name HJ_Remote --add-data "remote.html;." --collect-submodules winrt --hidden-import win32com.client --hidden-import pythoncom remote_server.py
if errorlevel 1 (echo 만들기에 실패했습니다. & pause & exit /b 1)
echo.
echo 완료! dist 폴더의 HJ_Remote.exe 를 원하는 곳에 두고 실행하세요.
explorer dist
pause
