#!/usr/bin/env python3
"""
UC Shop — простой сервер поддержки
Запуск: python3 server.py
Сайт будет на http://localhost:8080
"""

import json
import os
import time
import threading
import urllib.parse
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
import requests

# ============== НАСТРОЙКИ ==============
BOT_TOKEN = "9296789562:AAE2HtIHRyIjT9jDauFxkEh0f_hPTVT0WYk"
ADMIN_CHAT_ID = 7999123043
PORT = int(os.environ.get("PORT", 8080))
DATA_FILE = "tickets.json"
# =======================================

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ticket_id -> list of messages
# message = {"role": "user"|"support", "text": "...", "time": "..."}
tickets = {}
lock = threading.RLock()


def load_data():
    global tickets
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                tickets = json.load(f)
        except Exception:
            tickets = {}


def save_data():
    with lock:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(tickets, f, ensure_ascii=False, indent=2)


def send_tg(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(f"{API}/sendMessage", json=payload, timeout=10)
        return r.json()
    except Exception as e:
        print("TG send error:", e)
        return None


def tg_loop():
    """Long polling — ловим ответы админа"""
    offset = 0
    print("Telegram polling started...")
    while True:
        try:
            r = requests.get(
                f"{API}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=35,
            )
            data = r.json()
            if not data.get("ok"):
                time.sleep(3)
                continue

            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue

                chat_id = msg["chat"]["id"]
                text = (msg.get("text") or "").strip()
                if not text:
                    continue

                # Только админ
                if chat_id != ADMIN_CHAT_ID:
                    continue

                # Команда: /r T123456 текст ответа
                # или просто ответить на сообщение с заявкой
                if text.startswith("/r ") or text.startswith("/reply "):
                    parts = text.split(maxsplit=2)
                    if len(parts) >= 3:
                        tid = parts[1].upper().lstrip("#")
                        reply_text = parts[2]
                        add_support_message(tid, reply_text)
                        send_tg(ADMIN_CHAT_ID, f"✅ Ответ отправлен в заявку #{tid}")
                    else:
                        send_tg(ADMIN_CHAT_ID, "Формат: /r T123456 текст ответа")
                    continue

                # Ответ reply на сообщение бота с заявкой
                reply = msg.get("reply_to_message")
                if reply and reply.get("text"):
                    # Ищем ticket id в тексте
                    import re
                    m = re.search(r"#?(T\d{6})", reply["text"])
                    if m:
                        tid = m.group(1)
                        add_support_message(tid, text)
                        send_tg(ADMIN_CHAT_ID, f"✅ Ответ отправлен в заявку #{tid}")
                        continue

                # Подсказка
                if text in ("/start", "/help"):
                    send_tg(
                        ADMIN_CHAT_ID,
                        "Как отвечать клиенту:\n\n"
                        "1. Ответь <b>reply</b> на сообщение с заявкой\n"
                        "2. Или напиши:\n<code>/r T123456 текст ответа</code>\n\n"
                        "Ответ появится у клиента на сайте.",
                    )

        except Exception as e:
            print("Polling error:", e)
            time.sleep(5)


def add_support_message(ticket_id, text):
    ticket_id = ticket_id.upper()
    with lock:
        if ticket_id not in tickets:
            tickets[ticket_id] = []
        tickets[ticket_id].append({
            "role": "support",
            "text": text,
            "time": time.strftime("%H:%M"),
        })
        save_data()
    print(f"[SUPPORT] #{ticket_id}: {text}")


def add_user_message(ticket_id, name, telegram, case, text):
    ticket_id = ticket_id.upper()
    with lock:
        if ticket_id not in tickets:
            tickets[ticket_id] = []
        tickets[ticket_id].append({
            "role": "user",
            "text": text,
            "name": name,
            "telegram": telegram,
            "case": case,
            "time": time.strftime("%H:%M"),
        })
        save_data()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=os.path.dirname(os.path.abspath(__file__)), **kwargs)

    def log_message(self, format, *args):
        print(f"[HTTP] {args[0]}")

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/messages":
            qs = urllib.parse.parse_qs(parsed.query)
            tid = (qs.get("ticket") or [""])[0].upper()
            with lock:
                msgs = tickets.get(tid, [])
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "messages": msgs}, ensure_ascii=False).encode())
            return

        if parsed.path == "/api/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return

        # Статика
        super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"

        try:
            data = json.loads(body)
        except Exception:
            data = {}

        if parsed.path == "/api/ticket":
            name = (data.get("name") or "").strip()
            telegram = (data.get("telegram") or "").strip()
            case = (data.get("case") or "").strip()
            message = (data.get("message") or "").strip()
            ticket_id = data.get("ticket_id") or f"T{int(time.time()) % 1000000:06d}"

            if not name or not telegram or not case:
                self._json(400, {"ok": False, "error": "Заполни все поля"})
                return

            user_text = f"{name} · {case}"
            if message:
                user_text += f"\n{message}"

            add_user_message(ticket_id, name, telegram, case, user_text)

            # Уведомление админу
            tg_text = (
                f"🆕 <b>Новая заявка</b> #{ticket_id}\n\n"
                f"👤 Имя: {name}\n"
                f"📱 Telegram: {telegram}\n"
                f"💎 Кейс: {case}\n"
                f"💬 Сообщение: {message or '—'}\n\n"
                f"⏰ {time.strftime('%d.%m.%Y %H:%M')}\n\n"
                f"➡️ Ответь <b>reply</b> на это сообщение\n"
                f"или напиши: <code>/r {ticket_id} текст</code>"
            )
            send_tg(ADMIN_CHAT_ID, tg_text)

            self._json(200, {"ok": True, "ticket_id": ticket_id})
            return

        if parsed.path == "/api/message":
            ticket_id = (data.get("ticket_id") or "").upper()
            text = (data.get("text") or "").strip()
            name = data.get("name") or ""
            telegram = data.get("telegram") or ""
            case = data.get("case") or ""

            if not ticket_id or not text:
                self._json(400, {"ok": False, "error": "empty"})
                return

            add_user_message(ticket_id, name, telegram, case, text)

            send_tg(
                ADMIN_CHAT_ID,
                f"💬 <b>Дополнение</b> #{ticket_id}\n\n"
                f"👤 {name} ({telegram})\n"
                f"💎 {case}\n"
                f"📝 {text}",
            )

            self._json(200, {"ok": True})
            return

        self._json(404, {"ok": False, "error": "not found"})

    def _json(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode())


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    load_data()

    # Telegram polling в фоне
    t = threading.Thread(target=tg_loop, daemon=True)
    t.start()

    # Приветствие админу
    send_tg(
        ADMIN_CHAT_ID,
        "✅ <b>UC Shop сервер запущен</b>\n\n"
        "Как отвечать клиенту:\n"
        "• Сделай <b>Reply</b> на сообщение с заявкой\n"
        "• Или напиши: <code>/r T123456 текст ответа</code>\n\n"
        "Ответ сразу появится у клиента на сайте.",
    )

    server = ThreadedServer(("0.0.0.0", PORT), Handler)
    print(f"\n🚀 UC Shop запущен: http://localhost:{PORT}")
    print(f"   Открой index.html через этот адрес\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлен.")
        save_data()


if __name__ == "__main__":
    main()
