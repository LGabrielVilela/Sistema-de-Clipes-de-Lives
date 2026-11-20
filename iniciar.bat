@echo off
chcp 65001 >nul
title Cortador de Lives
cd /d "%~dp0"

where python >nul 2>nul || (
  echo Python nao encontrado. Instale o Python 3.10 ou mais novo em https://www.python.org/downloads/
  echo Marque a opcao "Add python.exe to PATH" durante a instalacao.
  pause & exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 if not exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe" (
  echo Instalando o ffmpeg...
  winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements --silent
)

rem ---- IA local gratis (Ollama) ----
set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if not exist "%OLLAMA_EXE%" (
  echo Instalando a IA local gratis - Ollama...
  winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements --silent
)
if exist "%OLLAMA_EXE%" (
  curl -s -m 2 http://127.0.0.1:11434/api/version >nul 2>nul || start "" /min "%OLLAMA_EXE%" serve
  timeout /t 3 /nobreak >nul
  "%OLLAMA_EXE%" list | findstr /i "qwen2.5:3b" >nul || (
    echo Baixando o modelo da IA local - cerca de 2 GB, so na primeira vez...
    "%OLLAMA_EXE%" pull qwen2.5:3b
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo Criando ambiente Python pela primeira vez. Isso leva alguns minutos...
  python -m venv .venv || goto :erro
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\pip.exe" install -r requirements.txt || goto :erro
)
if not exist ".env" copy ".env.example" ".env" >nul

echo Atualizando o yt-dlp (os sites mudam com frequencia)...
".venv\Scripts\python.exe" -m pip install -q -U "yt-dlp[default]"
".venv\Scripts\pip.exe" install -q -r requirements.txt

".venv\Scripts\python.exe" -m app.server
pause
exit /b

:erro
echo Algo deu errado na instalacao. Verifique sua internet e tente de novo.
pause
