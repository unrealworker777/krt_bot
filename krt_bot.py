# -*- coding: utf-8 -*-
"""
KRT-bot — собирает новости про КРТ (комплексное развитие территорий),
превращает их в короткие посты и публикует в Telegram-канал.

Логика работы (4 шага):
  1) СБОР      — читаем RSS-ленты источников, достаём свежие новости
  2) ФИЛЬТР    — отбрасываем то, что уже постили (дедупликация)
  3) ТЕКСТ     — превращаем новость в пост через Claude API
  4) ПУБЛИКАЦИЯ — отправляем пост в канал через Telegram Bot API

Запуск:  python krt_bot.py
"""

import os
import json
import time
import hashlib
import urllib.parse

import requests
import feedparser
from anthropic import Anthropic

# ----------------------------------------------------------------------------
# 1. НАСТРОЙКИ
#    Секреты читаем из переменных окружения (см. файл .env.example),
#    чтобы токены не лежали прямо в коде.
# ----------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]      # токен от @BotFather
TELEGRAM_CHANNEL   = os.environ["TELEGRAM_CHANNEL"]        # напр. "@my_krt_channel"
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY")   # ключ Claude API

# Модель Claude. Haiku — дёшево и быстро для коротких постов.
# Хочешь текст «покрасивее» — поставь "claude-sonnet-4-6".
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# Сколько новостей публиковать за один запуск (чтобы не спамить канал).
MAX_POSTS_PER_RUN = 3

# Файл-память: тут храним ссылки, которые уже опубликовали.
SEEN_FILE = "seen.json"

# Голос/стиль канала. Меняй под себя — это и есть «характер» постов.
CHANNEL_VOICE = (
    "Ты — редактор Telegram-канала о девелопменте и КРТ "
    "(комплексное развитие территорий) в России. Аудитория — застройщики, "
    "инвесторы, проектировщики. Пиши по-деловому, без воды и канцелярита, "
    "коротко и по сути."
)

# ----------------------------------------------------------------------------
# 2. ИСТОЧНИКИ
#    Самый надёжный универсальный источник — RSS-поиск Google News по слову.
#    Можно добавить и RSS конкретных отраслевых сайтов, если у них он есть.
# ----------------------------------------------------------------------------

def google_news_rss(query: str) -> str:
    """Собирает корректный URL RSS-поиска Google News на русском."""
    q = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl=ru&gl=RU&ceid=RU:ru"

SOURCES = [
    google_news_rss('КРТ "комплексное развитие территорий"'),
    google_news_rss('"комплексное развитие территорий" застройщик'),
    google_news_rss('договор КРТ торги застройка'),
    # Сюда же можно добавить прямые RSS отраслевых порталов, например:
    # "https://ancb.ru/rss",   # проверь, что лента реально существует
]

# ----------------------------------------------------------------------------
# 3. ПАМЯТЬ (дедупликация)
# ----------------------------------------------------------------------------

def load_seen() -> set:
    """Загружает множество уже опубликованных id новостей."""
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_seen(seen: set) -> None:
    """Сохраняет память на диск."""
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)

def news_id(entry) -> str:
    """Уникальный отпечаток новости — по ссылке (или заголовку, если ссылки нет)."""
    key = entry.get("link") or entry.get("title", "")
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

# ----------------------------------------------------------------------------
# 4. СБОР НОВОСТЕЙ
# ----------------------------------------------------------------------------

def collect_news(seen: set) -> list:
    """Читает все источники и возвращает список НОВЫХ новостей."""
    fresh = []
    for url in SOURCES:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            nid = news_id(entry)
            if nid in seen:
                continue  # уже постили — пропускаем
            fresh.append({
                "id": nid,
                "title": entry.get("title", "").strip(),
                "summary": entry.get("summary", "").strip(),
                "link": entry.get("link", "").strip(),
                "source": entry.get("source", {}).get("title", ""),
            })
    # Чем меньше дублей по заголовку, тем лучше — оставим уникальные заголовки
    unique = {}
    for item in fresh:
        unique.setdefault(item["title"], item)
    return list(unique.values())

# ----------------------------------------------------------------------------
# 5. ТЕКСТ ПОСТА через Claude
# ----------------------------------------------------------------------------

def make_post(item: dict) -> str:
    """Превращает сырую новость в готовый пост для канала."""
    # Если ключа Claude нет — делаем простой пост-заглушку без ИИ.
    if not ANTHROPIC_API_KEY:
        return f"<b>{item['title']}</b>\n\nПодробнее: {item['link']}"

    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""Сделай короткий пост для Telegram-канала по этой новости.

Заголовок: {item['title']}
Краткое описание: {item['summary']}
Ссылка: {item['link']}

Требования к посту:
- 1 цепляющий первый абзац (суть новости в 1–2 предложениях);
- 2–4 предложения, почему это важно застройщику/инвестору в контексте КРТ;
- без выдуманных фактов: опирайся только на заголовок и описание выше;
- объём 400–700 знаков;
- в самом конце строкой дай ссылку на источник: {item['link']}
- разрешённое форматирование — только теги <b> и <i>;
- НЕ добавляй хэштеги и не выдумывай цифры.

Верни ТОЛЬКО текст поста, без пояснений."""

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1000,
        system=CHANNEL_VOICE,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()

# ----------------------------------------------------------------------------
# 6. ПУБЛИКАЦИЯ в Telegram
# ----------------------------------------------------------------------------

def send_to_telegram(text: str) -> dict:
    """Отправляет готовый пост в канал. Бот должен быть админом канала."""
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(api, json={
        "chat_id": TELEGRAM_CHANNEL,
        "text": text[:4096],            # лимит Telegram на длину сообщения
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram вернул ошибку: {data}")
    return data

# ----------------------------------------------------------------------------
# 7. ГЛАВНАЯ ФУНКЦИЯ — связывает всё вместе
# ----------------------------------------------------------------------------

def main():
    seen = load_seen()
    news = collect_news(seen)
    print(f"Найдено новых новостей: {len(news)}")

    posted = 0
    for item in news:
        if posted >= MAX_POSTS_PER_RUN:
            break
        try:
            post_text = make_post(item)
            send_to_telegram(post_text)
            seen.add(item["id"])
            posted += 1
            print(f"✓ Опубликовано: {item['title'][:60]}")
            time.sleep(3)  # маленькая пауза между постами
        except Exception as e:
            print(f"✗ Пропустил «{item['title'][:40]}»: {e}")

    save_seen(seen)
    print(f"Готово. Опубликовано постов: {posted}")

if __name__ == "__main__":
    main()
