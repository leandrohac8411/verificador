#!/usr/bin/env python3
"""
Publica o resultado da verificação no GitHub (a Vercel atualiza sozinha).

  - copia relatorios/painel_links_atual.html para index.html e envia à branch main;
  - grava historico.json e o CSV de quebrados mais recente na branch "historico".

Roda depois de verifica_links.py (ver executar_mensal.bat). Usa as credenciais
do git já configuradas nesta máquina.
"""

import glob
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

PASTA = os.path.dirname(os.path.abspath(__file__))
RELATORIOS = os.path.join(PASTA, "relatorios")


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def git(*args, cwd=PASTA, ok=(0,)):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode not in ok:
        raise RuntimeError(f"git {' '.join(args)} falhou:\n{r.stdout}{r.stderr}")
    return r


def publicar_painel(data):
    painel = os.path.join(RELATORIOS, "painel_links_atual.html")
    shutil.copyfile(painel, os.path.join(PASTA, "index.html"))
    git("add", "index.html")
    if git("diff", "--cached", "--quiet", ok=(0, 1)).returncode == 0:
        log("Painel sem alterações.")
        return
    git("commit", "-m", f"Painel de links de {data}", "--", "index.html")
    try:
        git("push", "origin", "HEAD:main")
    except RuntimeError:
        # alguém enviou algo antes: traz as novidades e tenta de novo
        git("pull", "--rebase", "--autostash", "origin", "main")
        git("push", "origin", "HEAD:main")
    log("Painel enviado à branch main (index.html).")


def publicar_historico(data):
    csvs = sorted(glob.glob(os.path.join(RELATORIOS, "links_quebrados_*.csv")))
    git("fetch", "origin", "historico")
    tmp = tempfile.mkdtemp(prefix="historico_")
    try:
        git("worktree", "add", "--detach", tmp, "origin/historico")
        shutil.copy(os.path.join(RELATORIOS, "historico.json"), tmp)
        if csvs:
            shutil.copy(csvs[-1], tmp)
        # mantém só os CSVs dos 2 meses mais recentes
        for antigo in sorted(glob.glob(os.path.join(tmp, "links_quebrados_*.csv")))[:-2]:
            os.remove(antigo)
        git("add", "-A", cwd=tmp)
        if git("diff", "--cached", "--quiet", cwd=tmp, ok=(0, 1)).returncode == 0:
            log("Histórico sem alterações.")
            return
        git("commit", "-m", f"Histórico da verificação de {data}", cwd=tmp)
        git("push", "origin", "HEAD:historico", cwd=tmp)
        log("Histórico enviado à branch historico.")
    finally:
        git("worktree", "remove", "--force", tmp, ok=(0, 128))
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    if not os.path.exists(os.path.join(RELATORIOS, "painel_links_atual.html")):
        log("ERRO: relatorios/painel_links_atual.html não existe; rode verifica_links.py antes.")
        return 1
    data = datetime.now().strftime("%d/%m/%Y")
    try:
        publicar_painel(data)
        publicar_historico(data)
    except RuntimeError as e:
        log(f"ERRO ao publicar: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
