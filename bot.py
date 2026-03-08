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

# APIs
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"

last_seen = {wallet: None for wallet in WALLETS}
last_weekly_report = None


# ============================================================
# TRACKER — guarda cada decisión en disco
# ============================================================

def load_tracker() -> dict:
    """Carga el historial de trades del disco."""
    try:
        if os.path.exists(TRACKER_FILE):
            with open(TRACKER_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {"trades": [], "stats": {"total": 0, "entrar": 0, "no_entrar": 0, "observar": 0}}


def save_tracker(data: dict):
    """Guarda el historial al disco."""
    try:
        with open(TRACKER_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[Tracker Error] {e}")


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
        r2 = requests.get("https://clob.polymarket.com/data/trades",
                          params={"maker_address": wallet_address, "limit": 5}, timeout=10)
        if r2.ok:
            data = r2.json()
            return data.get("data", []) if isinstance(data, dict) else data
    except Exception as e:
        print(f"[API Error] {wallet_address}: {e}")
    return []


def get_market_info(market_id: str) -> dict:
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
                "slug": m.get("slug", ""),
                "groupItemTitle": m.get("groupItemTitle", ""),
                "conditionId": m.get("conditionId", market_id),
            }
    except Exception as e:
        print(f"[Market Info Error] {e}")
    return {"volume": 0, "description": "", "end_date": "", "liquidity": 0, "category": "", "slug": "", "groupItemTitle": "", "conditionId": ""}


def get_sibling_markets(market_id: str) -> list:
    """Busca otros sub-mercados del mismo evento (mismo slug base / grupo)."""
    siblings = []
    try:
        # Primero obtener el mercado base para encontrar su slug
        r = requests.get(f"{GAMMA_API}/markets", params={"id": market_id}, timeout=10)
        if not r.ok or not r.json():
            return siblings
        m = r.json()[0]
        slug = m.get("slug", "")
        # El slug de sub-mercados suele ser: "us-forces-enter-iran-by-march-14-337"
        # El evento padre suele estar en: "us-forces-enter-iran-by"
        # Buscamos por el slug base quitando la última parte
        parts = slug.rsplit("-", 2)
        base_slug = parts[0] if len(parts) > 1 else slug

        r2 = requests.get(f"{GAMMA_API}/markets", params={"slug": base_slug, "limit": 20}, timeout=10)
        if r2.ok and r2.json():
            for sm in r2.json():
                if sm.get("conditionId") != market_id:
                    siblings.append({
                        "title": sm.get("groupItemTitle", sm.get("question", "")),
                        "end_date": sm.get("endDate", ""),
                        "volume": float(sm.get("volume", 0)),
                        "liquidity": float(sm.get("liquidity", 0)),
                        "price_yes": float(sm.get("outcomePrices", ["0"])[0]) if sm.get("outcomePrices") else 0,
                        "price_no": float(sm.get("outcomePrices", ["0", "0"])[1]) if sm.get("outcomePrices") and len(sm.get("outcomePrices", [])) > 1 else 0,
                    })
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

    prompt = (
        "Eres un analista experto en prediction markets en Polymarket.\n"
        "Evalua si un usuario con $25 USD deberia copiar este trade.\n\n"
        f"TRADER: {trader_name}\n"
        f"- Win Rate: {profile.get('win_rate')}% | PnL: ${profile.get('pnl'):,} | PF: {profile.get('profit_factor')}x\n"
        f"- Especialidad: {profile.get('specialty')}\n"
        f"- Estilo: {profile.get('style')}\n\n"
        f"TRADE DETECTADO:\n"
        f"- Mercado: {market_title}\n"
        f"- Posicion: {outcome} | Accion: {side}\n"
        f"- Precio: {price:.3f} ({price*100:.1f}c) | Aposto: ${amount:.2f} | Retorno: {mult}x\n\n"
        f"MERCADO:\n"
        f"- Volumen: ${market_info.get('volume', 0):,.0f} | Liquidez: ${market_info.get('liquidity', 0):,.0f}\n"
        f"- Categoria: {market_info.get('category', '?')} | Cierre: {market_info.get('end_date', '?')[:10]}\n"
        f"- Descripcion: {market_info.get('description', '')[:200]}\n"
        f"{siblings_text}\n"
        'Responde SOLO con este JSON, sin texto adicional:\n'
        '{"recommendation":"ENTRAR"|"NO ENTRAR"|"OBSERVAR","score":<0-100>,"risk_level":"BAJO"|"MEDIO"|"ALTO","suggested_amount":<0.0-1.50>,"reasoning":"<max 2 oraciones>","key_factor":"<factor decisivo>","best_date":"<fecha recomendada o null>"}'
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

    action_emoji = "🟢" if "BUY" in side else "🔴"
    action_text = "COMPRÓ" if "BUY" in side else "VENDIÓ"
    multiplier = round(1 / price, 1) if 0 < price < 1 else "?"

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

        low_volume = 0 < market_info["volume"] < MIN_VOLUME

        if low_volume:
            side = latest.get("side", latest.get("type", "?")).upper()
            mkt = latest.get("title", latest.get("market", "Desconocido"))
            outcome = latest.get("outcome", latest.get("answer", ""))
            price = float(latest.get("price", latest.get("avgPrice", 0)) or 0)
            amount = float(latest.get("usdcSize", latest.get("size", 0)) or 0)
            ae = "🟢" if "BUY" in side else "🔴"
            at = "COMPRÓ" if "BUY" in side else "VENDIÓ"
            mult = round(1 / price, 1) if 0 < price < 1 else "?"
            vol = market_info["volume"]
            parts = [
                f"⚠️ <b>VOLUMEN BAJO — {trader_name}</b> (info only, no copiar)",
                "━━━━━━━━━━━━━━━━━━━━",
                f"{ae} <b>Acción:</b> {at}",
                f"📋 <b>Mercado:</b> {mkt}",
                f"🎯 <b>Posición:</b> {outcome}",
                f"💵 <b>Precio:</b> {price:.3f} ({price*100:.1f}¢)",
                f"💼 <b>Apostó:</b> ${amount:.2f} USDC",
                f"📈 <b>Retorno potencial:</b> {mult}x",
                f"🌊 <b>Volumen:</b> ${vol:,.0f} (mínimo recomendado: ${MIN_VOLUME:,})",
                "━━━━━━━━━━━━━━━━━━━━",
                "ℹ️ <i>Mercado pequeño — observar, no copiar</i>",
            ]
            send_telegram("\n".join(parts))
            print(f"[{trader_name}] Alerta volumen bajo enviada ✓")
            return

        print(f"[{trader_name}] Consultando análisis IA...")
        siblings = get_sibling_markets(market_id)
        if siblings:
            print(f"[{trader_name}] Encontrados {len(siblings)} sub-mercados")
        analysis = analyze_trade_with_claude(trader_name, latest, market_info, siblings)

        # Guardar en tracker
        if analysis:
            price = float(latest.get("price", latest.get("avgPrice", 0)) or 0)
            log_trade(
                trader=trader_name,
                market=latest.get("title", latest.get("market", "?")),
                outcome=latest.get("outcome", latest.get("answer", "")),
                price=price,
                recommendation=analysis.get("recommendation", "OBSERVAR"),
                score=analysis.get("score", 0),
                suggested_amount=analysis.get("suggested_amount", 0),
                market_id=market_id,
            )

        message = format_alert(trader_name, latest, market_info, analysis)
        send_telegram(message)

        verdict = analysis.get("recommendation", "N/A") if analysis else "Sin análisis"
        print(f"[{trader_name}] Alerta enviada ✓ | Veredicto: {verdict}")


def main():
    print("=" * 50)
    print("🤖 Polymarket Copy Alert Bot v3 — con Tracker")
    print(f"   Monitoreando: {', '.join(WALLETS.keys())}")
    print(f"   Intervalo: {CHECK_INTERVAL}s | Vol mínimo: ${MIN_VOLUME:,}")
    print(f"   Análisis IA: {'✓ Activo' if ANTHROPIC_API_KEY else '✗ Falta ANTHROPIC_API_KEY'}")
    print(f"   Reporte: todos los lunes 9am")
    print("=" * 50)

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

        cycle += 1
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
