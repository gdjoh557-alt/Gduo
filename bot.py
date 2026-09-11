"""Arabic Telegram group protection bot.

The bot is intentionally self-contained and uses Telegram's Bot API plus SQLite.
Set BOT_TOKEN and ADMIN_ID in the hosting provider's secret/environment settings.
"""

from __future__ import annotations

import logging
import json
import os
import random
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telegram import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    User,
)
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .database import Database

load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger("group-guard")

BOT_TOKEN = os.getenv("BOT_TOKEN", os.getenv("BOT_TOKEN_", "")).strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", os.getenv("ADMIN_ID_", "")).strip()
ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW.lstrip("-").isdigit() else None
DB_PATH = os.getenv("DB_PATH", "data/bot.sqlite3")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN غير موجود. أضفه في Secrets قبل تشغيل البوت.")
if ADMIN_ID is None:
    raise RuntimeError("ADMIN_ID غير موجود أو غير صحيح. أضفه كرقم Telegram user id.")

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
db = Database(DB_PATH)

# In-memory flood windows are deliberately short-lived; persistent settings/actions
# stay in SQLite.
message_windows: dict[tuple[int, int], deque[float]] = defaultdict(deque)
pending_captcha: set[tuple[int, int]] = set()
seen_users: dict[tuple[int, str], User] = {}

ROLE_LEVEL = {
    "member": 0,
    "moderator": 1,
    "admin": 2,
    "senior_admin": 3,
    "owner": 4,
}
ROLE_AR = {
    "owner": "المالك",
    "senior_admin": "الأدمن الرئيسي",
    "admin": "الأدمن",
    "moderator": "المشرف المساعد",
    "member": "عضو",
}
PROTECTED_ROLE_LEVEL = ROLE_LEVEL["moderator"]


def now_ts() -> int:
    return int(time.time())


def display_user(user: User | None) -> str:
    if not user:
        return "غير معروف"
    name = (user.full_name or "بدون اسم").replace("<", "").replace(">", "")
    return f"{name} (@{user.username})" if user.username else name


def mention_user(user: User | None) -> str:
    if not user:
        return "العضو"
    return f'<a href="tg://user?id={user.id}">{display_user(user)}</a>'


def clean_reason(text: str | None) -> str:
    text = (text or "").strip()
    return text or "سبب غير محدد"


def parse_duration(value: str) -> int | None:
    match = re.fullmatch(r"(\d+)\s*([mhdw])", value.lower())
    if not match:
        return None
    number = int(match.group(1))
    unit = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]
    seconds = number * unit
    return seconds if 0 < seconds <= 365 * 86400 else None


def duration_ar(seconds: int) -> str:
    if seconds % 604800 == 0:
        return f"{seconds // 604800} أسبوع"
    if seconds % 86400 == 0:
        return f"{seconds // 86400} يوم"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} ساعة"
    return f"{seconds // 60} دقيقة"


def is_group(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP))


async def reply_no_permission(update: Update) -> None:
    if update.effective_message:
        await update.effective_message.reply_text("ما عندك صلاحية لتنفيذ هذا الأمر.")


async def role_for(chat_id: int, user: User | None, context: ContextTypes.DEFAULT_TYPE) -> str:
    if not user:
        return "member"
    if user.id == ADMIN_ID:
        return "owner"
    stored = db.role(chat_id, user.id)
    if stored:
        return stored
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        if member.status == ChatMemberStatus.OWNER:
            return "owner"
        if member.status == ChatMemberStatus.ADMINISTRATOR:
            return "admin"
    except Exception:
        logger.debug("Could not inspect Telegram admin status", exc_info=True)
    return "member"


async def require_level(
    update: Update, context: ContextTypes.DEFAULT_TYPE, minimum: str
) -> bool:
    if not is_group(update):
        if update.effective_message:
            await update.effective_message.reply_text("هذا الأمر يعمل داخل المجموعة فقط.")
        return False
    if not db.is_activated(update.effective_chat.id):
        if update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ يجب تفعيل البوت أولًا بكتابة: تفعيل"
            )
        return False
    role = await role_for(update.effective_chat.id, update.effective_user, context)
    if ROLE_LEVEL.get(role, 0) < ROLE_LEVEL[minimum]:
        await reply_no_permission(update)
        return False
    return True


async def target_from_message(
    update: Update, args: list[str], context: ContextTypes.DEFAULT_TYPE
) -> tuple[User | None, list[str]]:
    message = update.effective_message
    if message and message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user, args
    if not args:
        return None, args
    raw = args[0]
    if raw.startswith("@"):
        cached = seen_users.get((update.effective_chat.id, raw[1:].casefold()))
        if cached:
            return cached, args[1:]
        try:
            # Telegram's getChatMember requires a numeric user id. A username
            # becomes resolvable after the bot has seen that user in the chat.
            return None, args
        except Exception:
            return None, args
    if raw.lstrip("-").isdigit():
        try:
            member = await context.bot.get_chat_member(update.effective_chat.id, int(raw))
            return member.user, args[1:]
        except Exception:
            return None, args
    return None, args


async def can_act_on(
    update: Update, context: ContextTypes.DEFAULT_TYPE, target: User, minimum: str
) -> bool:
    actor_role = await role_for(update.effective_chat.id, update.effective_user, context)
    target_role = await role_for(update.effective_chat.id, target, context)
    if target.id == update.effective_user.id:
        await update.effective_message.reply_text("لا يمكنك تنفيذ هذا الأمر على نفسك.")
        return False
    if target.id == getattr(context.bot, "id", None):
        await update.effective_message.reply_text("لا يمكن تنفيذ أمر إداري على البوت نفسه.")
        return False
    if target.id == ADMIN_ID or target_role == "owner":
        await update.effective_message.reply_text("لا يمكن تنفيذ أي عقوبة على المالك.")
        return False
    if ROLE_LEVEL.get(actor_role, 0) < ROLE_LEVEL.get(target_role, 0):
        await update.effective_message.reply_text("لا يمكنك تنفيذ إجراء على أدمن أعلى منك رتبة.")
        return False
    if ROLE_LEVEL.get(actor_role, 0) < ROLE_LEVEL[minimum]:
        await reply_no_permission(update)
        return False
    return True


async def bot_is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(
            update.effective_chat.id, context.bot.id
        )
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False


async def require_bot_permissions(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *permissions: str
) -> bool:
    """Check the exact Telegram administrator flags needed by an operation."""
    try:
        member = await context.bot.get_chat_member(
            update.effective_chat.id, context.bot.id
        )
    except Exception:
        await update.effective_message.reply_text("❌ تعذر التحقق من صلاحيات البوت.")
        return False
    if member.status == ChatMemberStatus.OWNER:
        return True
    if member.status != ChatMemberStatus.ADMINISTRATOR:
        await update.effective_message.reply_text("❌ يجب ترقية البوت إلى مشرف أولًا.")
        return False
    labels = {
        "can_delete_messages": "حذف الرسائل",
        "can_restrict_members": "تقييد الأعضاء",
        "can_promote_members": "إضافة مشرفين",
        "can_pin_messages": "تثبيت الرسائل",
        "can_invite_users": "دعوة الأعضاء",
        "can_change_info": "تعديل معلومات المجموعة",
    }
    missing = [labels.get(p, p) for p in permissions if not getattr(member, p, False)]
    if missing:
        await update.effective_message.reply_text(
            "❌ لا أملك صلاحية: " + "، ".join(f"«{item}»" for item in missing)
            + ".\nالرجاء منحي هذه الصلاحية من إعدادات المجموعة."
        )
        return False
    return True


async def log_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    target: User | None = None,
    duration: str | None = None,
    reason: str | None = None,
) -> None:
    chat_id = update.effective_chat.id
    admin_id = update.effective_user.id if update.effective_user else None
    db.add_action(chat_id, target.id if target else None, action, duration, reason, admin_id)
    settings = db.settings(chat_id)
    log_chat_id = settings.get("log_chat_id")
    if not log_chat_id:
        return
    target_text = mention_user(target) if target else "—"
    admin_text = mention_user(update.effective_user)
    text = (
        f"🧾 <b>سجل إجراء</b>\n"
        f"• الإجراء: <b>{action}</b>\n"
        f"• العضو: {target_text}\n"
        f"• المدة: {duration or '—'}\n"
        f"• السبب: {clean_reason(reason)}\n"
        f"• المنفذ: {admin_text}\n"
        f"• الوقت: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    try:
        await context.bot.send_message(log_chat_id, text, parse_mode="HTML")
    except Exception:
        logger.warning("Could not send action to log chat %s", log_chat_id, exc_info=True)


async def apply_mute(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, seconds: int | None = None
) -> None:
    permissions = ChatPermissions(can_send_messages=False)
    until = now_ts() + seconds if seconds else None
    await context.bot.restrict_chat_member(
        chat_id, user_id, permissions=permissions, until_date=until
    )
    if seconds:
        db.add_temp_restriction(chat_id, user_id, "mute", until)


async def apply_unmute(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> None:
    permissions = ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )
    await context.bot.restrict_chat_member(chat_id, user_id, permissions=permissions)
    db.remove_temp_restriction(chat_id, user_id, "mute")


async def do_ban(
    update: Update, context: ContextTypes.DEFAULT_TYPE, target: User, reason: str, seconds: int | None = None
) -> bool:
    chat_id = update.effective_chat.id
    try:
        until = now_ts() + seconds if seconds else None
        await context.bot.ban_chat_member(chat_id, target.id, until_date=until)
        if seconds:
            db.add_temp_restriction(chat_id, target.id, "ban", until)
        return True
    except Exception as exc:
        logger.info("Ban failed: %s", exc)
        await update.effective_message.reply_text(
            "تعذر تنفيذ الحظر. تأكد أن البوت أدمن ولديه صلاحية حظر الأعضاء."
        )
        return False


async def command_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "🛡️ <b>حارس المجموعة</b>\n\n"
        "أضفني كمشرف مع صلاحيات حذف الرسائل وتقييد وحظر الأعضاء، ثم اكتب «تفعيل» داخل المجموعة.",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["الأوامر الخدمية", "الحماية"],
            ["أوامر النسب", "أوامر التسلية"],
            ["مبرمج السورس", "السورس"],
        ],
        resize_keyboard=True,
    )


def back_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([["رجوع"]], resize_keyboard=True)


async def command_activate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_group(update):
        await update.effective_message.reply_text("⚠️ أمر التفعيل يعمل داخل المجموعة فقط.")
        return
    actor_role = await role_for(update.effective_chat.id, update.effective_user, context)
    if ROLE_LEVEL.get(actor_role, 0) < ROLE_LEVEL["admin"]:
        await reply_no_permission(update)
        return
    if not await require_bot_permissions(
        update, context, "can_delete_messages", "can_restrict_members"
    ):
        return
    db.activate_group(update.effective_chat.id, update.effective_user.id)
    bot_user = await context.bot.get_me()
    activator = (
        f"@{update.effective_user.username}"
        if update.effective_user.username
        else mention_user(update.effective_user)
    )
    await update.effective_message.reply_text(
        "━━━━━━━━━━━━━━━\n"
        "🎉 <b>تم تفعيل البوت بنجاح!</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"🤖 <b>الاسم:</b> @{bot_user.username or bot_user.first_name}\n"
        f"👤 <b>تم التفعيل بواسطة:</b> {activator}\n"
        "✨ جاهز للعمل الآن!\n"
        "━━━━━━━━━━━━━━━",
        parse_mode="HTML",
    )


async def command_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "📋 اختر القسم المطلوب من القائمة:",
        reply_markup=main_keyboard(),
    )


async def menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    text = (update.effective_message.text or "").strip()
    if text == "رجوع":
        await command_menu(update, context)
        return True
    if text == "الأوامر الخدمية":
        await update.effective_message.reply_text(
            "📋 <b>الأوامر الخدمية</b>\n\n"
            "معلومات — عرض بيانات عضو بالرد\n"
            "القوانين — عرض قوانين المجموعة\n"
            "الردود — عرض الردود التلقائية\n"
            "تفعيل — تفعيل البوت في المجموعة",
            parse_mode="HTML", reply_markup=back_keyboard(),
        )
        return True
    if text == "الحماية":
        await update.effective_message.reply_text(
            "🛡️ <b>الحماية</b>\n\n"
            "✨ أضف البوت في المجموعة ثم قم برفعه مشرفًا وأرسل «تفعيل».\n"
            "✨ استخدم /settings أو «الإعدادات» لإدارة خصائص الحماية.",
            parse_mode="HTML", reply_markup=back_keyboard(),
        )
        return True
    if text == "أوامر النسب":
        await update.effective_message.reply_text(
            "💫 <b>أهلًا بك في أوامر النسب</b>\n\n"
            "نسبة الحب — نسبة عشوائية\n"
            "نسبة الذكاء — نسبة عشوائية\n"
            "نسبة الغباء — نسبة عشوائية",
            parse_mode="HTML", reply_markup=back_keyboard(),
        )
        return True
    if text == "أوامر التسلية":
        await update.effective_message.reply_text(
            "🎭 <b>أوامر التسلية</b>\n\n"
            "ميمز — أمر ترفيهي\n"
            "شعر — أمر ترفيهي\n"
            "فلم — أمر ترفيهي",
            parse_mode="HTML", reply_markup=back_keyboard(),
        )
        return True
    if text == "مبرمج السورس":
        await update.effective_message.reply_text(
            "👨‍💻 تم تطوير هذا السورس ليكون قابلًا للتخصيص والاستضافة الخارجية.",
            reply_markup=back_keyboard(),
        )
        return True
    if text == "السورس":
        await update.effective_message.reply_text(
            "📦 سورس حارس المجموعة — Python + SQLite.",
            reply_markup=back_keyboard(),
        )
        return True
    return False


async def command_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🛡️ <b>أوامر حارس المجموعة</b>\n\n"
        "<b>العقوبات:</b>\n"
        "/kick /ban /unban /tban /mute /unmute\n"
        "/warn /unwarn /warns\n"
        "/del /purge\n\n"
        "<b>الحماية:</b>\n"
        "/addword /delword /wordlist\n"
        "/setspam /setflood /setcaptcha /setcaptchatime\n"
        "/lock /unlock /trust /untrust\n\n"
        "<b>الإدارة:</b>\n"
        "/promote /demote /adminlist /setlogchat\n"
        "/setwelcome /setrules /rules /settings\n"
        "/log /history /info\n\n"
        "<b>الأوامر العربية:</b>\n"
        "تفعيل، حظر، طرد، كتم، تحذير، رفع مشرف، تنزيل مشرف، تثبيت، حذف، معلومات\n"
        "اضف رد، حذف رد، تعديل رد، الردود، القائمة\n\n"
        "يمكن استهداف العضو بالرد على رسالته أو بكتابة المعرف/الرقم."
    )
    await update.effective_message.reply_text(text, parse_mode="HTML")


async def command_kick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "admin"):
        return
    if not await require_bot_permissions(update, context, "can_restrict_members"):
        return
    target, rest = await target_from_message(update, context.args, context)
    if not target or not await can_act_on(update, context, target, "admin"):
        await update.effective_message.reply_text("استخدم الأمر بالرد على رسالة العضو أو: /kick @user السبب")
        return
    reason = clean_reason(" ".join(rest))
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
        await context.bot.unban_chat_member(update.effective_chat.id, target.id, only_if_banned=True)
        await log_action(update, context, "طرد", target, reason=reason)
        await update.effective_message.reply_text(
            f"✅ تم طرد {mention_user(target)}\nالسبب: {reason}\nالمنفذ: {mention_user(update.effective_user)}",
            parse_mode="HTML",
        )
    except Exception:
        await update.effective_message.reply_text("تعذر الطرد. تأكد من صلاحيات البوت.")


async def command_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "admin"):
        return
    if not await require_bot_permissions(update, context, "can_restrict_members"):
        return
    target, rest = await target_from_message(update, context.args, context)
    if not target:
        await update.effective_message.reply_text("استخدم: /ban @user السبب أو بالرد على رسالة.")
        return
    if not await can_act_on(update, context, target, "admin"):
        return
    reason = clean_reason(" ".join(rest))
    if await do_ban(update, context, target, reason):
        await log_action(update, context, "حظر دائم", target, reason=reason)
        await update.effective_message.reply_text(
            f"🚫 تم حظر {mention_user(target)} نهائيًا.\nالسبب: {reason}",
            parse_mode="HTML",
        )


async def command_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "admin"):
        return
    if not await require_bot_permissions(update, context, "can_restrict_members"):
        return
    target, _ = await target_from_message(update, context.args, context)
    if not target or not await can_act_on(update, context, target, "admin"):
        await update.effective_message.reply_text("استخدم: /unban @user أو اكتب رقم العضو.")
        return
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, target.id, only_if_banned=True)
        db.remove_temp_restriction(update.effective_chat.id, target.id, "ban")
        await log_action(update, context, "رفع الحظر", target)
        await update.effective_message.reply_text(f"✅ تم رفع الحظر عن {mention_user(target)}.", parse_mode="HTML")
    except Exception:
        await update.effective_message.reply_text("تعذر رفع الحظر.")


async def command_tban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "admin"):
        return
    if not await require_bot_permissions(update, context, "can_restrict_members"):
        return
    if context.args and context.args[0].lower() == "list":
        rows = db.temp_restrictions(update.effective_chat.id)
        bans = [r for r in rows if r["kind"] == "ban"]
        if not bans:
            await update.effective_message.reply_text("لا توجد حظورات مؤقتة حاليًا.")
            return
        lines = ["⏳ <b>الحظورات المؤقتة</b>"]
        for row in bans:
            remaining = max(0, row["until_ts"] - now_ts())
            lines.append(f"• <code>{row['user_id']}</code> — متبقٍ {duration_ar(remaining)}")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")
        return
    target, rest = await target_from_message(update, context.args, context)
    if not target or not rest:
        await update.effective_message.reply_text("استخدم: /tban @user 2h السبب أو بالرد.")
        return
    seconds = parse_duration(rest[0])
    if not seconds:
        await update.effective_message.reply_text("صيغة المدة غير صحيحة. استخدم مثلًا 30m أو 2h أو 7d.")
        return
    if not await can_act_on(update, context, target, "admin"):
        return
    reason = clean_reason(" ".join(rest[1:]))
    if await do_ban(update, context, target, reason, seconds):
        readable = duration_ar(seconds)
        await log_action(update, context, "حظر مؤقت", target, readable, reason)
        await update.effective_message.reply_text(
            f"⏳ تم حظر {mention_user(target)} لمدة {readable}.\nالسبب: {reason}",
            parse_mode="HTML",
        )


async def command_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "moderator"):
        return
    if not await require_bot_permissions(update, context, "can_restrict_members"):
        return
    target, rest = await target_from_message(update, context.args, context)
    if not target:
        await update.effective_message.reply_text("استخدم: /mute @user 30m أو بالرد.")
        return
    permanent = bool(rest and rest[0].lower() in {"دائم", "دائمة", "permanent"})
    seconds = parse_duration(rest[0]) if rest and not permanent else None
    if rest and not seconds:
        if not permanent:
            await update.effective_message.reply_text("صيغة المدة غير صحيحة مثل 30m أو 2h أو دائم.")
            return
    if rest and permanent:
        seconds = None
    if rest and not permanent and not seconds:
        return
    if not await can_act_on(update, context, target, "moderator"):
        return
    try:
        await apply_mute(context, update.effective_chat.id, target.id, seconds)
        readable = duration_ar(seconds) if seconds else "حتى إشعار آخر"
        await log_action(update, context, "كتم", target, readable)
        await update.effective_message.reply_text(
            f"🔇 تم كتم {mention_user(target)} لمدة {readable}.", parse_mode="HTML"
        )
    except Exception:
        await update.effective_message.reply_text("تعذر الكتم. تأكد أن البوت أدمن.")


async def command_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "moderator"):
        return
    if not await require_bot_permissions(update, context, "can_restrict_members"):
        return
    target, _ = await target_from_message(update, context.args, context)
    if not target or not await can_act_on(update, context, target, "moderator"):
        await update.effective_message.reply_text("استخدم: /unmute @user أو بالرد.")
        return
    try:
        await apply_unmute(context, update.effective_chat.id, target.id)
        await log_action(update, context, "رفع الكتم", target)
        await update.effective_message.reply_text(f"✅ تم رفع الكتم عن {mention_user(target)}.", parse_mode="HTML")
    except Exception:
        await update.effective_message.reply_text("تعذر رفع الكتم.")


async def command_warn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "moderator"):
        return
    target, rest = await target_from_message(update, context.args, context)
    if not target or not await can_act_on(update, context, target, "moderator"):
        await update.effective_message.reply_text("استخدم: /warn @user السبب أو بالرد.")
        return
    chat_id = update.effective_chat.id
    reason = clean_reason(" ".join(rest))
    db.add_warning(chat_id, target.id, reason, update.effective_user.id)
    count = db.active_warning_count(chat_id, target.id)
    settings = db.settings(chat_id)
    limit = settings["warn_limit"]
    await log_action(update, context, "تحذير", target, f"{count}/{limit}", reason)
    action_text = ""
    if count >= limit:
        if settings["warn_action"] == "mute":
            seconds = 86400
            try:
                await apply_mute(context, chat_id, target.id, seconds)
                action_text = " وتم كتمه 24 ساعة"
            except Exception:
                action_text = " (تعذر تنفيذ الكتم تلقائيًا)"
        elif settings["warn_action"] == "tban":
            if await do_ban(update, context, target, reason, 7 * 86400):
                action_text = " وتم حظره 7 أيام"
        elif settings["warn_action"] == "ban":
            if await do_ban(update, context, target, reason):
                action_text = " وتم حظره نهائيًا"
        elif settings["warn_action"] == "kick":
            try:
                await context.bot.ban_chat_member(chat_id, target.id)
                await context.bot.unban_chat_member(chat_id, target.id, only_if_banned=True)
                action_text = " وتم طرده"
            except Exception:
                action_text = " (تعذر تنفيذ الطرد تلقائيًا)"
    await update.effective_message.reply_text(
        f"⚠️ تحذير لـ {mention_user(target)}: {count}/{limit}\n"
        f"السبب: {reason}{action_text}",
        parse_mode="HTML",
    )


async def command_unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "moderator"):
        return
    target, _ = await target_from_message(update, context.args, context)
    if not target or not await can_act_on(update, context, target, "moderator"):
        await update.effective_message.reply_text("استخدم: /unwarn @user أو بالرد.")
        return
    if db.remove_last_warning(update.effective_chat.id, target.id):
        await log_action(update, context, "إزالة آخر تحذير", target)
        await update.effective_message.reply_text(f"✅ أزيل آخر تحذير عن {mention_user(target)}.", parse_mode="HTML")
    else:
        await update.effective_message.reply_text("لا توجد تحذيرات نشطة لهذا العضو.")


async def command_warns(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target, _ = await target_from_message(update, context.args, context)
    if is_group(update) and not target and context.args:
        await update.effective_message.reply_text("لم أجد العضو. استخدم رقم العضو أو الرد على رسالته.")
        return
    if not target and update.effective_user:
        target = update.effective_user
    if not target:
        await update.effective_message.reply_text("استخدم: /warns @user أو بالرد.")
        return
    rows = db.warnings(update.effective_chat.id, target.id)
    if not rows:
        await update.effective_message.reply_text(f"✅ لا توجد تحذيرات نشطة على {display_user(target)}.")
        return
    lines = [f"⚠️ <b>تحذيرات {display_user(target)}</b> ({len(rows)})"]
    for row in rows:
        lines.append(f"• {row['reason']} — {row['created_at'][:16].replace('T', ' ')}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def command_setwarnlimit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "senior_admin"):
        return
    if not context.args or not context.args[0].isdigit() or not 1 <= int(context.args[0]) <= 20:
        await update.effective_message.reply_text("استخدم: /setwarnlimit 3 (من 1 إلى 20).")
        return
    db.update_settings(update.effective_chat.id, warn_limit=int(context.args[0]))
    await update.effective_message.reply_text(f"✅ أصبح حد التحذيرات {context.args[0]}.")


async def command_setwarnaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "senior_admin"):
        return
    if len(context.args) < 2 or not context.args[0].isdigit() or context.args[1] not in {"kick", "ban", "tban", "mute"}:
        await update.effective_message.reply_text("استخدم: /setwarnaction 3 mute")
        return
    db.update_settings(update.effective_chat.id, warn_limit=int(context.args[0]), warn_action=context.args[1])
    await update.effective_message.reply_text("✅ تم تحديث إجراء الوصول إلى حد التحذيرات.")


async def command_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "moderator"):
        return
    if not await require_bot_permissions(update, context, "can_delete_messages"):
        return
    message = update.effective_message
    if not message.reply_to_message:
        await message.reply_text("يجب استخدام /del بالرد على الرسالة.")
        return
    try:
        await context.bot.delete_message(update.effective_chat.id, message.reply_to_message.message_id)
        await context.bot.delete_message(update.effective_chat.id, message.message_id)
        await log_action(update, context, "حذف رسالة")
    except Exception:
        await message.reply_text("تعذر حذف الرسالة. تأكد من صلاحيات البوت.")


async def command_purge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "admin"):
        return
    if not await require_bot_permissions(update, context, "can_delete_messages"):
        return
    message = update.effective_message
    ids: list[int] = []
    if message.reply_to_message:
        start = message.reply_to_message.message_id
        end = message.message_id
        ids = list(range(start, end + 1))
    elif context.args and context.args[0].isdigit():
        amount = min(int(context.args[0]), 100)
        ids = list(range(message.message_id - amount, message.message_id + 1))
    else:
        await message.reply_text("استخدم /purge بالرد أو /purge 20.")
        return
    deleted = 0
    for message_id in ids:
        try:
            await context.bot.delete_message(update.effective_chat.id, message_id)
            deleted += 1
        except Exception:
            pass
    await log_action(update, context, "حذف جماعي", reason=f"{deleted} رسالة")


async def command_addword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "admin"):
        return
    word = " ".join(context.args).strip()
    if not word:
        await update.effective_message.reply_text("استخدم: /addword كلمة")
        return
    db.add_word(update.effective_chat.id, word)
    await update.effective_message.reply_text("✅ أضيفت الكلمة إلى القائمة السوداء.")


async def command_delword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "admin"):
        return
    word = " ".join(context.args).strip()
    if not word:
        await update.effective_message.reply_text("استخدم: /delword كلمة")
        return
    db.remove_word(update.effective_chat.id, word)
    await update.effective_message.reply_text("✅ حُذفت الكلمة من القائمة السوداء.")


async def command_wordlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "moderator"):
        return
    words = db.word_list(update.effective_chat.id)
    await update.effective_message.reply_text(
        "📚 القائمة فارغة." if not words else "📚 الكلمات:\n" + "\n".join(f"• {w}" for w in words)
    )


async def command_setflood(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "senior_admin"):
        return
    if len(context.args) < 3 or not all(x.isdigit() for x in context.args[:2]) or context.args[2] not in {"mute", "warn", "delete"}:
        await update.effective_message.reply_text("استخدم: /setflood 5 10 mute")
        return
    count, seconds = int(context.args[0]), int(context.args[1])
    if not 2 <= count <= 100 or not 2 <= seconds <= 300:
        await update.effective_message.reply_text("القيم المسموحة: الرسائل 2-100 والثواني 2-300.")
        return
    db.update_settings(update.effective_chat.id, flood_count=count, flood_seconds=seconds, flood_action=context.args[2])
    await update.effective_message.reply_text("✅ تم تحديث إعدادات مكافحة الفيضان.")


async def command_setspam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "senior_admin"):
        return
    if not context.args or context.args[0].lower() not in {"delete", "warn", "mute"}:
        await update.effective_message.reply_text("استخدم: /setspam delete أو /setspam warn أو /setspam mute")
        return
    db.update_settings(update.effective_chat.id, spam_action=context.args[0].lower())
    await update.effective_message.reply_text("✅ تم تحديث إجراء مكافحة الروابط والكلمات الممنوعة.")


async def command_setcaptcha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "senior_admin"):
        return
    if not context.args or context.args[0].lower() not in {"on", "off"}:
        await update.effective_message.reply_text("استخدم: /setcaptcha on أو /setcaptcha off")
        return
    enabled = context.args[0].lower() == "on"
    db.update_settings(update.effective_chat.id, captcha_enabled=int(enabled))
    await update.effective_message.reply_text(f"✅ تم {'تفعيل' if enabled else 'تعطيل'} التحقق عند الدخول.")


async def command_setcaptchatime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "senior_admin"):
        return
    if not context.args or not context.args[0].isdigit() or not 1 <= int(context.args[0]) <= 60:
        await update.effective_message.reply_text("استخدم: /setcaptchatime 5 (من 1 إلى 60 دقيقة).")
        return
    db.update_settings(update.effective_chat.id, captcha_minutes=int(context.args[0]))
    await update.effective_message.reply_text("✅ تم تحديث مهلة التحقق.")


async def command_lock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "admin"):
        return
    if not await require_bot_permissions(update, context, "can_restrict_members"):
        return
    try:
        await context.bot.set_chat_permissions(
            update.effective_chat.id, ChatPermissions(can_send_messages=False)
        )
        db.update_settings(update.effective_chat.id, lockdown=1)
        await log_action(update, context, "قفل طارئ")
        await update.effective_message.reply_text("🔒 تم تفعيل القفل الطارئ. الأعضاء لا يستطيعون الكتابة.")
    except Exception:
        await update.effective_message.reply_text("تعذر تفعيل القفل.")


async def command_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "admin"):
        return
    if not await require_bot_permissions(update, context, "can_restrict_members"):
        return
    try:
        await context.bot.set_chat_permissions(
            update.effective_chat.id,
            ChatPermissions(
                can_send_messages=True, can_send_audios=True, can_send_documents=True,
                can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                can_send_voice_notes=True, can_send_polls=True,
                can_send_other_messages=True, can_add_web_page_previews=True,
            ),
        )
        db.update_settings(update.effective_chat.id, lockdown=0)
        await log_action(update, context, "فتح القفل")
        await update.effective_message.reply_text("🔓 تم رفع القفل الطارئ.")
    except Exception:
        await update.effective_message.reply_text("تعذر رفع القفل.")


async def command_trust(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "admin"):
        return
    target, _ = await target_from_message(update, context.args, context)
    if not target:
        await update.effective_message.reply_text("استخدم: /trust @user أو بالرد.")
        return
    db.set_trusted(update.effective_chat.id, target.id, True)
    await update.effective_message.reply_text(f"✅ تمت إضافة {mention_user(target)} إلى الموثوقين.", parse_mode="HTML")


async def command_untrust(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "admin"):
        return
    target, _ = await target_from_message(update, context.args, context)
    if not target:
        await update.effective_message.reply_text("استخدم: /untrust @user أو بالرد.")
        return
    db.set_trusted(update.effective_chat.id, target.id, False)
    await update.effective_message.reply_text(f"✅ أزيل {mention_user(target)} من الموثوقين.", parse_mode="HTML")


async def command_promote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "senior_admin"):
        return
    if not await require_bot_permissions(update, context, "can_promote_members"):
        return
    target, rest = await target_from_message(update, context.args, context)
    role = rest[0].lower() if rest else "admin"
    if not target or role not in {"senior_admin", "admin", "moderator"}:
        await update.effective_message.reply_text("استخدم: /promote @user admin|moderator")
        return
    actor_role = await role_for(update.effective_chat.id, update.effective_user, context)
    if role == "senior_admin" and actor_role != "owner":
        await update.effective_message.reply_text("ترقية الأدمن الرئيسي متاحة للمالك فقط.")
        return
    target_role = await role_for(update.effective_chat.id, target, context)
    if ROLE_LEVEL.get(target_role, 0) >= ROLE_LEVEL.get(actor_role, 0):
        await update.effective_message.reply_text("لا يمكنك ترقية عضو إلى رتبة مساوية أو أعلى من رتبتك.")
        return
    try:
        await context.bot.promote_chat_member(
            update.effective_chat.id,
            target.id,
            can_change_info=role == "senior_admin",
            can_delete_messages=True,
            can_restrict_members=True,
            can_invite_users=True,
            can_pin_messages=True,
            can_promote_members=role == "senior_admin",
        )
    except Exception:
        await update.effective_message.reply_text("❌ تعذرت الترقية. تأكد من صلاحيات «إضافة مشرفين».")
        return
    db.set_role(update.effective_chat.id, target.id, role)
    previous = db.previous_user(
        update.effective_chat.id, target.id, target.username, target.first_name, target.last_name
    )
    previous_text = "لا يوجد"
    if previous:
        previous_text = (
            f"@{previous['username']}" if previous["username"]
            else previous["first_name"] or "اسم سابق غير معروف"
        )
    current_username = f"@{target.username}" if target.username else "لا يوجد"
    await log_action(update, context, "ترقية مشرف", target, role)
    await update.effective_message.reply_text(
        "━━━━━━━━━━━━━━━\n"
        "⭐️ <b>تمت ترقية عضو جديد إلى مشرف</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"👤 <b>الاسم:</b> {display_user(target)}\n"
        f"🆔 <b>الآيدي:</b> <code>{target.id}</code>\n"
        f"🔗 <b>اليوزر الحالي:</b> {current_username}\n"
        f"📜 <b>اليوزر/الاسم السابق:</b> {previous_text}\n"
        f"👮 <b>رقّاه:</b> {mention_user(update.effective_user)}\n"
        "━━━━━━━━━━━━━━━",
        parse_mode="HTML",
    )


async def command_demote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "senior_admin"):
        return
    if not await require_bot_permissions(update, context, "can_promote_members"):
        return
    target, _ = await target_from_message(update, context.args, context)
    if not target:
        await update.effective_message.reply_text("استخدم: /demote @user أو بالرد.")
        return
    actor_role = await role_for(update.effective_chat.id, update.effective_user, context)
    target_role = await role_for(update.effective_chat.id, target, context)
    if target_role == "owner" or ROLE_LEVEL.get(target_role, 0) >= ROLE_LEVEL.get(actor_role, 0):
        await update.effective_message.reply_text("لا يمكنك تنزيل هذا العضو.")
        return
    try:
        await context.bot.promote_chat_member(
            update.effective_chat.id, target.id,
            can_change_info=False, can_delete_messages=False,
            can_restrict_members=False, can_invite_users=False,
            can_pin_messages=False, can_promote_members=False,
        )
    except Exception:
        await update.effective_message.reply_text("❌ تعذر تنزيل المشرف.")
        return
    db.remove_role(update.effective_chat.id, target.id)
    await log_action(update, context, "تنزيل مشرف", target)
    await update.effective_message.reply_text(
        "━━━━━━━━━━━━━━━\n"
        "⬇️ <b>تم تنزيل المشرف</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"👤 <b>الاسم:</b> {display_user(target)}\n"
        f"🆔 <b>الآيدي:</b> <code>{target.id}</code>\n"
        f"🔗 <b>اليوزر:</b> @{target.username if target.username else 'لا يوجد'}\n"
        f"👮 <b>بواسطة:</b> {mention_user(update.effective_user)}\n"
        "━━━━━━━━━━━━━━━",
        parse_mode="HTML",
    )


async def command_pin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "admin"):
        return
    if not await require_bot_permissions(update, context, "can_pin_messages"):
        return
    replied = update.effective_message.reply_to_message
    if not replied:
        await update.effective_message.reply_text("استخدم «تثبيت» بالرد على الرسالة المطلوبة.")
        return
    try:
        await context.bot.pin_chat_message(
            update.effective_chat.id, replied.message_id,
            disable_notification=not bool(context.args and context.args[0] in {"اشعار", "إشعار"}),
        )
        await log_action(update, context, "تثبيت رسالة")
        await update.effective_message.reply_text("📌 تم تثبيت الرسالة.")
    except Exception:
        await update.effective_message.reply_text("❌ تعذر تثبيت الرسالة.")


async def command_unpin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "admin"):
        return
    if not await require_bot_permissions(update, context, "can_pin_messages"):
        return
    try:
        await context.bot.unpin_chat_message(update.effective_chat.id)
        await log_action(update, context, "فك تثبيت")
        await update.effective_message.reply_text("✅ تم فك تثبيت الرسالة.")
    except Exception:
        await update.effective_message.reply_text("❌ تعذر فك التثبيت.")


async def command_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_group(update):
        await update.effective_message.reply_text("هذا الأمر يعمل داخل المجموعة.")
        return
    target, _ = await target_from_message(update, context.args, context)
    if not target:
        target = update.effective_user
    db.remember_user(
        update.effective_chat.id, target.id, target.username,
        target.first_name, target.last_name,
    )
    role = await role_for(update.effective_chat.id, target, context)
    first_seen = db.first_seen(update.effective_chat.id, target.id) or "غير مسجل"
    username = f"@{target.username}" if target.username else "لا يوجد"
    await update.effective_message.reply_text(
        "━━━━━━━━━━━━━━━\n"
        "📋 <b>معلومات العضو</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"👤 <b>الاسم:</b> {target.full_name}\n"
        f"🆔 <b>الآيدي:</b> <code>{target.id}</code>\n"
        f"🔗 <b>اليوزر:</b> {username}\n"
        f"🏷️ <b>الرتبة:</b> {ROLE_AR.get(role, 'عضو')}\n"
        f"📅 <b>أول ظهور مسجل:</b> {first_seen}\n"
        "━━━━━━━━━━━━━━━",
        parse_mode="HTML",
    )


def reply_payload(message: Message) -> tuple[str, str] | None:
    if message.text:
        return "text", message.text
    if message.photo:
        return "photo", json.dumps({
            "file_id": message.photo[-1].file_id, "caption": message.caption or ""
        }, ensure_ascii=False)
    if message.video:
        return "video", json.dumps({
            "file_id": message.video.file_id, "caption": message.caption or ""
        }, ensure_ascii=False)
    if message.document:
        return "document", json.dumps({
            "file_id": message.document.file_id, "caption": message.caption or ""
        }, ensure_ascii=False)
    if message.animation:
        return "animation", json.dumps({
            "file_id": message.animation.file_id, "caption": message.caption or ""
        }, ensure_ascii=False)
    if message.sticker:
        return "sticker", json.dumps({"file_id": message.sticker.file_id}, ensure_ascii=False)
    if message.audio:
        return "audio", json.dumps({
            "file_id": message.audio.file_id, "caption": message.caption or ""
        }, ensure_ascii=False)
    if message.voice:
        return "voice", json.dumps({"file_id": message.voice.file_id}, ensure_ascii=False)
    return None


async def save_reply_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, editing: bool = False
) -> None:
    if not await require_level(update, context, "moderator"):
        return
    keyword = " ".join(context.args).strip()
    replied = update.effective_message.reply_to_message
    if not keyword or not replied:
        await update.effective_message.reply_text(
            "استخدم الأمر بالرد على رسالة: اضف رد كلمة"
        )
        return
    existing = db.auto_reply(update.effective_chat.id, keyword)
    if editing and not existing:
        await update.effective_message.reply_text("لا يوجد رد محفوظ بهذه الكلمة.")
        return
    if not editing and not existing and len(db.auto_replies(update.effective_chat.id)) >= 200:
        await update.effective_message.reply_text("❌ وصلت المجموعة إلى الحد الأقصى: 200 رد.")
        return
    payload = reply_payload(replied)
    if not payload:
        await update.effective_message.reply_text("نوع الرسالة غير مدعوم للحفظ.")
        return
    content_type, content = payload
    db.save_auto_reply(
        update.effective_chat.id, keyword, content_type, content,
        update.effective_user.id,
    )
    await update.effective_message.reply_text(
        f"✅ تم {'تعديل' if editing else 'حفظ'} الرد للكلمة: {keyword}"
    )


async def command_add_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_reply_command(update, context, editing=False)


async def command_edit_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_reply_command(update, context, editing=True)


async def command_delete_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "moderator"):
        return
    keyword = " ".join(context.args).strip()
    if not keyword:
        await update.effective_message.reply_text("استخدم: حذف رد كلمة")
        return
    db.remove_auto_reply(update.effective_chat.id, keyword)
    await update.effective_message.reply_text("✅ تم حذف الرد.")


async def command_list_replies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_group(update):
        await update.effective_message.reply_text("هذا الأمر يعمل داخل المجموعة.")
        return
    rows = db.auto_replies(update.effective_chat.id)
    if not rows:
        await update.effective_message.reply_text("📚 لا توجد ردود تلقائية.")
        return
    await update.effective_message.reply_text(
        "📚 <b>الردود التلقائية</b>\n" + "\n".join(
            f"• {row['keyword']}" for row in rows
        ), parse_mode="HTML",
    )


def render_reply(text: str, user: User, chat_title: str) -> str:
    now = datetime.now()
    values = {
        "/id": str(user.id),
        "/user": f"@{user.username}" if user.username else "لا يوجد",
        "/name": user.first_name or "",
        "/fullname": user.full_name or "",
        "/mention": mention_user(user),
        "/group": chat_title,
        "/date": now.strftime("%Y-%m-%d"),
        "/time": now.strftime("%H:%M"),
    }
    for key, value in values.items():
        text = text.replace(key, value)
    return text


async def send_auto_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE, row
) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not update.effective_message:
        return
    content = row["content"]
    if row["content_type"] == "text":
        await update.effective_message.reply_text(
            render_reply(content, user, chat.title or "المجموعة"), parse_mode="HTML"
        )
        return
    payload = json.loads(content)
    caption = render_reply(payload.get("caption", ""), user, chat.title or "المجموعة")
    sender = {
        "photo": context.bot.send_photo,
        "video": context.bot.send_video,
        "document": context.bot.send_document,
        "animation": context.bot.send_animation,
        "sticker": context.bot.send_sticker,
        "audio": context.bot.send_audio,
        "voice": context.bot.send_voice,
    }[row["content_type"]]
    kwargs = {"chat_id": chat.id, "caption": caption} if row["content_type"] not in {"sticker", "voice"} else {"chat_id": chat.id}
    if row["content_type"] == "sticker":
        kwargs["sticker"] = payload["file_id"]
    elif row["content_type"] == "voice":
        kwargs["voice"] = payload["file_id"]
    else:
        kwargs[row["content_type"]] = payload["file_id"]
    if caption and row["content_type"] in {"photo", "video", "document", "animation", "audio"}:
        kwargs["caption"] = caption
    await sender(**kwargs)


async def command_adminlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_group(update):
        await update.effective_message.reply_text("هذا الأمر يعمل داخل المجموعة فقط.")
        return
    rows = db.all_roles(update.effective_chat.id)
    lines = ["👮 <b>قائمة فريق الإدارة</b>", f"• <code>{ADMIN_ID}</code> — المالك"]
    for row in rows:
        if row["user_id"] == ADMIN_ID:
            continue
        lines.append(f"• <code>{row['user_id']}</code> — {ROLE_AR.get(row['role'], row['role'])}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def command_setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "senior_admin"):
        return
    text = " ".join(context.args).strip()
    db.update_settings(update.effective_chat.id, welcome=text)
    await update.effective_message.reply_text("✅ تم حفظ رسالة الترحيب." if text else "✅ تم تعطيل رسالة الترحيب.")


async def command_setrules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "senior_admin"):
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.effective_message.reply_text("استخدم: /setrules نص القوانين")
        return
    db.update_settings(update.effective_chat.id, rules=text)
    await update.effective_message.reply_text("✅ تم تحديث قوانين المجموعة.")


async def command_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_group(update):
        await update.effective_message.reply_text("هذا الأمر يعمل داخل المجموعة فقط.")
        return
    await update.effective_message.reply_text(f"📜 <b>قوانين المجموعة</b>\n\n{db.settings(update.effective_chat.id)['rules']}", parse_mode="HTML")


async def command_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "moderator"):
        return
    s = db.settings(update.effective_chat.id)
    text = (
        "⚙️ <b>إعدادات الحماية</b>\n"
        f"• التحذير التلقائي: {s['warn_limit']} ثم {s['warn_action']}\n"
        f"• مكافحة الفيضان: {s['flood_count']} رسالة / {s['flood_seconds']} ثوانٍ — {s['flood_action']}\n"
        f"• الكابتشا: {'مفعلة' if s['captcha_enabled'] else 'معطلة'} ({s['captcha_minutes']} دقائق)\n"
        f"• القفل الطارئ: {'مفعل' if s['lockdown'] else 'معطل'}\n"
        f"• الكلمات الممنوعة: {len(db.word_list(update.effective_chat.id))}\n"
        f"• رسالة الترحيب: {'مفعلة' if s['welcome'] else 'معطلة'}"
    )
    await update.effective_message.reply_text(text, parse_mode="HTML")


async def command_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "moderator"):
        return
    rows = db.actions(update.effective_chat.id, limit=15)
    if not rows:
        await update.effective_message.reply_text("السجل فارغ.")
        return
    lines = ["🧾 <b>آخر الإجراءات</b>"]
    for row in rows:
        when = row["created_at"][:16].replace("T", " ")
        lines.append(f"• {row['action']} — <code>{row['user_id'] or '—'}</code> — {when}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def command_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "moderator"):
        return
    target, _ = await target_from_message(update, context.args, context)
    if not target:
        await update.effective_message.reply_text("استخدم: /history @user أو بالرد.")
        return
    rows = db.actions(update.effective_chat.id, target.id, 20)
    if not rows:
        await update.effective_message.reply_text("لا يوجد سجل لهذا العضو.")
        return
    lines = [f"📚 <b>سجل {display_user(target)}</b>"]
    for row in rows:
        lines.append(f"• {row['action']} | {clean_reason(row['reason'])} | {row['created_at'][:16]}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def command_logchat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_level(update, context, "senior_admin"):
        return
    if not context.args or not context.args[0].lstrip("-").isdigit():
        await update.effective_message.reply_text("استخدم /setlogchat CHAT_ID داخل مجموعة السجل.")
        return
    db.update_settings(update.effective_chat.id, log_chat_id=int(context.args[0]))
    await update.effective_message.reply_text("✅ تم ربط قناة/مجموعة السجل.")


async def new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_group(update):
        return
    settings = db.settings(update.effective_chat.id)
    for user in update.effective_message.new_chat_members or []:
        if user.is_bot:
            continue
        if user.username:
            seen_users[(update.effective_chat.id, user.username.casefold())] = user
        db.remember_user(
            update.effective_chat.id, user.id, user.username,
            user.first_name, user.last_name,
        )
        welcome = settings["welcome"].replace("{name}", user.full_name).replace(
            "{group}", update.effective_chat.title or "المجموعة"
        )
        if settings["captcha_enabled"]:
            a, b = random.randint(1, 9), random.randint(1, 9)
            answer = str(a + b)
            expires = now_ts() + settings["captcha_minutes"] * 60
            try:
                await apply_mute(context, update.effective_chat.id, user.id)
                keyboard = InlineKeyboardMarkup(
                    [[InlineKeyboardButton(f"أنا لست بوت — {a} + {b} = ؟", callback_data=f"captcha:{user.id}:{answer}")]]
                )
                sent = await update.effective_message.reply_text(
                    f"👋 أهلًا {mention_user(user)}!\nأكمل التحقق خلال {settings['captcha_minutes']} دقائق.",
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
                db.save_challenge(update.effective_chat.id, user.id, sent.message_id, answer, expires)
                pending_captcha.add((update.effective_chat.id, user.id))
            except Exception:
                logger.warning("Captcha setup failed", exc_info=True)
        elif welcome:
            await update.effective_message.reply_text(welcome)


async def captcha_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not query.data:
        return
    _, raw_user_id, answer = query.data.split(":", 2)
    user_id = int(raw_user_id)
    if query.from_user.id != user_id:
        await query.answer("هذا التحقق ليس لك.", show_alert=True)
        return
    chat_id = query.message.chat_id
    challenge = db.challenge(chat_id, user_id)
    if not challenge or int(challenge["expires_ts"]) < now_ts():
        await query.edit_message_text("انتهت مهلة التحقق. اطلب من الأدمن إعادة دخولك.")
        return
    if answer != challenge["answer"]:
        await query.answer("إجابة غير صحيحة.", show_alert=True)
        return
    await apply_unmute(context, chat_id, user_id)
    db.remove_challenge(chat_id, user_id)
    pending_captcha.discard((chat_id, user_id))
    await query.edit_message_text(f"✅ تم التحقق من {mention_user(query.from_user)}.", parse_mode="HTML")


async def arabic_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Support the no-slash Arabic commands from the specification."""
    if not update.effective_message or not update.effective_message.text:
        return
    if await menu_text(update, context):
        return
    text = update.effective_message.text.strip()
    lowered = text.casefold()
    command = None
    args: list[str] = []
    aliases = [
        ("تعديل رد", command_edit_reply),
        ("حذف رد", command_delete_reply),
        ("اضف رد", command_add_reply),
        ("أضف رد", command_add_reply),
        ("مسح تحذير", command_unwarn),
        ("فك حظر", command_unban),
        ("فك كتم", command_unmute),
        ("تنزيل مشرف", command_demote),
        ("رفع مشرف", command_promote),
        ("قفل القروب", command_lock),
        ("فتح القروب", command_unlock),
        ("فك تثبيت", command_unpin),
    ]
    for prefix, handler in aliases:
        if lowered == prefix.casefold() or lowered.startswith(prefix.casefold() + " "):
            command = handler
            args = text[len(prefix):].strip().split()
            break
    if command is None:
        single_commands = {
            "تفعيل": command_activate,
            "القائمة": command_menu,
            "الاوامر": command_help,
            "الأوامر": command_help,
            "معلومات": command_info,
            "id": command_info,
            "الردود": command_list_replies,
            "قوانين": command_rules,
            "القوانين": command_rules,
            "إعدادات": command_settings,
            "الاعدادات": command_settings,
            "تثبيت": command_pin,
            "حذف": command_del,
            "طرد": command_kick,
            "حظر": command_ban,
            "كتم": command_mute,
            "تحذير": command_warn,
            "ردود": command_list_replies,
        }
        command = single_commands.get(lowered)
        if command:
            args = []
        else:
            # Commands with a single Arabic word plus arguments.
            for prefix, handler in [
                ("حظر", command_ban), ("طرد", command_kick), ("كتم", command_mute),
                ("تحذير", command_warn), ("تثبيت", command_pin),
            ]:
                if lowered.startswith(prefix.casefold() + " "):
                    command = handler
                    args = text[len(prefix):].strip().split()
                    break
    if command is None:
        await moderate_message(update, context)
        return
    context.args = args
    await command(update, context)


async def cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = now_ts()
    for row in db.due_restrictions(now):
        chat_id, user_id, kind = row["chat_id"], row["user_id"], row["kind"]
        try:
            if kind == "mute":
                await apply_unmute(context, chat_id, user_id)
            elif kind == "ban":
                await context.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
                db.remove_temp_restriction(chat_id, user_id, "ban")
            db.add_action(chat_id, user_id, f"انتهاء {kind}", None, "انتهاء المدة", None)
        except Exception:
            logger.info("Temporary action cleanup failed for %s/%s", chat_id, user_id, exc_info=True)
    # Captcha timeout: remove the user quietly from the group.
    for chat_id, user_id in list(pending_captcha):
        challenge = db.challenge(chat_id, user_id)
        if not challenge:
            pending_captcha.discard((chat_id, user_id))
            continue
        if int(challenge["expires_ts"]) <= now:
            try:
                await context.bot.ban_chat_member(chat_id, user_id)
                await context.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
            except Exception:
                pass
            db.remove_challenge(chat_id, user_id)
            pending_captcha.discard((chat_id, user_id))


async def moderate_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_group(update) or not update.effective_message or not update.effective_user:
        return
    message = update.effective_message
    user = update.effective_user
    chat_id = update.effective_chat.id
    if user.username:
        seen_users[(chat_id, user.username.casefold())] = user
    db.remember_user(chat_id, user.id, user.username, user.first_name, user.last_name)
    if user.is_bot or db.trusted(chat_id, user.id):
        return
    role = await role_for(chat_id, user, context)
    if ROLE_LEVEL.get(role, 0) >= PROTECTED_ROLE_LEVEL:
        return
    content = " ".join(
        x for x in [message.text or "", message.caption or "", message.entities and str(message.entities) or ""] if x
    ).casefold()
    words = db.word_list(chat_id)
    found_word = next((word for word in words if word.casefold() in content), None)
    suspicious_link = bool(re.search(r"(https?://|t\.me/|telegram\.me/|www\.)", content))
    settings = db.settings(chat_id)
    if found_word or suspicious_link:
        try:
            await message.delete()
        except Exception:
            pass
        reason = f"كلمة ممنوعة: {found_word}" if found_word else "رابط/إعلان"
        db.add_action(chat_id, user.id, "حذف تلقائي", None, reason, None)
        if settings["spam_action"] in {"warn", "mute"}:
            db.add_warning(chat_id, user.id, reason, 0)
        if settings["spam_action"] == "mute":
            try:
                await apply_mute(context, chat_id, user.id, 10 * 60)
            except Exception:
                pass
        return
    key = (chat_id, user.id)
    window = message_windows[key]
    current = time.monotonic()
    window.append(current)
    while window and current - window[0] > settings["flood_seconds"]:
        window.popleft()
    if len(window) > settings["flood_count"]:
        window.clear()
        try:
            await message.delete()
        except Exception:
            pass
        db.add_action(chat_id, user.id, "مكافحة فيضان", None, "رسائل متتالية", None)
        if settings["flood_action"] == "mute":
            try:
                await apply_mute(context, chat_id, user.id, settings["flood_mute_minutes"] * 60)
            except Exception:
                pass
        elif settings["flood_action"] == "warn":
            db.add_warning(chat_id, user.id, "فيضان رسائل", 0)
        return
    if db.is_activated(chat_id) and message.text:
        row = db.matching_auto_reply(chat_id, message.text)
        if row:
            try:
                await send_auto_reply(update, context, row)
            except Exception:
                logger.info("Auto reply failed", exc_info=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled update error: %s", context.error, exc_info=context.error)


def build_application() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", command_start))
    app.add_handler(CommandHandler("help", command_help))
    commands = {
        "kick": command_kick, "ban": command_ban, "unban": command_unban,
        "tban": command_tban, "mute": command_mute, "unmute": command_unmute,
        "warn": command_warn, "unwarn": command_unwarn, "warns": command_warns,
        "setwarnlimit": command_setwarnlimit, "setwarnaction": command_setwarnaction,
        "del": command_del, "purge": command_purge, "addword": command_addword,
        "delword": command_delword, "wordlist": command_wordlist, "setflood": command_setflood,
        "setspam": command_setspam, "setcaptcha": command_setcaptcha,
        "setcaptchatime": command_setcaptchatime,
        "lock": command_lock, "unlock": command_unlock, "trust": command_trust,
        "untrust": command_untrust, "promote": command_promote, "demote": command_demote,
        "adminlist": command_adminlist, "setwelcome": command_setwelcome,
        "setrules": command_setrules, "rules": command_rules, "settings": command_settings,
        "log": command_log, "history": command_history, "setlogchat": command_logchat,
        "info": command_info, "pin": command_pin, "unpin": command_unpin,
        "addreply": command_add_reply, "editreply": command_edit_reply,
        "delreply": command_delete_reply, "replies": command_list_replies,
    }
    for name, handler in commands.items():
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(captcha_callback, pattern=r"^captcha:"))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_members))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, arabic_command_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, moderate_message))
    app.add_error_handler(error_handler)
    if app.job_queue:
        app.job_queue.run_repeating(cleanup_job, interval=30, first=10)
    return app


def main() -> None:
    logger.info("Group Guard starting. Database: %s", DB_PATH)
    build_application().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()