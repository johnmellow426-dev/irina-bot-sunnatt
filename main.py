import os
import asyncio
import logging
import random
import io
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InputMediaPhoto, InlineKeyboardButton, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from PIL import Image, ImageDraw
from aiohttp import web

BOT_TOKEN = os.getenv("BOT_TOKEN")
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Хранилище состояний
status_messages = {}  # {chat_id: message_id}
current_status = {}   # {chat_id: color}
boy_chat_id = None
girl_chat_id = None   # <-- НОВОЕ: запоминаем ID девушки
chat_locks = {}       # Блокировки от двойных нажатий

COLORS = {
    "green": (0, 200, 0),
    "yellow": (255, 220, 0),
    "orange": (255, 140, 0),
    "red": (220, 0, 0)
}

TEXTS = {
    "green": "💚 Готова общаться",
    "yellow": "🤔 Можно...",
    "orange": "😤 Раздражаюсь...",
    "red": "🔴 Обиделась!"
}

COMMENTS = {
    "green": [
        "🟢 Ну наконец-то! Я уже заждалась...",
        "🟢 Ладно, я готова тебя выслушать",
        "🟢 У меня хорошее настроение, не испорти!",
    ],
    "yellow": [
        "🟡 можно да, чуть выебу",
        "🟡 Будем или нет покажет время ",
        "🟡 набери закусимся, иномарка",
    ],
    "orange": [
        "🟠 Ты меня уже раздражаешь...",
        "🟠 Всё, я начинаю злиться",
        "🟠 Серьёзно? Опять это?",
        "🟠 Я уже на пределе",
    ],
    "red": [
        "😡 ВСЁ! Я ОБИДЕЛАСЬ!",
        "🔴 НЕ БЕСИ МЕНЯ!",
        "🔴 Всё, я устала. Отстань.",
        "🔴 Ты меня довел. Поздравляю.",
        "🔴 Даже не Звони мне сейчас!",
    ]
}

def make_circle(color_name: str) -> BufferedInputFile:
    size = 512
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([0, 0, size, size], fill=(*COLORS[color_name], 255))
    draw.ellipse([0, 0, size, size], outline=(255, 255, 255, 255), width=12)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return BufferedInputFile(buf.read(), filename=f"{color_name}_circle.png")

def get_keyboard():
    return InlineKeyboardBuilder(
        [
            [InlineKeyboardButton(text="🟢 Зелёная", callback_data="green"),
             InlineKeyboardButton(text="🟡 Жёлтая", callback_data="yellow")],
            [InlineKeyboardButton(text="🟠 Оранжевая", callback_data="orange"),
             InlineKeyboardButton(text="🔴 Красная", callback_data="red")]
        ]
    ).as_markup()

# Кнопка запроса статуса для парня
def get_boy_keyboard():
    return InlineKeyboardBuilder(
        [
            [InlineKeyboardButton(text="❓ Запросить статус", callback_data="ask_status")]
        ]
    ).as_markup()

async def get_lock(chat_id: int):
    if chat_id not in chat_locks:
        chat_locks[chat_id] = asyncio.Lock()
    return chat_locks[chat_id]

async def update_girl_photo(chat_id: int, color: str) -> bool:
    """
    Возвращает True, только если статус РЕАЛЬНО изменился.
    """
    lock = await get_lock(chat_id)
    
    async with lock:
        if current_status.get(chat_id) == color:
            logger.info(f"ℹ️ Статус для {chat_id} уже {color}, игнорируем повторное нажатие.")
            return False
            
        msg_id = status_messages.get(chat_id)
        photo = make_circle(color)
        caption = TEXTS[color]
        kb = get_keyboard()

        if msg_id:
            try:
                await bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=msg_id,
                    media=InputMediaPhoto(media=photo, caption=caption),
                    reply_markup=kb
                )
                current_status[chat_id] = color
                return True
            except Exception as e:
                err_str = str(e).lower()
                if "not modified" in err_str:
                    current_status[chat_id] = color
                    return True
                
                logger.warning(f"⚠️ Редактирование не удалось ({e}). Пробую удалить и отправить заново.")
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except Exception:
                    pass

        try:
            photo = make_circle(color)
            sent = await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=caption,
                reply_markup=kb
            )
            status_messages[chat_id] = sent.message_id
            current_status[chat_id] = color
            logger.info(f"📤 Отправлено новое фото (msg_id: {sent.message_id}) для {chat_id}")
            return True
        except Exception as e:
            logger.error(f"❌ Критическая ошибка отправки фото: {e}")
            return False

# --- ХЕНДЛЕРЫ ПАРНЯ ---

@dp.message(Command("start_me"))
async def start_me(message: types.Message):
    global boy_chat_id
    boy_chat_id = message.chat.id
    await message.answer(
        "✅ Твой ID сохранён! Теперь будешь получать уведомления о её настроении 😏\n\n"
        "Ты можешь запрашивать статус по кнопке ниже:",
        reply_markup=get_boy_keyboard()
    )
    logger.info(f"💾 Сохранён ID парня: {message.chat.id}")

@dp.callback_query(F.data == "ask_status")
async def on_ask_status_click(callback: types.CallbackQuery):
    await request_status_from_girl(callback)

@dp.message(Command("ask"))
async def ask_command(message: types.Message):
    await request_status_from_girl(message)

async def request_status_from_girl(event: types.Message | types.CallbackQuery):
    """Отправляет запрос девушке обновить статус"""
    global girl_chat_id
    
    if not girl_chat_id:
        text = "❌ Девушка ещё не запустила бота (не нажала /start)!"
        if isinstance(event, types.CallbackQuery):
            await event.answer(text, show_alert=True)
        else:
            await event.answer(text)
        return

    try:
        # Отправляем уведомление девушке
        await bot.send_message(
            chat_id=girl_chat_id, 
            text="🔔 **Он спрашивает, какое у тебя настроение!**\nВыбери текущий статус выше 👆"
        )
        
        # Подтверждение парню
        text_ok = "📩 Запрос отправлен! Ждём ответа..."
        if isinstance(event, types.CallbackQuery):
            await event.answer("Запрос отправлен!")
            await event.message.answer(text_ok)
        else:
            await event.answer(text_ok)
            
    except Exception as e:
        logger.error(f"❌ Не удалось отправить запрос девушке: {e}")
        err_msg = "❌ Ошибка при отправке запроса."
        if isinstance(event, types.CallbackQuery):
            await event.answer(err_msg, show_alert=True)
        else:
            await event.answer(err_msg)

# --- ХЕНДЛЕРЫ ДЕВУШКИ ---

@dp.message(Command("start"))
async def start_girl(message: types.Message):
    global girl_chat_id
    girl_chat_id = message.chat.id  # Сохраняем ID девушки
    await update_girl_photo(message.chat.id, "green")
    logger.info(f"🎬 Девушка запустила бота: {message.chat.id}")

@dp.callback_query(F.data.in_(["green", "yellow", "orange", "red"]))
async def on_color_change(callback: types.CallbackQuery):
    color = callback.data
    chat_id = callback.message.chat.id

    is_updated = await update_girl_photo(chat_id, color)

    if is_updated and boy_chat_id:
        try:
            comment = random.choice(COMMENTS[color])
            await bot.send_message(chat_id=boy_chat_id, text=comment)
            logger.info(f"📩 Отправлен комментарий парню: {comment}")
        except Exception as e:
            logger.error(f"❌ Не удалось отправить сообщение парню: {e}")

    await callback.answer()

@dp.message(Command("ping"))
async def ping(message: types.Message):
    await message.answer("🏓 Понг! Бот работает.")

# ==========================================
# ФИКС ДЛЯ RENDER: Фиктивный веб-сервер
# ==========================================
async def handle_health(request):
    return web.Response(text="Bot is running 🟢")

async def init_web_app():
    app = web.Application()
    app.router.add_get('/', handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"✅ Веб-сервер запущен на порту {port} (Render happy)")

async def main():
    logger.info("🚀 Запуск бота 'Светофор'...")
    await asyncio.gather(
        init_web_app(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())
