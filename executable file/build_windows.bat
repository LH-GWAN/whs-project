@echo off
REM ============================================================
REM  GPS Tracer - Windows exe 빌드 스크립트
REM
REM  ※ 반드시 Windows에서 실행할 것. PyInstaller는 크로스 컴파일을
REM     지원하지 않아서 Windows exe는 Windows에서만 만들 수 있다.
REM
REM  사전 준비: Python 3.11 또는 3.12 (64bit) 설치
REM             https://www.python.org/downloads/windows/
REM             설치 시 "Add python.exe to PATH" 체크
REM ============================================================

setlocal
cd /d "%~dp0"

echo.
echo [1/4] 가상환경 준비...
if not exist ".venv\" (
    python -m venv .venv
    if errorlevel 1 goto :error
)
call .venv\Scripts\activate.bat

echo.
echo [2/4] 의존성 설치...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo.
echo [3/4] 이전 빌드 정리...
if exist "build\" rmdir /s /q build
if exist "dist\"  rmdir /s /q dist

echo.
echo [4/4] exe 빌드 중... (수 분 소요)
pyinstaller gpstracer.spec --noconfirm
if errorlevel 1 goto :error

echo.
echo ============================================================
echo  빌드 완료
echo.
echo  결과물 : dist\GPSTracer\GPSTracer.exe
echo.
echo  배포할 때는 GPSTracer.exe 하나가 아니라 dist\GPSTracer\
echo  폴더 전체를 통째로 옮겨야 한다(one-dir 방식).
echo  exe 옆의 _internal\ 폴더에 Qt 라이브러리와 분석 엔진이
echo  들어 있어서, exe만 떼어내면 실행되지 않는다.
echo ============================================================
echo.
pause
exit /b 0

:error
echo.
echo *** 빌드 실패 - 위 오류 메시지를 확인하세요 ***
pause
exit /b 1
