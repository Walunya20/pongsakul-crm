@echo off
chcp 65001 >nul
cd /d "%~dp0"
title CRM พงษ์สกุล (ทดลองบนเครื่อง)

where py >nul 2>nul
if %errorlevel%==0 (set PY=py -3) else (set PY=python)

if not exist "venv\Scripts\python.exe" (
  echo ติดตั้งครั้งแรก กรุณารอสักครู่ ^(ต้องต่ออินเทอร์เน็ตครั้งเดียว^)...
  %PY% -m venv venv
  if errorlevel 1 goto :nopython
  "venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto :failed
)

REM  โหมดทดลองบนเครื่องนี้เท่านั้น (ไม่ใช่ https จึงปิด Secure cookie) ข้อมูลอยู่ในโฟลเดอร์ data_test
set CRM_DATA_DIR=%~dp0data_test
set CRM_COOKIE_SECURE=0
set CRM_HOST=127.0.0.1
set CRM_PORT=5055
set CRM_OPEN=1
if not defined CRM_ADMIN_PASSWORD set CRM_ADMIN_PASSWORD=Test12345678
echo.
echo ทดลองใช้: เปิด http://localhost:5055  ผู้ใช้ admin  รหัสผ่าน Test12345678
echo ^(นี่คือข้อมูลทดลอง ห้ามใช้รหัสนี้บนเซิร์ฟเวอร์จริง^)
echo.
"venv\Scripts\python.exe" app.py
pause
goto :eof

:nopython
echo.
echo ไม่พบ Python — ติดตั้งจาก https://www.python.org/downloads/ แล้วติ๊ก "Add Python to PATH"
pause
goto :eof

:failed
echo.
echo ติดตั้งไม่สำเร็จ ตรวจสอบการเชื่อมต่ออินเทอร์เน็ตแล้วลองใหม่
pause
