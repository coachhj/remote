@echo off
chcp 65001 > nul
net session >nul 2>&1
if not errorlevel 1 goto admin
echo 관리자 권한으로 다시 실행합니다. 확인 창에서 [예]를 누르세요.
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b
:admin
echo 예전에 만들어진 차단 규칙을 지우는 중...
powershell -NoProfile -Command "Get-NetFirewallRule | Where-Object { $_.DisplayName -like '*HJ_Remote*' -or $_.DisplayName -eq 'HJ Remote' } | Remove-NetFirewallRule"
echo 허용 규칙을 추가하는 중...
netsh advfirewall firewall add rule name="HJ Remote" dir=in action=allow protocol=TCP localport=8765 profile=any
echo.
echo 완료. 이제 HJ_Remote.exe 를 다시 실행하세요.
pause
