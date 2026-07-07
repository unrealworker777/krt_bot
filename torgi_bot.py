cat > torgi_bot.py << 'PYEOF'
# -*- coding: utf-8 -*-
import os, json, time, requests, urllib3
urllib3.disable_warnings()

TOKEN   = os.environ["TORGI_BOT_TOKEN"]
CHANNEL = os.environ["TELEGRAM_CHANNEL"]
REVIEW  = os.environ["REVIEW_CHAT_ID"]

API = "https://torgi.gov.ru/new/api/public/lotcards/search"
FETCH_EVERY   = 3600
MAX_PER_FETCH = 5
SEEN_FILE    = "seen_torgi.json"
PENDING_FILE = "pending_torgi.json"
STATE_FILE   = "state_torgi.json"

def tg(method, **params):
    return requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=params, timeout=60).json()

def load(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f: return json.load(f)
    return default

def save(path, data):
    with open(path, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)

def fetch_lots():
    r = requests.get(API, params={"text":"комплексное развитие территории",
        "lotStatus":"PUBLISHED,APPLICATIONS_SUBMISSION","page":0,"size":20,
        "sort":"firstVersionPublicationDate,desc"},
        headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"}, timeout=30, verify=False)
    return r.json().get("content", [])

def build_post(lot):
    name = (lot.get("lotName") or "Лот КРТ").strip()
    url = f"https://torgi.gov.ru/new/public/lots/lot/{lot.get('id','')}".replace("&","&amp;")
    chars = {c.get("code"): c.get("characteristicValue") for c in lot.get("characteristics",[]) if c.get("code")}
    lines = ["📍 <b>Аукцион по КРТ</b>", "", f"<b>{name}</b>", ""]
    price = lot.get("priceMin")
    if price: lines.append("💰 Стартовая цена: " + f"{int(price):,}".replace(","," ") + " ₽")
    if chars.get("squareKRT"): lines.append(f"📐 Площадь участка: {chars['squareKRT']} м²")
    if chars.get("CadastralNumberKRT"): lines.append(f"🗺 Кадастровый номер: {chars['CadastralNumberKRT']}")
    end = (lot.get("biddEndTime") or "")[:10]
    if end: lines.append(f"🗓 Приём заявок до: {end}")
    lines += ["", f'📎 <a href="{url}">Лот на ГИС Торги</a>']
    return "\n".join(lines)

def fetch_and_send(seen, pending):
    sent = 0
    for lot in fetch_lots():
        if sent >= MAX_PER_FETCH: break
        lid = str(lot.get("id",""))
        if not lid or lid in seen: continue
        post = build_post(lot)
        resp = tg("sendMessage", chat_id=REVIEW, text=post, parse_mode="HTML",
                  disable_web_page_preview=False,
                  reply_markup={"inline_keyboard":[[
                      {"text":"✅ Опубликовать","callback_data":"pub"},
                      {"text":"🚫 Отклонить","callback_data":"rej"}]]})
        if resp.get("ok"):
            pending[str(resp["result"]["message_id"])] = post
            seen.add(lid); sent += 1
            print("→ Черновик лота отправлен:", (lot.get("lotName") or "")[:50])
        else:
            print("Не смог отправить:", resp)
        time.sleep(1)
    print("Сбор завершён. Новых лотов:", sent)

def handle_callback(cb, pending):
    data = cb.get("data"); msg = cb.get("message",{})
    mid = str(msg.get("message_id")); chat_id = msg.get("chat",{}).get("id")
    post = pending.get(mid)
    if data == "pub" and post:
        r = tg("sendMessage", chat_id=CHANNEL, text=post, parse_mode="HTML", disable_web_page_preview=False)
        if r.get("ok"):
            tg("editMessageText", chat_id=chat_id, message_id=int(mid), text=post+"\n\n✅ ОПУБЛИКОВАНО", parse_mode="HTML")
            pending.pop(mid, None)
            tg("answerCallbackQuery", callback_query_id=cb["id"], text="Опубликовано ✅")
        else:
            tg("answerCallbackQuery", callback_query_id=cb["id"], text=f"Не удалось: {r.get('description','')[:180]}", show_alert=True)
    elif data == "rej":
        tg("editMessageText", chat_id=chat_id, message_id=int(mid), text=(post or "")+"\n\n🚫 ОТКЛОНЕНО", parse_mode="HTML")
        pending.pop(mid, None)
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Отклонено 🚫")
    else:
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Уже обработано")

def main():
    seen = set(load(SEEN_FILE, [])); pending = load(PENDING_FILE, {}); state = load(STATE_FILE, {"offset":0})
    last_fetch = 0
    print("Бот ГИС Торги запущен.")
    while True:
        if time.time() - last_fetch > FETCH_EVERY:
            try: fetch_and_send(seen, pending)
            except Exception as e: print("Ошибка сбора:", e)
            last_fetch = time.time(); save(SEEN_FILE, sorted(seen)); save(PENDING_FILE, pending)
        try:
            upd = tg("getUpdates", offset=state["offset"], timeout=30, allowed_updates=["callback_query"])
            for u in upd.get("result", []):
                state["offset"] = u["update_id"] + 1
                if "callback_query" in u: handle_callback(u["callback_query"], pending)
            save(STATE_FILE, state); save(PENDING_FILE, pending)
        except Exception as e:
            print("Ошибка getUpdates:", e); time.sleep(5)

if __name__ == "__main__":
    main()
PYEOF
