# Verificador de links quebrados – sítio do IPEDF

Atende à deliberação da 31ª Reunião do CIG: verificação automática e mensal
dos links do sítio eletrônico, para melhoria da transparência ativa.

## O que faz

1. Percorre o sítio a partir de https://www.ipe.df.gov.br/ (todas as páginas internas).
2. Coleta todos os links: páginas, PDFs/documentos, imagens e links externos.
3. Testa cada link e classifica:
   - **QUEBRADO** – erro 404/410/5xx, domínio inexistente, tempo esgotado, ou página
     "não encontrada" servida com código 200 (soft 404, comum no Liferay).
   - **ALERTA** – conferir manualmente: acesso negado a robôs (401/403 externos),
     redireciona para a página inicial ou para o login, limite de requisições (429).
   - **OK** / **IGNORADO** (ex.: Webmail, que exige login).
4. Gera na pasta `relatorios/`:
   - `painel_links_AAAA-MM.html` – **painel interativo** (abre com dois cliques, sem servidor):
     visão geral com indicadores e gráficos, lista de links com problema, aba
     **"Onde corrigir"** (página a página, separando o que está no menu/rodapé),
     inventário completo, páginas percorridas, busca, filtros, exportação CSV e
     botão "Imprimir / PDF" para anexar ao SEI;
   - `painel_links_atual.html` – cópia com nome fixo (útil para publicar na intranet);
   - `historico.json` – resumo de cada execução, que alimenta o gráfico de evolução mensal
     e a marcação de links quebrados "novos" (mantenha a pasta entre execuções);
   - `links_quebrados_AAAA-MM.csv` – só os problemas, com a página onde cada link aparece;
   - `todos_links_AAAA-MM.csv` – inventário completo.

Não altera nada no sítio: só faz leituras (GET/HEAD), respeita o `robots.txt`
e limita a 6 requisições simultâneas.

## Requisitos

Python 3.9 ou superior. Nenhuma biblioteca extra.

## Uso

```bash
python verifica_links.py                       # padrão
python verifica_links.py --sem-externos        # só links do próprio sítio
python verifica_links.py --max-paginas 5000    # sítio maior
python verifica_links.py --dominio infodf.ipe.df.gov.br   # tratar subdomínio como interno
```

Código de saída: `0` sem quebrados · `1` há quebrados · `2` sítio inacessível.

## Agendamento mensal (escolha um)

**GitLab CI** – suba esta pasta num repositório, e em *Build > Pipeline schedules*
crie o agendamento `0 7 1 * *` (fuso America/Sao_Paulo). O relatório fica nos
artefatos do pipeline por 1 ano. Arquivo: `.gitlab-ci.yml`.

**Servidor Linux (cron)**:
```
0 7 1 * * cd /opt/verificador-links && python3 verifica_links.py --saida relatorios >> relatorios/execucao.log 2>&1
```

**Windows (Agendador de Tarefas)** – crie tarefa mensal (dia 1, 07h) executando
`executar_mensal.bat`.

## Observações

- Se a máquina estiver atrás de proxy, defina `HTTPS_PROXY`/`HTTP_PROXY`.
- Use `--inseguro` apenas se a rede interna substituir certificados TLS.
- Links externos com 403 costumam ser sítios que bloqueiam robôs; por isso
  aparecem como ALERTA e não como QUEBRADO.
