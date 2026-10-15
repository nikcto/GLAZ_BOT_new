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


reminder_store = ReminderStore()
