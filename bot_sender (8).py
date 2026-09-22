r"""
TESTPOISHOP — самостоятельный Telegram-бот-приёмщик заказов Poizon/Dewu.

Установка:
    python -m venv .venv
    source .venv/bin/activate        # Windows: .venv\Scripts\activate
    pip install python-telegram-bot>=21.0

Запуск:
    export TELEGRAM_BOT_TOKEN="8915135810:AAFj8B27EzAR6afGJTCUmpt-UJnziU9IrS0"
    python TESTPOISHOP.py

Необязательные переменные окружения:
    PAYMENT_CONTACT="@sysnor"
    CNY_RUB_RATE="13"
    DATABASE_PATH="poishop.sqlite3"
    ADMIN_IDS="1701942831,7693264087"

Данные заказов хранятся в SQLite-файле. Токен бота в код не записывается.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telegram import BotCommand, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("testpoishop")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PAYMENT_CONTACT = os.getenv("PAYMENT_CONTACT", "менеджеру по оплате")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "poishop.sqlite3"))

try:
    CNY_RUB_RATE = float(os.getenv("CNY_RUB_RATE", "13"))
except ValueError as error:
    raise RuntimeError("CNY_RUB_RATE должен быть положительным числом") from error

if CNY_RUB_RATE <= 0:
    raise RuntimeError("CNY_RUB_RATE должен быть положительным числом")

raw_admin_ids = os.getenv("ADMIN_IDS", "1701942831,7693264087")
ADMIN_IDS = {item.strip() for item in raw_admin_ids.split(",") if item.strip()}

SERVICE_FEE_RUB = 29
MARKUP_PERCENT = 5
MAX_USER_ORDERS = 20
MAX_ADMIN_ORDERS = 20

STATUS_LABELS = {
    "awaiting_payment": "💳 Ожидает оплаты",
    "payment_review": "🔎 Проверяем оплату",
    "payment_confirmed": "✅ Оплата подтверждена",
    "ordered": "🛍️ Вещь заказана",
    "in_transit": "🚚 В пути",
    "delivered": "🎉 Доставлено",
    "cancelled": "⛔ Отменён",
}

PAYMENT_LABELS = {
    "awaiting": "💳 Не оплачено",
    "under_review": "🔎 Проверяем",
    "confirmed": "✅ Подтверждена",
    "rejected": "⚠️ Не найдена",
}

STATUS_BY_BUTTON = {
    "🛍️ Заказано": "ordered",
    "🚚 В пути": "in_transit",
    "🎉 Доставлено": "delivered",
    "⛔ Отменить заказ": "cancelled",
}

# Состояния оформления и админских действий хранятся в памяти.
# Сами заказы и статусы хранятся в SQLite и не пропадают после перезапуска.
sessions: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_database() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id TEXT NOT NULL,
                username TEXT,
                customer_name TEXT,
                product_url TEXT NOT NULL,
                size TEXT NOT NULL,
                color TEXT NOT NULL,
                product_price_cny INTEGER NOT NULL DEFAULT 0,
                exchange_rate_cny_rub REAL NOT NULL DEFAULT 12.5,
                product_price_rub INTEGER NOT NULL,
                service_fee_rub INTEGER NOT NULL DEFAULT 29,
                markup_percent INTEGER NOT NULL DEFAULT 5,
                markup_rub INTEGER NOT NULL,
                total_rub INTEGER NOT NULL,
                current_location TEXT,
                status TEXT NOT NULL DEFAULT 'awaiting_payment',
                payment_status TEXT NOT NULL DEFAULT 'awaiting',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.commit()


def row_to_order(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    order = dict(row)
    # Совместимость со старыми строками, созданными до добавления цены в юанях.
    if not order["product_price_cny"]:
        order["product_price_cny"] = round(order["product_price_rub"] / CNY_RUB_RATE)
    return order


def create_order(
    *,
    telegram_user_id: str,
    username: str | None,
    customer_name: str | None,
    product_url: str,
    size: str,
    color: str,
    product_price_cny: float,
) -> dict[str, Any]:
    price_cny = round(product_price_cny)
    price_rub = round(price_cny * CNY_RUB_RATE)
    markup_rub = round(price_rub * MARKUP_PERCENT / 100)
    now = utc_now()

    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO orders (
                telegram_user_id, username, customer_name, product_url,
                size, color, product_price_cny, exchange_rate_cny_rub,
                product_price_rub, service_fee_rub, markup_percent, markup_rub,
                total_rub, current_location, status, payment_status,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL,
                    'awaiting_payment', 'awaiting', ?, ?)
            """,
            (
                telegram_user_id,
                username,
                customer_name,
                product_url,
                size,
                color,
                price_cny,
                CNY_RUB_RATE,
                price_rub,
                SERVICE_FEE_RUB,
                MARKUP_PERCENT,
                markup_rub,
                price_rub + markup_rub + SERVICE_FEE_RUB,
                now,
                now,
            ),
        )
        order_id = cursor.lastrowid
        connection.commit()

    order = get_order(int(order_id))
    if order is None:
        raise RuntimeError("Заказ не удалось прочитать после создания")
    return order


def get_order(order_id: int) -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM orders WHERE id = ? LIMIT 1", (order_id,)
        ).fetchone()
    return row_to_order(row)


def list_orders(
    *,
    telegram_user_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    conditions: list[str] = []
    values: list[Any] = []

    if telegram_user_id is not None:
        conditions.append("telegram_user_id = ?")
        values.append(telegram_user_id)
    if status and status != "all":
        conditions.append("status = ?")
        values.append(status)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM orders
            {where}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (*values, limit),
        ).fetchall()
    return [row_to_order(row) for row in rows if row_to_order(row) is not None]


def update_order_status(order_id: int, status: str) -> dict[str, Any] | None:
    if status not in STATUS_LABELS:
        return None
    with get_connection() as connection:
        connection.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
            (status, utc_now(), order_id),
        )
        connection.commit()
    return get_order(order_id)


def update_payment_status(
    order_id: int, payment_status: str
) -> dict[str, Any] | None:
    if payment_status not in PAYMENT_LABELS:
        return None

    next_status = {
        "confirmed": "payment_confirmed",
        "under_review": "payment_review",
    }.get(payment_status)

    with get_connection() as connection:
        if next_status:
            connection.execute(
                """
                UPDATE orders
                SET payment_status = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (payment_status, next_status, utc_now(), order_id),
            )
        else:
            connection.execute(
                """
                UPDATE orders
                SET payment_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (payment_status, utc_now(), order_id),
            )
        connection.commit()
    return get_order(order_id)


def update_order_location(
    order_id: int, location: str
) -> dict[str, Any] | None:
    location = location.strip()
    if not location:
        return None
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE orders
            SET current_location = ?, updated_at = ?
            WHERE id = ?
            """,
            (location, utc_now(), order_id),
        )
        connection.commit()
    return get_order(order_id)


def search_orders(needle: str) -> list[dict[str, Any]]:
    needle = needle.strip().lstrip("@").lower()
    if not needle:
        return []
    pattern = f"%{needle}%"
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT * FROM orders
            WHERE lower(telegram_user_id) LIKE ?
               OR lower(COALESCE(username, '')) LIKE ?
               OR lower(COALESCE(customer_name, '')) LIKE ?
            ORDER BY created_at DESC
            LIMIT 100
            """,
            (pattern, pattern, pattern),
        ).fetchall()
    return [row_to_order(row) for row in rows if row_to_order(row) is not None]


def dashboard_summary() -> dict[str, int]:
    with get_connection() as connection:
        total = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        awaiting = connection.execute(
            "SELECT COUNT(*) FROM orders WHERE status = 'awaiting_payment'"
        ).fetchone()[0]
        review = connection.execute(
            "SELECT COUNT(*) FROM orders WHERE status = 'payment_review'"
        ).fetchone()[0]
        active = connection.execute(
            """
            SELECT COUNT(*) FROM orders
            WHERE status NOT IN ('delivered', 'cancelled')
            """
        ).fetchone()[0]
        delivered = connection.execute(
            "SELECT COUNT(*) FROM orders WHERE status = 'delivered'"
        ).fetchone()[0]
        revenue = connection.execute(
            """
            SELECT COALESCE(SUM(total_rub), 0) FROM orders
            WHERE payment_status = 'confirmed'
            """
        ).fetchone()[0]
    return {
        "total_orders": int(total),
        "awaiting_payment": int(awaiting),
        "payment_review": int(review),
        "active_orders": int(active),
        "delivered_orders": int(delivered),
        "total_revenue_rub": int(revenue),
    }


# ---------------------------------------------------------------------------
# Telegram UI
# ---------------------------------------------------------------------------

def reply_keyboard(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
    )


def main_keyboard(chat_id: int) -> ReplyKeyboardMarkup:
    if str(chat_id) in ADMIN_IDS:
        return reply_keyboard(
            [
                ["🛡️ Админ-панель", "🔎 Найти покупателя"],
                ["💳 Оплаты на проверке", "📦 Последние заказы"],
                ["🛍️ Купить вещь", "📦 Мои заказы"],
                ["🏠 В меню", "❌ Отменить заказ"],
            ]
        )
    return reply_keyboard(
        [
            ["📚 Как это работает"],
            ["🛍️ Купить вещь", "📦 Мои заказы"],
            ["💬 Поддержка", "❌ Отменить заказ"],
        ]
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return reply_keyboard([["❌ Отменить заказ"]])


def admin_back_keyboard() -> ReplyKeyboardMarkup:
    return reply_keyboard([["⬅️ В админ-панель"]])


def admin_order_keyboard() -> ReplyKeyboardMarkup:
    return reply_keyboard(
        [
            ["✅ Оплата пришла", "⚠️ Оплата не найдена"],
            ["🛍️ Заказано", "🚚 В пути"],
            ["📍 Обновить где товар", "🎉 Доставлено"],
            ["⛔ Отменить заказ", "⬅️ В админ-панель"],
        ]
    )


def order_payment_keyboard(
    order: dict[str, Any],
) -> ReplyKeyboardMarkup:
    rows: list[list[str]] = []
    if order["payment_status"] in {"awaiting", "rejected"}:
        rows.append([f"✅ Я оплатил заказ #{order['id']}"])
    rows.append(["🔄 Обновить мои заказы", "🏠 В меню"])
    return reply_keyboard(rows)


def money(value: int | float) -> str:
    return f"{value:,.0f}".replace(",", " ") + " ₽"


def money_cny(value: int | float) -> str:
    if float(value).is_integer():
        return f"{int(value):,}".replace(",", " ") + " ¥"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ¥"


def status_label(value: str) -> str:
    return STATUS_LABELS.get(value, value)


def payment_label(value: str) -> str:
    return PAYMENT_LABELS.get(value, value)


def order_summary(order: dict[str, Any]) -> str:
    location = order["current_location"] or (
        "Пока уточняем — обновление появится здесь!"
    )
    return "\n".join(
        [
            f"🧾 Заказ #{order['id']}",
            f"🔗 Товар: {order['product_url']}",
            f"📏 Размер: {order['size']}",
            f"🎨 Цвет: {order['color']}",
            "",
            (
                f"👟 Цена вещи: {money_cny(order['product_price_cny'])} "
                f"≈ {money(order['product_price_rub'])}"
            ),
            f"💱 Курс расчёта: 1 ¥ = {order['exchange_rate_cny_rub']} ₽",
            f"➕ Наценка {order['markup_percent']}%: {money(order['markup_rub'])}",
            f"🧩 Сервисный сбор: {money(order['service_fee_rub'])}",
            f"💰 Итого к оплате: {money(order['total_rub'])}",
            f"📍 Где товар: {location}",
        ]
    )


def order_button(order: dict[str, Any], prefix: str = "👤") -> str:
    customer = order["username"] or order["customer_name"] or order["telegram_user_id"]
    return f"{prefix} {customer} · #{order['id']}"


async def send(
    update: Update,
    text: str,
    reply_markup: ReplyKeyboardMarkup | None = None,
) -> None:
    if update.effective_message is not None:
        await update.effective_message.reply_text(text, reply_markup=reply_markup)


async def notify_admins(
    application: Application,
    text: str,
    reply_markup: ReplyKeyboardMarkup | None = None,
) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.send_message(
                chat_id=int(admin_id),
                text=text,
                reply_markup=reply_markup,
            )
        except Exception:
            logger.exception("Не удалось отправить сообщение администратору %s", admin_id)


async def show_start(update: Update) -> None:
    chat_id = update.effective_chat.id
    await send(
        update,
        "\n".join(
            [
                "Привет! 👋",
                "",
                "Я твой помощник по заказу оригинальных вещей с Poizon и Dewu 🛍️",
                "Помогу пройти весь путь спокойно и без лишней путаницы: от ссылки на товар до доставки 📦",
                "",
                "Нажми кнопку ниже — начнём! ✨",
            ]
        ),
        main_keyboard(chat_id),
    )


async def show_info(update: Update) -> None:
    await send(
        update,
        "\n".join(
            [
                "📚 Как проходит заказ",
                "",
                "1️⃣ Отправляешь ссылку на понравившуюся вещь.",
                "2️⃣ Указываешь размер, цвет и цену в юанях.",
                "3️⃣ Я сразу показываю подробный расчёт суммы.",
                "4️⃣ Оплачиваешь заказ менеджеру и нажимаешь «Я оплатил».",
                "5️⃣ Администратор проверяет перевод и оформляет покупку.",
                "6️⃣ Следишь за движением заказа в разделе «Мои заказы».",
                "",
                (
                    f"🧮 Расчёт: цена вещи в ¥ × {CNY_RUB_RATE:g} ₽ "
                    "+ 5% наценка + 29 ₽ сервисного сбора."
                ),
                "💡 Никаких скрытых платежей — итоговую сумму увидишь до оплаты.",
                "",
                "⚠️ Важно: после оформления заказа возврат вещей невозможен. "
                "Пожалуйста, внимательно проверь размер, цвет и ссылку перед оплатой.",
            ]
        ),
        main_keyboard(update.effective_chat.id),
    )


async def start_purchase(update: Update) -> None:
    sessions[str(update.effective_chat.id)] = {"step": "link"}
    await send(
        update,
        "Отлично, начинаем! 🚀\n\n"
        "Пришли ссылку на вещь с Poizon или Dewu — я сохраню её для заказа.\n"
        "Можно просто скопировать ссылку из приложения и отправить сюда 👇",
        cancel_keyboard(),
    )


async def show_orders(update: Update) -> None:
    chat_id = update.effective_chat.id
    orders = list_orders(telegram_user_id=str(chat_id), limit=MAX_USER_ORDERS)
    if not orders:
        await send(
            update,
            "📦 У тебя пока нет заказов.\n\n"
            "Но это легко исправить — выбери «Купить вещь» и найдём что-нибудь "
            "классное ✨",
            main_keyboard(chat_id),
        )
        return

    sessions[str(chat_id)] = {
        "step": "user_orders",
        "order_ids": [order["id"] for order in orders],
    }
    for order in orders:
        location = order["current_location"] or "Пока уточняем — скоро добавим обновление!"
        await send(
            update,
            "\n".join(
                [
                    f"📦 Заказ #{order['id']}",
                    f"💰 Сумма: {money(order['total_rub'])}",
                    f"📊 Статус: {status_label(order['status'])}",
                    f"💳 Оплата: {payment_label(order['payment_status'])}",
                    f"📍 Где товар: {location}",
                ]
            ),
            order_payment_keyboard(order),
        )


async def finish_purchase(
    update: Update,
    application: Application,
    session: dict[str, Any],
) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id
    username = f"@{user.username}" if user and user.username else None
    customer_name = (
        " ".join(
            part
            for part in [
                user.first_name if user else None,
                user.last_name if user else None,
            ]
            if part
        )
        or None
    )

    order = create_order(
        telegram_user_id=str(chat_id),
        username=username,
        customer_name=customer_name,
        product_url=session["product_url"],
        size=session["size"],
        color=session["color"],
        product_price_cny=session["product_price_cny"],
    )
    sessions.pop(str(chat_id), None)

    await send(
        update,
        "\n".join(
            [
                "🎊 Заказ почти готов!",
                "",
                order_summary(order),
                "",
                f"💳 Для оплаты напиши {PAYMENT_CONTACT}.",
                "После перевода обязательно нажми кнопку «Я оплатил» — "
                "так администратор быстрее увидит заявку ✅",
                "",
                "⚠️ Проверь детали ещё раз: после оформления возврат вещей невозможен.",
            ]
        ),
        order_payment_keyboard(order),
    )

    for admin_id in ADMIN_IDS:
        sessions[admin_id] = {"step": "admin_order", "order_id": order["id"]}

    await notify_admins(
        application,
        "\n".join(
            [
                f"🆕 Новый заказ #{order['id']}",
                f"👤 Пользователь: {order['customer_name'] or 'без имени'}",
                f"🔖 Username: {order['username'] or 'не указан'}",
                f"🆔 Telegram ID: {order['telegram_user_id']}",
                order_summary(order),
                "",
                "Проверьте перевод и выберите действие ниже 👇",
            ]
        ),
        admin_order_keyboard(),
    )


async def confirm_customer_payment(
    update: Update,
    application: Application,
    order_id: int,
) -> None:
    chat_id = update.effective_chat.id
    order = get_order(order_id)
    if order is None or order["telegram_user_id"] != str(chat_id):
        await send(update, "Не нашёл этот заказ среди твоих заказов 🤔")
        return

    updated = update_payment_status(order_id, "under_review")
    if updated is None:
        return

    await send(
        update,
        "✅ Отметка получена!\n\n"
        "Администратор проверит перевод и обновит статус. Спасибо, что всё "
        "сделал по инструкции 💪",
        main_keyboard(chat_id),
    )
    for admin_id in ADMIN_IDS:
        sessions[admin_id] = {"step": "admin_order", "order_id": order_id}
    await notify_admins(
        application,
        (
            f"💳 Пользователь {order['username'] or order['customer_name'] or order['telegram_user_id']} "
            f"отметил оплату по заказу #{order_id}.\n\n"
            "Проверьте перевод и нажмите кнопку ниже 👇"
        ),
        admin_order_keyboard(),
    )


async def notify_order_status(
    application: Application,
    order: dict[str, Any],
) -> None:
    location = order["current_location"] or "Пока уточняем — скоро сообщим!"
    await application.bot.send_message(
        chat_id=int(order["telegram_user_id"]),
        text="\n".join(
            [
                f"📣 Обновление по заказу #{order['id']}",
                "",
                f"📦 Статус: {status_label(order['status'])}",
                f"💳 Оплата: {payment_label(order['payment_status'])}",
                f"📍 Где товар: {location}",
                "",
                "Мы держим всё под контролем — следующее обновление появится здесь ✨",
            ]
        ),
        reply_markup=reply_keyboard(
            [
                ["📦 Мои заказы", "🏠 В меню"],
                ["❌ Отменить заказ"],
            ]
        ),
    )


# ---------------------------------------------------------------------------
# Админские экраны
# ---------------------------------------------------------------------------

async def show_admin_panel(update: Update) -> None:
    chat_id = update.effective_chat.id
    summary = dashboard_summary()
    orders = list_orders(limit=MAX_ADMIN_ORDERS)
    visible_orders = orders[:8]
    sessions[str(chat_id)] = {
        "step": "admin_select",
        "order_ids": [order["id"] for order in visible_orders],
    }

    rows = [
        ["🔄 Обновить панель", "🔎 Найти покупателя"],
        ["💳 Оплаты на проверке", "📦 Последние заказы"],
    ]
    rows.extend([[order_button(order)] for order in visible_orders])
    rows.append(["🏠 В меню"])

    await send(
        update,
        "\n".join(
            [
                "🛡️ Панель администратора",
                "",
                f"📦 Всего заказов: {summary['total_orders']}",
                f"🔎 Нужно проверить оплату: {summary['payment_review']}",
                f"🚚 Активных заказов: {summary['active_orders']}",
                f"🎉 Доставлено: {summary['delivered_orders']}",
                "",
                "👇 Последние заказы:" if orders else "✨ Заказов пока нет.",
            ]
        ),
        reply_keyboard(rows),
    )


async def show_admin_payments(update: Update) -> None:
    chat_id = update.effective_chat.id
    orders = list_orders(status="payment_review", limit=MAX_ADMIN_ORDERS)
    if not orders:
        sessions.pop(str(chat_id), None)
        await send(
            update,
            "🎉 Сейчас нет оплат, ожидающих проверки!\n\n"
            "Как только покупатель отметит перевод, заявка появится здесь 💳",
            main_keyboard(chat_id),
        )
        return

    sessions[str(chat_id)] = {
        "step": "admin_select",
        "order_ids": [order["id"] for order in orders],
    }
    rows = [[f"🔎 #{order['id']} · {order['username'] or order['telegram_user_id']}"] for order in orders]
    rows.append(["⬅️ В админ-панель"])
    await send(
        update,
        "💳 Оплаты на проверке — выбери заказ кнопкой ниже:",
        reply_keyboard(rows),
    )


async def show_admin_order(
    update: Update,
    order_id: int,
) -> None:
    order = get_order(order_id)
    if order is None:
        await send(
            update,
            "😔 Заказ не найден. Вернись в панель и выбери его заново.",
            main_keyboard(update.effective_chat.id),
        )
        return

    sessions[str(update.effective_chat.id)] = {
        "step": "admin_order",
        "order_id": order_id,
    }
    await send(
        update,
        "\n".join(
            [
                "🛡️ Карточка заказа",
                "",
                order_summary(order),
                "",
                f"👤 Покупатель: {order['customer_name'] or 'без имени'}",
                f"🔖 Username: {order['username'] or 'не указан'}",
                f"🆔 Telegram ID: {order['telegram_user_id']}",
                "",
                f"📊 Статус: {status_label(order['status'])}",
                f"💳 Оплата: {payment_label(order['payment_status'])}",
                "",
                "Выбери действие в клавиатуре ниже 👇",
            ]
        ),
        admin_order_keyboard(),
    )


# ---------------------------------------------------------------------------
# Обработчики Telegram
# ---------------------------------------------------------------------------

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    sessions.pop(str(update.effective_chat.id), None)
    await send(
        update,
        "✅ Текущий заказ отменён.\n\n"
        "Если захочешь начать заново — я рядом 🛍️",
        main_keyboard(update.effective_chat.id),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    await show_start(update)


async def orders_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    del context
    await show_orders(update)


async def admin_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    del context
    if str(update.effective_chat.id) not in ADMIN_IDS:
        await send(update, "Эта команда доступна только администраторам.")
        return
    await show_admin_panel(update)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    application = context.application
    chat_id = update.effective_chat.id
    text = (update.effective_message.text or "").strip()
    is_admin = str(chat_id) in ADMIN_IDS

    # Общие кнопки пользователя.
    if text == "📚 Как это работает":
        await show_info(update)
        return
    if text == "🛍️ Купить вещь":
        await start_purchase(update)
        return
    if text in {"📦 Мои заказы", "🔄 Обновить мои заказы"}:
        await show_orders(update)
        return
    if text == "💬 Поддержка":
        sessions[str(chat_id)] = {"step": "support"}
        await send(
            update,
            "💬 Напиши сообщение для поддержки одним сообщением — мы обязательно поможем!",
            cancel_keyboard(),
        )
        return
    if text == "🏠 В меню" and not is_admin:
        await show_start(update)
        return
    if text in {"❌ Отменить заказ", "/cancel"}:
        await cancel(update, context)
        return

    paid_match = re.fullmatch(r"✅ Я оплатил заказ #(\d+)", text)
    if paid_match:
        await confirm_customer_payment(update, application, int(paid_match.group(1)))
        return

    # Общие кнопки администратора.
    if is_admin and text in {"🛡️ Админ-панель", "📦 Последние заказы", "🔄 Обновить панель"}:
        await show_admin_panel(update)
        return
    if is_admin and text == "🔎 Найти покупателя":
        sessions[str(chat_id)] = {"step": "admin_search"}
        await send(
            update,
            "🔎 Введи username, имя или Telegram ID покупателя.\n\n"
            "Например: @username или 1701942831",
            admin_back_keyboard(),
        )
        return
    if is_admin and text == "💳 Оплаты на проверке":
        await show_admin_payments(update)
        return
    if is_admin and text == "⬅️ В админ-панель":
        await show_admin_panel(update)
        return
    if is_admin and text == "🏠 В меню":
        await show_start(update)
        return

    session = sessions.get(str(chat_id))
    if session is None:
        await show_start(update)
        return

    if session["step"] == "support":
        user = update.effective_user
        author = f"@{user.username}" if user and user.username else str(chat_id)
        await notify_admins(
            application,
            f"🆘 Сообщение в поддержку от {author}:\n\n{text}",
        )
        sessions.pop(str(chat_id), None)
        await send(
            update,
            "✅ Сообщение передано поддержке!\n\n"
            "Мы уже отправили его администратору и ответим здесь, как только увидим сообщение 💬",
            main_keyboard(chat_id),
        )
        return

    if session["step"] == "admin_search":
        if not is_admin:
            return
        found = search_orders(text)
        if not found:
            await send(
                update,
                "🔎 Ничего не нашёл.\n\n"
                "Проверь username, имя или Telegram ID и попробуй ещё раз.",
                main_keyboard(chat_id),
            )
            return
        visible = found[:MAX_ADMIN_ORDERS]
        sessions[str(chat_id)] = {
            "step": "admin_select",
            "order_ids": [order["id"] for order in visible],
        }
        await send(
            update,
            f"🔎 Нашёл заказов: {len(found)}\n\nВыбери нужного покупателя:",
            reply_keyboard(
                [[order_button(order)] for order in visible]
                + [["⬅️ В админ-панель"]]
            ),
        )
        return

    if session["step"] == "admin_select":
        if not is_admin:
            return
        match = re.search(r"#(\d+)", text)
        selected_id = int(match.group(1)) if match else None
        if selected_id not in session["order_ids"]:
            await send(
                update,
                "🤔 Выбери заказ кнопкой из списка — так я точно открою нужного покупателя.",
                main_keyboard(chat_id),
            )
            return
        await show_admin_order(update, selected_id)
        return

    if session["step"] == "admin_order":
        if not is_admin:
            return
        order_id = int(session["order_id"])

        if text == "✅ Оплата пришла":
            updated = update_payment_status(order_id, "confirmed")
            if updated:
                await send(
                    update,
                    f"✅ Заказ #{order_id}: оплата подтверждена!",
                    admin_order_keyboard(),
                )
                await notify_order_status(application, updated)
            return

        if text == "⚠️ Оплата не найдена":
            updated = update_payment_status(order_id, "rejected")
            if updated:
                await send(
                    update,
                    f"⚠️ Заказ #{order_id}: оплату не нашли.",
                    admin_order_keyboard(),
                )
                await notify_order_status(application, updated)
            return

        if text in STATUS_BY_BUTTON:
            updated = update_order_status(order_id, STATUS_BY_BUTTON[text])
            if updated:
                await send(
                    update,
                    f"🎉 Заказ #{order_id}: статус изменён на "
                    f"«{status_label(updated['status'])}».",
                    admin_order_keyboard(),
                )
                await notify_order_status(application, updated)
            return

        if text == "📍 Обновить где товар":
            sessions[str(chat_id)] = {
                "step": "admin_location",
                "order_id": order_id,
            }
            await send(
                update,
                "📍 Напиши одним сообщением, где сейчас находится товар.\n\n"
                "Например: «Склад в Китае», «Проходит таможню» или "
                "«Курьер уже в пути» ✍️",
                admin_back_keyboard(),
            )
            return

    if session["step"] == "admin_location":
        if not is_admin:
            return
        if len(text) < 2:
            await send(
                update,
                "Напиши чуть подробнее, где товар сейчас находится 🙂",
                admin_back_keyboard(),
            )
            return
        updated = update_order_location(int(session["order_id"]), text)
        if updated is None:
            sessions.pop(str(chat_id), None)
            await send(
                update,
                "Не смог найти этот заказ 😔 Вернись в админ-панель и выбери его снова.",
                main_keyboard(chat_id),
            )
            return
        await send(
            update,
            f"📍 Готово! Для заказа #{updated['id']} указано новое "
            f"местоположение:\n\n«{text}» ✅",
            admin_order_keyboard(),
        )
        sessions[str(chat_id)] = {
            "step": "admin_order",
            "order_id": updated["id"],
        }
        await notify_order_status(application, updated)
        return

    if session["step"] == "user_orders" and text == "🏠 В меню":
        await show_start(update)
        return

    # Шаги оформления заказа.
    if session["step"] == "link":
        if not re.match(r"^https?://\S+$", text, flags=re.IGNORECASE):
            await send(
                update,
                "Ой, похоже, это не ссылка 🤔\n\n"
                "Пришли ссылку, которая начинается с http:// или https:// — "
                "и двинемся дальше 🚀",
                cancel_keyboard(),
            )
            return
        sessions[str(chat_id)] = {"step": "size", "product_url": text}
        await send(
            update,
            "Ссылка принята ✅\n\n"
            "Какой размер нужен? Напиши его сообщением, например: 42, M или 10 US 👟",
            cancel_keyboard(),
        )
        return

    if session["step"] == "size":
        sessions[str(chat_id)] = {
            "step": "color",
            "product_url": session["product_url"],
            "size": text,
        }
        await send(
            update,
            "Размер записал ✅\n\n"
            "Какой нужен цвет? Если цвет не важен, просто напиши «нет» 🎨",
            cancel_keyboard(),
        )
        return

    if session["step"] == "color":
        sessions[str(chat_id)] = {
            "step": "price",
            "product_url": session["product_url"],
            "size": session["size"],
            "color": "Без разницы" if text.lower() == "нет" else text,
        }
        await send(
            update,
            "Цвет записал ✅\n\n"
            "Теперь напиши цену вещи в юанях, например: 1250 ¥ 💴\n\n"
            "Я переведу сумму в рубли по текущему курсу расчёта и добавлю "
            "сервисный сбор с наценкой.",
            cancel_keyboard(),
        )
        return

    if session["step"] == "price":
        try:
            product_price_cny = float(text.replace(" ", "").replace(",", "."))
        except ValueError:
            product_price_cny = 0
        if product_price_cny <= 0:
            await send(
                update,
                "Не получилось распознать цену 😅\n\n"
                "Напиши только число больше нуля в юанях, например: 1250",
                cancel_keyboard(),
            )
            return
        session["product_price_cny"] = product_price_cny
        await finish_purchase(update, application, session)


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Открыть меню"),
            BotCommand("orders", "Мои заказы"),
            BotCommand("cancel", "Отменить текущий шаг"),
            BotCommand("admin", "Админ-панель"),
        ]
    )


def build_application() -> Application:
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("orders", orders_command))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )
    return application


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не задан TELEGRAM_BOT_TOKEN. Создай бота через @BotFather "
            "и передай токен через переменную окружения."
        )
    init_database()
    application = build_application()
    logger.info("Бот запущен. База данных: %s", DATABASE_PATH.resolve())
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()