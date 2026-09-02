"""
Script: FullTrader (todos os filtros) -> Telegram
---------------------------------------------------
Busca todos os filtros salvos no FullTrader, aplica cada um nos jogos do
dia, e envia o resultado formatado para o Telegram.

Este script lê as credenciais de variáveis de ambiente (env vars), pra
funcionar com "Secrets" do GitHub Actions sem expor nada no código:
- FULLTRADER_ACCESS_TOKEN
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID

COMO PEGAR O FULLTRADER_ACCESS_TOKEN:
1. Faça login normalmente em app.fulltrader.com pelo navegador
2. Aperte F12 -> aba "Network" -> filtre por "Fetch/XHR"
3. Clique em qualquer chamada pra apiprelive.fulltraderapps.com
4. Na aba "Headers", em "Request Headers", procure "Authorization"
5. Copie só a parte depois de "Bearer " (sem espaços)

IMPORTANTE: esse token expira de tempos em tempos. Quando expirar,
a execução vai falhar com um erro claro -- basta repetir os passos
acima e atualizar o Secret no GitHub.
"""

import os
import requests
from datetime import datetime, timezone, timedelta

# ======================= CONFIGURAÇÃO =======================
ACCESS_TOKEN = os.environ.get("FULLTRADER_ACCESS_TOKEN", "COLE_SEU_TOKEN_AQUI")

GAMES_COUNT = 5

ENVIAR_TELEGRAM = True
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "SEU_TOKEN_DO_BOT_AQUI")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "SEU_CHAT_ID_AQUI")
# ==============================================================

FILTERS_URL = "https://apiprelive.fulltraderapps.com/filters"
GAMES_URL_TEMPLATE = "https://apiprelive.fulltraderapps.com/games/list/{date}"

FUSO_BRASILIA = timezone(timedelta(hours=-3))


def _checar_erro_auth(resp):
    if resp.status_code == 401:
        raise Exception(
            "Token expirado ou inválido. Pegue um token novo no navegador "
            "e atualize o Secret FULLTRADER_ACCESS_TOKEN no GitHub."
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


def formatar_mensagem(jogos_detalhados, nome_filtro, data_formatada):
    linhas = [f"🤖 Jogos de {data_formatada} - filtro: {nome_filtro}\n"]

    for jogo in jogos_detalhados:
        time_casa = jogo[2]
        time_fora = jogo[3]
        timestamp = jogo[12]
        campeonato = jogo[14]
        pais = jogo[13]

        try:
            horario = datetime.fromtimestamp(timestamp, tz=FUSO_BRASILIA).strftime("%H:%M")
        except Exception:
            horario = "?"

        linhas.append(f"🕐 {horario} | {pais} - {campeonato}\n⚽ {time_casa} x {time_fora}\n")

    if len(linhas) == 1:
        linhas.append("Nenhum jogo encontrado para hoje com esse filtro.")

    return "\n".join(linhas)


LIMITE_TELEGRAM = 3500  # um pouco abaixo do limite real (4096) por segurança


def _dividir_mensagem_em_partes(mensagem, separador="\n\n---\n\n"):
    """Divide a mensagem em pedaços que cabem no limite do Telegram,
    tentando quebrar nos separadores entre filtros (não no meio de um bloco)."""
    if len(mensagem) <= LIMITE_TELEGRAM:
        return [mensagem]

    blocos = mensagem.split(separador)
    partes = []
    parte_atual = ""

    for bloco in blocos:
        candidato = (parte_atual + separador + bloco) if parte_atual else bloco
        if len(candidato) > LIMITE_TELEGRAM and parte_atual:
            partes.append(parte_atual)
            parte_atual = bloco
        else:
            parte_atual = candidato

    if parte_atual:
        partes.append(parte_atual)

    partes_finais = []
    for parte in partes:
        while len(parte) > LIMITE_TELEGRAM:
            partes_finais.append(parte[:LIMITE_TELEGRAM])
            parte = parte[LIMITE_TELEGRAM:]
        partes_finais.append(parte)

    return partes_finais


def enviar_telegram(mensagem):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    partes = _dividir_mensagem_em_partes(mensagem)

    for i, parte in enumerate(partes, start=1):
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": parte}
        resp = requests.post(url, json=payload)
        print(f"  [parte {i}/{len(partes)}] status={resp.status_code} resposta={resp.text}", flush=True)
        resp.raise_for_status()
        print(f"  Parte {i}/{len(partes)} enviada com sucesso!", flush=True)


def avisar_erro_no_telegram(erro):
    """Tenta avisar no Telegram que a execução falhou (ex: token expirado)."""
    try:
        mensagem = f"⚠️ Falha ao buscar os jogos do dia\n\n{erro}"
        enviar_telegram(mensagem)
    except Exception as e:
        print(f"Não consegui nem avisar no Telegram sobre o erro: {e}")


def main():
    hoje = datetime.now(FUSO_BRASILIA).strftime("%Y-%m-%d")
    hoje_formatado = datetime.now(FUSO_BRASILIA).strftime("%d/%m/%Y")

    print("Buscando todos os filtros salvos na conta...")
    try:
        todos_filtros = buscar_todos_filtros(ACCESS_TOKEN)
    except Exception as e:
        print(f"Erro ao buscar filtros: {e}")
        avisar_erro_no_telegram(e)
        raise

    print(f"Encontrados {len(todos_filtros)} filtros: {[f.get('name') for f in todos_filtros]}\n")

    blocos_mensagem = []

    for filtro in todos_filtros:
        nome_filtro = filtro.get("name", "").strip()
        filtro_data = filtro.get("data", [])

        print(f"Aplicando filtro '{nome_filtro}'...")
        payload_filtro = montar_payload_filtro(GAMES_COUNT, filtro_data)

        try:
            jogos_filtrados = buscar_jogos_filtrados(ACCESS_TOKEN, hoje, payload_filtro)
        except Exception as e:
            print(f"  Erro ao aplicar '{nome_filtro}': {e}")
            continue

        bloco = formatar_mensagem(jogos_filtrados, nome_filtro, hoje_formatado)
        blocos_mensagem.append(bloco)

    mensagem_final = "\n\n" + ("\n\n---\n\n".join(blocos_mensagem))

    print("\n===== RESULTADO (TODOS OS FILTROS) =====\n")
    print(mensagem_final)

    if ENVIAR_TELEGRAM:
        print("\nEnviando para o Telegram...")
        enviar_telegram(mensagem_final)
    else:
        print("\n(Envio pro Telegram está desativado)")


if __name__ == "__main__":
    main()
