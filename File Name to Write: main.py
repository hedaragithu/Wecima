import os
import sqlite3
import asyncio
from difflib import get_close_matches
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler
)
from telegram.error import Forbidden

# متغيرات البيئة
TOKEN = os.environ.get("TOKEN")
GROUP_ID = int(os.environ.get("GROUP_ID"))      # ID المجموعة (مثال: -1001234567890)
CHANNEL_ID = os.environ.get("CHANNEL_ID")       # اسم القناة (مثال: @mychannel)
ADMIN_ID = int(os.environ.get("ADMIN_ID"))      # ID حساب الأدمن
DB_PATH = os.environ.get("DB_PATH", "movies.db")

# إعداد قاعدة البيانات
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()
cursor.executescript("""
CREATE TABLE IF NOT EXISTS movies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT UNIQUE,
    message_id INTEGER,
    requests INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS favorites (
    user_id INTEGER,
    movie_id INTEGER,
    PRIMARY KEY(user_id, movie_id)
);
CREATE TABLE IF NOT EXISTS missing (
    title TEXT UNIQUE,
    count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS requests_log (
    user_id INTEGER,
    movie_id INTEGER
);
""")
conn.commit()

# التحقق من اشتراك المستخدم في القناة
async def is_subscribed(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ["member", "creator", "administrator"]
    except Forbidden:
        return False
    except:
        return False

# ديكوريتور للتقييد على المشتركين فقط
def restrict(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not await is_subscribed(context.bot, user_id):
            await update.message.reply_text("🔒 يجب أن تكون مشتركًا في القناة لاستخدام البوت.")
            return
        return await func(update, context)
    return wrapper

# أوامر البوت
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 مرحبًا! اكتب اسم فيلم أو أرسل نصًا، وسأبحث لك عنه."
    )

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ID:
        return
    caption = update.message.caption
    if not caption:
        return
    title = caption.lower().strip()
    msg_id = update.message.message_id
    cursor.execute(
        "INSERT OR IGNORE INTO movies (title, message_id) VALUES (?,?)",
        (title, msg_id)
    )
    conn.commit()
    print(f"[DB] Added movie: {title}")

# البحث الذكي بالفيديو
@restrict
async def search_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower().strip()
    # اجلب العناوين
    cursor.execute("SELECT title FROM movies")
    titles = [row[0] for row in cursor.fetchall()]
    match = get_close_matches(text, titles, n=1, cutoff=0.6)
    if not match:
        # سجل البحث المفقود
        cursor.execute(
            "INSERT INTO missing (title, count) VALUES (?,1) ON CONFLICT(title) DO UPDATE SET count=count+1",
            (text,)
        )
        conn.commit()
        # تنبيه الأدمن عند 3 مرات
        cursor.execute("SELECT count FROM missing WHERE title=?", (text,))
        cnt = cursor.fetchone()[0]
        if cnt >= 3:
            await context.bot.send_message(ADMIN_ID, f"🔔 تم البحث المتكرر عن فيلم غير موجود: '{text}' (عدد: {cnt})")
        await update.message.reply_text("❌ لم أجد الفيلم. يمكنك اقتراحه باستخدام /اقترح <اسم الفيلم>")
        return
    title = match[0]
    # زيادة العداد
    cursor.execute("UPDATE movies SET requests=requests+1 WHERE title=?", (title,))
    conn.commit()
    # جلب الرسالة وإرسالها
    cursor.execute("SELECT message_id, id FROM movies WHERE title=?", (title,))
    msg_id, movie_id = cursor.fetchone()
    sent = await context.bot.copy_message(
        chat_id=update.effective_chat.id,
        from_chat_id=GROUP_ID,
        message_id=msg_id
    )
    # سجل الطلب
    cursor.execute(
        "INSERT INTO requests_log (user_id, movie_id) VALUES (?,?)",
        (update.effective_user.id, movie_id)
    )
    conn.commit()
    # زر حذف
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ حذف الآن", callback_data=f"delete:{sent.message_id}")]
    ])
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="✅ تم إرسال الفيلم. يمكنك حذفه الآن إذا أردت.",
        reply_markup=keyboard
    )

# التعامل مع الضغط على الأزرار
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("delete:"):
        msg_id = int(data.split(':')[1])
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=msg_id)
            await query.edit_message_text("🗑️ تم حذف الفيديو.")
        except:
            await query.edit_message_text("⚠️ عذرًا، لا يمكن حذف الفيديو.")

# اقتراح فيلم للأدمن
@restrict
async def suggest_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("استخدم: /اقترح <اسم الفيلم>")
        return
    suggestion = " ".join(context.args).strip()
    sender = update.effective_user.full_name
    await update.message.reply_text("✅ تم إرسال اقتراحك للأدمن.")
    await context.bot.send_message(ADMIN_ID, f"💡 اقتراح جديد من {sender}:\n{suggestion}")

# إدارة المفضلات
@restrict
async def add_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("استخدم: /مفضل <اسم الفيلم>")
        return
    title = " ".join(context.args).lower().strip()
    cursor.execute("SELECT id FROM movies WHERE title=?", (title,))
    res = cursor.fetchone()
    if not res:
        await update.message.reply_text("⚠️ الفيلم غير موجود.")
        return
    movie_id = res[0]
    cursor.execute(
        "INSERT OR IGNORE INTO favorites (user_id, movie_id) VALUES (?,?)",
        (update.effective_user.id, movie_id)
    )
    conn.commit()
    await update.message.reply_text(f"⭐ تم إضافة '{title}' إلى مفضلاتك.")

@restrict
async def show_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cursor.execute(
        "SELECT movies.title FROM movies JOIN favorites ON movies.id=favorites.movie_id WHERE favorites.user_id=?",
        (update.effective_user.id,)
    )
    titles = [row[0] for row in cursor.fetchall()]
    text = "مفضلاتك:\n" + ("\n".join(titles) if titles else "(فارغة بعد)")
    await update.message.reply_text(text)

# إحصائيات أكثر 10 أفلام طلبًا
@restrict
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cursor.execute("SELECT title, requests FROM movies ORDER BY requests DESC LIMIT 10")
    top = cursor.fetchall()
    lines = [f"{title} — {cnt} طلب" for title, cnt in top]
    text = "🎯 أكثر 10 أفلام طلبًا:\n" + ("\n".join(lines) if lines else "لا توجد بيانات بعد.")
    await update.message.reply_text(text)

# توصيات للمستخدم
@restrict
async def recommend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cursor.execute("SELECT movie_id FROM requests_log WHERE user_id=?", (uid,))
    seen = {r[0] for r in cursor.fetchall()}
    cursor.execute("SELECT id, title FROM movies ORDER BY requests DESC LIMIT 10")
    recs = [title for mid, title in cursor.fetchall() if mid not in seen]
    text = "توصيات لك:\n" + ("\n".join(recs[:3]) if recs else "لا توجد توصيات جديدة.")
    await update.message.reply_text(text)

# إنشاء التطبيق وضبط المعالجات
app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.VIDEO & filters.Chat(GROUP_ID), handle_video))
app.add_handler(MessageHandler(filters.TEXT & ~filters.Command, search_movie))
app.add_handler(CallbackQueryHandler(button_handler))
app.add_handler(CommandHandler("اقترح", suggest_movie))
app.add_handler(CommandHandler("مفضل", add_favorite))
app.add_handler(CommandHandler("مفضلاتي", show_favorites))
app.add_handler(CommandHandler("احصائيات", stats))
app.add_handler(CommandHandler("توصيات", recommend))

if __name__ == "__main__":
    app.run_polling()

