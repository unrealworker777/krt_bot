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
from bs4 import BeautifulSoup
from anthropic import Anthropic

# ----------------------------------------------------------------------------
# 1. НАСТРОЙКИ
#    Секреты читаем из переменных окружения (см. файл .env.example),
#    чтобы токены не лежали прямо в коде.
# ----------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]      # токен от @BotFather
TELEGRAM_CHANNEL   = os.environ["TELEGRAM_CHANNEL"]        # напр. "@my_krt_channel"
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY")   # ключ Claude API

# Режим проверки. True = бот сначала шлёт черновик ТЕБЕ в личку,
# и публикует в канал только после того, как ты поставишь реакцию.
# False = старое поведение: публикует в канал сразу.
REVIEW_MODE = True

# Твой личный chat_id (куда бот шлёт черновики на проверку).
# Узнать: напиши своему боту любое сообщение, потом открой в браузере
# https://api.telegram.org/bot<ТОКЕН>/getUpdates — там будет "chat":{"id": ...}.
# Или перешли любой свой пост боту @userinfobot — он покажет твой id.
REVIEW_CHAT_ID = os.environ.get("REVIEW_CHAT_ID")

# Модель Claude. Haiku — дёшево и быстро для коротких постов.
# Хочешь текст «покрасивее» — поставь "claude-sonnet-4-6".
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# Сколько новостей публиковать за один запуск (чтобы не спамить канал).
MAX_POSTS_PER_RUN = 3

# Файл-память: тут храним ссылки, которые уже опубликовали.
SEEN_FILE = "seen.json"

# Очередь черновиков, ждущих твоей реакции: {message_id: текст поста}.
PENDING_FILE = "pending.json"

# Служебное состояние (offset для чтения реакций из Telegram).
STATE_FILE = "state.json"

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
    # --- Новости (вся пресса) ---
    google_news_rss('КРТ "комплексное развитие территорий"'),
    google_news_rss('"комплексное развитие территорий" застройщик'),
    google_news_rss('договор КРТ торги застройка'),

    # --- Дзен --- (берём через Google News фильтром по сайту dzen.ru)
    google_news_rss('КРТ "комплексное развитие территорий" site:dzen.ru'),

    # Сюда же можно добавить прямые RSS отраслевых порталов, например:
    # "https://ancb.ru/rss",   # проверь, что лента реально существует
]

# --- Телеграм-каналы по КРТ ---
# Просто имена публичных каналов БЕЗ символа @ (как в ссылке t.me/...).
# Бот читает их веб-витрину t.me/s/<имя> — доступ/админка не нужны.
# Впиши сюда реальные каналы, за которыми хочешь следить:
TELEGRAM_SOURCES = [
    # "expert_developer",
    # "krt_russia",
]

# Слова-маркеры: пост из телеграм-канала берём, только если он про КРТ
# (каналы часто пишут и на другие темы — так отсекаем лишнее).
KEYWORDS = ["крт", "комплексное развитие территор"]

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


def load_json(path: str, default):
    """Универсальная загрузка JSON-файла с запасным значением."""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path: str, data) -> None:
    """Универсальное сохранение JSON-файла."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def news_id(entry) -> str:
    """Уникальный отпечаток новости — по ссылке (или заголовку, если ссылки нет)."""
    key = entry.get("link") or entry.get("title", "")
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

# ----------------------------------------------------------------------------
# 4. СБОР НОВОСТЕЙ
# ----------------------------------------------------------------------------

def read_telegram_channel(username: str) -> list:
    """Читает последние посты публичного телеграм-канала через его веб-витрину
    t.me/s/<username>. Никакого доступа/админки не нужно — это открытая страница."""
    items = []
    url = f"https://t.me/s/{username}"
    try:
        html = requests.get(
            url, timeout=30, headers={"User-Agent": "Mozilla/5.0"}
        ).text
    except Exception as e:
        print(f"  Не смог прочитать @{username}: {e}")
        return items

    soup = BeautifulSoup(html, "html.parser")
    for msg in soup.select(".tgme_widget_message_wrap"):
        text_el = msg.select_one(".tgme_widget_message_text")
        link_el = msg.select_one("a.tgme_widget_message_date")
        if not text_el:
            continue
        text = text_el.get_text("\n", strip=True)
        # Берём только посты, где реально речь про КРТ.
        if not any(k in text.lower() for k in KEYWORDS):
            continue
        link = link_el["href"] if (link_el and link_el.has_attr("href")) else url
        items.append({
            "title": text.split("\n")[0][:120],   # первая строка как заголовок
            "summary": text[:1500],
            "link": link,
            "source": f"Telegram @{username}",
        })
    return items


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

    # --- Телеграм-каналы (веб-витрина t.me/s/...) ---
    for username in TELEGRAM_SOURCES:
        for tg in read_telegram_channel(username):
            nid = news_id(tg)
            if nid in seen:
                continue
            tg["id"] = nid
            fresh.append(tg)

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
# 6. ОБЩЕНИЕ С TELEGRAM
# ----------------------------------------------------------------------------

def tg_api(method: str, payload: dict) -> dict:
    """Базовый вызов любого метода Telegram Bot API."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    resp = requests.post(url, json=payload, timeout=60)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram вернул ошибку ({method}): {data}")
    return data


def send_message(chat_id, text: str) -> dict:
    """Отправляет сообщение в указанный чат (канал или личку). Возвращает ответ."""
    return tg_api("sendMessage", {
        "chat_id": chat_id,
        "text": text[:4096],            # лимит Telegram на длину сообщения
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    })


def get_reaction_approvals(offset: int) -> tuple:
    """Читает свежие реакции из Telegram.
    Возвращает (множество одобренных message_id, новый offset).
    Одобрение = на сообщение бота поставили ЛЮБУЮ реакцию."""
    data = tg_api("getUpdates", {
        "offset": offset,
        "timeout": 0,
        "allowed_updates": ["message_reaction"],  # реакции по умолчанию выключены!
    })
    approved = set()
    new_offset = offset
    for upd in data.get("result", []):
        new_offset = upd["update_id"] + 1
        reaction = upd.get("message_reaction")
        if not reaction:
            continue
        # new_reaction непустой = реакцию поставили (а не сняли)
        if reaction.get("new_reaction"):
            approved.add(reaction["message_id"])
    return approved, new_offset

# ----------------------------------------------------------------------------
# 7. ГЛАВНАЯ ЛОГИКА
# ----------------------------------------------------------------------------

def publish_approved():
    """ТАКТ 1: проверяем, что ты одобрил реакцией, и публикуем это в канал."""
    pending = load_json(PENDING_FILE, {})     # {message_id(строка): текст поста}
    state = load_json(STATE_FILE, {"offset": 0})
    if not pending:
        return  # нечего проверять

    approved_ids, new_offset = get_reaction_approvals(state.get("offset", 0))
    state["offset"] = new_offset
    save_json(STATE_FILE, state)

    published = 0
    for mid in list(approved_ids):
        text = pending.get(str(mid))
        if not text:
            continue  # реакция на что-то не из очереди — игнор
        try:
            send_message(TELEGRAM_CHANNEL, text)
            del pending[str(mid)]
            published += 1
            print(f"✓ Одобрено и опубликовано в канал (msg {mid})")
            time.sleep(3)
        except Exception as e:
            print(f"✗ Не смог опубликовать (msg {mid}): {e}")

    save_json(PENDING_FILE, pending)
    if published:
        print(f"Опубликовано одобренных постов: {published}")


def queue_drafts(seen: set):
    """ТАКТ 2: собираем свежие новости и шлём черновики ТЕБЕ на проверку."""
    pending = load_json(PENDING_FILE, {})
    news = collect_news(seen)
    print(f"Найдено новых материалов: {len(news)}")

    sent = 0
    for item in news:
        if sent >= MAX_POSTS_PER_RUN:
            break
        try:
            post_text = make_post(item)
            # шлём черновик в личку на проверку и запоминаем id сообщения
            resp = send_message(REVIEW_CHAT_ID, "🔎 ЧЕРНОВИК (поставь реакцию = публикуем)\n\n" + post_text)
            mid = resp["result"]["message_id"]
            pending[str(mid)] = post_text          # храним чистый текст для канала
            seen.add(item["id"])                   # больше этот материал не берём
            sent += 1
            print(f"→ Черновик отправлен на проверку: {item['title'][:60]}")
            time.sleep(2)
        except Exception as e:
            print(f"✗ Пропустил «{item['title'][:40]}»: {e}")

    save_json(PENDING_FILE, pending)
    save_seen(seen)
    print(f"Отправлено черновиков: {sent}")


def main():
    seen = load_seen()

    if REVIEW_MODE:
        if not REVIEW_CHAT_ID:
            raise SystemExit("Не задан REVIEW_CHAT_ID — некуда слать черновики на проверку.")
        publish_approved()   # сначала публикуем то, что ты уже одобрил
        queue_drafts(seen)   # потом шлём новые черновики
    else:
        # Прямой режим без проверки (как раньше): сразу в канал.
        news = collect_news(seen)
        print(f"Найдено новых материалов: {len(news)}")
        posted = 0
        for item in news:
            if posted >= MAX_POSTS_PER_RUN:
                break
            try:
                send_message(TELEGRAM_CHANNEL, make_post(item))
                seen.add(item["id"])
                posted += 1
                print(f"✓ Опубликовано: {item['title'][:60]}")
                time.sleep(3)
            except Exception as e:
                print(f"✗ Пропустил «{item['title'][:40]}»: {e}")
        save_seen(seen)
        print(f"Готово. Опубликовано постов: {posted}")


if __name__ == "__main__":
    main()
