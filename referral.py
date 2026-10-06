import logging
from datetime import datetime, timedelta
from supabase import create_client, Client
import os
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv('ton.env')
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def generate_referral_link(user_id: int):
    try:
        user = supabase.table("users").select("user_id").eq("user_id", user_id).execute()
        if not user.data:
            logger.error(f"User {user_id} not found")
            return None

        bot_username = (os.getenv("BOT_USERNAME") or "spyglaz_bot").strip().lstrip("@")
        return f"https://t.me/{bot_username}?start=ref_{user_id}"

    except Exception as e:
        logger.error(f"Error generating referral link: {str(e)}")
        return None


def process_referral(user_id: int, referrer_id: int) -> bool:
    """Создаёт реферальную связь (ожидает подключения бота рефералом)."""
    try:
        if user_id == referrer_id:
            return False

        user = supabase.table("users").select("user_id").eq("user_id", user_id).execute()
        if not user.data:
            return False

        existing = supabase.table("referrals").select("user_id").eq("user_id", user_id).execute()
        if existing.data:
            return False

        referral_data = {
            "user_id": user_id,
            "referrer_id": referrer_id,
            "created_at": datetime.now().isoformat(),
            "is_active": False,
        }

        result = supabase.table("referrals").insert(referral_data).execute()
        return bool(result.data)

    except Exception as e:
        logger.error(f"Error processing referral: {str(e)}")
        return False


def complete_referral_on_connection(user_id: int):
    """
    Активирует реферала после подключения бота к аккаунту.
    Возвращает данные о реферере или None.
    """
    try:
        referral = supabase.table("referrals").select("*").eq("user_id", user_id).execute()
        if not referral.data or referral.data[0]["is_active"]:
            return None

        referral_info = referral.data[0]
        referrer_id = referral_info["referrer_id"]
        now = datetime.now().isoformat()

        update_data = {"is_active": True, "activated_at": now}
        try:
            supabase.table("referrals").update(update_data).eq("user_id", user_id).execute()
        except Exception:
            supabase.table("referrals").update({"is_active": True}).eq("user_id", user_id).execute()

        activate_referral_bonus(user_id)

        return {
            "referrer_id": referrer_id,
            "user_id": user_id,
            "activated_at": now,
        }

    except Exception as e:
        logger.error(f"Error completing referral on connection: {str(e)}")
        return None


def activate_referral_bonus(user_id: int) -> bool:
    """Активирует бонус для реферера (7 дней подписки)."""
    try:
        referral = supabase.table("referrals").select("*").eq("user_id", user_id).execute()
        if not referral.data:
            return False

        referral_info = referral.data[0]
        referrer_id = referral_info["referrer_id"]

        subscription = supabase.table("subscriptions").select("*").eq("user_id", referrer_id).execute()

        if subscription.data:
            current_end = datetime.fromisoformat(subscription.data[0]["end_date"])
            new_end = current_end + timedelta(days=7)
        else:
            new_end = datetime.now() + timedelta(days=7)

        update_result = supabase.table("subscriptions").update({
            "subscription_type": "referral",
            "start_date": datetime.now().isoformat(),
            "end_date": new_end.isoformat(),
            "payment_id": "referral_bonus",
        }).eq("user_id", referrer_id).execute()

        if not update_result.data:
            supabase.table("subscriptions").insert({
                "user_id": referrer_id,
                "subscription_type": "referral",
                "start_date": datetime.now().isoformat(),
                "end_date": new_end.isoformat(),
                "payment_id": "referral_bonus",
            }).execute()

        return True

    except Exception as e:
        logger.error(f"Error activating referral bonus: {str(e)}")
        return False


def get_connected_referrals_count(referrer_id: int, since: str = None) -> int:
    try:
        base_query = supabase.table("referrals") \
            .select("user_id") \
            .eq("referrer_id", referrer_id) \
            .eq("is_active", True)

        if since:
            try:
                result = base_query.gte("activated_at", since).execute()
            except Exception:
                result = base_query.gte("created_at", since).execute()
        else:
            result = base_query.execute()

        return len(result.data or [])

    except Exception as e:
        logger.error(f"Error counting connected referrals: {str(e)}")
        return 0


def get_referral_stats(user_id: int) -> dict:
    try:
        referrals = supabase.table("referrals").select("*").eq("referrer_id", user_id).execute()
        active_referrals = supabase.table("referrals") \
            .select("*") \
            .eq("referrer_id", user_id) \
            .eq("is_active", True) \
            .execute()

        return {
            "total_referrals": len(referrals.data or []),
            "active_referrals": len(active_referrals.data or []),
            "days_earned": len(active_referrals.data or []) * 7,
        }

    except Exception as e:
        logger.error(f"Error getting referral stats: {str(e)}")
        return {
            "total_referrals": 0,
            "active_referrals": 0,
            "days_earned": 0,
        }


def get_user_profile(user_id: int):
    try:
        user_info = supabase.table("users").select("*").eq("user_id", user_id).execute()
        if not user_info.data:
            return None

        subscription = supabase.table("subscriptions") \
            .select("*") \
            .eq("user_id", user_id) \
            .eq("is_active", True) \
            .execute()

        referral_stats = get_referral_stats(user_id)

        return {
            "user": user_info.data[0],
            "subscription": subscription.data[0] if subscription.data else None,
            "referral_stats": referral_stats,
        }
    except Exception as e:
        logger.error(f"Error getting user profile: {str(e)}")
        return None
