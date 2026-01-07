# coding: utf-8
"""
CryptoBot (Telegram bot + Flask Dashboard) — single-file project.

Features (UA + EN):
- Disclaimer shown once per user (data/accepted.json)
- Language switch (data/lang.json)
- Menu buttons:
  💱 Converter / Конвертер
  🤝 P2P Sellers / P2P Продавці
  ⏰ Alerts / Нагадування
  🧠 Advisor / Радник  (cached + fallback)
  💹 FX Market / Валютний ринок (NBU official rates)
  📊 Market Analytics / Аналітика ринку (14-day dynamics + sparkline)
  💻 Exchange Monitor / Моніторинг бірж (BTC quotes across ~15 exchanges)
  📰 News / Новини (RSS)
  ℹ️ Help / Допомога (FAQ)
- Converter:
  - UAH↔USD/EUR: NBU official rates
  - USD↔EUR: NBU cross-rate
  - Crypto (BTC/ETH/SOL/USDT) ↔ USD/EUR: CoinGecko
  - Crypto → UAH: CoinGecko(→USD) + NBU(USD→UAH)
- Alerts:
  - Crypto alerts in USD (BTC/ETH/SOL/USDT) above/below threshold
  - FX alerts: USDUAH / EURUAH above/below using NBU rate
  - Stored in data/alerts.json, background checker

Run:
  cd ~/Desktop/CryptoBot
  source venv/bin/activate
  python bot.py

Dashboard:
  http://127.0.0.1:8080
"""

from __future__ import annotations

import inspect
import asyncio
import json
import os
import random
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from dotenv import load_dotenv
from flask import Flask, Response, redirect, render_template_string, request, session, url_for

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# -------------------- ENV --------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    BOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "").strip()

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8080"))

DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "password")
DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET", "change-me")

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

ACCEPTED_FILE = DATA_DIR / "accepted.json"
LANG_FILE = DATA_DIR / "lang.json"
ALERTS_FILE = DATA_DIR / "alerts.json"
P2P_FILE = Path(__file__).resolve().parent / "p2p.json"

HTTP_HEADERS = {
    "User-Agent": "CryptoBot/1.0 (+aiogram; cached requests)",
    "Accept": "application/json,text/plain,*/*",
}

# -------------------- I18N --------------------
def i18n(lang: str, ua: str, en: str) -> str:
    return en if lang == "en" else ua


UA = {
    "CONVERT": "💱 Конвертер",
    "P2P": "🤝 P2P Продавці",
    "ALERTS": "⏰ Нагадування",
    "ADVISOR": "🧠 Радник",
    "FX": "💹 Валютний ринок",
    "ANALYTICS": "📊 Аналітика ринку",
    "EXCH": "💻 Моніторинг бірж",
    "NEWS": "📰 Новини",
    "HELP": "ℹ️ Допомога",
    "LANG": "🌐 Мова",
}
EN = {
    "CONVERT": "💱 Converter",
    "P2P": "🤝 P2P Sellers",
    "ALERTS": "⏰ Alerts",
    "ADVISOR": "🧠 Advisor",
    "FX": "💹 FX Market",
    "ANALYTICS": "📊 Market Analytics",
    "EXCH": "💻 Exchange Monitor",
    "NEWS": "📰 News",
    "HELP": "ℹ️ Help",
    "LANG": "🌐 Language",
}


def tbtn(lang: str, key: str) -> str:
    return EN[key] if lang == "en" else UA[key]


def menu_texts_all() -> set[str]:
    base = set(UA.values()) | set(EN.values())
    base |= {
        "Конвертер",
        "P2P",
        "Нагадування",
        "Радник",
        "Валютний ринок",
        "Аналітика ринку",
        "Моніторинг бірж",
        "Новини",
        "Допомога",
        "Мова",
        "Converter",
        "P2P sellers",
        "Reminders",
        "Advisor",
        "FX Market",
        "Market Analytics",
        "Exchange Monitor",
        "News",
        "Help",
        "Language",
        "🌐 Language",
        "🌐 Мова",
        "menu",
        "Меню",
    }
    return base


def main_menu(lang: str) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=tbtn(lang, "CONVERT")), KeyboardButton(text=tbtn(lang, "P2P"))],
        [KeyboardButton(text=tbtn(lang, "ALERTS")), KeyboardButton(text=tbtn(lang, "ADVISOR"))],
        [KeyboardButton(text=tbtn(lang, "FX")), KeyboardButton(text=tbtn(lang, "ANALYTICS"))],
        [KeyboardButton(text=tbtn(lang, "EXCH")), KeyboardButton(text=tbtn(lang, "NEWS"))],
        [KeyboardButton(text=tbtn(lang, "HELP")), KeyboardButton(text=tbtn(lang, "LANG"))],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


LANG_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Українська 🇺🇦"), KeyboardButton(text="English 🇬🇧")]],
    resize_keyboard=True,
)

DISCLAIMER_UA = (
    "⚠️ <b>Юридичне застереження (Disclaimer)</b>\n\n"
    "Цей бот надає інформацію лише в ознайомчих цілях і не є фінансовою порадою.\n"
    "Курси можуть відрізнятися між джерелами (НБУ — офіційний, CoinGecko — ринковий).\n\n"
    "Натискаючи «Приймаю», ви погоджуєтесь, що використовуєте інформацію на власний ризик."
)
DISCLAIMER_EN = (
    "⚠️ <b>Disclaimer</b>\n\n"
    "This bot provides information for educational purposes only and is not financial advice.\n"
    "Rates may differ by source (NBU = official, CoinGecko = market).\n\n"
    "By pressing “I accept”, you agree you use this information at your own risk."
)

# -------------------- JSON storage helpers --------------------
def _safe_read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _safe_write_json(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_accepted() -> Dict[str, bool]:
    data = _safe_read_json(ACCEPTED_FILE, {})
    return data if isinstance(data, dict) else {}


def save_accepted(m: Dict[str, bool]) -> None:
    _safe_write_json(ACCEPTED_FILE, m)


def is_accepted(user_id: int) -> bool:
    return bool(load_accepted().get(str(user_id), False))


def set_accepted(user_id: int, val: bool = True) -> None:
    m = load_accepted()
    m[str(user_id)] = bool(val)
    save_accepted(m)


def load_lang_map() -> Dict[str, str]:
    data = _safe_read_json(LANG_FILE, {})
    return data if isinstance(data, dict) else {}


def save_lang_map(m: Dict[str, str]) -> None:
    _safe_write_json(LANG_FILE, m)


def get_lang(user_id: int) -> str:
    m = load_lang_map()
    lang = m.get(str(user_id), "ua")
    return "en" if lang == "en" else "ua"


def set_lang(user_id: int, lang: str) -> None:
    m = load_lang_map()
    m[str(user_id)] = "en" if lang == "en" else "ua"
    save_lang_map(m)


def load_alerts() -> List[Dict[str, Any]]:
    data = _safe_read_json(ALERTS_FILE, [])
    return data if isinstance(data, list) else []


def save_alerts(items: List[Dict[str, Any]]) -> None:
    _safe_write_json(ALERTS_FILE, items)


# -------------------- HTTP helpers --------------------
async def fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 18,
) -> Any:
    for attempt in range(2):
        try:
            async with session.get(url, params=params, timeout=timeout) as r:
                if r.status == 429 and attempt == 0:
                    await asyncio.sleep(1.8)
                    continue
                r.raise_for_status()
                return await r.json()
        except asyncio.TimeoutError:
            if attempt == 0:
                await asyncio.sleep(0.7)
                continue
            raise


# -------------------- Data sources --------------------
COINGECKO_SIMPLE_PRICE = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
COINGECKO_TICKERS = "https://api.coingecko.com/api/v3/coins/bitcoin/tickers"
NBU_EXCHANGE = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange"

COIN_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "USDT": "tether",
}

FX_PAIRS = {"USDUAH", "EURUAH"}  # for alerts


async def get_crypto_price(coin_id: str, vs: str = "usd") -> Optional[float]:
    vs = vs.lower()
    async with aiohttp.ClientSession(headers=HTTP_HEADERS) as s:
        data = await fetch_json(s, COINGECKO_SIMPLE_PRICE, params={"ids": coin_id, "vs_currencies": vs})
    try:
        return float(data[coin_id][vs])
    except Exception:
        return None


async def get_crypto_snapshot_usd(ids: List[str]) -> Dict[str, Dict[str, Any]]:
    params = {"vs_currency": "usd", "ids": ",".join(ids), "price_change_percentage": "24h"}
    async with aiohttp.ClientSession(headers=HTTP_HEADERS) as s:
        data = await fetch_json(s, COINGECKO_MARKETS, params=params)
    out: Dict[str, Dict[str, Any]] = {}
    if isinstance(data, list):
        for item in data:
            cid = str(item.get("id") or "")
            if not cid:
                continue
            out[cid] = {
                "price": item.get("current_price"),
                "ch24": item.get("price_change_percentage_24h"),
            }
    return out


async def get_nbu_rates(date: Optional[datetime] = None) -> Dict[str, float]:
    params = {"json": ""}
    if date is not None:
        params["date"] = date.strftime("%Y%m%d")
    async with aiohttp.ClientSession(headers=HTTP_HEADERS) as s:
        data = await fetch_json(s, NBU_EXCHANGE, params=params)
    rates: Dict[str, float] = {}
    if isinstance(data, list):
        for row in data:
            try:
                cc = str(row.get("cc", "")).upper()
                rate = float(row.get("rate"))
                if cc:
                    rates[cc] = rate
            except Exception:
                continue
    return rates


async def get_nbu_rate(code: str) -> Optional[float]:
    code = code.upper()
    rates = await get_nbu_rates()
    return rates.get(code)


async def get_nbu_rate_history(code: str, days: int = 7) -> List[Tuple[str, float]]:
    code = code.upper()
    out: List[Tuple[str, float]] = []
    for i in range(days - 1, -1, -1):
        d = datetime.utcnow() - timedelta(days=i)
        try:
            rates = await get_nbu_rates(d)
            if code in rates:
                out.append((d.strftime("%m-%d"), float(rates[code])))
        except Exception:
            continue
        await asyncio.sleep(0.05)
    return out


def sparkline(values: List[float]) -> str:
    if not values:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    mn, mx = min(values), max(values)
    if mx - mn < 1e-9:
        return blocks[0] * len(values)
    res = []
    for v in values:
        idx = int((v - mn) / (mx - mn) * (len(blocks) - 1))
        res.append(blocks[idx])
    return "".join(res)


# -------------------- Converter --------------------
def _norm_ccy(s: str) -> str:
    return (s or "").strip().upper()


async def convert(amount: float, src: str, dst: str) -> Tuple[float, str]:
    src = _norm_ccy(src)
    dst = _norm_ccy(dst)

    if amount < 0:
        raise ValueError("amount<0")

    if src == dst:
        return amount, "Identity"

    # FX: USD/EUR <-> UAH via NBU
    if src in {"USD", "EUR"} and dst == "UAH":
        rate = await get_nbu_rate(src)
        if not rate:
            raise RuntimeError("NBU rate missing")
        return amount * rate, f"NBU {src}/UAH={rate:.4f}"

    if src == "UAH" and dst in {"USD", "EUR"}:
        rate = await get_nbu_rate(dst)
        if not rate:
            raise RuntimeError("NBU rate missing")
        return amount / rate, f"NBU {dst}/UAH={rate:.4f}"

    # Cross: USD <-> EUR via NBU
    if src in {"USD", "EUR"} and dst in {"USD", "EUR"} and src != dst:
        r_src = await get_nbu_rate(src)
        r_dst = await get_nbu_rate(dst)
        if not r_src or not r_dst:
            raise RuntimeError("NBU cross missing")
        return (amount * r_src) / r_dst, f"NBU cross ({src}->{dst})"

    # Crypto codes
    if src in COIN_IDS and dst in {"USD", "EUR"}:
        coin_id = COIN_IDS[src]
        price = await get_crypto_price(coin_id, dst.lower())
        if price is None:
            raise RuntimeError("CG price missing")
        return amount * price, f"CoinGecko {src}/{dst}={price:.6f}"

    if dst in COIN_IDS and src in {"USD", "EUR"}:
        coin_id = COIN_IDS[dst]
        price = await get_crypto_price(coin_id, src.lower())
        if price is None or price == 0:
            raise RuntimeError("CG price missing")
        return amount / price, f"CoinGecko {dst}/{src}={price:.6f} (inverted)"

    # Crypto -> UAH via USD + NBU
    if src in COIN_IDS and dst == "UAH":
        coin_id = COIN_IDS[src]
        price_usd = await get_crypto_price(coin_id, "usd")
        usd_uah = await get_nbu_rate("USD")
        if price_usd is None or usd_uah is None:
            raise RuntimeError("CG/NBU missing")
        return amount * price_usd * usd_uah, "CoinGecko (→USD) + NBU USD/UAH"

    if src == "UAH" and dst in COIN_IDS:
        coin_id = COIN_IDS[dst]
        price_usd = await get_crypto_price(coin_id, "usd")
        usd_uah = await get_nbu_rate("USD")
        if price_usd is None or usd_uah is None or price_usd == 0:
            raise RuntimeError("CG/NBU missing")
        usd_amt = amount / usd_uah
        return usd_amt / price_usd, "NBU USD/UAH + CoinGecko (USD→coin)"

    raise RuntimeError("pair not supported")


def parse_convert_input(text: str) -> Optional[Tuple[float, str, str]]:
    if not text:
        return None
    parts = text.strip().replace("to", " ").replace("в", " ").split()
    if len(parts) == 2:
        return 1.0, parts[0], parts[1]
    if len(parts) >= 3:
        try:
            amount = float(parts[0].replace(",", "."))
        except Exception:
            return None
        return amount, parts[1], parts[2]
    return None


# -------------------- Advisor (cache + fallback) --------------------
ADVISOR_CACHE_TTL_SEC = 60
ADVISOR_CACHE_MAX_STALE_SEC = 24 * 3600
_advisor_cache: Dict[str, Dict[str, Any]] = {"ua": {"ts": 0.0, "text": ""}, "en": {"ts": 0.0, "text": ""}}


async def build_advisor_text(lang: str) -> str:
    ids = [COIN_IDS["BTC"], COIN_IDS["ETH"], COIN_IDS["SOL"]]
    snap = await get_crypto_snapshot_usd(ids)

    def row(sym: str, cid: str) -> str:
        it = snap.get(cid, {})
        p = it.get("price")
        ch = it.get("ch24")
        if not isinstance(p, (int, float)) or not isinstance(ch, (int, float)):
            return f"{sym}: " + i18n(lang, "дані недоступні", "unavailable")
        mood_ua = "флет" if -3 <= ch <= 3 else ("імпульс ↑" if ch > 3 else "просадка ↓")
        mood_en = "flat" if -3 <= ch <= 3 else ("impulse ↑" if ch > 3 else "dip ↓")
        mood = mood_en if lang == "en" else mood_ua
        return f"{sym}: <b>${p:,.2f}</b> | 24h: <b>{ch:+.2f}%</b> | <b>{mood}</b>".replace(",", " ")

    rows = "\n".join(
        [
            row("BTC", COIN_IDS["BTC"]),
            row("ETH", COIN_IDS["ETH"]),
            row("SOL", COIN_IDS["SOL"]),
        ]
    )

    if lang == "en":
        return (
            "🧠 <b>Advisor (quick snapshot)</b>\n"
            f"{rows}\n\n"
            "✅ Tips:\n"
            "• Impulse ↑ — fast move; avoid chasing spikes.\n"
            "• Dip ↓ — may be sell-off; trend can continue.\n"
            "• Flat — sideways; often before a strong move.\n\n"
            "⚠️ Not financial advice."
        )
    return (
        "🧠 <b>Радник (короткий огляд)</b>\n"
        f"{rows}\n\n"
        "✅ Пояснення:\n"
        "• Імпульс ↑ — швидкий рух; ризик входу на піку.\n"
        "• Просадка ↓ — можливі розпродажі; тренд може продовжитись.\n"
        "• Флет — боковик; часто перед сильним рухом.\n\n"
        "⚠️ Це не фінансова порада."
    )


async def get_advisor_text_cached(lang: str) -> str:
    now = datetime.utcnow().timestamp()
    key = "en" if lang == "en" else "ua"
    cached_ts = float(_advisor_cache[key].get("ts", 0.0) or 0.0)
    cached_text = str(_advisor_cache[key].get("text", "") or "")

    if cached_text and (now - cached_ts) <= ADVISOR_CACHE_TTL_SEC:
        return cached_text

    try:
        fresh = await asyncio.wait_for(build_advisor_text(lang), timeout=16)
        _advisor_cache[key] = {"ts": now, "text": fresh}
        return fresh
    except Exception:
        if cached_text and (now - cached_ts) <= ADVISOR_CACHE_MAX_STALE_SEC:
            note = (
                "\n\nℹ️ <i>Showing cached data (may be outdated).</i>"
                if key == "en"
                else "\n\nℹ️ <i>Показую кеш (може бути застарілим).</i>"
            )
            return cached_text + note
        raise


# -------------------- FX Market + Analytics --------------------
async def build_fx_text(lang: str) -> str:
    usd = await get_nbu_rate("USD")
    eur = await get_nbu_rate("EUR")
    hist_usd = await get_nbu_rate_history("USD", days=7)

    trend_ua = "📈 USD/UAH за 7 днів: дані недоступні"
    trend_en = "📈 USD/UAH 7 days: unavailable"
    if len(hist_usd) >= 2:
        r0 = hist_usd[0][1]
        r1 = hist_usd[-1][1]
        diff = r1 - r0
        trend_ua = f"📈 USD/UAH за 7 днів: {r0:.2f} → {r1:.2f} ({diff:+.2f})"
        trend_en = f"📈 USD/UAH 7 days: {r0:.2f} → {r1:.2f} ({diff:+.2f})"

    if lang == "en":
        base = "💹 <b>FX Market (official averages)</b>\nSource: <b>NBU</b> (official mid rates).\n\n"
        base += f"• USD/UAH: <b>{usd:.2f}</b>\n" if usd else "• USD/UAH: unavailable\n"
        base += f"• EUR/UAH: <b>{eur:.2f}</b>\n" if eur else "• EUR/UAH: unavailable\n"
        return base + f"\n{trend_en}"

    base = "💹 <b>Валютний ринок (середні офіційні)</b>\nДжерело: <b>НБУ</b> (офіційний середній курс).\n\n"
    base += f"• USD/UAH: <b>{usd:.2f}</b>\n" if usd else "• USD/UAH: недоступно\n"
    base += f"• EUR/UAH: <b>{eur:.2f}</b>\n" if eur else "• EUR/UAH: недоступно\n"
    return base + f"\n{trend_ua}"


async def build_analytics_text(lang: str) -> str:
    hist_usd = await get_nbu_rate_history("USD", days=14)
    hist_eur = await get_nbu_rate_history("EUR", days=14)

    def block(title: str, hist: List[Tuple[str, float]]) -> str:
        if len(hist) < 2:
            return f"{title}: " + i18n(lang, "дані недоступні", "unavailable")
        labels = [d for d, _ in hist]
        vals = [v for _, v in hist]
        sp = sparkline(vals)
        delta = vals[-1] - vals[0]
        return (
            f"<b>{title}</b>\n"
            f"{labels[0]} … {labels[-1]}\n"
            f"{sp}\n"
            f"Start: {vals[0]:.2f}  End: {vals[-1]:.2f}  Δ {delta:+.2f}"
        )

    if lang == "en":
        return (
            "📊 <b>Market Analytics (NBU)</b>\n"
            "Last 14 days dynamics (sparkline).\n\n"
            + block("USD/UAH", hist_usd)
            + "\n\n"
            + block("EUR/UAH", hist_eur)
        )

    return (
        "📊 <b>Аналітика ринку (НБУ)</b>\n"
        "Динаміка за 14 днів (спарклайн).\n\n"
        + block("USD/UAH", hist_usd)
        + "\n\n"
        + block("EUR/UAH", hist_eur)
    )


# -------------------- Exchange Monitor --------------------
async def build_exchange_monitor_text(lang: str) -> str:
    async with aiohttp.ClientSession(headers=HTTP_HEADERS) as s:
        data = await fetch_json(s, COINGECKO_TICKERS, params={"include_exchange_logo": "false"}, timeout=22)

    tickers = data.get("tickers", []) if isinstance(data, dict) else []
    rows = []
    for t in tickers:
        try:
            market = t.get("market", {}).get("name") or "?"
            base = (t.get("base") or "").upper()
            target = (t.get("target") or "").upper()
            last = t.get("last")
            vol = t.get("volume")
            if base != "BTC":
                continue
            if target not in {"USDT", "USD"}:
                continue
            if not isinstance(last, (int, float)):
                continue
            rows.append((float(vol) if isinstance(vol, (int, float)) else 0.0, market, target, float(last)))
        except Exception:
            continue

    rows.sort(key=lambda x: x[0], reverse=True)
    rows = rows[:15]

    if not rows:
        return i18n(lang, "❌ Дані бірж тимчасово недоступні.", "❌ Exchange data temporarily unavailable.")

    lines = []
    for i, (_, market, target, last) in enumerate(rows, 1):
        lines.append(f"{i:>2}. <b>{market}</b> — BTC/{target}: <b>{last:,.2f}</b>".replace(",", " "))

    sym_map_ua = (
        "\n\n<b>Карта символів</b>:\n"
        "• BTC = Bitcoin\n"
        "• ETH = Ethereum\n"
        "• SOL = Solana\n"
        "• USDT = Tether\n"
    )
    sym_map_en = (
        "\n\n<b>Symbol map</b>:\n"
        "• BTC = Bitcoin\n"
        "• ETH = Ethereum\n"
        "• SOL = Solana\n"
        "• USDT = Tether\n"
    )

    title = (
        "💻 <b>Моніторинг бірж</b>\nBTC котирування на ~15 біржах (CoinGecko):\n\n"
        if lang != "en"
        else "💻 <b>Exchange Monitor</b>\nBTC quotes across ~15 exchanges (CoinGecko):\n\n"
    )
    return title + "\n".join(lines) + (sym_map_en if lang == "en" else sym_map_ua)


# -------------------- News (RSS) --------------------
RSS_CRYPTO = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]
RSS_FX = [
    "https://www.reuters.com/rssFeed/topNews",
]


async def fetch_rss_titles(url: str, limit: int = 5) -> List[str]:
    async with aiohttp.ClientSession(
        headers={"User-Agent": "CryptoBot/1.0", "Accept": "application/rss+xml,application/xml,text/xml,*/*"}
    ) as s:
        async with s.get(url, timeout=18) as r:
            r.raise_for_status()
            xml = await r.text()
    titles = []
    for part in xml.split("<title>")[1:]:
        t = part.split("</title>")[0].strip()
        if not t:
            continue
        if len(titles) == 0:
            titles.append(t)
            continue
        titles.append(t)
        if len(titles) >= limit + 1:
            break
    return titles[1 : limit + 1] if len(titles) > 1 else titles[:limit]


async def build_news_text(lang: str) -> str:
    urls = RSS_CRYPTO + RSS_FX
    random.shuffle(urls)
    items: List[str] = []
    for url in urls[:3]:
        try:
            titles = await fetch_rss_titles(url, limit=4)
            items.extend(titles)
        except Exception:
            continue
    if not items:
        return i18n(lang, "❌ Новини тимчасово недоступні.", "❌ News temporarily unavailable.")

    head = "📰 <b>Новини</b>\n" if lang != "en" else "📰 <b>News</b>\n"
    lines = [f"• {t}" for t in items[:10]]
    return head + "\n".join(lines)


# -------------------- P2P sellers --------------------
@dataclass
class P2PSeller:
    name: str
    currency: str
    rate: str
    limit: str
    contact: str


def load_p2p_sellers() -> List[P2PSeller]:
    if not P2P_FILE.exists():
        return []
    try:
        data = json.loads(P2P_FILE.read_text(encoding="utf-8"))
        out = []
        if isinstance(data, list):
            for row in data:
                if not isinstance(row, dict):
                    continue
                out.append(
                    P2PSeller(
                        name=str(row.get("name", "")),
                        currency=str(row.get("currency", "")),
                        rate=str(row.get("rate", "")),
                        limit=str(row.get("limit", "")),
                        contact=str(row.get("contact", "")),
                    )
                )
        return out
    except Exception:
        return []


def save_p2p_sellers(items: List[P2PSeller]) -> None:
    data = [asdict(x) for x in items]
    P2P_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def p2p_inline_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=i18n(lang, "Відкрити Dashboard", "Open Dashboard"),
                    url=f"http://{HOST}:{PORT}",
                )
            ]
        ]
    )


# -------------------- Dashboard (Flask) --------------------
app = Flask(__name__)
app.secret_key = DASHBOARD_SECRET

BASE_HTML = """
<!doctype html>
<html lang="uk">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }}</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body { background: #f6f7fb; }
    .navbar-brand { font-weight: 700; }
    .card { border-radius: 16px; }
    .table td, .table th { vertical-align: middle; }
    .muted { color: #6c757d; font-size: 0.9rem; }
    .container-narrow { max-width: 1100px; }
  </style>
</head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark bg-dark">
  <div class="container container-narrow">
    <a class="navbar-brand" href="{{ url_for('dash_home') }}">CryptoBot Dashboard</a>
    <div class="collapse navbar-collapse">
      {% if logged_in %}
      <ul class="navbar-nav ms-auto">
        <li class="nav-item"><a class="nav-link" href="{{ url_for('dash_home') }}">Home</a></li>
        <li class="nav-item"><a class="nav-link" href="{{ url_for('dash_p2p') }}">P2P</a></li>
        <li class="nav-item"><a class="nav-link" href="{{ url_for('dash_logout') }}">Logout</a></li>
      </ul>
      {% endif %}
    </div>
  </div>
</nav>

<div class="container container-narrow my-4">
  <div class="card shadow-sm">
    <div class="card-body p-4">
      {{ body|safe }}
    </div>
  </div>
  <p class="muted mt-3 mb-0">
    Tip: змінити логін/пароль можна через <code>DASHBOARD_USER</code> / <code>DASHBOARD_PASS</code> у <code>.env</code>
  </p>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""


def _is_logged_in() -> bool:
    return bool(session.get("logged_in", False))


@app.get("/")
def dash_home() -> Response:
    if not _is_logged_in():
        return redirect(url_for("dash_login"))
    body = """
    <h3 class="mb-2">Home</h3>
    <p class="muted">Manage P2P sellers for the bot.</p>
    <div class="d-flex gap-2">
      <a class="btn btn-primary" href="/p2p">Open P2P list</a>
      <a class="btn btn-outline-secondary" href="/logout">Logout</a>
    </div>
    """
    return render_template_string(BASE_HTML, title="Home", body=body, logged_in=True)


@app.get("/login")
def dash_login() -> str:
    body = """
    <div class="row justify-content-center">
      <div class="col-12 col-md-6 col-lg-5">
        <h3 class="mb-3">Login</h3>
        <p class="muted">Вхід у панель керування P2P продавцями</p>
        <form method="post" action="/login" class="mt-3">
          <div class="mb-3">
            <label class="form-label">Username</label>
            <input class="form-control" name="user" required />
          </div>
          <div class="mb-3">
            <label class="form-label">Password</label>
            <input class="form-control" name="pass" type="password" required />
          </div>
          <button class="btn btn-primary w-100" type="submit">Login</button>
        </form>
      </div>
    </div>
    """
    return render_template_string(BASE_HTML, title="Login", body=body, logged_in=_is_logged_in())


@app.post("/login")
def dash_login_post() -> Response:
    user = (request.form.get("user") or "").strip()
    pw = (request.form.get("pass") or "").strip()
    if user == DASHBOARD_USER and pw == DASHBOARD_PASS:
        session["logged_in"] = True
        return redirect(url_for("dash_home"))
    return redirect(url_for("dash_login"))


@app.get("/logout")
def dash_logout() -> Response:
    session.clear()
    return redirect(url_for("dash_login"))


def _seller_form_html(seller: Optional[P2PSeller]) -> str:
    s = seller or P2PSeller(name="", currency="USDT", rate="", limit="", contact="")
    return f"""
    <form method="post" class="mt-3">
      <div class="row g-3">
        <div class="col-12 col-md-6">
          <label class="form-label">Name</label>
          <input class="form-control" name="name" value="{s.name}" required />
        </div>
        <div class="col-12 col-md-6">
          <label class="form-label">Contact</label>
          <input class="form-control" name="contact" value="{s.contact}" placeholder="@telegram або телефон" />
        </div>

        <div class="col-12 col-md-4">
          <label class="form-label">Currency</label>
          <select class="form-select" name="currency">
            <option {"selected" if s.currency=="USDT" else ""}>USDT</option>
            <option {"selected" if s.currency=="UAH" else ""}>UAH</option>
            <option {"selected" if s.currency=="USD" else ""}>USD</option>
          </select>
        </div>
        <div class="col-12 col-md-4">
          <label class="form-label">Rate</label>
          <input class="form-control" name="rate" value="{s.rate}" placeholder="e.g. 39.20" />
        </div>
        <div class="col-12 col-md-4">
          <label class="form-label">Limit</label>
          <input class="form-control" name="limit" value="{s.limit}" placeholder="e.g. 10k–200k" />
        </div>

        <div class="col-12 d-flex gap-2 mt-2">
          <button class="btn btn-primary" type="submit">Save</button>
          <a class="btn btn-outline-secondary" href="/p2p">Cancel</a>
        </div>
      </div>
    </form>
    """


@app.get("/p2p")
def dash_p2p() -> Response:
    if not _is_logged_in():
        return redirect(url_for("dash_login"))

    sellers = load_p2p_sellers()
    rows = ""
    for i, s in enumerate(sellers):
        rows += f"""
        <tr>
          <td class="text-muted">{i+1}</td>
          <td><b>{s.name}</b></td>
          <td><span class="badge bg-secondary">{s.currency}</span></td>
          <td>{s.rate}</td>
          <td>{s.limit}</td>
          <td>{s.contact}</td>
          <td class="text-end">
            <a class="btn btn-sm btn-outline-primary" href="/p2p/edit/{i}">Edit</a>
            <a class="btn btn-sm btn-outline-danger" href="/p2p/delete/{i}" onclick="return confirm('Delete this seller?')">Delete</a>
          </td>
        </tr>
        """

    body = f"""
    <div class="d-flex justify-content-between align-items-center mb-3">
      <div>
        <h3 class="mb-0">P2P sellers</h3>
        <div class="muted">Total: <b>{len(sellers)}</b></div>
      </div>
      <a class="btn btn-success" href="/p2p/new">+ Add seller</a>
    </div>

    <div class="table-responsive">
      <table class="table table-hover align-middle">
        <thead class="table-light">
          <tr>
            <th style="width:60px;">#</th>
            <th>Name</th>
            <th style="width:110px;">Currency</th>
            <th style="width:140px;">Rate</th>
            <th style="width:140px;">Limit</th>
            <th>Contact</th>
            <th style="width:170px;" class="text-end">Actions</th>
          </tr>
        </thead>
        <tbody>
          {rows if rows else '<tr><td colspan="7" class="text-center text-muted py-4">No sellers yet</td></tr>'}
        </tbody>
      </table>
    </div>
    """
    return render_template_string(BASE_HTML, title="P2P", body=body, logged_in=True)


@app.get("/p2p/new")
def dash_p2p_new() -> Response:
    if not _is_logged_in():
        return redirect(url_for("dash_login"))
    body = "<h3>Add seller</h3>" + _seller_form_html(None)
    return render_template_string(BASE_HTML, title="Add", body=body, logged_in=True)


@app.post("/p2p/new")
def dash_p2p_new_post() -> Response:
    if not _is_logged_in():
        return redirect(url_for("dash_login"))
    sellers = load_p2p_sellers()
    sellers.append(
        P2PSeller(
            name=request.form.get("name", ""),
            currency=request.form.get("currency", ""),
            rate=request.form.get("rate", ""),
            limit=request.form.get("limit", ""),
            contact=request.form.get("contact", ""),
        )
    )
    save_p2p_sellers(sellers)
    return redirect(url_for("dash_p2p"))


@app.get("/p2p/edit/<int:idx>")
def dash_p2p_edit(idx: int) -> Response:
    if not _is_logged_in():
        return redirect(url_for("dash_login"))
    sellers = load_p2p_sellers()
    if idx < 0 or idx >= len(sellers):
        return redirect(url_for("dash_p2p"))
    body = "<h3>Edit seller</h3>" + _seller_form_html(sellers[idx])
    return render_template_string(BASE_HTML, title="Edit", body=body, logged_in=True)


@app.post("/p2p/edit/<int:idx>")
def dash_p2p_edit_post(idx: int) -> Response:
    if not _is_logged_in():
        return redirect(url_for("dash_login"))
    sellers = load_p2p_sellers()
    if idx < 0 or idx >= len(sellers):
        return redirect(url_for("dash_p2p"))
    sellers[idx] = P2PSeller(
        name=request.form.get("name", ""),
        currency=request.form.get("currency", ""),
        rate=request.form.get("rate", ""),
        limit=request.form.get("limit", ""),
        contact=request.form.get("contact", ""),
    )
    save_p2p_sellers(sellers)
    return redirect(url_for("dash_p2p"))


@app.get("/p2p/delete/<int:idx>")
def dash_p2p_delete(idx: int) -> Response:
    if not _is_logged_in():
        return redirect(url_for("dash_login"))
    sellers = load_p2p_sellers()
    if 0 <= idx < len(sellers):
        sellers.pop(idx)
        save_p2p_sellers(sellers)
    return redirect(url_for("dash_p2p"))


def run_dashboard() -> None:
    app.run(host=HOST, port=PORT, debug=False)


# -------------------- Telegram bot (aiogram v3) --------------------
router = Router()
dp = Dispatcher()
dp.include_router(router)

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


class ConverterState(StatesGroup):
    waiting_text = State()


class AlertState(StatesGroup):
    waiting_text = State()


def require_accept(func):
    sig = inspect.signature(func)
    allowed = set(sig.parameters.keys())

    async def wrapper(message: Message, state: FSMContext, *args, **kwargs):
        lang = get_lang(message.from_user.id)

        if not is_accepted(message.from_user.id):
            await message.answer(i18n(lang, DISCLAIMER_UA, DISCLAIMER_EN), reply_markup=disclaimer_kb(lang))
            await message.answer(i18n(lang, "Оберіть мову:", "Choose language:"), reply_markup=LANG_MENU)
            return

        # ✅ aiogram може передавати dispatcher, bot, event_from_user тощо
        # ми пропускаємо тільки те, що реально є в сигнатурі функції
        filtered_kwargs = {k: v for k, v in kwargs.items() if k in allowed}

        return await func(message, state, *args, **filtered_kwargs)

    return wrapper


def disclaimer_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=i18n(lang, "✅ Приймаю", "✅ I accept"), callback_data="disclaimer:accept"),
                InlineKeyboardButton(text=i18n(lang, "❌ Не приймаю", "❌ Decline"), callback_data="disclaimer:decline"),
            ]
        ]
    )


@router.callback_query(F.data == "disclaimer:accept")
async def disclaimer_accept(call: CallbackQuery) -> None:
    user_id = call.from_user.id
    set_accepted(user_id, True)
    lang = get_lang(user_id)
    await call.answer()
    await call.message.answer(i18n(lang, "✅ Прийнято. Меню нижче 👇", "✅ Accepted. Menu below 👇"), reply_markup=main_menu(lang))


@router.callback_query(F.data == "disclaimer:decline")
async def disclaimer_decline(call: CallbackQuery) -> None:
    lang = get_lang(call.from_user.id)
    await call.answer()
    await call.message.answer(i18n(lang, "Ок. Якщо не погоджуєтесь — не використовуйте бота.", "OK. If you decline — please don't use the bot."))


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    lang = get_lang(message.from_user.id)
    if not is_accepted(message.from_user.id):
        await message.answer(i18n(lang, DISCLAIMER_UA, DISCLAIMER_EN), reply_markup=disclaimer_kb(lang))
        await message.answer(i18n(lang, "Оберіть мову:", "Choose language:"), reply_markup=LANG_MENU)
        return
    await message.answer(i18n(lang, "Меню 👇", "Menu 👇"), reply_markup=main_menu(lang))


@router.message(Command("help"))
@require_accept
async def cmd_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    lang = get_lang(message.from_user.id)
    await message.answer(help_text(lang), reply_markup=main_menu(lang))


@router.message(F.text.in_(["Українська 🇺🇦", "English 🇬🇧"]))
async def lang_pick(message: Message, state: FSMContext) -> None:
    await state.clear()
    if "English" in (message.text or ""):
        set_lang(message.from_user.id, "en")
    else:
        set_lang(message.from_user.id, "ua")
    lang = get_lang(message.from_user.id)
    await message.answer(i18n(lang, "✅ Мову змінено.", "✅ Language changed."), reply_markup=main_menu(lang))


def help_text(lang: str) -> str:
    if lang == "en":
        return (
            "ℹ️ <b>Help / FAQ</b>\n\n"
            "💱 <b>Converter</b>\n"
            "Examples:\n"
            "• <code>100 UAH USD</code>\n"
            "• <code>200 USD EUR</code>\n"
            "• <code>0.01 BTC UAH</code>\n"
            "• <code>BTC UAH</code> (amount=1)\n\n"
            "⏰ <b>Alerts</b>\n"
            "Crypto in USD: <code>BTC below 65000</code>\n"
            "FX NBU: <code>USDUAH above 42</code>\n\n"
            "🧠 <b>Advisor</b>\n"
            "Quick snapshot (price + 24h %). Cached fallback if API is down.\n\n"
            "💹 <b>FX Market</b> — official NBU rates.\n"
            "📊 <b>Market Analytics</b> — 14-day dynamics (sparkline).\n"
            "💻 <b>Exchange Monitor</b> — BTC quotes across ~15 exchanges.\n"
        )
    return (
        "ℹ️ <b>Допомога / FAQ</b>\n\n"
        "💱 <b>Конвертер</b>\n"
        "Приклади:\n"
        "• <code>100 UAH USD</code>\n"
        "• <code>200 USD EUR</code>\n"
        "• <code>0.01 BTC UAH</code>\n"
        "• <code>BTC UAH</code> (сума=1)\n\n"
        "⏰ <b>Нагадування</b>\n"
        "Crypto в USD: <code>BTC below 65000</code>\n"
        "FX НБУ: <code>USDUAH above 42</code>\n\n"
        "🧠 <b>Радник</b>\n"
        "Короткий огляд (ціна + 24h %). Є кеш+fallback.\n\n"
        "💹 <b>Валютний ринок</b> — офіційні курси НБУ.\n"
        "📊 <b>Аналітика ринку</b> — динаміка 14 днів (спарклайн).\n"
        "💻 <b>Моніторинг бірж</b> — BTC котирування ~15 бірж.\n"
    )


# -------------------- Converter FSM handler --------------------
@router.message(ConverterState.waiting_text)
@require_accept
async def converter_input(message: Message, state: FSMContext) -> None:
    lang = get_lang(message.from_user.id)
    q = (message.text or "").strip()

    if q in menu_texts_all():
        await state.clear()
        await router_menu(message, state)
        return

    if q.lower() in {"cancel", "відміна", "назад", "menu", "меню"}:
        await state.clear()
        await message.answer(i18n(lang, "Скасовано ✅", "Canceled ✅"), reply_markup=main_menu(lang))
        return

    parsed = parse_convert_input(q)
    if not parsed:
        await message.answer(
            i18n(
                lang,
                "Формат: <code>100 UAH USD</code> або <code>0.5 BTC UAH</code> або <code>BTC UAH</code> (сума=1)",
                "Format: <code>100 UAH USD</code> or <code>0.5 BTC UAH</code> or <code>BTC UAH</code> (amount=1)",
            )
        )
        return

    amount, src, dst = parsed
    try:
        result, rate_info = await asyncio.wait_for(convert(float(amount), str(src), str(dst)), timeout=14)
        await message.answer(
            i18n(lang, "🧮 <b>Результат</b>\n", "🧮 <b>Result</b>\n")
            + f"{amount:g} {_norm_ccy(src)} ≈ <b>{result:,.6f}</b> {_norm_ccy(dst)}\n".replace(",", " ")
            + f"<i>Source: {rate_info}</i>",
            reply_markup=main_menu(lang),
        )
    except Exception:
        await message.answer(
            i18n(
                lang,
                "❌ Не вдалося конвертувати. Спробуй: <code>100 UAH USD</code> або <code>200 USD EUR</code> або <code>0.01 BTC UAH</code>",
                "❌ Conversion failed. Try: <code>100 UAH USD</code> or <code>200 USD EUR</code> or <code>0.01 BTC UAH</code>",
            ),
            reply_markup=main_menu(lang),
        )
    finally:
        await state.clear()


# -------------------- Alerts --------------------
def parse_alert_input(text: str) -> Optional[Tuple[str, str, float]]:
    if not text:
        return None
    s = text.strip().upper().replace(",", ".")
    parts = s.split()

    if len(parts) >= 4 and parts[0] in {"USD", "EUR"} and parts[1] == "UAH":
        parts = [parts[0] + parts[1]] + parts[2:]

    if len(parts) != 3:
        return None
    symbol, direction, target_s = parts
    if direction not in {"ABOVE", "BELOW"}:
        return None
    try:
        target = float(target_s)
    except Exception:
        return None

    if symbol in COIN_IDS:
        return symbol, direction, target
    if symbol in FX_PAIRS:
        return symbol, direction, target
    return None


async def get_symbol_price(symbol: str) -> Optional[float]:
    symbol = symbol.upper()
    if symbol in COIN_IDS:
        return await get_crypto_price(COIN_IDS[symbol], "usd")
    if symbol == "USDUAH":
        return await get_nbu_rate("USD")
    if symbol == "EURUAH":
        return await get_nbu_rate("EUR")
    return None


def add_alert(user_id: int, symbol: str, direction: str, target: float) -> None:
    items = load_alerts()
    items.append(
        {
            "user_id": int(user_id),
            "symbol": symbol.upper(),
            "direction": direction.upper(),
            "target": float(target),
            "active": True,
            "created_at": datetime.utcnow().isoformat(),
        }
    )
    save_alerts(items)


def list_alerts(user_id: int) -> List[Dict[str, Any]]:
    items = load_alerts()
    return [a for a in items if int(a.get("user_id", 0)) == int(user_id)]


def deactivate_alert(user_id: int, idx: int) -> bool:
    items = load_alerts()
    user_items = [i for i, a in enumerate(items) if int(a.get("user_id", 0)) == int(user_id)]
    if idx < 0 or idx >= len(user_items):
        return False
    real_i = user_items[idx]
    items[real_i]["active"] = False
    save_alerts(items)
    return True


def alerts_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=i18n(lang, "➕ Додати алерт", "➕ Add alert"), callback_data="alert:add")],
            [InlineKeyboardButton(text=i18n(lang, "📄 Мої алерти", "📄 My alerts"), callback_data="alert:list")],
            [InlineKeyboardButton(text=i18n(lang, "ℹ️ Як це працює", "ℹ️ How it works"), callback_data="alert:how")],
        ]
    )


@router.callback_query(F.data == "alert:how")
@require_accept
async def alert_how(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    lang = get_lang(call.from_user.id)
    await call.answer()
    await call.message.answer(
        i18n(
            lang,
            "ℹ️ <b>Як працюють алерти</b>\n\n"
            "Формат:\n"
            "• <code>BTC below 65000</code>\n"
            "• <code>USDUAH above 42</code>\n\n"
            "Підтримка:\n"
            "• BTC/ETH/SOL/USDT — ціна в USD (CoinGecko)\n"
            "• USDUAH/EURUAH — офіційний курс НБУ\n\n"
            "Коли ціна перетне рівень — ти отримаєш повідомлення, а алерт автоматично вимкнеться.",
            "ℹ️ <b>How alerts work</b>\n\n"
            "Format:\n"
            "• <code>BTC below 65000</code>\n"
            "• <code>USDUAH above 42</code>\n\n"
            "Supported:\n"
            "• BTC/ETH/SOL/USDT — USD price (CoinGecko)\n"
            "• USDUAH/EURUAH — official NBU rate\n\n"
            "When price crosses target — you get a message and the alert auto-disables.",
        )
    )


@router.callback_query(F.data == "alert:add")
@require_accept
async def alert_add(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    lang = get_lang(call.from_user.id)
    await state.set_state(AlertState.waiting_text)
    await call.message.answer(
        i18n(
            lang,
            "✍️ Введіть алерт:\n<code>BTC below 65000</code> або <code>USDUAH above 42</code>\n\nСкасування: <code>menu</code>",
            "✍️ Enter alert:\n<code>BTC below 65000</code> or <code>USDUAH above 42</code>\n\nCancel: <code>menu</code>",
        ),
        reply_markup=ReplyKeyboardRemove(),
    )


@router.callback_query(F.data == "alert:list")
@require_accept
async def alert_list(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    lang = get_lang(call.from_user.id)
    items = list_alerts(call.from_user.id)
    if not items:
        await call.message.answer(i18n(lang, "Поки немає алертів.", "No alerts yet."), reply_markup=main_menu(lang))
        return
    lines = [i18n(lang, "📄 <b>Ваші алерти</b>", "📄 <b>Your alerts</b>")]
    for i, a in enumerate(items, 1):
        status = "✅" if a.get("active") else "⏸"
        lines.append(f"{i}. {status} <b>{a.get('symbol')}</b> {a.get('direction').lower()} <b>{a.get('target')}</b>")
    lines.append(
        i18n(
            lang,
            "\nЩоб вимкнути: напишіть <code>off N</code> (наприклад <code>off 1</code>)",
            "\nTo disable: send <code>off N</code> (e.g. <code>off 1</code>)",
        )
    )
    await call.message.answer("\n".join(lines), reply_markup=main_menu(lang))


@router.message(AlertState.waiting_text)
@require_accept
async def alert_input(message: Message, state: FSMContext) -> None:
    lang = get_lang(message.from_user.id)
    q = (message.text or "").strip()
    if q.lower() in {"menu", "меню", "cancel", "відміна", "назад"} or q in menu_texts_all():
        await state.clear()
        await message.answer(i18n(lang, "Скасовано ✅", "Canceled ✅"), reply_markup=main_menu(lang))
        return

    parsed = parse_alert_input(q)
    if not parsed:
        await message.answer(
            i18n(
                lang,
                "❌ Невірний формат.\nПриклад: <code>BTC below 65000</code> або <code>USDUAH above 42</code>",
                "❌ Wrong format.\nExample: <code>BTC below 65000</code> or <code>USDUAH above 42</code>",
            )
        )
        return

    symbol, direction, target = parsed
    add_alert(message.from_user.id, symbol, direction, target)
    await state.clear()
    await message.answer(i18n(lang, "✅ Алерт додано.", "✅ Alert added."), reply_markup=main_menu(lang))


@router.message(F.text.regexp(r"^(off|OFF)\s+\d+$"))
@require_accept
async def alert_off(message: Message, state: FSMContext) -> None:
    await state.clear()
    lang = get_lang(message.from_user.id)
    try:
        idx = int((message.text or "").split()[1]) - 1
    except Exception:
        await message.answer(i18n(lang, "Формат: <code>off 1</code>", "Format: <code>off 1</code>"), reply_markup=main_menu(lang))
        return
    ok = deactivate_alert(message.from_user.id, idx)
    await message.answer(
        i18n(lang, "✅ Вимкнено." if ok else "❌ Не знайдено.", "✅ Disabled." if ok else "❌ Not found."),
        reply_markup=main_menu(lang),
    )


# -------------------- Menu router --------------------
@router.message(F.text)
@require_accept
async def router_menu(message: Message, state: FSMContext) -> None:
    lang = get_lang(message.from_user.id)
    text = (message.text or "").strip()

    if await state.get_state() is not None and text in menu_texts_all():
        await state.clear()

    if text in {UA["LANG"], EN["LANG"], "🌐 Language", "🌐 Мова", "Language", "Мова"}:
        await message.answer(i18n(lang, "Оберіть мову:", "Choose language:"), reply_markup=LANG_MENU)
        return

    if text in {UA["HELP"], EN["HELP"], "Help", "Допомога"}:
        await message.answer(help_text(lang), reply_markup=main_menu(lang))
        return

    if text in {UA["P2P"], EN["P2P"], "P2P"}:
        sellers = load_p2p_sellers()
        if not sellers:
            await message.answer(
                i18n(lang, "Поки немає продавців. Додайте в Dashboard.", "No sellers yet. Add via Dashboard."),
                reply_markup=p2p_inline_kb(lang),
            )
            await message.answer(i18n(lang, "Меню 👇", "Menu 👇"), reply_markup=main_menu(lang))
            return
        lines = [i18n(lang, "🤝 <b>P2P продавці</b>", "🤝 <b>P2P sellers</b>")]
        for i, s in enumerate(sellers[:30], 1):
            lines.append(f"{i}. <b>{s.name}</b> — {s.currency} — rate: {s.rate} — limit: {s.limit} — {s.contact}")
        if len(sellers) > 30:
            lines.append(i18n(lang, f"... і ще {len(sellers)-30} (див. Dashboard)", f"... plus {len(sellers)-30} (see Dashboard)"))
        await message.answer("\n".join(lines), reply_markup=p2p_inline_kb(lang))
        await message.answer(i18n(lang, "Меню 👇", "Menu 👇"), reply_markup=main_menu(lang))
        return

    if text in {UA["CONVERT"], EN["CONVERT"], "Конвертер", "Converter"}:
        await state.set_state(ConverterState.waiting_text)
        await message.answer(
            i18n(
                lang,
                "💱 <b>Конвертер</b>\n"
                "Введіть запит у форматі:\n"
                "• <code>0.5 BTC UAH</code>\n"
                "• <code>100 UAH USD</code>\n"
                "• <code>200 USD EUR</code>\n"
                "• <code>BTC UAH</code> (сума = 1)\n\n"
                "Підтримка пар:\n"
                "• BTC/ETH/SOL/USDT ↔ USD/EUR (CoinGecko)\n"
                "• USD/EUR ↔ UAH (НБУ)\n"
                "• USD ↔ EUR (крос-курс НБУ)\n\n"
                "Скасування: <code>menu</code>",
                "💱 <b>Converter</b>\n"
                "Enter query:\n"
                "• <code>0.5 BTC UAH</code>\n"
                "• <code>100 UAH USD</code>\n"
                "• <code>200 USD EUR</code>\n"
                "• <code>BTC UAH</code> (amount = 1)\n\n"
                "Supported pairs:\n"
                "• BTC/ETH/SOL/USDT ↔ USD/EUR (CoinGecko)\n"
                "• USD/EUR ↔ UAH (NBU)\n"
                "• USD ↔ EUR (NBU cross-rate)\n\n"
                "Cancel: <code>menu</code>",
            ),
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if text in {UA["ALERTS"], EN["ALERTS"], "Нагадування", "Alerts", "Reminders"}:
        await state.clear()
        await message.answer(
            i18n(
                lang,
                "⏰ <b>Нагадування (алерти)</b>\nСтвори правило: символ + above/below + ціна.",
                "⏰ <b>Alerts</b>\nCreate rule: symbol + above/below + price.",
            ),
            reply_markup=alerts_kb(lang),
        )
        await message.answer(i18n(lang, "Меню 👇", "Menu 👇"), reply_markup=main_menu(lang))
        return

    if text in {UA["ADVISOR"], EN["ADVISOR"], "Радник", "Advisor"}:
        await state.clear()
        await message.answer(i18n(lang, "⏳ Формую огляд…", "⏳ Building snapshot…"))
        try:
            msg = await get_advisor_text_cached(lang)
            await message.answer(msg, reply_markup=main_menu(lang))
        except Exception:
            await message.answer(
                i18n(lang, "❌ Дані Радника недоступні. Спробуйте пізніше.", "❌ Advisor data is unavailable. Try later."),
                reply_markup=main_menu(lang),
            )
        return

    if text in {UA["FX"], EN["FX"], "FX Market", "Валютний ринок"}:
        await state.clear()
        await message.answer(i18n(lang, "⏳ Завантажую курси…", "⏳ Loading rates…"))
        try:
            msg = await asyncio.wait_for(build_fx_text(lang), timeout=18)
            await message.answer(msg, reply_markup=main_menu(lang))
        except Exception:
            await message.answer(i18n(lang, "❌ Не вдалося отримати дані НБУ.", "❌ Failed to load NBU rates."), reply_markup=main_menu(lang))
        return

    if text in {UA["ANALYTICS"], EN["ANALYTICS"], "Market Analytics", "Аналітика ринку"}:
        await state.clear()
        await message.answer(i18n(lang, "⏳ Формую аналітику…", "⏳ Building analytics…"))
        try:
            msg = await asyncio.wait_for(build_analytics_text(lang), timeout=25)
            await message.answer(msg, reply_markup=main_menu(lang))
        except Exception:
            await message.answer(i18n(lang, "❌ Аналітика тимчасово недоступна.", "❌ Analytics temporarily unavailable."), reply_markup=main_menu(lang))
        return

    if text in {UA["EXCH"], EN["EXCH"], "Exchange Monitor", "Моніторинг бірж"}:
        await state.clear()
        await message.answer(i18n(lang, "⏳ Завантажую котирування…", "⏳ Loading quotes…"))
        try:
            msg = await asyncio.wait_for(build_exchange_monitor_text(lang), timeout=28)
            await message.answer(msg, reply_markup=main_menu(lang))
        except Exception:
            await message.answer(i18n(lang, "❌ Не вдалося отримати дані бірж.", "❌ Failed to fetch exchange data."), reply_markup=main_menu(lang))
        return

    if text in {UA["NEWS"], EN["NEWS"], "News", "Новини"}:
        await state.clear()
        await message.answer(i18n(lang, "⏳ Завантажую новини…", "⏳ Loading news…"))
        try:
            msg = await asyncio.wait_for(build_news_text(lang), timeout=25)
            await message.answer(msg, reply_markup=main_menu(lang))
        except Exception:
            await message.answer(i18n(lang, "❌ Новини тимчасово недоступні.", "❌ News temporarily unavailable."), reply_markup=main_menu(lang))
        return

    await message.answer(
        i18n(lang, "Не зрозумів команду. Натисніть кнопку меню 👇", "I didn't understand. Use the menu buttons 👇"),
        reply_markup=main_menu(lang),
    )


# -------------------- Background alerts checker --------------------
async def alerts_checker() -> None:
    await asyncio.sleep(3)
    while True:
        try:
            items = load_alerts()
            changed = False
            symbols = sorted({a.get("symbol") for a in items if a.get("active")})
            prices: Dict[str, Optional[float]] = {}
            for sym in symbols:
                if not sym:
                    continue
                try:
                    prices[sym] = await get_symbol_price(sym)
                except Exception:
                    prices[sym] = None
                await asyncio.sleep(0.2)

            for a in items:
                if not a.get("active"):
                    continue
                user_id = int(a.get("user_id", 0))
                sym = str(a.get("symbol", "")).upper()
                direction = str(a.get("direction", "")).upper()
                target = float(a.get("target", 0))
                cur = prices.get(sym)
                if cur is None:
                    continue
                hit = (direction == "ABOVE" and cur >= target) or (direction == "BELOW" and cur <= target)
                if hit:
                    a["active"] = False
                    changed = True
                    lang = get_lang(user_id)
                    await bot.send_message(
                        user_id,
                        i18n(
                            lang,
                            f"🔔 <b>Алерт спрацював</b>\n{sym} {direction.lower()} {target}\nПоточна: <b>{cur:.4f}</b>",
                            f"🔔 <b>Alert triggered</b>\n{sym} {direction.lower()} {target}\nCurrent: <b>{cur:.4f}</b>",
                        ),
                        reply_markup=main_menu(lang),
                    )

            if changed:
                save_alerts(items)
        except Exception:
            pass

        await asyncio.sleep(10)


# -------------------- Main --------------------
async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Put BOT_TOKEN in .env")

    t = threading.Thread(target=run_dashboard, daemon=True)
    t.start()

    asyncio.create_task(alerts_checker())

    print("[bot] Starting Telegram polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
