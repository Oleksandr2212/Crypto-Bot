import os
import logging
from typing import List, Dict, Any

import ccxt.async_support as ccxt
import aiohttp
import feedparser
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ------- Конфіг -------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("Please set TELEGRAM_TOKEN environment variable")

EXCHANGES = ["binance", "kucoin", "kraken", "okx"]
PER_EXCHANGE = 15
FALLBACK_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT",
    "SOL/USDT", "DOGE/USDT", "MATIC/USDT", "DOT/USDT", "AVAX/USDT",
    "TRX/USDT", "UNI/USDT", "LINK/USDT", "LTC/USDT", "BCH/USDT"
]

RSS_FEEDS = {
    "Cointelegraph": "https://cointelegraph.com/rss",
    "NewsNow Crypto": "https://www.newsnow.com/us/Business/Cryptocurrencies/rss",
    "Yahoo Crypto": "https://finance.yahoo.com/topic/crypto/rss",
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------- Допоміжні функції -------
async def create_exchange_clients(exchange_ids: List[str]) -> Dict[str, ccxt.Exchange]:
    clients: Dict[str, ccxt.Exchange] = {}
    for ex_id in exchange_ids:
        try:
            ex_class = getattr(ccxt, ex_id)
            ex = ex_class({"enableRateLimit": True})
            clients[ex_id] = ex
        except Exception as e:
            logger.warning("Could not create client for %s: %s", ex_id, e)
    return clients

async def close_exchange_clients(clients: Dict[str, ccxt.Exchange]) -> None:
    for ex in clients.values():
        try:
            await ex.close()
        except Exception:
            pass

async def fetch_top_tickers_for_exchange(ex: ccxt.Exchange, per_exchange: int) -> List[Dict[str, Any]]:
    try:
        tickers = await ex.fetch_tickers()
        items = []
        for symbol, t in tickers.items():
            try:
                last = t.get("last")
                change = t.get("percentage")
                vol = t.get("quoteVolume") or t.get("baseVolume") or 0
                items.append({"symbol": symbol, "last": last, "change_pct": change, "volume": vol})
            except Exception:
                continue

        usdt = [x for x in items if "/USDT" in x["symbol"]]
        if len(usdt) >= per_exchange:
            candidates = sorted(usdt, key=lambda x: (x["volume"] or 0), reverse=True)[:per_exchange]
        else:
            candidates = sorted(items, key=lambda x: (x["volume"] or 0), reverse=True)[:per_exchange]

        if not candidates:
            # fallback - try fetching fixed list
            for sym in FALLBACK_SYMBOLS[:per_exchange]:
                try:
                    t = await ex.fetch_ticker(sym)
                    candidates.append({
                        "symbol": sym,
                        "last": t.get("last"),
                        "change_pct": t.get("percentage"),
                        "volume": t.get("quoteVolume") or t.get("baseVolume"),
                    })
                except Exception:
                    continue
        return candidates
    except Exception as e:
        logger.warning("Error fetching tickers for %s: %s", getattr(ex, "id", str(ex)), e)
        results = []
        for sym in FALLBACK_SYMBOLS[:per_exchange]:
            try:
                t = await ex.fetch_ticker(sym)
                results.append({
                    "symbol": sym,
                    "last": t.get("last"),
                    "change_pct": t.get("percentage"),
                    "volume": t.get("quoteVolume") or t.get("baseVolume"),
                })
            except Exception:
                continue
        return results

def format_price(p):
    if p is None:
        return "n/a"
    try:
        if p >= 1:
            return f"{p:,.2f}"
        else:
            return f"{p:.8f}".rstrip("0").rstrip(".")
    except Exception:
        return str(p)

def format_change(ch):
    if ch is None:
        return "n/a"
    sign = "+" if ch >= 0 else ""
    return f"{sign}{ch:.2f}%"

# ------- Telegram handlers -------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Ціни (Prices)", callback_data="prices")],
        [InlineKeyboardButton("Аналітика (Analytics)", callback_data="analytics")],
        [InlineKeyboardButton("Новини (News)", callback_data="news")],
        [InlineKeyboardButton("BTC price (команда /price)", callback_data="cmd_price")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Вітаю! Ось меню бота крипто-інформації. Виберіть дію:", reply_markup=reply_markup)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data == "prices":
        await query.edit_message_text("Завантажую ціни...")
        await handle_prices(query)
    elif data == "analytics":
        await query.edit_message_text("Генерую аналітику...")
        await handle_analytics(query)
    elif data == "news":
        await query.edit_message_text("Отримую останні новини...")
        await handle_news(query)
    elif data == "cmd_price":
        # викликаємо ту саму логіку, що й команда /price
        # тут просто повідомлення про очікування, потім виклик
        await query.edit_message_text("Отримую поточну ціну BTC/USDT...")
        # створимо тимчасове повідомлення з ціною
        # викликаємо price handler-подібно:
        class Dummy:
            async def reply_text(self, t): pass
        # замість складного – просто викликаємо price через контекст команд
        await price_from_exchange_and_reply(update, context)
    else:
        await query.edit_message_text("Невідома дія")

# Команда /price (повертає BTC/USDT з Binance)
async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await price_from_exchange_and_reply(update, context)

async def price_from_exchange_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        ex = ccxt.binance({"enableRateLimit": True})
        try:
            ticker = await ex.fetch_ticker("BTC/USDT")
            last = ticker.get("last")
            await update.effective_message.reply_text(f"💰 Поточна ціна BTC/USDT: {format_price(last)} $")
        finally:
            try:
                await ex.close()
            except Exception:
                pass
    except Exception as e:
        logger.exception("Price fetch error")
        await update.effective_message.reply_text(f"⚠️ Помилка отримання ціни: {e}")

# Обробник кнопки "Prices" — повертає топ символів по кожній біржі
async def handle_prices(query):
    clients = await create_exchange_clients(EXCHANGES)
    try:
        tasks = [fetch_top_tickers_for_exchange(c, PER_EXCHANGE) for c in clients.values()]
        results = await __import__("asyncio").gather(*tasks, return_exceptions=True)

        parts = []
        ex_keys = list(clients.keys())
        for idx, ex_id in enumerate(ex_keys):
            res = results[idx]
            if isinstance(res, Exception):
                parts.append(f"{ex_id.upper()}: error fetching\n")
                continue
            lines = [f"{ex_id.upper()}:"]
            for t in res:
                sym = t.get("symbol")
                last = format_price(t.get("last"))
                ch = format_change(t.get("change_pct"))
                lines.append(f"{sym}: {last} ({ch})")
            parts.append("\n".join(lines))

        final = "\n\n".join(parts)
        if len(final) > 3800:
            final = final[:3800] + "\n\n...truncated..."
        await query.edit_message_text(final)
    except Exception as e:
        logger.exception("handle_prices error")
        await query.edit_message_text(f"⚠️ Помилка при отриманні цін: {e}")
    finally:
        await close_exchange_clients(clients)

# Аналітика (текстова)
async def handle_analytics(query):
    clients = await create_exchange_clients(EXCHANGES)
    try:
        tasks = [fetch_top_tickers_for_exchange(c, PER_EXCHANGE) for c in clients.values()]
        results = await __import__("asyncio").gather(*tasks, return_exceptions=True)

        aggregate = {}
        ex_names = list(clients.keys())
        for ex_idx, res in enumerate(results):
            ex_id = ex_names[ex_idx]
            if isinstance(res, Exception):
                continue
            for t in res:
                sym = t.get("symbol")
                entry = {"exchange": ex_id, "last": t.get("last"), "change_pct": t.get("change_pct"), "volume": t.get("volume")}
                aggregate.setdefault(sym, []).append(entry)

        summary = []
        for sym, entries in aggregate.items():
            avg_vol = sum((e.get("volume") or 0) for e in entries) / len(entries)
            last = entries[0].get("last")
            avg_change = sum((e.get("change_pct") or 0) for e in entries) / len(entries)
            summary.append({"symbol": sym, "avg_vol": avg_vol, "last": last, "avg_change": avg_change, "ex_count": len(entries)})

        movers = sorted(summary, key=lambda x: abs(x["avg_change"] or 0), reverse=True)[:20]
        lines = ["Аналітика — прості числа (без графіків):"]
        for m in movers:
            sym = m["symbol"]
            last = format_price(m["last"])
            ch = format_change(m["avg_change"])
            vol = m["avg_vol"]
            vol_str = f"{vol:,.0f}" if vol else "n/a"
            lines.append(f"{sym} — {last} | {ch} | avg vol: {vol_str} | on {m['ex_count']} exch")

        final = "\n".join(lines)
        if len(final) > 3800:
            final = final[:3800] + "\n...truncated..."
        await query.edit_message_text(final)
    except Exception as e:
        logger.exception("handle_analytics error")
        await query.edit_message_text(f"⚠️ Помилка при генерації аналітики: {e}")
    finally:
        await close_exchange_clients(clients)

# Новини — читаємо RSS
async def handle_news(query):
    headlines = []
    try:
        async with aiohttp.ClientSession() as session:
            for name, url in RSS_FEEDS.items():
                try:
                    async with session.get(url, timeout=15) as resp:
                        if resp.status != 200:
                            continue
                        text = await resp.text()
                        feed = feedparser.parse(text)
                        for e in feed.entries[:5]:
                            title = e.get("title", "No title")
                            link = e.get("link", "")
                            published = e.get("published", "")
                            headlines.append(f"[{name}] {title} — {published}\n{link}")
                except Exception:
                    logger.warning("Failed to fetch RSS %s", url)
                    continue
    except Exception as e:
        logger.exception("RSS session error")

    if not headlines:
        await query.edit_message_text("Не вдалося отримати новини з RSS.")
        return

    final = "\n\n".join(headlines)[:3800]
    await query.edit_message_text(final)

# ------- Запуск бота -------
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # додаємо хендлери
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CallbackQueryHandler(callback_handler))

    print("🚀 Бот запущений! Відправ команду /start у Telegram")
    # Блокуючий виклик — бібліотека сама керує loop
    app.run_polling()

