"""Volatile message storage.

Message content intentionally never leaves the process. The store is bounded
so a long-running bot cannot grow without limit.
"""

from collections import defaultdict
from threading import RLock
from time import time


class MessageStore:
    def __init__(self, max_messages: int = 10000, ttl_seconds: int = 7 * 24 * 3600):
        self._messages = defaultdict(dict)
        self._lock = RLock()
        self.max_messages = max_messages
        self.ttl_seconds = ttl_seconds

    def set(self, chat_id, message_id, data):
        with self._lock:
            self._messages[chat_id][message_id] = data
            self.cleanup()

    def get(self, chat_id, message_id, default=None):
        with self._lock:
            return self._messages.get(chat_id, {}).get(message_id, default)

    def pop(self, chat_id, message_id, default=None):
        with self._lock:
            chat = self._messages.get(chat_id)
            if not chat:
                return default
            value = chat.pop(message_id, default)
            if not chat:
                self._messages.pop(chat_id, None)
            return value

    def get_chat(self, chat_id):
        with self._lock:
            return dict(self._messages.get(chat_id, {}))

    def cleanup(self):
        cutoff = time() - self.ttl_seconds
        total = sum(len(messages) for messages in self._messages.values())
        for chat_id, messages in list(self._messages.items()):
            for message_id, data in list(messages.items()):
                if data.get("timestamp", 0) < cutoff:
                    messages.pop(message_id, None)
        if total > self.max_messages:
            all_items = sorted(
                ((data.get("timestamp", 0), chat_id, message_id)
                 for chat_id, messages in self._messages.items()
                 for message_id, data in messages.items())
            )
            for _, chat_id, message_id in all_items[:total - self.max_messages]:
                self._messages[chat_id].pop(message_id, None)
