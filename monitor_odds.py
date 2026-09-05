"""
Script: Monitor de Odds - HT-Z1
---------------------------------
Roda a cada 5 minutos (via GitHub Actions). Busca os jogos do dia que
batem no filtro HT-Z1, verifica a odd atual do mercado "1st half - total
0.5 (over)" via feed da Sportradar, e avisa no Telegram assim que a odd
atingir o alvo definido (uma única vez por jogo).

Variaveis de ambiente (Secrets do GitHub):
- FULLTRADER_ACCESS_TOKEN
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID
- SPORTRADAR_ODDS_TOKEN  -> a parte da URL depois do "?" numa chamada
  match_markets capturada no DevTools (Network) enquanto navega no
  Scanner -> Jogos do FullTrader. Formato:
  T=exp=...~acl=...~data=...~hmac=...
  Expira de tempos em tempos (dias) -- quando expirar, o script avisa
  automaticamente no Telegram pra você renovar.

COMO RENOVAR O SPORTRADAR_ODDS_TOKEN:
1. app.fulltrader.com -> menu -> Scanner -> aba "Jogos"
2. F12 -> Network -> filtro Fetch/XHR
3. Procure uma chamada pra lmt.fn.sportradar.com/.../match_markets/...
4. Clique nela, copie a "Request URL" completa
5. Pegue só a parte DEPOIS do "?" (começa com "T=exp=")
6. Cole isso no Secret SPORTRADAR_ODDS_TOKEN no GitHub
"""

import os
import re
import json
import requests
from datetime import datetime, timezone, timedelta

# ======================= CONFIGURAÇÃO =======================
ACCESS_TOKEN = os.environ.get("FULLTRADER_ACCESS_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SPORTRADAR_ODDS_TOKEN = os.environ.get("SPORTRADAR_ODDS_TOKEN", "")

GITHUB_REPO_URL = "https://github.com/Uzebetis/automacaofull"

GAMES_COUNT = 5
FILTRO_NOMES_ACEITOS = {"HT-Z1", "HTZ1", "HT-ZI", "HTZI"}

ALVO_ODD = 1.40          # odd mínima pra avisar (odds p/ lucro do HT-Z1)
JANELA_MINUTOS = 50       # só checa a odd do início do jogo até X minutos depois (1º tempo + acréscimos)
MARKET_ID = 68            # "1st half - total"
MARKET_TOTAL = "0.5"      # linha 0.5
OUTCOME_PREFIXO = "over"  # queremos o lado "over 0.5"

ARQUIVO_ESTADO = "estado_alertas_odds.json"
# ==============================================================

FILTERS_URL = "https://apiprelive.fulltraderapps.com/filters"
GAMES_URL_TEMPLATE = "https://apiprelive.fulltraderapps.com/games/list/{date}"
MARKETS_URL_TEMPLATE = "https://lmt.fn.sportradar.com/common/en/Etc:UTC/gismo/match_markets/{match_id}?{token}"

FUSO_BRASILIA = timezone(timedelta(hours=-3))


# ------------------------- FullTrader -------------------------
def _checar_erro_auth(resp):
    if resp.status_code == 401:
        raise Exception(
            "Token do FullTrader expirado ou inválido. Atualize o Secret "
            "FULLTRADER_ACCESS_TOKEN."
        )
    resp.raise_for_status()


def buscar_todos_filtros(token):
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(FILTERS_URL, headers=headers)
    _checar_erro_auth(resp)
    return resp.json()


def montar_payload_filtro(games_count, filtro_data):
    blocos = list(filtro_data)
    while len(blocos) < 6:
        blocos.append([])
    return [games_count] + blocos


def buscar_jogos_filtrados(token, data_hoje, payload_filtro):
    headers = {"Authorization": f"Bearer {token}"}
    url = GAMES_URL_TEMPLATE.format(date=data_hoje)
    resp = requests.post(url, headers=headers, json=payload_filtro)
    _checar_erro_auth(resp)
    return resp.json()


def normalizar_nome(nome):
    return nome.strip().upper().replace(" ", "").replace("-", "")


def encontrar_filtro_alvo(todos_filtros):
    alvo_normalizado = {normalizar_nome(n) for n in FILTRO_NOMES_ACEITOS}
    for filtro in todos_filtros:
        nome = filtro.get("name", "")
        if normalizar_nome(nome) in alvo_normalizado:
            return filtro
    return None


# ------------------------- Odds ao vivo (Sportradar) -------------------------
def extrair_sportradar_id(jogo):
    """O ID sportradar vem no formato 'sr:match:12345' (posição 9 do array bruto)."""
    if len(jogo) <= 9 or not jogo[9]:
        return None
    valor = str(jogo[9])
    m = re.search(r"(\d+)$", valor)
    return m.group(1) if m else None


def buscar_odd_atual(match_id):
    """Retorna a odd atual do mercado configurado (0.5 HT).
    - Retorna um número > 0 se o mercado estiver aberto, com a odd atual.
    - Retorna 0 se o mercado já foi fechado (ex: já saiu o gol, aposta resolvida)
      -> sinal pra parar de monitorar esse jogo específico.
    - Retorna None se o mercado ainda nem foi publicado (tenta de novo depois).
    Levanta exceção se o token de odds estiver expirado/inválido."""
    url = MARKETS_URL_TEMPLATE.format(match_id=match_id, token=SPORTRADAR_ODDS_TOKEN)
    resp = requests.get(url, timeout=15)

    if resp.status_code != 200:
        raise Exception(
            f"Token de odds (SPORTRADAR_ODDS_TOKEN) expirado ou inválido "
            f"(status {resp.status_code}) ao consultar o jogo {match_id}."
        )

    data = resp.json()
    try:
        markets = data["doc"][0]["data"]["markets"]
    except (KeyError, IndexError, TypeError):
        # resposta "200 OK" mas sem a estrutura esperada -> quase sempre é
        # o token de odds vencido (a Sportradar não devolve 403 nesse caso,
        # devolve uma resposta vazia/incompleta), então tratamos igual.
        raise Exception(
            f"Resposta inesperada da Sportradar pro jogo {match_id} "
            f"(token de odds provavelmente vencido). Resposta recebida: {str(data)[:200]}"
        )

    for mercado in markets:
        if mercado.get("_marketId") == MARKET_ID and str(mercado.get("specifiers", {}).get("total")) == MARKET_TOTAL:
            if not mercado.get("active", False):
                return 0  # mercado fechado (gol já saiu) -> parar de monitorar esse jogo
            for outcome in mercado.get("outcomes", []):
                nome = (outcome.get("name") or "").lower()
                if nome.startswith(OUTCOME_PREFIXO):
                    if not outcome.get("active", False):
                        return 0  # essa opção específica já fechou (gol já saiu)
                    return outcome.get("odds")
    return None


# ------------------------- Telegram -------------------------
def enviar_telegram(mensagem):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": mensagem})
    resp.raise_for_status()


def avisar_erro(mensagem):
    print(f"ERRO: {mensagem}")
    try:
        enviar_telegram(f"⚠️ Monitor de odds - erro\n\n{mensagem}")
    except Exception as e:
        print(f"Não consegui nem avisar no Telegram: {e}")


def avisar_token_odds_vencido():
    link = f"{GITHUB_REPO_URL}/settings/secrets/actions/SPORTRADAR_ODDS_TOKEN"
    avisar_erro(
        "O token de odds ao vivo (SPORTRADAR_ODDS_TOKEN) expirou.\n\n"
        "Pra renovar:\n"
        "1. app.fulltrader.com -> Scanner -> Jogos\n"
        "2. F12 -> Network -> filtro Fetch/XHR\n"
        "3. Ache uma chamada 'match_markets', copie a Request URL\n"
        "4. Cole aqui só a parte depois do '?' (começa com T=exp=)\n"
        f"5. Cole em: {link}"
    )


# ------------------------- Estado (evita aviso duplicado) -------------------------
def carregar_estado():
    if os.path.exists(ARQUIVO_ESTADO):
        with open(ARQUIVO_ESTADO, "r", encoding="utf-8") as f:
            estado = json.load(f)
    else:
        estado = {}

    hoje = datetime.now(FUSO_BRASILIA).strftime("%Y-%m-%d")
    if estado.get("data") != hoje:
        estado = {"data": hoje, "alertados": []}
    return estado


def salvar_estado(estado):
    with open(ARQUIVO_ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


# ------------------------- Main -------------------------
def main():
    if not SPORTRADAR_ODDS_TOKEN:
        avisar_erro("SPORTRADAR_ODDS_TOKEN não configurado. Nada foi verificado nesta execução.")
        return

    hoje = datetime.now(FUSO_BRASILIA).strftime("%Y-%m-%d")
    estado = carregar_estado()
    alertados = set(estado.get("alertados", []))

    print("Buscando filtros...")
    try:
        todos_filtros = buscar_todos_filtros(ACCESS_TOKEN)
    except Exception as e:
        avisar_erro(f"Erro ao buscar filtros do FullTrader: {e}")
        return

    filtro = encontrar_filtro_alvo(todos_filtros)
    if not filtro:
        avisar_erro("Não encontrei nenhum filtro chamado HT-Z1 na conta. Verifique o nome exato.")
        return

    payload = montar_payload_filtro(GAMES_COUNT, filtro.get("data", []))

    try:
        jogos = buscar_jogos_filtrados(ACCESS_TOKEN, hoje, payload)
    except Exception as e:
        avisar_erro(f"Erro ao buscar jogos do filtro HT-Z1: {e}")
        return

    print(f"{len(jogos)} jogo(s) no filtro HT-Z1 hoje.")

    agora = datetime.now(FUSO_BRASILIA)
    checados = 0

    for jogo in jogos:
        match_id = extrair_sportradar_id(jogo)
        if not match_id:
            continue

        chave = f"{hoje}:{match_id}"
        if chave in alertados:
            continue

        time_casa = jogo[2]
        time_fora = jogo[3]

        # só checa o jogo se estiver dentro da janela relevante:
        # do horário de início até JANELA_MINUTOS depois (1º tempo + acréscimos)
        timestamp = jogo[12] if len(jogo) > 12 else None
        if timestamp:
            horario_jogo = datetime.fromtimestamp(timestamp, tz=FUSO_BRASILIA)
            fim_janela = horario_jogo + timedelta(minutes=JANELA_MINUTOS)
            if not (horario_jogo <= agora <= fim_janela):
                continue  # fora da janela: pula silenciosamente, sem gastar chamada

        checados += 1
        try:
            odd_atual = buscar_odd_atual(match_id)
        except Exception as e:
            avisar_token_odds_vencido()
            return  # token vencido afeta todos os jogos, não adianta continuar

        if odd_atual is None:
            print(f"  {time_casa} x {time_fora}: mercado indisponível no momento.")
            continue

        if odd_atual == 0:
            print(f"  {time_casa} x {time_fora}: mercado já fechado (gol já saiu) - parando de monitorar esse jogo.")
            alertados.add(chave)  # marca como resolvido, sem enviar aviso, pra não checar mais hoje
            continue

        print(f"  {time_casa} x {time_fora}: odd atual {odd_atual}")

        if odd_atual >= ALVO_ODD:
            mensagem = (
                f"🎯 HT-Z1 bateu a odd alvo!\n\n"
                f"⚽ {time_casa} x {time_fora}\n"
                f"📈 Odd atual: {odd_atual} (alvo: {ALVO_ODD})"
            )
            try:
                enviar_telegram(mensagem)
                print("  -> Aviso enviado!")
            except Exception as e:
                print(f"  -> Falha ao enviar aviso: {e}")
                continue

            alertados.add(chave)

    estado["alertados"] = sorted(alertados)
    salvar_estado(estado)
    print(f"{checados} jogo(s) estavam dentro da janela de checagem (de {len(jogos)} no total). Execução concluída.")


if __name__ == "__main__":
    main()
