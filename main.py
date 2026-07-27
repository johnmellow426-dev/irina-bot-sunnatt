import os
import json
import asyncio
import logging
import random
import io
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InputMediaPhoto, InlineKeyboardButton, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from PIL import Image, ImageDraw

BOT_TOKEN = os.getenv("BOT_TOKEN")
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --------------------------------------------------------------------------
# ПЕРСИСТЕНТНОЕ СОСТОЯНИЕ
# Раньше все эти словари жили только в оперативной памяти. Как только процесс
# перезапускался (деплой / краш / "сон" хостинга), они обнулялись — и бот
# терял ID уже отправленных карточек/стикеров, поэтому не мог их удалить.
# Новые сообщения копились поверх старых. Теперь состояние сохраняется в
# файл и подгружается при старте — старые ID не теряются.
# --------------------------------------------------------------------------
STATE_FILE = "state.json"

status_messages = {}   # {chat_id: message_id} — карточка со статусом
sticker_messages = {}  # {chat_id: [msg_id1, msg_id2, ...]} — стикеры
current_status = {}    # {chat_id: color}
boy_chat_id = None
girl_chat_id = None
chat_locks = {}         # asyncio.Lock per chat — не сериализуются, создаются заново


def save_state():
    data = {
        "boy_chat_id": boy_chat_id,
        "girl_chat_id": girl_chat_id,
        "status_messages": status_messages,
        "sticker_messages": sticker_messages,
        "current_status": current_status,
    }
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"❌ Не удалось сохранить состояние: {e}")


def load_state():
    global boy_chat_id, girl_chat_id, status_messages, sticker_messages, current_status
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        boy_chat_id = data.get("boy_chat_id")
        girl_chat_id = data.get("girl_chat_id")
        # JSON превращает int-ключи словарей в строки — конвертируем обратно
        status_messages = {int(k): v for k, v in data.get("status_messages", {}).items()}
        sticker_messages = {int(k): v for k, v in data.get("sticker_messages", {}).items()}
        current_status = {int(k): v for k, v in data.get("current_status", {}).items()}
        logger.info("✅ Состояние загружено из файла")
    except Exception as e:
        logger.error(f"❌ Не удалось загрузить состояние: {e}")


COLORS = {
    "green": (0, 200, 0),
    "yellow": (255, 220, 0),
    "orange": (255, 140, 0),
    "red": (220, 0, 0)
}

TEXTS = {
    "green": "🟢",
    "yellow": "🟡",
    "orange": "🟠",
    "red": "🔴"
}

STICKERS = {
    "green": "CAACAgIAAxkBAAERnNBqZyX-PHFYtWZjowm5KAwrarpL5gACHxEAAs63mEs-6jDBRqMUBD0E",
    "yellow": "CAACAgIAAxkBAAERnM5qZyXrwE01LQReexVHgAO_mTAoBwACnxIAAgKvmUsyx0PpsZAfRD0E",
    "orange": "CAACAgIAAxkBAAERnMdqZyXRYjAmr-vdRz5YzSlOEG_PvwACvwADY-ZrLhY3j8AZLr2OPQQ",
    "red": "CAACAgIAAxkBAAERnMVqZyWolmr7j5Y01teSGFx7knW-IQADFQACrAGQS5_oTC2UZF6iPQQ"
}

ASK_MESSAGES = [
    "🔔 *Зайка милашка!* Не могла бы выбрать статус.👇",
    "😏 *Твой хочет контакта!* Но пока просто выбери статус для разговора👇",
    "👑 *Ваша милейшество,* могли бы выбрать кнопочку, плиз?",
    "🚨 *Прилетел запрос на проверку обстановки!* Подай сигнал светофора 👇",
    "👀 *Парень дёргается и хочет внимания.* Обнови статус, не томи!"
]

COMMENTS = {
    "green": [
        "🟢 Ну наконец-то! Я уже заждалась...",
        "🟢 Ладно, я готова тебя выслушать",
        "🟢 У меня хорошее настроение, не испорти!",
    ],
    "yellow": [
        "🟡 Можно, но осторожно",
        "🟡 Будем или нет — покажет время",
        "🟡 Набери, поговорим",
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
        "🔴 Ты меня довёл. Поздравляю.",
        "🔴 Даже не звони мне сейчас!",
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


def get_boy_keyboard():
    return InlineKeyboardBuilder(
        [
            [InlineKeyboardButton(text="❓ Запросить статус", callback_data="ask_status")]
        ]
    ).as_markup()


async def get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in chat_locks:
        chat_locks[chat_id] = asyncio.Lock()
    return chat_locks[chat_id]


async def clear_all_stickers(chat_id: int):
    stickers_list = sticker_messages.get(chat_id, [])
    if not stickers_list:
        return

    for msg_id in list(stickers_list):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            logger.info(f"🗑 Удалён стикер {msg_id}")
        except Exception as e:
            # Сообщение могло быть уже удалено вручную — это не ошибка
            logger.info(f"ℹ️ Стикер {msg_id} уже недоступен для удаления: {e}")

    sticker_messages[chat_id] = []
    save_state()


async def send_status_sticker(chat_id: int, color: str):
    sticker_id = STICKERS.get(color)
    if not sticker_id:
        return

    await clear_all_stickers(chat_id)

    try:
        sent_sticker = await bot.send_sticker(chat_id=chat_id, sticker=sticker_id)
        sticker_messages.setdefault(chat_id, []).append(sent_sticker.message_id)
        save_state()
    except Exception as e:
        logger.error(f"❌ Ошибка отправки стикера: {e}")


async def update_girl_photo(chat_id: int, color: str) -> bool:
    lock = await get_lock(chat_id)

    async with lock:
        is_same_color = (current_status.get(chat_id) == color)

        await send_status_sticker(chat_id, color)

        if is_same_color:
            return True

        msg_id = status_messages.get(chat_id)
        caption = TEXTS[color]
        kb = get_keyboard()

        if msg_id:
            try:
                photo = make_circle(color)
                await bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=msg_id,
                    media=InputMediaPhoto(media=photo, caption=caption),
                    reply_markup=kb
                )
                current_status[chat_id] = color
                save_state()
                return True
            except Exception as e:
                logger.warning(f"⚠️ Редактирование не удалось ({e}). Пробую отправить заново.")

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
            save_state()
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка отправки карточки: {e}")
            return False


# --- ХЕНДЛЕРЫ ПАРНЯ ---

@dp.message(Command("start_me"))
async def start_me(message: types.Message):
    global boy_chat_id
    boy_chat_id = message.chat.id
    save_state()
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
    global girl_chat_id

    if not girl_chat_id:
        text = "❌ Девушка ещё не запустила бота (не нажала /start)!"
        if isinstance(event, types.CallbackQuery):
            await event.answer(text, show_alert=True)
        else:
            await event.answer(text)
        return

    lock = await get_lock(girl_chat_id)
    async with lock:
        try:
            curr_color = current_status.get(girl_chat_id, "green")
            photo = make_circle(curr_color)
            ask_caption = random.choice(ASK_MESSAGES)

            await clear_all_stickers(girl_chat_id)

            old_msg_id = status_messages.get(girl_chat_id)
            if old_msg_id:
                try:
                    await bot.delete_message(chat_id=girl_chat_id, message_id=old_msg_id)
                except Exception:
                    pass

            sent = await bot.send_photo(
                chat_id=girl_chat_id,
                photo=photo,
                caption=ask_caption,
                parse_mode="Markdown",
                reply_markup=get_keyboard()
            )
            status_messages[girl_chat_id] = sent.message_id
            save_state()

            text_ok = "📩 Запрос отправлен! Ждём ответа..."
            if isinstance(event, types.CallbackQuery):
                await event.answer("Запрос отправлен!")
                await event.message.answer(text_ok)
            else:
                await event.answer(text_ok)

        except Exception as e:
            logger.error(f"❌ Ошибка запроса статуса: {e}")
            err_msg = "❌ Ошибка при отправке запроса."
            if isinstance(event, types.CallbackQuery):
                await event.answer(err_msg, show_alert=True)
            else:
                await event.answer(err_msg)


# --- ХЕНДЛЕРЫ ДЕВУШКИ ---

@dp.message(Command("start"))
async def start_girl(message: types.Message):
    global girl_chat_id
    girl_chat_id = message.chat.id
    save_state()
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
        except Exception as e:
            logger.error(f"❌ Не удалось отправить сообщение парню: {e}")

    await callback.answer()


@dp.message(Command("ping"))
async def ping(message: types.Message):
    await message.answer("🏓 Понг! Бот работает.")


async def main():
    load_state()
    logger.info("🚀 Запуск бота 'Светофор'...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
