#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Domadores de Feras - servidor do bot do Telegram.

Usa apenas a biblioteca padrao do Python (3.6+), sem dependencias.
- Bot: /start, /jogar, /ajuda, /ranking e o botao que abre o jogo.
- Pagamentos VIP em Telegram Stars (moeda XTR).
- API HTTP (atras do Caddy com HTTPS) para o jogo:
    POST /api/fatura     -> cria o link de pagamento de um pacote
    POST /api/resgatar   -> entrega as compras pagas ainda nao entregues
    POST /api/ranking    -> envia o progresso do jogador
    GET  /api/ranking    -> top 50
    GET  /api/saude      -> verificacao simples
Toda chamada do jogo leva o initData do Telegram, validado com o token do bot.
"""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn


class ServidorThreads(ThreadingMixIn, HTTPServer):
    daemon_threads = True

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
URL_JOGO = os.environ.get("URL_JOGO", "https://iurerodrigues.github.io/jogo/")
BANCO = os.environ.get("BANCO", "/opt/bot/dados.db")
PORTA = int(os.environ.get("PORTA", "8080"))
ORIGENS = {"https://iurerodrigues.github.io"}
VERSAO = "2.2"

# Precos em estrelas. Devem bater com PACOTES_VIP do jogo.
PACOTES = {
    "amuleto1": {"titulo": "Amuleto da Sorte", "descricao": "30 exploracoes com 3x mais chance de feras raras.", "estrelas": 50},
    "amuleto5": {"titulo": "Caixa de 5 Amuletos", "descricao": "150 exploracoes com sorte tripla.", "estrelas": 200},
    "incenso3": {"titulo": "Kit Criador", "descricao": "3 Incensos de Ovo e 1 Menta da Natureza.", "estrelas": 75},
    "cristal": {"titulo": "Cristal Perfeito", "descricao": "Deixa um IV no maximo.", "estrelas": 120},
    "ultra10": {"titulo": "10 Ultra Esferas", "descricao": "Para cacar raras e lendarias.", "estrelas": 60},
    "passe": {"titulo": "Passe VIP (30 dias)", "descricao": "Recompensa diaria em dobro e +25% de XP.", "estrelas": 300},
}


def log(*partes):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *partes, flush=True)


# ----------------------------------------------------------------------------
# Ajustes da maquina (1 GB de RAM): swap e sem atualizacoes automaticas pesadas
# ----------------------------------------------------------------------------
def ajustar_sistema():
    import subprocess

    def rodar(cmd):
        try:
            r = subprocess.run(cmd, shell=True, timeout=600, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            log("sistema:", cmd[:60], "->", r.returncode)
        except Exception as e:
            log("sistema falhou:", cmd[:60], e)

    if os.geteuid() != 0:
        return
    rodar("systemctl disable --now dnf-makecache.timer dnf-automatic.timer dnf-automatic-install.timer packagekit.service 2>/dev/null; true")
    if not os.path.exists("/swapfile"):
        rodar("dd if=/dev/zero of=/swapfile bs=1M count=2048 && chmod 600 /swapfile && mkswap /swapfile")
        rodar("grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab")
    rodar("swapon /swapfile 2>/dev/null; sysctl -w vm.swappiness=60; true")


# ----------------------------------------------------------------------------
# Banco de dados
# ----------------------------------------------------------------------------
_trava_banco = threading.Lock()


def banco():
    con = sqlite3.connect(BANCO, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def preparar_banco():
    with _trava_banco, banco() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS compras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid INTEGER NOT NULL,
            pacote TEXT NOT NULL,
            estrelas INTEGER NOT NULL,
            cobranca TEXT UNIQUE NOT NULL,
            criado INTEGER NOT NULL,
            entregue INTEGER NOT NULL DEFAULT 0)""")
        con.execute("""CREATE TABLE IF NOT EXISTS ranking (
            uid INTEGER PRIMARY KEY,
            nome TEXT NOT NULL,
            nivel INTEGER NOT NULL DEFAULT 1,
            insignias INTEGER NOT NULL DEFAULT 0,
            bestiario INTEGER NOT NULL DEFAULT 0,
            torre INTEGER NOT NULL DEFAULT 0,
            campeao INTEGER NOT NULL DEFAULT 0,
            pontos INTEGER NOT NULL DEFAULT 0,
            atualizado INTEGER NOT NULL)""")


# ----------------------------------------------------------------------------
# API do Telegram
# ----------------------------------------------------------------------------
def tg(metodo, limite_http=60, **params):
    url = "https://api.telegram.org/bot%s/%s" % (TOKEN, metodo)
    dados = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(url, data=dados, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=limite_http) as r:
        resp = json.loads(r.read().decode("utf-8"))
    if not resp.get("ok"):
        raise RuntimeError("Telegram: %s" % resp)
    return resp.get("result")


def teclado_jogar():
    return {"inline_keyboard": [[{"text": "\U0001F3AE Jogar Domadores de Feras", "web_app": {"url": URL_JOGO}}]]}


def enviar(chat_id, texto, teclado=None):
    params = {"chat_id": chat_id, "text": texto}
    if teclado:
        params["reply_markup"] = teclado
    return tg("sendMessage", **params)


# ----------------------------------------------------------------------------
# Validacao do initData (https://core.telegram.org/bots/webapps)
# ----------------------------------------------------------------------------
def validar_init_data(init_data, max_idade=86400):
    if not init_data or not TOKEN:
        return None
    pares = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    recebido = pares.pop("hash", None)
    if not recebido:
        return None
    verificacao = "\n".join("%s=%s" % (k, pares[k]) for k in sorted(pares))
    chave = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    calculado = hmac.new(chave, verificacao.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculado, recebido):
        return None
    try:
        if time.time() - int(pares.get("auth_date", "0")) > max_idade:
            return None
        usuario = json.loads(pares.get("user", "{}"))
        if not isinstance(usuario.get("id"), int):
            return None
        return usuario
    except (ValueError, TypeError):
        return None


# ----------------------------------------------------------------------------
# Regras de negocio
# ----------------------------------------------------------------------------
def criar_fatura(uid, pacote_id):
    p = PACOTES.get(pacote_id)
    if not p:
        raise ValueError("pacote invalido")
    carga = "%d:%s:%d" % (uid, pacote_id, int(time.time()))
    return tg("createInvoiceLink",
              title=p["titulo"], description=p["descricao"], payload=carga,
              provider_token="", currency="XTR",
              prices=[{"label": p["titulo"], "amount": p["estrelas"]}])


def registrar_pagamento(pagamento, uid):
    carga = pagamento.get("invoice_payload", "")
    partes = carga.split(":")
    if len(partes) < 2 or partes[1] not in PACOTES:
        log("pagamento com carga estranha", carga)
        return None
    with _trava_banco, banco() as con:
        try:
            con.execute("INSERT INTO compras (uid, pacote, estrelas, cobranca, criado) VALUES (?,?,?,?,?)",
                        (uid, partes[1], int(pagamento.get("total_amount", 0)),
                         pagamento.get("telegram_payment_charge_id", ""), int(time.time())))
        except sqlite3.IntegrityError:
            pass  # pagamento repetido: ja registrado
    return partes[1]


def resgatar(uid):
    with _trava_banco, banco() as con:
        linhas = con.execute("SELECT id, pacote FROM compras WHERE uid=? AND entregue=0 ORDER BY id", (uid,)).fetchall()
        if linhas:
            con.executemany("UPDATE compras SET entregue=1 WHERE id=?", [(l["id"],) for l in linhas])
    return [l["pacote"] for l in linhas]


def _int(v, minimo, maximo):
    try:
        return max(minimo, min(maximo, int(v)))
    except (TypeError, ValueError):
        return minimo


def salvar_ranking(usuario, dados):
    apelido = re.sub(r"[^\w .-]", "", re.sub(r"<[^>]*>", "", str(dados.get("nome") or "")), flags=re.UNICODE).strip()[:16]
    nome = apelido if len(apelido) >= 3 else (usuario.get("first_name") or "Domador")[:24]
    nivel = _int(dados.get("nivel"), 1, 100)
    insignias = _int(dados.get("insignias"), 0, 8)
    bestiario = _int(dados.get("bestiario"), 0, 500)
    torre = _int(dados.get("torre"), 0, 10000)
    campeao = 1 if dados.get("campeao") else 0
    pontos = nivel * 10 + insignias * 150 + bestiario * 40 + torre * 25 + campeao * 1500
    with _trava_banco, banco() as con:
        con.execute("""INSERT INTO ranking (uid, nome, nivel, insignias, bestiario, torre, campeao, pontos, atualizado)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(uid) DO UPDATE SET nome=excluded.nome, nivel=excluded.nivel, insignias=excluded.insignias,
              bestiario=excluded.bestiario, torre=MAX(ranking.torre, excluded.torre), campeao=MAX(ranking.campeao, excluded.campeao),
              pontos=excluded.pontos, atualizado=excluded.atualizado""",
                    (usuario["id"], nome, nivel, insignias, bestiario, torre, campeao, pontos, int(time.time())))
    return pontos


def top_ranking(limite=50):
    with _trava_banco, banco() as con:
        linhas = con.execute("SELECT uid, nome, nivel, insignias, bestiario, torre, campeao, pontos FROM ranking ORDER BY pontos DESC, atualizado ASC LIMIT ?", (limite,)).fetchall()
    return [dict(l) for l in linhas]


def posicao(uid):
    with _trava_banco, banco() as con:
        l = con.execute("SELECT pontos FROM ranking WHERE uid=?", (uid,)).fetchone()
        if not l:
            return None
        return con.execute("SELECT COUNT(*) FROM ranking WHERE pontos > ?", (l["pontos"],)).fetchone()[0] + 1


# ----------------------------------------------------------------------------
# Bot (long polling)
# ----------------------------------------------------------------------------
def tratar_mensagem(msg):
    chat = msg["chat"]["id"]
    usuario = msg.get("from", {})
    if "successful_payment" in msg:
        pacote = registrar_pagamento(msg["successful_payment"], usuario.get("id"))
        nome = PACOTES.get(pacote, {}).get("titulo", "seu pacote")
        enviar(chat, "⭐ Pagamento confirmado! %s já está esperando por você no jogo.\n\nAbra o jogo para receber." % nome, teclado_jogar())
        log("pagamento", usuario.get("id"), pacote)
        return
    texto = (msg.get("text") or "").strip()
    nome = usuario.get("first_name") or "Domador"
    if texto.startswith("/start") or texto.startswith("/jogar"):
        enviar(chat,
               "Bem-vindo a Ferália, %s!\n\n"
               "Capture feras, vença os 8 ginásios, desafie a Liga e encontre os Primordiais.\n\n"
               "Toque no botão abaixo para começar." % nome, teclado_jogar())
    elif texto.startswith("/ranking"):
        top = top_ranking(10)
        if not top:
            enviar(chat, "O ranking ainda está vazio. Seja o primeiro!", teclado_jogar())
            return
        medalhas = ["\U0001F947", "\U0001F948", "\U0001F949"]
        linhas = ["%s %s - %d pts" % (medalhas[i] if i < 3 else "%d." % (i + 1), r["nome"], r["pontos"]) for i, r in enumerate(top)]
        enviar(chat, "\U0001F3C6 Top 10 Domadores de Ferália\n\n" + "\n".join(linhas), teclado_jogar())
    elif texto.startswith("/ajuda"):
        enviar(chat, "/jogar - abrir o jogo\n/ranking - os melhores domadores\n/ajuda - esta mensagem\n\n"
                     "Seu progresso fica salvo na sua conta do Telegram.", teclado_jogar())
    else:
        enviar(chat, "Toque no botão para jogar!", teclado_jogar())


def tratar_pre_checkout(q):
    carga = q.get("invoice_payload", "")
    partes = carga.split(":")
    ok = len(partes) >= 2 and partes[1] in PACOTES and q.get("currency") == "XTR" \
        and int(q.get("total_amount", 0)) == PACOTES[partes[1]]["estrelas"]
    if ok:
        tg("answerPreCheckoutQuery", pre_checkout_query_id=q["id"], ok=True)
    else:
        tg("answerPreCheckoutQuery", pre_checkout_query_id=q["id"], ok=False, error_message="Pacote inválido. Tente de novo pelo jogo.")


def laco_bot():
    deslocamento = 0
    try:
        tg("deleteWebhook", drop_pending_updates=False)
    except Exception:
        pass
    log("bot iniciado, versao", VERSAO)
    while True:
        try:
            atualizacoes = tg("getUpdates", limite_http=70, offset=deslocamento, timeout=50,
                              allowed_updates=["message", "pre_checkout_query"])
            for u in atualizacoes:
                deslocamento = u["update_id"] + 1
                try:
                    if "pre_checkout_query" in u:
                        tratar_pre_checkout(u["pre_checkout_query"])
                    elif "message" in u:
                        tratar_mensagem(u["message"])
                except Exception:
                    log("erro tratando atualizacao", traceback.format_exc())
        except Exception as e:
            log("erro no polling:", e)
            time.sleep(5)


# ----------------------------------------------------------------------------
# API HTTP para o jogo
# ----------------------------------------------------------------------------
_acessos = {}


def limitar_taxa(chave, max_por_minuto=30):
    agora = int(time.time() // 60)
    k = (chave, agora)
    _acessos[k] = _acessos.get(k, 0) + 1
    if len(_acessos) > 5000:
        for antiga in [x for x in _acessos if x[1] < agora]:
            _acessos.pop(antiga, None)
    return _acessos[k] <= max_por_minuto


class Api(BaseHTTPRequestHandler):
    server_version = "Feras/" + VERSAO

    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        origem = self.headers.get("Origin", "")
        if origem in ORIGENS:
            self.send_header("Access-Control-Allow-Origin", origem)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")

    def _json(self, codigo, corpo):
        dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/api/saude"):
            return self._json(200, {"ok": True, "versao": VERSAO})
        if self.path.startswith("/api/ranking"):
            return self._json(200, {"ok": True, "top": top_ranking(50)})
        return self._json(404, {"ok": False})

    def do_POST(self):
        try:
            tamanho = int(self.headers.get("Content-Length", "0"))
            if tamanho > 20000:
                return self._json(413, {"ok": False, "erro": "grande demais"})
            corpo = json.loads(self.rfile.read(tamanho).decode("utf-8") or "{}")
        except (ValueError, TypeError):
            return self._json(400, {"ok": False, "erro": "json invalido"})
        usuario = validar_init_data(corpo.get("initData", ""))
        if not usuario:
            return self._json(401, {"ok": False, "erro": "nao autorizado"})
        uid = usuario["id"]
        if not limitar_taxa(uid):
            return self._json(429, {"ok": False, "erro": "muitas requisicoes"})
        try:
            if self.path.startswith("/api/fatura"):
                return self._json(200, {"ok": True, "link": criar_fatura(uid, corpo.get("pacote", ""))})
            if self.path.startswith("/api/resgatar"):
                return self._json(200, {"ok": True, "pacotes": resgatar(uid)})
            if self.path.startswith("/api/ranking"):
                pontos = salvar_ranking(usuario, corpo.get("dados") or {})
                return self._json(200, {"ok": True, "pontos": pontos, "posicao": posicao(uid)})
        except ValueError as e:
            return self._json(400, {"ok": False, "erro": str(e)})
        except Exception:
            log("erro na api", traceback.format_exc())
            return self._json(500, {"ok": False, "erro": "erro interno"})
        return self._json(404, {"ok": False})


def iniciar_api():
    servidor = ServidorThreads(("127.0.0.1", PORTA), Api)
    log("api ouvindo na porta", PORTA)
    servidor.serve_forever()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Defina TELEGRAM_TOKEN")
    threading.Thread(target=ajustar_sistema, daemon=True).start()
    preparar_banco()
    threading.Thread(target=iniciar_api, daemon=True).start()
    laco_bot()
