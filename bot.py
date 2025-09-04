import os
import asyncio
import logging
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

import gspread
from dotenv import load_dotenv


load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_CREDS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "google-credentials.json")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "0"))  # -100xxxxxxxxxx
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
INVITE_TTL_DAYS = int(os.getenv("INVITE_TTL_DAYS", "7"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")


try:
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        creds = json.loads(creds_json)
        gc = gspread.service_account_from_dict(creds)
        sh = gc.open_by_key(SHEET_ID)
        ws = sh.sheet1
    else:
        gc = None
        ws = None

    sh = gc.open_by_key(SHEET_ID)
    ws = sh.sheet1
except Exception as e:
    logging.warning(f"Google Sheets init issue: {e}")
    ws = None


QUESTIONS = [
    {
        "text": "Знаете ли вы, какие меры поддержки существуют для студентов, которые хотят открыть бизнес?",
        "options": ["не знаю", "слышал, но не разбираюсь", "знаю несколько", "хорошо ориентируюсь"],
    },
    {
        "text": "Понимаете ли вы, что такое акселератор или бизнес-инкубатор?",
        "options": ["нет", "что-то слышал", "знаю в общих чертах", "могу объяснить другим"],
    },
    {
        "text": "Пользовались ли вы грантами, субсидиями или программами поддержки для стартапов?",
        "options": ["нет и не знаю как", "нет, но знаю где искать", "да, пробовал(а)", "да, успешно использовал(а)"],
    },
    {
        "text": "Насколько хорошо вы ориентируетесь в возможностях бесплатного нетворкинга?",
        "options": ["совсем не ориентируюсь", "знаю только по слухам", "знаю несколько площадок", "активно участвую"],
    },
    {
        "text": "Как вы оцениваете свой уровень знаний о юридических и организационных аспектах открытия бизнеса?",
        "options": ["ничего не знаю", "базовые знания", "средний уровень", "высокий уровень"],
    },
]

class TestStates(StatesGroup):
    waiting_contact = State()
    in_test = State()


def contact_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить контакт", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def options_kb(q_index: int) -> InlineKeyboardMarkup:
    buttons = []
    for i, opt in enumerate(QUESTIONS[q_index]["options"]):
        buttons.append([InlineKeyboardButton(text=opt, callback_data=f"ans:{q_index}:{i}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def ask_question(message: Message, state: FSMContext, q_index: int):
    await state.update_data(current_q=q_index)
    q = QUESTIONS[q_index]
    await message.answer(f"Вопрос {q_index+1}/{len(QUESTIONS)}\n\n{q['text']}", reply_markup=options_kb(q_index))

async def save_row_to_sheet(row: list):
    if ws is None:
        logging.error("Google Sheets not configured; skipping append_row")
        return
    try:
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        logging.exception(f"Failed to append row: {e}")

async def create_single_use_invite(bot: Bot, user_id: int) -> str:
    if TARGET_CHAT_ID == 0:
        return "(не настроен TARGET_CHAT_ID)"
    try:
        expire_date = int((datetime.now().astimezone(ZoneInfo("UTC")) + timedelta(days=INVITE_TTL_DAYS)).timestamp())
        link = await bot.create_chat_invite_link(
            chat_id=TARGET_CHAT_ID,
            name=f"bp-{user_id}-{datetime.now().strftime('%Y%m%d')}",
            expire_date=expire_date,
            member_limit=1
        )
        return link.invite_link
    except Exception as e:
        logging.exception(f"Invite link error: {e}")
        return ""


async def on_start(message: Message, state: FSMContext):
    await state.clear()
    text = (
        "Привет! Это бот проекта «Бизнес-Погружение».\n\n"
        "Чтобы присоединиться к закрытому комьюнити, отправьте свой контакт и ответьте на 5 коротких вопросов."
    )
    await message.answer(text, reply_markup=contact_kb())
    await state.set_state(TestStates.waiting_contact)

async def on_contact(message: Message, state: FSMContext):
    if not message.contact:
        await message.answer("Пожалуйста, используйте кнопку ниже, чтобы отправить контакт.", reply_markup=contact_kb())
        return

    await state.update_data(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        phone=message.contact.phone_number,
        answers=[None]*len(QUESTIONS)
    )
    await state.set_state(TestStates.in_test)
    await message.answer("Спасибо! Начинаем тест.", reply_markup=ReplyKeyboardRemove())
    await ask_question(message, state, 0)

async def on_answer(call: CallbackQuery, state: FSMContext, bot: Bot):
    if not await state.get_state() == TestStates.in_test:
        await call.answer()
        return

    try:
        _prefix, q_str, opt_str = call.data.split(":")
        q_index = int(q_str)
        opt_index = int(opt_str)
    except Exception:
        await call.answer()
        return

    data = await state.get_data()
    answers = data.get("answers", [None]*len(QUESTIONS))
    answers[q_index] = QUESTIONS[q_index]["options"][opt_index]
    await state.update_data(answers=answers)

    await call.answer("Ответ сохранён")

    next_q = q_index + 1
    if next_q < len(QUESTIONS):
        await call.message.answer("Ок!")
        await ask_question(call.message, state, next_q)
    else:
        tz = ZoneInfo(TIMEZONE)
        timestamp = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
        row = [
            timestamp,
            data.get("user_id"),
            data.get("username"),
            data.get("first_name"),
            data.get("last_name"),
            data.get("phone"),
        ] + answers

        await save_row_to_sheet(row)

        group_link = "https://t.me/business_immersion"
        await call.message.answer(
            "Готово! Вот ссылка для входа в закрытое комьюнити:\n" + group_link
        )
        await state.clear()


async def whoami(message: Message):
    await message.answer(f"Ваш user_id: {message.from_user.id}")

async def whereami(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    await message.answer(f"chat_id: {message.chat.id}")

async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.register(on_start, CommandStart())
    dp.message.register(whoami, Command("whoami"))
    dp.message.register(whereami, Command("whereami"))
    dp.message.register(on_contact, F.contact, TestStates.waiting_contact)

    dp.callback_query.register(on_answer, F.data.startswith("ans:"), TestStates.in_test)

    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
