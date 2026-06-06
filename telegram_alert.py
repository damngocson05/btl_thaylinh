import requests
from typing import Optional


class TelegramNotifier:
    def __init__(self, bot_token: Optional[str], chat_id: Optional[str]) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = None
        self.session = requests.Session()
        if bot_token and chat_id:
            self.base_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def is_configured(self) -> bool:
        return bool(self.base_url)

    def send_message(self, message: str) -> None:
        if not self.is_configured():
            return
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
        }
        try:
            response = self.session.post(self.base_url, json=payload, timeout=10)
            response.raise_for_status()
        except Exception as exc:
            print(f"Telegram error: {exc}")
