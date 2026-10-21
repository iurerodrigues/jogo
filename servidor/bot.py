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
VERSAO = "2.4"

# Precos em estrelas. Devem bater com PACOTES_VIP do jogo.
# Pacotes de Cristais (moeda VIP) vendidos em Telegram Stars
PACOTES = {
    "cristais60": {"titulo": "60 Cristais", "descricao": "Moeda VIP para roupas, auras e itens especiais.", "estrelas": 50, "cristais": 60},
    "cristais130": {"titulo": "130 Cristais", "descricao": "Moeda VIP (+8% de bonus).", "estrelas": 100, "cristais": 130},
    "cristais700": {"titulo": "700 Cristais", "descricao": "Moeda VIP (+16% de bonus).", "estrelas": 500, "cristais": 700},
    "cristais1500": {"titulo": "1500 Cristais", "descricao": "Moeda VIP (+25% de bonus).", "estrelas": 1000, "cristais": 1500},
}

# O que se compra com Cristais. Os precos ficam no servidor (o jogo so mostra).
CATALOGO = {
    "amuleto1": 60, "amuleto5": 240, "incenso3": 90, "ultra10": 70, "passe": 350,
    "menta1": 40, "cristal": 150,
    "roupaReal": 150, "roupaObsidiana": 200, "roupaEstelar": 250, "roupaPirata": 120,
    "auraDourada": 120, "auraSombria": 120, "auraGelida": 120, "auraFlamejante": 120,
}


# Trocas entre jogadores
ESPECIES_TROCAVEIS = set(["cinzor", "brasador", "infernor", "verdrax", "espinhador", "silvagor", "hidrax", "maregor", "leviator", "rasgor", "dentrax", "corvax", "gralhador", "falcorei", "lobrax", "lobrasa", "lobomar", "lobovolt", "lobosilva", "lobumbra", "vinhax", "constritor", "esporax", "micotar", "piranox", "tubarax", "krakel", "krakengor", "voltrix", "voltragon", "amperon", "tesloran", "gigavolt", "magmor", "caldeirax", "granor", "basaltor", "montanhor", "cascor", "cascalhao", "peconhar", "viperax", "basiliscor", "lodrax", "pestilar", "umbrax", "noctifer", "sombral", "eclipsor", "fagulhor", "pirotitan", "toupax", "escavor", "estalix", "trovoave", "ostrax", "perlador", "sapux", "coaxor", "morcelix", "vampirax", "lagartix", "salamandror", "geadax", "nevrak", "glaciaur", "criostal", "criogor", "brisax", "ventorax", "furacor", "zefiron", "ciclonar", "punhor", "golpeador", "brutamor", "gingar", "mestrax"])
TAXA_TROCA = 0.10           # 10% do valor fica com o mercado (tira moedas de circulação)
TROCA_VALIDADE = 24 * 3600  # oferta vence em 24 h
MAX_OFERTAS_ABERTAS = 3


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
        con.execute("""CREATE TABLE IF NOT EXISTS trocas (
            codigo TEXT PRIMARY KEY,
            de_uid INTEGER NOT NULL,
            de_nome TEXT NOT NULL,
            fera TEXT NOT NULL,
            pede_tipo TEXT NOT NULL,
            pede_valor INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'aberta',
            para_uid INTEGER,
            para_nome TEXT,
            contra_fera TEXT,
            criado INTEGER NOT NULL,
            fechado INTEGER,
            entregue_dono INTEGER NOT NULL DEFAULT 0)""")
        con.execute("""CREATE TABLE IF NOT EXISTS bonus_diario (
            uid INTEGER PRIMARY KEY,
            ultimo TEXT NOT NULL,
            sequencia INTEGER NOT NULL DEFAULT 0)""")
        con.execute("""CREATE TABLE IF NOT EXISTS saldo (
            uid INTEGER PRIMARY KEY,
            cristais INTEGER NOT NULL DEFAULT 0)""")


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
            # entregue=1: os Cristais entram no saldo na hora
            con.execute("INSERT INTO compras (uid, pacote, estrelas, cobranca, criado, entregue) VALUES (?,?,?,?,?,1)",
                        (uid, partes[1], int(pagamento.get("total_amount", 0)),
                         pagamento.get("telegram_payment_charge_id", ""), int(time.time())))
            creditar(con, uid, PACOTES[partes[1]]["cristais"])
        except sqlite3.IntegrityError:
            pass  # pagamento repetido: ja registrado
    return partes[1]


def creditar(con, uid, qtd):
    con.execute("INSERT INTO saldo (uid, cristais) VALUES (?, ?) ON CONFLICT(uid) DO UPDATE SET cristais = cristais + excluded.cristais", (uid, qtd))


def ver_saldo(uid):
    with _trava_banco, banco() as con:
        l = con.execute("SELECT cristais FROM saldo WHERE uid=?", (uid,)).fetchone()
    return l["cristais"] if l else 0


def gastar(uid, item):
    preco = CATALOGO.get(item)
    if not preco:
        raise ValueError("item invalido")
    with _trava_banco, banco() as con:
        cur = con.execute("UPDATE saldo SET cristais = cristais - ? WHERE uid=? AND cristais >= ?", (preco, uid, preco))
        if cur.rowcount != 1:
            raise ValueError("cristais insuficientes")
        # a entrega vai pela mesma fila das compras: o jogo recebe em /api/resgatar
        con.execute("INSERT INTO compras (uid, pacote, estrelas, cobranca, criado) VALUES (?,?,0,?,?)",
                    (uid, item, "gasto:%d:%s:%f" % (uid, item, time.time()), int(time.time())))
        return con.execute("SELECT cristais FROM saldo WHERE uid=?", (uid,)).fetchone()["cristais"]


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
# Trocas entre jogadores
# fera compacta = [especie, level, xp, hp, variante, iv[4], ev[4], natureza, golpes, item, aura]
# ----------------------------------------------------------------------------
def validar_fera(f):
    if not isinstance(f, list) or len(f) < 9:
        raise ValueError("fera invalida")
    esp, lv, xp, hp, var, iv, ev, nat, golpes = f[:9]
    if esp not in ESPECIES_TROCAVEIS:
        raise ValueError("essa fera nao pode ser trocada")
    if not (isinstance(lv, int) and 1 <= lv <= 100) or not isinstance(xp, int) or xp < 0:
        raise ValueError("nivel invalido")
    if var not in ("", "s", "b"):
        raise ValueError("variante invalida")
    if not (isinstance(iv, list) and len(iv) == 4 and all(isinstance(x, int) and 0 <= x <= 31 for x in iv)):
        raise ValueError("IV invalido")
    if not (isinstance(ev, list) and len(ev) == 4 and all(isinstance(x, int) and 0 <= x <= 252 for x in ev) and sum(ev) <= 510):
        raise ValueError("EV invalido")
    if not (isinstance(nat, int) and 0 <= nat < 40):
        raise ValueError("natureza invalida")
    if not (isinstance(golpes, list) and 1 <= len(golpes) <= 4 and all(isinstance(g, str) and len(g) < 40 for g in golpes)):
        raise ValueError("golpes invalidos")
    extra = f[9:11]
    for x in extra:
        if not (x == 0 or x is None or (isinstance(x, str) and len(x) < 40)):
            raise ValueError("item invalido")
    return json.dumps(f[:11], separators=(",", ":"))


def novo_codigo(con):
    import random
    letras = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(20):
        c = "".join(random.choice(letras) for _ in range(6))
        if not con.execute("SELECT 1 FROM trocas WHERE codigo=?", (c,)).fetchone():
            return c
    raise ValueError("tente de novo")


def expirar_trocas(con):
    con.execute("UPDATE trocas SET status='expirada', fechado=? WHERE status='aberta' AND criado < ?",
                (int(time.time()), int(time.time()) - TROCA_VALIDADE))


def nome_de(usuario, dados):
    n = re.sub(r"[^\w .-]", "", re.sub(r"<[^>]*>", "", str(dados.get("nome") or "")), flags=re.UNICODE).strip()[:16]
    return n if len(n) >= 3 else (usuario.get("first_name") or "Domador")[:24]


def troca_publica(l):
    return {"codigo": l["codigo"], "de": l["de_nome"], "fera": json.loads(l["fera"]), "pedeTipo": l["pede_tipo"],
            "pedeValor": l["pede_valor"], "status": l["status"], "criado": l["criado"],
            "para": l["para_nome"], "contraFera": json.loads(l["contra_fera"]) if l["contra_fera"] else None}


def troca_criar(usuario, dados):
    uid = usuario["id"]
    fera = validar_fera(dados.get("fera"))
    tipo = dados.get("pedeTipo")
    if tipo not in ("fera", "moedas", "cristais"):
        raise ValueError("pedido invalido")
    valor = 0
    if tipo == "moedas":
        valor = _int(dados.get("pedeValor"), 1, 10000000)
    elif tipo == "cristais":
        valor = _int(dados.get("pedeValor"), 1, 100000)
    with _trava_banco, banco() as con:
        expirar_trocas(con)
        abertas = con.execute("SELECT COUNT(*) FROM trocas WHERE de_uid=? AND status='aberta'", (uid,)).fetchone()[0]
        if abertas >= MAX_OFERTAS_ABERTAS:
            raise ValueError("voce ja tem 3 ofertas abertas")
        cod = novo_codigo(con)
        con.execute("INSERT INTO trocas (codigo, de_uid, de_nome, fera, pede_tipo, pede_valor, criado) VALUES (?,?,?,?,?,?,?)",
                    (cod, uid, nome_de(usuario, dados), fera, tipo, valor, int(time.time())))
    return cod


def troca_ver(uid, codigo):
    with _trava_banco, banco() as con:
        expirar_trocas(con)
        l = con.execute("SELECT * FROM trocas WHERE codigo=?", (str(codigo).upper().strip()[:8],)).fetchone()
    if not l or l["status"] != "aberta":
        raise ValueError("codigo invalido ou oferta encerrada")
    t = troca_publica(l)
    t["minha"] = l["de_uid"] == uid
    return t


def troca_aceitar(usuario, dados):
    uid = usuario["id"]
    codigo = str(dados.get("codigo", "")).upper().strip()[:8]
    with _trava_banco, banco() as con:
        expirar_trocas(con)
        l = con.execute("SELECT * FROM trocas WHERE codigo=?", (codigo,)).fetchone()
        if not l or l["status"] != "aberta":
            raise ValueError("codigo invalido ou oferta encerrada")
        if l["de_uid"] == uid:
            raise ValueError("voce nao pode aceitar a propria oferta")
        contra = None
        if l["pede_tipo"] == "fera":
            contra = validar_fera(dados.get("fera"))
        elif l["pede_tipo"] == "cristais":
            preco = l["pede_valor"]
            cur = con.execute("UPDATE saldo SET cristais = cristais - ? WHERE uid=? AND cristais >= ?", (preco, uid, preco))
            if cur.rowcount != 1:
                raise ValueError("cristais insuficientes")
            creditar(con, l["de_uid"], int(preco * (1 - TAXA_TROCA)))
        # moedas: o jogo de quem compra desconta; o vendedor recebe ao resgatar
        cur = con.execute("UPDATE trocas SET status='concluida', para_uid=?, para_nome=?, contra_fera=?, fechado=? WHERE codigo=? AND status='aberta'",
                          (uid, nome_de(usuario, dados), contra, int(time.time()), codigo))
        if cur.rowcount != 1:
            raise ValueError("oferta encerrada")
        return {"fera": json.loads(l["fera"]), "pedeTipo": l["pede_tipo"], "pedeValor": l["pede_valor"],
                "cristais": ver_saldo_con(con, uid)}


def ver_saldo_con(con, uid):
    l = con.execute("SELECT cristais FROM saldo WHERE uid=?", (uid,)).fetchone()
    return l["cristais"] if l else 0


def bonus_cristais(uid):
    """Todo mundo ganha Cristais grátis uma vez por dia: 1 por dia, 5 no 7º dia seguido."""
    hoje = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 3 * 3600))
    ontem = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 27 * 3600))
    with _trava_banco, banco() as con:
        l = con.execute("SELECT ultimo, sequencia FROM bonus_diario WHERE uid=?", (uid,)).fetchone()
        if l and l["ultimo"] == hoje:
            return 0, l["sequencia"], ver_saldo_con(con, uid)
        seq = (l["sequencia"] + 1) if l and l["ultimo"] == ontem else 1
        ganho = 5 if seq % 7 == 0 else 1
        con.execute("INSERT INTO bonus_diario (uid, ultimo, sequencia) VALUES (?,?,?) ON CONFLICT(uid) DO UPDATE SET ultimo=excluded.ultimo, sequencia=excluded.sequencia", (uid, hoje, seq))
        creditar(con, uid, ganho)
        return ganho, seq, ver_saldo_con(con, uid)


def troca_cancelar(uid, codigo):
    with _trava_banco, banco() as con:
        cur = con.execute("UPDATE trocas SET status='cancelada', fechado=? WHERE codigo=? AND de_uid=? AND status='aberta'",
                          (int(time.time()), str(codigo).upper().strip()[:8], uid))
        if cur.rowcount != 1:
            raise ValueError("oferta nao pode ser cancelada")
        # a fera volta pelo /api/troca/minhas (entrega pendente)


def troca_minhas(uid):
    """Lista as ofertas do jogador e entrega o que ele tem a receber (uma única vez)."""
    entregas = []
    with _trava_banco, banco() as con:
        expirar_trocas(con)
        pendentes = con.execute("SELECT * FROM trocas WHERE de_uid=? AND status IN ('concluida','cancelada','expirada') AND entregue_dono=0", (uid,)).fetchall()
        for l in pendentes:
            if l["status"] == "concluida":
                if l["pede_tipo"] == "fera":
                    entregas.append({"tipo": "fera", "fera": json.loads(l["contra_fera"]), "codigo": l["codigo"], "de": l["para_nome"]})
                elif l["pede_tipo"] == "moedas":
                    entregas.append({"tipo": "moedas", "valor": int(l["pede_valor"] * (1 - TAXA_TROCA)), "codigo": l["codigo"], "de": l["para_nome"]})
                else:
                    entregas.append({"tipo": "cristais", "valor": int(l["pede_valor"] * (1 - TAXA_TROCA)), "codigo": l["codigo"], "de": l["para_nome"]})
            else:
                entregas.append({"tipo": "devolvida", "fera": json.loads(l["fera"]), "codigo": l["codigo"], "motivo": l["status"]})
        if pendentes:
            con.executemany("UPDATE trocas SET entregue_dono=1 WHERE codigo=?", [(l["codigo"],) for l in pendentes])
        abertas = con.execute("SELECT * FROM trocas WHERE de_uid=? AND status='aberta' ORDER BY criado DESC", (uid,)).fetchall()
        historico = con.execute("SELECT * FROM trocas WHERE (de_uid=? OR para_uid=?) AND status!='aberta' ORDER BY fechado DESC LIMIT 10", (uid, uid)).fetchall()
        saldo = ver_saldo_con(con, uid)
    return {"entregas": entregas, "abertas": [troca_publica(l) for l in abertas],
            "historico": [troca_publica(l) for l in historico], "cristais": saldo}


# ----------------------------------------------------------------------------
# Bot (long polling)
# ----------------------------------------------------------------------------
def tratar_mensagem(msg):
    chat = msg["chat"]["id"]
    usuario = msg.get("from", {})
    if "successful_payment" in msg:
        pacote = registrar_pagamento(msg["successful_payment"], usuario.get("id"))
        nome = PACOTES.get(pacote, {}).get("titulo", "seu pacote")
        enviar(chat, "💎 Pagamento confirmado! %s foram adicionados à sua conta.\n\nSaldo atual: %d Cristais." % (nome, ver_saldo(usuario.get("id"))), teclado_jogar())
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
                return self._json(200, {"ok": True, "pacotes": resgatar(uid), "cristais": ver_saldo(uid)})
            if self.path.startswith("/api/saldo"):
                return self._json(200, {"ok": True, "cristais": ver_saldo(uid)})
            if self.path.startswith("/api/gastar"):
                restante = gastar(uid, str(corpo.get("item", "")))
                return self._json(200, {"ok": True, "cristais": restante})
            if self.path.startswith("/api/cristais/diario"):
                ganho, seq, saldo = bonus_cristais(uid)
                return self._json(200, {"ok": True, "ganho": ganho, "sequencia": seq, "cristais": saldo})
            if self.path.startswith("/api/troca/criar"):
                return self._json(200, {"ok": True, "codigo": troca_criar(usuario, corpo)})
            if self.path.startswith("/api/troca/ver"):
                return self._json(200, {"ok": True, "troca": troca_ver(uid, corpo.get("codigo", ""))})
            if self.path.startswith("/api/troca/aceitar"):
                r = troca_aceitar(usuario, corpo)
                r["ok"] = True
                return self._json(200, r)
            if self.path.startswith("/api/troca/cancelar"):
                troca_cancelar(uid, corpo.get("codigo", ""))
                return self._json(200, {"ok": True})
            if self.path.startswith("/api/troca/minhas"):
                r = troca_minhas(uid)
                r["ok"] = True
                return self._json(200, r)
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
