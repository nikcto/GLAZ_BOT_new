"""
ntfy_feature.py
================
Модуль push-уведомлений через ntfy.sh — для регионов, где не приходят
стандартные уведомления Telegram.

Как подключить в главном файле бота:

    from ntfy_feature import register_ntfy_handlers, send_ntfy_notification

    # где-то рядом с созданием bot/supabase (после их инициализации):
    register_ntfy_handlers(bot, supabase, safe_send_message)

    # внутри handle_message (business_message_handler), после сохранения
    # сообщения в БД, добавить один вызов:

        preview = message.text if content_type == 'text' else caption
        send_ntfy_notification(
            supabase,
            owner_id,
            get_chat_title(message.chat),
            sender_info['name'],
            preview,
        )

Перед использованием нужно создать таблицу в Supabase:

    create table ntfy_settings (
        user_id       bigint primary key,
        topic         text not null unique,
        enabled       boolean not null default true,
        show_content  boolean not null default true,
        created_at    timestamptz not null default now()
    );
"""

import logging
import secrets
import requests
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

logger = logging.getLogger(__name__)

NTFY_BASE_URL = "https://ntfy.sh"
NTFY_TABLE = "ntfy_settings"


# ---------------------- Данные / бизнес-логика ----------------------

def _generate_topic(user_id: int) -> str:
    """Генерирует уникальный непредсказуемый topic для ntfy.sh.

    ntfy topics по умолчанию публичны (кто знает имя — тот может подписаться
    и читать), поэтому имя должно быть достаточно случайным, чтобы его нельзя
    было подобрать или угадать.
    """
    random_part = secrets.token_hex(10)
    return f"tgwatch-{user_id}-{random_part}"


def get_ntfy_settings(supabase, user_id: int):
    """Возвращает dict с настройками пользователя или None, если ключ не создан."""
    try:
        result = supabase.table(NTFY_TABLE).select("*").eq("user_id", user_id).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error(f"[ntfy] Ошибка получения настроек для {user_id}: {e}")
        return None


def create_ntfy_key(supabase, user_id: int) -> str:
    """Создаёт topic для пользователя (если его ещё нет) и возвращает его."""
    existing = get_ntfy_settings(supabase, user_id)
    if existing:
        return existing["topic"]

    topic = _generate_topic(user_id)
    data = {
        "user_id": user_id,
        "topic": topic,
        "enabled": True,
        "show_content": True,
    }
    try:
        supabase.table(NTFY_TABLE).insert(data).execute()
        logger.info(f"[ntfy] Создан topic для пользователя {user_id}")
    except Exception as e:
        logger.error(f"[ntfy] Ошибка создания ключа для {user_id}: {e}")
    return topic


def set_ntfy_enabled(supabase, user_id: int, enabled: bool):
    try:
        supabase.table(NTFY_TABLE).update({"enabled": enabled}).eq("user_id", user_id).execute()
    except Exception as e:
        logger.error(f"[ntfy] Ошибка обновления enabled для {user_id}: {e}")


def set_ntfy_show_content(supabase, user_id: int, show_content: bool):
    try:
        supabase.table(NTFY_TABLE).update({"show_content": show_content}).eq("user_id", user_id).execute()
    except Exception as e:
        logger.error(f"[ntfy] Ошибка обновления show_content для {user_id}: {e}")


def send_ntfy_notification(supabase, user_id: int, chat_title: str, sender_name: str, preview: str = None):
    """
    Отправляет push-уведомление через ntfy.sh, если у владельца включена
    эта функция. Ничего не делает, если ключ не создан или уведомления выключены.
    Вызывать из обработчика входящих сообщений бизнес-чата (не блокирует —
    просто HTTP-запрос с коротким таймаутом).
    """
    settings = get_ntfy_settings(supabase, user_id)
    if not settings or not settings.get("enabled"):
        return

    topic = settings.get("topic")
    if not topic:
        return

    title = f"Новое сообщение: {chat_title}"

    if settings.get("show_content") and preview:
        # ntfy ожидает данные в теле запроса как обычный текст/байты
        body_text = f"{sender_name}: {preview}"
    else:
        body_text = f"Новое сообщение от {sender_name}"

    try:
        requests.post(
            f"{NTFY_BASE_URL}/{topic}",
            data=body_text.encode("utf-8"),
            headers={
                # non-ASCII значения заголовков ntfy принимает в UTF-8 байтах
                "Title": title.encode("utf-8"),
                "Priority": "default",
                "Tags": "speech_balloon",
            },
            timeout=5,
        )
    except requests.RequestException as e:
        logger.warning(f"[ntfy] Не удалось отправить уведомление для {user_id}: {e}")


# ---------------------- UI (меню /ntfy) ----------------------

def _build_menu(settings):
    keyboard = InlineKeyboardMarkup(row_width=1)

    if not settings:
        text = (
            "🔔 <b>Уведомления через ntfy</b>\n\n"
            "Если в вашем регионе не приходят стандартные уведомления Telegram, "
            "можно получать их через бесплатное приложение <b>ntfy</b>.\n\n"
            "Нажмите «Создать ключ», чтобы начать."
        )
        keyboard.add(InlineKeyboardButton("🔑 Создать ключ", callback_data="ntfy_create"))
        return text, keyboard

    topic = settings["topic"]
    enabled = settings.get("enabled", True)
    show_content = settings.get("show_content", True)

    text = (
        "🔔 <b>Уведомления через ntfy</b>\n\n"
        f"Ваш персональный topic:\n<code>{topic}</code>\n\n"
        "Как подключить:\n"
        "1. Установите приложение ntfy (Android/iOS) или откройте ntfy.sh в браузере\n"
        f"2. Подпишитесь на topic из сообщения выше\n\n"
        f"Статус: {'🟢 включены' if enabled else '🔴 отключены'}\n"
        f"Содержимое в уведомлении: {'✅ показывается' if show_content else '🙈 скрыто'}"
    )

    keyboard.add(
        InlineKeyboardButton(
            "🔕 Выключить уведы" if enabled else "🔔 Включить уведы",
            callback_data="ntfy_toggle_enabled",
        )
    )
    keyboard.add(
        InlineKeyboardButton(
            "🙈 Скрыть содержимое" if show_content else "👁 Показывать содержимое",
            callback_data="ntfy_toggle_content",
        )
    )
    return text, keyboard


def register_ntfy_handlers(bot, supabase, safe_send_message=None):
    """
    Регистрирует хендлер команды /ntfy и её callback'и на переданном экземпляре
    бота. Вызвать один раз при старте бота, например сразу после создания
    объектов bot и supabase в главном файле:

        from ntfy_feature import register_ntfy_handlers, send_ntfy_notification
        register_ntfy_handlers(bot, supabase, safe_send_message)
    """

    send = safe_send_message or bot.send_message

    @bot.message_handler(commands=['ntfy'])
    def handle_ntfy_command(message):
        try:
            user_id = message.from_user.id
            settings = get_ntfy_settings(supabase, user_id)
            text, keyboard = _build_menu(settings)
            send(message.chat.id, text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as e:
            logger.error(f"[ntfy] Ошибка в команде /ntfy: {e}", exc_info=True)
            send(message.chat.id, "⚠️ Произошла ошибка")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('ntfy_'))
    def handle_ntfy_callback(call):
        try:
            user_id = call.from_user.id

            if call.data == 'ntfy_create':
                create_ntfy_key(supabase, user_id)

            elif call.data == 'ntfy_toggle_enabled':
                settings = get_ntfy_settings(supabase, user_id)
                if settings:
                    set_ntfy_enabled(supabase, user_id, not settings.get("enabled", True))

            elif call.data == 'ntfy_toggle_content':
                settings = get_ntfy_settings(supabase, user_id)
                if settings:
                    set_ntfy_show_content(supabase, user_id, not settings.get("show_content", True))

            settings = get_ntfy_settings(supabase, user_id)
            text, keyboard = _build_menu(settings)
            bot.edit_message_text(
                text,
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            bot.answer_callback_query(call.id)

        except Exception as e:
            logger.error(f"[ntfy] Ошибка в callback ntfy: {e}", exc_info=True)
            try:
                bot.answer_callback_query(call.id, "⚠️ Произошла ошибка", show_alert=True)
            except Exception:
                pass
