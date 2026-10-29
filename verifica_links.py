#!/usr/bin/env python3
"""
Verificador de links quebrados — sítio do IPEDF
================================================

Percorre o sítio a partir da página inicial, coleta todos os links (páginas,
PDFs, imagens, links externos) e testa cada um. Gera:

  painel_links_AAAA-MM.html     -> painel interativo (abre no navegador; imprime p/ SEI)
  historico.json                -> evolução mês a mês (alimenta o gráfico do painel)
  links_quebrados_AAAA-MM.csv   -> planilha só com os problemas
  todos_links_AAAA-MM.csv       -> inventário completo (auditoria)

Só usa a biblioteca padrão do Python 3.9+ (não precisa instalar nada).

Uso básico:
    python verifica_links.py
    python verifica_links.py --max-paginas 3000 --saida relatorios/
    python verifica_links.py --url https://www.ipe.df.gov.br/ --sem-externos

Código de saída: 0 = nenhum link quebrado, 1 = há links quebrados,
2 = falha geral (ex.: sítio fora do ar).
"""

import argparse
import csv
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from threading import Lock

USER_AGENT = "IPEDF-UCTIS-VerificadorLinks/1.0 (+transparencia ativa)"
# Usado para confirmar um link quebrado do jeito que um navegador o abriria
UA_NAVEGADOR = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# Esquemas e padrões que não são "links" de verdade
IGNORAR_ESQUEMAS = ("mailto:", "tel:", "javascript:", "data:", "whatsapp:", "sms:")

# Padrões de URL do Liferay que não devem ser *rastreados* (geram páginas
# infinitas ou exigem login). Eles ainda são *testados* se aparecerem.
NAO_RASTREAR = [
    re.compile(r"p_p_id=com_liferay_login", re.I),
    re.compile(r"[?&]p_p_lifecycle=", re.I),
    re.compile(r"/c/portal/login", re.I),
    re.compile(r"/c/portal/logout", re.I),
    re.compile(r"[?&](cur|delta|resetCur)=", re.I),       # paginação de listas
    re.compile(r"/-/(calendar|blogs)/", re.I),
    re.compile(r"[?&]redirect=", re.I),
]

# Links que nunca vale a pena testar (exigem login por natureza)
NAO_TESTAR = [
    re.compile(r"p_p_id=com_liferay_login", re.I),
    re.compile(r"cas\.gdfnet\.df\.gov\.br/owa", re.I),     # Webmail
]

# Botões "Compartilhar" de redes sociais: não são links de conteúdo
COMPARTILHAR = re.compile(
    r"^https?://(www\.)?("
    r"(twitter|x)\.com/(intent/|share)|"
    r"facebook\.com/(sharer|share\.php|dialog/share)|"
    r"linkedin\.com/(sharearticle|shareArticle|share)|"
    r"(api\.|web\.)?whatsapp\.com/send|wa\.me/|"
    r"t\.me/share|telegram\.me/share|"
    r"pinterest\.com/pin/create|reddit\.com/submit"
    r")",
    re.I,
)

# Prefixo de idioma do Liferay (/pt/pagina == /pagina)
PREFIXO_IDIOMA = re.compile(r"^/(pt|pt-br|pt_br)(/|$)", re.I)

# Texto típico de "página não encontrada" servida com HTTP 200 (soft 404)
SOFT_404 = re.compile(
    r"(p[áa]gina\s+n[ãa]o\s+(foi\s+)?encontrada|"
    r"the\s+requested\s+resource\s+could\s+not\s+be\s+found|"
    r"conte[úu]do\s+n[ãa]o\s+(est[áa]\s+)?dispon[íi]vel|"
    r"not\s+found</title>)",
    re.I,
)

# Endereço de e-mail cadastrado como link de página (faltou o "mailto:")
EMAIL_SEM_MAILTO = re.compile(r"/([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})/?$", re.I)
DETALHE_EMAIL = "E-mail cadastrado sem mailto:"

EXTENSOES_ARQUIVO = re.compile(
    r"\.(pdf|docx?|xlsx?|pptx?|odt|ods|csv|zip|rar|7z|jpe?g|png|gif|svg|webp|mp4|mp3)$",
    re.I,
)


# --------------------------------------------------------------------------- #
# Extração de links
# --------------------------------------------------------------------------- #
class ExtratorLinks(HTMLParser):
    ATRIBUTOS = {
        "a": "href",
        "area": "href",
        "iframe": "src",
        "img": "src",
        "embed": "src",
        "source": "src",
        "link": "href",
        "script": "src",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []          # (url, tag, texto)
        self.base = None
        self._a_atual = None
        self._texto = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "base" and a.get("href"):
            self.base = a["href"]
            return
        attr = self.ATRIBUTOS.get(tag)
        if not attr:
            return
        valor = (a.get(attr) or "").strip()
        if not valor:
            return
        # Só estilos e ícones: <link rel="canonical/alternate"> não é clicável pelo visitante
        # (no Liferay aponta para o endereço "curto" da página, que pode dar 404
        # enquanto o endereço do menu funciona)
        if tag == "link" and (a.get("rel") or "").lower() not in ("stylesheet", "icon", "shortcut icon"):
            return
        if tag == "a":
            self._a_atual = [valor, "a", a.get("title") or a.get("aria-label") or ""]
            self._texto = []
        else:
            rotulo = a.get("alt") or a.get("title") or ""
            self.links.append((valor, tag, rotulo))

    def handle_data(self, data):
        if self._a_atual is not None:
            self._texto.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._a_atual is not None:
            texto = " ".join("".join(self._texto).split())
            url, t, titulo = self._a_atual
            self.links.append((url, t, texto or titulo))
            self._a_atual = None


# --------------------------------------------------------------------------- #
# Estruturas
# --------------------------------------------------------------------------- #
@dataclass
class Resultado:
    url: str
    status: int = 0
    situacao: str = ""          # OK | QUEBRADO | ALERTA | IGNORADO
    detalhe: str = ""
    url_final: str = ""
    tempo_ms: int = 0


@dataclass
class Ocorrencia:
    pagina: str
    texto: str
    tag: str


@dataclass
class Estado:
    ocorrencias: dict = field(default_factory=lambda: defaultdict(list))
    resultados: dict = field(default_factory=dict)
    paginas_visitadas: set = field(default_factory=set)
    titulos: dict = field(default_factory=dict)
    trilhas: dict = field(default_factory=dict)      # url -> caminho de navegação
    lock: Lock = field(default_factory=Lock)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def criar_contexto_ssl(inseguro: bool):
    ctx = ssl.create_default_context()
    if inseguro:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class SemRedirecionar(urllib.request.HTTPRedirectHandler):
    """Deixa o urllib seguir redirecionamentos normalmente (padrão)."""
    pass


def requisitar(url, metodo, timeout, ctx, ler_corpo=0, ua=USER_AGENT):
    req = urllib.request.Request(
        url,
        method=metodo,
        headers={
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9",
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx), SemRedirecionar()
    )
    inicio = time.monotonic()
    try:
        with opener.open(req, timeout=timeout) as resp:
            corpo = resp.read(ler_corpo) if ler_corpo else b""
            return (
                resp.status,
                resp.geturl(),
                resp.headers.get("Content-Type", ""),
                corpo,
                int((time.monotonic() - inicio) * 1000),
                "",
            )
    except urllib.error.HTTPError as e:
        corpo = b""
        try:
            corpo = e.read(ler_corpo) if ler_corpo else b""
        except Exception:
            pass
        return (e.code, url, e.headers.get("Content-Type", "") if e.headers else "",
                corpo, int((time.monotonic() - inicio) * 1000), str(e.reason))
    except urllib.error.URLError as e:
        return (0, url, "", b"", int((time.monotonic() - inicio) * 1000),
                f"Erro de conexão: {e.reason}")
    except Exception as e:  # timeout, reset, etc.
        return (0, url, "", b"", int((time.monotonic() - inicio) * 1000),
                f"{type(e).__name__}: {e}")


BREADCRUMB = re.compile(r'<ol[^>]*class="[^"]*\bbreadcrumb\b[^"]*"[^>]*>(.*?)</ol>', re.I | re.S)


def caminho_navegacao(texto_html: str) -> list:
    """Lê o "Caminho de Navegação" (breadcrumb do Liferay) da página."""
    m = BREADCRUMB.search(texto_html)
    if not m:
        return []
    itens = []
    for li in re.findall(r"<li[^>]*>(.*?)</li>", m.group(1), re.I | re.S):
        t = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", li)).split())
        if t and t not in itens:
            itens.append(t)
    if itens and itens[0].startswith("Instituto de Pesquisa"):
        itens[0] = "Início"
    return itens


# Página de erro do Liferay que redireciona no navegador, ex.: /educacao/ responde 404
# mas traz <meta http-equiv="refresh" content="0; URL='/w/educacao'">
REDIR_CLIENTE = re.compile(
    r"""http-equiv=["']?refresh["']?[^>]*?content=["']?\s*\d+\s*;\s*url=['"]?([^'">\s]+)"""
    r"""|location\.replace\(\s*["']([^"']+)["']""",
    re.I,
)


def destino_cliente(corpo: bytes, ct: str, base: str):
    """Endereço para onde a página redireciona via meta refresh/JavaScript, se houver."""
    if not corpo:
        return None
    m = REDIR_CLIENTE.search(decodificar(corpo[:100_000], ct))
    if not m:
        return None
    return urllib.parse.urljoin(base, html.unescape(m.group(1) or m.group(2)))


def decodificar(corpo: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    for enc in ([m.group(1)] if m else []) + ["utf-8", "latin-1"]:
        try:
            return corpo.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return corpo.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Normalização
# --------------------------------------------------------------------------- #
def normalizar(url_bruta: str, base: str):
    url_bruta = html.unescape(url_bruta.strip())
    if not url_bruta or url_bruta.startswith("#"):
        return None
    if url_bruta.lower().startswith(IGNORAR_ESQUEMAS):
        return None
    try:
        absoluta = urllib.parse.urljoin(base, url_bruta)
        partes = urllib.parse.urlsplit(absoluta)
    except ValueError:
        return url_bruta  # URL malformada: será reportada como quebrada
    if partes.scheme not in ("http", "https"):
        return None
    # Remove fragmento, normaliza host e codifica só o que o navegador codificaria
    # (acentos, espaços). Os escapes %XX existentes ficam como estão: o sítio tem
    # arquivos cujo nome real contém "%25..." e decodificá-los leva a um 404 falso.
    caminho = urllib.parse.quote(partes.path or "/", safe="/%:@!$&'()*+,;=-._~")
    # Na consulta, codifica só o que não é permitido (espaços, acentos, quebras
    # de linha), preservando os escapes %XX já existentes
    consulta = urllib.parse.quote(partes.query, safe="=&%/?:@!$'()*+,;-._~")
    netloc = partes.netloc.lower()
    try:  # domínio com acento -> forma IDNA (xn--...)
        netloc.encode("ascii")
    except UnicodeEncodeError:
        try:
            netloc = netloc.encode("idna").decode("ascii")
        except UnicodeError:
            pass
    return urllib.parse.urlunsplit(
        (partes.scheme.lower(), netloc, caminho, consulta, "")
    )


def canonica(url: str, dominios: set, inicio: str) -> str:
    """Unifica variantes do mesmo endereço interno: com/sem www, http/https,
    prefixo de idioma do Liferay (/pt/...) e barra final."""
    partes = urllib.parse.urlsplit(url)
    host = partes.hostname or ""
    if host not in dominios:
        return url
    ini = urllib.parse.urlsplit(inicio)
    if host.removeprefix("www.") == (ini.hostname or "").removeprefix("www."):
        netloc = ini.netloc
    else:
        netloc = partes.netloc
    caminho = PREFIXO_IDIOMA.sub("/", partes.path) or "/"
    if len(caminho) > 1:
        caminho = caminho.rstrip("/") or "/"
    return urllib.parse.urlunsplit((ini.scheme, netloc, caminho, partes.query, ""))


def mesmo_site(url: str, dominios: set) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    return host in dominios


# --------------------------------------------------------------------------- #
# Teste de um link
# --------------------------------------------------------------------------- #
def testar_link(url, args, ctx, interno):
    if any(p.search(url) for p in NAO_TESTAR):
        return Resultado(url, 0, "IGNORADO", "Exige autenticação")
    if COMPARTILHAR.search(url):
        return Resultado(url, 0, "IGNORADO", "Botão de compartilhar em rede social")
    if not re.fullmatch(r"[a-z0-9.-]+(:\d+)?", urllib.parse.urlsplit(url).netloc.lower()):
        return Resultado(url, 0, "QUEBRADO", "URL malformada (endereço do sítio inválido)")

    ultimo = None
    for tentativa in range(args.tentativas):
        # HEAD primeiro (rápido); muitos servidores não suportam -> GET
        st, final, ct, _, ms, erro = requisitar(url, "HEAD", args.timeout, ctx)
        if st in (0, 400, 403, 404, 405, 429, 500, 501, 502, 503) or st == 0:
            st, final, ct, corpo, ms, erro = requisitar(
                url, "GET", args.timeout, ctx, ler_corpo=200_000 if interno else 20_000
            )
        else:
            corpo = b""
            # Para páginas internas HTML com 200, baixa para checar soft-404
            if interno and st == 200 and "html" in ct.lower():
                st, final, ct, corpo, ms, erro = requisitar(
                    url, "GET", args.timeout, ctx, ler_corpo=200_000
                )
        ultimo = (st, final, ct, corpo, ms, erro)
        if st and st < 500 and st != 429:
            break
        time.sleep(1.5 * (tentativa + 1))

    st, final, ct, corpo, ms, erro = ultimo
    # 404 interno que redireciona no navegador: vale o destino
    if interno and st in (404, 410):
        dest = destino_cliente(corpo, ct, url)
        if dest:
            st2, final2, ct2, corpo2, ms2, erro2 = requisitar(
                dest, "GET", args.timeout, ctx, ler_corpo=200_000)
            if 200 <= st2 < 400:
                st, final, ct, corpo, ms, erro = st2, final2, ct2, corpo2, ms + ms2, ""
    r = Resultado(url, st, "", erro, final, ms)

    if st == 0:
        r.situacao = "QUEBRADO"
        r.detalhe = erro or "Sem resposta"
    elif st >= 400:
        if st in (401, 403) and not interno:
            r.situacao = "ALERTA"
            r.detalhe = f"HTTP {st} – acesso negado (pode exigir login ou bloquear robôs)"
        elif st == 429:
            r.situacao = "ALERTA"
            r.detalhe = "HTTP 429 – servidor limitou requisições; testar novamente"
        else:
            r.situacao = "QUEBRADO"
            r.detalhe = f"HTTP {st} {erro}".strip()
    else:
        r.situacao = "OK"
        if interno and corpo and "html" in ct.lower():
            texto = decodificar(corpo, ct)
            titulo = re.search(r"<title[^>]*>(.*?)</title>", texto, re.I | re.S)
            alvo = (titulo.group(1) if titulo else "") + " " + texto[:50_000]
            if SOFT_404.search(alvo):
                r.situacao = "QUEBRADO"
                r.detalhe = "Página exibe 'não encontrada' (soft 404)"
        if r.situacao == "OK" and final and final.rstrip("/") != url.rstrip("/"):
            # redirecionou para a página inicial/login = provável link morto
            dest = urllib.parse.urlsplit(final)
            if dest.path in ("", "/", "/web/guest", "/pagina-inicial") and \
               urllib.parse.urlsplit(url).path not in ("", "/"):
                r.situacao = "ALERTA"
                r.detalhe = "Redireciona para a página inicial (conteúdo pode ter sido removido)"
            elif "login" in final.lower() and "login" not in url.lower():
                r.situacao = "ALERTA"
                r.detalhe = "Redireciona para tela de login"

    # Confirmação: antes de dar como quebrado, abre de novo como um navegador
    # (alguns servidores recusam robôs ou falham de forma intermitente)
    if r.situacao == "QUEBRADO" and not r.detalhe.startswith("Página exibe"):
        st2, final2, ct2, _, ms2, erro2 = requisitar(
            url, "GET", args.timeout, ctx, ler_corpo=1024, ua=UA_NAVEGADOR)
        if 200 <= st2 < 400:
            return Resultado(url, st2, "OK", "", final2, ms2)
    return r


# --------------------------------------------------------------------------- #
# Rastreamento
# --------------------------------------------------------------------------- #
def rastrear(args):
    ctx = criar_contexto_ssl(args.inseguro)
    inicio = normalizar(args.url, args.url)
    host_ini = urllib.parse.urlsplit(inicio).hostname
    dominios = {host_ini, host_ini.removeprefix("www."), "www." + host_ini.removeprefix("www.")}
    dominios |= set(args.dominio or [])
    inicio = canonica(inicio, dominios, inicio)

    robots = urllib.robotparser.RobotFileParser()
    if not args.ignorar_robots:
        try:
            st, _, ct, corpo, _, _ = requisitar(
                f"{urllib.parse.urlsplit(inicio).scheme}://{host_ini}/robots.txt",
                "GET", args.timeout, ctx, ler_corpo=100_000)
            robots.parse(decodificar(corpo, ct).splitlines() if st == 200 else [])
        except Exception:
            robots.parse([])
    else:
        robots.parse([])

    estado = Estado()
    fila = deque([inicio])
    enfileiradas = {inicio}
    log(f"Início: {inicio}  (domínios internos: {', '.join(sorted(dominios))})")

    # ---- Fase 1: rastrear páginas internas e coletar links -----------------
    def baixar_pagina(url):
        st, final, ct, corpo, ms, erro = requisitar(
            url, "GET", args.timeout, ctx, ler_corpo=5_000_000)
        return url, st, final, ct, corpo, ms, erro

    with ThreadPoolExecutor(max_workers=args.paralelo) as pool:
        em_andamento = set()
        while (fila or em_andamento) and len(estado.paginas_visitadas) < args.max_paginas:
            while fila and len(em_andamento) < args.paralelo * 2 and \
                    len(estado.paginas_visitadas) + len(em_andamento) < args.max_paginas:
                u = fila.popleft()
                em_andamento.add(pool.submit(baixar_pagina, u))
                if args.pausa:
                    time.sleep(args.pausa)
            if not em_andamento:
                break
            pronto = next(as_completed(em_andamento))
            em_andamento.discard(pronto)
            url, st, final, ct, corpo, ms, erro = pronto.result()
            # 404 que redireciona no navegador (meta refresh/JS): segue para o destino
            if st in (404, 410):
                dest = destino_cliente(corpo, ct, url)
                if dest:
                    _, st2, final2, ct2, corpo2, ms2, erro2 = baixar_pagina(dest)
                    if 200 <= st2 < 400:
                        st, final, ct, corpo, ms, erro = st2, final2, ct2, corpo2, ms + ms2, ""
            estado.paginas_visitadas.add(url)

            # A própria página já conta como resultado de teste
            r = Resultado(url, st, "OK" if 200 <= st < 400 else "QUEBRADO",
                          erro if st >= 400 or st == 0 else "", final, ms)
            if "html" in ct.lower() and corpo:
                _t = re.search(r"<title[^>]*>(.*?)</title>", decodificar(corpo[:200_000], ct), re.I | re.S)
                if _t:
                    estado.titulos[url] = " ".join(html.unescape(_t.group(1)).split())[:200]
                trilha = caminho_navegacao(decodificar(corpo[:300_000], ct))
                if trilha:
                    estado.trilhas[url] = trilha
            if r.situacao == "OK" and "html" in ct.lower():
                txt = decodificar(corpo[:200_000], ct)
                t = re.search(r"<title[^>]*>(.*?)</title>", txt, re.I | re.S)
                if SOFT_404.search((t.group(1) if t else "") + " " + txt[:50_000]):
                    r.situacao, r.detalhe = "QUEBRADO", "Página exibe 'não encontrada' (soft 404)"
            if st == 0:
                r.detalhe = erro or "Sem resposta"
            elif st >= 400:
                r.detalhe = f"HTTP {st} {erro}".strip()
            elif r.situacao == "OK" and final and final.rstrip("/") != url.rstrip("/"):
                dest = urllib.parse.urlsplit(final)
                if dest.path in ("", "/", "/web/guest", "/pagina-inicial") and \
                        urllib.parse.urlsplit(url).path not in ("", "/"):
                    r.situacao = "ALERTA"
                    r.detalhe = "Redireciona para a página inicial (conteúdo pode ter sido removido)"
                elif "login" in final.lower() and "login" not in url.lower():
                    r.situacao = "ALERTA"
                    r.detalhe = "Redireciona para tela de login"
            estado.resultados[url] = r
            if r.situacao != "OK":
                continue

            n = len(estado.paginas_visitadas)
            if n % 25 == 0:
                log(f"  {n} páginas lidas, {len(fila)} na fila, "
                    f"{len(estado.ocorrencias)} links distintos encontrados")

            if not (200 <= st < 400) or "html" not in ct.lower():
                continue
            # Se redirecionou para fora do sítio, não extrai links
            if not mesmo_site(final, dominios):
                continue

            parser = ExtratorLinks()
            try:
                parser.feed(decodificar(corpo, ct))
            except Exception:
                pass
            base = urllib.parse.urljoin(final, parser.base) if parser.base else final

            for bruto, tag, texto in parser.links:
                alvo = normalizar(bruto, base)
                if not alvo:
                    continue
                alvo = canonica(alvo, dominios, inicio)
                estado.ocorrencias[alvo].append(Ocorrencia(url, texto[:150], tag))
                if (tag in ("a", "area", "iframe") and mesmo_site(alvo, dominios)
                        and alvo not in enfileiradas
                        and not EXTENSOES_ARQUIVO.search(urllib.parse.urlsplit(alvo).path)
                        and "/documents/" not in alvo
                        and not any(p.search(alvo) for p in NAO_RASTREAR)
                        and robots.can_fetch(USER_AGENT, alvo)):
                    enfileiradas.add(alvo)
                    fila.append(alvo)

    log(f"Rastreamento concluído: {len(estado.paginas_visitadas)} páginas lidas.")
    if len(estado.paginas_visitadas) >= args.max_paginas:
        log(f"  ATENÇÃO: limite de {args.max_paginas} páginas atingido; "
            "use --max-paginas para ampliar.")

    # ---- Fase 2: testar os links ainda não testados -------------------------
    pendentes = [u for u in estado.ocorrencias if u not in estado.resultados]
    # Páginas que falharam no rastreamento por instabilidade (sem resposta/5xx): testar de novo
    pendentes += [u for u, r in estado.resultados.items()
                  if r.situacao == "QUEBRADO" and (r.status == 0 or r.status >= 500)]
    if args.sem_externos:
        externos = [u for u in pendentes if not mesmo_site(u, dominios)]
        for u in externos:
            estado.resultados[u] = Resultado(u, 0, "IGNORADO", "Externo (--sem-externos)")
        pendentes = [u for u in pendentes if mesmo_site(u, dominios)]

    log(f"Testando {len(pendentes)} links (documentos, imagens e externos)...")
    with ThreadPoolExecutor(max_workers=args.paralelo) as pool:
        futuros = {pool.submit(testar_link, u, args, ctx, mesmo_site(u, dominios)): u
                   for u in pendentes}
        for i, f in enumerate(as_completed(futuros), 1):
            u = futuros[f]
            try:
                estado.resultados[u] = f.result()
            except Exception as e:
                estado.resultados[u] = Resultado(u, 0, "QUEBRADO", f"Falha no teste: {e}")
            if i % 100 == 0:
                log(f"  {i}/{len(pendentes)} testados")

    return estado, dominios, inicio


# --------------------------------------------------------------------------- #
# Relatórios
# --------------------------------------------------------------------------- #
def categoria_link(url, tags, dominios):
    caminho = urllib.parse.urlsplit(url).path.lower()
    if not mesmo_site(url, dominios):
        return "externo"
    if "img" in tags or re.search(r"\.(jpe?g|png|gif|svg|webp|ico)$", caminho):
        return "imagem"
    if "/documents/" in caminho or EXTENSOES_ARQUIVO.search(caminho):
        return "documento"
    # Só é "recurso" se aparecer apenas como <script>/<link>; se também estiver
    # num <a> (ex.: item de menu), é um link que o visitante clica
    if tags & {"script", "link"} and not tags & {"a", "area", "iframe"}:
        return "recurso"
    return "pagina"


def gerar_relatorios(estado, dominios, inicio, args, inicio_execucao):
    os.makedirs(args.saida, exist_ok=True)
    agora = datetime.now()
    ref = agora.strftime("%Y-%m")
    duracao = int(time.time() - inicio_execucao)

    # ---- Índice de páginas percorridas ----------------------------------
    paginas = sorted(estado.paginas_visitadas)
    idx = {u: i for i, u in enumerate(paginas)}
    links_por_pagina = defaultdict(int)
    for url, ocs in estado.ocorrencias.items():
        for p in {o.pagina for o in ocs}:
            links_por_pagina[p] += 1

    # E-mail usado como endereço de página: o erro é de cadastro, não de página removida
    for url, r in estado.resultados.items():
        m = EMAIL_SEM_MAILTO.search(urllib.parse.urlsplit(url).path)
        if r.situacao == "QUEBRADO" and m:
            r.detalhe = f"{DETALHE_EMAIL} trocar por mailto:{urllib.parse.unquote(m.group(1))}"

    # ---- Links ------------------------------------------------------------
    links = []
    linhas_csv = []
    for url, ocs in estado.ocorrencias.items():
        r = estado.resultados.get(url) or Resultado(url, 0, "IGNORADO", "Não testado")
        pags = sorted({o.pagina for o in ocs})
        textos = []
        for o in ocs:
            if o.texto and o.texto not in textos:
                textos.append(o.texto)
            if len(textos) >= 5:
                break
        cat = categoria_link(url, {o.tag for o in ocs}, dominios)
        links.append({
            "u": url, "s": r.situacao, "h": r.status or 0, "c": cat,
            "d": r.detalhe, "f": r.url_final if r.url_final and r.url_final != url else "",
            "ms": r.tempo_ms, "x": textos, "p": [idx[p] for p in pags if p in idx],
        })
        linhas_csv.append({
            "situacao": r.situacao, "status_http": r.status or "", "link": url,
            "categoria": cat, "texto_do_link": " | ".join(textos)[:300],
            "caminho_navegacao": " ; ".join(" > ".join(estado.trilhas[p]) for p in pags[:5]
                                            if p in estado.trilhas),
            "qtd_paginas": len(pags),
            "paginas_onde_aparece": " ; ".join(pags[:30]) + (" ; ..." if len(pags) > 30 else ""),
            "detalhe": r.detalhe,
            "url_final": r.url_final if r.url_final != url else "", "tempo_ms": r.tempo_ms,
        })
    # Páginas com erro que ninguém referencia (ex.: ponto de partida)
    for url, r in estado.resultados.items():
        if url not in estado.ocorrencias and r.situacao == "QUEBRADO":
            links.append({"u": url, "s": r.situacao, "h": r.status or 0, "c": "pagina",
                          "d": r.detalhe, "f": "", "ms": r.tempo_ms, "x": [], "p": []})
            linhas_csv.append({"situacao": r.situacao, "status_http": r.status or "",
                               "link": url, "categoria": "pagina", "texto_do_link": "",
                               "caminho_navegacao": "",
                               "qtd_paginas": 0, "paginas_onde_aparece": "(ponto de partida)",
                               "detalhe": r.detalhe, "url_final": "", "tempo_ms": r.tempo_ms})

    ordem = {"QUEBRADO": 0, "ALERTA": 1, "OK": 2, "IGNORADO": 3}
    linhas_csv.sort(key=lambda l: (ordem.get(l["situacao"], 9), l["categoria"], -l["qtd_paginas"], l["link"]))

    # ---- CSVs ---------------------------------------------------------------
    campos = ["situacao", "status_http", "link", "categoria", "texto_do_link", "caminho_navegacao", "qtd_paginas",
              "paginas_onde_aparece", "detalhe", "url_final", "tempo_ms"]
    arq_todos = os.path.join(args.saida, f"todos_links_{ref}.csv")
    arq_quebr = os.path.join(args.saida, f"links_quebrados_{ref}.csv")
    for arq, dados in ((arq_todos, linhas_csv),
                       (arq_quebr, [l for l in linhas_csv if l["situacao"] == "QUEBRADO"])):
        with open(arq, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=campos, delimiter=";")
            w.writeheader(); w.writerows(dados)

    # ---- Resumo e histórico -------------------------------------------------
    cont = defaultdict(int)
    for l in links:
        cont[l["s"]] += 1
    quebr_int = sum(1 for l in links if l["s"] == "QUEBRADO" and l["c"] != "externo")
    resumo = {
        "data": agora.strftime("%Y-%m-%d %H:%M"), "ref": ref,
        "paginas": len(paginas), "links": len(links),
        "quebrados": cont["QUEBRADO"], "quebrados_internos": quebr_int,
        "quebrados_externos": cont["QUEBRADO"] - quebr_int,
        "alertas": cont["ALERTA"], "ok": cont["OK"],
    }
    arq_hist = os.path.join(args.saida, "historico.json")
    historico = []
    try:
        with open(arq_hist, encoding="utf-8") as f:
            historico = json.load(f)
    except (OSError, ValueError):
        pass
    historico = [h for h in historico if h.get("ref") != ref] + [resumo]
    historico.sort(key=lambda h: h.get("data", ""))
    historico = historico[-36:]
    with open(arq_hist, "w", encoding="utf-8") as f:
        json.dump(historico, f, ensure_ascii=False, indent=1)

    # Links quebrados no mês anterior (para marcar "novo" x "persistente")
    anteriores = set()
    previos = sorted(fn for fn in os.listdir(args.saida)
                     if re.fullmatch(r"links_quebrados_\d{4}-\d{2}\.csv", fn) and ref not in fn)
    if previos:
        try:
            with open(os.path.join(args.saida, previos[-1]), encoding="utf-8-sig") as f:
                anteriores = {canonica(row["link"], dominios, inicio)
                              for row in csv.DictReader(f, delimiter=";")
                              if row.get("situacao") == "QUEBRADO"}
        except (OSError, KeyError):
            pass
    for l in links:
        if l["s"] == "QUEBRADO":
            l["n"] = 0 if l["u"] in anteriores else 1   # 1 = novo neste mês

    dados = {
        "meta": {
            "site": inicio, "data": agora.strftime("%d/%m/%Y %H:%M"), "ref": ref,
            "duracao": duracao, "limite": args.max_paginas,
            "limite_atingido": len(paginas) >= args.max_paginas,
            "externos_testados": not args.sem_externos,
            "comparou_com": previos[-1][16:23] if previos else "",
            "demo": bool(getattr(args, "demo", False)),
        },
        "paginas": [{"u": u, "t": estado.titulos.get(u, ""), "b": estado.trilhas.get(u, []),
                     "h": estado.resultados[u].status if u in estado.resultados else 0,
                     "s": estado.resultados[u].situacao if u in estado.resultados else "",
                     "n": links_por_pagina.get(u, 0)} for u in paginas],
        "links": links,
        "historico": historico,
    }
    js = json.dumps(dados, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    arq_html = os.path.join(args.saida, f"painel_links_{ref}.html")
    with open(arq_html, "w", encoding="utf-8") as f:
        f.write(PAINEL_HTML.replace("/*__DADOS__*/null", js))
    try:  # cópia com nome fixo, para quem quiser publicar/abrir sempre o mesmo endereço
        with open(os.path.join(args.saida, "painel_links_atual.html"), "w", encoding="utf-8") as f:
            f.write(PAINEL_HTML.replace("/*__DADOS__*/null", js))
    except OSError:
        pass

    log("")
    log(f"Resumo: {cont['QUEBRADO']} quebrados ({quebr_int} internos, "
        f"{cont['QUEBRADO'] - quebr_int} externos), {cont['ALERTA']} alertas, {cont['OK']} OK.")
    log(f"Painel:     {arq_html}")
    log(f"Problemas:  {arq_quebr}")
    log(f"Inventário: {arq_todos}")
    return cont["QUEBRADO"]


# --------------------------------------------------------------------------- #
def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def main():
    p = argparse.ArgumentParser(description="Verifica links quebrados no sítio do IPEDF.")
    p.add_argument("--url", default="https://www.ipe.df.gov.br/", help="Página inicial do sítio")
    p.add_argument("--dominio", action="append",
                   help="Domínio adicional tratado como interno (pode repetir), "
                        "ex.: --dominio infodf.ipe.df.gov.br")
    p.add_argument("--max-paginas", type=int, default=2000, help="Limite de páginas a percorrer")
    p.add_argument("--paralelo", type=int, default=6, help="Requisições simultâneas")
    p.add_argument("--timeout", type=float, default=20, help="Tempo limite por requisição (s)")
    p.add_argument("--tentativas", type=int, default=2, help="Tentativas para erros 5xx/timeout")
    p.add_argument("--pausa", type=float, default=0.05, help="Pausa entre páginas (s)")
    p.add_argument("--saida", default="relatorios", help="Pasta dos relatórios")
    p.add_argument("--sem-externos", action="store_true", help="Não testar links externos")
    p.add_argument("--ignorar-robots", action="store_true", help="Ignorar robots.txt")
    p.add_argument("--demo", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--inseguro", action="store_true",
                   help="Não validar certificado TLS (use só se a rede interna exigir)")
    args = p.parse_args()

    t0 = time.time()
    try:
        estado, dominios, inicio = rastrear(args)
    except KeyboardInterrupt:
        log("Interrompido.")
        return 2
    if not estado.paginas_visitadas or all(
            r.situacao == "QUEBRADO" for u, r in estado.resultados.items()
            if u in estado.paginas_visitadas):
        log("ERRO: não foi possível acessar o sítio. Verifique a URL, a rede ou o proxy.")
        gerar_relatorios(estado, dominios, inicio, args, t0)
        return 2
    quebrados = gerar_relatorios(estado, dominios, inicio, args, t0)
    return 1 if quebrados else 0


# Modelo do painel (HTML autocontido; os dados entram no lugar do marcador)
PAINEL_HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Painel de Links do Sítio</title>
<style>
:root{
  --bg:#f5f6f8;--surface:#ffffff;--surface-2:#f0f2f5;--fg:#1b2230;--mut:#5d6677;--bd:#dfe3ea;
  --brand:#12355b;--brand-fg:#ffffff;--accent:#1d5fbf;
  --q:#c0362c;--q-bg:#fdecea;--a:#a46a00;--a-bg:#fff5df;--ok:#1d7a4a;--ok-bg:#e6f4ec;--ig:#7b8494;--ig-bg:#eef0f3;
  --shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.08);
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#0f141c;--surface:#171e29;--surface-2:#1e2733;--fg:#e6e9ef;--mut:#9aa3b2;--bd:#2a3443;
  --brand:#0b2440;--brand-fg:#e6e9ef;--accent:#6ea3f5;
  --q:#ff7b70;--q-bg:#3a1c1a;--a:#f0b44c;--a-bg:#36290f;--ok:#5ccf90;--ok-bg:#12301f;--ig:#9aa3b2;--ig-bg:#252d39;
  --shadow:none;}}
:root[data-theme="dark"]{
  --bg:#0f141c;--surface:#171e29;--surface-2:#1e2733;--fg:#e6e9ef;--mut:#9aa3b2;--bd:#2a3443;
  --brand:#0b2440;--brand-fg:#e6e9ef;--accent:#6ea3f5;
  --q:#ff7b70;--q-bg:#3a1c1a;--a:#f0b44c;--a-bg:#36290f;--ok:#5ccf90;--ok-bg:#12301f;--ig:#9aa3b2;--ig-bg:#252d39;
  --shadow:none;}
*{box-sizing:border-box}
html,body{margin:0}
body{font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;background:var(--bg);color:var(--fg)}
a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
header{background:var(--brand);color:var(--brand-fg);padding:18px 24px 0}
header .top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap;max-width:1320px;margin:0 auto}
header h1{margin:0;font-size:20px;font-weight:650;letter-spacing:-.01em}
header .sub{opacity:.8;font-size:13px;margin-top:3px}
header .sub a{color:inherit;text-decoration:underline}
header .acoes{display:flex;gap:8px}
.btn{font:inherit;font-size:13px;border:1px solid var(--bd);background:var(--surface);color:var(--fg);border-radius:6px;padding:6px 11px;cursor:pointer}
.btn:hover{background:var(--surface-2)}
header .btn{background:rgba(255,255,255,.1);border-color:rgba(255,255,255,.25);color:var(--brand-fg)}
header .btn:hover{background:rgba(255,255,255,.18)}
nav{display:flex;gap:2px;max-width:1320px;margin:16px auto 0;overflow-x:auto}
nav button{font:inherit;font-size:13.5px;background:none;border:0;color:var(--brand-fg);opacity:.75;padding:10px 14px;border-radius:6px 6px 0 0;cursor:pointer;white-space:nowrap}
nav button:hover{opacity:1}
nav button.on{background:var(--bg);color:var(--fg);opacity:1;font-weight:600}
nav .cnt{font-size:11px;font-weight:600;background:var(--q);color:#fff;border-radius:9px;padding:1px 6px;margin-left:5px}
main{max-width:1320px;margin:0 auto;padding:20px 24px 48px}
.aviso{border:1px solid var(--a);background:var(--a-bg);color:var(--fg);border-radius:8px;padding:10px 14px;margin-bottom:16px;font-size:13px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
.kpi{background:var(--surface);border:1px solid var(--bd);border-radius:10px;padding:14px 16px;box-shadow:var(--shadow)}
.kpi .v{font-size:28px;font-weight:700;letter-spacing:-.02em;line-height:1.1;font-variant-numeric:tabular-nums}
.kpi .l{color:var(--mut);font-size:12.5px;margin-top:4px}
.kpi .dl{font-size:12px;margin-top:6px}
.kpi.q .v{color:var(--q)} .kpi.a .v{color:var(--a)} .kpi.ok .v{color:var(--ok)}
.up{color:var(--q)} .down{color:var(--ok)} .eq{color:var(--mut)}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}
.box{background:var(--surface);border:1px solid var(--bd);border-radius:10px;padding:16px 18px;box-shadow:var(--shadow);min-width:0}
.box h3{margin:0 0 4px;font-size:14.5px}
.box .hint{color:var(--mut);font-size:12.5px;margin:0 0 12px}
.c4{grid-column:span 4}.c5{grid-column:span 5}.c6{grid-column:span 6}.c7{grid-column:span 7}.c8{grid-column:span 8}.c12{grid-column:span 12}
@media (max-width:980px){.c4,.c5,.c6,.c7,.c8{grid-column:span 12}}
.stack{display:flex;height:14px;border-radius:7px;overflow:hidden;background:var(--surface-2);margin:8px 0 10px}
.stack span{display:block;height:100%}
.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:12.5px;color:var(--mut)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.bars .row{display:grid;grid-template-columns:minmax(90px,38%) 1fr 44px;gap:10px;align-items:center;padding:5px 0;font-size:13px}
.bars .row .lb{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bars .row .tr{height:10px;background:var(--surface-2);border-radius:5px;overflow:hidden;display:flex}
.bars .row .tr span{height:100%}
.bars .row .n{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.bars .row.click{cursor:pointer;border-radius:6px} .bars .row.click:hover{background:var(--surface-2)}
.empty{color:var(--mut);padding:18px 0;text-align:center}
.tipos{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
.tipos button{font:inherit;font-size:13px;border:1px solid var(--bd);background:var(--surface);color:var(--fg);border-radius:18px;padding:6px 13px;cursor:pointer}
.tipos button:hover{background:var(--surface-2)}
.tipos button.on{background:var(--brand);border-color:var(--brand);color:var(--brand-fg)}
.tipos .n{font-weight:650;font-variant-numeric:tabular-nums;margin-left:7px;opacity:.85}
.tools{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px;align-items:center}
.tools input[type=search]{flex:1 1 260px;font:inherit;padding:7px 10px;border:1px solid var(--bd);border-radius:6px;background:var(--surface);color:var(--fg)}
.tools select{font:inherit;font-size:13px;padding:6px 8px;border:1px solid var(--bd);border-radius:6px;background:var(--surface);color:var(--fg)}
.tools .info{color:var(--mut);font-size:12.5px;margin-left:auto}
.tblwrap{background:var(--surface);border:1px solid var(--bd);border-radius:10px;overflow:auto;box-shadow:var(--shadow)}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:8px 10px;border-bottom:1px solid var(--bd);text-align:left;vertical-align:top}
th{background:var(--surface-2);font-weight:600;font-size:12px;color:var(--mut);position:sticky;top:0;cursor:pointer;user-select:none;white-space:nowrap}
th.sorted::after{content:" ▾"} th.sorted.asc::after{content:" ▴"}
tbody tr{cursor:pointer} tbody tr:hover{background:var(--surface-2)}
td.u{word-break:break-all;min-width:240px;max-width:520px}
td.num{text-align:right;font-variant-numeric:tabular-nums}
td small,.mut{color:var(--mut)}
.tag{display:inline-block;font-size:11px;font-weight:650;padding:2px 7px;border-radius:4px;white-space:nowrap}
.t-QUEBRADO{background:var(--q-bg);color:var(--q)}.t-ALERTA{background:var(--a-bg);color:var(--a)}
.t-OK{background:var(--ok-bg);color:var(--ok)}.t-IGNORADO{background:var(--ig-bg);color:var(--ig)}
.chip{display:inline-block;font-size:11px;padding:1px 6px;border-radius:9px;border:1px solid var(--bd);color:var(--mut);margin-left:4px;white-space:nowrap}
.chip.novo{border-color:var(--q);color:var(--q)}
.mais{display:block;margin:12px auto 0}
.pg{background:var(--surface);border:1px solid var(--bd);border-radius:10px;margin-bottom:10px;box-shadow:var(--shadow)}
.pg summary{list-style:none;cursor:pointer;padding:12px 16px;display:flex;gap:12px;align-items:center}
.pg summary::-webkit-details-marker{display:none}
.pg summary .tt{flex:1;min-width:0}
.pg summary .tt b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pg summary .tt a{font-size:12px;word-break:break-all}
.pg summary .ct{display:flex;gap:6px;flex-shrink:0}
.pg .lst{border-top:1px solid var(--bd);padding:4px 16px 10px}
.pg .lst div{padding:7px 0;border-bottom:1px dashed var(--bd);font-size:13px;word-break:break-all}
.pg .lst div:last-child{border-bottom:0}
.secao{font-size:13px;color:var(--mut);margin:18px 0 8px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
#drawer{position:fixed;inset:0;display:none;z-index:20}
#drawer.on{display:block}
#drawer .bg{position:absolute;inset:0;background:rgba(10,15,25,.45)}
#drawer .pn{position:absolute;right:0;top:0;bottom:0;width:min(620px,100%);background:var(--surface);border-left:1px solid var(--bd);padding:20px 22px;overflow:auto}
#drawer h2{font-size:16px;margin:0 0 12px;word-break:break-all}
#drawer dl{display:grid;grid-template-columns:120px 1fr;gap:6px 12px;font-size:13px;margin:0 0 16px}
#drawer dt{color:var(--mut)} #drawer dd{margin:0;word-break:break-all}
#drawer ol{padding-left:20px;font-size:13px} #drawer li{margin:4px 0;word-break:break-all}
#drawer .x{position:absolute;right:14px;top:12px}
svg text{fill:var(--mut);font-size:11px}
.foot{color:var(--mut);font-size:12px;margin-top:28px;text-align:center}
@media (max-width:640px){header{padding:14px 16px 0} main{padding:16px 16px 40px} .kpi .v{font-size:23px}}
@media print{
  header nav,header .acoes,.tools,.tipos,.mais,#drawer,.noprint{display:none!important}
  header{background:none;color:#000;padding:0} body{background:#fff;color:#000}
  .tab{display:none!important} .tab.print{display:block!important}
  .tblwrap,.box,.kpi{box-shadow:none;border-color:#ccc} th{position:static}
  td.u{max-width:none}
}
</style>
</head>
<body>
<header>
  <div class="top">
    <div>
      <h1>Painel de Verificação de Links</h1>
      <div class="sub" id="sub"></div>
    </div>
    <div class="acoes">
      <button class="btn" id="bt-tema" title="Alternar tema">◐ Tema</button>
      <button class="btn" id="bt-print">Imprimir / PDF</button>
    </div>
  </div>
  <nav id="nav"></nav>
</header>
<main>
  <div id="avisos"></div>

  <section class="tab" id="tab-geral">
    <div class="kpis" id="kpis"></div>
    <div class="grid">
      <div class="box c7">
        <h3>Situação dos links testados</h3>
        <p class="hint">Cada link distinto encontrado no sítio conta uma vez, mesmo que apareça em várias páginas.</p>
        <div class="stack" id="stack"></div>
        <div class="legend" id="legend"></div>
        <div class="bars" id="bars-cat" style="margin-top:16px"></div>
      </div>
      <div class="box c5">
        <h3>Evolução mensal</h3>
        <p class="hint">Links quebrados e alertas em cada execução.</p>
        <div id="hist"></div>
      </div>
      <div class="box c7">
        <h3>Páginas com mais links quebrados</h3>
        <p class="hint">Clique para ver o que corrigir em cada página. Links de menu/rodapé estão fora desta contagem.</p>
        <div class="bars" id="bars-pag"></div>
      </div>
      <div class="box c5">
        <h3>Tipos de erro</h3>
        <p class="hint">Por código HTTP ou motivo da falha.</p>
        <div class="bars" id="bars-err"></div>
      </div>
    </div>
  </section>

  <section class="tab print" id="tab-prob">
    <div class="tipos" id="tp-prob"></div>
    <div class="tools">
      <input type="search" id="q-prob" placeholder="Buscar link, texto ou página…">
      <select id="f-prob-s" hidden><option value="">Quebrados</option></select>
      <select id="f-prob-c" hidden></select>
      <button class="btn" id="csv-prob">Exportar CSV</button>
      <span class="info" id="i-prob"></span>
    </div>
    <div class="tblwrap"><table id="t-prob"></table></div>
    <button class="btn mais" id="m-prob">Mostrar mais</button>
  </section>

  <section class="tab" id="tab-onde">
    <div class="tipos" id="tp-onde"></div>
    <div class="tools">
      <input type="search" id="q-onde" placeholder="Filtrar páginas…">
      <select id="f-onde-s" hidden><option value="">Quebrados</option></select>
      <select id="f-onde-c" hidden></select>
      <span class="info" id="i-onde"></span>
    </div>
    <div id="onde"></div>
  </section>

  <section class="tab" id="tab-todos">
    <div class="tools">
      <input type="search" id="q-todos" placeholder="Buscar em todos os links…">
      <select id="f-todos-s"><option value="">Todas as situações</option><option>QUEBRADO</option><option>ALERTA</option><option>OK</option><option>IGNORADO</option></select>
      <select id="f-todos-c"></select>
      <button class="btn" id="csv-todos">Exportar CSV</button>
      <span class="info" id="i-todos"></span>
    </div>
    <div class="tblwrap"><table id="t-todos"></table></div>
    <button class="btn mais" id="m-todos">Mostrar mais</button>
  </section>

  <section class="tab" id="tab-pags">
    <div class="tools">
      <input type="search" id="q-pags" placeholder="Buscar página por endereço ou título…">
      <select id="f-pags-p"><option value="">Todas as páginas</option><option value="1">Só com problemas</option></select>
      <span class="info" id="i-pags"></span>
    </div>
    <div class="tblwrap"><table id="t-pags"></table></div>
    <button class="btn mais" id="m-pags">Mostrar mais</button>
  </section>

  <div class="foot" id="foot"></div>
</main>

<div id="drawer"><div class="bg"></div><div class="pn"><button class="btn x" id="dr-x">Fechar</button><div id="dr"></div></div></div>

<script>
const D = /*__DADOS__*/null;
(function(){
"use strict";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt = n => Number(n).toLocaleString("pt-BR");
const curto = u => u.replace(/^https?:\/\/(www\.)?/, "");
const CAT = {pagina:"Página interna", documento:"Documento (PDF etc.)", imagem:"Imagem", recurso:"Script/estilo", externo:"Sítio externo"};
const COR = {QUEBRADO:"var(--q)", ALERTA:"var(--a)", OK:"var(--ok)", IGNORADO:"var(--ig)"};
const ROT = {QUEBRADO:"Quebrado", ALERTA:"Alerta", OK:"OK", IGNORADO:"Ignorado"};
const PROB = l => l.s === "QUEBRADO";

const P = D.paginas, L = D.links, M = D.meta, H = D.historico || [];
const nPag = P.length;
// Link "de menu/rodapé": aparece em mais de 60% das páginas (só faz sentido com várias páginas)
const LIM_MENU = nPag >= 10 ? Math.ceil(nPag * 0.6) : Infinity;
L.forEach((l, i) => { l.i = i; l.menu = l.p.length >= LIM_MENU; l.busca = (l.u + " " + l.x.join(" ") + " " + l.d).toLowerCase(); });
const probs = L.filter(PROB);
const probPorPag = P.map(() => []);
probs.forEach(l => { if (!l.menu) l.p.forEach(pi => probPorPag[pi].push(l)); });

/* ---------- Tema ---------- */
try { const t = localStorage.getItem("tema"); if (t) document.documentElement.dataset.theme = t; } catch(e) {}
$("#bt-tema").onclick = () => {
  const r = document.documentElement, escuro = r.dataset.theme ? r.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  r.dataset.theme = escuro ? "light" : "dark";
  try { localStorage.setItem("tema", r.dataset.theme); } catch(e) {}
};
$("#bt-print").onclick = () => { abrir("prob"); setTimeout(() => window.print(), 50); };

/* ---------- Cabeçalho ---------- */
const min = Math.floor(M.duracao/60), seg = M.duracao%60;
$("#sub").innerHTML = `<a href="${esc(M.site)}" target="_blank">${esc(curto(M.site))}</a> · verificado em ${esc(M.data)} · duração ${min} min ${seg} s · UCTIS`;
let av = "";
if (M.demo) av += `<div class="aviso"><b>Dados de demonstração.</b> Este painel foi gerado a partir de um sítio de teste, não do sítio real. Execute <code>verifica_links.py</code> na rede do Instituto para obter os dados reais.</div>`;
if (M.limite_atingido) av += `<div class="aviso">O limite de ${fmt(M.limite)} páginas foi atingido; parte do sítio pode não ter sido percorrida. Rode novamente com <code>--max-paginas</code> maior.</div>`;
if (!M.externos_testados) av += `<div class="aviso">Links externos não foram testados nesta execução (<code>--sem-externos</code>).</div>`;
$("#avisos").innerHTML = av;

/* ---------- Abas ---------- */
const ABAS = [["geral","Visão geral"],["prob","Links quebrados"],["onde","Onde corrigir"],["todos","Todos os links"],["pags","Páginas percorridas"]];
const nQ = L.filter(l => l.s === "QUEBRADO").length;
$("#nav").innerHTML = ABAS.map(([k, t]) => `<button data-k="${k}">${t}${k === "prob" && nQ ? `<span class="cnt">${fmt(nQ)}</span>` : ""}</button>`).join("");
const render = {};
function abrir(k){
  document.querySelectorAll("nav button").forEach(b => b.classList.toggle("on", b.dataset.k === k));
  document.querySelectorAll(".tab").forEach(s => s.style.display = s.id === "tab-" + k ? "" : "none");
  if (render[k]) { render[k](); }
  try { history.replaceState(null, "", "#" + k); } catch(e) {}
}
$("#nav").onclick = e => { const b = e.target.closest("button"); if (b) abrir(b.dataset.k); };

/* ---------- Visão geral ---------- */
const cont = {QUEBRADO:0, ALERTA:0, OK:0, IGNORADO:0};
L.forEach(l => cont[l.s] = (cont[l.s] || 0) + 1);
const qInt = L.filter(l => l.s === "QUEBRADO" && l.c !== "externo").length;
const testados = cont.QUEBRADO + cont.ALERTA + cont.OK;
const saude = testados ? (cont.OK / testados * 100) : 100;
const ant = H.length > 1 ? H[H.length - 2] : null;
function delta(atual, anterior, menorMelhor = true){
  if (anterior == null) return "";
  const d = atual - anterior;
  if (!d) return `<div class="dl eq">= igual a ${esc(ant.ref)}</div>`;
  const ruim = menorMelhor ? d > 0 : d < 0;
  return `<div class="dl ${ruim ? "up" : "down"}">${d > 0 ? "▲" : "▼"} ${fmt(Math.abs(d))} vs ${esc(ant.ref)}</div>`;
}
const novos = L.filter(l => l.s === "QUEBRADO" && l.n === 1).length;
$("#kpis").innerHTML = [
  ["", fmt(nPag), "páginas percorridas", delta(nPag, ant && ant.paginas, false).replace(/class="dl (up|down)"/, 'class="dl eq"')],
  ["", fmt(L.length), "links distintos testados", ""],
  ["q", fmt(qInt), "quebrados no próprio sítio", delta(qInt, ant && ant.quebrados_internos)],
  ["q", fmt(cont.QUEBRADO - qInt), "quebrados em sítios externos", delta(cont.QUEBRADO - qInt, ant && ant.quebrados_externos)],
  ["a", fmt(cont.ALERTA), "alertas para conferir", delta(cont.ALERTA, ant && ant.alertas)],
  ["ok", saude.toLocaleString("pt-BR", {maximumFractionDigits: 1}) + "%", "links funcionando", M.comparou_com && cont.QUEBRADO ? `<div class="dl mut">${fmt(novos)} quebrado(s) novo(s) desde ${esc(M.comparou_com)}</div>` : ""],
].map(([c, v, l, d]) => `<div class="kpi ${c}"><div class="v">${v}</div><div class="l">${l}</div>${d}</div>`).join("");

const tot = L.length || 1;
$("#stack").innerHTML = ["QUEBRADO","ALERTA","OK","IGNORADO"].map(s => cont[s] ? `<span style="width:${cont[s]/tot*100}%;background:${COR[s]}" title="${ROT[s]}: ${fmt(cont[s])}"></span>` : "").join("");
$("#legend").innerHTML = ["QUEBRADO","ALERTA","OK","IGNORADO"].map(s => `<span><i style="background:${COR[s]}"></i>${ROT[s]} ${fmt(cont[s])}</span>`).join("");

// Por categoria: barra empilhada de quebrado/alerta/ok
const porCat = {};
L.forEach(l => { const c = porCat[l.c] = porCat[l.c] || {QUEBRADO:0, ALERTA:0, OK:0, IGNORADO:0, t:0}; c[l.s]++; c.t++; });
const maxCat = Math.max(1, ...Object.values(porCat).map(c => c.t));
$("#bars-cat").innerHTML = Object.keys(CAT).filter(k => porCat[k]).map(k => {
  const c = porCat[k];
  return `<div class="row click" data-cat="${k}" title="${fmt(c.QUEBRADO)} quebrados, ${fmt(c.ALERTA)} alertas, ${fmt(c.OK)} OK"><div class="lb">${CAT[k]}</div>
    <div class="tr" style="width:${c.t/maxCat*100}%">${["QUEBRADO","ALERTA","OK","IGNORADO"].map(s => c[s] ? `<span style="width:${c[s]/c.t*100}%;background:${COR[s]}"></span>` : "").join("")}</div>
    <div class="n" style="color:${c.QUEBRADO ? "var(--q)" : "var(--mut)"}">${fmt(c.QUEBRADO)}</div></div>`;
}).join("") + `<div class="mut" style="font-size:12px;margin-top:6px">Número à direita: links quebrados na categoria.</div>`;
$("#bars-cat").onclick = e => { const r = e.target.closest("[data-cat]"); if (!r) return; $("#f-prob-c").value = r.dataset.cat; $("#f-prob-c").dispatchEvent(new Event("input")); abrir("prob"); };

// Páginas com mais problemas
const rankPag = P.map((p, i) => ({i, n: probPorPag[i].length, q: probPorPag[i].filter(l => l.s === "QUEBRADO").length})).filter(x => x.n).sort((a, b) => b.n - a.n).slice(0, 10);
const maxP = rankPag.length ? rankPag[0].n : 1;
$("#bars-pag").innerHTML = rankPag.length ? rankPag.map(x => `<div class="row click" data-pi="${x.i}" title="${esc(P[x.i].u)}">
  <div class="lb">${esc(P[x.i].t || curto(P[x.i].u))}</div>
  <div class="tr" style="width:${x.n/maxP*100}%"><span style="width:${x.q/x.n*100}%;background:var(--q)"></span><span style="width:${(x.n-x.q)/x.n*100}%;background:var(--a)"></span></div>
  <div class="n">${fmt(x.n)}</div></div>`).join("") : `<div class="empty">Nenhuma página com links problemáticos. ✔</div>`;
$("#bars-pag").onclick = e => { const r = e.target.closest("[data-pi]"); if (!r) return; abrir("onde"); const el = document.getElementById("pg-" + r.dataset.pi); if (el) { el.open = true; el.scrollIntoView({behavior: "smooth", block: "start"}); } };

// Tipos de erro
function motivo(l){
  if (l.d && l.d.startsWith("E-mail cadastrado sem mailto:")) return "E-mail cadastrado sem mailto:";
  if (l.h) {
    const nomes = {400:"Requisição inválida",401:"Exige login",403:"Acesso negado",404:"Não encontrado",405:"Método não permitido",410:"Removido",429:"Excesso de requisições",500:"Erro no servidor",502:"Gateway inválido",503:"Indisponível",504:"Tempo esgotado no gateway"};
    if (l.d && /soft 404/.test(l.d)) return "Página “não encontrada” (HTTP 200)";
    if (l.d && /Redireciona/.test(l.d)) return l.d.replace(/\s*\(.*\)/, "");
    return `HTTP ${l.h} – ${nomes[l.h] || "erro"}`;
  }
  if (/Name or service|getaddrinfo|nodename|resolve/i.test(l.d)) return "Domínio inexistente";
  if (/timed? ?out|tempo/i.test(l.d)) return "Tempo esgotado";
  if (/CERTIFICATE|SSL/i.test(l.d)) return "Certificado inválido";
  if (/refused|recusad/i.test(l.d)) return "Conexão recusada";
  return "Sem resposta";
}
const porErr = {};
probs.forEach(l => { const m = motivo(l); (porErr[m] = porErr[m] || {n:0, s:l.s}).n++; });
const errs = Object.entries(porErr).sort((a, b) => b[1].n - a[1].n);
const maxE = errs.length ? errs[0][1].n : 1;
$("#bars-err").innerHTML = errs.length ? errs.map(([m, v]) => `<div class="row click" data-m="${esc(m)}"><div class="lb" title="${esc(m)}">${esc(m)}</div><div class="tr" style="width:${v.n/maxE*100}%"><span style="width:100%;background:${COR[v.s]}"></span></div><div class="n">${fmt(v.n)}</div></div>`).join("") : `<div class="empty">Nenhum erro. ✔</div>`;
$("#bars-err").onclick = e => { const r = e.target.closest("[data-m]"); if (!r) return; $("#q-prob").value = ""; filtroMotivo = r.dataset.m; abrir("prob"); };
L.forEach(l => l.mot = PROB(l) ? motivo(l) : "");

// Histórico (SVG)
(function(){
  const el = $("#hist");
  if (H.length < 2) { el.innerHTML = `<div class="empty">O gráfico aparece a partir da 2ª execução mensal.<br><small>Esta é a ${H.length ? "1ª" : "primeira"} execução registrada.</small></div>`; return; }
  const W = 460, Ht = 200, pl = 34, pr = 10, pt = 12, pb = 28;
  const passo = Math.max(1, Math.ceil(Math.max(1, ...H.map(h => h.quebrados + h.alertas)) / 4)), mx = passo * 4;
  const bw = Math.min(36, (W - pl - pr) / H.length * 0.62);
  const x = i => pl + (i + 0.5) * (W - pl - pr) / H.length;
  const y = v => pt + (Ht - pt - pb) * (1 - v / mx);
  let g = "";
  for (let k = 0; k <= 4; k++) { const v = passo * k; g += `<line x1="${pl}" x2="${W-pr}" y1="${y(v)}" y2="${y(v)}" stroke="var(--bd)"/><text x="${pl-6}" y="${y(v)+4}" text-anchor="end">${v}</text>`; }
  H.forEach((h, i) => {
    const yq = y(h.quebrados), ya = y(h.quebrados + h.alertas);
    g += `<rect x="${x(i)-bw/2}" y="${yq}" width="${bw}" height="${Ht-pb-yq}" fill="var(--q)" rx="2"><title>${h.ref}: ${h.quebrados} quebrados</title></rect>`;
    g += `<rect x="${x(i)-bw/2}" y="${ya}" width="${bw}" height="${yq-ya}" fill="var(--a)" rx="2"><title>${h.ref}: ${h.alertas} alertas</title></rect>`;
    g += `<text x="${x(i)}" y="${Ht-10}" text-anchor="middle">${h.ref.slice(5)}/${h.ref.slice(2,4)}</text>`;
  });
  el.innerHTML = `<svg viewBox="0 0 ${W} ${Ht}" width="100%" role="img" aria-label="Histórico mensal">${g}</svg><div class="legend"><span><i style="background:var(--q)"></i>Quebrados</span><span><i style="background:var(--a)"></i>Alertas</span></div>`;
})();

/* ---------- Tabelas genéricas ---------- */
const optsCat = `<option value="">Todas as categorias</option>` + Object.keys(CAT).filter(k => porCat[k]).map(k => `<option value="${k}">${CAT[k]}</option>`).join("");
$("#f-prob-c").innerHTML = optsCat; $("#f-todos-c").innerHTML = optsCat; $("#f-onde-c").innerHTML = optsCat;
let filtroMotivo = "";

// Botões por tipo de link (controlam o filtro de categoria oculto)
const ORD_TIPO = ["documento", "pagina", "imagem", "externo", "recurso"];
const probPorCat = {};
probs.forEach(l => probPorCat[l.c] = (probPorCat[l.c] || 0) + 1);
function botoesTipo(el, sel){
  $(el).innerHTML = `<button data-c="">Todos<span class="n">${fmt(probs.length)}</span></button>`
    + ORD_TIPO.filter(k => probPorCat[k]).map(k => `<button data-c="${k}">${CAT[k]}<span class="n">${fmt(probPorCat[k])}</span></button>`).join("");
  const marcar = () => $(el).querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.c === $(sel).value));
  $(el).onclick = e => { const b = e.target.closest("button"); if (!b) return; $(sel).value = b.dataset.c; $(sel).dispatchEvent(new Event("input")); };
  $(sel).addEventListener("input", marcar);
  marcar();
}
botoesTipo("#tp-prob", "#f-prob-c");
botoesTipo("#tp-onde", "#f-onde-c");

function celLink(l){
  const chips = (l.menu ? `<span class="chip" title="Aparece em quase todas as páginas – corrigir no menu/rodapé">menu/rodapé</span>` : "")
    + (l.n === 1 && M.comparou_com ? `<span class="chip novo">novo</span>` : "");
  return `<a href="${esc(l.u)}" target="_blank" onclick="event.stopPropagation()">${esc(curto(l.u))}</a>${chips}${l.x.length ? `<br><small>texto: ${esc(l.x.slice(0,2).join(" · "))}</small>` : ""}`;
}
// Caminho de navegação da página (Início › Pesquisas › PMAD), ou o título se não houver
const trilha = p => (p.b && p.b.length) ? p.b.join(" › ") : (p.t || curto(p.u));
function celOnde(l){
  if (!l.p.length) return `<small>(ponto de partida)</small>`;
  const p = P[l.p[0]];
  return `<a href="${esc(p.u)}" target="_blank" onclick="event.stopPropagation()" title="${esc(p.u)}">${esc(trilha(p))}</a>${l.p.length > 1 ? `<br><small>+ ${fmt(l.p.length - 1)} página(s)</small>` : ""}`;
}
function tabela(cfg){
  let limite = 100, ord = cfg.ord, asc = cfg.asc ?? false, linhas = [];
  const t = $(cfg.t);
  function desenhar(){
    linhas = cfg.dados();
    const col = cfg.cols.find(c => c.k === ord);
    if (col && col.v) linhas.sort((a, b) => { const va = col.v(a), vb = col.v(b); return (va < vb ? -1 : va > vb ? 1 : 0) * (asc ? 1 : -1); });
    t.innerHTML = `<thead><tr>${cfg.cols.map(c => `<th data-k="${c.k}" class="${c.k === ord ? "sorted" + (asc ? " asc" : "") : ""}">${c.h}</th>`).join("")}</tr></thead>
      <tbody>${linhas.slice(0, limite).map(r => `<tr data-i="${cfg.id(r)}">${cfg.cols.map(c => `<td class="${c.cls || ""}">${c.r(r)}</td>`).join("")}</tr>`).join("")}</tbody>`;
    if (!linhas.length) t.innerHTML += `<tbody><tr><td colspan="${cfg.cols.length}" class="empty">Nada encontrado com esses filtros.</td></tr></tbody>`;
    $(cfg.info).textContent = `${fmt(Math.min(limite, linhas.length))} de ${fmt(linhas.length)}`;
    $(cfg.mais).style.display = linhas.length > limite ? "" : "none";
  }
  t.addEventListener("click", e => {
    const th = e.target.closest("th");
    if (th) { if (ord === th.dataset.k) asc = !asc; else { ord = th.dataset.k; asc = true; } desenhar(); return; }
    const tr = e.target.closest("tr[data-i]"); if (tr) cfg.click(+tr.dataset.i);
  });
  $(cfg.mais).onclick = () => { limite += 200; desenhar(); };
  cfg.inputs.forEach(s => $(s).addEventListener("input", () => { limite = 100; desenhar(); }));
  return {desenhar, linhas: () => linhas};
}
const ordS = {QUEBRADO:0, ALERTA:1, OK:2, IGNORADO:3};
const colsLink = [
  {k:"s", h:"Situação", r: l => `<span class="tag t-${l.s}">${ROT[l.s]}</span>`, v: l => ordS[l.s]},
  {k:"h", h:"HTTP", cls:"num", r: l => l.h || "—", v: l => l.h},
  {k:"u", h:"Link", cls:"u", r: celLink, v: l => l.u},
  {k:"c", h:"Categoria", r: l => CAT[l.c] || l.c, v: l => l.c},
  {k:"p", h:"Onde está no sítio", r: celOnde, v: l => l.p.length},
  {k:"d", h:"Detalhe", r: l => esc(l.d || ""), v: l => l.d},
];
function filtraLinks(base, q, s, c){
  q = $(q).value.trim().toLowerCase(); s = $(s).value; c = $(c).value;
  return base.filter(l => (!s || l.s === s) && (!c || l.c === c) && (!q || l.busca.includes(q)));
}
const tProb = tabela({t:"#t-prob", info:"#i-prob", mais:"#m-prob", inputs:["#q-prob","#f-prob-s","#f-prob-c"], cols: colsLink, ord:"s", asc:true, id: l => l.i, click: detalhe,
  dados: () => filtraLinks(probs, "#q-prob", "#f-prob-s", "#f-prob-c").filter(l => !filtroMotivo || l.mot === filtroMotivo)});
["#q-prob","#f-prob-s","#f-prob-c"].forEach(s => $(s).addEventListener("input", () => filtroMotivo = ""));
const tTodos = tabela({t:"#t-todos", info:"#i-todos", mais:"#m-todos", inputs:["#q-todos","#f-todos-s","#f-todos-c"], cols: colsLink, ord:"s", asc:true, id: l => l.i, click: detalhe,
  dados: () => filtraLinks(L, "#q-todos", "#f-todos-s", "#f-todos-c")});
P.forEach((p, i) => { p.i = i; p.np = probPorPag[i].length; p.busca = (p.u + " " + p.t + " " + (p.b || []).join(" ")).toLowerCase(); });
const tPags = tabela({t:"#t-pags", info:"#i-pags", mais:"#m-pags", inputs:["#q-pags","#f-pags-p"], ord:"np",
  cols: [
    {k:"t", h:"Página", cls:"u", r: p => `<b>${esc(p.t || "(sem título)")}</b>${p.b && p.b.length ? `<br><small class="mut">${esc(trilha(p))}</small>` : ""}<br><a href="${esc(p.u)}" target="_blank" onclick="event.stopPropagation()">${esc(curto(p.u))}</a>`, v: p => p.t},
    {k:"h", h:"HTTP", cls:"num", r: p => p.s === "QUEBRADO" ? `<span class="tag t-QUEBRADO">${p.h || "erro"}</span>` : p.h, v: p => p.h},
    {k:"n", h:"Links na página", cls:"num", r: p => fmt(p.n), v: p => p.n},
    {k:"np", h:"Com problema", cls:"num", r: p => p.np ? `<b style="color:var(--q)">${fmt(p.np)}</b>` : "0", v: p => p.np},
  ], id: p => p.i, click: i => { abrir("onde"); const el = document.getElementById("pg-" + i); if (el) { el.open = true; el.scrollIntoView({block:"start"}); } },
  dados: () => { const q = $("#q-pags").value.trim().toLowerCase(), so = $("#f-pags-p").value; return P.filter(p => (!so || p.np) && (!q || p.busca.includes(q))); }});
render.prob = tProb.desenhar; render.todos = tTodos.desenhar; render.pags = tPags.desenhar;

/* ---------- Onde corrigir ---------- */
function linhaOnde(l){
  return `<div><span class="tag t-${l.s}">${ROT[l.s]}</span> <a href="${esc(l.u)}" target="_blank">${esc(curto(l.u))}</a>
    ${l.x.length ? ` <small>— texto do link: “${esc(l.x[0])}”</small>` : ""}<br><small>${esc(l.d || "")}</small></div>`;
}
render.onde = function(){
  const q = $("#q-onde").value.trim().toLowerCase(), s = $("#f-onde-s").value, c = $("#f-onde-c").value;
  const ok = l => (!s || l.s === s) && (!c || l.c === c);
  let h = "";
  const menus = probs.filter(l => l.menu && ok(l));
  if (menus.length && (!q || "menu rodapé".includes(q) || menus.some(l => l.busca.includes(q)))) {
    h += `<div class="secao">Menu, cabeçalho ou rodapé — corrigir uma vez no modelo do sítio</div>
      <details class="pg" open><summary><div class="tt"><b>Links presentes em quase todas as páginas</b><small class="mut">Aparecem em ${fmt(LIM_MENU)}+ de ${fmt(nPag)} páginas</small></div>
      <div class="ct"><span class="tag t-QUEBRADO">${menus.filter(l => l.s === "QUEBRADO").length}</span><span class="tag t-ALERTA">${menus.filter(l => l.s === "ALERTA").length}</span></div></summary>
      <div class="lst">${menus.map(linhaOnde).join("")}</div></details>`;
  }
  const lista = P.map((p, i) => ({p, i, ls: probPorPag[i].filter(ok)})).filter(x => x.ls.length && (!q || x.p.busca.includes(q) || x.ls.some(l => l.busca.includes(q))))
    .sort((a, b) => b.ls.length - a.ls.length);
  h += `<div class="secao">Por página (${fmt(lista.length)})</div>`;
  h += lista.length ? lista.map(({p, i, ls}) => `<details class="pg" id="pg-${i}"><summary><div class="tt"><b>${esc(p.t || "(sem título)")}</b>
      <small class="mut">${esc(trilha(p))}</small><br><a href="${esc(p.u)}" target="_blank" onclick="event.stopPropagation()">${esc(curto(p.u))}</a></div>
      <div class="ct">${ls.filter(l => l.s === "QUEBRADO").length ? `<span class="tag t-QUEBRADO">${ls.filter(l => l.s === "QUEBRADO").length} quebrado(s)</span>` : ""}${ls.filter(l => l.s === "ALERTA").length ? `<span class="tag t-ALERTA">${ls.filter(l => l.s === "ALERTA").length} alerta(s)</span>` : ""}</div></summary>
      <div class="lst">${ls.sort((a, b) => ordS[a.s] - ordS[b.s]).map(linhaOnde).join("")}</div></details>`).join("")
    : `<div class="empty">Nenhuma página com links problemáticos${q || s ? " para esse filtro" : ""}. ✔</div>`;
  $("#onde").innerHTML = h;
  $("#i-onde").textContent = `${fmt(lista.length)} página(s)`;
};
["#q-onde","#f-onde-s","#f-onde-c"].forEach(s => $(s).addEventListener("input", render.onde));

/* ---------- Detalhe ---------- */
function detalhe(i){
  const l = L[i];
  $("#dr").innerHTML = `<h2>${esc(l.u)}</h2><dl>
    <dt>Situação</dt><dd><span class="tag t-${l.s}">${ROT[l.s]}</span>${l.n === 1 && M.comparou_com ? ` <span class="chip novo">novo desde ${esc(M.comparou_com)}</span>` : ""}</dd>
    <dt>Código HTTP</dt><dd>${l.h || "—"}</dd>
    <dt>Categoria</dt><dd>${CAT[l.c] || l.c}</dd>
    <dt>Detalhe</dt><dd>${esc(l.d || "—")}</dd>
    ${l.f ? `<dt>Redireciona para</dt><dd><a href="${esc(l.f)}" target="_blank">${esc(l.f)}</a></dd>` : ""}
    <dt>Tempo de resposta</dt><dd>${fmt(l.ms)} ms</dd>
    <dt>Texto do link</dt><dd>${l.x.length ? l.x.map(esc).join("<br>") : "—"}</dd>
    </dl>
    <p><a class="btn" href="${esc(l.u)}" target="_blank">Abrir o link</a></p>
    <h3 style="font-size:14px">Aparece em ${fmt(l.p.length)} página(s)${l.menu ? " — provavelmente no menu ou rodapé" : ""}</h3>
    <ol>${l.p.slice(0, 300).map(pi => `<li><a href="${esc(P[pi].u)}" target="_blank">${esc(trilha(P[pi]))}</a><br><small>${esc(curto(P[pi].u))}</small></li>`).join("")}</ol>
    ${l.p.length > 300 ? `<p class="mut">… e mais ${fmt(l.p.length - 300)}.</p>` : ""}`;
  $("#drawer").classList.add("on");
}
$("#dr-x").onclick = $("#drawer .bg").onclick = () => $("#drawer").classList.remove("on");
document.addEventListener("keydown", e => { if (e.key === "Escape") $("#drawer").classList.remove("on"); });

/* ---------- CSV ---------- */
function baixarCSV(linhas, nome){
  const cab = ["situacao","http","link","categoria","texto_do_link","caminho_navegacao","qtd_paginas","pagina_exemplo","detalhe"];
  const q = v => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const corpo = linhas.map(l => [l.s, l.h || "", l.u, CAT[l.c] || l.c, l.x.join(" | "), l.p.length ? trilha(P[l.p[0]]) : "", l.p.length, l.p.length ? P[l.p[0]].u : "", l.d].map(q).join(";"));
  const blob = new Blob(["﻿" + [cab.join(";"), ...corpo].join("\r\n")], {type: "text/csv;charset=utf-8"});
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = nome; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
$("#csv-prob").onclick = () => baixarCSV(tProb.linhas(), `links_problemas_${M.ref}.csv`);
$("#csv-todos").onclick = () => baixarCSV(tTodos.linhas(), `links_${M.ref}.csv`);

$("#foot").textContent = `Gerado automaticamente por verifica_links.py · ${fmt(nPag)} páginas · ${fmt(L.length)} links · referência ${M.ref}`;
const ini = (location.hash || "").slice(1);
abrir(ABAS.some(a => a[0] === ini) ? ini : "geral");
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
