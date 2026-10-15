"""RAM-only reminders for unanswered Business chat messages."""

import re
import threading
import time


REMINDER_AFTER_SECONDS = 4 * 60 * 60

# Only these phrases are intentionally supported for now.
PROMISE_PHRASES = (
    "ща",
    "сейчас",
    "погоди",
    "минуту",
    "секунду",
    "потом",
    "позже",
    "уточню",
)


class ReminderStore:
    def __init__(self):
        self._pending = {}
        self._disabled = set()
        self._disabled_owners = set()
        self._lock = threading.RLock()

    @staticmethod
    def _normalise(text):
        return re.sub(r"\s+", " ", (text or "").strip().lower())

    def update(self, owner_id, chat_id, is_outgoing, text, timestamp=None):
        """Replace the pending reminder for this conversation with latest state."""
        text = (text or "").strip()
        normalised = self._normalise(text)
        pending = None

        if text:
            if is_outgoing and any(phrase in normalised for phrase in PROMISE_PHRASES):
                pending = {"kind": "promise", "text": text}
            elif not is_outgoing and text.rstrip().endswith("?"):
                pending = {"kind": "question", "text": text}

        with self._lock:
            key = (owner_id, chat_id)
            if owner_id in self._disabled_owners or key in self._disabled:
                self._pending.pop(key, None)
                return
            if pending:
                pending.update({
                    "created_at": timestamp or time.time(),
                    "reminded": False,
                })
                self._pending[key] = pending
            else:
                self._pending.pop(key, None)

    def due(self, now=None):
        now = now or time.time()
        result = []
        with self._lock:
            for key, item in self._pending.items():
                if not item["reminded"] and now - item["created_at"] >= REMINDER_AFTER_SECONDS:
                    item["reminded"] = True
                    result.append((key[0], key[1], dict(item)))
        return result

    def dismiss(self, owner_id, chat_id):
        with self._lock:
            self._pending.pop((owner_id, chat_id), None)

    def disable(self, owner_id, chat_id):
        with self._lock:
            key = (owner_id, chat_id)
            self._disabled.add(key)
            self._pending.pop(key, None)

    def is_enabled(self, owner_id):
        with self._lock:
            return owner_id not in self._disabled_owners

    def set_enabled(self, owner_id, enabled):
        with self._lock:
            if enabled:
                self._disabled_owners.discard(owner_id)
            else:
                self._disabled_owners.add(owner_id)
                for key in list(self._pending):
                    if key[0] == owner_id:
                        self._pending.pop(key, None)

    def snooze(self, owner_id, chat_id, seconds=4 * 60 * 60):
        with self._lock:
            item = self._pending.get((owner_id, chat_id))
            if item:
                item["created_at"] = time.time() + seconds
                item["reminded"] = False



reminder_store = ReminderStore()
