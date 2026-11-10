@echo off
REM Verificação mensal de links (Agendador de Tarefas do Windows).
REM Roda o verificador e publica o painel no GitHub; a Vercel atualiza sozinha.
REM Registro de cada execução: relatorios\execucao.log
chcp 65001 >nul
cd /d "%~dp0"
if not exist relatorios mkdir relatorios

REM Se a rede exigir proxy, descomente e ajuste:
REM set HTTPS_PROXY=http://proxy.df.gov.br:8080
REM set HTTP_PROXY=http://proxy.df.gov.br:8080

set PYTHONIOENCODING=utf-8
echo. >> relatorios\execucao.log
echo ===== %date% %time% ===== >> relatorios\execucao.log

python verifica_links.py --saida relatorios --max-paginas 3000 >> relatorios\execucao.log 2>&1
set CODIGO=%ERRORLEVEL%

REM 0 = sem quebrados, 1 = há quebrados (normal); 2 = sítio inacessível: não publica
if %CODIGO% GEQ 2 (
  echo Verificacao falhou ^(codigo %CODIGO%^); painel NAO publicado. >> relatorios\execucao.log
  exit /b %CODIGO%
)

python publicar.py >> relatorios\execucao.log 2>&1
exit /b %ERRORLEVEL%
