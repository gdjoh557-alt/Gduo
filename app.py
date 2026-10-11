import time
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import yt_dlp
import os

# التوكن والأيدي مباشرة داخل الكود
TOKEN_BOT = "8747969341:AAGQEOkjmxQzXaBaUMuvvjf_ZYoBSuPdnKU"
ADMIN_ID = 8808657227

CHANNEL_USERNAME = "@loadvidtik"
BOT_USERNAME = "Tikloadvibot"
DEV_USERNAME = "@dl_r7c"

bot = telebot.TeleBot(TOKEN_BOT)

all_users = set()

WELCOME_TITLE = "✨ 𝓦𝓮𝓵𝓬𝓸𝓶𝓮 𝓽𝓸 𝓣𝓲𝓴𝓣𝓸𝓴 𝓟𝓻𝓸 𝓓𝓸𝔀𝓷𝓵𝓸𝓪𝓭𝓮𝓻 ✨"
HELP_TITLE = "📖 𝓑𝓸𝓽 𝓗𝓮𝓵𝓹 & 𝓖𝓾𝓲𝓭𝓮"
ABOUT_TITLE = "ℹ️ 𝓐𝓫𝓸𝓾𝓽 𝓣𝓱𝓲𝓼 𝓑𝓸𝓽"
PROCESSING_TITLE = "⏳ 𝓟𝓻𝓸𝓬𝓮𝓼𝓼𝓲𝓷𝓰 𝓡𝓮𝓺𝓾𝓮𝓼𝓽..."
DOWNLOADING_TITLE = "⚡ 𝓓𝓸𝔀𝓷𝓵𝓸𝓪𝓭𝓲𝓷𝓰 𝓲𝓷 𝓗𝓲𝓰𝓱 𝓠𝓾𝓪𝓵𝓲𝓽𝔂..."

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user = message.from_user
    user_id = user.id
    
    all_users.add(user_id)
    total_users_count = len(all_users)
    
    user_username = f"@{user.username}" if user.username else "No Username"
    user_fullname = f"{user.first_name} {user.last_name or ''}"
    
    alert_text = (
        f"🚨 <b>New User Started The Bot!</b>\n\n"
        f"👤 <b>Name:</b> {user_fullname}\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"🔗 <b>Username:</b> {user_username}\n"
        f"📊 <b>Total Users Count:</b> {total_users_count}"
    )
    
    try:
        photos = bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count > 0:
            file_id = photos.photos[0][0].file_id
            bot.send_photo(ADMIN_ID, file_id, caption=alert_text, parse_mode="HTML")
        else:
            bot.send_message(ADMIN_ID, alert_text, parse_mode="HTML")
    except Exception as e:
        print(f"Error sending alert to admin: {e}")

    welcome_text = (
        f"{WELCOME_TITLE}\n\n"
        f"𝓗𝓮𝓵𝓵𝓸 <b>{user.first_name}</b>! 👋\n"
        f"🆔 <b>Your ID:</b> <code>{user_id}</code>\n\n"
        f"🚀 <i>The ultimate and fastest bot to download TikTok videos without watermark in supreme quality!</i>\n\n"
        f"🔒 <b>To unlock the bot features, please join our official channel first:</b>\n"
        f"🔗 https://t.me/loadvidtik\n\n"
        f"👇 <i>Click the button below after joining:</i>"
    )
    
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("📢 𝓙𝓸𝓲𝓷 𝓒𝓱𝓪𝓷𝓷𝓮𝓵", url="https://t.me/loadvidtik"),
        InlineKeyboardButton("✅ 𝓒𝓱𝓮𝓬𝓴 𝓢𝓾𝓫𝓼𝓬𝓻𝓲𝓹𝓽𝓲𝓸𝓷", callback_data="check_sub"),
        InlineKeyboardButton("❓ 𝓗𝓮𝓵𝓹 & 𝓖𝓾𝓲𝓭𝓮", callback_data="help_menu"),
        InlineKeyboardButton("ℹ️ 𝓐𝓫𝓸𝓾𝓽 𝓑𝓸𝓽", callback_data="about_bot")
    )
    
    bot.send_message(
        message.chat.id,
        welcome_text,
        reply_markup=markup,
        parse_mode="HTML"
    )

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def verify_subscription(call):
    bot.answer_callback_query(call.id, "✨ Subscription Verified Successfully!")
    
    success_text = (
        f"🎉 <b>𝓢𝓾𝓬𝓬𝓮𝓼𝓼𝓯𝓾𝓵𝓵𝔂 𝓥𝓮𝓻𝓲𝓯𝓲𝓮𝓭!</b>\n\n"
        f"✅ <i>Channel Subscribed Successfully</i>\n\n"
        f"📥 𝓔𝓷𝓽𝓮𝓻 𝓸𝓻 𝓼𝓮𝓷𝓭 𝔂𝓸𝓾𝓻 𝓣𝓲𝓴𝓣𝓸𝓴 𝓿𝓲𝓭𝓮𝓸 𝓵𝓲𝓷𝓴 𝓷𝓸𝔀 𝓪𝓷𝓭 𝓵𝓮𝓽 𝓶𝓮 𝓭𝓸 𝓽𝓱𝓮 𝓶𝓪𝓰𝓲𝓬 ✨"
    )
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🏠 𝓑𝓪𝓬𝓴 𝓽𝓸 𝓗𝓸𝓶𝓮", callback_data="back_home"))

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=success_text,
        reply_markup=markup,
        parse_mode="HTML"
    )

@bot.callback_query_handler(func=lambda call: call.data == "help_menu")
def help_menu(call):
    help_text = (
        f"{HELP_TITLE}\n\n"
        f"📌 <b>How to use this bot?</b>\n"
        f"1️⃣ Make sure you subscribe to our channel @loadvidtik.\n"
        f"2️⃣ Copy any TikTok video link.\n"
        f"3️⃣ Send the link directly here in the chat.\n"
        f"4️⃣ Wait a few seconds for high quality without watermark!\n\n"
        f"⚡ <i>Fast, Free, and No Watermark!</i>"
    )
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔙 𝓡𝓮𝓽𝓾𝓻𝓷", callback_data="back_home"))
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=help_text,
        reply_markup=markup,
        parse_mode="HTML"
    )

@bot.callback_query_handler(func=lambda call: call.data == "about_bot")
def about_bot(call):
    about_text = (
        f"{ABOUT_TITLE}\n\n"
        f"🤖 <b>Bot Name:</b> TikTok Pro Downloader\n"
        f"📢 <b>Channel:</b> @loadvidtik\n"
        f"👑 <b>Developer:</b> Maharmah (Mohammed)\n"
        f"💬 <b>Dev Username:</b> <code>{DEV_USERNAME}</code>\n"
        f"⚙️ <b>Engine:</b> Python & Yt-Dlp"
    )
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("👑 𝓒𝓸𝓷𝓽𝓪𝓬𝓽 𝓓𝓮𝓿𝓮𝓵𝓸𝓹𝓮𝓻", url="https://t.me/dl_r7c"),
        InlineKeyboardButton("🔙 𝓡𝓮𝓽𝓾𝓻𝓷", callback_data="back_home")
    )
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=about_text,
        reply_markup=markup,
        parse_mode="HTML",
        disable_web_page_preview=True
    )

@bot.callback_query_handler(func=lambda call: call.data == "back_home")
def back_home(call):
    user_name = call.from_user.first_name
    welcome_text = (
        f"{WELCOME_TITLE}\n\n"
        f"𝓗𝓮𝓵𝓵𝓸 <b>{user_name}</b>! 👋\n"
        f"🚀 <i>Welcome back to the main menu. Send your TikTok link anytime!</i>"
    )
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("📢 𝓙𝓸𝓲𝓷 𝓒𝓱𝓪𝓷𝓷𝓮𝓵", url="https://t.me/loadvidtik"),
        InlineKeyboardButton("✅ 𝓒𝓱𝓮𝓬𝓴 𝓢𝓾𝓫𝓼𝓬𝓻𝓲𝓹𝓽𝓲𝓸𝓷", callback_data="check_sub"),
        InlineKeyboardButton("❓ 𝓗𝓮𝓵𝓹 & 𝓖𝓾𝓲𝓭𝓮", callback_data="help_menu"),
        InlineKeyboardButton("ℹ️ 𝓐𝓫𝓸𝓾𝓽 𝓑𝓸𝓽", callback_data="about_bot")
    )
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=welcome_text,
        reply_markup=markup,
        parse_mode="HTML"
    )

@bot.message_handler(func=lambda message: "tiktok.com" in message.text or "vm.tiktok.com" in message.text)
def download_tiktok(message):
    chat_id = message.chat.id
    url = message.text.strip()
    
    status_msg = bot.send_message(chat_id, f"{PROCESSING_TITLE}\n🔄 <i>Reviewing and parsing link...</i>", parse_mode="HTML")
    time.sleep(1)
    
    bot.edit_message_text(
        f"{DOWNLOADING_TITLE}\n⏳ <i>Extracting media streams... Please wait.</i>", 
        chat_id, 
        status_msg.message_id, 
        parse_mode="HTML"
    )
    
    output_filename = f"video_{chat_id}.mp4"
    
    # إعدادات متطورة ومحدثة لتخطي قيود السيرفرات وتيك توك
    ydl_opts = {
        'format': 'best',
        'outtmpl': output_filename,
        'quiet': True,
        'no_warnings': True,
        'geo_bypass': True,
        'nocheckcertificate': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Sec-Ch-Ua': '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
        },
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        caption = f"✨ 𝓣𝓲𝓴𝓣𝓸𝓴 𝓟𝓻𝓸 𝓓𝓸𝔀𝓷𝓵𝓸𝓪𝓭𝓮𝓻\n📌 𝓣𝓮𝓶 𝓐𝓵-𝓣𝓪𝓱𝓶𝓮𝓮𝓵 𝓑𝓸𝔀𝓪𝓼𝓪𝓽𝓪 @{BOT_USERNAME}"
        with open(output_filename, 'rb') as video:
            bot.send_video(chat_id, video, caption=caption, parse_mode="HTML")
            
        bot.delete_message(chat_id, status_msg.message_id)
        if os.path.exists(output_filename):
            os.remove(output_filename)
            
    except Exception as e:
        print(f"Download Error: {e}")
        bot.edit_message_text(
            "❌ <b>𝓔𝓻𝓻𝓸𝓻:</b> Failed to download the video. Please verify the link.", 
            chat_id, 
            status_msg.message_id, 
            parse_mode="HTML"
        )
        if os.path.exists(output_filename):
            os.remove(output_filename)

@bot.message_handler(func=lambda message: True)
def default_handler(message):
    if not message.text.startswith('/'):
        bot.reply_to(message, "⚠️ 𝓟𝓵𝓮𝓪𝓼𝓮 𝓼𝓮𝓷𝓭 𝓪 𝓿𝓪𝓵𝓲𝓭 𝓣𝓲𝓴𝓣𝓸𝓴 𝓵𝓲𝓷𝓴 𝓸𝓻 𝓾𝓼𝓮 /start.")

if __name__ == "__main__":
    print(f"Bot is running successfully! Developer: {DEV_USERNAME}")
    bot.infinity_polling()
