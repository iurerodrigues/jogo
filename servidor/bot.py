#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Domadores de Feras - servidor do bot do Telegram.

Usa apenas a biblioteca padrao do Python (3.6+), sem dependencias.
- Bot: /start, /jogar, /ajuda, /ranking e o botao que abre o jogo.
- Moedas do jogo com saldo no servidor (anti-trapaca): POST /api/moedas
- Cristais (VIP) comprados por pagamento externo (Pix/cartao):
    POST /api/pedido -> cria o pedido e devolve o link externo
    GET  /pagar?pedido=... -> pagina de pagamento provisoria
    POST /api/pagamento/confirmar -> webhook assinado (HMAC) que credita
- API HTTP (atras do Caddy com HTTPS) para o jogo:
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
import random
import re
import sqlite3
import threading
import time
import traceback
import urllib.parse
import urllib.error
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
VERSAO = "2.7"

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


# Tabelas do jogo (geradas pelo build a partir de 1_dados.js - nao editar a mao)
# <<TABELAS_JOGO>>
TABELAS = json.loads(r'''{"itens":{"esfera":50,"superEsfera":200,"ultraEsfera":600,"pocao":60,"superPocao":180,"antidoto":120,"pedraIgnea":3000,"pedraAbissal":3000,"pedraTrovao":3000,"pedraVerdejante":3000,"pedraLunar":3500,"pedraEterna":2000,"garraAfiada":2500,"escamaDura":2500,"penaVeloz":2500,"amuletoVital":2500,"orbeArcano":2500,"veuProtetor":2500,"frutaVital":400,"folhaRegen":4000,"talismaXP":5000,"compartilhadorXP":15000,"lenteFoco":3500,"faixaElemental":4500,"tonicoVital":1000,"tonicoForca":1000,"tonicoGuarda":1000,"tonicoAgil":1000,"tonicoMente":1000,"tonicoEspirito":1000,"ervaReset":1500,"mentaNatureza":6000,"cristalPerfeito":15000,"doceRaro":1200,"incenso":1500},"roupas":{"jaqueta":{"preco":2000,"requer":null},"lenco":{"preco":2500,"requer":null},"chapeu":{"preco":3000,"requer":null},"capa":{"preco":4500,"requer":null},"capuz":{"preco":6000,"requer":null},"armadura":{"preco":9000,"requer":null},"campeao":{"preco":20000,"requer":"campeao"}},"ginasios":[{"premio":800,"qtd":3},{"premio":1200,"qtd":3},{"premio":1700,"qtd":3},{"premio":2200,"qtd":3},{"premio":2800,"qtd":3},{"premio":3500,"qtd":3},{"premio":4200,"qtd":3},{"premio":5000,"qtd":3}],"liga":[{"premio":6000,"qtd":4},{"premio":7000,"qtd":4},{"premio":8000,"qtd":4},{"premio":15000,"qtd":5}],"missoes":{"vitorias":{"alvos":[5,8,12],"por":120,"longo":false},"capturas":{"alvos":[2,3,5],"por":200,"longo":false},"treinadores":{"alvos":[1,2,3],"por":300,"longo":false},"exploracoes":{"alvos":[10,15,25],"por":40,"longo":false},"criticos":{"alvos":[2,4],"por":250,"longo":false},"superEficaz":{"alvos":[5,10],"por":80,"longo":false},"torreAndares":{"alvos":[3,5],"por":200,"longo":false},"evolucoes":{"alvos":[1,2],"por":600,"longo":true},"ovos":{"alvos":[1,2],"por":500,"longo":true}},"periodos":{"dia":{"qtd":3,"mult":1},"semana":{"qtd":4,"mult":6},"mes":{"qtd":5,"mult":22}},"conquistas":{"cap10":{"premio":1000,"cont":"capturas","alvo":10},"cap50":{"premio":5000,"cont":"capturas","alvo":50},"cap150":{"premio":20000,"cont":"capturas","alvo":150},"vit25":{"premio":1500,"cont":"vitorias","alvo":25},"vit100":{"premio":6000,"cont":"vitorias","alvo":100},"vit500":{"premio":30000,"cont":"vitorias","alvo":500},"raro1":{"premio":3000,"cont":"raros","alvo":1},"raro10":{"premio":15000,"cont":"raros","alvo":10},"ovo1":{"premio":1000,"cont":"ovos","alvo":1},"ovo20":{"premio":10000,"cont":"ovos","alvo":20},"evo5":{"premio":2000,"cont":"evolucoes","alvo":5},"ins8":{"premio":10000,"cont":"insignias","alvo":8},"camp":{"premio":20000,"cont":"campeao","alvo":1},"torre10":{"premio":5000,"cont":"torreRecorde","alvo":10},"torre30":{"premio":25000,"cont":"torreRecorde","alvo":30},"arena5":{"premio":5000,"cont":"arena","alvo":5},"dex30":{"premio":8000,"cont":"bestiario","alvo":30},"dexAll":{"premio":100000,"cont":"bestiario","alvo":86}}}''')
# <</TABELAS_JOGO>>

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
        con.execute("CREATE TABLE IF NOT EXISTS admins (uid INTEGER PRIMARY KEY, nome TEXT, desde REAL)")
        con.execute("CREATE TABLE IF NOT EXISTS config_jogo (id INTEGER PRIMARY KEY CHECK (id = 1), valor TEXT, versao INTEGER, atualizado REAL, por INTEGER)")
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
        preparar_carteira(con)
        preparar_pedidos(con)


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
# fera compacta = [especie, level, xp, hp, variante, iv[4 ou 6], ev[4 ou 6], natureza, golpes, item, aura]
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
    # 4 atributos (versao antiga) ou 6 (com Ataque e Defesa Especial)
    if not (isinstance(iv, list) and len(iv) in (4, 6) and all(isinstance(x, int) and 0 <= x <= 31 for x in iv)):
        raise ValueError("IV invalido")
    if not (isinstance(ev, list) and len(ev) in (4, 6) and all(isinstance(x, int) and 0 <= x <= 252 for x in ev) and sum(ev) <= 510):
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
        elif l["pede_tipo"] == "moedas":
            preco = l["pede_valor"]
            carteira(con, uid)
            carteira(con, l["de_uid"])
            cur = con.execute("UPDATE carteira SET moedas = moedas - ? WHERE uid=? AND moedas >= ?", (preco, uid, preco))
            if cur.rowcount != 1:
                raise ValueError("moedas insuficientes")
            con.execute("UPDATE carteira SET moedas = moedas + ? WHERE uid=?", (int(preco * (1 - TAXA_TROCA)), l["de_uid"]))
        cur = con.execute("UPDATE trocas SET status='concluida', para_uid=?, para_nome=?, contra_fera=?, fechado=? WHERE codigo=? AND status='aberta'",
                          (uid, nome_de(usuario, dados), contra, int(time.time()), codigo))
        if cur.rowcount != 1:
            raise ValueError("oferta encerrada")
        cart = con.execute("SELECT moedas FROM carteira WHERE uid=?", (uid,)).fetchone()
        return {"fera": json.loads(l["fera"]), "pedeTipo": l["pede_tipo"], "pedeValor": l["pede_valor"],
                "cristais": ver_saldo_con(con, uid), "moedas": cart["moedas"] if cart else None}


def ver_saldo_con(con, uid):
    l = con.execute("SELECT cristais FROM saldo WHERE uid=?", (uid,)).fetchone()
    return l["cristais"] if l else 0


def bonus_cristais(uid):
    """Todo mundo ganha 1 Cristal grátis por semana (segunda a domingo)."""
    semana = semana_br()
    with _trava_banco, banco() as con:
        l = con.execute("SELECT ultimo, sequencia FROM bonus_diario WHERE uid=?", (uid,)).fetchone()
        if l and l["ultimo"] == semana:
            return 0, l["sequencia"], ver_saldo_con(con, uid)
        seq = (l["sequencia"] + 1) if l else 1
        con.execute("INSERT INTO bonus_diario (uid, ultimo, sequencia) VALUES (?,?,?) ON CONFLICT(uid) DO UPDATE SET ultimo=excluded.ultimo, sequencia=excluded.sequencia", (uid, semana, seq))
        creditar(con, uid, 1)
        return 1, seq, ver_saldo_con(con, uid)


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
                    entregas.append({"tipo": "moedas", "valor": int(l["pede_valor"] * (1 - TAXA_TROCA)), "codigo": l["codigo"], "de": l["para_nome"], "creditado": True})
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
# Carteira de Moedas (autoritativa): o saldo de Moedas e o XP do treinador
# vivem no servidor. O jogo so informa o que aconteceu; o servidor decide
# quanto vale, com limites de nivel, tempo e teto diario. Editar o numero no
# celular nao muda nada: na proxima sincronizacao o valor do servidor vence.
# ----------------------------------------------------------------------------
XP_TREINADOR = 110
MIGRACAO_MAX_MOEDAS = 10000          # quem ja jogava traz no maximo isso
MIGRACAO_MAX_XP = XP_TREINADOR * 16  # e no maximo o XP do Level 5
INTERVALO_ABATE = 2.5                # segundos minimos entre duas feras derrotadas
MAX_LENDARIOS_DIA = 3
MAX_LIBERTAR_DIA = 30
MAX_ARENA_DIA = 15
MARCOS_NIVEL = list(range(10, 101, 10))  # Level 10, 20, ... 100 -> 1 Cristal cada


def dia_br(desloc=0):
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() - 3 * 3600 + desloc * 86400))


def semana_br():
    t = time.gmtime(time.time() - 3 * 3600)
    seg = time.time() - 3 * 3600 - t.tm_wday * 86400
    return "S" + time.strftime("%Y-%m-%d", time.gmtime(seg))


def chave_periodo_srv(periodo):
    if periodo == "mes":
        return time.strftime("%Y-%m", time.gmtime(time.time() - 3 * 3600))
    if periodo == "semana":
        return semana_br()
    return dia_br()


def nivel_tr(xp):
    return min(100, int((max(0, xp) / XP_TREINADOR) ** 0.5) + 1)


def limite_nivel(nv):
    base = regra_jogo("comandoBase", 8, 1, 100)
    passo = regra_jogo("comandoPorNivel", 4, 0.5, 50)
    return min(100, int(base + passo * nv))


def preparar_carteira(con):
    con.execute("""CREATE TABLE IF NOT EXISTS carteira (
        uid INTEGER PRIMARY KEY,
        moedas INTEGER NOT NULL DEFAULT 500,
        xp INTEGER NOT NULL DEFAULT 0,
        vitorias INTEGER NOT NULL DEFAULT 0,
        pend INTEGER NOT NULL DEFAULT 0,
        ult_abate REAL NOT NULL DEFAULT 0,
        dia TEXT NOT NULL DEFAULT '',
        ganho_dia INTEGER NOT NULL DEFAULT 0,
        lend_dia INTEGER NOT NULL DEFAULT 0,
        libertar_dia INTEGER NOT NULL DEFAULT 0,
        arena_dia INTEGER NOT NULL DEFAULT 0,
        arena_vit INTEGER NOT NULL DEFAULT 0,
        torre INTEGER NOT NULL DEFAULT 0,
        torre_rec INTEGER NOT NULL DEFAULT 0,
        diario_ult TEXT NOT NULL DEFAULT '',
        diario_seq INTEGER NOT NULL DEFAULT 0,
        criado INTEGER NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS chaves (
        uid INTEGER NOT NULL,
        chave TEXT NOT NULL,
        criado INTEGER NOT NULL,
        PRIMARY KEY (uid, chave))""")


def carteira(con, uid, local=None):
    l = con.execute("SELECT * FROM carteira WHERE uid=?", (uid,)).fetchone()
    if l:
        if l["dia"] != dia_br():
            con.execute("UPDATE carteira SET dia=?, ganho_dia=0, lend_dia=0, libertar_dia=0, arena_dia=0 WHERE uid=?", (dia_br(), uid))
            l = con.execute("SELECT * FROM carteira WHERE uid=?", (uid,)).fetchone()
        return l
    local = local or {}
    moedas = _int(local.get("d", 500), 0, MIGRACAO_MAX_MOEDAS)
    xp = _int(local.get("x", 0), 0, MIGRACAO_MAX_XP)
    con.execute("INSERT INTO carteira (uid, moedas, xp, dia, criado) VALUES (?,?,?,?,?)",
                (uid, moedas, xp, dia_br(), int(time.time())))
    return con.execute("SELECT * FROM carteira WHERE uid=?", (uid,)).fetchone()


def tem_chave(con, uid, chave):
    return con.execute("SELECT 1 FROM chaves WHERE uid=? AND chave=?", (uid, chave)).fetchone() is not None


def marcar_chave(con, uid, chave):
    try:
        con.execute("INSERT INTO chaves (uid, chave, criado) VALUES (?,?,?)", (uid, chave, int(time.time())))
        return True
    except sqlite3.IntegrityError:
        return False


def vip_ativo(con, uid):
    """O Passe VIP comprado com Cristais vale 30 dias (somando compras)."""
    ate = 0
    for l in con.execute("SELECT criado FROM compras WHERE uid=? AND pacote='passe' ORDER BY criado", (uid,)):
        ate = max(ate, l["criado"]) + 30 * 86400
    return ate > time.time()


def teto_diario(nv):
    return 20000 + 2000 * nv


def _ganhar(con, l, valor, repetivel=True):
    """Credita Moedas respeitando o teto diario dos ganhos repetiveis."""
    valor = max(0, int(valor))
    if repetivel:
        # multiplicador de Moedas de batalha definido no painel ADM
        valor = int(round(valor * regra_jogo("moedas", 1, 0.1, 5)))
        valor = min(valor, max(0, teto_diario(nivel_tr(l["xp"])) - l["ganho_dia"]))
        con.execute("UPDATE carteira SET moedas=moedas+?, ganho_dia=ganho_dia+? WHERE uid=?", (valor, valor, l["uid"]))
    else:
        con.execute("UPDATE carteira SET moedas=moedas+? WHERE uid=?", (valor, l["uid"]))
    return valor


def _marcos(con, uid, xp_antes, xp_depois, cristais_ganhos):
    """Cristais grátis por marco de Level, verificado pelo XP que o servidor contou."""
    na, nd = nivel_tr(xp_antes), nivel_tr(xp_depois)
    for m in MARCOS_NIVEL:
        if na < m <= nd and marcar_chave(con, uid, "nv%d" % m):
            creditar(con, uid, 1)
            cristais_ganhos.append({"motivo": "Level %d" % m, "qtd": 1})


def _niveis(lista, teto, maximo=6):
    if not isinstance(lista, list) or not 1 <= len(lista) <= maximo:
        raise ValueError("time invalido")
    return [_int(n, 1, teto) for n in lista]


def moedas_acao(usuario, dados):
    uid = usuario["id"]
    acao = str(dados.get("acao", ""))
    ganho = 0
    cristais_ganhos = []
    aviso = None
    with _trava_banco, banco() as con:
        l = carteira(con, uid, dados.get("local") if acao == "sync" else None)
        nv = nivel_tr(l["xp"])
        teto = min(100, limite_nivel(nv) + 6)
        xp_antes = l["xp"]

        if acao == "sync":
            pass

        elif acao == "abate":
            # uma fera inimiga derrotada: XP do treinador (+ Moedas se for selvagem)
            agora = time.time()
            if agora - l["ult_abate"] < INTERVALO_ABATE:
                raise ValueError("rapido demais")
            nivel = _int(dados.get("nivel"), 1, teto)
            var = _int(dados.get("var"), 0, 2)
            lend = bool(dados.get("lend")) and l["lend_dia"] < MAX_LENDARIOS_DIA
            mult_var = (1, 1.5, 2)[var]
            xp = min(_int(dados.get("xp"), 0, 100000), nivel * 36 * regra_jogo("xpFera", 1, 0.1, 10))
            tipo = str(dados.get("tipo", ""))
            xp = xp * regra_jogo("xpTreinador", 1, 0.1, 10)
            con.execute("UPDATE carteira SET xp=xp+?, ult_abate=? WHERE uid=?", (int(round(xp * 0.5)), agora, uid))
            if tipo == "selvagem":
                ganho = _ganhar(con, l, round(nivel * 15 * mult_var * (5 if lend else 1)))
                con.execute("UPDATE carteira SET vitorias=vitorias+1, lend_dia=lend_dia+? WHERE uid=?", (1 if lend else 0, uid))
            else:
                con.execute("UPDATE carteira SET pend=pend+1 WHERE uid=?", (uid,))

        elif acao == "premio":
            tipo = str(dados.get("tipo", ""))
            revanche = bool(dados.get("revanche"))
            pend = l["pend"]
            if tipo in ("treinador", "rival"):
                niveis = _niveis(dados.get("niveis"), teto)
                if pend < len(niveis):
                    raise ValueError("batalha nao confere")
                pedido = _int(dados.get("premio"), 0, 10 ** 7)
                ganho = _ganhar(con, l, min(pedido, max(sum(niveis) * 22, max(niveis) * 60)))
                con.execute("UPDATE carteira SET vitorias=vitorias+1 WHERE uid=?", (uid,))
            elif tipo in ("ginasio", "liga"):
                tabela = TABELAS["ginasios"] if tipo == "ginasio" else TABELAS["liga"]
                ref = _int(dados.get("ref"), 0, len(tabela) - 1)
                cfg = tabela[ref]
                if pend < cfg["qtd"]:
                    raise ValueError("batalha nao confere")
                letra = "g" if tipo == "ginasio" else "l"
                if not revanche and marcar_chave(con, uid, "%s%d" % (letra, ref)):
                    ganho = _ganhar(con, l, cfg["premio"], repetivel=False)
                    if tipo == "ginasio" and con.execute("SELECT COUNT(*) FROM chaves WHERE uid=? AND chave GLOB 'g[0-9]*'", (uid,)).fetchone()[0] >= len(TABELAS["ginasios"]) and marcar_chave(con, uid, "ins_todas"):
                        creditar(con, uid, 1)
                        cristais_ganhos.append({"motivo": "Todas as insígnias", "qtd": 1})
                    if tipo == "liga" and ref == len(tabela) - 1 and marcar_chave(con, uid, "campeao"):
                        creditar(con, uid, 2)
                        cristais_ganhos.append({"motivo": "Campeão da Liga", "qtd": 2})
                elif marcar_chave(con, uid, "r%s%d:%s" % (letra, ref, dia_br())):
                    ganho = _ganhar(con, l, round(cfg["premio"] / 3))
                else:
                    aviso = "Revanche já premiada hoje."
                con.execute("UPDATE carteira SET vitorias=vitorias+1 WHERE uid=?", (uid,))
            elif tipo == "torre":
                andar = _int(dados.get("andar"), 1, 1000)
                if andar != 1 and andar != l["torre"] + 1:
                    raise ValueError("andar nao confere")
                if pend < (2 if andar < 5 else 3):
                    raise ValueError("batalha nao confere")
                ganho = _ganhar(con, l, 150 + andar * 60)
                con.execute("UPDATE carteira SET torre=?, torre_rec=MAX(torre_rec, ?) WHERE uid=?", (andar, andar, uid))
            elif tipo == "arena":
                niveis = _niveis(dados.get("niveis"), teto)
                if pend < len(niveis):
                    raise ValueError("batalha nao confere")
                if l["arena_dia"] >= MAX_ARENA_DIA:
                    aviso = "Limite diário de prêmios da Arena atingido."
                else:
                    pedido = _int(dados.get("premio"), 0, 10 ** 7)
                    ganho = _ganhar(con, l, min(pedido, 400 + sum(niveis) * 10))
                    con.execute("UPDATE carteira SET arena_dia=arena_dia+1, arena_vit=arena_vit+1 WHERE uid=?", (uid,))
            else:
                raise ValueError("premio invalido")
            con.execute("UPDATE carteira SET pend=0 WHERE uid=?", (uid,))

        elif acao == "missao":
            periodo = str(dados.get("periodo", "dia"))
            p = TABELAS["periodos"].get(periodo)
            md = TABELAS["missoes"].get(str(dados.get("modelo", "")))
            if not p or not md or (periodo == "dia" and md["longo"]):
                raise ValueError("missao invalida")
            alvo = _int(dados.get("alvo"), 1, 100000)
            validos = set(max(1, int(round(a * p["mult"]))) for a in md["alvos"])
            if alvo not in validos:
                raise ValueError("missao invalida")
            chave = chave_periodo_srv(periodo)
            ja = con.execute("SELECT COUNT(*) FROM chaves WHERE uid=? AND chave LIKE ?", (uid, "m:%s:%s:%%" % (periodo, chave))).fetchone()[0]
            if ja >= p["qtd"] or not marcar_chave(con, uid, "m:%s:%s:%s" % (periodo, chave, dados.get("modelo"))):
                raise ValueError("missao ja resgatada")
            ganho = _ganhar(con, l, md["por"] * alvo, repetivel=False)

        elif acao == "conquista":
            cid = str(dados.get("id", ""))
            c = TABELAS["conquistas"].get(cid)
            if not c:
                raise ValueError("conquista invalida")
            ok = True
            if c["cont"] == "vitorias":
                ok = l["vitorias"] >= c["alvo"]
            elif c["cont"] == "insignias":
                ok = con.execute("SELECT COUNT(*) FROM chaves WHERE uid=? AND chave GLOB 'g[0-9]*'", (uid,)).fetchone()[0] >= c["alvo"]
            elif c["cont"] == "campeao":
                ok = tem_chave(con, uid, "campeao")
            elif c["cont"] == "torreRecorde":
                ok = l["torre_rec"] >= c["alvo"]
            elif c["cont"] == "arena":
                ok = l["arena_vit"] >= c["alvo"]
            if not ok:
                raise ValueError("conquista ainda nao confere no servidor")
            if not marcar_chave(con, uid, "c:" + cid):
                raise ValueError("conquista ja resgatada")
            ganho = _ganhar(con, l, c["premio"], repetivel=False)

        elif acao == "diario":
            if l["diario_ult"] == dia_br():
                aviso = "Recompensa de hoje já resgatada."
            else:
                seq = l["diario_seq"] + 1 if l["diario_ult"] == dia_br(-1) else 1
                dia = (seq - 1) % 7 + 1
                mult = 2 if vip_ativo(con, uid) else 1
                con.execute("UPDATE carteira SET diario_ult=?, diario_seq=? WHERE uid=?", (dia_br(), seq, uid))
                ganho = _ganhar(con, l, (150 + dia * 100) * mult, repetivel=False)

        elif acao == "libertar":
            if l["libertar_dia"] >= MAX_LIBERTAR_DIA:
                aviso = "Limite diário de recompensas por libertar atingido."
            else:
                nivel = _int(dados.get("nivel"), 1, teto)
                var = _int(dados.get("var"), 0, 1)
                ganho = _ganhar(con, l, nivel * 5 * (3 if var else 1))
                con.execute("UPDATE carteira SET libertar_dia=libertar_dia+1 WHERE uid=?", (uid,))

        elif acao == "derrota":
            tipo = str(dados.get("tipo", ""))
            con.execute("UPDATE carteira SET pend=0 WHERE uid=?", (uid,))
            if tipo not in ("torre", "arena"):
                perda = l["moedas"] // 10
                piso = XP_TREINADOR * (nv - 1) ** 2
                faixa = XP_TREINADOR * nv ** 2 - piso
                perda_xp = max(0, min(l["xp"] - piso, int(round(faixa * 0.15))))
                con.execute("UPDATE carteira SET moedas=moedas-?, xp=xp-? WHERE uid=?", (perda, perda_xp, uid))
                ganho = -perda
            else:
                con.execute("UPDATE carteira SET torre=0 WHERE uid=?", (uid,))

        elif acao in ("comprar", "roupa"):
            if acao == "comprar":
                preco_un = TABELAS["itens"].get(str(dados.get("item", "")))
                qtd = _int(dados.get("qtd"), 1, 99)
                if not preco_un:
                    raise ValueError("item invalido")
                preco = preco_un * qtd
            else:
                r = TABELAS["roupas"].get(str(dados.get("id", "")))
                if not r:
                    raise ValueError("roupa invalida")
                if r["requer"] == "campeao" and not tem_chave(con, uid, "campeao"):
                    raise ValueError("so para o Campeao")
                if not marcar_chave(con, uid, "roupa:" + str(dados.get("id"))):
                    raise ValueError("roupa ja comprada")
                preco = r["preco"]
            cur = con.execute("UPDATE carteira SET moedas=moedas-? WHERE uid=? AND moedas>=?", (preco, uid, preco))
            if cur.rowcount != 1:
                raise ValueError("moedas insuficientes")
            ganho = -preco

        else:
            raise ValueError("acao invalida")

        l = con.execute("SELECT * FROM carteira WHERE uid=?", (uid,)).fetchone()
        _marcos(con, uid, xp_antes, l["xp"], cristais_ganhos)
        resp = {"moedas": l["moedas"], "xp": l["xp"], "nivel": nivel_tr(l["xp"]), "ganho": ganho,
                "torre": l["torre"], "cristais": ver_saldo_con(con, uid)}
        if cristais_ganhos:
            resp["cristaisGanhos"] = cristais_ganhos
        if aviso:
            resp["aviso"] = aviso
        return resp


# ----------------------------------------------------------------------------
# Cristais por pagamento externo (Pix / cartao) - pre-programado.
# O jogo pede um "pedido" e abre um link fora do Telegram. Quando a plataforma
# de pagamento confirmar, ela chama /api/pagamento/confirmar com a assinatura
# HMAC (segredo PAGAMENTO_SEGREDO) e os Cristais entram no saldo.
# ----------------------------------------------------------------------------
PAGAMENTO_URL = os.environ.get("PAGAMENTO_URL", "")           # checkout da plataforma escolhida
PAGAMENTO_SEGREDO = os.environ.get("PAGAMENTO_SEGREDO", "")   # segredo do webhook
URL_PUBLICA = os.environ.get("URL_PUBLICA", "https://147-15-21-209.sslip.io")
PACOTES_REAIS = {
    "cristais60": {"titulo": "60 Cristais", "centavos": 490, "cristais": 60},
    "cristais130": {"titulo": "130 Cristais", "centavos": 990, "cristais": 130},
    "cristais700": {"titulo": "700 Cristais", "centavos": 4990, "cristais": 700},
    "cristais1500": {"titulo": "1500 Cristais", "centavos": 9990, "cristais": 1500},
}


def preparar_pedidos(con):
    con.execute("""CREATE TABLE IF NOT EXISTS pedidos (
        id TEXT PRIMARY KEY,
        uid INTEGER NOT NULL,
        pacote TEXT NOT NULL,
        centavos INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pendente',
        criado INTEGER NOT NULL,
        pago INTEGER,
        referencia TEXT)""")


def criar_pedido(uid, pacote_id):
    p = PACOTES_REAIS.get(pacote_id)
    if not p:
        raise ValueError("pacote invalido")
    with _trava_banco, banco() as con:
        abertos = con.execute("SELECT COUNT(*) FROM pedidos WHERE uid=? AND status='pendente' AND criado>?", (uid, int(time.time()) - 3600)).fetchone()[0]
        if abertos >= 5:
            raise ValueError("muitos pedidos abertos")
        pid = "P" + "".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(11))
        con.execute("INSERT INTO pedidos (id, uid, pacote, centavos, criado) VALUES (?,?,?,?,?)",
                    (pid, uid, pacote_id, p["centavos"], int(time.time())))
    # So o codigo do pedido vai no link: nada de dados pessoais na URL.
    base = PAGAMENTO_URL or (URL_PUBLICA + "/pagar")
    link = base + ("&" if "?" in base else "?") + urllib.parse.urlencode({"pedido": pid})
    return {"pedido": pid, "link": link, "valor": p["centavos"]}


def confirmar_pedido(pid, referencia=""):
    """Credita os Cristais de um pedido pago (uma unica vez)."""
    with _trava_banco, banco() as con:
        l = con.execute("SELECT * FROM pedidos WHERE id=?", (pid,)).fetchone()
        if not l:
            raise ValueError("pedido inexistente")
        if l["status"] == "pago":
            return False
        con.execute("UPDATE pedidos SET status='pago', pago=?, referencia=? WHERE id=? AND status='pendente'",
                    (int(time.time()), str(referencia)[:120], pid))
        creditar(con, l["uid"], PACOTES_REAIS[l["pacote"]]["cristais"])
        con.execute("INSERT INTO compras (uid, pacote, estrelas, cobranca, criado, entregue) VALUES (?,?,?,?,?,1)",
                    (l["uid"], l["pacote"], 0, "pedido:" + pid, int(time.time())))
    try:
        enviar(l["uid"], "💎 Pagamento confirmado! %s já estão no seu saldo." % PACOTES_REAIS[l["pacote"]]["titulo"], teclado_jogar())
    except Exception:
        pass
    return True


def ver_pedido(pid):
    with _trava_banco, banco() as con:
        l = con.execute("SELECT id, pacote, centavos, status FROM pedidos WHERE id=?", (str(pid)[:20],)).fetchone()
    return dict(l) if l else None


def pagina_pagar(pid):
    """Pagina provisoria de checkout. Quando a plataforma for escolhida, ela
    passa a criar o Pix/cartao aqui (ou PAGAMENTO_URL aponta direto para ela)."""
    p = ver_pedido(pid)
    if not p:
        corpo = "<h1>Pedido não encontrado</h1><p>Volte ao jogo e tente de novo.</p>"
    else:
        pac = PACOTES_REAIS[p["pacote"]]
        valor = "R$ %d,%02d" % (p["centavos"] // 100, p["centavos"] % 100)
        if p["status"] == "pago":
            corpo = "<h1>✅ Pago!</h1><p>%s já estão na sua conta. Pode voltar ao jogo.</p>" % pac["titulo"]
        else:
            corpo = ("<h1>💎 %s</h1><p class=v>%s</p><p>Pedido <b>%s</b></p>"
                     "<div class=box>Pagamento por <b>Pix</b> ou <b>cartão de crédito</b> chegando em breve.<br>"
                     "Nenhuma cobrança foi feita.</div>") % (pac["titulo"], valor, p["id"])
    html = ("<!doctype html><html lang=pt-BR><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Domadores de Feras · Cristais</title><style>body{margin:0;font-family:system-ui,sans-serif;background:#14121c;color:#eee;"
            "display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px}main{max-width:420px;text-align:center}"
            "h1{font-size:24px}.v{font-size:32px;font-weight:700;color:#f5c542}.box{background:#211d2e;border-radius:12px;padding:16px;margin-top:16px}"
            "</style></head><body><main>%s</main></body></html>") % corpo
    return html.encode("utf-8")


def webhook_pagamento(corpo_bruto, assinatura):
    """Chamado pela plataforma de pagamento (ou por um adaptador nosso)."""
    if not PAGAMENTO_SEGREDO:
        raise ValueError("pagamento externo ainda nao configurado")
    esperado = hmac.new(PAGAMENTO_SEGREDO.encode(), corpo_bruto, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(esperado, str(assinatura or "")):
        raise PermissionError("assinatura invalida")
    dados = json.loads(corpo_bruto.decode("utf-8") or "{}")
    if dados.get("status") != "pago":
        return False
    p = ver_pedido(dados.get("pedido", ""))
    if not p:
        raise ValueError("pedido inexistente")
    if _int(dados.get("valor"), 0, 10 ** 9) < p["centavos"]:
        raise ValueError("valor diferente do pedido")
    return confirmar_pedido(p["id"], dados.get("referencia", ""))


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
        if self.path.startswith("/api/config"):
            cfg, versao = ler_config()
            return self._json(200, {"ok": True, "config": cfg, "versao": versao})
        if self.path.startswith("/pagar"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            dados = pagina_pagar((q.get("pedido") or [""])[0])
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(dados)))
            self.end_headers()
            self.wfile.write(dados)
            return
        return self._json(404, {"ok": False})

    def do_POST(self):
        try:
            tamanho = int(self.headers.get("Content-Length", "0"))
            if tamanho > (CONFIG_MAX + 8000 if self.path.startswith("/api/admin/config") or self.path.startswith("/api/admin/ia") else 20000):
                return self._json(413, {"ok": False, "erro": "grande demais"})
            bruto = self.rfile.read(tamanho)
            if self.path.startswith("/api/pagamento/confirmar"):
                try:
                    novo = webhook_pagamento(bruto, self.headers.get("X-Assinatura", ""))
                    return self._json(200, {"ok": True, "creditado": novo})
                except PermissionError:
                    return self._json(403, {"ok": False, "erro": "assinatura invalida"})
                except ValueError as e:
                    return self._json(400, {"ok": False, "erro": str(e)})
            corpo = json.loads(bruto.decode("utf-8") or "{}")
        except (ValueError, TypeError):
            return self._json(400, {"ok": False, "erro": "json invalido"})
        usuario = validar_init_data(corpo.get("initData", ""))
        if not usuario:
            return self._json(401, {"ok": False, "erro": "nao autorizado"})
        uid = usuario["id"]
        if not limitar_taxa(uid, 90):
            return self._json(429, {"ok": False, "erro": "muitas requisicoes"})
        try:
            if self.path.startswith("/api/moedas"):
                r = moedas_acao(usuario, corpo)
                r["ok"] = True
                return self._json(200, r)
            if self.path.startswith("/api/pedido"):
                r = criar_pedido(uid, str(corpo.get("pacote", "")))
                r["ok"] = True
                return self._json(200, r)
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
            if self.path.startswith("/api/admin/eu"):
                return self._json(200, {"ok": True, "admin": eh_admin(uid), "id": uid})
            if self.path.startswith("/api/admin/reivindicar"):
                reivindicar_admin(usuario, corpo.get("codigo"))
                return self._json(200, {"ok": True, "admin": True})
            if self.path.startswith("/api/admin/ia/chave"):
                try:
                    salvar_chave_ia(uid, corpo.get("chave"), corpo.get("modelo"))
                except PermissionError:
                    return self._json(403, {"ok": False, "erro": "so administrador"})
                return self._json(200, {"ok": True, "ia": info_ia()})
            if self.path.startswith("/api/admin/ia/info"):
                if not eh_admin(uid):
                    return self._json(403, {"ok": False, "erro": "so administrador"})
                return self._json(200, {"ok": True, "ia": info_ia()})
            if self.path.startswith("/api/admin/ia"):
                try:
                    r = assistente_ia(uid, corpo.get("pedido"), corpo.get("contexto") or {})
                except PermissionError:
                    return self._json(403, {"ok": False, "erro": "so administrador"})
                return self._json(200, {"ok": True, "resposta": r})
            if self.path.startswith("/api/admin/config"):
                try:
                    versao = gravar_config(uid, corpo.get("config"))
                except PermissionError:
                    return self._json(403, {"ok": False, "erro": "so administrador"})
                return self._json(200, {"ok": True, "versao": versao})
            if self.path.startswith("/api/ranking"):
                pontos = salvar_ranking(usuario, corpo.get("dados") or {})
                return self._json(200, {"ok": True, "pontos": pontos, "posicao": posicao(uid)})
        except ValueError as e:
            return self._json(400, {"ok": False, "erro": str(e)})
        except Exception:
            log("erro na api", traceback.format_exc())
            return self._json(500, {"ok": False, "erro": "erro interno"})
        return self._json(404, {"ok": False})


# ----------------------------------------------------------------------------
# Painel ADM: balanceamento do jogo editado pelo dono, sem mexer no codigo
#   POST /api/admin/eu          -> {admin: bool}
#   POST /api/admin/reivindicar -> {codigo} vira administrador (codigo secreto, so o hash fica aqui)
#   POST /api/admin/config      -> {config} grava o balanceamento (so administrador)
#   GET  /api/config            -> balanceamento atual (o jogo baixa ao abrir)
# ----------------------------------------------------------------------------
HASH_CODIGO_ADM = "00dc785dab0875b5d382cd07aaf7b4fb571a8e3e0c4cd480d7ae31709eb21c1a"
CONFIG_MAX = 400000


def _tabelas_adm(con):
    con.execute("CREATE TABLE IF NOT EXISTS admins (uid INTEGER PRIMARY KEY, nome TEXT, desde REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS config_jogo (id INTEGER PRIMARY KEY CHECK (id = 1), valor TEXT, versao INTEGER, atualizado REAL, por INTEGER)")


def eh_admin(uid):
    with _trava_banco, banco() as con:
        _tabelas_adm(con)
        return con.execute("SELECT 1 FROM admins WHERE uid=?", (uid,)).fetchone() is not None


def reivindicar_admin(usuario, codigo):
    codigo = str(codigo or "").strip().upper()
    if not codigo or hashlib.sha256(codigo.encode()).hexdigest() != HASH_CODIGO_ADM:
        time.sleep(1.5)
        raise ValueError("codigo invalido")
    with _trava_banco, banco() as con:
        _tabelas_adm(con)
        con.execute("INSERT OR REPLACE INTO admins (uid, nome, desde) VALUES (?, ?, ?)",
                    (usuario["id"], str(usuario.get("first_name", ""))[:40], time.time()))
    log("novo administrador", usuario["id"])
    return True


def ler_config():
    with _trava_banco, banco() as con:
        _tabelas_adm(con)
        l = con.execute("SELECT valor, versao FROM config_jogo WHERE id=1").fetchone()
    if not l:
        return {}, 0
    try:
        return json.loads(l["valor"]), l["versao"]
    except ValueError:
        return {}, 0


def gravar_config(uid, cfg):
    if not isinstance(cfg, dict):
        raise ValueError("config invalida")
    texto = json.dumps(cfg, ensure_ascii=False, separators=(",", ":"))
    if len(texto) > CONFIG_MAX:
        raise ValueError("config grande demais")
    with _trava_banco, banco() as con:
        _tabelas_adm(con)
        if con.execute("SELECT 1 FROM admins WHERE uid=?", (uid,)).fetchone() is None:
            raise PermissionError("so administrador")
        l = con.execute("SELECT versao FROM config_jogo WHERE id=1").fetchone()
        versao = (l["versao"] if l else 0) + 1
        con.execute("INSERT OR REPLACE INTO config_jogo (id, valor, versao, atualizado, por) VALUES (1, ?, ?, ?, ?)",
                    (texto, versao, time.time(), uid))
    _cache_cfg["t"] = 0
    log("config do jogo atualizada", versao, "por", uid)
    return versao



_cache_cfg = {"t": 0, "cfg": {}}


def regra_jogo(nome, padrao, minimo, maximo):
    """Le uma regra do painel ADM (com cache de 30 s) dentro de limites seguros."""
    agora = time.time()
    if agora - _cache_cfg["t"] > 30:
        try:
            # sem pegar a trava: esta funcao e chamada de dentro de operacoes que ja estao com ela
            con = sqlite3.connect(BANCO, timeout=2)
            con.row_factory = sqlite3.Row
            try:
                # so leitura (sem CREATE): nao disputa a escrita com a operacao em andamento
                l = con.execute("SELECT valor FROM config_jogo WHERE id=1").fetchone()
                _cache_cfg["cfg"] = json.loads(l["valor"]) if l else {}
            finally:
                con.close()
        except Exception:
            _cache_cfg["cfg"] = {}
        _cache_cfg["t"] = agora
    try:
        v = float((_cache_cfg["cfg"].get("regras") or {}).get(nome, padrao))
    except (TypeError, ValueError):
        v = padrao
    return max(minimo, min(maximo, v))


# ----------------------------------------------------------------------------
# Assistente do painel ADM: o dono escreve o pedido em portugues e uma IA
# devolve as mudancas no formato do painel (so dados de balanceamento, nunca codigo).
# A chave da IA (Gemini gratis ou Anthropic) fica so no servidor.
# ----------------------------------------------------------------------------
INSTRUCOES_IA = """Voce e o assistente do painel de administrador do jogo Domadores de Feras (RPG de monstros).
O dono do jogo NAO e programador. Ele escreve um pedido em portugues e voce devolve SO um JSON com as mudancas.
Voce so pode mexer nos dados descritos no ESQUEMA. Use apenas ids que existem no CATALOGO.
Responda exatamente neste formato JSON:
{"explicacao": "frase curta em portugues dizendo o que voce mudou e por que", "mudancas": {"especies": {}, "golpes": {}, "rotas": {}, "ginasios": {}, "regras": {}}, "duvida": ""}
- Coloque em "mudancas" so o que muda (os valores novos completos de cada item alterado).
- Se o pedido for ambiguo, impossivel ou fora do esquema, deixe "mudancas" vazio e explique em "duvida".
- Nunca invente ids. Nunca mude nada que nao foi pedido.
"""


def _segredo(con, chave, valor=None):
    con.execute("CREATE TABLE IF NOT EXISTS segredos (k TEXT PRIMARY KEY, v TEXT)")
    if valor is not None:
        con.execute("INSERT OR REPLACE INTO segredos (k, v) VALUES (?, ?)", (chave, valor))
        return valor
    l = con.execute("SELECT v FROM segredos WHERE k=?", (chave,)).fetchone()
    return l["v"] if l else ""


def salvar_chave_ia(uid, chave, modelo):
    chave = str(chave or "").strip()
    with _trava_banco, banco() as con:
        _tabelas_adm(con)
        if con.execute("SELECT 1 FROM admins WHERE uid=?", (uid,)).fetchone() is None:
            raise PermissionError("so administrador")
        if chave:
            if len(chave) < 20 or len(chave) > 300:
                raise ValueError("chave invalida")
            _segredo(con, "ia_chave", chave)
        if modelo is not None:
            _segredo(con, "ia_modelo", str(modelo).strip()[:80])
    return True


def info_ia():
    with _trava_banco, banco() as con:
        c = _segredo(con, "ia_chave")
        m = _segredo(con, "ia_modelo")
    prov = "anthropic" if c.startswith("sk-ant") else ("gemini" if c else "")
    return {"configurada": bool(c), "provedor": prov, "modelo": m or ("claude-haiku-5-5" if prov == "anthropic" else "gemini-2.5-flash")}


def _post_json(url, corpo, cabecalhos, limite=90):
    req = urllib.request.Request(url, data=json.dumps(corpo).encode("utf-8"), headers=dict({"Content-Type": "application/json"}, **cabecalhos))
    try:
        with urllib.request.urlopen(req, timeout=limite) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detalhe = e.read().decode("utf-8", "replace")[:300]
        log("erro ia", e.code, detalhe)
        raise ValueError("a IA recusou (%d): %s" % (e.code, detalhe[:160]))


def assistente_ia(uid, pedido, contexto):
    if not eh_admin(uid):
        raise PermissionError("so administrador")
    pedido = str(pedido or "").strip()[:2000]
    if not pedido:
        raise ValueError("pedido vazio")
    with _trava_banco, banco() as con:
        chave = _segredo(con, "ia_chave")
        modelo = _segredo(con, "ia_modelo")
    if not chave:
        raise ValueError("sem chave de IA")
    texto = "ESQUEMA E CATALOGO:\n" + json.dumps(contexto, ensure_ascii=False)[:90000] + "\n\nPEDIDO DO DONO DO JOGO:\n" + pedido
    if chave.startswith("sk-ant"):
        r = _post_json("https://api.anthropic.com/v1/messages",
                       {"model": modelo or "claude-haiku-5-5", "max_tokens": 4000, "system": INSTRUCOES_IA,
                        "messages": [{"role": "user", "content": texto}]},
                       {"x-api-key": chave, "anthropic-version": "2023-06-01"})
        saida = "".join(p.get("text", "") for p in r.get("content", []))
    else:
        r = _post_json("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s" % (urllib.parse.quote(modelo or "gemini-2.5-flash"), urllib.parse.quote(chave)),
                       {"systemInstruction": {"parts": [{"text": INSTRUCOES_IA}]},
                        "contents": [{"role": "user", "parts": [{"text": texto}]}],
                        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}, {})
        saida = "".join(p.get("text", "") for c in r.get("candidates", []) for p in (c.get("content") or {}).get("parts", []))
    ini, fim = saida.find("{"), saida.rfind("}")
    if ini < 0 or fim < 0:
        raise ValueError("a IA nao devolveu mudancas")
    try:
        resposta = json.loads(saida[ini:fim + 1])
    except ValueError:
        raise ValueError("a IA devolveu um formato estranho, tente reescrever o pedido")
    return resposta


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
