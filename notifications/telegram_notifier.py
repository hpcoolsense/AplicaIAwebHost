# notifications/telegram_notifier.py
import os
import requests
from typing import Optional


class TelegramNotifier:
    """
    Telegram notifier simple:
    - Lee TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID desde .env
    - send(text) devuelve True/False
    - enabled() indica si hay credenciales
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        timeout_s: float = 8.0,
    ):
        self.bot_token = (bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
        self.chat_id = (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
        self.timeout_s = float(timeout_s)

    def enabled(self) -> bool:
        return bool(self.bot_token) and bool(self.chat_id)

    def send(self, text: str, parse_mode: str = "HTML", disable_preview: bool = True) -> bool:
        if not self.enabled():
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview,
        }

        try:
            r = requests.post(url, json=payload, timeout=self.timeout_s)
            if r.status_code != 200:
                return False
            data = r.json()
            return bool(data.get("ok", False))
        except Exception:
            return False