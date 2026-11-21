"""Minimal Telegram Bot API client (owner chat only).

Buttons are given as rows of (label, callback_data) or (label, "url:https://...").
callback_data must stay under 64 bytes.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

MAX_UPLOAD_BYTES = 49 * 1024 * 1024      # Bot API upload limit is 50 MB
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024    # Bot API getFile limit is 20 MB

Buttons = list[list[tuple[str, str]]]


def keyboard(buttons: Buttons | None) -> str | None:
    if not buttons:
        return None
    rows = []
    for r in buttons:
        row = []
        for label, data in r:
            if data.startswith("url:"):
                row.append({"text": label, "url": data[4:]})
            else:
                if len(data.encode()) > 64:
                    raise ValueError(f"callback data too long: {data}")
                row.append({"text": label, "callback_data": data})
        rows.append(row)
    return json.dumps({"inline_keyboard": rows})


class TelegramError(RuntimeError):
    pass


class TelegramAPI:
    def __init__(self, token: str, chat_id: str, http: httpx.Client | None = None):
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"
        self.chat_id = str(chat_id)
        self.http = http or httpx.Client(timeout=httpx.Timeout(70, connect=15))

    def call(self, method: str, files: dict | None = None, timeout: float = 30, **params) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        try:
            if files:
                r = self.http.post(f"{self.base}/{method}", data=params, files=files, timeout=300)
            else:
                r = self.http.post(f"{self.base}/{method}", json=params, timeout=timeout)
        except httpx.TransportError as exc:
            raise TelegramError(f"{method}: {exc}") from exc
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code != 200 or not body.get("ok"):
            raise TelegramError(f"{method}: HTTP {r.status_code} {body.get('description', r.text[:200])}")
        return body["result"]

    def send_message(self, text: str, buttons: Buttons | None = None, chat_id: str | None = None) -> dict:
        return self.call("sendMessage", chat_id=chat_id or self.chat_id, text=text[:4000],
                         disable_web_page_preview=True, reply_markup=keyboard(buttons))

    def send_video(self, path: Path, caption: str, buttons: Buttons | None = None) -> dict:
        with path.open("rb") as fh:
            return self.call("sendVideo", files={"video": (path.name, fh, "video/mp4")}, chat_id=self.chat_id,
                             caption=caption[:1000], supports_streaming="true",
                             reply_markup=keyboard(buttons))

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])
        except TelegramError:
            pass  # callbacks expire; never fatal

    def clear_buttons(self, message_id: int) -> None:
        try:
            self.call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=message_id,
                      reply_markup=json.dumps({"inline_keyboard": []}))
        except TelegramError:
            pass

    def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        """Long poll; the HTTP timeout outlives Telegram's own wait."""
        try:
            r = self.http.post(f"{self.base}/getUpdates", timeout=timeout + 20,
                               json={"offset": offset, "timeout": timeout,
                                     "allowed_updates": ["message", "callback_query"]})
        except httpx.TransportError as exc:
            raise TelegramError(f"getUpdates: {exc}") from exc
        body = r.json() if r.content else {}
        if not body.get("ok"):
            raise TelegramError(f"getUpdates: {body.get('description', r.status_code)}")
        return body["result"]

    def download_file(self, file_id: str, dest: Path) -> Path:
        info = self.call("getFile", file_id=file_id)
        if int(info.get("file_size") or 0) > MAX_DOWNLOAD_BYTES:
            raise TelegramError("file is larger than the 20 MB bot download limit")
        r = self.http.get(f"{self.file_base}/{info['file_path']}", timeout=120)
        if r.status_code != 200:
            raise TelegramError(f"file download failed: HTTP {r.status_code}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content[:MAX_DOWNLOAD_BYTES])
        return dest

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        try:
            self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in commands])
        except TelegramError:
            pass
