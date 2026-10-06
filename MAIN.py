import telebot
from telebot import types
import logging
from logging.handlers import RotatingFileHandler
from collections import defaultdict
import os
from datetime import datetime, timedelta
from supabase import create_client, Client
from dotenv import load_dotenv
from html import escape
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import matplotlib
matplotlib.use('Agg')  # Устанавливаем бэкенд Agg для работы без GUI
import matplotlib.pyplot as plt
import io
import time
import threading
import requests
from io import BytesIO
from telebot.apihelper import ApiTelegramException
from referral import (
    generate_referral_link,
    get_referral_stats,
    get_connected_referrals_count,
    process_referral,
    complete_referral_on_connection,
    get_user_profile
)
from contest import (
    get_active_contest,
    create_contest,
    is_user_connected,
    is_participant,
    register_participant,
    get_contest_stats,
    format_contest_conditions,
)
from ntfy_feature import register_ntfy_handlers, send_ntfy_notification
import sys
sys.path.append('./Working')


# Настройка логирования
def setup_logging():
    logger = logging.getLogger()
    logger.setLevel(logging.WARNING)

    # Отключаем логи HTTP-запросов
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Только вывод в консоль
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

setup_logging()
logger = logging.getLogger(__name__)

# Глобальный обработчик исключений для всех обработчиков telebot
def handle_telebot_exception(func):
    """Декоратор для обработки сетевых ошибок в обработчиках telebot"""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (ConnectionError, requests.exceptions.ConnectionError, OSError) as e:
            logger.warning(f"Network error in {func.__name__}: {str(e)}")
            return None
        except ApiTelegramException as e:
            if e.error_code == 403:
                logger.warning(f"Bot was blocked in {func.__name__}")
            else:
                logger.error(f"Telegram API error in {func.__name__}: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in {func.__name__}: {type(e).__name__}: {str(e)}", exc_info=True)
            return None
    return wrapper

# Загрузка переменных окружения
load_dotenv('ton.env')  # Supabase, ADMIN_ID, ЮKassa — как и раньше, отсюда

BOT_TOKEN = os.getenv("BOT_TOKEN")  # а токен теперь только из переменных окружения хоста
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в переменных окружения хоста")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
ADMIN_ID = int(os.getenv("ADMIN_ID"))
# Глобальные переменные
business_connection_owners = {}
messages_log = defaultdict(dict)
active_users = set()
cached_active_users_list = []
active_users_cache_date = None

# Добавляем словарь для отслеживания состояния админа
admin_states = {
    'waiting_for_broadcast': False,
    'waiting_for_individual_message': False,
    'selected_user_id': None,
    'waiting_for_support': False,
    'waiting_for_reply': False,
    'reply_to_user_id': None,
    'konk_step': None,
    'konk_data': {},
}


def reset_konk_state():
    admin_states['konk_step'] = None
    admin_states['konk_data'] = {}


def try_register_for_contest(user_id: int):
    """Регистрирует пользователя в активном конкурсе, если выполнены условия."""
    contest = get_active_contest()
    if not contest:
        return None
    if is_participant(contest['id'], user_id):
        return None
    if not is_user_connected(user_id):
        return None

    connected = get_connected_referrals_count(user_id, since=contest['created_at'])
    if connected < contest['required_referrals']:
        return None

    return register_participant(contest['id'], user_id)


def notify_contest_participation(user_id: int, participant_number: int):
    safe_send_message(
        user_id,
        f"🎉 <b>Поздравляем!</b>\n\n"
        f"Вы выполнили условия розыгрыша Telegram Premium и участвуете в нём!\n"
        f"🎫 Ваш номер: <b>#{participant_number}</b>\n\n"
        f"Удачи! 🍀"
    )


def handle_referral_connection(user_id: int, username: str):
    """Обрабатывает подключение бота: реферал + проверка конкурса."""
    result = complete_referral_on_connection(user_id)
    if result:
        referrer_id = result['referrer_id']
        try:
            safe_send_message(
                referrer_id,
                f"🎉 Пользователь @{username} подключил бота по вашей ссылке!"
            )
            participant_number = try_register_for_contest(referrer_id)
            if participant_number:
                notify_contest_participation(referrer_id, participant_number)
        except Exception as e:
            logger.error(f"Error notifying referrer {referrer_id}: {e}")

    participant_number = try_register_for_contest(user_id)
    if participant_number:
        notify_contest_participation(user_id, participant_number)


def update_user_data(user_id: int, username: str, is_connected: bool = False):
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        user_data = {
            "user_id": user_id,
            "username": username,
            "is_connected": is_connected,
            "connection_date": now if is_connected else None,
            "first_seen": now,
            "notify_self": True
        }

        result = supabase.table("users").upsert(user_data, on_conflict="user_id").execute()
        logger.info(f"User updated: {user_id} ({username}) - Connected: {is_connected}")
        return True
    except Exception as e:
        logger.error(f"Error updating user {user_id}: {str(e)}", exc_info=True)
        return False


def get_notify_setting(user_id: int) -> bool:
    try:
        result = supabase.table("users").select("notify_self").eq("user_id", user_id).execute()
        return result.data[0]["notify_self"] if result.data else True
    except Exception as e:
        logger.error(f"Error getting notify setting for {user_id}: {str(e)}", exc_info=True)
        return True


def safe_send_message(chat_id, text, parse_mode=None, **kwargs):
    """Безопасная отправка сообщения с обработкой ошибки 403 (бот заблокирован) и сетевых ошибок"""
    kwargs.setdefault('disable_web_page_preview', True)
    try:
        return bot.send_message(chat_id, text, parse_mode=parse_mode, **kwargs)
    except ApiTelegramException as e:
        if e.error_code == 403:
            logger.warning(f"Bot was blocked by user {chat_id}")
            return None
        else:
            logger.error(f"Telegram API error sending message to {chat_id}: {str(e)}")
            return None
    except (ConnectionError, requests.exceptions.ConnectionError, OSError) as e:
        logger.warning(f"Network error sending message to {chat_id}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Error sending message to {chat_id}: {type(e).__name__}: {str(e)}")
        return None


# Регистрируем хендлеры /ntfy — здесь, т.к. на этом месте уже определены
# bot, supabase и safe_send_message, которые требуются этой функции.
register_ntfy_handlers(bot, supabase, safe_send_message)


def safe_send_photo(chat_id, photo, caption=None, **kwargs):
    """Безопасная отправка фото с обработкой ошибки 403 (бот заблокирован) и сетевых ошибок"""
    try:
        return bot.send_photo(chat_id, photo, caption=caption, **kwargs)
    except ApiTelegramException as e:
        if e.error_code == 403:
            logger.warning(f"Bot was blocked by user {chat_id}")
            return None
        else:
            logger.error(f"Telegram API error sending photo to {chat_id}: {str(e)}")
            return None
    except (ConnectionError, requests.exceptions.ConnectionError, OSError) as e:
        logger.warning(f"Network error sending photo to {chat_id}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Error sending photo to {chat_id}: {type(e).__name__}: {str(e)}")
        return None


def safe_send_video(chat_id, video, caption=None, **kwargs):
    """Безопасная отправка видео с обработкой ошибки 403 (бот заблокирован) и сетевых ошибок"""
    try:
        return bot.send_video(chat_id, video, caption=caption, **kwargs)
    except ApiTelegramException as e:
        if e.error_code == 403:
            logger.warning(f"Bot was blocked by user {chat_id}")
            return None
        else:
            logger.error(f"Telegram API error sending video to {chat_id}: {str(e)}")
            return None
    except (ConnectionError, requests.exceptions.ConnectionError, OSError) as e:
        logger.warning(f"Network error sending video to {chat_id}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Error sending video to {chat_id}: {type(e).__name__}: {str(e)}")
        return None


def safe_send_voice(chat_id, voice, caption=None, **kwargs):
    """Безопасная отправка голосового сообщения с обработкой ошибки 403 (бот заблокирован) и сетевых ошибок"""
    try:
        return bot.send_voice(chat_id, voice, caption=caption, **kwargs)
    except ApiTelegramException as e:
        if e.error_code == 403:
            logger.warning(f"Bot was blocked by user {chat_id}")
            return None
        else:
            logger.error(f"Telegram API error sending voice to {chat_id}: {str(e)}")
            return None
    except (ConnectionError, requests.exceptions.ConnectionError, OSError) as e:
        logger.warning(f"Network error sending voice to {chat_id}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Error sending voice to {chat_id}: {type(e).__name__}: {str(e)}")
        return None


def safe_send_document(chat_id, document, caption=None, **kwargs):
    """Безопасная отправка документа с обработкой ошибки 403 (бот заблокирован) и сетевых ошибок"""
    try:
        return bot.send_document(chat_id, document, caption=caption, **kwargs)
    except ApiTelegramException as e:
        if e.error_code == 403:
            logger.warning(f"Bot was blocked by user {chat_id}")
            return None
        else:
            logger.error(f"Telegram API error sending document to {chat_id}: {str(e)}")
            return None
    except (ConnectionError, requests.exceptions.ConnectionError, OSError) as e:
        logger.warning(f"Network error sending document to {chat_id}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Error sending document to {chat_id}: {type(e).__name__}: {str(e)}")
        return None


def safe_send_audio(chat_id, audio, caption=None, **kwargs):
    """Безопасная отправка аудио с обработкой ошибки 403 (бот заблокирован) и сетевых ошибок"""
    try:
        return bot.send_audio(chat_id, audio, caption=caption, **kwargs)
    except ApiTelegramException as e:
        if e.error_code == 403:
            logger.warning(f"Bot was blocked by user {chat_id}")
            return None
        else:
            logger.error(f"Telegram API error sending audio to {chat_id}: {str(e)}")
            return None
    except (ConnectionError, requests.exceptions.ConnectionError, OSError) as e:
        logger.warning(f"Network error sending audio to {chat_id}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Error sending audio to {chat_id}: {type(e).__name__}: {str(e)}")
        return None


def safe_send_animation(chat_id, animation, caption=None, **kwargs):
    """Безопасная отправка анимации с обработкой ошибки 403 (бот заблокирован) и сетевых ошибок"""
    try:
        return bot.send_animation(chat_id, animation, caption=caption, **kwargs)
    except ApiTelegramException as e:
        if e.error_code == 403:
            logger.warning(f"Bot was blocked by user {chat_id}")
            return None
        else:
            logger.error(f"Telegram API error sending animation to {chat_id}: {str(e)}")
            return None
    except (ConnectionError, requests.exceptions.ConnectionError, OSError) as e:
        logger.warning(f"Network error sending animation to {chat_id}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Error sending animation to {chat_id}: {type(e).__name__}: {str(e)}")
        return None


def safe_send_video_note(chat_id, video_note, **kwargs):
    """Безопасная отправка кружка с обработкой ошибки 403 (бот заблокирован) и сетевых ошибок"""
    try:
        return bot.send_video_note(chat_id, video_note, **kwargs)
    except ApiTelegramException as e:
        if e.error_code == 403:
            logger.warning(f"Bot was blocked by user {chat_id}")
            return None
        else:
            logger.error(f"Telegram API error sending video_note to {chat_id}: {str(e)}")
            return None
    except (ConnectionError, requests.exceptions.ConnectionError, OSError) as e:
        logger.warning(f"Network error sending video_note to {chat_id}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Error sending video_note to {chat_id}: {type(e).__name__}: {str(e)}")
        return None


def safe_send_sticker(chat_id, sticker, **kwargs):
    """Безопасная отправка стикера с обработкой ошибки 403 (бот заблокирован) и сетевых ошибок"""
    try:
        return bot.send_sticker(chat_id, sticker, **kwargs)
    except ApiTelegramException as e:
        if e.error_code == 403:
            logger.warning(f"Bot was blocked by user {chat_id}")
            return None
        else:
            logger.error(f"Telegram API error sending sticker to {chat_id}: {str(e)}")
            return None
    except (ConnectionError, requests.exceptions.ConnectionError, OSError) as e:
        logger.warning(f"Network error sending sticker to {chat_id}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Error sending sticker to {chat_id}: {type(e).__name__}: {str(e)}")
        return None


def safe_reply_to(message, text, **kwargs):
    """Безопасный ответ на сообщение с обработкой сетевых ошибок"""
    try:
        return bot.reply_to(message, text, **kwargs)
    except ApiTelegramException as e:
        if e.error_code == 403:
            logger.warning(f"Bot was blocked by user {message.chat.id}")
            return None
        else:
            logger.error(f"Telegram API error replying to {message.chat.id}: {str(e)}")
            return None
    except (ConnectionError, requests.exceptions.ConnectionError, OSError) as e:
        logger.warning(f"Network error replying to {message.chat.id}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Error replying to {message.chat.id}: {type(e).__name__}: {str(e)}")
        return None


def safe_answer_callback(call_id, text=None, show_alert=False, **kwargs):
    """Безопасный ответ на callback query с обработкой сетевых ошибок"""
    try:
        return bot.answer_callback_query(call_id, text=text, show_alert=show_alert, **kwargs)
    except ApiTelegramException as e:
        # Callback может быть уже обработан или устарел - это нормально
        if e.error_code in [400, 409]:
            logger.debug(f"Callback query {call_id} already answered or expired")
            return None
        else:
            logger.error(f"Telegram API error answering callback {call_id}: {str(e)}")
            return None
    except (ConnectionError, requests.exceptions.ConnectionError, OSError) as e:
        logger.warning(f"Network error answering callback {call_id}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Error answering callback {call_id}: {type(e).__name__}: {str(e)}")
        return None


def get_connection_owner(bot, connection_id: str) -> int:
    try:
        if connection_id in business_connection_owners:
            return business_connection_owners[connection_id]

        result = supabase.table("business_connections").select("owner_id").eq("connection_id", connection_id).execute()
        if result.data:
            owner_id = result.data[0]["owner_id"]
            business_connection_owners[connection_id] = owner_id
            logger.debug(f"Cached business connection: {connection_id} -> {owner_id}")
            return owner_id

        connection = bot.get_business_connection(connection_id)
        owner_id = connection.user.id

        supabase.table("business_connections").insert({
            "connection_id": connection_id,
            "owner_id": owner_id,
            "created_at": datetime.now().isoformat()
        }).execute()

        business_connection_owners[connection_id] = owner_id
        logger.info(f"New business connection: {connection_id} -> {owner_id}")
        return owner_id

    except Exception as e:
        logger.error(f"Error getting connection owner: {str(e)}", exc_info=True)
        return None


def get_chat_title(chat: telebot.types.Chat) -> str:
    """Возвращает безопасное название чата с HTML-экранированием"""
    try:
        if chat.type == "private":
            return escape(chat.first_name or "Приватный чат")
        return escape(chat.title) if chat.title else "Без названия"
    except Exception as e:
        logger.error(f"Error getting chat title: {str(e)}")
        return "Неизвестный чат"


def get_user_display_name(user_id: int) -> str:
    """Получить отображаемое имя пользователя (никнейм или ID)"""
    try:
        result = supabase.table("users").select("username").eq("user_id", user_id).execute()
        if result.data and result.data[0].get('username'):
            return f"@{result.data[0]['username']}"
        else:
            return f"User_{user_id}"
    except Exception as e:
        logger.error(f"Error getting user display name for {user_id}: {str(e)}")
        return f"User_{user_id}"


def get_chat_display_name(chat_id: int, owner_id: int) -> str:
    """Получить отображаемое имя чата"""
    try:
        # Сначала пробуем получить информацию о чате из messages_log
        chat_log = messages_log.get(chat_id, {})
        if chat_log:
            # Ищем первое сообщение с информацией о чате
            for msg_data in chat_log.values():
                chat_title = msg_data.get('chat_title')
                if chat_title and chat_title != "Неизвестный чат":
                    return chat_title
        
        # Если не нашли в логах, пробуем определить по owner_id
        # Это может быть приватный чат с пользователем
        owner_display = get_user_display_name(owner_id)
        return f"Чат с {owner_display}"
        
    except Exception as e:
        logger.error(f"Error getting chat display name for {chat_id}: {str(e)}")
        return f"Чат {chat_id}"


def get_sender_type(message, owner_id: int) -> str:
    if hasattr(message, 'from_user') and message.from_user:
        return "🟢 Ваше сообщение" if message.from_user.id == owner_id else "🔴 Сообщение собеседника"
    return "🔴 Сообщение собеседника"


def get_sender_info(message) -> dict:
    user = getattr(message, 'from_user', None)
    if not user:
        return {'name': 'Неизвестно', 'username': None, 'id': None}
    return {
        'name': user.first_name or user.username or f"User_{user.id}",
        'username': user.username,
        'id': user.id,
    }


def format_sender_link(data: dict) -> str:
    name = escape(data.get('sender_name', 'Неизвестно'))
    sender_id = data.get('sender_id')
    if sender_id:
        return f'<a href="tg://user?id={sender_id}">{name}</a>'
    username = data.get('sender_username')
    if username:
        return f'{name} (@{escape(username)})'
    return name


def format_quoted_text(text: str) -> str:
    return f'<blockquote>{escape(text or "")}</blockquote>'


def _format_notification_header(title: str, data: dict) -> str:
    chat_title = escape(data.get('chat_title') or 'Неизвестный чат')
    sender_line = format_sender_link(data)
    sender_type = data.get('sender_type', '')
    return (
        f"━━━━━━━━━━━━━━━━\n"
        f"<b>{title}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💬 Чат: {chat_title}\n"
        f"👤 {sender_line}\n"
        f"{sender_type}\n\n"
    )


def build_deleted_notification(data: dict) -> str:
    notification = (
        f"🗑 Это сообщение было удалено:\n\n"
        f"Отправитель: {format_sender_link(data)}\n"
    )
    if data.get('type') == 'text':
        notification += f"Текст:\n{format_quoted_text(data.get('content', ''))}"
    elif data.get('caption'):
        notification += f"Текст:\n{format_quoted_text(data.get('caption', ''))}"
    return notification


def build_edited_notification(data: dict, new_text: str = None, new_caption: str = None) -> str:
    notification = (
        f"♻️ Это сообщение было изменено:\n\n"
        f"Отправитель: {format_sender_link(data)}\n"
    )
    if data.get('type') == 'text':
        notification += f"Было:\n{format_quoted_text(data.get('content', ''))}\n"
        notification += f"Стало:\n{format_quoted_text(new_text or '')}"
    elif data.get('caption') or new_caption:
        notification += f"Было:\n{format_quoted_text(data.get('caption', ''))}\n"
        notification += f"Стало:\n{format_quoted_text(new_caption or '')}"
    return notification


def get_file_info(message):
    content_type = message.content_type
    file_id = None
    caption = getattr(message, 'caption', None)

    if content_type == 'photo':
        file_id = message.photo[-1].file_id
    elif content_type == 'video':
        file_id = message.video.file_id
    elif content_type == 'document':
        file_id = message.document.file_id
    elif content_type == 'animation':
        file_id = message.animation.file_id
    elif content_type == 'voice':
        file_id = message.voice.file_id
    elif content_type == 'sticker':
        file_id = message.sticker.file_id
    elif content_type == 'audio':
        file_id = message.audio.file_id
    elif content_type == 'video_note':
        file_id = message.video_note.file_id
    elif content_type == 'contact':
        file_id = f"{message.contact.phone_number}"

    return content_type, file_id, caption


chat_title_cache = {}


def get_cached_chat_title(chat_id: int) -> str:
    if chat_id not in chat_title_cache:
        try:
            chat = bot.get_chat(chat_id)
            chat_title_cache[chat_id] = get_chat_title(chat)
        except Exception as e:
            logger.error(f"Can't get chat title: {str(e)}")
            return "Unknown"
    return chat_title_cache[chat_id]


@bot.business_message_handler(content_types=[
    'text', 'photo', 'video', 'document', 'animation',
    'voice', 'sticker', 'audio', 'contact', 'video_note'
])
def handle_message(message):
    try:
        logger.debug(f"Raw message data: {message.json}")
        bc_id = message.business_connection_id
        owner_id = get_connection_owner(bot, bc_id)
        if not owner_id:
            logger.warning(f"No owner for business connection: {bc_id}")
            return

        # Проверка подписки удалена

        # Обрабатываем самоуничтожающиеся медиафайлы
        handle_self_destruct_media(message)

        # Определяем тип сообщения
        is_outgoing = get_sender_type(message, owner_id) == "🟢 Ваше сообщение"

        # Обновляем статистику
        update_message_statistics(
            owner_id=owner_id,
            chat_id=message.chat.id,
            is_outgoing=is_outgoing
        )

        # Остальной код обработки сообщения...
        content_type, file_id, caption = get_file_info(message)
        # Сохраняем настоящий file_id для медиафайлов, а не маркер
        content = message.text if content_type == 'text' else file_id

        sender_info = get_sender_info(message)
        messages_log[message.chat.id][message.message_id] = {
            'type': content_type,
            'content': content,  # Теперь это настоящий file_id для медиа
            'timestamp': datetime.now().timestamp(),
            'caption': caption,
            'sender_type': get_sender_type(message, owner_id),
            'sender_name': sender_info['name'],
            'sender_username': sender_info['username'],
            'sender_id': sender_info['id'],
            'chat_title': get_chat_title(message.chat),
            'reply_to_message_id': message.reply_to_message.message_id if message.reply_to_message else None,
            'owner_id': owner_id  # Сохраняем owner_id для каждого сообщения
        }
        
        # Отправляем push-уведомление через ntfy только на входящие сообщения
        # (свои собственные сообщения не дублируем)
        if not is_outgoing:
            media_labels = {
                'photo': '📷 Фото',
                'video': '🎥 Видео',
                'video_note': '⭕ Кружок',
                'voice': '🎤 Голосовое сообщение',
                'audio': '🎵 Аудио',
                'document': '📄 Документ',
                'animation': '🎬 GIF',
                'sticker': '😊 Стикер',
                'contact': '📇 Контакт',
            }

            if content_type == 'text':
                preview = message.text
            else:
                preview = media_labels.get(content_type, 'Сообщение')
                if caption:
                    preview = f"{preview}: {caption}"

            send_ntfy_notification(
                supabase,
                owner_id,
                get_chat_title(message.chat),
                sender_info['name'],
                preview
            )

    except Exception as e:
        logger.error(f"Error handling message: {str(e)}", exc_info=True)


def update_message_statistics(owner_id: int, chat_id: int, is_outgoing: bool):
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        
        # Получаем текущую статистику за сегодня
        stats = supabase.table("daily_statistics") \
            .select("*") \
            .eq("user_id", owner_id) \
            .eq("chat_id", chat_id) \
            .eq("date", today) \
            .execute()

        if stats.data:
            # Обновляем существующую запись
            existing = stats.data[0]
            update_data = {
                "incoming_count": existing['incoming_count'] + (0 if is_outgoing else 1),
                "outgoing_count": existing['outgoing_count'] + (1 if is_outgoing else 0)
            }
            supabase.table("daily_statistics") \
                .update(update_data) \
                .eq("id", existing['id']) \
                .execute()
        else:
            # Создаем новую запись
            new_data = {
            "user_id": owner_id,
            "chat_id": chat_id,
                "date": today,
                "incoming_count": 0 if is_outgoing else 1,
                "outgoing_count": 1 if is_outgoing else 0
            }
            supabase.table("daily_statistics").insert(new_data).execute()

    except Exception as e:
        logger.error(f"Error updating daily statistics: {str(e)}")


def create_stats_keyboard(current_page: int, total_pages: int) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    buttons = []

    # Добавляем кнопку "назад"
    buttons.append(
        InlineKeyboardButton('⬅️', callback_data=f'stats_{current_page - 1}' if current_page > 0 else 'none'))

    # Добавляем счетчик страниц
    buttons.append(InlineKeyboardButton(f'| {current_page + 1}/{total_pages} |', callback_data='current_page'))

    # Добавляем кнопку "вперед"
    buttons.append(InlineKeyboardButton('➡️',
                                        callback_data=f'stats_{current_page + 1}' if current_page < total_pages - 1 else 'none'))

    keyboard.row(*buttons)
    return keyboard


def animate_loading(message_id, chat_id, stop_event):
    """Анимация точек в сообщении о загрузке"""
    dots = 0
    while not stop_event.is_set():
        try:
            dots = (dots + 1) % 4
            loading_text = "Рисуем вашу статистику📊" + "." * dots
            bot.edit_message_text(
                loading_text,
                chat_id,
                message_id
            )
            time.sleep(0.5)
        except Exception as e:
            logger.error(f"Error in loading animation: {str(e)}")
            break

    try:
        user_id = message.from_user.id
        
        # Отправляем сообщение о загрузке
        loading_message = bot.send_message(
            message.chat.id,
            "Рисуем вашу статистику📊",
            parse_mode="HTML"
        )
        
        # Создаем событие для остановки анимации
        stop_animation = threading.Event()
        
        # Запускаем анимацию в отдельном потоке
        animation_thread = threading.Thread(
            target=animate_loading,
            args=(loading_message.message_id, message.chat.id, stop_animation)
        )
        animation_thread.start()
        
        response = ["📊 <b>Ваша статистика:</b>\n"]

        # Получаем данные из Supabase
        stats_data = supabase.table("message_statistics") \
            .select("chat_id, total_messages, incoming, outgoing") \
            .eq("user_id", user_id) \
            .execute()

        # Создаем список чатов с их статистикой
        chat_stats = []
        total_all = 0
        incoming_all = 0
        outgoing_all = 0

        for stat in stats_data.data:
            try:
                chat_info = bot.get_chat(stat['chat_id'])
                chat_title = get_chat_title(chat_info)
            except Exception as e:
                chat_title = f"Неактивированный чат ({stat['chat_id']})"
                logger.debug(f"Can't get chat info: {str(e)}")

            total_messages = stat['incoming'] + stat['outgoing']
            chat_stats.append({
                'title': chat_title,
                'incoming': stat['incoming'],
                'outgoing': stat['outgoing'],
                'total': total_messages
            })

            total_all += stat['total_messages']
            incoming_all += stat['incoming']
            outgoing_all += stat['outgoing']

        # Сортируем чаты по общему количеству сообщений
        chat_stats.sort(key=lambda x: x['total'], reverse=True)

        # Разбиваем на страницы по 6 чатов
        chats_per_page = 6
        total_pages = (len(chat_stats) + chats_per_page - 1) // chats_per_page
        start_idx = page * chats_per_page
        end_idx = start_idx + chats_per_page
        current_page_chats = chat_stats[start_idx:end_idx]

        # Формируем отчет для текущей страницы
        for chat in current_page_chats:
            response.append(
                f"\n👥 <b>Чат:</b> {chat['title']}\n"
                f"• Входящих: {chat['incoming']}\n"
                f"• Исходящих: {chat['outgoing']}\n"
                f"────────────────"
            )

        # Добавляем общую статистику только на первой странице
        nopeact = 'неактивированный чат'
        if page == 0:
            response.append(
                f"\n<b>Итого по всем чатам:</b>\n"
                f"📥 Входящих: {incoming_all}\n"
                f"📤 Исходящих: {outgoing_all}"
                f"\n\n<i>Про чаты с названием {nopeact} читать в /help</i>"
            )

        # Создаем клавиатуру для навигации
        keyboard = create_stats_keyboard(page, total_pages)

        # Останавливаем анимацию
        stop_animation.set()
        animation_thread.join()

        # Редактируем сообщение о загрузке, заменяя его на статистику
        bot.edit_message_text(
            '\n'.join(response),
            loading_message.chat.id,
            loading_message.message_id,
            parse_mode="HTML",
            reply_markup=keyboard
        )

    except Exception as e:
        logger.error(f"Error generating statistics: {str(e)}")
        bot.send_message(message.chat.id, "⚠️ Ошибка получения статистики")


@bot.callback_query_handler(func=lambda call: call.data.startswith('stats_'))
def handle_stats_choice(call):
    try:
        if call.data == 'stats_graph':
            # Создаем новое сообщение для передачи в handle_statistics_gui
            message = call.message
            message.text = "/statistic_gui"  # Имитируем команду
            message.from_user = call.from_user  # Добавляем информацию о пользователе
            handle_statistics_gui(message)
        elif call.data == 'stats_daily':
            # Создаем новое сообщение для передачи в handle_statistic_daily
            message = call.message
            message.text = "/statistic_daily"  # Имитируем команду
            message.from_user = call.from_user  # Добавляем информацию о пользователе
            handle_statistic_daily(message)
            
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, "⚠️ Произошла ошибка при отображении статистики")

@bot.message_handler(commands=['statistic_gui'])
def handle_statistics_gui(message):
    try:
        user_id = message.from_user.id
        
        # Получаем данные за последние 7 дней
        seven_days_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        
        # Получаем статистику за последние 7 дней
        stats_data = supabase.table("daily_statistics") \
            .select("chat_id, incoming_count, outgoing_count") \
            .eq("user_id", user_id) \
            .gte("date", seven_days_ago) \
            .execute()

        if not stats_data.data:
            bot.send_message(message.chat.id, "📊 У вас пока нет сообщений за последние 7 дней")
            return

        # Создаем словарь для подсчета статистики по чатам
        chat_stats = {}
        
        for stat in stats_data.data:
            chat_id = stat['chat_id']
            if chat_id not in chat_stats:
                chat_stats[chat_id] = {'incoming': 0, 'outgoing': 0}
            
            chat_stats[chat_id]['incoming'] += stat['incoming_count']
            chat_stats[chat_id]['outgoing'] += stat['outgoing_count']

        # Преобразуем статистику в список и сортируем по общему количеству сообщений
        chat_stats_list = []
        for chat_id, stats in chat_stats.items():
            try:
                chat_title = get_cached_chat_title(chat_id)
            except Exception:
                chat_title = f"Неизвестный чат [{chat_id}]"

            total_messages = stats['incoming'] + stats['outgoing']
            chat_stats_list.append({
                'title': chat_title,
                'incoming': stats['incoming'],
                'outgoing': stats['outgoing'],
                'total': total_messages
            })

        # Сортируем чаты по общему количеству сообщений
        chat_stats_list.sort(key=lambda x: x['total'], reverse=True)
        
        # Берем топ-10 чатов
        top_10_chats = chat_stats_list[:10]

        # Создаем график со светлым фоном
        plt.style.use('default')
        fig, ax = plt.subplots(figsize=(12, 6))
        
        # Данные для графика
        chat_names = [chat['title'][:20] + '...' if len(chat['title']) > 20 else chat['title'] 
                     for chat in top_10_chats]
        incoming = [chat['incoming'] for chat in top_10_chats]
        outgoing = [chat['outgoing'] for chat in top_10_chats]

        # Создаем столбчатую диаграмму
        x = range(len(chat_names))
        width = 0.35

        # Рисуем столбцы с улучшенным стилем
        bars1 = ax.bar(x, incoming, width, label='Входящие', color='#2ecc71', alpha=0.8)
        bars2 = ax.bar([i + width for i in x], outgoing, width, label='Исходящие', color='#3498db', alpha=0.8)

        # Добавляем значения над столбцами
        ax.bar_label(bars1, padding=3, color='black')
        ax.bar_label(bars2, padding=3, color='black')

        # Настройка графика
        ax.set_xlabel('Чаты', fontsize=10, color='black', labelpad=10)
        ax.set_ylabel('Количество сообщений', fontsize=10, color='black', labelpad=10)
        ax.set_title('Распределение сообщений по чатам за последние 7 дней (Топ-10)', fontsize=12, color='black', pad=20)
        
        # Настраиваем подписи осей
        plt.xticks([i + width/2 for i in x], chat_names, rotation=45, ha='right', color='black')
        plt.yticks(color='black')
        
        # Добавляем легенду
        plt.legend(loc='upper right', facecolor='white', edgecolor='black', labelcolor='black')
        
        # Устанавливаем цвет фона
        ax.set_facecolor('white')
        fig.patch.set_facecolor('white')
        
        # Настраиваем отступы
        plt.tight_layout()

        # Сохраняем график в байтовый поток
        img_stream = io.BytesIO()
        plt.savefig(img_stream, format='png', dpi=300, bbox_inches='tight', 
                   facecolor='white', edgecolor='none')
        img_stream.seek(0)
        plt.close()

        # Отправляем изображение
        bot.send_photo(
            message.chat.id,
            photo=img_stream,
            caption=f"📊 <b>Топ-10 чатов по количеству сообщений за последние 7 дней</b>\n\n<i>Про чаты с названием Unknown читать в /help</i>",
            parse_mode="HTML"
        )

    except Exception as e:
        bot.send_message(message.chat.id, "⚠️ Ошибка при создании визуализации статистики")

@bot.message_handler(commands=['statistic_daily'])
def handle_statistic_daily(message):
    try:
        user_id = message.from_user.id
        
        # Получаем все чаты за последние 7 дней
        seven_days_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        
        stats_data = supabase.table("daily_statistics") \
            .select("chat_id, incoming_count, outgoing_count") \
            .eq("user_id", user_id) \
            .gte("date", seven_days_ago) \
            .execute()

        logger.info(f"Получено {len(stats_data.data) if stats_data.data else 0} записей статистики")

        if not stats_data.data:
            logger.info("Нет данных для отображения")
            bot.send_message(message.chat.id, "📊 У вас пока нет сообщений за последние 7 дней")
            return

        # Считаем общее количество сообщений для каждого чата
        chat_stats = {}
        for stat in stats_data.data:
            chat_id = stat['chat_id']
            if chat_id not in chat_stats:
                chat_stats[chat_id] = 0
            chat_stats[chat_id] += stat['incoming_count'] + stat['outgoing_count']

        logger.info(f"Обработано {len(chat_stats)} чатов")

        # Сортируем чаты по количеству сообщений и берем топ-10
        top_chats = sorted(chat_stats.items(), key=lambda x: x[1], reverse=True)[:10]

        # Получаем названия чатов
        chats = []
        for chat_id, _ in top_chats:
            try:
                chat_info = bot.get_chat(chat_id)
                chat_title = get_chat_title(chat_info)
            except Exception as e:
                chat_title = f"Неизвестный чат ({chat_id})"
                logger.debug(f"Can't get chat info: {str(e)}")

            chats.append({
                'chat_id': chat_id,
                'title': chat_title
            })

        logger.info(f"Создаем клавиатуру для {len(chats)} чатов")

        # Создаем клавиатуру для выбора чата
        keyboard = create_chat_selection_keyboard(chats)
        
        bot.send_message(
            message.chat.id,
            "📊 Выберите чат для просмотра ежедневной статистики:",
            reply_markup=keyboard
        )

        logger.info("Сообщение с выбором чата отправлено")

    except Exception as e:
        logger.error(f"Ошибка в команде statistic_daily: {str(e)}", exc_info=True)
        bot.send_message(message.chat.id, "⚠️ Ошибка при получении статистики")

@bot.callback_query_handler(func=lambda call: call.data.startswith('stats_') or call.data in ['none', 'current_page'])
def handle_stats_pagination(call):
    try:
        if call.data == 'none':
            bot.answer_callback_query(call.id, "Больше страниц нет")
            return

        if call.data == 'current_page':
            # При нажатии на счетчик страниц переходим на последнюю страницу
            user_id = call.from_user.id
            response = ["📊 <b>Ваша статистика:</b>\n"]

            # Получаем данные из Supabase
            stats_data = supabase.table("message_statistics") \
                .select("chat_id, total_messages, incoming, outgoing") \
                .eq("user_id", user_id) \
                .execute()

            # Создаем список чатов с их статистикой
            chat_stats = []
            total_all = 0
            incoming_all = 0
            outgoing_all = 0

            for stat in stats_data.data:
                try:
                    chat_info = bot.get_chat(stat['chat_id'])
                    chat_title = get_chat_title(chat_info)
                except Exception as e:
                    chat_title = f"Удалённый чат ({stat['chat_id']})"
                    logger.debug(f"Can't get chat info: {str(e)}")

                total_messages = stat['incoming'] + stat['outgoing']
                chat_stats.append({
                    'title': chat_title,
                    'incoming': stat['incoming'],
                    'outgoing': stat['outgoing'],
                    'total': total_messages
                })

                total_all += stat['total_messages']
                incoming_all += stat['incoming']
                outgoing_all += stat['outgoing']

            # Сортируем чаты по общему количеству сообщений
            chat_stats.sort(key=lambda x: x['total'], reverse=True)

            # Разбиваем на страницы по 6 чатов
            chats_per_page = 6
            total_pages = (len(chat_stats) + chats_per_page - 1) // chats_per_page
            # Переходим на последнюю страницу
            page = total_pages - 1
            start_idx = page * chats_per_page
            end_idx = start_idx + chats_per_page
            current_page_chats = chat_stats[start_idx:end_idx]

            # Формируем отчет для текущей страницы
            for chat in current_page_chats:
                response.append(
                    f"\n👥 <b>Чат:</b> {chat['title']}\n"
                    f"• Входящих: {chat['incoming']}\n"
                    f"• Исходящих: {chat['outgoing']}\n"
                    f"────────────────"
                )

            # Добавляем общую статистику только на первой странице
            if page == 0:
                response.append(
                    f"\n<b>Итого по всем чатам:</b>\n"
                    f"📥 Входящих: {incoming_all}\n"
                    f"📤 Исходящих: {outgoing_all}"
                )

            # Создаем клавиатуру для навигации
            keyboard = create_stats_keyboard(page, total_pages)

            # Редактируем существующее сообщение
            bot.edit_message_text(
                '\n'.join(response),
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=keyboard
            )

            bot.answer_callback_query(call.id, f"Переход на последнюю страницу ({page + 1}/{total_pages})")
            return

        # Проверяем, что это действительно номер страницы
        try:
            page = int(call.data.split('_')[1])
        except ValueError:
            # Если это не номер страницы, игнорируем
            return

        user_id = call.from_user.id
        response = ["📊 <b>Ваша статистика:</b>\n"]

        # Получаем данные из Supabase
        stats_data = supabase.table("message_statistics") \
            .select("chat_id, total_messages, incoming, outgoing") \
            .eq("user_id", user_id) \
            .execute()

        # Создаем список чатов с их статистикой
        chat_stats = []
        total_all = 0
        incoming_all = 0
        outgoing_all = 0

        for stat in stats_data.data:
            try:
                chat_info = bot.get_chat(stat['chat_id'])
                chat_title = get_chat_title(chat_info)
            except Exception as e:
                chat_title = f"Удалённый чат ({stat['chat_id']})"
                logger.debug(f"Can't get chat info: {str(e)}")

            total_messages = stat['incoming'] + stat['outgoing']
            chat_stats.append({
                'title': chat_title,
                'incoming': stat['incoming'],
                'outgoing': stat['outgoing'],
                'total': total_messages
            })

            total_all += stat['total_messages']
            incoming_all += stat['incoming']
            outgoing_all += stat['outgoing']

        # Сортируем чаты по общему количеству сообщений
        chat_stats.sort(key=lambda x: x['total'], reverse=True)

        # Разбиваем на страницы по 6 чатов
        chats_per_page = 6
        total_pages = (len(chat_stats) + chats_per_page - 1) // chats_per_page
        start_idx = page * chats_per_page
        end_idx = start_idx + chats_per_page
        current_page_chats = chat_stats[start_idx:end_idx]

        # Формируем отчет для текущей страницы
        for chat in current_page_chats:
            response.append(
                f"\n👥 <b>Чат:</b> {chat['title']}\n"
                f"• Входящих: {chat['incoming']}\n"
                f"• Исходящих: {chat['outgoing']}\n"
                f"────────────────"
            )

        # Добавляем общую статистику только на первой странице
        if page == 0:
            response.append(
                f"\n<b>Итого по всем чатам:</b>\n"
                f"📥 Входящих: {incoming_all}\n"
                f"📤 Исходящих: {outgoing_all}"
            )

        # Создаем клавиатуру для навигации
        keyboard = create_stats_keyboard(page, total_pages)

        # Редактируем существующее сообщение
        bot.edit_message_text(
            '\n'.join(response),
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=keyboard
        )

        bot.answer_callback_query(call.id)

    except Exception as e:
        logger.error(f"Error handling stats pagination: {str(e)}")
        bot.answer_callback_query(call.id, "⚠️ Ошибка при переключении страницы")


@bot.edited_business_message_handler(content_types=[
    'text', 'photo', 'video', 'document', 'animation',
    'voice', 'sticker', 'audio', 'contact', 'video_note'
])
def handle_text_edit(message):
    owner_id = None
    try:
        bc_id = message.business_connection_id
        owner_id = get_connection_owner(bot, bc_id)
        if not owner_id:
            return

        # Проверка подписки удалена

        old_data = messages_log[message.chat.id].get(message.message_id, {})
        
        # Проверяем настройку уведомлений
        notify_self = get_notify_setting(owner_id)
        if old_data.get('sender_type') == "🟢 Ваше сообщение" and not notify_self:
            logger.info(f"Пропуск уведомления об изменении собственного сообщения {message.message_id}")
            return

        if not old_data.get('sender_name'):
            sender_info = get_sender_info(message)
            old_data['sender_name'] = sender_info['name']
            old_data['sender_username'] = sender_info['username']
            old_data['sender_id'] = sender_info['id']

        new_content_type, new_file_id, new_caption = get_file_info(message)

        notification = build_edited_notification(
            old_data,
            new_text=message.text if new_content_type == 'text' else None,
            new_caption=new_caption,
        )

        # Если есть старая версия медиа
        if old_data.get('content') and validate_file_id(old_data['content']):
            try:
                # Отправляем старое медиа с объединенным уведомлением
                media_method = {
                    'photo': safe_send_photo,
                    'video': safe_send_video,
                    'document': safe_send_document,
                    'animation': safe_send_animation,
                    'voice': safe_send_voice,
                    'audio': safe_send_audio,
                    'sticker': safe_send_sticker,
                    'video_note': safe_send_video_note
                }.get(old_data['type'], safe_send_message)
                
                if old_data['type'] == 'video_note' or old_data['type'] == 'sticker':
                    # Для video_note и sticker нет caption, отправляем отдельно
                    media_method(owner_id, old_data['content'])
                    safe_send_message(owner_id, notification, parse_mode="HTML")
                else:
                    media_method(
                        owner_id,
                        old_data['content'],
                        caption=notification,
                        parse_mode="HTML"
                    )

            except Exception as e:
                logger.error(f"Ошибка отправки старого медиа: {str(e)}")
                safe_send_message(owner_id, notification + "\n🚫 <i>Не удалось прикрепить файл</i>", parse_mode="HTML")
        else:
            # Если нет старого медиа - отправляем только текст
            safe_send_message(owner_id, notification, parse_mode="HTML")

        # Обновляем кеш новой версией
        messages_log[message.chat.id][message.message_id] = {
            'type': new_content_type,
            'content': message.text if new_content_type == 'text' else new_file_id,
            'caption': new_caption,
            'sender_type': old_data.get('sender_type'),
            'sender_name': old_data.get('sender_name'),
            'sender_username': old_data.get('sender_username'),
            'sender_id': old_data.get('sender_id'),
            'chat_title': old_data.get('chat_title'),
            'owner_id': old_data.get('owner_id'),
            'timestamp': datetime.now().timestamp()
        }

    except Exception as exc:
        logger.error(f"Error handling edit: {str(exc)}", exc_info=True)
        if owner_id:
            error_msg = f"⚠️ Ошибка обработки изменения: {escape(str(exc))}" if exc else "Неизвестная ошибка"
            safe_send_message(owner_id, error_msg, parse_mode="HTML")


@bot.deleted_business_messages_handler()
def handle_delete(deleted):
    try:
        bc_id = deleted.business_connection_id
        owner_id = get_connection_owner(bot, bc_id)
        if not owner_id:
            return

        # Проверка подписки удалена

        notify_self = get_notify_setting(owner_id)

        for msg_id in deleted.message_ids:
            data = messages_log[deleted.chat.id].pop(msg_id, None)
            if not data:
                continue

            if data.get('sender_type') == "🟢 Ваше сообщение" and not notify_self:
                continue

            notification = build_deleted_notification(data)

            try:
                # Для текстовых сообщений
                if data.get('type') == 'text':
                    safe_send_message(owner_id, notification, parse_mode="HTML")
                    continue

                # Для медиа-файлов
                content = data.get('content')
                
                if not content:
                    raise ValueError("Отсутствует содержимое сообщения")

                # Проверяем, является ли content маркером вида ['photo'] или настоящим file_id
                if content.startswith('[') and content.endswith(']'):
                    safe_send_message(owner_id, notification, parse_mode="HTML")
                    continue

                # Проверяем, что content - это настоящий file_id
                if not validate_file_id(content):
                    raise ValueError("Некорректный идентификатор файла")

                if data.get('type') in ['photo', 'video', 'document', 'animation', 'video_note']:
                    send_media = {
                        'photo': safe_send_photo,
                        'video': safe_send_video,
                        'document': safe_send_document,
                        'animation': safe_send_animation,
                        'video_note': safe_send_video_note
                    }[data['type']]

                    # Для video_note отправляем сначала кружок, потом уведомление
                    if data['type'] == 'video_note':
                        send_media(owner_id, content)
                        safe_send_message(owner_id, notification, parse_mode="HTML")
                    else:
                        send_media(owner_id, content, caption=notification, parse_mode="HTML")

                elif data.get('type') in ['voice', 'audio', 'sticker']:
                    send_media = {
                        'voice': safe_send_voice,
                        'audio': safe_send_audio,
                        'sticker': safe_send_sticker
                    }[data['type']]
                    send_media(owner_id, content)
                    safe_send_message(owner_id, notification, parse_mode="HTML")

            except Exception as e:
                logger.error(f"Ошибка при обработке удаленного сообщения {msg_id}: {str(e)}", exc_info=True)
                error_notification = f"⚠️ Не удалось восстановить {data.get('type', 'сообщение')}: {escape(str(e))}"
                if content:
                    error_notification += f"\n🚫 Идентификатор файла: {content}"
                safe_send_message(owner_id, error_notification, parse_mode="HTML")
                safe_send_message(owner_id, notification, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Ошибка при обработке события удаления: {str(e)}", exc_info=True)


def validate_file_id(file_id: str) -> bool:
    """Улучшенная валидация file_id"""
    try:
        if not isinstance(file_id, str):
            return False
        if len(file_id) < 20 or len(file_id) > 255:
            return False
        return all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in file_id)
    except:
        return False


@bot.message_handler(commands=['start'])
def start_command(message):
    try:
        user = message.from_user
        username = user.username or user.first_name or f"User_{user.id}"
        logger.info(f"Command /start from {user.id} ({username})")

        result = supabase.table("users").select("user_id").eq("user_id", user.id).execute()

        if not result.data:
            logger.info(f"New user registered: {user.id} ({username})")
            if not update_user_data(user.id, username):
                logger.error(f"Failed to register user: {user.id}")
            else:
                active_users.add(user.id)

        # Проверяем, является ли это реферальной ссылкой
        args = message.text.split()
        if len(args) > 1 and args[1].startswith('ref_'):
            try:
                referrer_id = int(args[1].split('_')[1])
                if process_referral(user.id, referrer_id):
                    bot.send_message(
                        message.chat.id,
                        "🎁 Вы перешли по реферальной ссылке!\n"
                        "Подключите бота к аккаунту в настройках Telegram Business — "
                        "тогда пригласивший получит засчитанное приглашение.\n"
                        "<i>Telegram Premium для этого не нужен.</i>"
                    )
                    try:
                        safe_send_message(
                            referrer_id,
                            f"👤 @{username} перешёл по вашей ссылке.\n"
                            f"Приглашение засчитается, когда он подключит бота к аккаунту."
                        )
                    except Exception as e:
                        logger.error(f"Error notifying referrer: {str(e)}")
            except Exception as e:
                logger.error(f"Error processing referral link: {str(e)}")

        bot.send_message(
            message.chat.id,
            "Инструкция в описании \n<b>Наблюдаю!👀</b>\n"
            "Отправьте /help для помощи"
        )

    except Exception as e:
        logger.error(f"Error in start_command: {str(e)}", exc_info=True)

@bot.message_handler(commands=['onmy', 'offmy'])
def toggle_notifications(message):
    try:
        user = message.from_user
        # Получаем команду из текста сообщения
        command = message.text.split()[0].lower().replace('/', '')
        new_value = command == 'onmy'

        supabase.table("users").update({"notify_self": new_value}).eq("user_id", user.id).execute()
        status = "включены" if new_value else "отключены"
        logger.info(f"Notifications toggled: {user.id} -> {status}")
        bot.reply_to(message, f"🔔 Уведомления о ваших сообщениях теперь {status}")

    except Exception as e:
        logger.error(f"Error toggling notifications: {str(e)}", exc_info=True)
        bot.reply_to(message, "⚠️ Произошла ошибка при изменении настроек")


@bot.business_connection_handler(func=lambda connection: True)
def handle_business_connection(business_connection):
    try:
        user = business_connection.user
        username = user.username or user.first_name or f"User_{user.id}"

        if business_connection.date > 0:
            logger.info(f"Business connection established: {user.id} ({username})")
            update_user_data(user.id, username, True)
            business_connection_owners[business_connection.id] = user.id
            handle_referral_connection(user.id, username)
        else:
            logger.info(f"Business connection removed: {user.id} ({username})")
            update_user_data(user.id, username, False)
            if business_connection.id in business_connection_owners:
                del business_connection_owners[business_connection.id]

    except Exception as e:
        logger.error(f"Error handling business connection: {str(e)}", exc_info=True)


def split_message(text: str, max_length: int = 4096) -> list:
    return [text[i:i + max_length] for i in range(0, len(text), max_length)]


def get_activated_users_count() -> int:
    """Пользователи, которые хотя бы раз подключали business-аккаунт."""
    try:
        result = supabase.table("business_connections").select("owner_id").execute()
        return len({row["owner_id"] for row in (result.data or [])})
    except Exception as e:
        logger.error(f"Error counting activated users: {str(e)}", exc_info=True)
        return 0


def refresh_active_users_cache(force: bool = False) -> list:
    """Кэш активных пользователей по записям в daily_statistics (обновляется раз в сутки)."""
    global cached_active_users_list, active_users_cache_date

    today = datetime.now().date()
    if not force and active_users_cache_date == today:
        return cached_active_users_list

    try:
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        stats = supabase.table("daily_statistics") \
            .select("user_id") \
            .gte("date", yesterday) \
            .execute()

        active_user_ids = sorted({row["user_id"] for row in (stats.data or [])})

        users_data = supabase.table("users").select("user_id, username").execute()
        users_map = {u["user_id"]: u.get("username") for u in (users_data.data or [])}

        cached_active_users_list = [
            {
                "user_id": user_id,
                "username": users_map.get(user_id) or f"User_{user_id}",
            }
            for user_id in active_user_ids
        ]
        active_users_cache_date = today
        logger.info(f"Active users cache refreshed: {len(cached_active_users_list)} users")
    except Exception as e:
        logger.error(f"Error refreshing active users cache: {str(e)}", exc_info=True)

    return cached_active_users_list


def format_active_users_page(active_users_page: list, page: int, total_pages: int) -> str:
    lines = [f"👥 <b>Активные пользователи</b> (стр. {page + 1}/{total_pages})\n"]
    for user in active_users_page:
        username = escape(user["username"])
        lines.append(f"• {username} — <code>{user['user_id']}</code>")
    return "\n".join(lines)


def create_active_users_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    nav_buttons = []

    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton("⬅️", callback_data=f"admin_stat_active_{page - 1}")
        )

    nav_buttons.append(
        InlineKeyboardButton(f"| {page + 1}/{total_pages} |", callback_data="admin_stat_noop")
    )

    if page < total_pages - 1:
        nav_buttons.append(
            InlineKeyboardButton("➡️", callback_data=f"admin_stat_active_{page + 1}")
        )

    if nav_buttons:
        keyboard.row(*nav_buttons)

    keyboard.add(InlineKeyboardButton("⬅️ К статистике", callback_data="admin_stat_back"))
    return keyboard


def send_active_users_list(chat_id: int, page: int = 0, message_id: int = None):
    active_users_data = refresh_active_users_cache()
    users_per_page = 20

    if not active_users_data:
        text = "📭 Активных пользователей не найдено"
        keyboard = InlineKeyboardMarkup().add(
            InlineKeyboardButton("⬅️ К статистике", callback_data="admin_stat_back")
        )
    else:
        total_pages = max(1, (len(active_users_data) + users_per_page - 1) // users_per_page)
        page = max(0, min(page, total_pages - 1))
        start_idx = page * users_per_page
        end_idx = start_idx + users_per_page
        text = format_active_users_page(active_users_data[start_idx:end_idx], page, total_pages)
        keyboard = create_active_users_keyboard(page, total_pages)

    if message_id:
        bot.edit_message_text(
            text,
            chat_id,
            message_id,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    else:
        bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=keyboard)


@bot.message_handler(commands=['stat'])
def handle_stats(message):
    try:
        if message.from_user.id != ADMIN_ID:
            logger.warning(f"Unauthorized stats access attempt from {message.from_user.id}")
            bot.reply_to(message, "🚫 Доступ запрещен!")
            return

        logger.info(f"Generating stats for admin {ADMIN_ID}")

        activated_count = get_activated_users_count()
        active_users_data = refresh_active_users_cache()
        active_count = len(active_users_data)
        cache_date = active_users_cache_date.strftime("%d.%m.%Y") if active_users_cache_date else "—"

        report = (
            "📊 <b>Статистика</b>\n\n"
            f"Всего активировавших: <b>{activated_count}</b>\n"
            f"Всего активных: <b>{active_count}</b>\n"
            f"<i>Активные обновлены: {cache_date}</i>"
        )

        keyboard = InlineKeyboardMarkup().add(
            InlineKeyboardButton(
                f"👥 Список активных ({active_count})",
                callback_data="admin_stat_active_0",
            )
        )

        bot.send_message(message.chat.id, report, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Error generating stats: {str(e)}", exc_info=True)
        bot.send_message(message.chat.id, f"⚠️ Ошибка: {escape(str(e))}")


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_stat_"))
def handle_admin_stat_callback(call):
    try:
        if call.from_user.id != ADMIN_ID:
            bot.answer_callback_query(call.id, "🚫 Доступ запрещен!", show_alert=True)
            return

        if call.data == "admin_stat_noop":
            bot.answer_callback_query(call.id)
            return

        if call.data == "admin_stat_back":
            activated_count = get_activated_users_count()
            active_users_data = refresh_active_users_cache()
            active_count = len(active_users_data)
            cache_date = active_users_cache_date.strftime("%d.%m.%Y") if active_users_cache_date else "—"

            report = (
                "📊 <b>Статистика</b>\n\n"
                f"Всего активировавших: <b>{activated_count}</b>\n"
                f"Всего активных: <b>{active_count}</b>\n"
                f"<i>Активные обновлены: {cache_date}</i>"
            )
            keyboard = InlineKeyboardMarkup().add(
                InlineKeyboardButton(
                    f"👥 Список активных ({active_count})",
                    callback_data="admin_stat_active_0",
                )
            )
            bot.edit_message_text(
                report,
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            bot.answer_callback_query(call.id)
            return

        if call.data.startswith("admin_stat_active_"):
            page = int(call.data.split("_")[-1])
            send_active_users_list(call.message.chat.id, page=page, message_id=call.message.message_id)
            bot.answer_callback_query(call.id)

    except Exception as e:
        logger.error(f"Error in admin stat callback: {str(e)}", exc_info=True)
        bot.answer_callback_query(call.id, "⚠️ Ошибка", show_alert=True)


@bot.message_handler(commands=['tell'])
def handle_tell_command(message):
    try:
        if message.from_user.id != ADMIN_ID:
            logger.warning(f"Unauthorized tell attempt from {message.from_user.id}")
            bot.reply_to(message, "🚫 Доступ запрещен!")
            return

        admin_states['waiting_for_broadcast'] = True
        bot.reply_to(message, "📢 Отправьте сообщение для рассылки всем пользователям\n"
                              "Поддерживаются текст и фото с подписью\n"
                              "Для отмены используйте команду /stop")

    except Exception as e:
        logger.error(f"Error in tell command: {str(e)}", exc_info=True)
        bot.reply_to(message, "⚠️ Произошла ошибка")


@bot.message_handler(commands=['stop'])
def handle_stop_command(message):
    try:
        if message.from_user.id != ADMIN_ID:
            return

        cancelled = False
        if admin_states.get('waiting_for_broadcast'):
            admin_states['waiting_for_broadcast'] = False
            cancelled = True
            bot.reply_to(message, "✅ Команда рассылки отменена")
            logger.info("Broadcast cancelled by admin")

        if admin_states.get('konk_step'):
            reset_konk_state()
            cancelled = True
            bot.reply_to(message, "✅ Создание конкурса отменено")

        if not cancelled:
            bot.reply_to(message, "ℹ️ Нет активных команд для отмены")

    except Exception as e:
        logger.error(f"Error in stop command: {str(e)}", exc_info=True)
        bot.reply_to(message, "⚠️ Произошла ошибка при отмене команды")


@bot.message_handler(
    func=lambda message: message.from_user.id == ADMIN_ID and admin_states.get('waiting_for_broadcast'),
    content_types=['text', 'photo'])
def handle_broadcast_message(message):
    try:
        admin_states['waiting_for_broadcast'] = False

        # Получаем всех пользователей из базы
        users = supabase.table("users").select("user_id").execute()

        success_count = 0
        fail_count = 0

        for user in users.data:
            try:
                if message.content_type == 'photo':
                    # Для фото берём последнее (самое большое) изображение
                    photo = message.photo[-1].file_id
                    bot.send_photo(user['user_id'], photo, caption=message.caption)
                else:
                    bot.send_message(user['user_id'], message.text)
                success_count += 1
            except Exception as e:
                logger.error(f"Failed to send broadcast to user {user['user_id']}: {str(e)}")
                fail_count += 1

        report = (f"📊 Рассылка завершена\n"
                  f"✅ Успешно отправлено: {success_count}\n"
                  f"❌ Ошибок отправки: {fail_count}")

        bot.reply_to(message, report)
        logger.info(f"Broadcast completed: {success_count} successful, {fail_count} failed")

    except Exception as e:
        logger.error(f"Error in broadcast: {str(e)}", exc_info=True)
        bot.reply_to(message, "⚠️ Произошла ошибка при рассылке")


@bot.message_handler(commands=['konk'])
def handle_konk_command(message):
    try:
        if message.from_user.id != ADMIN_ID:
            bot.reply_to(message, "🚫 Доступ запрещен!")
            return

        reset_konk_state()
        admin_states['konk_step'] = 'referrals'
        bot.reply_to(
            message,
            "🎁 <b>Создание конкурса</b>\n\n"
            "Сколько человек нужно пригласить?\n"
            "(они должны подключить бота к аккаунту; Premium им не нужен)\n\n"
            "Для отмены: /stop",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error in konk command: {e}", exc_info=True)
        bot.reply_to(message, "⚠️ Произошла ошибка")


@bot.message_handler(
    func=lambda m: m.from_user.id == ADMIN_ID and admin_states.get('konk_step') == 'referrals',
    content_types=['text']
)
def handle_konk_referrals_count(message):
    try:
        if message.text.startswith('/'):
            return

        try:
            count = int(message.text.strip())
            if count < 1 or count > 100:
                raise ValueError()
        except ValueError:
            bot.reply_to(message, "⚠️ Введите число от 1 до 100")
            return

        admin_states['konk_data']['required_referrals'] = count
        admin_states['konk_step'] = 'post'
        bot.reply_to(
            message,
            f"✅ Нужно пригласить: <b>{count}</b> чел.\n\n"
            "Теперь отправьте пост для рассылки:\n"
            "📷 <b>Фото с текстом</b> (текст — в подписи к фото)\n\n"
            "Для отмены: /stop",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error in konk referrals step: {e}", exc_info=True)
        bot.reply_to(message, "⚠️ Произошла ошибка")


@bot.message_handler(
    func=lambda m: m.from_user.id == ADMIN_ID and admin_states.get('konk_step') == 'post',
    content_types=['photo']
)
def handle_konk_post(message):
    try:
        photo = message.photo[-1].file_id
        caption = message.caption or ""

        if not caption.strip():
            bot.reply_to(message, "⚠️ Добавьте текст в подпись к фото")
            return

        admin_states['konk_data']['photo_file_id'] = photo
        admin_states['konk_data']['post_text'] = caption
        admin_states['konk_step'] = 'confirm'

        required = admin_states['konk_data']['required_referrals']
        preview = (
            f"🎁 <b>Предпросмотр конкурса</b>\n\n"
            f"<b>Условие:</b> пригласить {required} чел. (подключить бота)\n\n"
            f"<b>Текст поста:</b>\n{caption}\n\n"
            f"Подтвердить рассылку?"
        )
        keyboard = InlineKeyboardMarkup()
        keyboard.row(
            InlineKeyboardButton("✅ Запустить", callback_data="konk_confirm"),
            InlineKeyboardButton("❌ Отмена", callback_data="konk_cancel"),
        )
        bot.send_photo(message.chat.id, photo, caption=preview, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Error in konk post step: {e}", exc_info=True)
        bot.reply_to(message, "⚠️ Произошла ошибка")


def broadcast_contest_post(contest: dict) -> tuple[int, int]:
    success_count = 0
    fail_count = 0
    contest_id = contest['id']
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("📋 Узнать подробности", callback_data=f"konk_info_{contest_id}"))

    users = supabase.table("users").select("user_id").execute()
    for user in users.data or []:
        try:
            safe_send_photo(
                user['user_id'],
                contest['photo_file_id'],
                caption=contest['post_text'],
                reply_markup=keyboard,
            )
            success_count += 1
        except Exception as e:
            logger.error(f"Failed to send contest post to {user['user_id']}: {e}")
            fail_count += 1

    return success_count, fail_count


@bot.message_handler(commands=['konkstat'])
def handle_konkstat_command(message):
    try:
        if message.from_user.id != ADMIN_ID:
            bot.reply_to(message, "🚫 Доступ запрещен!")
            return

        contest = get_active_contest()
        if not contest:
            bot.reply_to(message, "📭 Нет активного конкурса")
            return

        stats = get_contest_stats(contest['id'])
        text = (
            f"📊 <b>Статистика конкурса</b>\n\n"
            f"🎯 Нужно пригласить: {contest['required_referrals']} чел.\n"
            f"👥 Участников: {stats['participants_count']}\n"
            f"🆕 Новых пользователей: {stats['new_users_count']}\n"
            f"🔗 Подключились по реф. ссылкам: {stats['referrals_connected']}\n"
        )

        if stats['participants']:
            text += "\n<b>Участники:</b>\n"
            for p in stats['participants'][:30]:
                text += f"• #{p['participant_number']} — <code>{p['user_id']}</code>\n"
            if len(stats['participants']) > 30:
                text += f"... и ещё {len(stats['participants']) - 30}\n"

        bot.reply_to(message, text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error in konkstat: {e}", exc_info=True)
        bot.reply_to(message, "⚠️ Произошла ошибка")


@bot.callback_query_handler(func=lambda call: call.data.startswith('konk_'))
def handle_konk_callbacks(call):
    try:
        if call.data == "konk_cancel":
            if call.from_user.id != ADMIN_ID:
                safe_answer_callback(call.id, "🚫 Доступ запрещен!", show_alert=True)
                return
            reset_konk_state()
            bot.edit_message_caption(
                "❌ Создание конкурса отменено",
                call.message.chat.id,
                call.message.message_id,
            )
            safe_answer_callback(call.id)
            return

        if call.data == "konk_confirm":
            if call.from_user.id != ADMIN_ID:
                safe_answer_callback(call.id, "🚫 Доступ запрещен!", show_alert=True)
                return

            data = admin_states.get('konk_data', {})
            if not data.get('photo_file_id') or not data.get('post_text'):
                safe_answer_callback(call.id, "⚠️ Данные конкурса потеряны", show_alert=True)
                reset_konk_state()
                return

            contest = create_contest(
                required_referrals=data['required_referrals'],
                post_text=data['post_text'],
                photo_file_id=data['photo_file_id'],
                created_by=call.from_user.id,
            )
            reset_konk_state()

            if not contest:
                safe_answer_callback(call.id, "⚠️ Ошибка создания конкурса. Проверьте таблицы в Supabase.", show_alert=True)
                return

            bot.edit_message_caption(
                "📢 Конкурс создан! Идёт рассылка...",
                call.message.chat.id,
                call.message.message_id,
            )
            safe_answer_callback(call.id, "Рассылка запущена")

            success, fail = broadcast_contest_post(contest)
            safe_send_message(
                call.from_user.id,
                f"✅ <b>Конкурс запущен!</b>\n\n"
                f"📨 Доставлено: {success}\n"
                f"❌ Ошибок: {fail}\n\n"
                f"Статистика: /konkstat",
                parse_mode="HTML",
            )
            return

        if call.data.startswith('konk_info_'):
            contest_id = int(call.data.replace('konk_info_', ''))
            contest_result = supabase.table("contests").select("*").eq("id", contest_id).execute()
            if not contest_result.data:
                safe_answer_callback(call.id, "Конкурс не найден", show_alert=True)
                return

            contest = contest_result.data[0]
            user_id = call.from_user.id
            ref_link = generate_referral_link(user_id)
            connected = get_connected_referrals_count(user_id, since=contest['created_at'])

            text = format_contest_conditions(contest, user_id, ref_link, connected)

            if is_participant(contest_id, user_id):
                participant = supabase.table("contest_participants") \
                    .select("participant_number") \
                    .eq("contest_id", contest_id) \
                    .eq("user_id", user_id) \
                    .execute()
                if participant.data:
                    text += f"\n🎫 Вы уже участвуете! Ваш номер: <b>#{participant.data[0]['participant_number']}</b>"

            safe_send_message(call.from_user.id, text, parse_mode="HTML")
            safe_answer_callback(call.id)

    except Exception as e:
        logger.error(f"Error in konk callback: {e}", exc_info=True)
        safe_answer_callback(call.id, "⚠️ Ошибка", show_alert=True)


@bot.message_handler(commands=['help'])
def help_command(message):
    try:
        help_text = (
            "🤖 <b>О боте:</b>\n"
            "Этот бот помогает отслеживать сообщения в ваших бизнес-чатах Telegram. "
            "Он уведомляет вас об удалённых и отредактированных сообщениях, показывает статистику по чатам.\n\n"
            "А так же позволяет сохранять самоуничтожающиеся фотки, видео и кружочки\n\n"


            "📝 <b>Основные команды:</b>\n"
            "• /start - Запустить бота\n"
        )
        help_text += (
            "• /statistic_gui - Показать графики статистики\n"
            "• /statistic_daily - Показать графики статистики по чатам\n"
            "• /onmy - Включить уведомления о ваших удалённых сообщениях\n"
            "• /offmy - Отключить уведомления о ваших удалённых сообщениях\n\n"

            "⚙️ <b>Настройка:</b>\n"
            "1. Добавьте этого бота в настройках Business аккаунта\n"
        )
        help_text += (
            "3. Готово! Бот начнёт отслеживать сообщения\n\n"

            "⚙️ <b>Инструкция для сохранения:</b>\n"
            "Чтобы бот смог сохранить самоуничтожающиеся медиафайлы, ответьте на них любой фразой\n"
            "Поддерживаются: фото, видео, кружочки, голосовые\n\n"

            "🔒 <b>Безопасность:</b>\n"
            "Бот хранит только метаданные сообщений и не имеет доступа к личной переписке вне бизнес-чатов.\n\n"

            "<code>Название чатов с названием 'неактивированный чат'/'unknown' начнут отображатся после того как вы напишите в них любое сообщение</code>"
        )

        bot.send_message(message.chat.id, help_text, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error in help command: {str(e)}", exc_info=True)
        bot.reply_to(message, "⚠️ Произошла ошибка при отображении справки")
        
def create_main_menu() -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("👤 Профиль", callback_data="menu_profile"),
        types.InlineKeyboardButton("📊 Статистика", callback_data="menu_stats")
    )
    keyboard.add(
        types.InlineKeyboardButton("🔔 Уведомления", callback_data="menu_notify")
    )
    keyboard.add(
        types.InlineKeyboardButton("❓ Помощь", callback_data="menu_help")
    )
    return keyboard

@bot.message_handler(commands=['menu'])
def show_menu(message):
    try:
        bot.send_message(
            message.chat.id,
            "🤖 <b>Главное меню</b>\n\nВыберите нужный раздел:",
            reply_markup=create_main_menu(),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error showing menu: {str(e)}", exc_info=True)
        bot.send_message(message.chat.id, "⚠️ Ошибка при отображении меню")

@bot.callback_query_handler(func=lambda call: call.data == "menu_back")
def handle_menu_back(call):
    try:
        bot.edit_message_text(
            "🤖 <b>Главное меню</b>\n\nВыберите нужный раздел:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=create_main_menu(),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error in menu back: {str(e)}")
        bot.answer_callback_query(call.id, "⚠️ Произошла ошибка")

@bot.callback_query_handler(func=lambda call: call.data.startswith('menu_'))
def handle_menu_callback(call):
    try:
        action = call.data.split('_')[1]
        
        if action == "profile":
            profile_text, keyboard = build_profile_content(call.from_user.id)
            if not profile_text:
                bot.answer_callback_query(call.id, "⚠️ Ошибка получения профиля", show_alert=True)
                return
            bot.delete_message(call.message.chat.id, call.message.message_id)
            bot.send_message(
                call.message.chat.id,
                profile_text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
            
        elif action == "stats":
            # Показываем подменю статистики
            keyboard = types.InlineKeyboardMarkup(row_width=1)
            keyboard.add(
                types.InlineKeyboardButton("📈 Топ за 7 дней", callback_data="stats_graph"),
                types.InlineKeyboardButton("📊 Статистика по чату", callback_data="stats_daily"),
                types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")
            )
            
            bot.edit_message_text(
                "📊 <b>Статистика</b>\n\n"
                "Выберите тип статистики:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            
        elif action == "sub":
            bot.answer_callback_query(call.id, "Раздел недоступен", show_alert=True)
            
        elif action == "notify":
            # Показываем настройки уведомлений
            user_id = call.from_user.id
            notify_self = get_notify_setting(user_id)
            status = "включены" if notify_self else "отключены"
            
            keyboard = types.InlineKeyboardMarkup(row_width=1)
            keyboard.add(
                types.InlineKeyboardButton(
                    "🔔 Включить уведомления" if not notify_self else "🔕 Отключить уведомления",
                    callback_data="notify_toggle"
                ),
                types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")
            )
            
            bot.edit_message_text(
                f"🔔 <b>Уведомления</b>\n\n"
                f"Уведомления о ваших сообщениях сейчас {status}",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            
        elif action == "help":
            # Показываем справку
            help_text = (
                "❓ <b>Помощь</b>\n\n"
                "• /menu - Открыть главное меню\n"
                "• /start - Запустить бота\n"
                "• /statistic_gui - Показать графики\n"
                "• /onmy - Включить уведомления\n"
                "• /offmy - Отключить уведомления\n\n"
                "Для получения подробной информации используйте /help"
            )
            
            keyboard = types.InlineKeyboardMarkup(row_width=1)
            keyboard.add(
                types.InlineKeyboardButton("💬 Написать в поддержку", callback_data="menu_support"),
                types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")
            )
            
            bot.edit_message_text(
                help_text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            
        elif action == "support":
            # Показываем форму поддержки
            keyboard = types.InlineKeyboardMarkup(row_width=1)
            keyboard.add(
                types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_help")
            )
            
            bot.edit_message_text(
                "💬 <b>Поддержка</b>\n\n"
                "Напишите ваше сообщение для поддержки.\n"
                "Оно будет отправлено администратору",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            
            # Устанавливаем состояние ожидания сообщения поддержки
            admin_states['waiting_for_support'] = True
            
        elif action == "back":
            # Возвращаемся в главное меню
            keyboard = create_main_menu()
            
            bot.edit_message_text(
                "🤖 <b>Главное меню</b>\n\n"
                "Выберите нужный раздел:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            
    except Exception as e:
        logger.error(f"Error in menu callback: {str(e)}", exc_info=True)
        bot.answer_callback_query(call.id, "⚠️ Произошла ошибка")

@bot.callback_query_handler(func=lambda call: call.data in ['notify_toggle'])
def handle_additional_callbacks(call):
    try:
        if call.data == "notify_toggle":
            user_id = call.from_user.id
            current_setting = get_notify_setting(user_id)
            new_setting = not current_setting
            
            supabase.table("users").update({"notify_self": new_setting}).eq("user_id", user_id).execute()
            status = "включены" if new_setting else "отключены"
            
            keyboard = types.InlineKeyboardMarkup(row_width=1)
            keyboard.add(
                types.InlineKeyboardButton(
                    "🔔 Включить уведомления" if not new_setting else "🔕 Отключить уведомления",
                    callback_data="notify_toggle"
                ),
                types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")
            )
            
            bot.edit_message_text(
                f"🔔 <b>Уведомления</b>\n\n"
                f"Уведомления о ваших сообщениях теперь {status}",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            
        
            
    except Exception as e:
        logger.error(f"Error in additional callbacks: {str(e)}", exc_info=True)
        bot.answer_callback_query(call.id, "⚠️ Произошла ошибка")

def cleanup_old_statistics():
    """Удаляет статистику и сообщения старше 2 недель"""
    try:
        two_weeks_ago = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d')
        
        # Удаляем старые записи статистики
        stats_result = supabase.table("daily_statistics") \
            .delete() \
            .lt("date", two_weeks_ago) \
            .execute()
            
        logger.info(f"Удалено {len(stats_result.data) if stats_result.data else 0} записей статистики старше 2 недель")

        # Удаляем старые сообщения
        messages_result = supabase.table("messages") \
            .delete() \
            .lt("created_at", two_weeks_ago) \
            .execute()
            
        logger.info(f"Удалено {len(messages_result.data) if messages_result.data else 0} сообщений старше 2 недель")

    except Exception as e:
        logger.error(f"Ошибка при очистке старых данных: {str(e)}")

def cleanup_scheduler():
    """Планировщик очистки старых данных"""
    last_cleanup_date = None
    last_active_refresh_date = None
    while True:
        try:
            now = datetime.now()
            today = now.date()

            # Запускаем очистку в 3 часа ночи
            if now.hour == 3 and now.minute == 0 and last_cleanup_date != today:
                cleanup_old_statistics()
                last_cleanup_date = today

            # Обновляем список активных пользователей раз в сутки (в 3:00)
            if now.hour == 3 and now.minute == 0 and last_active_refresh_date != today:
                refresh_active_users_cache(force=True)
                last_active_refresh_date = today

            # Спим 1 минуту перед следующей проверкой
            time.sleep(60)
        except Exception as e:
            logger.error(f"Ошибка в планировщике очистки: {str(e)}")
            time.sleep(60)

def run_bot():
    """Основной цикл работы бота"""
    try:
        # Запускаем очистку старых данных при старте
        cleanup_old_statistics()
        refresh_active_users_cache(force=True)
        
        # Запускаем планировщик очистки в отдельном потоке
        cleanup_thread = threading.Thread(target=cleanup_scheduler, daemon=True)
        cleanup_thread.start()
        
        # Запускаем бота
        bot.infinity_polling()
    except Exception as e:
        logger.error(f"Ошибка в работе бота: {str(e)}")
        # Перезапускаем бота через 5 секунд в случае ошибки
        time.sleep(5)
        run_bot()

def create_users_keyboard(users: list, current_page: int, total_pages: int) -> InlineKeyboardMarkup:
    """Создает клавиатуру с пользователями и навигацией"""
    keyboard = InlineKeyboardMarkup()
    
    # Добавляем кнопки пользователей
    start_idx = current_page * 10
    end_idx = min(start_idx + 10, len(users))
    
    for user in users[start_idx:end_idx]:
        username = user['username'] or f"User_{user['user_id']}"
        status = "🟢" if user['is_connected'] else "🔴"
        keyboard.add(InlineKeyboardButton(
            text=f"{status} {username}",
            callback_data=f"select_user_{user['user_id']}"
        ))
    
    # Добавляем навигацию
    nav_buttons = []
    if current_page > 0:
        nav_buttons.append(InlineKeyboardButton('⬅️', callback_data=f'users_page_{current_page - 1}'))
    
    nav_buttons.append(InlineKeyboardButton(f'| {current_page + 1}/{total_pages} |', callback_data='current_page'))
    
    if current_page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton('➡️', callback_data=f'users_page_{current_page + 1}'))
    
    if nav_buttons:  # Добавляем навигацию только если есть кнопки
        keyboard.row(*nav_buttons)
    
    # Добавляем кнопку отмены
    keyboard.add(InlineKeyboardButton("❌ Отмена", callback_data="cancel_operation"))
    
    return keyboard

@bot.message_handler(commands=['tellone'])
def handle_tellone_command(message):
    try:
        if message.from_user.id != ADMIN_ID:
            logger.warning(f"Unauthorized tellone attempt from {message.from_user.id}")
            bot.reply_to(message, "🚫 Доступ запрещен!")
            return

        # Получаем список всех пользователей
        users = supabase.table("users").select("user_id, username, is_connected").execute()
        
        if not users.data:
            bot.reply_to(message, "❌ Нет пользователей")
            return

        # Сортируем пользователей по статусу подключения
        users.data.sort(key=lambda x: (not x['is_connected'], x['username'] or f"User_{x['user_id']}"))
        
        # Рассчитываем количество страниц
        total_pages = (len(users.data) + 9) // 10  # Округляем вверх
        
        # Создаем клавиатуру для первой страницы
        keyboard = create_users_keyboard(users.data, 0, total_pages)
        
        admin_states['waiting_for_individual_message'] = True
        bot.reply_to(message, 
                    "👥 Выберите пользователя для отправки сообщения:\n"
                    "🟢 - подключен\n"
                    "🔴 - не подключен",
                    reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Error in tellone command: {str(e)}", exc_info=True)
        bot.reply_to(message, "⚠️ Произошла ошибка")

@bot.callback_query_handler(func=lambda call: call.data.startswith('users_page_') or 
                          call.data.startswith('select_user_') or 
                          call.data == 'cancel_operation' or 
                          call.data == 'current_page')
def handle_users_pagination(call):
    try:
        if call.data == 'cancel_operation':
            admin_states['waiting_for_individual_message'] = False
            admin_states['selected_user_id'] = None
            bot.edit_message_text(
                "❌ Операция отменена",
                call.message.chat.id,
                call.message.message_id
            )
            return

        if call.data == 'current_page':
            bot.answer_callback_query(call.id, "Текущая страница")
            return

        if not admin_states['waiting_for_individual_message']:
            bot.answer_callback_query(call.id, "❌ Операция уже отменена")
            return

        if call.data.startswith('users_page_'):
            # Обработка навигации по страницам
            page = int(call.data.split('_')[2])
            users = supabase.table("users").select("user_id, username, is_connected").execute()
            users.data.sort(key=lambda x: (not x['is_connected'], x['username'] or f"User_{x['user_id']}"))
            total_pages = (len(users.data) + 9) // 10
            
            keyboard = create_users_keyboard(users.data, page, total_pages)
            bot.edit_message_reply_markup(
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard
            )
            return

        # Обработка выбора пользователя
        user_id = int(call.data.split('_')[2])
        admin_states['selected_user_id'] = user_id
        
        # Получаем информацию о пользователе
        user_info = supabase.table("users").select("username, is_connected").eq("user_id", user_id).execute()
        if not user_info.data:
            bot.answer_callback_query(call.id, "❌ Пользователь не найден")
            return
            
        user = user_info.data[0]
        status = "🟢 подключен" if user['is_connected'] else "🔴 не подключен"
        username = user['username'] or f"User_{user_id}"
        
        bot.edit_message_text(
            f"👤 Выбран пользователь: {username}\n"
            f"Статус: {status}\n\n"
            "Отправьте сообщение для этого пользователя.\n"
            "Поддерживаются текст и фото с подписью.\n"
            "Для отмены используйте команду /cancel",
            call.message.chat.id,
            call.message.message_id
        )

    except Exception as e:
        logger.error(f"Error handling user selection: {str(e)}", exc_info=True)
        bot.answer_callback_query(call.id, "⚠️ Произошла ошибка")

@bot.message_handler(commands=['cancel'])
def handle_cancel_command(message):
    try:
        if message.from_user.id != ADMIN_ID:
            return

        if admin_states['waiting_for_individual_message']:
            admin_states['waiting_for_individual_message'] = False
            admin_states['selected_user_id'] = None
            bot.reply_to(message, "❌ Операция отменена")
            logger.info("Individual message operation cancelled by admin")

    except Exception as e:
        logger.error(f"Error in cancel command: {str(e)}", exc_info=True)
        bot.reply_to(message, "⚠️ Произошла ошибка при отмене операции")

@bot.message_handler(
    func=lambda message: message.from_user.id == ADMIN_ID and 
    admin_states['waiting_for_individual_message'] and 
    admin_states['selected_user_id'] is not None,
    content_types=['text', 'photo'])
def handle_individual_message(message):
    try:
        user_id = admin_states['selected_user_id']
        
        try:
            if message.content_type == 'photo':
                photo = message.photo[-1].file_id
                bot.send_photo(user_id, photo, caption=message.caption)
            else:
                bot.send_message(user_id, message.text)
            
            bot.reply_to(message, "✅ Сообщение успешно отправлено")
            logger.info(f"Message sent to user {user_id}")
            
        except Exception as e:
            bot.reply_to(message, f"❌ Не удалось отправить сообщение: {str(e)}")
            logger.error(f"Failed to send message to user {user_id}: {str(e)}")
        
        finally:
            # Сбрасываем состояние
            admin_states['waiting_for_individual_message'] = False
            admin_states['selected_user_id'] = None

    except Exception as e:
        logger.error(f"Error in individual message handler: {str(e)}", exc_info=True)
        bot.reply_to(message, "⚠️ Произошла ошибка при отправке сообщения")

def create_chat_selection_keyboard(chats: list) -> InlineKeyboardMarkup:
    """Создает клавиатуру для выбора чата"""
    keyboard = InlineKeyboardMarkup()
    
    for chat in chats:
        keyboard.add(InlineKeyboardButton(
            text=chat['title'],
            callback_data=f"daily_stats_{chat['chat_id']}"
        ))
    
    return keyboard

def animate_loading_daily(message_id, chat_id, stop_event):
    """Анимация точек в сообщении о загрузке для ежедневной статистики"""
    dots = 0
    while not stop_event.is_set():
        try:
            dots = (dots + 1) % 4
            loading_text = "Рисуем вашу статистику📊" + "." * dots
            bot.edit_message_text(
                loading_text,
                chat_id,
                message_id
            )
            time.sleep(0.5)
        except Exception as e:
            logger.error(f"Error in loading animation: {str(e)}")
            break


def create_main_menu() -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("Профиль", callback_data="menu_profile")
    )
    keyboard.add(
        types.InlineKeyboardButton("Статистика", callback_data="menu_stats")
    )
    keyboard.add(
        types.InlineKeyboardButton("Уведомления", callback_data="menu_notify"),
        types.InlineKeyboardButton("Помощь", callback_data="menu_help")
    )
    return keyboard

def create_back_button() -> InlineKeyboardMarkup:
    """Создает клавиатуру с кнопкой 'Назад'"""
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("⬅️ Назад", callback_data="daily_stats_back"))
    return keyboard

@bot.callback_query_handler(func=lambda call: call.data.startswith('daily_stats_'))
def handle_daily_stats_selection(call):
    try:
        logger.info(f"Получен callback для ежедневной статистики: {call.data}")
        
        if call.data == "daily_stats_back":
            logger.info("Возврат к выбору чата")
            # Возвращаемся к выбору чата
            user_id = call.from_user.id
            seven_days_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
            
            stats_data = supabase.table("daily_statistics") \
                .select("chat_id, incoming_count, outgoing_count") \
                .eq("user_id", user_id) \
                .gte("date", seven_days_ago) \
                .execute()

            if not stats_data.data:
                logger.info("Нет данных для отображения")
                bot.edit_message_text(
                    "📊 У вас пока нет сообщений за последние 7 дней",
                    call.message.chat.id,
                    call.message.message_id
                )
                return

            # Считаем общее количество сообщений для каждого чата
            chat_stats = {}
            for stat in stats_data.data:
                chat_id = stat['chat_id']
                if chat_id not in chat_stats:
                    chat_stats[chat_id] = 0
                chat_stats[chat_id] += stat['incoming_count'] + stat['outgoing_count']

            logger.info(f"Обработано {len(chat_stats)} чатов")

            # Сортируем чаты по количеству сообщений и берем топ-10
            top_chats = sorted(chat_stats.items(), key=lambda x: x[1], reverse=True)[:10]

            # Получаем названия чатов
            chats = []
            for chat_id, _ in top_chats:
                try:
                    chat_info = bot.get_chat(chat_id)
                    chat_title = get_chat_title(chat_info)
                except Exception as e:
                    chat_title = f"Неизвестный чат ({chat_id})"
                    logger.debug(f"Can't get chat info: {str(e)}")

                chats.append({
                    'chat_id': chat_id,
                    'title': chat_title
                })

            logger.info(f"Создаем клавиатуру для {len(chats)} чатов")

            # Создаем клавиатуру для выбора чата
            keyboard = create_chat_selection_keyboard(chats)
            
            # Удаляем старое сообщение с графиком
            bot.delete_message(call.message.chat.id, call.message.message_id)
            
            # Отправляем новое сообщение со списком чатов
            bot.send_message(
                call.message.chat.id,
                "📊 Выберите чат для просмотра ежедневной статистики:",
                reply_markup=keyboard
            )
            return

        chat_id = int(call.data.split('_')[2])
        user_id = call.from_user.id
        
        logger.info(f"Обработка статистики для чата {chat_id}")
        
        # Получаем название чата
        try:
            chat_info = bot.get_chat(chat_id)
            chat_title = get_chat_title(chat_info)
        except Exception as e:
            chat_title = f"Неизвестный чат ({chat_id})"
            logger.debug(f"Can't get chat info: {str(e)}")
        
        # Получаем статистику за последние 7 дней
        seven_days_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        
        stats_data = supabase.table("daily_statistics") \
            .select("date, incoming_count, outgoing_count") \
            .eq("user_id", user_id) \
            .eq("chat_id", chat_id) \
            .gte("date", seven_days_ago) \
            .order("date") \
            .execute()

        logger.info(f"Получено {len(stats_data.data) if stats_data.data else 0} записей статистики")

        if not stats_data.data:
            logger.info("Нет данных для отображения")
            bot.edit_message_text(
                "📊 Нет данных для выбранного чата за последние 7 дней",
                call.message.chat.id,
                call.message.message_id
            )
            return

        # Подготавливаем данные для графика
        dates = []
        incoming = []
        outgoing = []
        
        for stat in stats_data.data:
            dates.append(datetime.strptime(stat['date'], '%Y-%m-%d').strftime('%d.%m'))
            incoming.append(stat['incoming_count'])
            outgoing.append(stat['outgoing_count'])

        logger.info("Создаем график")

        # Создаем график
        plt.style.use('default')
        fig, ax = plt.subplots(figsize=(12, 6))
        
        # Рисуем линейный график
        ax.plot(dates, incoming, label='Входящие', color='#2ecc71', marker='o')
        ax.plot(dates, outgoing, label='Исходящие', color='#3498db', marker='o')
        
        # Добавляем точки данных
        for i, (inc, out) in enumerate(zip(incoming, outgoing)):
            ax.text(i, inc, str(inc), ha='center', va='bottom')
            ax.text(i, out, str(out), ha='center', va='bottom')
        
        # Настраиваем график
        ax.set_xlabel('Дата', fontsize=10, color='black', labelpad=10)
        ax.set_ylabel('Количество сообщений', fontsize=10, color='black', labelpad=10)
        ax.set_title('Статистика сообщений по дням', fontsize=12, color='black', pad=20)
        plt.xticks(rotation=45)
        plt.legend(loc='upper right')
        
        # Устанавливаем цвет фона
        ax.set_facecolor('white')
        fig.patch.set_facecolor('white')
        
        # Настраиваем отступы
        plt.tight_layout()

        logger.info("Сохраняем график")

        # Сохраняем график в байтовый поток
        img_stream = io.BytesIO()
        plt.savefig(img_stream, format='png', dpi=300, bbox_inches='tight')
        img_stream.seek(0)
        plt.close()

        logger.info("Отправляем график пользователю")

        # Удаляем старое сообщение со списком чатов
        bot.delete_message(call.message.chat.id, call.message.message_id)
        
        # Отправляем новое сообщение с графиком
        bot.send_photo(
            call.message.chat.id,
            photo=img_stream,
            caption=f"📊 Статистика сообщений с {chat_title}\n"
                   f"📥 Входящих: {sum(incoming)}\n"
                   f"📤 Исходящих: {sum(outgoing)}",
            reply_markup=create_back_button()
        )

        logger.info("График успешно отправлен")

    except Exception as e:
        logger.error(f"Ошибка при создании графика ежедневной статистики: {str(e)}", exc_info=True)
        bot.answer_callback_query(call.id, "⚠️ Ошибка при создании графика")

# Добавляем обработчик сообщений поддержки
@bot.message_handler(
    func=lambda message: admin_states.get('waiting_for_support', False),
    content_types=['text', 'photo', 'document', 'video', 'audio', 'voice', 'video_note', 'animation', 'sticker']
)
def handle_support_message(message):
    try:
        user = message.from_user
        username = user.username or user.first_name or f"User_{user.id}"
        current_time = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        
        # Формируем заголовок сообщения
        header = (
            f"📨 <b>Новое сообщение в поддержку</b>\n\n"
            f"👤 От: {username} (ID: {user.id})\n"
            f"🕒 Время: {current_time}\n"
            f"─────────────────\n"
        )
        
        # Создаем клавиатуру с кнопкой ответа
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton(
            "💬 Ответить",
            callback_data=f"reply_to_{user.id}"
        ))
        
        # Отправляем сообщение администратору
        if message.content_type == 'text':
            bot.send_message(
                ADMIN_ID,
                f"{header}{message.text}",
                parse_mode="HTML",
                reply_markup=keyboard
            )
        else:
            # Для медиа-файлов
            media_method = {
                'photo': bot.send_photo,
                'video': bot.send_video,
                'document': bot.send_document,
                'audio': bot.send_audio,
                'voice': bot.send_voice,
                'video_note': bot.send_video_note,
                'animation': bot.send_animation,
                'sticker': bot.send_sticker
            }.get(message.content_type)
            
            if media_method:
                file_id = getattr(message, message.content_type)[-1].file_id
                media_method(
                    ADMIN_ID,
                    file_id,
                    caption=f"{header}{message.caption or ''}",
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
        
        # Отправляем подтверждение пользователю
        bot.reply_to(
            message,
            "✅ Ваше сообщение отправлено в поддержку.\n"
            "Мы ответим вам в ближайшее время."
        )
        
        # Сбрасываем состояние
        admin_states['waiting_for_support'] = False
        
    except Exception as e:
        logger.error(f"Error handling support message: {str(e)}", exc_info=True)
        bot.reply_to(message, "⚠️ Произошла ошибка при отправке сообщения в поддержку")
        admin_states['waiting_for_support'] = False

# Добавляем обработчик кнопки ответа
@bot.callback_query_handler(func=lambda call: call.data.startswith('reply_to_'))
def handle_reply_button(call):
    try:
        if call.from_user.id != ADMIN_ID:
            bot.answer_callback_query(call.id, "🚫 Доступ запрещен!")
            return
            
        user_id = int(call.data.split('_')[2])
        admin_states['waiting_for_reply'] = True
        admin_states['reply_to_user_id'] = user_id
        
        # Получаем информацию о пользователе
        user_info = supabase.table("users").select("username").eq("user_id", user_id).execute()
        username = user_info.data[0]['username'] if user_info.data else f"User_{user_id}"
        
        bot.edit_message_reply_markup(
            call.message.chat.id,
            call.message.message_id,
            reply_markup=None
        )
        
        bot.send_message(
            call.message.chat.id,
            f"💬 Отправьте сообщение для пользователя {username} (ID: {user_id})\n"
            "Поддерживаются текст и медиафайлы.\n"
            "Для отмены используйте /cancel"
        )
        
    except Exception as e:
        logger.error(f"Error handling reply button: {str(e)}", exc_info=True)
        bot.answer_callback_query(call.id, "⚠️ Произошла ошибка")

# Добавляем обработчик команды /reply
@bot.message_handler(commands=['reply'])
def handle_reply_command(message):
    try:
        if message.from_user.id != ADMIN_ID:
            logger.warning(f"Unauthorized reply attempt from {message.from_user.id}")
            bot.reply_to(message, "🚫 Доступ запрещен!")
            return

        # Проверяем формат команды: /reply user_id
        args = message.text.split()
        if len(args) != 2:
            bot.reply_to(
                message,
                "⚠️ Неверный формат команды.\n"
                "Используйте: /reply user_id"
            )
            return

        try:
            user_id = int(args[1])
            admin_states['waiting_for_reply'] = True
            admin_states['reply_to_user_id'] = user_id
            
            # Получаем информацию о пользователе
            user_info = supabase.table("users").select("username").eq("user_id", user_id).execute()
            username = user_info.data[0]['username'] if user_info.data else f"User_{user_id}"
            
            bot.reply_to(
                message,
                f"💬 Отправьте сообщение для пользователя {username} (ID: {user_id})\n"
                "Поддерживаются текст и медиафайлы.\n"
                "Для отмены используйте /cancel"
            )
            
        except ValueError:
            bot.reply_to(message, "⚠️ Неверный ID пользователя")
            
    except Exception as e:
        logger.error(f"Error in reply command: {str(e)}", exc_info=True)
        bot.reply_to(message, "⚠️ Произошла ошибка")

# Обновляем обработчик сообщений для ответа
@bot.message_handler(
    func=lambda message: message.from_user.id == ADMIN_ID and 
    admin_states['waiting_for_reply'] and 
    admin_states['reply_to_user_id'] is not None,
    content_types=['text', 'photo', 'document', 'video', 'audio', 'voice', 'video_note', 'animation', 'sticker']
)
def handle_admin_reply(message):
    try:
        user_id = admin_states['reply_to_user_id']
        
        try:
            # Отправляем сообщение пользователю
            if message.content_type == 'text':
                bot.send_message(
                    user_id,
                    f"💬 <b>Ответ поддержки:</b>\n\n{message.text}",
                    parse_mode="HTML"
                )
            else:
                # Для медиа-файлов
                media_method = {
                    'photo': bot.send_photo,
                    'video': bot.send_video,
                    'document': bot.send_document,
                    'audio': bot.send_audio,
                    'voice': bot.send_voice,
                    'video_note': bot.send_video_note,
                    'animation': bot.send_animation,
                    'sticker': bot.send_sticker
                }.get(message.content_type)
                
                if media_method:
                    file_id = getattr(message, message.content_type)[-1].file_id
                    caption = f"💬 <b>Ответ поддержки:</b>\n\n{message.caption or ''}"
                    media_method(
                        user_id,
                        file_id,
                        caption=caption,
                        parse_mode="HTML"
                    )
            
            # Отправляем подтверждение администратору
            bot.reply_to(
                message,
                "✅ Сообщение успешно отправлено пользователю"
            )
            
        except Exception as e:
            bot.reply_to(
                message,
                f"❌ Не удалось отправить сообщение: {str(e)}"
            )
            
        finally:
            # Сбрасываем состояние
            admin_states['waiting_for_reply'] = False
            admin_states['reply_to_user_id'] = None
            
    except Exception as e:
        logger.error(f"Error handling admin reply: {str(e)}", exc_info=True)
        bot.reply_to(message, "⚠️ Произошла ошибка при отправке сообщения")

@bot.message_handler(commands=['profile'])
def handle_profile(message):
    try:
        show_profile_message(message.chat.id, message.from_user.id)
    except Exception as e:
        logger.error(f"Error showing profile: {str(e)}", exc_info=True)
        bot.reply_to(message, "⚠️ Произошла ошибка при отображении профиля")


def build_profile_content(user_id: int) -> tuple:
    profile = get_user_profile(user_id)
    if not profile:
        return None, None

    profile_text = (
        f"👤 <b>Личный кабинет</b>\n\n"
        f"👤 Пользователь: {profile['user']['username']}\n"
    )

    stats = profile['referral_stats']
    profile_text += (
        f"\n👥 <b>Реферальная статистика</b>\n"
        f"• Всего приглашено: {stats['total_referrals']}\n"
        f"• Активных рефералов (подключили бота): {stats['active_referrals']}\n"
    )

    ref_link = generate_referral_link(user_id)
    if ref_link:
        profile_text += f"\n🔗 <b>Ваша реферальная ссылка:</b>\n{ref_link}\n"

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("📊 Статистика", callback_data="menu_stats"),
        types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")
    )
    return profile_text, keyboard


def show_profile_message(chat_id: int, user_id: int):
    profile_text, keyboard = build_profile_content(user_id)
    if not profile_text:
        bot.send_message(chat_id, "⚠️ Ошибка получения данных профиля")
        return
    bot.send_message(chat_id, profile_text, parse_mode="HTML", reply_markup=keyboard)


def build_exclusive_content(user_id: int) -> tuple:
    exclusive_info = get_exclusive_subscription_info(user_id)
    text = (
        "✨ <b>Эксклюзив</b>\n\n"
        "Подписка улучшает оформление уведомлений об удалении и редактировании сообщений:\n"
        "• стильное оформление с разделителями\n"
        "• название чата в уведомлении\n"
        "• улучшенная читаемость текста\n\n"
        f"💰 Стоимость: <b>{int(EXCLUSIVE_SUBSCRIPTION_PRICE)} ₽</b> / год\n\n"
    )

    keyboard = types.InlineKeyboardMarkup(row_width=1)

    if exclusive_info["has_subscription"]:
        end_date = datetime.fromisoformat(exclusive_info["end_date"]).strftime("%d.%m.%Y")
        text += f"✅ Подписка активна до <b>{end_date}</b>"
    else:
        text += "Оформите подписку, чтобы получить эксклюзивное оформление уведомлений."
        keyboard.add(
            types.InlineKeyboardButton("💳 Оплатить", callback_data="exclusive_pay")
        )

    keyboard.add(
        types.InlineKeyboardButton("⬅️ Назад в профиль", callback_data="profile_back")
    )
    return text, keyboard


@bot.callback_query_handler(func=lambda call: call.data == "profile_exclusive")
def handle_profile_exclusive(call):
    try:
        text, keyboard = build_exclusive_content(call.from_user.id)
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error in exclusive menu: {str(e)}", exc_info=True)
        bot.answer_callback_query(call.id, "⚠️ Произошла ошибка")


@bot.callback_query_handler(func=lambda call: call.data == "profile_back")
def handle_profile_back(call):
    try:
        profile_text, keyboard = build_profile_content(call.from_user.id)
        if not profile_text:
            bot.answer_callback_query(call.id, "⚠️ Ошибка получения профиля", show_alert=True)
            return
        bot.edit_message_text(
            profile_text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error returning to profile: {str(e)}", exc_info=True)
        bot.answer_callback_query(call.id, "⚠️ Произошла ошибка")


@bot.callback_query_handler(func=lambda call: call.data == "exclusive_pay")
def handle_exclusive_pay(call):
    try:
        user_id = call.from_user.id
        if check_exclusive_subscription(user_id):
            bot.answer_callback_query(call.id, "У вас уже активна подписка «Эксклюзив»", show_alert=True)
            return

        payment = create_exclusive_payment(user_id)
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        keyboard.add(
            types.InlineKeyboardButton("💳 Перейти к оплате", url=payment["confirmation_url"]),
            types.InlineKeyboardButton("🔄 Проверить оплату", callback_data=f"check_payment_{payment['payment_id']}"),
            types.InlineKeyboardButton("⬅️ Назад", callback_data="profile_exclusive")
        )

        bot.edit_message_text(
            f"💳 <b>Оплата «Эксклюзив»</b>\n\n"
            f"Сумма: <b>{int(payment['amount'])} ₽</b>\n"
            f"Срок: <b>1 год</b>\n\n"
            "Нажмите «Перейти к оплате», завершите платёж и вернитесь в бота.\n"
            "После оплаты нажмите «Проверить оплату».",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    except YookassaCredentialsError as e:
        logger.error(f"YooKassa credentials error: {str(e)}")
        bot.answer_callback_query(
            call.id,
            "Оплата временно недоступна: неверные ключи ЮKassa на сервере. "
            "Обратитесь к администратору.",
            show_alert=True
        )
    except Exception as e:
        logger.error(f"Error creating exclusive payment: {str(e)}", exc_info=True)
        bot.answer_callback_query(call.id, "⚠️ Не удалось создать платёж", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "menu_sub")
def handle_subscription_menu(call):
    bot.answer_callback_query(call.id, "Раздел недоступен", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "sub_free")
def handle_free_subscription(call):
    bot.answer_callback_query(call.id, "Раздел недоступен", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "menu_stats")
def handle_stats_menu(call):
    try:
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        keyboard.add(
            types.InlineKeyboardButton("📈 Топ за 7 дней", callback_data="stats_graph"),
            types.InlineKeyboardButton("📊 Статистика по чату", callback_data="stats_daily"),
            types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")
        )
        
        bot.edit_message_text(
            "📊 <b>Статистика</b>\n\n"
            "Выберите тип статистики:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error in stats menu: {str(e)}")
        bot.answer_callback_query(call.id, "⚠️ Произошла ошибка")

@bot.callback_query_handler(func=lambda call: call.data == "menu_profile")
def handle_profile_menu(call):
    try:
        profile_text, keyboard = build_profile_content(call.from_user.id)
        if not profile_text:
            bot.answer_callback_query(call.id, "⚠️ Ошибка получения профиля", show_alert=True)
            return
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(
            call.message.chat.id,
            profile_text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Error in profile menu: {str(e)}")
        bot.answer_callback_query(call.id, "⚠️ Произошла ошибка")

def get_user_profile(user_id: int) -> dict:
    """Получает информацию профиля пользователя"""
    try:
        # Получаем основную информацию о пользователе
        user_info = supabase.table("users").select("*").eq("user_id", user_id).execute()
        if not user_info.data:
            return None
        
        # Блок подписки удален из профиля
        
        # Получаем статистику рефералов
        referral_stats = get_referral_stats(user_id)
        
        return {
            "user": user_info.data[0],
            "subscription": None,
            "referral_stats": referral_stats
        }
    except Exception as e:
        logger.error(f"Error getting user profile: {str(e)}")
        return None

def handle_self_destruct_media(message):
    try:
        # Проверяем, является ли сообщение ответом на другое сообщение
        if not message.reply_to_message:
            logger.debug("Message is not a reply")
            return

        replied_msg = message.reply_to_message
        logger.debug(f"Processing reply to message {replied_msg.message_id}")
        
        # Проверяем, имеет ли исходное сообщение защищенный контент
        if not getattr(replied_msg, 'has_protected_content', False):
            logger.debug("Message does not have protected content")
            return

        # Получаем информацию о бизнес-соединении
        bc_id = message.business_connection_id
        owner_id = get_connection_owner(bot, bc_id)
        if not owner_id:
            logger.warning(f"No owner found for business connection {bc_id}")
            return

        # Проверка подписки удалена

        # Обрабатываем разные типы медиа
        try:
            if replied_msg.photo:
                # Для фото берем последний элемент (самое высокое разрешение)
                file_id = replied_msg.photo[-1].file_id
                logger.info(f"Processing protected photo with file_id: {file_id}")
                send_protected_media(owner_id, file_id, 'photo')  # Отправляем владельцу бизнес-аккаунта

            elif replied_msg.video:
                file_id = replied_msg.video.file_id
                logger.info(f"Processing protected video with file_id: {file_id}")
                send_protected_media(owner_id, file_id, 'video')  # Отправляем владельцу бизнес-аккаунта

            elif replied_msg.video_note:
                file_id = replied_msg.video_note.file_id
                logger.info(f"Processing protected video note with file_id: {file_id}")
                send_protected_media(owner_id, file_id, 'video_note')  # Отправляем владельцу бизнес-аккаунта
                
            elif replied_msg.voice:
                file_id = replied_msg.voice.file_id
                logger.info(f"Processing protected voice with file_id: {file_id}")
                send_protected_media(owner_id, file_id, 'voice')  # Отправляем владельцу бизнес-аккаунта
                
            elif replied_msg.audio:
                file_id = replied_msg.audio.file_id
                logger.info(f"Processing protected audio with file_id: {file_id}")
                send_protected_media(owner_id, file_id, 'audio')  # Отправляем владельцу бизнес-аккаунта
                
            elif replied_msg.animation:
                file_id = replied_msg.animation.file_id
                logger.info(f"Processing protected animation with file_id: {file_id}")
                send_protected_media(owner_id, file_id, 'animation')  # Отправляем владельцу бизнес-аккаунта
                
            elif replied_msg.sticker:
                file_id = replied_msg.sticker.file_id
                logger.info(f"Processing protected sticker with file_id: {file_id}")
                send_protected_media(owner_id, file_id, 'sticker')  # Отправляем владельцу бизнес-аккаунта
                
            elif replied_msg.document:
                file_id = replied_msg.document.file_id
                logger.info(f"Processing protected document with file_id: {file_id}")
                send_protected_media(owner_id, file_id, 'document')  # Отправляем владельцу бизнес-аккаунта
            else:
                logger.debug("No supported media type found in message")

        except Exception as media_error:
            logger.error(f"Error processing media: {str(media_error)}", exc_info=True)

    except Exception as e:
        logger.error(f"Error handling self-destruct media: {str(e)}", exc_info=True)

def send_protected_media(chat_id: int, file_id: str, media_type: str):
    """
    Отправка защищенного медиа-контента путем:
    1. Получения файла через Telegram API
    2. Скачивания файла в память
    3. Переотправки как нового медиа-файла
    """
    try:
        logger.info(f"Getting file info for {media_type} with file_id: {file_id}")
        
        # Получаем информацию о файле из Telegram
        file = bot.get_file(file_id)
        
        if not file or not file.file_path:
            logger.error(f"Invalid file info received for file_id: {file_id}")
            return

        # Формируем полный URL для скачивания файла
        file_url = f"https://api.telegram.org/file/bot{bot.token}/{file.file_path}"
        logger.info(f"Downloading file from: {file_url}")
        
        # Скачиваем файл в память
        response = requests.get(file_url)
        if response.status_code != 200:
            logger.error(f"Failed to download file. Status code: {response.status_code}")
            return

        buffer = BytesIO(response.content)
        buffer.name = file.file_path.split('/')[-1]  # Важно для определения типа файла
        
        # Небольшая задержка для стабильности API
        time.sleep(0.1)

        # Выбираем метод отправки в зависимости от типа медиа
        sent_message = None
        if media_type == 'photo':
            sent_message = bot.send_photo(chat_id, buffer)
            logger.info(f"✅ Self-destruct photo saved and sent to {chat_id}")
        elif media_type == 'video':
            sent_message = bot.send_video(chat_id, buffer)
            logger.info(f"✅ Self-destruct video saved and sent to {chat_id}")
        elif media_type == 'video_note':
            sent_message = bot.send_video_note(chat_id, buffer)
            logger.info(f"✅ Self-destruct video note saved and sent to {chat_id}")
        elif media_type == 'voice':
            sent_message = bot.send_voice(chat_id, buffer)
            logger.info(f"✅ Self-destruct voice saved and sent to {chat_id}")
        elif media_type == 'audio':
            sent_message = bot.send_audio(chat_id, buffer)
            logger.info(f"✅ Self-destruct audio saved and sent to {chat_id}")
        elif media_type == 'animation':
            sent_message = bot.send_animation(chat_id, buffer)
            logger.info(f"✅ Self-destruct animation saved and sent to {chat_id}")
        elif media_type == 'sticker':
            sent_message = bot.send_sticker(chat_id, buffer)
            logger.info(f"✅ Self-destruct sticker saved and sent to {chat_id}")
        elif media_type == 'document':
            sent_message = bot.send_document(chat_id, buffer)
            logger.info(f"✅ Self-destruct document saved and sent to {chat_id}")
        else:
            logger.error(f"Unsupported media type: {media_type}")

        return sent_message

    except requests.RequestException as e:
        logger.error(f"Network error while downloading file: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Error sending protected {media_type}: {str(e)}", exc_info=True)
        return None


if __name__ == "__main__":
    # Пытаемся удалить webhook, но не критично если не получится (для polling режима)
    # Обрабатываем все возможные сетевые ошибки
    try:
        bot.remove_webhook()
        logger.info("Webhook успешно удален")
    except requests.exceptions.ConnectionError as e:
        logger.warning(f"Не удалось удалить webhook из-за сетевой ошибки (это нормально для polling режима): {str(e)}")
    except ConnectionError as e:
        logger.warning(f"Не удалось удалить webhook из-за ошибки подключения (это нормально для polling режима): {str(e)}")
    except OSError as e:
        logger.warning(f"Не удалось удалить webhook из-за системной ошибки (это нормально для polling режима): {str(e)}")
    except ApiTelegramException as e:
        logger.warning(f"Telegram API ошибка при удалении webhook (это нормально для polling режима): {str(e)}")
    except Exception as e:
        # Перехватываем все остальные исключения, включая вложенные
        error_type = type(e).__name__
        error_msg = str(e)
        logger.warning(f"Не удалось удалить webhook (это нормально для polling режима): {error_type}: {error_msg}")
        # Не прерываем выполнение - продолжаем запуск бота
    
    # Запускаем бота в режиме polling
    try:
        bot.polling(none_stop=True, interval=0, timeout=20)
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Критическая ошибка при запуске polling: {type(e).__name__}: {str(e)}")
        raise
