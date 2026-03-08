# 🤖 Polymarket Copy Alert Bot

Bot que monitorea las wallets de Gohst y de5nuts en Polymarket y te manda una notificación a Telegram cada vez que entran a una posición nueva.

---

## Paso 1 — Crear tu bot de Telegram

1. Abre Telegram y busca **@BotFather**
2. Escribe `/newbot`
3. Ponle un nombre, ej: `Mi Polymarket Bot`
4. BotFather te va a dar un **token** — guárdalo, se ve así:
   ```
   7123456789:AAHdqTJRTlSBkL3Pdo7VjGk9sXkKxYPmTAI
   ```

5. Ahora busca **@userinfobot** en Telegram
6. Escríbele `/start` — te responde con tu **Chat ID**, se ve así:
   ```
   Id: 123456789
   ```

---

## Paso 2 — Obtener las wallet addresses

1. Ve a **polymarketanalytics.com**
2. Busca "Gohst" — haz click en su perfil
3. La URL se ve así: `polymarketanalytics.com/traders/0x2d4bf8f...`
4. Esa parte después de `/traders/` es la wallet address
5. Repite para de5nuts

---

## Paso 3 — Subir a Railway

1. Crea cuenta en **railway.app** (gratis)
2. Click en **"New Project"** → **"Deploy from GitHub"**
   - O usa **"Empty Project"** y sube los archivos manualmente
3. Sube los 3 archivos: `bot.py`, `requirements.txt`, `railway.toml`

---

## Paso 4 — Configurar las variables de entorno en Railway

En tu proyecto de Railway, ve a **Variables** y agrega estas:

| Variable | Valor |
|---|---|
| `TELEGRAM_TOKEN` | El token que te dio BotFather |
| `TELEGRAM_CHAT_ID` | Tu Chat ID de @userinfobot |
| `WALLET_GOHST` | `0x2d4bf8f846bf68f43b9157bf30810d334ac6ca7a` |
| `WALLET_DE5NUTS` | La wallet address de de5nuts |
| `CHECK_INTERVAL` | `120` (chequea cada 2 minutos) |
| `MIN_VOLUME` | `50000` (ignora mercados con menos de $50K) |

---

## Paso 5 — Deploy

1. Click en **Deploy** en Railway
2. Ve a los **Logs** — deberías ver:
   ```
   🤖 Polymarket Copy Alert Bot
   Monitoreando: Gohst, de5nuts
   ```
3. En Telegram te va a llegar: `"Bot iniciado"`

---

## ¿Qué te manda el bot?

Cuando Gohst o de5nuts entran a una posición nueva, te llega esto:

```
🟢 NUEVA ENTRADA — Gohst
━━━━━━━━━━━━━━━━━━━━
📋 Mercado: Will US strike Iran by June 30?
🎯 Posición: Yes
⚡ Acción: COMPRÓ
💵 Precio: 0.09¢
💼 Monto: $500 USDC
💰 Retorno potencial: 11.1x si resuelve YES
⏰ Hora: 14:32:15
━━━━━━━━━━━━━━━━━━━━
🔗 Ver en Polymarket
```

---

## Antes de copiar una entrada — checklist rápido

✅ ¿El mercado tiene más de $50,000 de volumen?
✅ ¿El precio no subió más de 20¢ desde su entrada?
✅ ¿Entra máximo $1.50–$2 por posición (5-7% de tu bankroll)?
✅ ¿Tienes menos de 3 posiciones abiertas simultáneas?

Si pasan los 4 filtros → entra.

---

## Costos

- Railway: **gratis** (plan hobby tiene $5 de crédito mensual, el bot consume ~$0.50/mes)
- Telegram: **gratis**
- Polymarket API: **gratis y pública**
