import os
import time
import requests
from datetime import datetime

# ============================================================
# CONFIGURACIÓN
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8529223538:AAG6zHWzMr8ncZfjShtjc55Y3IGiNvCwQW8")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "8715771861")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")  # Agregar en Railway

WALLETS = {
    "Gohst": os.getenv("WALLET_GOHST", "0x2d4bf8f846bf68f43b9157bf30810d334ac6ca7a"),
    "de5nuts": os.getenv("WALLET_DE5NUTS", "0x80a0da00fbdc8440b0ef601341f14c3e24795708"),
}

# Perfil de cada trader para darle contexto a Claude
TRADER_PROFILES = {
    "Gohst": {
        "win_rate": 57.1,
        "pnl": 103620,
        "profit_factor": 3.4,
        "specialty": "Política y Geopolítica (Middle East, Iran, US Politics)",
        "style": "Contrarian, apuesta baja probabilidad con alta convicción, posiciones largas",
        "months_active": 7,
    },
    "de5nuts": {
        "win_rate": 48.6,
        "pnl": 195145,
        "profit_factor": 4.69,
        "specialty": "Geopolítica y Macro (Taiwan, conflictos internacionales, tech)",
        "style": "Contrarian extremo, fragmenta posiciones, concentra capital cuando está muy seguro",
        "months_active": 8,
    },
}

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))
MIN_VOLUME = int(os.getenv("MIN_VOLUME", "50000"))
USER_BANKROLL = float(os.getenv("USER_BANKROLL", "25"))

# ============================================================
# APIs
# ============================================================

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"

last_seen = {wallet: None for wallet in WALLETS}


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if not r.ok:
            print(f"[Telegram Error] {r.text}")
    except Exception as e:
        print(f"[Telegram Exception] {e}")


# ============================================================
# POLYMARKET API
# ============================================================

def get_recent_trades(wallet_address: str) -> list:
    try:
        r = requests.get(f"{DATA_API}/activity", params={"user": wallet_address, "limit": 5}, timeout=10)
        if r.ok:
            return r.json() or []
        r2 = requests.get("https://clob.polymarket.com/data/trades", params={"maker_address": wallet_address, "limit": 5}, timeout=10)
        if r2.ok:
            data = r2.json()
            return data.get("data", []) if isinstance(data, dict) else data
    except Exception as e:
        print(f"[API Error] {wallet_address}: {e}")
    return []


def get_market_info(market_id: str) -> dict:
    """Obtiene volumen, descripción y fecha de cierre del mercado."""
    try:
        r = requests.get(f"{GAMMA_API}/markets", params={"id": market_id}, timeout=10)
        if r.ok and r.json():
            m = r.json()[0]
            return {
                "volume": float(m.get("volume", 0)),
                "description": m.get("description", ""),
                "end_date": m.get("endDate", ""),
                "liquidity": float(m.get("liquidity", 0)),
                "category": m.get("category", ""),
            }
    except Exception as e:
        print(f"[Market Info Error] {e}")
    return {"volume": 0, "description": "", "end_date": "", "liquidity": 0, "category": ""}


# ============================================================
# ANÁLISIS CON CLAUDE
# ============================================================

def analyze_trade_with_claude(trader_name: str, trade: dict, market_info: dict) -> dict:
    """Llama a Claude Haiku para analizar si vale copiar el trade."""
    if not ANTHROPIC_API_KEY:
        return None

    profile = TRADER_PROFILES.get(trader_name, {})
    side = trade.get("side", trade.get("type", "?")).upper()
    market_title = trade.get("title", trade.get("market", "Desconocido"))
    outcome = trade.get("outcome", trade.get("answer", ""))
    price = float(trade.get("price", trade.get("avgPrice", 0)) or 0)
    amount = float(trade.get("usdcSize", trade.get("size", 0)) or 0)

    prompt = f"""Eres un analista experto en prediction markets en Polymarket.
Evalúa si un usuario con $25 USD debería copiar este trade.

TRADER: {trader_name}
- Win Rate: {profile.get('win_rate')}% | PnL: ${profile.get('pnl'):,} | Profit Factor: {profile.get('profit_factor')}x
- Especialidad: {profile.get('specialty')}
- Estilo: {profile.get('style')}

TRADE:
- Mercado: {market_title}
- Posición: {outcome} | Acción: {side}
- Precio: {price:.3f} ({price*100:.1f}¢) | Apostó: ${amount:.2f}
- Retorno potencial: {round(1/price, 1) if price > 0 else '?'}x

MERCADO:
- Volumen: ${market_info.get('volume', 0):,.0f} | Liquidez: ${market_info.get('liquidity', 0):,.0f}
- Categoría: {market_info.get('category', '?')} | Cierre: {market_info.get('end_date', '?')}
- Descripción: {market_info.get('description', '')[:200]}

Responde SOLO con este JSON exacto, sin texto adicional:
{{"recommendation": "ENTRAR" | "NO ENTRAR" | "OBSERVAR", "score": <0-100>, "risk_level": "BAJO" | "MEDIO" | "ALTO", "suggested_amount": <0.0-1.50>, "reasoning": "<max 2 oraciones>", "key_factor": "<factor decisivo>"}}"""

    try:
        r = requests.post(
            ANTHROPIC_API,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=20,
        )
        if r.ok:
            import json
            content = r.json()["content"][0]["text"].strip()
            content = content.replace("```json", "").replace("```", "").strip()
            return json.loads(content)
    except Exception as e:
        print(f"[Claude Error] {e}")
    return None


# ============================================================
# FORMATEO DE MENSAJES
# ============================================================

def format_alert(trader_name: str, trade: dict, market_info: dict, analysis: dict) -> str:
    side = trade.get("side", trade.get("type", "?")).upper()
    market = trade.get("title", trade.get("market", "Mercado desconocido"))
    outcome = trade.get("outcome", trade.get("answer", ""))
    price = float(trade.get("price", trade.get("avgPrice", 0)) or 0)
    amount = float(trade.get("usdcSize", trade.get("size", 0)) or 0)
    volume = market_info.get("volume", 0)
    timestamp = datetime.now().strftime("%H:%M:%S")

    action_emoji = "🟢" if "BUY" in side else "🔴"
    action_text = "COMPRÓ" if "BUY" in side else "VENDIÓ"
    multiplier = round(1 / price, 1) if price > 0 and price < 1 else "?"

    if analysis:
        rec = analysis.get("recommendation", "OBSERVAR")
        score = analysis.get("score", 0)
        risk = analysis.get("risk_level", "?")
        suggested = analysis.get("suggested_amount", 0)
        reasoning = analysis.get("reasoning", "")
        key_factor = analysis.get("key_factor", "")

        rec_emoji = {"ENTRAR": "✅", "NO ENTRAR": "❌", "OBSERVAR": "👁"}.get(rec, "👁")

        analysis_block = (
            f"\n━━━━━━━━━━━━━━━━━━━━\n"
            f"🧠 <b>ANÁLISIS IA</b>\n"
            f"{rec_emoji} <b>Veredicto: {rec}</b>\n"
            f"📊 Score: {score}/100  |  Riesgo: {risk}\n"
            f"💡 {reasoning}\n"
            f"🔑 <b>Factor clave:</b> {key_factor}\n"
            f"💰 <b>Monto sugerido:</b> ${suggested:.2f} de tus $25"
        )
    else:
        analysis_block = "\n━━━━━━━━━━━━━━━━━━━━\n⚠️ Análisis IA no disponible"

    return (
        f"{action_emoji} <b>NUEVA ENTRADA — {trader_name}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>Mercado:</b> {market}\n"
        f"🎯 <b>Posición:</b> {outcome}\n"
        f"⚡ <b>Acción:</b> {action_text}\n"
        f"💵 <b>Precio:</b> {price:.3f} ({price*100:.1f}¢)\n"
        f"💼 <b>Apostó:</b> ${amount:.2f} USDC\n"
        f"📈 <b>Retorno potencial:</b> {multiplier}x\n"
        f"🌊 <b>Volumen mercado:</b> ${volume:,.0f}"
        f"{analysis_block}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {timestamp} | 🔗 <a href='https://polymarket.com'>Ver en Polymarket</a>"
    )


# ============================================================
# LOOP PRINCIPAL
# ============================================================

def check_wallet(trader_name: str, wallet_address: str):
    global last_seen

    if not wallet_address:
        return

    trades = get_recent_trades(wallet_address)
    if not trades:
        return

    latest = trades[0]
    trade_id = latest.get("id", latest.get("transactionHash", str(latest)))

    if last_seen[trader_name] is None:
        last_seen[trader_name] = trade_id
        print(f"[{trader_name}] Iniciado. Último trade: {trade_id[:20]}...")
        return

    if trade_id != last_seen[trader_name]:
        last_seen[trader_name] = trade_id
        print(f"[{trader_name}] ¡Trade nuevo detectado!")

        market_id = latest.get("market", latest.get("conditionId", ""))
        market_info = get_market_info(market_id)

        if market_info["volume"] < MIN_VOLUME and market_info["volume"] > 0:
            print(f"[{trader_name}] Ignorado — volumen ${market_info['volume']:,.0f} < ${MIN_VOLUME:,}")
            return

        print(f"[{trader_name}] Consultando análisis IA...")
        analysis = analyze_trade_with_claude(trader_name, latest, market_info)

        message = format_alert(trader_name, latest, market_info, analysis)
        send_telegram(message)

        verdict = analysis.get("recommendation", "N/A") if analysis else "Sin análisis"
        print(f"[{trader_name}] Alerta enviada ✓ | Veredicto: {verdict}")


def main():
    print("=" * 50)
    print("🤖 Polymarket Copy Alert Bot v2 — con IA")
    print(f"   Monitoreando: {', '.join(WALLETS.keys())}")
    print(f"   Intervalo: {CHECK_INTERVAL}s | Vol mínimo: ${MIN_VOLUME:,}")
    print(f"   Análisis IA: {'✓ Activo' if ANTHROPIC_API_KEY else '✗ Falta ANTHROPIC_API_KEY'}")
    print("=" * 50)

    send_telegram(
        "🤖 <b>Bot v2 iniciado — con análisis IA</b>\n"
        f"Monitoreando: {', '.join(WALLETS.keys())}\n"
        f"Cada alerta incluye: ENTRAR / NO ENTRAR / OBSERVAR\n"
        f"{'✅ Análisis IA activo' if ANTHROPIC_API_KEY else '⚠️ Agrega ANTHROPIC_API_KEY en Railway'}"
    )

    while True:
        for name, wallet in WALLETS.items():
            try:
                check_wallet(name, wallet)
            except Exception as e:
                print(f"[Error] {name}: {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
