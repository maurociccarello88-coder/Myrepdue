"""Bot Telegram "To-do" + lista della spesa a bottoni, per GitHub Actions.

Ad ogni esecuzione:
1. legge i messaggi e i tocchi sui bottoni arrivati dall'ultima volta (getUpdates)
2. aggiorna le liste in todo.json
3. risponde ai comandi e aggiorna i messaggi con i bottoni (voce toccata = bottone sparito)
4. dopo le 19:00 (ora di Roma) invia il recap serale, una volta al giorno
"""
import json
import os
import re
import urllib.error
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
SHOP_CAT = "spesa"

# tipo di lista -> (chiave dei dati, chiave dell'ultimo messaggio inviato)
KEYS = {"t": ("items", "todo_msg"), "s": ("shopping", "shop_msg")}

HELP = (
    "📝 To-do\n"
    "• scrivi il testo (in modalità to-do) oppure /add testo\n"
    "• /add #categoria testo - con categoria\n"
    "• /list - la lista con un bottone per voce: tocca per segnarla come fatta\n"
    "• /clear - svuota la lista\n\n"
    "🛒 Spesa\n"
    "• Incolla un elenco di ingredienti (anche con punti elenco e quantità): "
    "il bot lo riconosce da solo, toglie le intestazioni e unisce le voci uguali.\n"
    "• /modospesa - da ora ogni messaggio che scrivi va nella spesa\n"
    "• /modotodo - torna al to-do (impostazione di base)\n"
    "• /spesa - la lista con un bottone per prodotto (tocca per toglierlo)\n"
    "• /spesa latte, pane - aggiunge e mostra la lista\n"
    "• #spesa latte - aggiunge alla spesa anche in modalità to-do\n"
    "• /svuotaspesa - svuota la spesa\n\n"
    "Ogni riga di un messaggio è una voce separata. "
    "Le risposte e gli aggiornamenti arrivano a ogni passaggio del bot "
    "(ogni ora circa), non subito. Quando tocchi una voce, al passaggio "
    "successivo ricevi la lista aggiornata in un nuovo messaggio."
)


def api(method, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def api_safe(method, **params):
    """Come api(), ma se Telegram rifiuta la richiesta il bot va avanti."""
    try:
        return api(method, **params)
    except urllib.error.HTTPError as e:
        print(f"{method}: HTTP {e.code} {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        print(f"{method}: {e.reason}")
    return None


def send(text, markup=None):
    params = {"chat_id": CHAT_ID, "text": text}
    if markup:
        params["reply_markup"] = json.dumps(markup)
    return api("sendMessage", **params)


def new_id(db):
    n = db["next_id"]
    db["next_id"] += 1
    return n


def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, encoding="utf-8") as f:
            db = json.load(f)
    else:
        db = {}
    db.setdefault("offset", 0)
    db.setdefault("last_recap", "")
    db.setdefault("items", [])
    db.setdefault("shopping", [])
    db.setdefault("next_id", 1)
    db.setdefault("shop_msg", 0)
    db.setdefault("todo_msg", 0)
    db.setdefault("mode", "todo")
    # voci con categoria "spesa" salvate dalla versione precedente -> lista della spesa
    old = [i for i in db["items"] if i.get("cat") == SHOP_CAT]
    if old:
        db["items"] = [i for i in db["items"] if i.get("cat") != SHOP_CAT]
        add_shopping(db, "\n".join(i["text"] for i in old))
    # le voci del to-do hanno bisogno di un id per i bottoni
    for item in db["items"]:
        if "id" not in item:
            item["id"] = new_id(db)
    return db


def save_db(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def parse_item(line):
    line = line.strip()
    m = re.match(r"^#(\w+)\s+(.+)$", line)
    if m:
        return {"text": m.group(2).strip(), "cat": m.group(1).lower()}
    return {"text": line, "cat": DEFAULT_CAT}


# ---------- riconoscimento ingredienti ----------

# unità riconosciute: nome -> (tipo, fattore verso g / ml)
UNITS = {
    "g": ("g", 1), "gr": ("g", 1), "grammo": ("g", 1), "grammi": ("g", 1),
    "hg": ("g", 100), "etto": ("g", 100), "etti": ("g", 100),
    "kg": ("g", 1000), "chilo": ("g", 1000), "chili": ("g", 1000),
    "ml": ("ml", 1), "millilitri": ("ml", 1), "cl": ("ml", 10), "dl": ("ml", 100),
    "l": ("ml", 1000), "lt": ("ml", 1000), "litro": ("ml", 1000), "litri": ("ml", 1000),
}
QTY = re.compile(r"^(\d+(?:[.,]\d+)?(?:\s*/\s*\d+)?|[½¼¾])")
COMMA_SPLIT = r"\n|(?<!\d),(?!\d)"  # virgola che non è un decimale (1,5)


def to_number(text):
    text = text.replace(" ", "")
    if text in ("½", "¼", "¾"):
        return {"½": 0.5, "¼": 0.25, "¾": 0.75}[text]
    if "/" in text:
        a, b = text.split("/")
        return float(a) / float(b) if float(b) else None
    return float(text.replace(",", "."))


def fmt_num(value):
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    if abs(value - 0.5) < 1e-9:
        return "1/2"
    return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def clean_line(line):
    """Toglie punti elenco, numerazioni, markdown ed emoji iniziali."""
    s = re.sub(r"[*_`]", "", line).strip()
    s = re.sub(r"^\[[ xX]?\]\s*", "", s)   # [ ] [x]
    s = re.sub(r"^\d+[.)]\s+", "", s)      # 1. 2)
    s = re.sub(r"^[^\w½¼¾]+", "", s)       # - • ☐ ✅ 🥚 ...
    s = s.strip(" \t;,")
    if s.endswith(".") and not s.lower().endswith("q.b."):
        s = s[:-1]
    return s.strip()


def is_header(s):
    return s.endswith(":") or bool(
        re.match(r"^(ingredienti|per\s+\d+|dosi\b|porzioni\b)", s, re.I)
    )


def parse_ingredient(chunk):
    """Ritorna {name, key, kind, qty} oppure None se la riga non è un prodotto."""
    s = clean_line(chunk)
    if not s or is_header(s):
        return None
    qty, kind, name = None, "none", s
    m = QTY.match(s)
    if m:
        qty = to_number(m.group(1))
        if not qty:
            return None
        kind = "count"
        name = s[m.end():].strip()
        um = re.match(r"^([A-Za-z]+)\.?\s*(.*)$", name)
        if um and um.group(1).lower() in UNITS and um.group(2).strip():
            kind, factor = UNITS[um.group(1).lower()]
            qty *= factor
            name = um.group(2)
        name = re.sub(r"^(?:di\s+|d['’]\s*)", "", name, flags=re.I).strip(" ,;-")
        if not name:
            return None
    key = re.sub(r"\s+", " ", name.lower()).strip()
    key = re.sub(r"\s*q\.?b\.?$", "", key)
    return {"name": name, "key": key, "kind": kind, "qty": qty}


def format_ingredient(p):
    if p["qty"] is None:
        return p["name"]
    q = p["qty"]
    if p["kind"] == "g":
        return f"{fmt_num(q / 1000)} kg {p['name']}" if q >= 1000 else f"{fmt_num(q)} g {p['name']}"
    if p["kind"] == "ml":
        return f"{fmt_num(q / 1000)} l {p['name']}" if q >= 1000 else f"{fmt_num(q)} ml {p['name']}"
    return f"{fmt_num(q)} {p['name']}"


def looks_like_ingredients(text):
    """True se il messaggio è un elenco di ingredienti incollato."""
    lines = [l for l in text.splitlines() if l.strip()]
    if any(re.match(r"^\W*ingredienti\b", l.strip(), re.I) for l in lines):
        return True
    body = [clean_line(l) for l in lines]
    body = [l for l in body if l and not is_header(l)]
    if len(body) < 3:
        return False
    with_qty = sum(1 for l in body if QTY.match(l))
    return with_qty / len(body) >= 0.5


def explode(raw, split_commas):
    """Divide il testo in righe/prodotti; gestisce 'Ingredienti: farina, uova'."""
    for line in raw.splitlines():
        m = re.match(r"^\W*ingredienti\b[^:\n]*:\s*(.+)$", line.strip(), re.I)
        if m:
            yield from re.split(COMMA_SPLIT, m.group(1))
        elif split_commas:
            yield from re.split(COMMA_SPLIT, line)
        else:
            yield line


# ---------- lista della spesa ----------

def add_shopping(db, raw, split_commas=True):
    """Aggiunge prodotti alla spesa unendo quelli uguali. Ritorna un riepilogo."""
    status, dup = {}, 0
    for chunk in explode(raw, split_commas):
        p = parse_ingredient(chunk)
        if not p:
            continue
        match = None
        for item in db["shopping"]:
            q = parse_ingredient(item["text"])
            if q and q["key"] == p["key"] and q["kind"] == p["kind"]:
                match = (item, q)
                break
        if match is None:
            item = {"id": new_id(db), "text": format_ingredient(p)}
            db["shopping"].append(item)
            status[item["id"]] = "added"
        elif p["qty"] is None:
            dup += 1  # stesso prodotto senza quantità: già in lista
        else:
            item, q = match
            q["qty"] += p["qty"]
            item["text"] = format_ingredient(q)
            status.setdefault(item["id"], "merged")
    by_id = {it["id"]: it["text"] for it in db["shopping"]}
    return {
        "added": [by_id[i] for i, s in status.items() if s == "added"],
        "merged": [by_id[i] for i, s in status.items() if s == "merged"],
        "dup": dup,
    }


def shopping_reply(res):
    parts = []
    if res["added"]:
        shown = res["added"][:15]
        more = f"; … e altri {len(res['added']) - 15}" if len(res["added"]) > 15 else ""
        parts.append(f"🛒 Aggiunti {len(res['added'])}: " + "; ".join(shown) + more)
    if res["merged"]:
        parts.append("➕ Quantità sommate: " + "; ".join(res["merged"][:15]))
    if res["dup"]:
        parts.append(f"Già in lista: {res['dup']}")
    return "\n".join(parts)


# ---------- liste a bottoni (to-do e spesa) ----------

def view(db, kind):
    """Testo e tastiera inline di una lista: un bottone per voce."""
    if kind == "s":
        items = db["shopping"]
        empty = "🛒 Lista della spesa vuota ✅"
        title = "🛒 Lista della spesa\nTocca un prodotto per toglierlo."

        def label(it):
            return f"❌ {it['text']}"
    else:
        # raggruppate per categoria, "generale" per ultima (l'ordine resta stabile)
        items = sorted(db["items"], key=lambda i: (i["cat"] == DEFAULT_CAT, i["cat"]))
        empty = "📝 Niente da fare ✅"
        title = "📝 Cose da fare\nTocca una voce quando è fatta."

        def label(it):
            if it["cat"] == DEFAULT_CAT:
                return f"✅ {it['text']}"
            return f"✅ {it['cat'].capitalize()} · {it['text']}"

    if not items:
        return empty, None
    keyboard = [
        [{"text": label(it), "callback_data": f"{kind}:{it['id']}"}]
        for it in items[:100]  # limite Telegram: 100 bottoni
    ]
    return title, {"inline_keyboard": keyboard}


def send_list(db, kind):
    text, markup = view(db, kind)
    result = send(text, markup)
    db[KEYS[kind][1]] = result["result"]["message_id"]


def refresh_lists(db, state):
    """Aggiorna i messaggi già inviati, così le voci toccate spariscono."""
    for kind, (_, msg_key) in KEYS.items():
        if kind in state["resend"]:
            continue  # arriva un nuovo messaggio: quello vecchio resta com'è
        ids = set(state["refresh"][kind])
        if state["dirty"][kind] and db.get(msg_key):
            ids.add(db[msg_key])
        if not ids:
            continue
        text, markup = view(db, kind)
        for message_id in ids:
            api_safe(
                "editMessageText",
                chat_id=CHAT_ID,
                message_id=message_id,
                text=text,
                reply_markup=json.dumps(markup or {"inline_keyboard": []}),
            )


# ---------- messaggi e bottoni ----------

def add_items(db, raw):
    """Voci del to-do; le righe '#spesa ...' vanno nella lista della spesa."""
    todo, shop_lines = [], []
    for line in raw.splitlines():
        line = re.sub(r"^\s*[-•*]\s+", "", line)  # toglie il punto elenco
        if not line.strip():
            continue
        item = parse_item(line)
        if item["cat"] == SHOP_CAT:
            shop_lines.append(item["text"])
        else:
            item["id"] = new_id(db)
            db["items"].append(item)
            todo.append(item["text"])
    parts = []
    if todo:
        parts.append("Aggiunto: " + "; ".join(todo))
    if shop_lines:
        parts.append(shopping_reply(add_shopping(db, "\n".join(shop_lines))))
    return "\n".join(p for p in parts if p) or "Scrivi cosa aggiungere, ad esempio: /add #spesa latte"


def handle_message(db, text, state):
    text = text.strip()
    if not text.startswith("/"):
        if looks_like_ingredients(text):
            return shopping_reply(add_shopping(db, text, split_commas=False)) or "Niente da aggiungere."
        if db["mode"] == "spesa":
            return shopping_reply(add_shopping(db, text)) or "Niente da aggiungere."
        return add_items(db, text)

    parts = text.split(None, 1)
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/add":
        return add_items(db, arg)
    if cmd == "/list":
        state["show"].add("t")  # la lista viene inviata a fine passaggio
        return None
    if cmd == "/clear":
        db["items"] = []
        return "Lista svuotata."
    if cmd == "/spesa":
        if arg.strip():
            add_shopping(db, arg, split_commas=not looks_like_ingredients(arg))
        state["show"].add("s")
        return None
    if cmd == "/svuotaspesa":
        db["shopping"] = []
        return "🛒 Spesa svuotata."
    if cmd == "/modospesa":
        db["mode"] = "spesa"
        return (
            "🛒 Modalità spesa attiva: ogni messaggio che scrivi va nella lista della spesa.\n"
            "/modotodo per tornare al to-do, /add per aggiungere un'attività."
        )
    if cmd == "/modotodo":
        db["mode"] = "todo"
        return "📝 Modalità to-do attiva: i messaggi vanno nella lista delle cose da fare."
    return "Comando non riconosciuto. Scrivi /help."


def handle_callback(db, cq, state):
    msg = cq.get("message") or {}
    if cq.get("from", {}).get("id") != CHAT_ID:
        return
    if msg.get("chat", {}).get("id") != CHAT_ID:
        return
    kind, done = None, None
    m = re.fullmatch(r"([st]):(\d+)", cq.get("data", ""))
    if m:
        kind, item_id = m.group(1), int(m.group(2))
        items = db[KEYS[kind][0]]
        for item in items:
            if item["id"] == item_id:
                items.remove(item)
                done = item["text"]
                break
        if msg.get("message_id"):
            state["refresh"][kind].add(msg["message_id"])
    if done:
        state["resend"].add(kind)  # a fine passaggio arriva la lista aggiornata
        answer = f"{'Tolto' if kind == 's' else 'Fatto'}: {done}"
    else:
        answer = "Già fatto"
    # a volte il tocco è troppo vecchio per una risposta: nessun problema
    api_safe("answerCallbackQuery", callback_query_id=cq["id"], text=answer)


def process_updates(db):
    state = {
        "show": set(),
        "resend": set(),
        "refresh": {"t": set(), "s": set()},
        "dirty": {"t": False, "s": False},
    }
    before = {k: json.dumps(db[KEYS[k][0]]) for k in KEYS}
    result = api(
        "getUpdates",
        offset=db["offset"],
        timeout=0,
        allowed_updates=json.dumps(["message", "callback_query"]),
    )
    for update in result.get("result", []):
        db["offset"] = update["update_id"] + 1
        if "callback_query" in update:
            handle_callback(db, update["callback_query"], state)
            continue
        msg = update.get("message") or {}
        if msg.get("chat", {}).get("id") != CHAT_ID:
            continue  # ignora chiunque non sia tu
        text = msg.get("text")
        if text:
            reply = handle_message(db, text, state)
            if reply:
                send(reply)
    for k in KEYS:
        state["dirty"][k] = json.dumps(db[KEYS[k][0]]) != before[k]
    return state


def maybe_send_recap(db, sent):
    now = datetime.now(TZ)
    today = now.date().isoformat()
    due = now.hour >= RECAP_HOUR and db.get("last_recap") != today
    if due or FORCE_RECAP:
        if not db["items"]:
            send("✅ Niente in sospeso, buona serata!")
        elif "t" not in sent:
            send_list(db, "t")
        if db["shopping"] and "s" not in sent:
            send_list(db, "s")
        if due:
            db["last_recap"] = today


def main():
    db = load_db()
    state = process_updates(db)
    refresh_lists(db, state)
    # nuovo messaggio con i bottoni: su richiesta, o se non ne esiste ancora uno
    sent = set()
    for kind, (key, msg_key) in KEYS.items():
        if kind in state["show"] or kind in state["resend"] or (
            state["dirty"][kind] and db[key] and not db.get(msg_key)
        ):
            send_list(db, kind)
            sent.add(kind)
    maybe_send_recap(db, sent)
    save_db(db)


if __name__ == "__main__":
    main()