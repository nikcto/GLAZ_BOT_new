"""Telegram transport helpers shared by handlers."""

import logging
import requests

from telebot.apihelper import ApiTelegramException


logger = logging.getLogger(__name__)


def safe_send_message(bot, chat_id, text, parse_mode=None, **kwargs):
    """Send a message without allowing Telegram/network errors to escape."""
    kwargs.setdefault("disable_web_page_preview", True)
    try:
        return bot.send_message(chat_id, text, parse_mode=parse_mode, **kwargs)
    except ApiTelegramException as exc:
        if exc.error_code == 403:
            logger.warning("Bot was blocked by user %s", chat_id)
        else:
            logger.error("Telegram API error sending message to %s: %s", chat_id, exc)
    except (ConnectionError, requests.exceptions.ConnectionError, OSError) as exc:
        logger.warning("Network error sending message to %s: %s", chat_id, exc)
    except Exception as exc:
        logger.error("Error sending message to %s: %s", chat_id, exc)
    return None
