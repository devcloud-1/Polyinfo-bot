import os
import time
import requests
from datetime import datetime

# ============================================================
# CONFIGURACIÓN — edita solo esta sección
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8529223538:AAG6zHWzMr8ncZfjShtjc55Y3IGiNvCwQW8")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "8715771861")

# Wallets a monitorear
WALLETS = {
    "Gohst": os.getenv("WALLET_GOHST", "0x2d4bf8f846bf68f43b9157bf30810d334ac6ca7a"),
    "de5nuts": os.getenv("WALLET_DE5NUTS", "0x80a0da00fbdc8440b0ef601341f14c3e24795708"),
}

# Cuántos segundos entre cada chequeo (120 = 2 minutos)
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))

# Volumen mínimo del mercado para notificar (en USD)
MIN_VOLUME = int(os.getenv("MIN_VOLUME", "50000"))

# ============================================================
# LÓGICA DEL BOT — no necesitas modificar esto
# ============================================================

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

# Guarda el último trade visto por wallet
last_seen = {wallet: None for wallet in WALLETS}


def send_telegram(message: str):
    """Manda mensaje a Telegram."""
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


def get_recent_trades(wallet_address: str) -> list:
    """Obtiene los trades recientes de una wallet via Polymarket Data API."""
    try:
        url = f"{DATA_API}/activity"
        params = {
            "user": wallet_address,
            "limit": 5,
        }
        r = requests.get(url, params=params, timeout=10)
        if r.ok:
            return r.json() or []
        # Fallback: intenta con el endpoint alternativo
        url2 = f"https://clob.polymarket.com/data/trades"
        params2 = {"maker_address": wallet_address, "limit": 5}
        r2 = requests.get(url2, params=params2, timeout=10)
        if r2.ok:
            data = r2.json()
            return data.get("data", []) if isinstance(data, dict) else data
    except Exception as e:
        print(f"[API Error] {wallet_address}: {e}")
    return []


def get_market_volume(market_slug_or_id: str) -> float:
    """Obtiene el volumen de un mercado."""
    try:
        url = f"{GAMMA_API}/markets"
        params = {"slug": market_slug_or_id}
        r = requests.get(url, params=params, timeout=10)
        if r.ok:
            markets = r.json()
            if markets and len(markets) > 0:
                return float(markets[0].get("volume", 0))
    except Exception as e:
        print(f"[Volume Error] {e}")
    return 0


def format_alert(trader_name: str, trade: dict) -> str:
    """Formatea el mensaje de alerta."""
    side = trade.get("side", trade.get("type", "?")).upper()
    market = trade.get("title", trade.get("market", "Mercado desconocido"))
    outcome = trade.get("outcome", trade.get("answer", ""))
    price = trade.get("price", trade.get("avgPrice", 0))
    amount = trade.get("usdcSize", trade.get("size", 0))
    timestamp = datetime.now().strftime("%H:%M:%S")

    # Emoji según tipo
    if "BUY" in side or "COMPRAR" in side:
        emoji = "🟢"
        action = "COMPRÓ"
    elif "SELL" in side or "VENDER" in side:
        emoji = "🔴"
        action = "VENDIÓ"
    else:
        emoji = "⚪"
        action = side

    try:
        price_float = float(price)
        amount_float = float(amount)
    except:
        price_float = 0
        amount_float = 0

    # Calcular retorno potencial si es compra
    potential = ""
    if price_float > 0 and price_float < 1 and "BUY" in side.upper():
        multiplier = round(1 / price_float, 1)
        potential = f"\n💰 <b>Retorno potencial:</b> {multiplier}x si resuelve YES"

    msg = f"""{emoji} <b>NUEVA ENTRADA — {trader_name}</b>
━━━━━━━━━━━━━━━━━━━━
📋 <b>Mercado:</b> {market}
🎯 <b>Posición:</b> {outcome}
⚡ <b>Acción:</b> {action}
💵 <b>Precio:</b> {price_float:.2f}¢
💼 <b>Monto:</b> ${amount_float:.2f} USDC{potential}
⏰ <b>Hora:</b> {timestamp}
━━━━━━━━━━━━━━━━━━━━
🔗 <a href="https://polymarket.com">Ver en Polymarket</a>"""

    return msg


def check_wallet(trader_name: str, wallet_address: str):
    """Chequea si hay trades nuevos para una wallet."""
    global last_seen

    if not wallet_address:
        return

    trades = get_recent_trades(wallet_address)
    if not trades:
        return

    latest = trades[0]

    # Identificador único del trade
    trade_id = latest.get("id", latest.get("transactionHash", str(latest)))

    # Primera vez — solo guarda el estado, no notifica
    if last_seen[trader_name] is None:
        last_seen[trader_name] = trade_id
        print(f"[{trader_name}] Iniciado. Último trade: {trade_id[:20]}...")
        return

    # Si hay trade nuevo
    if trade_id != last_seen[trader_name]:
        last_seen[trader_name] = trade_id
        print(f"[{trader_name}] ¡Trade nuevo detectado!")

        # Verificar volumen del mercado (filtro de calidad)
        market_id = latest.get("market", latest.get("conditionId", ""))
        volume = get_market_volume(market_id)

        if volume < MIN_VOLUME and volume > 0:
            print(f"[{trader_name}] Ignorado — volumen ${volume:,.0f} < ${MIN_VOLUME:,}")
            return

        # Mandar alerta
        message = format_alert(trader_name, latest)
        send_telegram(message)
        print(f"[{trader_name}] Alerta enviada ✓")


def main():
    print("=" * 50)
    print("🤖 Polymarket Copy Alert Bot")
    print(f"   Monitoreando: {', '.join(WALLETS.keys())}")
    print(f"   Intervalo: {CHECK_INTERVAL}s | Vol mínimo: ${MIN_VOLUME:,}")
    print("=" * 50)

    # Mensaje de inicio
    send_telegram(
        "🤖 <b>Bot iniciado</b>\n"
        f"Monitoreando: {', '.join(WALLETS.keys())}\n"
        f"Volumen mínimo: ${MIN_VOLUME:,}"
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
