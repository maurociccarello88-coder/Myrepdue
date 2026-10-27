"""Bot Telegram "To-do" pensato per girare su GitHub Actions.
 
Ad ogni esecuzione:
1. legge i messaggi nuovi inviati al bot (getUpdates)
2. aggiorna la lista in todo.json
3. risponde ai comandi
4. dopo le 19:00 (ora di Roma) invia il recap serale, una volta al giorno
"""
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo
 
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
FORCE_RECAP = os.environ.get("FORCE_RECAP", "").lower() == "true"
 
TZ = ZoneInfo("Europe/Rome")
RECAP_HOUR = 19
DB_FILE = "todo.json"
API = f"https://api.telegram.org/bot{TOKEN}"
DEFAULT_CAT = "generale"
 
HELP = (
    "Comandi:\n"
    "/add testo - aggiunge una cosa (anche solo scrivendo il testo)\n"
    "/add #spesa latte - aggiunge con categoria\n"
    "/list - mostra la lista\n"
    "/done 2 - rimuove la voce 2 (anche /done 1 3 4)\n"
    "/clear - svuota la lista\n"
    "/help - questo messaggio\n\n"
    "Ogni riga di un messaggio è una voce separata.\n"
    "Le risposte arrivano a ogni passaggio orario del bot, non subito."
)
 
 
def api(method, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)
 
 
def send(text):
    api("sendMessage", chat_id=CHAT_ID, text=text)
 
 
def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"offset": 0, "last_recap": "", "items": []}
 
 
def save_db(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
 
 
def parse_item(line):
    line = line.strip()
    m = re.match(r"^#(\w+)\s+(.+)$", line)
    if m:
        return {"text": m.group(2).strip(), "cat": m.group(1).lower()}
    return {"text": line, "cat": DEFAULT_CAT}
 
 
def format_list(items, title):
    if not items:
        return "✅ Niente in sospeso, buona serata!"
    groups = {}
    for idx, item in enumerate(items, 1):
        groups.setdefault(item["cat"], []).append((idx, item["text"]))
    lines = [title, ""]
    for cat in sorted(groups, key=lambda c: (c == DEFAULT_CAT, c)):
        lines.append(f"▪ {cat.capitalize()}")
        for idx, text in groups[cat]:
            lines.append(f"  {idx}. {text}")
        lines.append("")
    return "\n".join(lines).strip()
 
 
def add_items(db, raw):
    added = []
    for line in raw.splitlines():
        if line.strip():
            item = parse_item(line)
            db["items"].append(item)
            added.append(item["text"])
    if not added:
        return "Scrivi cosa aggiungere, ad esempio: /add #spesa latte"
    return "Aggiunto: " + "; ".join(added)
 
 
def handle_message(db, text):
    text = text.strip()
    if not text.startswith("/"):
        return add_items(db, text)
 
    parts = text.split(None, 1)
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
 
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/add":
        return add_items(db, arg)
    if cmd == "/list":
        return format_list(db["items"], "📝 La tua lista")
    if cmd == "/done":
        nums = sorted({int(n) for n in re.findall(r"\d+", arg)}, reverse=True)
        valid = [n for n in nums if 1 <= n <= len(db["items"])]
        if not valid:
            return "Indica il numero della voce, ad esempio: /done 2"
        removed = [db["items"].pop(n - 1)["text"] for n in valid]
        return "Fatto: " + "; ".join(reversed(removed))
    if cmd == "/clear":
        db["items"] = []
        return "Lista svuotata."
    return "Comando non riconosciuto. Scrivi /help."
 
 
def process_updates(db):
    result = api(
        "getUpdates",
        offset=db["offset"],
        timeout=0,
        allowed_updates=json.dumps(["message"]),
    )
    for update in result.get("result", []):
        db["offset"] = update["update_id"] + 1
        msg = update.get("message") or {}
        if msg.get("chat", {}).get("id") != CHAT_ID:
            continue  # ignora chiunque non sia tu
        text = msg.get("text")
        if text:
            send(handle_message(db, text))
 
 
def maybe_send_recap(db):
    now = datetime.now(TZ)
    today = now.date().isoformat()
    due = now.hour >= RECAP_HOUR and db.get("last_recap") != today
    if due or FORCE_RECAP:
        send(format_list(db["items"], "📝 To-do di stasera"))
        if due:
            db["last_recap"] = today
 
 
def main():
    db = load_db()
    process_updates(db)
    maybe_send_recap(db)
    save_db(db)
 
 
if __name__ == "__main__":
    main()
