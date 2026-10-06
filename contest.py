import logging
from datetime import datetime
from supabase import create_client, Client
import os
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv('ton.env')
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def get_active_contest():
    try:
        result = supabase.table("contests") \
            .select("*") \
            .eq("is_active", True) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error(f"Error getting active contest: {e}")
        return None


def deactivate_all_contests() -> None:
    try:
        supabase.table("contests").update({"is_active": False}).eq("is_active", True).execute()
    except Exception as e:
        logger.error(f"Error deactivating contests: {e}")


def create_contest(required_referrals: int, post_text: str, photo_file_id: str, created_by: int):
    try:
        deactivate_all_contests()
        data = {
            "required_referrals": required_referrals,
            "post_text": post_text,
            "photo_file_id": photo_file_id,
            "is_active": True,
            "created_at": datetime.now().isoformat(),
            "created_by": created_by,
        }
        result = supabase.table("contests").insert(data).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error(f"Error creating contest: {e}")
        return None


def is_user_connected(user_id: int) -> bool:
    try:
        result = supabase.table("users").select("is_connected").eq("user_id", user_id).execute()
        return bool(result.data and result.data[0].get("is_connected"))
    except Exception as e:
        logger.error(f"Error checking user connection for {user_id}: {e}")
        return False


def is_participant(contest_id: int, user_id: int) -> bool:
    try:
        result = supabase.table("contest_participants") \
            .select("id") \
            .eq("contest_id", contest_id) \
            .eq("user_id", user_id) \
            .execute()
        return bool(result.data)
    except Exception as e:
        logger.error(f"Error checking participant: {e}")
        return False


def get_next_participant_number(contest_id: int) -> int:
    try:
        result = supabase.table("contest_participants") \
            .select("participant_number") \
            .eq("contest_id", contest_id) \
            .order("participant_number", desc=True) \
            .limit(1) \
            .execute()
        if result.data:
            return result.data[0]["participant_number"] + 1
        return 1
    except Exception as e:
        logger.error(f"Error getting next participant number: {e}")
        return 1


def register_participant(contest_id: int, user_id: int):
    try:
        if is_participant(contest_id, user_id):
            existing = supabase.table("contest_participants") \
                .select("participant_number") \
                .eq("contest_id", contest_id) \
                .eq("user_id", user_id) \
                .execute()
            return existing.data[0]["participant_number"] if existing.data else None

        number = get_next_participant_number(contest_id)
        supabase.table("contest_participants").insert({
            "contest_id": contest_id,
            "user_id": user_id,
            "participant_number": number,
            "joined_at": datetime.now().isoformat(),
        }).execute()
        return number
    except Exception as e:
        logger.error(f"Error registering participant: {e}")
        return None


def get_contest_stats(contest_id: int) -> dict:
    stats = {
        "participants_count": 0,
        "new_users_count": 0,
        "referrals_connected": 0,
        "participants": [],
    }
    try:
        contest = supabase.table("contests").select("*").eq("id", contest_id).execute()
        if not contest.data:
            return stats

        created_at = contest.data[0]["created_at"]

        participants = supabase.table("contest_participants") \
            .select("user_id, participant_number, joined_at") \
            .eq("contest_id", contest_id) \
            .order("participant_number") \
            .execute()
        stats["participants"] = participants.data or []
        stats["participants_count"] = len(stats["participants"])

        new_users = supabase.table("users") \
            .select("user_id") \
            .gte("first_seen", created_at) \
            .execute()
        stats["new_users_count"] = len(new_users.data or [])

        all_referrals = supabase.table("referrals") \
            .select("activated_at, created_at") \
            .eq("is_active", True) \
            .execute()
        stats["referrals_connected"] = sum(
            1 for ref in (all_referrals.data or [])
            if (ref.get("activated_at") or ref.get("created_at") or "") >= created_at
        )

    except Exception as e:
        logger.error(f"Error getting contest stats: {e}")

    return stats


def format_contest_conditions(contest: dict, user_id: int, referral_link, connected_count: int) -> str:
    required = contest["required_referrals"]
    text = (
        f"🎁 <b>Розыгрыш Telegram Premium</b>\n"
        f"<i>Приз — подписка Premium. Для участия Premium не нужен.</i>\n\n"
        f"<b>Условия участия:</b>\n"
        f"1. Бот должен быть подключён к вашему аккаунту\n"
        f"2. Пригласить <b>{required}</b> человек(а), которые тоже подключат бота\n"
        f"<i>Telegram Premium приглашённым не требуется.</i>\n\n"
        f"📊 <b>Ваш прогресс:</b>\n"
        f"• Бот подключён: {'✅' if is_user_connected(user_id) else '❌'}\n"
        f"• Приглашено (подключили бота): <b>{connected_count}/{required}</b>\n"
    )
    if referral_link:
        text += f"\n🔗 <b>Ваша реферальная ссылка:</b>\n{referral_link}\n"
    return text
