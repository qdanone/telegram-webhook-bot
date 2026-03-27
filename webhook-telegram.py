import os
import json
import pandas as pd
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.types import Update
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from aiohttp import web

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ================= НАСТРОЙКИ =================
TOKEN = os.getenv("BOT_TOKEN")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")

if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не задан")

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

boss_ids = [int(x) for x in os.getenv("BOSS_IDS", "").split(",") if x]
worker_ids = [int(x) for x in os.getenv("WORKER_IDS", "").split(",") if x]

users_info_raw = os.getenv("USERS_INFO", "")
users_info = {}
for x in users_info_raw.split(","):
    if ":" in x:
        k, v = x.split(":")
        users_info[int(k)] = v

# ================= GOOGLE SHEETS =================
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

creds_json = os.getenv("GOOGLE_CREDS_JSON")

if not creds_json:
    raise ValueError("❌ GOOGLE_CREDS_JSON пустой")

creds_dict = json.loads(creds_json)

creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)

data_sheet = client.open("bot_data").sheet1
log_sheet = client.open("bot_logs").sheet1

# ================= ЛОГИ =================
def log_action(user_id, action, query):
    user_name = users_info.get(user_id, "Неизвестный")
    log_sheet.append_row([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        user_name,
        action,
        query
    ])

# ================= ДАННЫЕ =================
def load_data():
    data = data_sheet.get_all_records()
    df = pd.DataFrame(data)

    if df.empty:
        df = pd.DataFrame(columns=["Индекс", "Буква", "Порядковый номер", "Препарат", "Количество"])

    df["Индекс"] = df["Индекс"].fillna("").astype(str).str.strip()
    df["Буква"] = df["Буква"].fillna("").astype(str).str.strip()
    df["Препарат"] = df["Препарат"].fillna("").astype(str).str.strip()

    if "Порядковый номер" in df.columns:
        df["Порядковый номер"] = pd.to_numeric(df["Порядковый номер"], errors="coerce").fillna(0).astype(int)

    return df

def save_data(df):
    data_sheet.clear()
    values = [df.columns.tolist()] + df.values.tolist()
    data_sheet.update(values)

# ================= СОСТОЯНИЯ =================
class AddDrug(StatesGroup):
    index = State()
    letter = State()
    name = State()

class SearchDrug(StatesGroup):
    name = State()

# ================= КЛАВИАТУРЫ =================
def boss_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Добавить")],
            [KeyboardButton(text="Номер банки")],
            [KeyboardButton(text="Логи сотрудников")]
        ],
        resize_keyboard=True
    )

def worker_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Номер банки")]
        ],
        resize_keyboard=True
    )

# ================= ХЕНДЛЕРЫ =================
@dp.message(Command("start"))
async def start(message: Message):
    user_id = message.from_user.id

    if user_id in boss_ids:
        await message.answer("Вы руководитель", reply_markup=boss_keyboard())
    elif user_id in worker_ids:
        await message.answer("Вы сотрудник", reply_markup=worker_keyboard())
    else:
        await message.answer("Нет доступа")

@dp.message(F.text == "Добавить")
async def add_start(message: Message, state: FSMContext):
    if message.from_user.id not in boss_ids:
        return
    await message.answer("Введите индекс:")
    await state.set_state(AddDrug.index)

@dp.message(AddDrug.index)
async def add_index(message: Message, state: FSMContext):
    await state.update_data(index=message.text.strip())
    await message.answer("Введите букву:")
    await state.set_state(AddDrug.letter)

@dp.message(AddDrug.letter)
async def add_letter(message: Message, state: FSMContext):
    await state.update_data(letter=message.text.strip())
    await message.answer("Введите название:")
    await state.set_state(AddDrug.name)

@dp.message(AddDrug.name)
async def add_name(message: Message, state: FSMContext):
    data = await state.get_data()
    df = load_data()

    index = data["index"]
    letter = data["letter"]
    name = message.text.strip()

    existing = df[
        (df["Индекс"] == index) &
        (df["Буква"] == letter) &
        (df["Препарат"].str.lower() == name.lower())
    ]

    next_number = 1 if existing.empty else existing["Порядковый номер"].max() + 1

    new_row = {
        "Индекс": index,
        "Буква": letter,
        "Порядковый номер": next_number,
        "Препарат": name,
        "Количество": 0
    }

    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
    save_data(df)

    log_action(message.from_user.id, "Добавить", name)

    await message.answer(f"Добавлено ✅\nНомер: {next_number}")
    await state.clear()

@dp.message(F.text == "Номер банки")
async def search_start(message: Message, state: FSMContext):
    await message.answer("Введите название:")
    await state.set_state(SearchDrug.name)

@dp.message(F.text == "Номер банки")
async def search_start(message: Message, state: FSMContext):
    if message.from_user.id not in boss_ids + worker_ids:
        return

    await message.answer("Укажите название:")
    await state.set_state(SearchDrug.name)

@dp.message(SearchDrug.name)
async def search_name(message: Message, state: FSMContext):
    df = load_data()
    query = message.text.strip().lower()

    result = df[df["Препарат"].str.lower().str.contains(query, na=False)]

    if result.empty:
        await message.answer("Не найдено")
        await state.clear()
        return

    response = "\n".join(
        f"{row['Индекс']} {row['Буква']} {row['Препарат']}"
        for _, row in result.iterrows()
    )

    await message.answer(response)
    log_action(message.from_user.id, "Поиск", query)
    await state.clear()
'''
@dp.message(F.text == "Логи сотрудников")
async def logs_start(message: Message, state: FSMContext):
    if message.from_user.id not in boss_ids:
        return

    await message.answer("Введите дату в формате ГГГГ-ММ-ДД:")
    await state.set_state(LogsDate.date)


@dp.message(LogsDate.date)
async def logs_date(message: Message, state: FSMContext):
    date_str = message.text.strip()

    logs = log_sheet.get_all_records()
    df = pd.DataFrame(logs)

    if df.empty:
        await message.answer("Логи пустые.")
        return

    logs_filtered = df[df["datetime"].str.startswith(date_str)]

    if logs_filtered.empty:
        await message.answer(f"Запросов за {date_str} нет.")
    else:
        lines = [
            f"{row['user_name']} — {row['action']} — {row['query']}"
            for _, row in logs_filtered.iterrows()
        ]

        await message.answer("\n".join(lines))

    await state.clear()
'''
# ================= WEBHOOK =================
async def handle(request):
    try:
        data = await request.json()
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
        return web.Response(text="OK")
    except Exception as e:
        import traceback
        print("❌ ERROR:", e)
        traceback.print_exc()
        return web.Response(status=500)

# ================= ЗАПУСК =================
async def on_startup(app):
    webhook_url = f"{RENDER_URL}/webhook"
    await bot.set_webhook(webhook_url)
    print(f"✅ Webhook установлен: {webhook_url}")

def main():
    if not RENDER_URL:
        raise ValueError("❌ RENDER_EXTERNAL_URL не задан")

    app = web.Application()
    app.router.add_post("/webhook", handle)

    app.on_startup.append(on_startup)

    port = int(os.environ.get("PORT", 10000))
    print("🚀 BOT STARTED")

    web.run_app(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()





































'''
import os
import json
import pandas as pd
from datetime import datetime
import asyncio

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, Update
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from flask import Flask, request

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ================= НАСТРОЙКИ =================
TOKEN = os.getenv("BOT_TOKEN")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")

if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не задан")

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

boss_ids = [int(x) for x in os.getenv("BOSS_IDS", "").split(",") if x]
worker_ids = [int(x) for x in os.getenv("WORKER_IDS", "").split(",") if x]

users_info_raw = os.getenv("USERS_INFO", "")
users_info = {}
for x in users_info_raw.split(","):
    if ":" in x:
        k, v = x.split(":")
        users_info[int(k)] = v

# ================= GOOGLE SHEETS =================
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

creds_json = os.getenv("GOOGLE_CREDS_JSON")

if not creds_json:
    raise ValueError("❌ GOOGLE_CREDS_JSON пустой")

creds_dict = json.loads(creds_json)

creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)

data_sheet = client.open("bot_data").sheet1
log_sheet = client.open("bot_logs").sheet1

# ================= ЛОГИ =================
def log_action(user_id, action, query):
    user_name = users_info.get(user_id, "Неизвестный")
    log_sheet.append_row([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        user_name,
        action,
        query
    ])

# ================= ДАННЫЕ =================
def load_data():
    data = data_sheet.get_all_records()
    df = pd.DataFrame(data)

    if df.empty:
        df = pd.DataFrame(columns=["Индекс", "Буква", "Порядковый номер", "Препарат", "Количество"])

    df["Индекс"] = df["Индекс"].fillna("").astype(str).str.strip()
    df["Буква"] = df["Буква"].fillna("").astype(str).str.strip()
    df["Препарат"] = df["Препарат"].fillna("").astype(str).str.strip()

    if "Порядковый номер" in df.columns:
        df["Порядковый номер"] = pd.to_numeric(df["Порядковый номер"], errors="coerce").fillna(0).astype(int)

    return df

def save_data(df):
    data_sheet.clear()
    values = [df.columns.tolist()] + df.values.tolist()
    data_sheet.update(values)

# ================= СОСТОЯНИЯ =================
class AddDrug(StatesGroup):
    index = State()
    letter = State()
    name = State()

class SearchDrug(StatesGroup):
    name = State()

class LogsDate(StatesGroup):
    date = State()

# ================= КЛАВИАТУРЫ =================
def boss_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Добавить")],
            [KeyboardButton(text="Номер банки")],
            [KeyboardButton(text="Логи сотрудников")]
        ],
        resize_keyboard=True
    )

def worker_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Номер банки")]
        ],
        resize_keyboard=True
    )

# ================= ХЕНДЛЕРЫ =================
@dp.message(Command("start"))
async def start(message: Message):
    user_id = message.from_user.id

    if user_id in boss_ids:
        await message.answer("Вы руководитель", reply_markup=boss_keyboard())
    elif user_id in worker_ids:
        await message.answer("Вы сотрудник", reply_markup=worker_keyboard())
    else:
        await message.answer("У вас нет доступа.")

@dp.message(F.text == "Добавить")
async def add_start(message: Message, state: FSMContext):
    if message.from_user.id not in boss_ids:
        return
    await message.answer("Введите индекс:")
    await state.set_state(AddDrug.index)

@dp.message(AddDrug.index)
async def add_index(message: Message, state: FSMContext):
    await state.update_data(index=message.text.strip())
    await message.answer("Введите букву:")
    await state.set_state(AddDrug.letter)

@dp.message(AddDrug.letter)
async def add_letter(message: Message, state: FSMContext):
    await state.update_data(letter=message.text.strip())
    await message.answer("Введите название препарата:")
    await state.set_state(AddDrug.name)

@dp.message(AddDrug.name)
async def add_name(message: Message, state: FSMContext):
    data = await state.get_data()
    df = load_data()

    index = data["index"]
    letter = data["letter"]
    name = message.text.strip()

    existing = df[
        (df["Индекс"] == index) &
        (df["Буква"] == letter) &
        (df["Препарат"].str.lower() == name.lower())
    ]

    next_number = 1 if existing.empty else existing["Порядковый номер"].max() + 1

    new_row = {
        "Индекс": index,
        "Буква": letter,
        "Порядковый номер": next_number,
        "Препарат": name,
        "Количество": 0
    }

    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
    save_data(df)

    log_action(message.from_user.id, "Добавить", f"{index} {letter} {name}")

    await message.answer(f"Добавлено ✅\nПорядковый номер: {next_number}")
    await state.clear()

@dp.message(F.text == "Номер банки")
async def search_start(message: Message, state: FSMContext):
    if message.from_user.id not in boss_ids + worker_ids:
        return

    await message.answer("Укажите название:")
    await state.set_state(SearchDrug.name)

@dp.message(SearchDrug.name)
async def search_name(message: Message, state: FSMContext):
    df = load_data()
    query = message.text.strip().lower()

    result = df[df["Препарат"].str.lower().str.contains(query, na=False)]

    if result.empty:
        await message.answer("Ничего не найдено.")
        await state.clear()
        return

    unique = result[["Индекс", "Буква", "Препарат"]].drop_duplicates()

    response = "\n".join(
        f"{row['Индекс']} {row['Буква']} {row['Препарат']}"
        for _, row in unique.iterrows()
    )

    await message.answer(response)
    log_action(message.from_user.id, "Номер банки", message.text.strip())
    await state.clear()

# ================= FLASK =================
app = Flask(__name__)

@app.route("/", methods=["GET"])
def index():
    return "Bot is running"

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json()

    telegram_update = Update(**update)

    asyncio.run(dp.process_update(telegram_update))

    return "OK"


import threading
import asyncio

def start_flask():
    port = int(os.environ.get("PORT", 10000))
    # Debug выключен, чтобы не конфликтовал с asyncio
    app.run(host="0.0.0.0", port=port, debug=False)

if __name__ == "__main__":
    if not RENDER_URL:
        raise ValueError("❌ RENDER_EXTERNAL_URL не задан")

    print("🚀 STARTING BOT...")

    # Устанавливаем webhook Telegram
    success = asyncio.run(bot.set_webhook(f"{RENDER_URL}/webhook"))
    if success:
        print(f"✅ Webhook установлен: {RENDER_URL}/webhook")
    else:
        print("❌ Ошибка при установке webhook")

    # Запускаем Flask в отдельном потоке, чтобы webhook работал параллельно
    flask_thread = threading.Thread(target=start_flask)
    flask_thread.start()

    print("🌐 Flask сервер запущен на порту", os.environ.get("PORT", 10000))

# ================= ЗАПУСК =================
if __name__ == "__main__":
    print("🚀 STARTING BOT...")

    if not RENDER_URL:
        raise ValueError("❌ RENDER_EXTERNAL_URL не задан")

    asyncio.run(bot.set_webhook(f"{RENDER_URL}/webhook"))

    port = int(os.environ.get("PORT", 10000))
    print(f"🌐 PORT: {port}")

    app.run(host="0.0.0.0", port=port)
'''
