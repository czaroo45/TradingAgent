"""Optional Telegram alerts with durable duplicate suppression."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from urllib.request import Request, urlopen


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, db_path: str, timeout_seconds: float = 8, transport=None):
        if not token or not chat_id or timeout_seconds <= 0:
            raise ValueError("Telegram token/chat id and positive timeout required")
        self._token, self.chat_id, self.db_path, self.timeout = token, chat_id, db_path, timeout_seconds
        self.transport=transport or self._send
        with sqlite3.connect(db_path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS notification_events (fingerprint TEXT PRIMARY KEY, sent_at_ms INTEGER NOT NULL)")

    @classmethod
    def from_env(cls, db_path: str):
        if os.getenv("TRADINGAGENT_TELEGRAM_ENABLED", "false").lower() not in {"1","true","yes","on"}:
            return None
        return cls(os.environ["TRADINGAGENT_TELEGRAM_BOT_TOKEN"],os.environ["TRADINGAGENT_TELEGRAM_CHAT_ID"],
                   db_path,float(os.getenv("TRADINGAGENT_TELEGRAM_TIMEOUT_SECONDS","8")))

    def notify(self, category: str, key: str, message: str, cooldown_seconds: int = 900) -> bool:
        if not category or not key or not message or len(message)>4000:
            raise ValueError("invalid Telegram alert")
        fingerprint=hashlib.sha256(f"{category}\0{key}".encode()).hexdigest()
        now=int(time.time()*1000)
        with sqlite3.connect(self.db_path,timeout=5) as db:
            row=db.execute("SELECT sent_at_ms FROM notification_events WHERE fingerprint=?",(fingerprint,)).fetchone()
            if row and now-row[0] < cooldown_seconds*1000:return False
            db.execute("INSERT OR REPLACE INTO notification_events VALUES (?,?)",(fingerprint,now))
        # Do not log URL, token, or request headers; token is never included in message body.
        body=json.dumps({"chat_id":self.chat_id,"text":message,"disable_web_page_preview":True}).encode()
        try:
            result=self.transport(body,self.timeout)
            if not result.get("ok"):
                raise RuntimeError("Telegram rejected alert")
            return True
        except Exception:
            with sqlite3.connect(self.db_path) as db: db.execute("DELETE FROM notification_events WHERE fingerprint=?",(fingerprint,))
            raise

    def _send(self, body: bytes, timeout: float):
        request=Request(f"https://api.telegram.org/bot{self._token}/sendMessage",data=body,method="POST",
                        headers={"Content-Type":"application/json"})
        with urlopen(request,timeout=timeout) as response:return json.loads(response.read(100_000))

    def notify_signal(self, signal) -> bool:
        scenario=signal.entry_zone
        entry=(f"{scenario[0]:.8g}–{scenario[1]:.8g}" if scenario else "brak")
        message=(f"{signal.direction} · {signal.symbol} · {signal.timestamp.isoformat()} · "
          f"{', '.join(signal.timeframes)}\nStatus: {signal.status} · {signal.strategy}\n"
          f"Wejście: {entry}; SL: {signal.stop_loss}; cele: {', '.join(map(str,signal.targets)) or 'brak'}; "
          f"RR: {signal.risk_reward}\n{'; '.join(signal.explanation[:2])}")
        key=json.dumps({"symbol":signal.symbol,"strategy":signal.strategy,"status":signal.status,
            "direction":signal.direction,"entry":signal.entry_zone,"stop":signal.stop_loss,
            "targets":signal.targets,"timeframes":signal.timeframes},sort_keys=True)
        return self.notify("signal",key,message)


__all__=["TelegramNotifier"]
