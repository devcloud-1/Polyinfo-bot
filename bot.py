import os
import time
import json
import requests
from datetime import datetime, timedelta

# ============================================================
# CONFIGURACIÓN
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8529223538:AAG6zHWzMr8ncZfjShtjc55Y3IGiNvCwQW8")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "8715771861")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

WALLETS = {
    "Gohst": os.getenv("WALLET_GOHST", "0x2d4bf8f846bf68f43b9157bf30810d334ac6ca7a"),
    "de5nuts": os.getenv("WALLET_DE5NUTS", "0x80a0da00fbdc8440b0ef601341f14c3e24795708"),
    "aenews2": os.getenv("WALLET_AENEWS2", "0x44c1dfe43260c94ed4f1d00de2e1f80fb113ebc1"),
}

TRADER_PROFILES = {
    "Gohst": {
        "win_rate": 57.1, "pnl": 103620, "profit_factor": 3.4,
        "specialty": "Política y Geopolítica (Middle East, Iran, US Politics)",
        "style": "Contrarian, apuesta baja probabilidad con alta convicción, posiciones largas",
        "months_active": 7,
    },
    "aenews2": {
        "win_rate": 74.6,
        "pnl": 1948004,
        "profit_factor": 2.26,
        "specialty": "Política US, Trump, Geopolítica Middle East (Irán, Estrecho de Ormuz)",
        "style": "Especialista político puro, entra temprano cuando el mercado está mal calibrado, posiciones muy grandes con alta convicción, aguanta semanas o meses",
        "months_active": 26,
    },
    "de5nuts": {
        "win_rate": 48.6, "pnl": 195145, "profit_factor": 4.69,
        "specialty": "Geopolítica y Macro (Taiwan, conflictos internacionales, tech)",
        "style": "Contrarian extremo, fragmenta posiciones, concentra capital cuando está muy seguro",
        "months_active": 8,
    },
}

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))
MIN_VOLUME = int(os.getenv("MIN_VOLUME", "50000"))
USER_BANKROLL = float(os.getenv("USER_BANKROLL", "25"))
TRACKER_FILE = "/tmp/trade_tracker.json"

# Trading config
MY_PRIVATE_KEY = os.getenv("MY_PRIVATE_KEY", "")           # Polygon private key
MY_WALLET = os.getenv("MY_WALLET", "")                      # Your Polygon wallet address
MAX_PER_TRADE = float(os.getenv("MAX_PER_TRADE", "2.0"))    # Max USDC per trade
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "3"))  # Max simultaneous positions
AUTO_TRADE = os.getenv("AUTO_TRADE", "false").lower() == "true"  # False = semi-auto (buttons)

# Polymarket CLOB API credentials (from polymarket.com → Settings → API Keys)
POLY_API_KEY = os.getenv("POLY_API_KEY", "")
POLY_SECRET = os.getenv("POLY_SECRET", "")
POLY_PASSPHRASE = os.getenv("POLY_PASSPHRASE", "")

# APIs
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"

# GitHub persistence — set these in Railway Variables
# GITHUB_TOKEN: your Personal Access Token (repo scope)
# GITHUB_REPO: "usuario/nombre-repo" e.g. "juan/polymarket-bot"
# GITHUB_TRACKER_PATH: path inside repo e.g. "data/trade_tracker.json"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_TRACKER_PATH = os.getenv("GITHUB_TRACKER_PATH", "data/trade_tracker.json")
GITHUB_POSITIONS_PATH = os.getenv("GITHUB_POSITIONS_PATH", "data/positions.json")
_github_tracker_sha = ""    # SHA del archivo actual en GitHub (necesario para updates)
_github_positions_sha = ""  # SHA positions file

# Pending orders waiting for user approval
# Structure: { "callback_data": { trade info } }
pending_approvals: dict = {}

last_seen = {wallet: None for wallet in WALLETS}
last_weekly_report = None

# Pending trades buffer: groups transactions by (trader, market_id) within a time window
# Structure: { "trader:market_id": {"trades": [...], "first_seen": timestamp, "market_info": {...}} }
GROUPING_WINDOW = int(os.getenv("GROUPING_WINDOW", "600"))  # seconds to wait before sending (default 10 min)
pending_trades: dict = {}

# Open positions memory: tracks entries to calculate PnL on exit
# Structure: { "trader:market_id:outcome": {"avg_price": float, "total_amount": float, "entry_time": str, "market_title": str} }
POSITIONS_FILE = "/tmp/positions.json"

def load_positions() -> dict:
    """Carga posiciones — desde GitHub primero, fallback a disco."""
    global _github_positions_sha
    if GITHUB_TOKEN and GITHUB_REPO:
        data, sha = _github_get(GITHUB_POSITIONS_PATH)
        if data is not None:
            _github_positions_sha = sha
            try:
                with open(POSITIONS_FILE, "w") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                pass
            return data
    try:
        if os.path.exists(POSITIONS_FILE):
            with open(POSITIONS_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_positions(data: dict):
    """Guarda posiciones en disco y sincroniza a GitHub en background."""
    global _github_positions_sha
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[Positions Error] {e}")
    if GITHUB_TOKEN and GITHUB_REPO:
        import threading
        def _push():
            global _github_positions_sha
            new_sha = _github_put(
                GITHUB_POSITIONS_PATH, data, _github_positions_sha,
                f"positions: {len(data)} open [{datetime.now().strftime('%Y-%m-%d %H:%M')}]"
            )
            if new_sha:
                _github_positions_sha = new_sha
        threading.Thread(target=_push, daemon=True).start()

def record_entry(trader: str, market_id: str, outcome: str, avg_price: float, total_amount: float, market_title: str):
    """Record an entry position for later PnL calculation on exit."""
    positions = load_positions()
    key = f"{trader}:{market_id}:{outcome.lower()}"
    if key in positions:
        # Average down/up existing position
        existing = positions[key]
        total = existing["total_amount"] + total_amount
        avg = (existing["avg_price"] * existing["total_amount"] + avg_price * total_amount) / total
        existing["avg_price"] = avg
        existing["total_amount"] = total
        print(f"[Positions] Updated: {key} avg={avg:.3f} total=${total:.2f}")
    else:
        positions[key] = {
            "avg_price": avg_price,
            "total_amount": total_amount,
            "entry_time": datetime.now().isoformat(),
            "market_title": market_title,
            "trader": trader,
        }
        print(f"[Positions] New entry: {key} @ {avg_price:.3f} ${total_amount:.2f}")
    save_positions(positions)

def get_entry_for_exit(trader: str, market_id: str, outcome: str) -> dict | None:
    """Retrieve recorded entry position when a SELL is detected."""
    positions = load_positions()
    key = f"{trader}:{market_id}:{outcome.lower()}"
    return positions.get(key)

def close_position(trader: str, market_id: str, outcome: str):
    """Remove position after exit."""
    positions = load_positions()
    key = f"{trader}:{market_id}:{outcome.lower()}"
    if key in positions:
        del positions[key]
        save_positions(positions)
        print(f"[Positions] Closed: {key}")


# ============================================================
# TRACKER — guarda cada decisión en disco
# ============================================================

def _github_headers() -> dict:
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }

def _github_get(path: str) -> tuple:
    """Fetch a file from GitHub. Returns (content_dict, sha)."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return None, ""
    try:
        import base64
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
        r = requests.get(url, headers=_github_headers(), timeout=15)
        if r.ok:
            data = r.json()
            content = json.loads(base64.b64decode(data["content"]).decode())
            return content, data.get("sha", "")
        elif r.status_code == 404:
            return None, ""
    except Exception as e:
        print(f"[GitHub] Error leyendo {path}: {e}")
    return None, ""

def _github_put(path: str, data: dict, sha: str, message: str) -> str:
    """Write a file to GitHub. Returns new SHA or empty string on failure."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return sha
    try:
        import base64
        content = base64.b64encode(json.dumps(data, indent=2).encode()).decode()
        payload = {"message": message, "content": content}
        if sha:
            payload["sha"] = sha
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
        r = requests.put(url, headers=_github_headers(), json=payload, timeout=15)
        if r.ok:
            new_sha = r.json().get("content", {}).get("sha", sha)
            print(f"[GitHub] ✓ Guardado: {path}")
            return new_sha
        else:
            print(f"[GitHub] Error guardando {path}: {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"[GitHub] Excepcion guardando {path}: {e}")
    return sha


def load_tracker() -> dict:
    """Carga el historial — primero desde GitHub, fallback a disco local."""
    global _github_tracker_sha
    if GITHUB_TOKEN and GITHUB_REPO:
        data, sha = _github_get(GITHUB_TRACKER_PATH)
        if data is not None:
            _github_tracker_sha = sha
            try:
                with open(TRACKER_FILE, "w") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                pass
            print(f"[GitHub] Tracker cargado ({len(data.get('trades', []))} trades)")
            return data
    try:
        if os.path.exists(TRACKER_FILE):
            with open(TRACKER_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {"trades": [], "stats": {"total": 0, "entrar": 0, "no_entrar": 0, "observar": 0}}


def save_tracker(data: dict):
    """Guarda en disco local inmediatamente, luego sincroniza a GitHub en background."""
    global _github_tracker_sha
    try:
        with open(TRACKER_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[Tracker Error] disco: {e}")
    if GITHUB_TOKEN and GITHUB_REPO:
        import threading
        def _push():
            global _github_tracker_sha
            n = len(data.get("trades", []))
            new_sha = _github_put(
                GITHUB_TRACKER_PATH, data, _github_tracker_sha,
                f"tracker: {n} trades [{datetime.now().strftime('%Y-%m-%d %H:%M')}]"
            )
            if new_sha:
                _github_tracker_sha = new_sha
        threading.Thread(target=_push, daemon=True).start()


def log_trade(trader: str, market: str, outcome: str, price: float,
              recommendation: str, score: int, suggested_amount: float,
              market_id: str):
    """Registra un trade nuevo con estado pendiente."""
    tracker = load_tracker()
    trade_entry = {
        "id": f"{trader}_{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "timestamp": datetime.now().isoformat(),
        "trader": trader,
        "market": market,
        "market_id": market_id,
        "outcome": outcome,
        "entry_price": price,
        "recommendation": recommendation,
        "score": score,
        "suggested_amount": suggested_amount,
        "status": "PENDING",   # PENDING → WIN | LOSS | EXPIRED
        "resolved_price": None,
        "pnl_if_followed": None,
        "pnl_if_ignored": None,
    }
    tracker["trades"].append(trade_entry)
    tracker["stats"]["total"] += 1
    tracker["stats"][recommendation.lower().replace(" ", "_")] = \
        tracker["stats"].get(recommendation.lower().replace(" ", "_"), 0) + 1
    save_tracker(tracker)
    print(f"[Tracker] Trade registrado: {trade_entry['id']}")
    return trade_entry["id"]


def check_pending_resolutions():
    """Revisa si los trades pendientes ya resolvieron en Polymarket."""
    tracker = load_tracker()
    updated = False

    for trade in tracker["trades"]:
        if trade["status"] != "PENDING":
            continue

        # Solo revisa trades con más de 1 hora de antigüedad
        trade_time = datetime.fromisoformat(trade["timestamp"])
        if datetime.now() - trade_time < timedelta(hours=1):
            continue

        try:
            market_id = trade.get("market_id", "")
            if not market_id:
                continue

            r = requests.get(f"{GAMMA_API}/markets", params={"id": market_id}, timeout=10)
            if not r.ok or not r.json():
                continue

            market_data = r.json()[0]
            is_resolved = market_data.get("resolved", False)
            winning_outcome = market_data.get("winnerOutcome", "")

            if is_resolved and winning_outcome:
                entry_price = trade["entry_price"]
                bet_outcome = trade["outcome"].lower()
                winner = winning_outcome.lower()

                if bet_outcome == winner or bet_outcome == "yes" and winner == "yes":
                    # Ganó
                    resolved_price = 1.0
                    pnl_pct = (1.0 - entry_price) / entry_price * 100
                    trade["status"] = "WIN"
                else:
                    # Perdió
                    resolved_price = 0.0
                    pnl_pct = -100.0
                    trade["status"] = "LOSS"

                amount = trade["suggested_amount"]
                trade["resolved_price"] = resolved_price
                trade["pnl_if_followed"] = amount * (pnl_pct / 100) if trade["recommendation"] == "ENTRAR" else 0
                trade["pnl_if_ignored"] = amount * (pnl_pct / 100) if trade["recommendation"] != "ENTRAR" else 0
                updated = True

                print(f"[Tracker] Resuelto: {trade['id']} → {trade['status']}")

        except Exception as e:
            print(f"[Resolution Error] {e}")

    if updated:
        save_tracker(tracker)

    return tracker


# ============================================================
# REPORTE SEMANAL
# ============================================================

def generate_weekly_report() -> str:
    """Genera el reporte semanal de efectividad."""
    tracker = load_tracker()
    trades = tracker["trades"]

    if not trades:
        return "📊 <b>Reporte Semanal</b>\n\nAún no hay trades registrados."

    # Filtrar última semana
    week_ago = datetime.now() - timedelta(days=7)
    weekly = [t for t in trades if datetime.fromisoformat(t["timestamp"]) > week_ago]
    resolved = [t for t in weekly if t["status"] in ("WIN", "LOSS")]

    total = len(weekly)
    total_resolved = len(resolved)

    # Por recomendación
    entrar_trades = [t for t in resolved if t["recommendation"] == "ENTRAR"]
    no_entrar_trades = [t for t in resolved if t["recommendation"] == "NO ENTRAR"]
    observar_trades = [t for t in resolved if t["recommendation"] == "OBSERVAR"]

    entrar_wins = len([t for t in entrar_trades if t["status"] == "WIN"])
    no_entrar_wins = len([t for t in no_entrar_trades if t["status"] == "WIN"])

    # PnL simulado
    pnl_siguiendo_ia = sum(t.get("pnl_if_followed", 0) or 0 for t in resolved)
    pnl_ignorando_ia = sum(t.get("pnl_if_ignored", 0) or 0 for t in resolved)

    # Win rate de la IA en ENTRAR
    ia_winrate = (entrar_wins / len(entrar_trades) * 100) if entrar_trades else 0

    # Por trader
    gohst_trades = [t for t in resolved if t["trader"] == "Gohst"]
    de5nuts_trades = [t for t in resolved if t["trader"] == "de5nuts"]
    gohst_wins = len([t for t in gohst_trades if t["status"] == "WIN"])
    de5nuts_wins = len([t for t in de5nuts_trades if t["status"] == "WIN"])

    report = (
        f"📊 <b>REPORTE SEMANAL — Efectividad IA</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 Período: últimos 7 días\n"
        f"🔢 Alertas totales: {total} ({total_resolved} resueltas)\n\n"

        f"<b>VEREDICTOS DE LA IA:</b>\n"
        f"✅ ENTRAR: {len(entrar_trades)} trades → {entrar_wins} wins "
        f"({ia_winrate:.0f}% win rate)\n"
        f"❌ NO ENTRAR: {len(no_entrar_trades)} trades → {no_entrar_wins} hubieran ganado\n"
        f"👁 OBSERVAR: {len(observar_trades)} trades\n\n"

        f"<b>PnL SIMULADO (con ${USER_BANKROLL}):</b>\n"
        f"💰 Siguiendo la IA: ${pnl_siguiendo_ia:+.2f}\n"
        f"💸 Ignorando la IA: ${pnl_ignorando_ia:+.2f}\n\n"

        f"<b>POR TRADER:</b>\n"
        f"👻 Gohst: {len(gohst_trades)} trades, {gohst_wins} wins "
        f"({gohst_wins/len(gohst_trades)*100:.0f}%)\n" if gohst_trades else ""
        f"🌰 de5nuts: {len(de5nuts_trades)} trades, {de5nuts_wins} wins "
        f"({de5nuts_wins/len(de5nuts_trades)*100:.0f}%)\n" if de5nuts_trades else ""

        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>PnL simulado — no dinero real</i>"
    )

    return report


def maybe_send_weekly_report():
    """Manda el reporte si no se mandó en los últimos 7 días."""
    global last_weekly_report
    now = datetime.now()

    if last_weekly_report is None or (now - last_weekly_report).days >= 7:
        # Solo manda los lunes a las 9am aproximado
        if now.weekday() == 0 and now.hour == 9:
            report = generate_weekly_report()
            send_telegram(report)
            last_weekly_report = now
            print("[Reporte] Reporte semanal enviado ✓")


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message: str, reply_markup: dict = None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(url, json=payload, timeout=10)
        if not r.ok:
            print(f"[Telegram Error] {r.text}")
        return r.json() if r.ok else None
    except Exception as e:
        print(f"[Telegram Exception] {e}")
    return None


def send_trade_alert_with_buttons(message: str, trade_data: dict) -> str:
    """Send alert with COPIAR/IGNORAR buttons. Returns callback_id."""
    import hashlib
    callback_id = hashlib.md5(
        f"{trade_data.get('trader')}:{trade_data.get('market_id')}:{time.time()}".encode()
    ).hexdigest()[:12]

    pending_approvals[callback_id] = trade_data

    keyboard = {
        "inline_keyboard": [[
            {"text": f"✅ COPIAR ${trade_data.get('amount', 0):.2f}", "callback_data": f"copy:{callback_id}"},
            {"text": "❌ IGNORAR", "callback_data": f"ignore:{callback_id}"},
        ]]
    }
    # Add EXIT button for sell alerts
    if trade_data.get("is_exit"):
        keyboard = {
            "inline_keyboard": [[
                {"text": "🚨 SALIR AHORA", "callback_data": f"exit:{callback_id}"},
                {"text": "⏳ MANTENER", "callback_data": f"ignore:{callback_id}"},
            ]]
        }

    send_telegram(message, reply_markup=keyboard)
    print(f"[Buttons] Alerta con botones enviada | callback_id: {callback_id}")
    return callback_id


def answer_callback(callback_query_id: str, text: str):
    """Acknowledge button press to remove loading spinner."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=5,
        )
    except Exception:
        pass


def edit_message_reply_markup(chat_id: str, message_id: int, text: str):
    """Update message after button press to show result."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=5,
        )
    except Exception:
        pass


# ============================================================
# POLYMARKET TRADING EXECUTION
# ============================================================

def get_my_usdc_balance() -> float:
    """Get USDC balance of our wallet via Polygon RPC."""
    if not MY_WALLET:
        return 0.0
    try:
        # USDC contract on Polygon
        usdc_contract = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        # balanceOf call
        data = f"0x70a08231000000000000000000000000{MY_WALLET.lower().replace('0x', '')}"
        payload = {
            "jsonrpc": "2.0", "method": "eth_call",
            "params": [{"to": usdc_contract, "data": data}, "latest"],
            "id": 1,
        }
        r = requests.post("https://polygon-rpc.com", json=payload, timeout=10)
        if r.ok:
            result = r.json().get("result", "0x0")
            balance_raw = int(result, 16)
            return balance_raw / 1_000_000  # USDC has 6 decimals
    except Exception as e:
        print(f"[Balance Error] {e}")
    return 0.0


def _get_clob_auth_headers(method: str, path: str, body: str = "") -> dict:
    """Generate authenticated headers for Polymarket CLOB API."""
    import hmac
    import hashlib
    import base64

    timestamp = str(int(time.time()))
    message = timestamp + method.upper() + path + body
    secret_bytes = base64.b64decode(POLY_SECRET) if POLY_SECRET else b""
    signature = hmac.new(secret_bytes, message.encode(), hashlib.sha256).digest()
    sig_b64 = base64.b64encode(signature).decode()

    return {
        "POLY-API-KEY": POLY_API_KEY,
        "POLY-PASSPHRASE": POLY_PASSPHRASE,
        "POLY-TIMESTAMP": timestamp,
        "POLY-SIGNATURE": sig_b64,
        "Content-Type": "application/json",
    }


def execute_polymarket_trade(token_id: str, side: str, amount_usdc: float, price: float) -> dict:
    """
    Execute a trade on Polymarket CLOB.
    token_id: outcome token ID from market
    side: "BUY" or "SELL"
    amount_usdc: USDC to spend
    price: limit price 0.0-1.0
    """
    if not MY_PRIVATE_KEY or not MY_WALLET:
        return {"success": False, "error": "Falta MY_PRIVATE_KEY o MY_WALLET en Railway"}

    if not POLY_API_KEY or not POLY_SECRET or not POLY_PASSPHRASE:
        return {"success": False, "error": "Faltan POLY_API_KEY, POLY_SECRET o POLY_PASSPHRASE en Railway"}

    if not token_id:
        return {"success": False, "error": "No se encontró token_id para este mercado"}

    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct

        # Calculate amounts (USDC has 6 decimals, shares have 6 decimals)
        if side == "BUY":
            # makerAmount = USDC spending, takerAmount = shares receiving
            maker_amount = int(amount_usdc * 1_000_000)
            taker_amount = int((amount_usdc / price) * 1_000_000) if price > 0 else 0
        else:
            # SELL: makerAmount = shares selling, takerAmount = USDC receiving
            maker_amount = int((amount_usdc / price) * 1_000_000) if price > 0 else 0
            taker_amount = int(amount_usdc * 1_000_000)

        order = {
            "salt": int(time.time() * 1000),
            "maker": MY_WALLET,
            "signer": MY_WALLET,
            "taker": "0x0000000000000000000000000000000000000000",
            "tokenId": token_id,
            "makerAmount": str(maker_amount),
            "takerAmount": str(taker_amount),
            "expiration": str(int(time.time()) + 3600),
            "nonce": "0",
            "feeRateBps": "0",
            "side": "0" if side == "BUY" else "1",
            "signatureType": "0",
        }

        # Sign with private key
        account = Account.from_key(MY_PRIVATE_KEY)
        order_str = json.dumps(order, separators=(",", ":"), sort_keys=True)
        msg = encode_defunct(text=order_str)
        signed = account.sign_message(msg)
        order["signature"] = signed.signature.hex()

        # Build authenticated headers
        body = json.dumps({"order": order, "owner": MY_WALLET, "orderType": "GTC"})
        headers = _get_clob_auth_headers("POST", "/order", body)

        r = requests.post(f"{CLOB_API}/order", headers=headers, data=body, timeout=15)

        if r.ok:
            data = r.json()
            order_id = data.get("orderID", data.get("id", "N/A"))
            print(f"[Trade] ✅ Orden ejecutada: {order_id}")
            return {"success": True, "order_id": order_id, "data": data}
        else:
            print(f"[Trade] ❌ Error: {r.status_code} {r.text}")
            return {"success": False, "error": f"{r.status_code}: {r.text[:200]}"}

    except ImportError:
        return {"success": False, "error": "Instalar eth-account: agrega 'eth-account==0.10.0' a requirements.txt"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_token_id_for_market(market_id: str, outcome: str) -> str:
    """Get the outcome token ID needed to execute a trade."""
    try:
        r = requests.get(f"{CLOB_API}/markets/{market_id}", timeout=10)
        if r.ok:
            data = r.json()
            tokens = data.get("tokens", [])
            for t in tokens:
                if t.get("outcome", "").lower() == outcome.lower():
                    return t.get("token_id", "")
    except Exception as e:
        print(f"[TokenID Error] {e}")
    return ""


# ============================================================
# TELEGRAM CALLBACK HANDLER
# ============================================================

last_update_id = 0

def poll_telegram_callbacks():
    """Poll Telegram for button presses and handle them."""
    global last_update_id, pending_approvals
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": last_update_id + 1, "timeout": 1, "allowed_updates": ["callback_query"]},
            timeout=5,
        )
        if not r.ok:
            return
        updates = r.json().get("result", [])
        for update in updates:
            last_update_id = update["update_id"]
            cb = update.get("callback_query")
            if not cb:
                continue

            data = cb.get("data", "")
            cq_id = cb["id"]
            msg = cb.get("message", {})
            chat_id = msg.get("chat", {}).get("id", TELEGRAM_CHAT_ID)
            msg_id = msg.get("message_id")

            parts = data.split(":", 1)
            action = parts[0]
            callback_id = parts[1] if len(parts) > 1 else ""

            trade_data = pending_approvals.get(callback_id)

            if action == "ignore" or not trade_data:
                answer_callback(cq_id, "❌ Ignorado")
                pending_approvals.pop(callback_id, None)
                edit_message_reply_markup(chat_id, msg_id,
                    msg.get("text", "") + "\n\n<i>❌ Ignorado por el usuario</i>")
                print(f"[Callback] Ignorado: {callback_id}")
                continue

            if action in ("copy", "exit"):
                # Safety checks
                balance = get_my_usdc_balance()
                amount = trade_data.get("amount", 0)
                open_pos = len(load_positions())

                if balance < amount:
                    answer_callback(cq_id, f"⚠️ Saldo insuficiente (${balance:.2f} USDC)")
                    send_telegram(f"⚠️ <b>Trade cancelado</b> — Saldo insuficiente\nTienes ${balance:.2f} USDC, necesitas ${amount:.2f}")
                    continue

                if action == "copy" and open_pos >= MAX_OPEN_POSITIONS:
                    answer_callback(cq_id, f"⚠️ Máximo {MAX_OPEN_POSITIONS} posiciones abiertas")
                    send_telegram(f"⚠️ <b>Trade cancelado</b> — Ya tienes {open_pos} posiciones abiertas (máximo: {MAX_OPEN_POSITIONS})")
                    continue

                answer_callback(cq_id, "⏳ Ejecutando orden...")
                send_telegram("⏳ <b>Ejecutando orden en Polymarket...</b>")

                token_id = get_token_id_for_market(
                    trade_data.get("market_id", ""),
                    trade_data.get("outcome", "Yes")
                )
                trade_side = "SELL" if action == "exit" else "BUY"
                result = execute_polymarket_trade(
                    token_id=token_id,
                    side=trade_side,
                    amount_usdc=amount,
                    price=trade_data.get("price", 0.5),
                )

                if result["success"]:
                    answer_callback(cq_id, "✅ Orden ejecutada")
                    success_parts = [
                        f"✅ <b>ORDEN EJECUTADA</b>",
                        f"📋 {trade_data.get('market_title', '')}",
                        f"⚡ {trade_side} {trade_data.get('outcome', '')} @ {trade_data.get('price', 0)*100:.1f}¢",
                        f"💵 Monto: ${amount:.2f} USDC",
                        f"🔑 Order ID: {result.get('order_id', 'N/A')}",
                    ]
                    send_telegram("\n".join(success_parts))
                    edit_message_reply_markup(chat_id, msg_id,
                        msg.get("text", "") + "\n\n<b>✅ Orden ejecutada</b>")
                    print(f"[Trade] Ejecutado: {trade_side} {amount} USDC en {trade_data.get('market_title','')}")
                else:
                    answer_callback(cq_id, "❌ Error al ejecutar")
                    send_telegram(f"❌ <b>Error al ejecutar orden</b>\n<code>{result.get('error','')}</code>")
                    print(f"[Trade Error] {result.get('error','')}")

                pending_approvals.pop(callback_id, None)

    except Exception as e:
        print(f"[Poll Error] {e}")


# ============================================================
# POLYMARKET API
# ============================================================

def get_recent_trades(wallet_address: str) -> list:
    try:
        r = requests.get(f"{DATA_API}/activity", params={"user": wallet_address, "limit": 10}, timeout=10)
        if r.ok:
            return r.json() or []
        r2 = requests.get("https://clob.polymarket.com/data/trades",
                          params={"maker_address": wallet_address, "limit": 10}, timeout=10)
        if r2.ok:
            data = r2.json()
            return data.get("data", []) if isinstance(data, dict) else data
    except Exception as e:
        print(f"[API Error] {wallet_address}: {e}")
    return []


def _parse_market(m: dict, market_id: str) -> dict:
    vol = float(m.get("volume", 0))
    print(f"[Market] Vol: ${vol:,.0f} | {m.get('question','')[:60]}")
    return {
        "volume": vol,
        "description": m.get("description", ""),
        "end_date": m.get("endDate", ""),
        "liquidity": float(m.get("liquidity", 0)),
        "category": m.get("category", ""),
        "slug": m.get("slug", ""),
        "conditionId": m.get("conditionId", market_id),
        "question": m.get("question", ""),
        "outcomes": m.get("outcomePrices", []),
    }


def _title_to_slug(title: str) -> str:
    """Convert a market title to its likely Polymarket slug."""
    import re
    slug = title.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


def _title_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    stopwords = {"will", "the", "a", "an", "be", "to", "of", "in", "by", "on", "at", "is", "or", "and"}
    wa -= stopwords
    wb -= stopwords
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


def _fetch_by_slug(slug: str) -> dict | None:
    """Try to find a market by exact or partial slug match."""
    try:
        r = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
        if r.ok and r.json():
            return r.json()[0]
        # Try truncated slug (remove last segment)
        parts = slug.rsplit("-", 1)
        if len(parts) > 1:
            r2 = requests.get(f"{GAMMA_API}/markets", params={"slug": parts[0]}, timeout=10)
            if r2.ok and r2.json():
                return r2.json()[0]
    except Exception:
        pass
    return None


def _fetch_by_clob(market_id: str) -> dict | None:
    """Fetch market directly from CLOB API by conditionId — most accurate, no collisions."""
    try:
        r = requests.get(f"{CLOB_API}/markets/{market_id}", timeout=10)
        if r.ok:
            data = r.json()
            if data and data.get("condition_id"):
                # CLOB returns different field names — normalize to Gamma format
                return {
                    "conditionId": data.get("condition_id", market_id),
                    "question": data.get("question", ""),
                    "description": data.get("description", ""),
                    "volume": float(data.get("volume", 0) or 0),
                    "liquidity": float(data.get("liquidity", 0) or 0),
                    "endDate": data.get("end_date_iso", data.get("end_date", "")),
                    "category": data.get("category", ""),
                    "slug": data.get("market_slug", ""),
                    "outcomePrices": [t.get("price", "0") for t in data.get("tokens", [])],
                }
    except Exception as e:
        print(f"[CLOB Error] {e}")
    return None


def get_market_info(market_id: str, trade_title: str = "") -> dict:
    """Fetch correct market info — CLOB first, then slug, then keyword search."""
    empty = {"volume": 0, "description": "", "end_date": "", "liquidity": 0,
             "category": "", "slug": "", "conditionId": market_id}
    if not market_id and not trade_title:
        return empty

    candidates = []
    try:
        # STRATEGY 0: CLOB API by conditionId — exact match, no ambiguity
        if market_id:
            clob_data = _fetch_by_clob(market_id)
            if clob_data and clob_data.get("question"):
                sim = _title_similarity(trade_title, clob_data.get("question", "")) if trade_title else 1.0
                if sim >= 0.2 or not trade_title:
                    print(f"[Market] ✓ CLOB match (sim={sim:.2f}) Vol:${float(clob_data.get('volume',0)):,.0f} | {clob_data.get('question','')[:60]}")
                    return _parse_market(clob_data, market_id)
                else:
                    print(f"[Market] CLOB found but low similarity ({sim:.2f}): '{clob_data.get('question','')[:50]}'")

        # STRATEGY 1: If we have a title, build slug and search directly
        if trade_title:
            slug = _title_to_slug(trade_title)
            m = _fetch_by_slug(slug)
            if m:
                sim = _title_similarity(trade_title, m.get("question", ""))
                if sim >= 0.3:
                    print(f"[Market] ✓ Slug match (sim={sim:.2f}) Vol:${float(m.get('volume',0)):,.0f} | {m.get('question','')[:60]}")
                    return _parse_market(m, market_id)
                else:
                    print(f"[Market] Slug found but low similarity {sim:.2f} — trying other methods")

        # STRATEGY 2: conditionId lookup via Gamma — validate result matches title
        if market_id:
            for param in [{"conditionId": market_id}, {"id": market_id}]:
                r = requests.get(f"{GAMMA_API}/markets", params=param, timeout=10)
                if r.ok and r.json():
                    for m in r.json():
                        candidates.append(m)
                    break

        if candidates and trade_title:
            best = max(candidates, key=lambda m: _title_similarity(trade_title, m.get("question", "")))
            sim = _title_similarity(trade_title, best.get("question", ""))
            if sim >= 0.3:
                print(f"[Market] ✓ conditionId match (sim={sim:.2f}) Vol:${float(best.get('volume',0)):,.0f}")
                return _parse_market(best, market_id)
            else:
                print(f"[Market] ⚠️ conditionId result mismatch (sim={sim:.2f}) — trying keyword search")
                # STRATEGY 3: Keyword search — include short words, broader net
                stopwords = {"will", "their", "there", "where", "which", "about", "after",
                             "before", "from", "with", "this", "that", "have", "been"}
                keywords = [w for w in trade_title.split()
                            if len(w) >= 3 and w.lower() not in stopwords][:6]
                query = " ".join(keywords[:4])
                print(f"[Market] Searching by keywords: '{query}'")
                r3 = requests.get(f"{GAMMA_API}/markets", params={"search": query, "limit": 30}, timeout=10)
                if r3.ok and r3.json():
                    search_results = r3.json()
                    best2 = max(search_results, key=lambda m: _title_similarity(trade_title, m.get("question", "")))
                    sim2 = _title_similarity(trade_title, best2.get("question", ""))
                    # Accept any improvement over current best, or any match > 0.1
                    if sim2 > sim or sim2 >= 0.1:
                        print(f"[Market] ✓ Keyword search match (sim={sim2:.2f}) Vol:${float(best2.get('volume',0)):,.0f} | {best2.get('question','')[:60]}")
                        return _parse_market(best2, market_id)

        elif candidates:
            return _parse_market(candidates[0], market_id)

    except Exception as e:
        print(f"[Market Info Error] {e}")
    return empty


def get_sibling_markets(market_id: str) -> list:
    """Busca otros sub-mercados del mismo evento por groupItemTitle o slug."""
    siblings = []
    try:
        # Get base market first — reuse get_market_info which already validates correctly
        base_info = get_market_info(market_id)
        if not base_info.get("slug"):
            return siblings
        m = {"slug": base_info["slug"], "conditionId": market_id}

        slug = m.get("slug", "")
        # Strip trailing date/id suffix to get event base slug
        # e.g. "will-iran-announce-supreme-leader-on-march-8-2026" -> "will-iran-announce-supreme-leader-on"
        import re
        base_slug = re.sub(r"-(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec).*$", "", slug, flags=re.IGNORECASE)
        if base_slug == slug:
            # fallback: remove last 2 dash-segments
            parts = slug.rsplit("-", 2)
            base_slug = parts[0]

        r2 = requests.get(f"{GAMMA_API}/markets", params={"slug": base_slug, "limit": 30}, timeout=10)
        if r2.ok and r2.json():
            for sm in r2.json():
                cid = sm.get("conditionId", "")
                if cid and cid != market_id:
                    op = sm.get("outcomePrices", [])
                    p_yes = float(op[0]) if op else 0
                    siblings.append({
                        "title": sm.get("groupItemTitle", sm.get("question", ""))[:60],
                        "end_date": sm.get("endDate", "")[:10],
                        "volume": float(sm.get("volume", 0)),
                        "liquidity": float(sm.get("liquidity", 0)),
                        "price_yes": p_yes,
                    })
        # Sort by volume desc so best options appear first
        siblings.sort(key=lambda x: x["volume"], reverse=True)
    except Exception as e:
        print(f"[Siblings Error] {e}")
    return siblings


# ============================================================
# ANÁLISIS CON CLAUDE
# ============================================================

def analyze_trade_with_claude(trader_name: str, trade: dict, market_info: dict, siblings: list = None) -> dict:
    if not ANTHROPIC_API_KEY:
        return None

    profile = TRADER_PROFILES.get(trader_name, {})
    side = trade.get("side", trade.get("type", "?")).upper()
    market_title = trade.get("title", trade.get("market", "Desconocido"))
    outcome = trade.get("outcome", trade.get("answer", ""))
    price = float(trade.get("price", trade.get("avgPrice", 0)) or 0)
    amount = float(trade.get("usdcSize", trade.get("size", 0)) or 0)
    mult = round(1/price, 1) if price > 0 else "?"

    # Build siblings section
    siblings_text = ""
    if siblings:
        siblings_text = "\nOTRAS FECHAS DEL MISMO EVENTO:\n"
        for s in siblings[:6]:
            py = s.get("price_yes", 0)
            sm = round(1/py, 1) if py > 0 else "?"
            siblings_text += (
                f"  - {s.get('title','?')}: "
                f"Vol=${s.get('volume',0):,.0f} | "
                f"Yes={py*100:.0f}c ({sm}x) | "
                f"Cierre={s.get('end_date','?')[:10]}\n"
            )
        siblings_text += "Compara fechas y recomienda la mejor opcion riesgo/retorno.\n"

    # Validate end_date — ignore if it's in the past by more than 1 year (likely bad API data)
    raw_end = market_info.get("end_date", "") or ""
    end_date_display = "desconocida"
    try:
        if raw_end:
            ed = datetime.fromisoformat(raw_end.replace("Z", "+00:00").replace("z", "+00:00"))
            ed_naive = ed.replace(tzinfo=None)
            if ed_naive > datetime(2020, 1, 1):
                end_date_display = ed_naive.strftime("%Y-%m-%d")
            else:
                end_date_display = "desconocida (dato API posiblemente incorrecto)"
    except Exception:
        pass

    # Flag near-certain markets (price > 0.95) — usually not worth copying
    near_certain = price > 0.95
    near_certain_note = (
        f"ATENCION: El precio es {price*100:.1f}c, el mercado esta casi resuelto. "
        f"El retorno real es minimo (~{(1-price)*100:.1f}c por dolar). "
        f"Evalua si vale la pena dado el riesgo residual.\n"
    ) if near_certain else ""

    # Check if this trade is in the trader's specialty area
    specialty = profile.get("specialty", "").lower()
    market_lower = market_title.lower()
    specialty_keywords = [w for w in specialty.replace(",", " ").split() if len(w) > 3]
    specialty_match = any(kw in market_lower for kw in specialty_keywords)
    specialty_note = (
        f"IMPORTANTE: Este mercado cae DIRECTAMENTE en la especialidad de {trader_name} "
        f"({profile.get('specialty')}). Su historial en esta area es especialmente relevante.\n"
    ) if specialty_match else ""

    # Max suggested amount scales with trader conviction (amount bet) and bankroll
    max_suggest = min(USER_BANKROLL * 0.15, MAX_PER_TRADE)  # up to 15% of bankroll

    prompt = (
        f"Eres un analista de prediction markets. Tu trabajo es evaluar si copiar este trade.\n"
        f"REGLA CRITICA: Debes recomendar ENTRAR cuando haya señales positivas claras. "
        f"Ser siempre conservador es un ERROR — significa perder oportunidades reales. "
        f"Calibra tu respuesta: aproximadamente 1 de cada 3 trades buenos deberia ser ENTRAR.\n\n"
        f"TRADER: {trader_name}\n"
        f"- Win Rate: {profile.get('win_rate')}% | PnL total: ${profile.get('pnl'):,} | Profit Factor: {profile.get('profit_factor')}x\n"
        f"- Especialidad: {profile.get('specialty')}\n"
        f"- Estilo: {profile.get('style')}\n"
        f"- Meses activo: {profile.get('months_active')}\n"
        f"{specialty_note}"
        f"\nTRADE DETECTADO:\n"
        f"- Mercado: {market_title}\n"
        f"- Posicion: {outcome} | Accion: {side}\n"
        f"- Precio: {price:.3f} ({price*100:.1f}c) | Aposto: ${amount:.2f} | Retorno potencial: {mult}x\n"
        f"{near_certain_note}"
        f"\nDATO DE MERCADO:\n"
        f"- Volumen: ${market_info.get('volume', 0):,.0f} | Liquidez: ${market_info.get('liquidity', 0):,.0f}\n"
        f"- Categoria: {market_info.get('category', '?')} | Cierre: {end_date_display}\n"
        f"- Descripcion: {market_info.get('description', '')[:300]}\n"
        f"{siblings_text}\n"
        f"BANKROLL DEL USUARIO: ${USER_BANKROLL} USD. suggested_amount debe ser entre $0.50 y ${max_suggest:.2f}.\n"
        f"Solo recomienda NO ENTRAR si hay razon concreta (mercado casi resuelto, fuera de especialidad, trader apostando pequeño = sin conviccion).\n"
        f'Responde SOLO con este JSON, sin texto adicional:\n'
        f'{{"recommendation":"ENTRAR"|"NO ENTRAR"|"OBSERVAR","score":<0-100>,"risk_level":"BAJO"|"MEDIO"|"ALTO","suggested_amount":<0.50-{max_suggest:.2f}>,"reasoning":"<max 2 oraciones>","key_factor":"<factor decisivo>","best_date":"<fecha recomendada o null>"}}'
    )

    try:
        resp = requests.post(
            ANTHROPIC_API,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=20,
        )
        if resp.ok:
            text = resp.json()["content"][0]["text"].strip()
            text = text.replace("```json", "").replace("```", "").strip()
            return json.loads(text)
    except Exception as e:
        print(f"[Claude Error] {e}")
    return None


# ============================================================
# FORMATEO
# ============================================================

def format_alert(trader_name: str, trade: dict, market_info: dict, analysis: dict) -> str:
    side = trade.get("side", trade.get("type", "?")).upper()
    market = trade.get("title", trade.get("market", "Mercado desconocido"))
    outcome = trade.get("outcome", trade.get("answer", ""))
    price = float(trade.get("price", trade.get("avgPrice", 0)) or 0)
    amount = float(trade.get("usdcSize", trade.get("size", 0)) or 0)
    volume = market_info.get("volume", 0)
    timestamp = datetime.now().strftime("%H:%M:%S")
    n_trades = trade.get("_n_trades", 1)
    price_range = trade.get("_price_range", None)

    action_emoji = "🟢" if "BUY" in side else "🔴"
    action_text = "COMPRÓ" if "BUY" in side else "VENDIÓ"
    multiplier = round(1 / price, 1) if 0 < price < 1 else "?"

    # Consolidated vs single trade labels
    is_sell = "SELL" in side or "VEND" in side.upper()
    trades_label = f" ({n_trades} transacciones)" if n_trades > 1 else ""
    price_line = (
        f"💵 <b>Precio promedio:</b> {price*100:.1f}¢  |  Rango: {price_range}"
        if n_trades > 1 and price_range
        else f"💵 <b>Precio:</b> {price:.3f} ({price*100:.1f}¢)"
    )
    if is_sell:
        header_label = "SALIDA CONSOLIDADA" if n_trades > 1 else "SALIDA"
    else:
        header_label = "POSICIÓN ACUMULADA" if n_trades > 1 else "NUEVA ENTRADA"

    if analysis:
        rec = analysis.get("recommendation", "OBSERVAR")
        score = analysis.get("score", 0)
        risk = analysis.get("risk_level", "?")
        suggested = analysis.get("suggested_amount", 0)
        reasoning = analysis.get("reasoning", "")
        key_factor = analysis.get("key_factor", "")
        rec_emoji = {"ENTRAR": "✅", "NO ENTRAR": "❌", "OBSERVAR": "👁"}.get(rec, "👁")
        best_date = analysis.get("best_date", None)
        best_date_line = f"\n📅 <b>Mejor fecha:</b> {best_date}" if best_date and best_date != "null" else ""

        analysis_block = (
            f"\n━━━━━━━━━━━━━━━━━━━━\n"
            f"🧠 <b>ANÁLISIS IA</b>\n"
            f"{rec_emoji} <b>Veredicto: {rec}</b>\n"
            f"📊 Score: {score}/100  |  Riesgo: {risk}\n"
            f"💡 {reasoning}\n"
            f"🔑 <b>Factor clave:</b> {key_factor}"
            f"{best_date_line}\n"
            f"💰 <b>Monto sugerido:</b> ${suggested:.2f} de tus $25\n"
            f"📁 <i>Guardado en tracker para medir efectividad</i>"
        )
    else:
        analysis_block = "\n━━━━━━━━━━━━━━━━━━━━\n⚠️ Análisis IA no disponible"

    return (
        f"{action_emoji} <b>{header_label} — {trader_name}</b>{trades_label}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>Mercado:</b> {market}\n"
        f"🎯 <b>Posición:</b> {outcome}\n"
        f"⚡ <b>Acción:</b> {action_text}\n"
        f"{price_line}\n"
        f"💼 <b>Total invertido:</b> ${amount:.2f} USDC\n"
        f"📈 <b>Retorno potencial:</b> {multiplier}x\n"
        f"🌊 <b>Volumen mercado:</b> ${volume:,.0f}"
        f"{analysis_block}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {timestamp} | 🔗 <a href='https://polymarket.com'>Ver en Polymarket</a>"
    )


# ============================================================
# LOOP PRINCIPAL
# ============================================================

def flush_pending(key: str):
    """Consolida todos los trades acumulados de un (trader, market) y envía UNA alerta."""
    global pending_trades
    if key not in pending_trades:
        return

    entry = pending_trades.pop(key)
    trader_name = entry["trader_name"]
    trades_list = entry["trades"]
    market_info = entry["market_info"]
    market_id = entry["market_id"]

    if not trades_list:
        return

    # If market_info came back empty (volume=0), retry lookup now — API may have recovered
    if market_info.get("volume", 0) == 0 and market_id:
        ref_title = trades_list[0].get("title", trades_list[0].get("market", ""))
        print(f"[Buffer] market_info vacío — reintentando lookup para {market_id[:20]}...")
        market_info = get_market_info(market_id, ref_title)

    # ── Sumarizaciones ──────────────────────────────────────────
    total_amount = sum(float(t.get("usdcSize", t.get("size", 0)) or 0) for t in trades_list)
    prices = [float(t.get("price", t.get("avgPrice", 0)) or 0) for t in trades_list]
    prices = [p for p in prices if p > 0]
    avg_price = sum(prices) / len(prices) if prices else 0
    min_price = min(prices) if prices else 0
    max_price = max(prices) if prices else 0
    n = len(trades_list)

    # Use first trade for metadata
    ref = trades_list[0]
    side = ref.get("side", ref.get("type", "?")).upper()
    market_title = ref.get("title", ref.get("market", "Desconocido"))
    outcome = ref.get("outcome", ref.get("answer", ""))
    mult = round(1 / avg_price, 1) if avg_price > 0 else "?"

    # Build a synthetic consolidated trade for Claude
    consolidated = dict(ref)
    consolidated["usdcSize"] = total_amount
    consolidated["price"] = avg_price
    consolidated["_n_trades"] = n
    consolidated["_price_range"] = f"{min_price*100:.1f}¢ – {max_price*100:.1f}¢" if n > 1 else None

    # Check volume filter
    low_volume = 0 < market_info["volume"] < MIN_VOLUME
    if low_volume:
        ae = "🟢" if "BUY" in side else "🔴"
        at = "COMPRÓ" if "BUY" in side else "VENDIÓ"
        vol = market_info["volume"]
        parts = [
            f"⚠️ <b>VOLUMEN BAJO — {trader_name}</b> (info only, no copiar)",
            "━━━━━━━━━━━━━━━━━━━━",
            f"{ae} <b>Acción:</b> {at} ({n} transacciones)",
            f"📋 <b>Mercado:</b> {market_title}",
            f"🎯 <b>Posición:</b> {outcome}",
            f"💵 <b>Precio promedio:</b> {avg_price*100:.1f}¢  |  Rango: {min_price*100:.1f}¢–{max_price*100:.1f}¢" if n > 1 else f"💵 <b>Precio:</b> {avg_price*100:.1f}¢",
            f"💼 <b>Total apostado:</b> ${total_amount:.2f} USDC",
            f"📈 <b>Retorno potencial:</b> {mult}x",
            f"🌊 <b>Volumen mercado:</b> ${vol:,.0f} (mínimo: ${MIN_VOLUME:,})",
            "━━━━━━━━━━━━━━━━━━━━",
            "ℹ️ <i>Mercado pequeño — observar, no copiar</i>",
        ]
        send_telegram("\n".join(parts))
        print(f"[{trader_name}] Alerta volumen bajo consolidada ({n} trades) ✓")
        return

    is_sell = "SELL" in side.upper()

    if is_sell:
        # Check if we have a recorded entry for this position
        entry = get_entry_for_exit(trader_name, market_id, outcome)
        if entry:
            entry_price = entry["avg_price"]
            entry_amount = entry["total_amount"]
            pnl_pct = (avg_price - entry_price) / entry_price * 100
            pnl_usd = entry_amount * (avg_price - entry_price) / entry_price
            multiplier = round(avg_price / entry_price, 2)
            hold_time = ""
            try:
                entry_dt = datetime.fromisoformat(entry["entry_time"])
                delta = datetime.now() - entry_dt
                hours = int(delta.total_seconds() // 3600)
                days = delta.days
                hold_time = f"{days}d {hours % 24}h" if days > 0 else f"{hours}h"
            except Exception:
                pass

            emoji = "🟩" if pnl_usd >= 0 else "🟥"
            pnl_sign = "+" if pnl_usd >= 0 else ""
            exit_parts = [
                f"🚨 <b>SALIDA CON GANANCIA — {trader_name}</b> ({n} transacciones)",
                "━━━━━━━━━━━━━━━━━━━━",
                f"📋 <b>Mercado:</b> {market_title}",
                f"🎯 <b>Posición:</b> {outcome}",
                "━━━━━━━━━━━━━━━━━━━━",
                f"📥 <b>Entrada:</b> {entry_price*100:.1f}¢  |  Invertido: ${entry_amount:.2f}",
                f"📤 <b>Salida:</b> {avg_price*100:.1f}¢  |  Recuperado: ${entry_amount*multiplier:.2f}",
                "━━━━━━━━━━━━━━━━━━━━",
                f"{emoji} <b>PnL: {pnl_sign}{pnl_usd:.2f} USDC ({pnl_sign}{pnl_pct:.1f}%)</b>",
                f"📈 <b>Multiplicador:</b> {multiplier}x",
                f"⏱ <b>Tiempo en posición:</b> {hold_time}",
                "━━━━━━━━━━━━━━━━━━━━",
                "⚠️ <b>Si copiaste esta entrada: considera salir ahora</b>",
                f"🌊 Volumen mercado: ${market_info.get('volume', 0):,.0f}",
                f"⏰ {datetime.now().strftime('%H:%M:%S')}",
            ]
            exit_msg = "\n".join(exit_parts)
            if MY_PRIVATE_KEY and MY_WALLET:
                exit_trade_data = {
                    "trader": trader_name,
                    "market_id": market_id,
                    "market_title": market_title,
                    "outcome": outcome,
                    "price": avg_price,
                    "amount": entry_amount,
                    "is_exit": True,
                }
                send_trade_alert_with_buttons(exit_msg, exit_trade_data)
            else:
                send_telegram(exit_msg)
            close_position(trader_name, market_id, outcome)
            print(f"[{trader_name}] Alerta de salida con PnL enviada ✓ | {pnl_pct:+.1f}%")
        else:
            # No recorded entry — send basic exit alert
            message = format_alert(trader_name, consolidated, market_info, None)
            send_telegram(message)
            print(f"[{trader_name}] Salida sin entrada registrada — alerta básica enviada")
        return

    # BUY — record position and send entry alert with buttons
    record_entry(trader_name, market_id, outcome, avg_price, total_amount, market_title)

    print(f"[{trader_name}] Analizando posición consolidada ({n} trades, ${total_amount:.0f} total)...")
    siblings = get_sibling_markets(market_id)
    if siblings:
        print(f"[{trader_name}] {len(siblings)} sub-mercados encontrados")
    analysis = analyze_trade_with_claude(trader_name, consolidated, market_info, siblings)

    if analysis:
        log_trade(
            trader=trader_name,
            market=market_title,
            outcome=outcome,
            price=avg_price,
            recommendation=analysis.get("recommendation", "OBSERVAR"),
            score=analysis.get("score", 0),
            suggested_amount=analysis.get("suggested_amount", 0),
            market_id=market_id,
        )

    message = format_alert(trader_name, consolidated, market_info, analysis)

    # Decide amount for button
    copy_amount = round(min(
        analysis.get("suggested_amount", 1.0) if analysis else 1.0,
        MAX_PER_TRADE,
        USER_BANKROLL * 0.07,
    ), 2)
    copy_amount = max(copy_amount, 0.50)  # minimum $0.50

    rec = analysis.get("recommendation", "OBSERVAR") if analysis else "OBSERVAR"

    if MY_PRIVATE_KEY and MY_WALLET:
        # Send with buttons
        trade_data = {
            "trader": trader_name,
            "market_id": market_id,
            "market_title": market_title,
            "outcome": outcome,
            "price": avg_price,
            "amount": copy_amount,
            "is_exit": False,
        }
        # Auto-execute if AUTO_TRADE is on and IA says ENTRAR
        if AUTO_TRADE and rec == "ENTRAR":
            send_telegram(message)
            balance = get_my_usdc_balance()
            if balance >= copy_amount:
                token_id = get_token_id_for_market(market_id, outcome)
                result = execute_polymarket_trade(token_id, "BUY", copy_amount, avg_price)
                if result["success"]:
                    send_telegram(f"🤖 <b>AUTO-TRADE ejecutado</b>\n${copy_amount:.2f} USDC @ {avg_price*100:.1f}¢\nOrder: {result.get('order_id','')}")
                else:
                    send_telegram(f"❌ <b>AUTO-TRADE fallido</b>\n{result.get('error','')}")
            else:
                send_telegram(f"⚠️ Saldo insuficiente para auto-trade (${balance:.2f} disponible)")
        else:
            send_trade_alert_with_buttons(message, trade_data)
    else:
        # No wallet configured — plain alert
        send_telegram(message)

    verdict = rec
    print(f"[{trader_name}] Alerta enviada ✓ | Veredicto: {verdict}")


def buffer_trade(trader_name: str, trade: dict, market_info: dict, market_id: str):
    """Agrega un trade al buffer. Agrupa por trader+mercado+side (buy/sell separados)."""
    global pending_trades
    side = trade.get("side", trade.get("type", "?")).upper()
    direction = "BUY" if "BUY" in side else "SELL"
    key = f"{trader_name}:{market_id}:{direction}"
    if key not in pending_trades:
        pending_trades[key] = {
            "trader_name": trader_name,
            "market_id": market_id,
            "market_info": market_info,
            "trades": [],
            "first_seen": time.time(),
        }
        print(f"[Buffer] Nueva posición abierta: {key}")
    else:
        # Update market_info with latest (may have better volume data)
        if market_info.get("volume", 0) > pending_trades[key]["market_info"].get("volume", 0):
            pending_trades[key]["market_info"] = market_info
        print(f"[Buffer] Trade acumulado en: {key} ({len(pending_trades[key]['trades'])+1} total)")
    pending_trades[key]["trades"].append(trade)


def process_trade(trader_name: str, trade: dict):
    """Recibe un trade nuevo y lo agrega al buffer de agrupación."""
    market_id = trade.get("market", trade.get("conditionId", ""))
    trade_title = trade.get("title", trade.get("market", ""))

    # Skip trades with no market identifier — nothing useful we can do with them
    if not market_id:
        print(f"[{trader_name}] Trade ignorado — sin market_id")
        return

    market_info = get_market_info(market_id, trade_title)
    buffer_trade(trader_name, trade, market_info, market_id)


def flush_stale_pending():
    """Revisa el buffer y envía alertas de grupos cuya ventana de tiempo ya venció."""
    global pending_trades
    now = time.time()
    to_flush = [
        key for key, entry in pending_trades.items()
        if now - entry["first_seen"] >= GROUPING_WINDOW
    ]
    for key in to_flush:
        print(f"[Buffer] Ventana cerrada — enviando alerta consolidada: {key}")
        flush_pending(key)
def check_wallet(trader_name: str, wallet_address: str):
    global last_seen

    if not wallet_address:
        return

    # Fetch last 10 trades to catch bursts of simultaneous entries
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
        # Find all new trades since last seen
        new_trades = []
        for t in trades:
            tid = t.get("id", t.get("transactionHash", str(t)))
            if tid == last_seen[trader_name]:
                break
            new_trades.append(t)

        # Update last seen immediately
        last_seen[trader_name] = trade_id
        print(f"[{trader_name}] {len(new_trades)} trade(s) nuevo(s) detectado(s)")

        # Send ALL new trades to the buffer — buffer handles grouping by market
        for trade in reversed(new_trades):  # oldest first
            try:
                process_trade(trader_name, trade)
            except Exception as e:
                print(f"[Process Error] {trader_name}: {e}")




def run_dashboard_server():
    """Serve the analytics dashboard on PORT (default 8080)."""
    import http.server
    import urllib.parse

    dashboard_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
    port = int(os.getenv("PORT", "8080"))

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # suppress access logs

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path

            if path == "/api/tracker":
                # Serve tracker JSON for the dashboard
                try:
                    tracker = load_tracker()
                    body = json.dumps(tracker).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as e:
                    self.send_error(500, str(e))

            elif path == "/api/positions":
                # Serve open positions
                try:
                    positions = load_positions()
                    body = json.dumps(positions).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as e:
                    self.send_error(500, str(e))

            elif path == "/" or path == "/dashboard":
                # Serve the HTML dashboard
                try:
                    if os.path.exists(dashboard_path):
                        with open(dashboard_path, "rb") as f:
                            body = f.read()
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    else:
                        # Fallback: simple status page if dashboard.html not found
                        body = b"<h1>Bot running. dashboard.html not found.</h1><p>Add dashboard.html to your repo.</p><p><a href='/api/tracker'>View raw tracker data</a></p>"
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html")
                        self.end_headers()
                        self.wfile.write(body)
                except Exception as e:
                    self.send_error(500, str(e))
            else:
                self.send_error(404)

    server = http.server.HTTPServer(("0.0.0.0", port), Handler)
    print(f"[Dashboard] Servidor iniciado en puerto {port}")
    server.serve_forever()


def main():
    print("=" * 50)
    print("🤖 Polymarket Copy Alert Bot v3 — con Tracker")
    print(f"   Monitoreando: {', '.join(WALLETS.keys())}")
    print(f"   Intervalo: {CHECK_INTERVAL}s | Vol mínimo: ${MIN_VOLUME:,}")
    print(f"   Análisis IA: {'✓ Activo' if ANTHROPIC_API_KEY else '✗ Falta ANTHROPIC_API_KEY'}")
    print(f"   Reporte: todos los lunes 9am")
    print("=" * 50)

    # Start dashboard server in background thread
    import threading
    dashboard_thread = threading.Thread(target=run_dashboard_server, daemon=True)
    dashboard_thread.start()

    send_telegram(
        "🤖 <b>Bot v3 iniciado — con Tracker de Efectividad</b>\n"
        f"Monitoreando: {', '.join(WALLETS.keys())}\n"
        f"✅ Cada decisión queda registrada\n"
        f"📊 Reporte semanal automático los lunes\n"
        f"{'✅ Análisis IA activo' if ANTHROPIC_API_KEY else '⚠️ Agrega ANTHROPIC_API_KEY en Railway'}"
    )

    cycle = 0
    while True:
        # Cada 10 ciclos revisa si hay trades que resolvieron
        if cycle % 10 == 0:
            check_pending_resolutions()

        # Revisar si toca reporte semanal
        maybe_send_weekly_report()

        for name, wallet in WALLETS.items():
            try:
                check_wallet(name, wallet)
            except Exception as e:
                print(f"[Error] {name}: {e}")

        # Flush any pending trade groups whose window has expired
        flush_stale_pending()

        # Poll Telegram for button presses (every cycle)
        poll_telegram_callbacks()

        cycle += 1
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
