import asyncio
import base64
import json
import logging
import os
from datetime import date, datetime
from calendar import monthrange

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ["GITHUB_REPO"]  # например: username/subscription-bot
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")

DATA_FILE = "data.json"

logging.basicConfig(level=logging.INFO)


# ============================================================
# СОСТОЯНИЕ ДОБАВЛЕНИЯ ПОДПИСКИ
# ============================================================

class AddSubscription(StatesGroup):
    name = State()
    amount = State()
    day = State()


# ============================================================
# GITHUB STORAGE
# ============================================================

def github_url():
    return (
        f"https://api.github.com/repos/"
        f"{GITHUB_REPO}/contents/{DATA_FILE}"
    )


async def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }


async def load_data():
    """
    Загружает data.json из GitHub.
    Если файла ещё нет — создаёт пустую структуру.
    """

    headers = await github_headers()

    async with aiohttp.ClientSession() as session:
        async with session.get(
            github_url(),
            headers=headers,
            params={"ref": GITHUB_BRANCH},
        ) as response:

            if response.status == 404:
                return {"subscriptions": []}

            if response.status != 200:
                text = await response.text()
                raise RuntimeError(
                    f"GitHub load error {response.status}: {text}"
                )

            result = await response.json()

            content = base64.b64decode(
                result["content"].replace("\n", "")
            ).decode("utf-8")

            return json.loads(content)


async def save_data(data):
    """
    Сохраняет data.json обратно в GitHub.
    """

    headers = await github_headers()

    content = json.dumps(
        data,
        ensure_ascii=False,
        indent=2,
    )

    encoded = base64.b64encode(
        content.encode("utf-8")
    ).decode("utf-8")

    async with aiohttp.ClientSession() as session:

        # Сначала узнаём SHA существующего файла.
        async with session.get(
            github_url(),
            headers=headers,
            params={"ref": GITHUB_BRANCH},
        ) as response:

            if response.status == 200:
                existing = await response.json()
                sha = existing["sha"]

            elif response.status == 404:
                sha = None

            else:
                text = await response.text()
                raise RuntimeError(
                    f"GitHub check error {response.status}: {text}"
                )

        payload = {
            "message": "Update subscriptions",
            "content": encoded,
            "branch": GITHUB_BRANCH,
        }

        if sha:
            payload["sha"] = sha

        async with session.put(
            github_url(),
            headers=headers,
            json=payload,
        ) as response:

            if response.status not in (200, 201):
                text = await response.text()
                raise RuntimeError(
                    f"GitHub save error {response.status}: {text}"
                )


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def today():
    return date.today()


def format_date(value):
    if not value:
        return "—"

    try:
        d = datetime.strptime(value, "%Y-%m-%d").date()
        return d.strftime("%d.%m.%Y")
    except Exception:
        return value


def month_name(month):
    names = [
        "",
        "январь",
        "февраль",
        "март",
        "апрель",
        "май",
        "июнь",
        "июль",
        "август",
        "сентябрь",
        "октябрь",
        "ноябрь",
        "декабрь",
    ]

    return names[month]


def previous_month_year(month, year):
    if month == 1:
        return 12, year - 1

    return month - 1, year


def scheduled_date(year, month, day):
    """
    Если человек указал 31 число, а в месяце только 30/28 дней,
    берём последний день месяца.
    """

    max_day = monthrange(year, month)[1]
    actual_day = min(day, max_day)

    return date(year, month, actual_day)


def money(amount):
    """
    Красиво показываем сумму.
    """

    if isinstance(amount, float) and amount.is_integer():
        return str(int(amount))

    return str(amount)


def get_main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 Мои подписки",
                    callback_data="list",
                )
            ],
            [
                InlineKeyboardButton(
                    text="➕ Добавить подписку",
                    callback_data="add",
                )
            ],
        ]
    )


# ============================================================
# ФОРМИРОВАНИЕ СПИСКА
# ============================================================

def subscription_text(sub):
    now = today()

    current_year = now.year
    current_month = now.month

    prev_month, prev_year = previous_month_year(
        current_month,
        current_year,
    )

    pay_date = scheduled_date(
        current_year,
        current_month,
        sub["day"],
    )

    previous_scheduled = scheduled_date(
        prev_year,
        prev_month,
        sub["day"],
    )

    last_paid = sub.get("last_paid")

    if last_paid:
        last_paid_text = format_date(last_paid)
    else:
        last_paid_text = "ещё не оплачено"

    # Считаем оплаченной текущую подписку,
    # если last_paid относится к текущему месяцу.
    paid_this_month = False

    if last_paid:
        try:
            paid_date = datetime.strptime(
                last_paid,
                "%Y-%m-%d",
            ).date()

            if (
                paid_date.year == current_year
                and paid_date.month == current_month
            ):
                paid_this_month = True

        except Exception:
            pass

    status = "🟢" if paid_this_month else "🔴"

    text = (
        f"{status} <b>{sub['name']}</b>\n"
        f"💰 {money(sub['amount'])} €\n"
        f"📅 Оплатить до: <b>{pay_date.strftime('%d.%m.%Y')}</b>\n"
        f"🕐 В прошлом месяце: {last_paid_text}\n"
    )

    if not last_paid:
        text += (
            f"ℹ️ Плановая дата прошлого месяца: "
            f"{previous_scheduled.strftime('%d.%m.%Y')}\n"
        )

    if paid_this_month:
        text += f"✅ Оплачено: {last_paid_text}\n"

    return text


def subscriptions_keyboard(subscriptions):
    buttons = []

    for sub in subscriptions:

        last_paid = sub.get("last_paid")
        paid_this_month = False

        if last_paid:
            try:
                d = datetime.strptime(
                    last_paid,
                    "%Y-%m-%d",
                ).date()

                now = today()

                paid_this_month = (
                    d.year == now.year
                    and d.month == now.month
                )

            except Exception:
                pass

        if paid_this_month:
            button_text = f"↩️ Отменить оплату: {sub['name']}"
            callback = f"unpay:{sub['id']}"
        else:
            button_text = f"☑️ Оплачено: {sub['name']}"
            callback = f"pay:{sub['id']}"

        buttons.append(
            [
                InlineKeyboardButton(
                    text=button_text,
                    callback_data=callback,
                )
            ]
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 Удалить: {sub['name']}",
                    callback_data=f"delete:{sub['id']}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="➕ Добавить подписку",
                callback_data="add",
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                text="🏠 Главное меню",
                callback_data="home",
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=buttons
    )


async def make_subscriptions_message():
    data = await load_data()
    subscriptions = data.get("subscriptions", [])

    now = today()

    header = (
        f"📋 <b>Мои подписки — "
        f"{month_name(now.month).capitalize()} {now.year}</b>\n\n"
    )

    if not subscriptions:
        return (
            header
            + "Пока нет ни одной подписки.\n\n"
            + "Нажми «➕ Добавить подписку»."
        ), subscriptions

    parts = [header]

    for sub in subscriptions:
        parts.append(subscription_text(sub))
        parts.append("\n")

    return "".join(parts), subscriptions


# ============================================================
# BOT
# ============================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ============================================================
# START
# ============================================================

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "👋 <b>Привет!</b>\n\n"
        "Я буду следить за твоими ежемесячными подписками.\n\n"
        "Добавь подписки один раз, а потом просто "
        "нажимай «Оплачено», когда заплатил.",
        reply_markup=get_main_keyboard(),
    )


# ============================================================
# HOME
# ============================================================

@dp.callback_query(F.data == "home")
async def home(callback: CallbackQuery):
    await callback.answer()

    await callback.message.edit_text(
        "🏠 <b>Главное меню</b>",
        reply_markup=get_main_keyboard(),
    )


# ============================================================
# LIST
# ============================================================

@dp.callback_query(F.data == "list")
async def list_subscriptions(callback: CallbackQuery):
    await callback.answer()

    try:
        text, subscriptions = await make_subscriptions_message()

        await callback.message.edit_text(
            text,
            reply_markup=subscriptions_keyboard(
                subscriptions
            ),
        )

    except Exception:
        logging.exception("Could not load subscriptions")

        await callback.message.answer(
            "❌ Не удалось загрузить подписки."
        )


# ============================================================
# ADD
# ============================================================

@dp.callback_query(F.data == "add")
async def add_start(
    callback: CallbackQuery,
    state: FSMContext,
):
    await callback.answer()

    await state.set_state(AddSubscription.name)

    await callback.message.answer(
        "➕ <b>Добавляем подписку</b>\n\n"
        "Напиши название.\n\n"
        "Например:\n"
        "<code>Netflix</code>"
    )


@dp.message(AddSubscription.name)
async def add_name(
    message: Message,
    state: FSMContext,
):
    name = message.text.strip()

    if not name:
        await message.answer(
            "Название не может быть пустым."
        )
        return

    await state.update_data(name=name)
    await state.set_state(AddSubscription.amount)

    await message.answer(
        "💰 Теперь напиши сумму.\n\n"
        "Например:\n"
        "<code>15</code>\n\n"
        "Можно написать и:\n"
        "<code>9.99</code>"
    )


@dp.message(AddSubscription.amount)
async def add_amount(
    message: Message,
    state: FSMContext,
):
    text = message.text.strip().replace(",", ".")

    try:
        amount = float(text)

        if amount <= 0:
            raise ValueError

    except ValueError:
        await message.answer(
            "❌ Не понял сумму.\n\n"
            "Напиши, например: <code>15</code> "
            "или <code>9.99</code>"
        )
        return

    await state.update_data(amount=amount)
    await state.set_state(AddSubscription.day)

    await message.answer(
        "📅 Теперь напиши число месяца, "
        "в которое обычно нужно платить.\n\n"
        "Например:\n"
        "<code>10</code>"
    )


@dp.message(AddSubscription.day)
async def add_day(
    message: Message,
    state: FSMContext,
):
    text = message.text.strip()

    try:
        day = int(text)

        if day < 1 or day > 31:
            raise ValueError

    except ValueError:
        await message.answer(
            "❌ Нужно число от 1 до 31."
        )
        return

    user_data = await state.get_data()

    data = await load_data()

    subscriptions = data.setdefault(
        "subscriptions",
        [],
    )

    next_id = 1

    if subscriptions:
        next_id = max(
            int(s["id"])
            for s in subscriptions
        ) + 1

    subscription = {
        "id": str(next_id),
        "name": user_data["name"],
        "amount": user_data["amount"],
        "day": day,
        "last_paid": None,
    }

    subscriptions.append(subscription)

    await save_data(data)

    await state.clear()

    await message.answer(
        "✅ <b>Подписка добавлена!</b>\n\n"
        f"Название: <b>{subscription['name']}</b>\n"
        f"Сумма: <b>{money(subscription['amount'])} €</b>\n"
        f"День оплаты: <b>{subscription['day']}</b>\n\n"
        "Теперь она будет отображаться "
        "в списке каждый месяц.",
        reply_markup=get_main_keyboard(),
    )


# ============================================================
# PAY
# ============================================================

@dp.callback_query(F.data.startswith("pay:"))
async def pay_subscription(callback: CallbackQuery):
    await callback.answer()

    subscription_id = callback.data.split(":", 1)[1]

    data = await load_data()

    subscriptions = data.get(
        "subscriptions",
        [],
    )

    found = None

    for sub in subscriptions:
        if sub["id"] == subscription_id:
            found = sub
            break

    if not found:
        await callback.message.answer(
            "❌ Подписка не найдена."
        )
        return

    found["last_paid"] = today().isoformat()

    await save_data(data)

    text, subscriptions = await make_subscriptions_message()

    await callback.message.edit_text(
        text,
        reply_markup=subscriptions_keyboard(
            subscriptions
        ),
    )


# ============================================================
# UNPAY
# ============================================================

@dp.callback_query(F.data.startswith("unpay:"))
async def unpay_subscription(callback: CallbackQuery):
    await callback.answer()

    subscription_id = callback.data.split(":", 1)[1]

    data = await load_data()

    subscriptions = data.get(
        "subscriptions",
        [],
    )

    for sub in subscriptions:
        if sub["id"] == subscription_id:
            sub["last_paid"] = None
            break

    await save_data(data)

    text, subscriptions = await make_subscriptions_message()

    await callback.message.edit_text(
        text,
        reply_markup=subscriptions_keyboard(
            subscriptions
        ),
    )


# ============================================================
# DELETE
# ============================================================

@dp.callback_query(F.data.startswith("delete:"))
async def delete_subscription(callback: CallbackQuery):
    await callback.answer()

    subscription_id = callback.data.split(":", 1)[1]

    data = await load_data()

    subscriptions = data.get(
        "subscriptions",
        [],
    )

    found = None

    for sub in subscriptions:
        if sub["id"] == subscription_id:
            found = sub
            break

    if not found:
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Да, удалить",
                    callback_data=f"confirm_delete:{subscription_id}",
                ),
                InlineKeyboardButton(
                    text="↩️ Отмена",
                    callback_data="list",
                ),
            ]
        ]
    )

    await callback.message.edit_text(
        f"⚠️ Удалить подписку "
        f"<b>{found['name']}</b>?",
        reply_markup=keyboard,
    )


@dp.callback_query(
    F.data.startswith("confirm_delete:")
)
async def confirm_delete(
    callback: CallbackQuery,
):
    await callback.answer()

    subscription_id = callback.data.split(
        ":",
        1,
    )[1]

    data = await load_data()

    subscriptions = data.get(
        "subscriptions",
        [],
    )

    data["subscriptions"] = [
        sub
        for sub in subscriptions
        if sub["id"] != subscription_id
    ]

    await save_data(data)

    text, subscriptions = await make_subscriptions_message()

    await callback.message.edit_text(
        text,
        reply_markup=subscriptions_keyboard(
            subscriptions
        ),
    )


# ============================================================
# ERROR HANDLER
# ============================================================

@dp.errors()
async def errors_handler(event):
    logging.exception(
        "Unhandled bot error",
        exc_info=event.exception,
    )


# ============================================================
# RUN
# ============================================================

async def main():
    logging.info("Starting subscription bot...")

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
