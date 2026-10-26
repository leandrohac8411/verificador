@echo off
REM Execução mensal no Windows (Agendador de Tarefas).
REM Ajuste a pasta abaixo para onde o script foi copiado.
cd /d "C:\UCTIS\verificador-links"

REM Se a rede exigir proxy, descomente e ajuste:
REM set HTTPS_PROXY=http://proxy.df.gov.br:8080
REM set HTTP_PROXY=http://proxy.df.gov.br:8080

python verifica_links.py --saida relatorios --max-paginas 3000 >> relatorios\execucao.log 2>&1
