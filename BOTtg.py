
"""
Telegram-бот магазина цифровых товаров и игровой валюты (Таджикистан).
Стек: Python 3.11+, aiogram 3.x, aiosqlite.

Возможности: каталог Игра → Подкатегория → Товар (Reply-кнопки), оплата переводом + чек,
админ-панель, промокоды, техработы, бан, ручной режим, тексты в texts.txt,
обложки игр (images/<ключ_игры>.jpg), автоотмена неоплаченных заказов.

Запуск: python bot.py
Настройки — в файле .env (см. .env.example). Тексты — в texts.txt (создаётся при первом запуске).
"""

import asyncio
import html
import logging
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from dotenv import load_dotenv

# ============================================================
# КОНФИГУРАЦИЯ (из .env)
# ============================================================
load_dotenv(".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
CARD_DC = os.getenv("CARD_DC", "не указана")          # Dushanbe City
CARD_ALIF = os.getenv("CARD_ALIF", "не указана")      # Alif
CARD_HOLDER = os.getenv("CARD_HOLDER", "")            # Имя получателя
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@support")
REVIEWS_URL = os.getenv("REVIEWS_URL", "")
NEWS_URL = os.getenv("NEWS_URL", "")
DB_PATH = os.getenv("DB_PATH", "shop.db")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGES_DIR = os.path.join(BASE_DIR, "images")
TEXTS_FILE = os.path.join(BASE_DIR, os.getenv("TEXTS_FILE", "texts.txt"))


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


AUTO_BACKUP_HOURS = _float_env("AUTO_BACKUP_HOURS", 0)    # автокопия базы в админ-чат; 0 — выключено
UNPAID_TTL_HOURS = _float_env("UNPAID_TTL_HOURS", 36)     # через сколько часов отменять неоплаченные заказы; 0 — выключено

# Таджикистан живёт по UTC+5 без перехода на летнее время
LOCAL_TZ = timezone(timedelta(hours=5))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("shop-bot")

router = Router()          # обычные пользователи
admin_router = Router()    # команды админа: пропускает только сообщения из ADMIN_CHAT_ID
admin_router.message.filter(F.chat.id == ADMIN_CHAT_ID)
router.include_router(admin_router)

db: aiosqlite.Connection  # глобальное подключение, открывается в main()

# Главные категории (игры): ключ -> (название, эмодзи).
# Картинка-обложка игры: images/<ключ>.jpg  (например images/ff.jpg)
GAMES = {
    "pubg": ("PUBG Mobile", "🔫"),
    "ff": ("Free Fire", "🔥"),
    "roblox": ("Roblox", "🧱"),
    "bs": ("Brawl Stars", "⭐"),
    "so2": ("Standoff 2", "🧨"),
    "st": ("Steam", "🕹"),
}
# Подписи кнопок игр на Reply-клавиатуре -> ключ игры
GAME_BUTTONS = {f"{emoji} {title}": key for key, (title, emoji) in GAMES.items()}

# Reply-клавиатура есть только в личных чатах
PRIVATE = F.chat.type == "private"

# Начальные подкатегории: (игра, ключ, название на кнопке, порядок).
SEED_SUBCATEGORIES = [
    # PUBG Mobile
    ("pubg", "uc", "🔫 UC", 1),
    ("pubg", "royale_pass", "🎫 Royale Pass", 2),

    # Free Fire
    ("ff", "diamonds", "💎 Алмазы", 1),
    ("ff", "oth", "🎟 Ваучеры и Прочее", 2),

    # Standoff 2
    ("so2", "gold", "🪙 Голда", 1),
    ("so2", "pass", "🎫 Пассы и Билеты", 2),
    ("so2", "oth", "🔪 Прочее", 3),

    # Roblox
    ("roblox", "robux", "🧱 Robux", 1),

    # Brawl Stars
    ("bs", "gems", "💎 Gems", 1),
]

# В какую подкатегорию попадают старые товары (из прошлых версий базы) при обновлении
DEFAULT_SUBCAT = {
    "pubg": "uc",
    "ff": "diamonds",
    "so2": "gold",
    "roblox": "robux",
    "bs": "gems",
}

# Начальный каталог: (игра, подкатегория, название, цена)
SEED_PRODUCTS = [
    # --- PUBG MOBILE ---
    ("pubg", "uc", "60 UC", 14),
    ("pubg", "uc", "325 UC", 50),
    ("pubg", "uc", "660 UC", 100),
    ("pubg", "uc", "1800 UC", 250),
    ("pubg", "royale_pass", "Elite Royale Pass", 90),
    ("pubg", "royale_pass", "Elite Royale Pass Plus", 220),

    # --- FREE FIRE ---
    ("ff", "diamonds", "100 Diamonds", 10),
    ("ff", "diamonds", "310 Diamonds", 33),
    ("ff", "diamonds", "520 Diamonds", 50),
    ("ff", "diamonds", "1060 Diamonds", 92),
    ("ff", "oth", "Лайт Ваучер", 8),
    ("ff", "oth", "Недельный Ваучер", 20),
    ("ff", "oth", "BOOYAH PASS", 50),
    ("ff", "oth", "Ваучер на месяц", 110),

    # --- STANDOFF 2 ---
    ("so2", "gold", "100 GOLD", 14),
    ("so2", "gold", "500 GOLD", 60),
    ("so2", "gold", "1000 GOLD", 100),
    ("so2", "gold", "3000 GOLD", 220),
    ("so2", "pass", "Gold Pass Osiris", 50),
    ("so2", "pass", "Gold Pass Osiris + 1000 tickets", 100),
    ("so2", "pass", "100 Golden tickets", 13),
    ("so2", "pass", "500 Golden Ticket", 45),
    ("so2", "pass", "1500 Golden Ticket", 120),
    ("so2", "pass", "5000 Golden Ticket", 270),
    ("so2", "oth", "Sticker \"Treasure hunter\"", 23),
    ("so2", "oth", "G22 \"Treasure hunter\" STATRACK", 130),
    ("so2", "oth", "M9 Bayonet \"Treasure hunter\"", 380),

    # --- ROBLOX ---
    ("roblox", "robux", "400 Robux", 40),
    ("roblox", "robux", "800 Robux", 80),
    ("roblox", "robux", "1700 Robux", 160),

    # --- BRAWL STARS ---
    ("bs", "gems", "30 Gems", 10),
    ("bs", "gems", "80 Gems", 65),
    ("bs", "gems", "170 Gems", 130),
]

# Статусы заказа
ST_NEW = "new"            # создан, ждёт оплаты
ST_PENDING = "pending"    # чек отправлен, ждёт проверки админом
ST_DONE = "done"          # выполнен
ST_REJECTED = "rejected"  # отклонён
ST_CANCELLED = "cancelled"  # отменён (клиентом или автоматически)


# ============================================================
# ТЕКСТЫ: хранятся в texts.txt, здесь — только значения по умолчанию.
# Если texts.txt нет — бот создаст его из этих значений при первом запуске.
# Если в файле нет какого-то ключа — бот берёт значение отсюда и дописывает его в конец файла.
# ============================================================
FILE_HEADER = """\
# ФАЙЛ ТЕКСТОВ БОТА. Редактируйте текст под строками вида  === ключ ===
# Строки, начинающиеся с «# » (решётка и пробел), — комментарии, бот их игнорирует.
# Форматирование — HTML Telegram: <b>жирный</b>, <i>курсив</i>, <code>моноширинный</code>.
# Подстановки в фигурных скобках ({order_id}, {title} ...) не удаляйте и не переименовывайте:
# бот заменит их данными заказа. Названия подстановок указаны в комментарии над каждым текстом.
#
# КАРТИНКИ ИГР. Одна обложка на игру: положите images/<ключ_игры>.jpg (или .png / .webp):
#   images/pubg.jpg, images/ff.jpg, images/roblox.jpg, images/bs.jpg.
#   Она показывается при входе в игру (выбор подкатегории и список товаров). Нет файла — обычный текст.
#   Картинки к остальным сообщениям: images/<ключ_текста>.jpg, например images/welcome.jpg (баннер /start).
#   Либо первой строкой текста укажите  @image: имя_файла.jpg  (или  @image: -  чтобы отключить картинку).
#   Картинки можно загружать прямо из админ-чата командой /setimage КЛЮЧ.
#
# Подсказка «где найти ID» для каждой игры — тексты uid_help_pubg, uid_help_ff, uid_help_roblox, uid_help_bs.
# Ручной режим: отдельный текст для игры — раздел === manual_roblox === и т. д.
#
# После правок отправьте в админ-чате /reloadtexts — перезапускать бота не нужно.
# Список всех ключей: команда /texts.
"""

# (ключ, подсказка про подстановки, текст по умолчанию)
DEFAULTS: list[tuple[str, str, str]] = [
    # ---- Кнопки (подписи) ----
    ("btn_games", "Подписи кнопок. Новые подписи появятся у пользователей при следующем показе меню.", "🎮 Пополнить / Игры"),
    ("btn_instruction", "", "📖 Инструкция"),
    ("btn_support", "", "💬 Поддержка"),
    ("btn_orders", "", "📦 Мои заказы"),
    ("btn_home", "", "🏠 Главное меню"),
    ("btn_back", "Кнопка «Назад» на каждом шаге каталога", "⬅️ Назад"),
    ("btn_promo", "Кнопка под экраном оплаты", "🎟 Ввести промокод"),
    ("btn_paid", "Кнопка под экраном оплаты", "✅ Я оплатил (отправить чек)"),
    ("btn_cancel_order", "Кнопка под экраном оплаты", "🚫 Отменить заказ"),
    ("btn_resume", "Кнопка в «Моих заказах». Подстановки: {order_id}", "💳 Продолжить заказ №{order_id}"),
    ("btn_write_support", "Кнопка-ссылка на поддержку (ручной режим, раздел «Поддержка»)", "✍️ Написать в поддержку"),
    ("placeholder_menu", "Серая подсказка в поле ввода (главное меню)", "Выберите раздел 👇"),
    ("placeholder_games", "Серая подсказка в поле ввода (выбор игры)", "Выберите игру 👇"),
    ("placeholder_subcats", "Серая подсказка в поле ввода (выбор категории)", "Выберите категорию 👇"),
    # ---- Главное меню и навигация ----
    ("welcome", "Сообщение на /start", "👋 Добро пожаловать в магазин игровой валюты!\n\n"
     "Здесь можно быстро пополнить <b>PUBG Mobile, Free Fire, Roblox и Brawl Stars</b> "
     "с оплатой переводом на карту в сомони.\n\nВыберите действие в меню ниже 👇"),
    ("home", "", "🏠 Главное меню"),
    ("games_menu", "", "🎮 Выберите игру на клавиатуре ниже 👇"),
    ("cancelled_action", "После команды /cancel", "Действие отменено."),
    ("instruction", "", "📖 <b>Как сделать заказ</b>\n\n"
     "1. Нажмите «Пополнить / Игры» и выберите игру.\n"
     "2. Выберите категорию и нужный пакет.\n"
     "3. Отправьте свой Player ID (UID) — проверьте его внимательно!\n"
     "4. Переведите точную сумму на указанную карту (Dushanbe City или Alif).\n"
     "5. Нажмите «Я оплатил» и пришлите скриншот чека.\n"
     "6. После проверки оператор выполнит заказ, и вы получите уведомление.\n\n"
     "⏱ Обычно заказ выполняется в течение нескольких минут."),
    ("support", "Подстановки: {support}", "💬 <b>Поддержка</b>\n\nПо любым вопросам пишите: {support}\nНе забудьте указать номер заказа."),
    # ---- Каталог ----
    ("subcats_menu", "Выбор подкатегории. Подстановки: {emoji}, {game}",
     "{emoji} <b>{game}</b>\nВыберите категорию на клавиатуре ниже 👇"),
    ("packages_sub", "Список пакетов. Подстановки: {emoji}, {game}, {subcat}",
     "{emoji} <b>{game}</b> › {subcat}\nВыберите пакет:"),
    ("no_packages", "", "В этом разделе пока нет товаров."),
    ("package_unavailable", "Всплывающее окно", "Пакет недоступен"),
    ("package_gone", "", "Пакет больше недоступен. Начните заново."),
    ("manual", "РУЧНОЙ РЕЖИМ игры. Подстановки: {game}, {support}. Для отдельной игры: ключ manual_roblox и т. д.",
     "🛠 Товары категории <b>{game}</b> временно оформляются через поддержку.\n\n"
     "Нажмите кнопку ниже и напишите нам — мы всё сделаем вручную."),
    ("manual_product", "РУЧНОЙ РЕЖИМ одного товара. Подстановки: {title}, {support}",
     "📞 Товар <b>{title}</b> временно оформляется через поддержку.\n\n"
     "Нажмите кнопку ниже и напишите нам."),
    ("ask_player_id", "Подстановки: {title}, {price}, {hint}. Подсказка «где найти ID» добавляется автоматически (ключи uid_help_*).",
     "Вы выбрали: <b>{title}</b> — {price} TJS\n\n"
     "✍️ Отправьте ваш {hint} одним сообщением.\n"
     "Для отмены нажмите /cancel или выберите другой раздел в меню."),
    ("hint_uid", "Подставляется в {hint}", "Player ID (UID)"),
    ("hint_roblox", "Подставляется в {hint} для Roblox", "ник Roblox (username)"),
    ("uid_tip", "Блок-подсказка под запросом ID. Подстановки: {where}", "💡 <b>Где найти ID:</b> {where}"),
    ("uid_help", "Подсказка по умолчанию, если для игры нет своей", "Ваш ID находится в профиле персонажа под аватаркой."),
    ("uid_help_pubg", "Подсказка для PUBG Mobile",
     "нажмите на аватарку в левом верхнем углу главного экрана игры — ID (число) указан в профиле рядом с ником."),
    ("uid_help_ff", "Подсказка для Free Fire",
     "нажмите на аватарку в левом верхнем углу главного экрана — ID (число) находится в профиле под ником."),
    ("uid_help_roblox", "Подсказка для Roblox",
     "отправьте точный <b>username</b> из профиля Roblox (не отображаемое имя). Его видно в настройках аккаунта."),
    ("uid_help_bs", "Подсказка для Brawl Stars",
     "нажмите на аватарку в левом верхнем углу — тег игрока (например <code>#ABC123</code>) указан в профиле. Отправьте его вместе с #."),
    ("bad_player_id", "", "⚠️ Некорректный ID. Используйте только буквы, цифры, «_», «.», «-» или «#» (3–40 символов).\n"
     "Попробуйте ещё раз или нажмите /cancel."),
    ("player_id_not_text", "", "⚠️ Пожалуйста, отправьте ID текстом. Для отмены — /cancel."),
    # ---- Оплата ----
    ("payment", "Экран оплаты. Подстановки: {order_id}, {game}, {title}, {player_id}, {price}, {card_dc}, {card_alif}, {holder_line}, {promo_line}",
     "🧾 <b>Заказ №{order_id}</b>\n\n"
     "🎮 Игра: <b>{game}</b>\n"
     "📦 Пакет: <b>{title}</b>\n"
     "🆔 Player ID: <code>{player_id}</code>\n\n"
     "💰 К оплате: <b>{price} TJS</b>\n\n"
     "💳 Реквизиты для перевода:\n"
     "• Dushanbe City: <code>{card_dc}</code>\n"
     "• Alif: <code>{card_alif}</code>{holder_line}\n\n"
     "После перевода нажмите «Я оплатил» и пришлите скриншот чека.\n"
     "Номер заказа: <b>№{order_id}</b>{promo_line}"),
    ("payment_holder", "Строка получателя. Подстановки: {holder}", "👤 Получатель: <b>{holder}</b>"),
    ("payment_promo_line", "Строка о скидке. Подстановки: {code}, {discount}", "🎟 Промокод <b>{code}</b>: −{discount} TJS"),
    ("order_not_found", "Всплывающее окно", "Заказ не найден"),
    ("order_cancelled", "Подстановки: {order_id}", "🚫 Заказ №{order_id} отменён."),
    ("cannot_cancel", "Всплывающее окно", "Этот заказ уже нельзя отменить"),
    ("resume_closed", "Всплывающее окно", "Этот заказ уже нельзя продолжить"),
    ("order_expired", "Автоотмена неоплаченного заказа. Подстановки: {order_id}, {hours}",
     "⌛ Заказ №{order_id} не был оплачен в течение {hours} ч и автоматически отменён.\n"
     "Если вы всё ещё хотите пополнить аккаунт — оформите заказ заново."),
    ("receipt_already", "Всплывающее окно", "По этому заказу чек уже отправлен или заказ закрыт"),
    ("ask_receipt", "Подстановки: {order_id}", "📸 Пришлите <b>фото или скриншот чека</b> перевода по заказу №{order_id}.\nДля отмены — /cancel."),
    ("receipt_not_image", "", "⚠️ Пришлите чек в виде фото/скриншота (изображение), а не файл другого типа."),
    ("receipt_invalid", "", "⚠️ Нужно прислать именно <b>фото или скриншот чека</b>. Для отмены — /cancel."),
    ("order_closed", "", "Заказ не найден или уже обработан."),
    ("receipt_send_failed", "", "⚠️ Не удалось передать чек оператору. Попробуйте ещё раз чуть позже или напишите в поддержку."),
    ("receipt_accepted", "Подстановки: {order_id}",
     "✅ Чек по заказу №{order_id} получен и отправлен на проверку.\nМы сообщим вам, как только заказ будет выполнен."),
    # ---- Промокоды ----
    ("promo_ask", "", "🎟 Отправьте промокод одним сообщением.\nОтмена — /cancel."),
    ("promo_before_pay", "Всплывающее окно", "Промокод можно ввести только до оплаты"),
    ("promo_already_applied", "Всплывающее окно", "Промокод к этому заказу уже применён"),
    ("promo_bad_format", "", "⚠️ Некорректный промокод. Попробуйте ещё раз или нажмите /cancel."),
    ("promo_used", "", "⚠️ Вы уже использовали этот промокод."),
    ("promo_not_found", "", "⚠️ Такой промокод не найден. Проверьте написание или нажмите /cancel."),
    ("promo_exhausted", "", "⚠️ Этот промокод уже закончился."),
    ("promo_failed", "", "⚠️ Не удалось применить промокод к этому заказу."),
    ("promo_applied", "Подстановки: {discount}", "✅ Промокод применён! Скидка {discount} TJS."),
    ("promo_not_text", "", "⚠️ Промокод нужно отправить текстом. Для отмены — /cancel."),
    # ---- Мои заказы ----
    ("orders_empty", "", "📦 У вас пока нет заказов."),
    ("orders_title", "", "📦 <b>Ваши последние заказы:</b>\n"),
    ("orders_line", "Подстановки: {order_id}, {game}, {title}, {price}, {status}", "№{order_id} — {game}, {title} ({price} TJS)\n{status}\n"),
    ("status_new", "Статусы заказов", "🕓 Ожидает оплаты"),
    ("status_pending", "", "⏳ На проверке"),
    ("status_done", "", "✅ Выполнен"),
    ("status_rejected", "", "❌ Отклонён"),
    ("status_cancelled", "", "🚫 Отменён"),
    # ---- Уведомления клиенту ----
    ("order_done", "Подстановки: {order_id}", "✅ Ваш заказ №{order_id} успешно выполнен! Наслаждайтесь игрой 🎮"),
    ("order_rejected", "Подстановки: {order_id}, {reason}, {support}",
     "❌ Ваш заказ №{order_id} отклонён.\n\n<b>Причина:</b> {reason}\n\nЕсли это ошибка, напишите в поддержку: {support}"),
    ("maintenance", "Техработы (команда /maintenance)", "🛠 Идут технические работы. Попробуйте позже!"),
    ("banned", "Подстановки: {support}", "🚫 Доступ ограничен. Свяжитесь с поддержкой: {support}"),
    ("btn_reviews", "", "⭐ Отзывы"),
    ("btn_news", "", "📢 Новости"),
    ("btn_open_channel", "Кнопка-ссылка под сообщением", "Открыть канал ↗️"),
    ("reviews", "", "⭐ <b>Отзывы наших клиентов</b>\n\nПосмотрите, что о нас пишут, и оставьте свой отзыв в канале:"),
    ("news", "", "📢 <b>Новости и акции</b>\n\nПодпишитесь, чтобы не пропускать скидки и обновления:"),
]

DEFAULT_TEXTS = {k: t for k, _, t in DEFAULTS}
TEXT_HINTS = {k: h for k, h, _ in DEFAULTS}

_texts: dict[str, str] = dict(DEFAULT_TEXTS)        # текущие тексты (файл + значения по умолчанию)
_text_meta: dict[str, str | None] = {}              # ключ -> директива @image (None — не задана)
_label_history: dict[str, set[str]] = {}            # старые подписи кнопок, чтобы они не «ломались» после правки
HEADER_RE = re.compile(r"^===\s*([A-Za-z0-9_]+)\s*===\s*$")


class _SafeDict(dict):
    """Неизвестные {подстановки} остаются в тексте как есть, а не вызывают ошибку."""
    def __missing__(self, key):
        return "{" + key + "}"


def tx(key: str, **fmt) -> str:
    """Возвращает текст по ключу с подстановками."""
    text = _texts.get(key) or DEFAULT_TEXTS.get(key, key)
    try:
        return text.format_map(_SafeDict(fmt))
    except (ValueError, IndexError, KeyError, AttributeError):
        return text  # в тексте кривые фигурные скобки — отправляем как есть


def _render_sections(items) -> str:
    parts = []
    for key, hint, text in items:
        if hint:
            parts.append(f"# {hint}")
        parts.append(f"=== {key} ===\n{text}\n")
    return "\n".join(parts)


def write_default_texts_file() -> None:
    with open(TEXTS_FILE, "w", encoding="utf-8") as f:
        f.write(FILE_HEADER + "\n" + _render_sections(DEFAULTS))


def parse_texts(raw: str) -> tuple[dict[str, str], dict[str, str | None]]:
    texts: dict[str, str] = {}
    meta: dict[str, str | None] = {}
    key: str | None = None
    buf: list[str] = []

    def finish():
        if key is None:
            return
        lines = list(buf)
        while lines and not lines[0].strip():
            lines.pop(0)
        image = None
        if lines and lines[0].strip().lower().startswith("@image:"):
            image = lines.pop(0).split(":", 1)[1].strip() or None
        texts[key] = "\n".join(lines).strip("\n").rstrip()
        meta[key] = image

    for line in raw.splitlines():
        m = HEADER_RE.match(line)
        if m:
            finish()
            key, buf = m.group(1).lower(), []
            continue
        if key is None:
            continue                      # всё до первого заголовка — комментарии
        if line.startswith("# ") or line == "#":
            continue                      # строка-комментарий
        buf.append(line)
    finish()
    return texts, meta


def load_texts() -> list[str]:
    """(Пере)читывает texts.txt. Возвращает список замечаний для админа."""
    global _texts, _text_meta
    problems: list[str] = []
    texts = dict(DEFAULT_TEXTS)
    meta: dict[str, str | None] = {}

    if not os.path.isfile(TEXTS_FILE):
        try:
            write_default_texts_file()
            log.info("Создан файл текстов: %s", TEXTS_FILE)
        except OSError as e:
            problems.append(f"Не удалось создать {os.path.basename(TEXTS_FILE)}: {e}")
    else:
        try:
            with open(TEXTS_FILE, encoding="utf-8-sig") as f:  # utf-8-sig — терпит BOM от Блокнота
                raw = f.read()
        except OSError as e:
            problems.append(f"Не удалось прочитать {os.path.basename(TEXTS_FILE)}: {e}")
        else:
            parsed, parsed_meta = parse_texts(raw)
            for key, text in parsed.items():
                if not text:
                    problems.append(f"«{key}»: пустой текст — используется стандартный")
                    continue
                default = DEFAULT_TEXTS.get(key)
                if default is not None:
                    unknown = set(re.findall(r"\{(\w+)\}", text)) - set(re.findall(r"\{(\w+)\}", default))
                    if unknown:
                        problems.append(f"«{key}»: неизвестные подстановки {', '.join('{' + u + '}' for u in sorted(unknown))}")
                texts[key] = text
            meta = parsed_meta

            # Новые тексты бота, которых ещё нет в вашем файле, дописываем в его конец — чтобы их можно было править
            missing = [d for d in DEFAULTS if d[0] not in parsed]
            if missing:
                try:
                    with open(TEXTS_FILE, "a", encoding="utf-8") as f:
                        f.write("\n\n# === Добавлено автоматически: новые тексты бота ===\n\n" + _render_sections(missing))
                    log.info("В %s дописаны новые тексты: %s", os.path.basename(TEXTS_FILE), ", ".join(d[0] for d in missing))
                except OSError as e:
                    problems.append(f"Не удалось дописать новые тексты в файл: {e}")

    _texts, _text_meta = texts, meta
    for key, value in _texts.items():
        if key.startswith("btn_"):
            _label_history.setdefault(key, set()).add(value)
    return problems


def status_text(status: str) -> str:
    return tx(f"status_{status}")


# ============================================================
# КАРТИНКИ К СООБЩЕНИЯМ
# Игровые экраны (подкатегории, список товаров, ручной режим) берут ОДНУ обложку игры: images/<ключ_игры>.jpg.
# Остальные сообщения: images/<ключ_текста>.jpg (необязательно).
# ============================================================
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
_photo_cache: dict[tuple[str, float], str] = {}  # (путь, дата изменения) -> file_id в Telegram


def find_image(keys: list[str]) -> str | None:
    """Ищет картинку по списку ключей (от частного к общему). @image: - отключает картинку."""
    for k in keys:
        directive = _text_meta.get(k)
        if directive == "-":
            return None
        if directive:
            path = os.path.join(IMAGES_DIR, os.path.basename(directive))
            if os.path.isfile(path):
                return path
            log.warning("Картинка %s из @image для «%s» не найдена", directive, k)
        for ext in IMAGE_EXTS:
            path = os.path.join(IMAGES_DIR, k + ext)
            if os.path.isfile(path):
                return path
    return None


async def send_screen_to(bot: Bot, chat_id: int, keys, reply_markup=None,
                         image_keys: list[str] | None = None, suffix: str = "", **fmt) -> Message:
    """Отправляет сообщение по ключу(ам) текста. Если находится картинка — текст идёт подписью к ней.
    keys       — строка или список ключей текста от частного к общему: ["manual_roblox", "manual"].
    image_keys — ключи картинок, проверяемые первыми (для игровых экранов — [game_key]).
    suffix     — дополнительный текст в конец (например, подсказка «где найти ID»)."""
    keys = [keys] if isinstance(keys, str) else list(keys)
    text_key = next((k for k in keys if k in _texts), keys[-1])
    text = tx(text_key, **fmt) + suffix

    path = find_image([*(image_keys or []), *keys])
    if path:
        cache_key = None
        try:
            cache_key = (path, os.path.getmtime(path))
            photo = _photo_cache.get(cache_key) or FSInputFile(path)
            if len(text) <= 1024:                       # лимит подписи к фото в Telegram
                sent = await bot.send_photo(chat_id, photo, caption=text, reply_markup=reply_markup)
            else:                                       # длинный текст — фото отдельно, текст следом
                sent = await bot.send_photo(chat_id, photo)
                await bot.send_message(chat_id, text, reply_markup=reply_markup)
            if cache_key not in _photo_cache and sent.photo:
                _photo_cache[cache_key] = sent.photo[-1].file_id
            return sent
        except (TelegramAPIError, OSError) as e:
            log.warning("Не удалось отправить картинку %s: %s", path, e)
            if cache_key:
                _photo_cache.pop(cache_key, None)
    return await bot.send_message(chat_id, text, reply_markup=reply_markup)


async def send_screen(message: Message, keys, reply_markup=None, **fmt) -> Message:
    return await send_screen_to(message.bot, message.chat.id, keys, reply_markup=reply_markup, **fmt)


async def delete_quietly(message) -> None:
    try:
        await message.delete()
    except (TelegramAPIError, AttributeError):
        pass


async def replace_screen(message: Message, keys, reply_markup=None, **fmt) -> Message:
    """Вместо редактирования (с картинками оно невозможно) удаляет старое сообщение и шлёт новое."""
    await delete_quietly(message)
    return await send_screen(message, keys, reply_markup=reply_markup, **fmt)


# ============================================================
# СОСТОЯНИЯ (FSM)
# ============================================================
class OrderStates(StatesGroup):
    waiting_player_id = State()   # ждём Player ID
    waiting_receipt = State()     # ждём фото чека


class PromoStates(StatesGroup):
    waiting_code = State()        # ждём промокод


class AdminStates(StatesGroup):
    waiting_reject_reason = State()  # админ вводит причину отказа


# ============================================================
# БАЗА ДАННЫХ
# ============================================================
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def _has_column(table: str, column: str) -> bool:
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        return any(row[1] == column for row in await cur.fetchall())


async def init_db() -> None:
    """Создаёт таблицы, дополняет старую базу новыми колонками/подкатегориями и заполняет каталог, если он пуст."""
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT,
            full_name  TEXT,
            created_at TEXT,
            banned     INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS subcategories (
            game_key   TEXT NOT NULL,
            subcat_key TEXT NOT NULL,
            title      TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            active     INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (game_key, subcat_key)
        );
        CREATE TABLE IF NOT EXISTS products (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            game_key        TEXT NOT NULL,
            subcategory_key TEXT,
            title           TEXT NOT NULL,
            price_tjs       REAL NOT NULL,
            active          INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS orders (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            product_id      INTEGER NOT NULL,
            game_key        TEXT NOT NULL,
            product_title   TEXT NOT NULL,
            price_tjs       REAL NOT NULL,
            player_id       TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'new',
            receipt_file_id TEXT,
            reject_reason   TEXT,
            created_at      TEXT,
            updated_at      TEXT,
            promo_code      TEXT,
            discount_tjs    REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS promocodes (
            code         TEXT PRIMARY KEY,
            discount_tjs REAL NOT NULL,
            quantity     INTEGER NOT NULL,
            used         INTEGER NOT NULL DEFAULT 0,
            active       INTEGER NOT NULL DEFAULT 1,
            created_at   TEXT
        );
        CREATE TABLE IF NOT EXISTS promo_uses (
            code    TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (code, user_id)
        );
        """
    )
    # Миграция для баз, созданных старыми версиями бота
    if not await _has_column("users", "banned"):
        await db.execute("ALTER TABLE users ADD COLUMN banned INTEGER NOT NULL DEFAULT 0")
    if not await _has_column("orders", "promo_code"):
        await db.execute("ALTER TABLE orders ADD COLUMN promo_code TEXT")
    if not await _has_column("orders", "discount_tjs"):
        await db.execute("ALTER TABLE orders ADD COLUMN discount_tjs REAL NOT NULL DEFAULT 0")
    if not await _has_column("products", "subcategory_key"):
        await db.execute("ALTER TABLE products ADD COLUMN subcategory_key TEXT")

    # Подкатегории: при первом запуске создаём стандартные
    async with db.execute("SELECT COUNT(*) FROM subcategories") as cur:
        (sub_count,) = await cur.fetchone()
    if sub_count == 0:
        await db.executemany(
            "INSERT INTO subcategories (game_key, subcat_key, title, sort_order) VALUES (?, ?, ?, ?)",
            SEED_SUBCATEGORIES,
        )
    # Товары из прошлых версий (без подкатегории) раскладываем по подкатегориям по умолчанию
    for game_key, subcat_key in DEFAULT_SUBCAT.items():
        await db.execute(
            "UPDATE products SET subcategory_key=? WHERE game_key=? AND (subcategory_key IS NULL OR subcategory_key='')",
            (subcat_key, game_key),
        )

    async with db.execute("SELECT COUNT(*) FROM products") as cur:
        (count,) = await cur.fetchone()
    if count == 0:
        await db.executemany(
            "INSERT INTO products (game_key, subcategory_key, title, price_tjs) VALUES (?, ?, ?, ?)", SEED_PRODUCTS
        )
    await db.commit()


async def upsert_user(message_or_cb) -> None:
    u = message_or_cb.from_user
    await db.execute(
        """INSERT INTO users (user_id, username, full_name, created_at) VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name""",
        (u.id, u.username, u.full_name, now_iso()),
    )
    await db.commit()


async def get_subcats(game_key: str):
    """Подкатегории игры, видимые клиентам: активные и содержащие хотя бы один активный товар."""
    async with db.execute(
        """SELECT s.subcat_key, s.title FROM subcategories s
           WHERE s.game_key=? AND s.active=1
             AND EXISTS (SELECT 1 FROM products p
                         WHERE p.game_key=s.game_key AND p.subcategory_key=s.subcat_key AND p.active=1)
           ORDER BY s.sort_order, s.title""",
        (game_key,),
    ) as cur:
        return await cur.fetchall()


async def get_subcat_title(game_key: str, subcat_key: str) -> str:
    async with db.execute(
        "SELECT title FROM subcategories WHERE game_key=? AND subcat_key=?", (game_key, subcat_key)
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else subcat_key


async def get_products(game_key: str, subcat_key: str):
    async with db.execute(
        """SELECT id, title, price_tjs FROM products
           WHERE game_key=? AND subcategory_key=? AND active=1 ORDER BY price_tjs""",
        (game_key, subcat_key),
    ) as cur:
        return await cur.fetchall()


async def get_product(product_id: int):
    async with db.execute(
        "SELECT id, game_key, title, price_tjs, subcategory_key FROM products WHERE id=? AND active=1", (product_id,)
    ) as cur:
        return await cur.fetchone()


async def get_order(order_id: int):
    async with db.execute(
        """SELECT id, user_id, product_id, game_key, product_title, price_tjs, player_id, status,
                  receipt_file_id, reject_reason FROM orders WHERE id=?""",
        (order_id,),
    ) as cur:
        return await cur.fetchone()


async def set_status(order_id: int, new_status: str, expected: str, reason: str | None = None) -> bool:
    """Меняет статус, только если текущий равен expected (защита от двойного нажатия)."""
    cur = await db.execute(
        "UPDATE orders SET status=?, reject_reason=COALESCE(?, reject_reason), updated_at=? WHERE id=? AND status=?",
        (new_status, reason, now_iso(), order_id, expected),
    )
    await db.commit()
    return cur.rowcount > 0


async def release_promo(order_id: int) -> None:
    """Возвращает использование промокода, если заказ с ним отменён (клиентом или автоматически)."""
    async with db.execute("SELECT user_id, promo_code FROM orders WHERE id=?", (order_id,)) as cur:
        row = await cur.fetchone()
    if row and row[1]:
        await db.execute("UPDATE promocodes SET used=MAX(used-1, 0) WHERE code=?", (row[1],))
        await db.execute("DELETE FROM promo_uses WHERE code=? AND user_id=?", (row[1], row[0]))
        await db.commit()


async def get_setting(key: str, default: str = "0") -> str:
    async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    await db.commit()


# ---------- Ручной режим (флаги хранятся в settings, переживают перезапуск) ----------
async def is_manual_game(game_key: str) -> bool:
    return await get_setting(f"manual:game:{game_key}") == "1"


async def is_manual_product(product_id: int) -> bool:
    return await get_setting(f"manual:product:{product_id}") == "1"


async def manual_product_ids() -> set[int]:
    async with db.execute("SELECT key FROM settings WHERE key LIKE 'manual:product:%' AND value='1'") as cur:
        return {int(row[0].rsplit(":", 1)[1]) for row in await cur.fetchall()}


def fmt_price(value: float) -> str:
    return f"{value:g}"


def esc(text: str | None) -> str:
    return html.escape(text or "")


def parse_price(raw: str) -> float | None:
    """Превращает строку в цену. Принимает и запятую, и точку. None — если некорректно."""
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        return None
    return value if 0 < value < 1_000_000 else None


# ============================================================
# КЛАВИАТУРЫ И ФИЛЬТРЫ КНОПОК
# ============================================================
    kb = ReplyKeyboardBuilder()
    kb.button(text=tx("btn_games"))
    kb.button(text=tx("btn_instruction"))
    kb.button(text=tx("btn_support"))
    kb.button(text=tx("btn_orders"))
    if REVIEWS_URL:
        kb.button(text=tx("btn_reviews"))
    if NEWS_URL:
        kb.button(text=tx("btn_news"))
    kb.adjust(1, 2, 1, 2)
    return kb.as_markup(resize_keyboard=True, is_persistent=True, input_field_placeholder=tx("placeholder_menu"))

def games_kb() -> ReplyKeyboardMarkup:
    """Выбор игры: Reply-клавиатура, плюс кнопка возврата в главное меню."""
    kb = ReplyKeyboardBuilder()
    for text in GAME_BUTTONS:
        kb.button(text=text)
    kb.button(text=tx("btn_home"))
    kb.adjust(3, 3, 1)
    return kb.as_markup(resize_keyboard=True, is_persistent=True, input_field_placeholder=tx("placeholder_games"))


def subcats_kb(subcats) -> ReplyKeyboardMarkup:
    """Подкатегории выбранной игры (из БД) + «Назад» (к списку игр) и «Главное меню»."""
    kb = ReplyKeyboardBuilder()
    for _, title in subcats:
        kb.button(text=title)
    kb.button(text=tx("btn_back"))
    kb.button(text=tx("btn_home"))
    n = len(subcats)
    kb.adjust(*([2] * (n // 2) + ([1] if n % 2 else []) + [2]))
    return kb.as_markup(resize_keyboard=True, is_persistent=True, input_field_placeholder=tx("placeholder_subcats"))


def kb_for(message: Message) -> ReplyKeyboardMarkup | None:
    """В админ-чате (группе) Reply-клавиатуру не показываем."""
    return None if message.chat.id == ADMIN_CHAT_ID else main_menu_kb()


class ButtonText(Filter):
    """Срабатывает на нажатие Reply-кнопки. Подпись берётся из texts.txt;
    старые подписи тоже узнаются — у пользователей клавиатура могла не обновиться."""
    def __init__(self, key: str) -> None:
        self.key = key

    async def __call__(self, message: Message) -> bool:
        if not message.text:
            return False
        return message.text == tx(self.key) or message.text in _label_history.get(self.key, ())


class GameButton(Filter):
    """Срабатывает на кнопку игры и передаёт в хендлер game_key."""
    async def __call__(self, message: Message):
        key = GAME_BUTTONS.get(message.text) if message.text else None
        return {"game_key": key} if key else False


class SubcatButton(Filter):
    """Срабатывает на кнопку подкатегории (названия берутся из БД) и передаёт game_key, subcat_key.
    Если одинаковое название есть у нескольких игр — выбирается подкатегория той игры, где сейчас клиент."""
    async def __call__(self, message: Message, state: FSMContext):
        text = message.text
        if not text or len(text) > 40:
            return False
        async with db.execute(
            "SELECT game_key, subcat_key FROM subcategories WHERE title=? AND active=1", (text,)
        ) as cur:
            rows = await cur.fetchall()
        if not rows:
            return False
        current = (await state.get_data()).get("browse_game")
        for game_key, subcat_key in rows:
            if game_key == current:
                return {"game_key": game_key, "subcat_key": subcat_key}
        return {"game_key": rows[0][0], "subcat_key": rows[0][1]}


def products_kb(products, manual_ids: set[int], game_key: str) -> InlineKeyboardMarkup:
    """Пакеты подкатегории — inline-кнопки. Товары в ручном режиме помечены 📞. Внизу — «Назад»."""
    kb = InlineKeyboardBuilder()
    for pid, title, price in products:
        mark = "📞 " if pid in manual_ids else ""
        kb.button(text=f"{mark}{title} — {fmt_price(price)} TJS", callback_data=f"prod:{pid}")
    kb.button(text=tx("btn_back"), callback_data=f"bk:g:{game_key}")
    kb.adjust(1)
    return kb.as_markup()


def back_to_products_kb(game_key: str, subcat_key: str | None) -> InlineKeyboardMarkup:
    """Кнопка «Назад» на шаге ввода ID: возвращает к списку пакетов."""
    data = f"bk:s:{game_key}:{subcat_key}" if subcat_key else f"bk:g:{game_key}"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=tx("btn_back"), callback_data=data)]])


def payment_kb(order_id: int, promo_applied: bool = False) -> InlineKeyboardMarkup:
    """Шаг оплаты — inline-кнопки под сообщением с реквизитами."""
    kb = InlineKeyboardBuilder()
    if not promo_applied:
        kb.button(text=tx("btn_promo"), callback_data=f"promo:{order_id}")
    kb.button(text=tx("btn_paid"), callback_data=f"paid:{order_id}")
    kb.button(text=tx("btn_cancel_order"), callback_data=f"cancel:{order_id}")
    kb.adjust(1)
    return kb.as_markup()


def admin_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Выполнено", callback_data=f"adm_ok:{order_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_no:{order_id}"),
        ]]
    )


def support_kb() -> InlineKeyboardMarkup | None:
    """Кнопка-ссылка на поддержку (только если SUPPORT_USERNAME похож на настоящий username)."""
    if re.fullmatch(r"@?[A-Za-z0-9_]{5,32}", SUPPORT_USERNAME):
        url = f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}"
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=tx("btn_write_support"), url=url)]])
    return None


def payment_fmt(order_id: int, game_key: str, title: str, price: float, player_id: str,
                promo_code: str | None = None, discount: float = 0) -> dict:
    """Подстановки для текста экрана оплаты (всё уже экранировано)."""
    holder_line = ("\n" + tx("payment_holder", holder=esc(CARD_HOLDER))) if CARD_HOLDER else ""
    promo_line = ("\n\n" + tx("payment_promo_line", code=esc(promo_code), discount=fmt_price(discount))) if promo_code else ""
    return dict(
        order_id=str(order_id), game=esc(GAMES.get(game_key, (game_key, ""))[0]), title=esc(title),
        player_id=esc(player_id), price=fmt_price(price), card_dc=esc(CARD_DC), card_alif=esc(CARD_ALIF),
        holder_line=holder_line, promo_line=promo_line,
    )


def uid_tip(game_key: str) -> str:
    """Подсказка «где найти ID» для конкретной игры (тексты uid_help_<игра> в texts.txt)."""
    help_key = f"uid_help_{game_key}"
    where = tx(help_key) if help_key in _texts else tx("uid_help")
    return "\n\n" + tx("uid_tip", where=where)


async def send_payment_screen(bot: Bot, chat_id: int, order_id: int) -> bool:
    """Показывает экран оплаты заказа (используется при создании, после промокода и при возврате к заказу)."""
    async with db.execute(
        """SELECT game_key, product_title, price_tjs, player_id, promo_code, discount_tjs
           FROM orders WHERE id=?""",
        (order_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return False
    game_key, title, price, player_id, promo, discount = row
    await send_screen_to(
        bot, chat_id, "payment", reply_markup=payment_kb(order_id, promo_applied=bool(promo)),
        **payment_fmt(order_id, game_key, title, price, player_id, promo, discount),
    )
    return True


# ============================================================
# MIDDLEWARE: техработы и бан (админ-чат не затрагивается)
# ============================================================
class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, CallbackQuery):
            chat = event.message.chat if event.message else None
        else:
            chat = getattr(event, "chat", None)
        user = getattr(event, "from_user", None)

        # Админ-чат и служебные события без пользователя пропускаем всегда
        if user is None or (chat is not None and chat.id == ADMIN_CHAT_ID):
            return await handler(event, data)

        async with db.execute("SELECT banned FROM users WHERE user_id=?", (user.id,)) as cur:
            row = await cur.fetchone()
        if row and row[0]:
            key, fmt = "banned", {"support": esc(SUPPORT_USERNAME)}
        elif await get_setting("maintenance") == "1":
            key, fmt = "maintenance", {}
        else:
            return await handler(event, data)

        try:
            if isinstance(event, CallbackQuery):
                await event.answer(tx(key, **fmt), show_alert=True)
            elif chat is not None:
                await send_screen_to(event.bot, chat.id, key, **fmt)
        except TelegramAPIError:
            pass
        return None


router.message.outer_middleware.register(AccessMiddleware())
router.callback_query.outer_middleware.register(AccessMiddleware())


# ============================================================
# КАТАЛОГ: Игра → Подкатегория → Товар
# ============================================================
async def send_manual_game(message: Message, game_key: str) -> None:
    """Ручной режим игры: вместо каталога — сообщение «обрабатывается через поддержку» + кнопка."""
    title, emoji = GAMES[game_key]
    await send_screen(message, [f"manual_{game_key}", "manual"], image_keys=[game_key],
                      reply_markup=support_kb(), game=esc(title), emoji=emoji, support=esc(SUPPORT_USERNAME))


async def show_subcats(message: Message, state: FSMContext, game_key: str) -> None:
    """Шаг 1: клиент выбрал игру — показываем Reply-кнопки подкатегорий (читаются из БД)."""
    if await is_manual_game(game_key):
        await send_manual_game(message, game_key)
        return
    subcats = await get_subcats(game_key)
    if not subcats:
        await send_screen(message, "no_packages", image_keys=[game_key])
        return
    title, emoji = GAMES[game_key]
    await state.update_data(browse_game=game_key)
    await send_screen(message, "subcats_menu", image_keys=[game_key], reply_markup=subcats_kb(subcats),
                      game=esc(title), emoji=emoji)


async def show_products(message: Message, state: FSMContext, game_key: str, subcat_key: str) -> None:
    """Шаг 2: клиент выбрал подкатегорию — показываем список товаров (inline-кнопки)."""
    products = await get_products(game_key, subcat_key)
    if not products:
        await send_screen(message, "no_packages", image_keys=[game_key])
        return
    title, emoji = GAMES[game_key]
    await state.update_data(browse_game=game_key)
    await send_screen(message, "packages_sub", image_keys=[game_key],
                      reply_markup=products_kb(products, await manual_product_ids(), game_key),
                      game=esc(title), emoji=emoji, subcat=esc(await get_subcat_title(game_key, subcat_key)))


# ============================================================
# ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЯ
# Порядок важен: кнопки меню регистрируются РАНЬШЕ обработчиков состояний,
# поэтому нажатие любой кнопки меню работает даже посреди заказа и отменяет текущий шаг.
# ============================================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await upsert_user(message)
    await send_screen(message, "welcome", reply_markup=kb_for(message))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await send_screen(message, "cancelled_action", reply_markup=kb_for(message))


@router.message(PRIVATE, ButtonText("btn_home"))
async def menu_home(message: Message, state: FSMContext):
    await state.clear()
    await send_screen(message, "home", reply_markup=main_menu_kb())


# «Назад» на клавиатуре подкатегорий возвращает к списку игр
@router.message(PRIVATE, ButtonText("btn_back"))
@router.message(PRIVATE, ButtonText("btn_games"))
async def menu_games(message: Message, state: FSMContext):
    await state.clear()
    await upsert_user(message)
    await send_screen(message, "games_menu", reply_markup=games_kb())


@router.message(PRIVATE, GameButton())
async def menu_pick_game(message: Message, state: FSMContext, game_key: str):
    await state.clear()
    await show_subcats(message, state, game_key)


@router.message(PRIVATE, SubcatButton())
async def menu_pick_subcat(message: Message, state: FSMContext, game_key: str, subcat_key: str):
    await state.clear()
    if await is_manual_game(game_key):
        await send_manual_game(message, game_key)
        return
    await show_products(message, state, game_key, subcat_key)


@router.message(PRIVATE, ButtonText("btn_instruction"))
async def menu_instruction(message: Message, state: FSMContext):
    await state.clear()
    await send_screen(message, "instruction")


@router.message(PRIVATE, ButtonText("btn_support"))
async def menu_support(message: Message, state: FSMContext):
    await state.clear()
    await send_screen(message, "support", reply_markup=support_kb(), support=esc(SUPPORT_USERNAME))

    def channel_kb(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=tx("btn_open_channel"), url=url)]])


@router.message(PRIVATE, ButtonText("btn_reviews"))
async def menu_reviews(message: Message, state: FSMContext):
    await state.clear()
    if REVIEWS_URL:
        await send_screen(message, "reviews", reply_markup=channel_kb(REVIEWS_URL))


@router.message(PRIVATE, ButtonText("btn_news"))
async def menu_news(message: Message, state: FSMContext):
    await state.clear()
    if NEWS_URL:
        await send_screen(message, "news", reply_markup=channel_kb(NEWS_URL))


@router.message(PRIVATE, ButtonText("btn_orders"))
async def menu_orders(message: Message, state: FSMContext):
    await state.clear()
    async with db.execute(
        """SELECT id, product_title, price_tjs, status, game_key FROM orders
           WHERE user_id=? ORDER BY id DESC LIMIT 10""",
        (message.from_user.id,),
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        await send_screen(message, "orders_empty")
        return
    lines = [tx("orders_title")]
    unpaid: list[int] = []
    for oid, title, price, status, game_key in rows:
        lines.append(tx("orders_line", order_id=str(oid), game=esc(GAMES.get(game_key, (game_key,))[0]),
                        title=esc(title), price=fmt_price(price), status=status_text(status)))
        if status == ST_NEW and len(unpaid) < 5:
            unpaid.append(oid)
    # Под списком — по кнопке на каждый неоплаченный заказ, чтобы вернуться к оплате в одно нажатие
    kb = InlineKeyboardBuilder()
    for oid in unpaid:
        kb.button(text=tx("btn_resume", order_id=str(oid)), callback_data=f"resume:{oid}")
    kb.adjust(1)
    await message.answer("\n".join(lines), reply_markup=kb.as_markup() if unpaid else None)


@router.callback_query(F.data.startswith("resume:"))
async def cb_resume(cb: CallbackQuery, state: FSMContext):
    order_id = int(cb.data.split(":", 1)[1])
    order = await get_order(order_id)
    if not order or order[1] != cb.from_user.id:
        await cb.answer(tx("order_not_found"), show_alert=True)
        return
    if order[7] != ST_NEW:
        await cb.answer(tx("resume_closed"), show_alert=True)
        return
    await state.clear()
    await send_payment_screen(cb.message.bot, cb.message.chat.id, order_id)
    await cb.answer()


# ---------- Кнопки «Назад» (inline) ----------
@router.callback_query(F.data.startswith("bk:"))
async def cb_back(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    await state.clear()
    msg = cb.message
    await delete_quietly(msg)
    if len(parts) >= 3 and parts[1] == "g" and parts[2] in GAMES:       # к подкатегориям игры
        await show_subcats(msg, state, parts[2])
    elif len(parts) >= 4 and parts[1] == "s" and parts[2] in GAMES:     # к списку пакетов подкатегории
        await show_products(msg, state, parts[2], parts[3])
    else:
        await send_screen(msg, "home", reply_markup=main_menu_kb())
    await cb.answer()


# ---------- Выбор пакета и ввод Player ID ----------
@router.callback_query(F.data.startswith("prod:"))
async def cb_product(cb: CallbackQuery, state: FSMContext):
    product = await get_product(int(cb.data.split(":", 1)[1]))
    if not product:
        await cb.answer(tx("package_unavailable"), show_alert=True)
        return
    pid, game_key, title, price, subcat_key = product

    # Ручной режим: на всю игру (мог быть включён, пока список висел на экране) или на один товар
    if await is_manual_game(game_key):
        await state.clear()
        await send_manual_game(cb.message, game_key)
        await cb.answer()
        return
    if await is_manual_product(pid):
        await state.clear()
        await send_screen(cb.message, "manual_product", reply_markup=support_kb(),
                          title=esc(title), support=esc(SUPPORT_USERNAME))
        await cb.answer()
        return

    await state.set_state(OrderStates.waiting_player_id)
    await state.update_data(product_id=pid, browse_game=game_key)
    hint = tx("hint_roblox") if game_key == "roblox" else tx("hint_uid")
    await replace_screen(
        cb.message, "ask_player_id", reply_markup=back_to_products_kb(game_key, subcat_key),
        suffix=uid_tip(game_key), title=esc(title), price=fmt_price(price), hint=hint,
    )
    await cb.answer()


@router.message(StateFilter(OrderStates.waiting_player_id), F.text)
async def msg_player_id(message: Message, state: FSMContext):
    player_id = message.text.strip()
    # Допускаем буквы, цифры, «_», «.», «-» и «#» (тег Brawl Stars), 3–40 символов
    if not re.fullmatch(r"[\w.\-#]{3,40}", player_id):
        await send_screen(message, "bad_player_id")
        return

    data = await state.get_data()
    product = await get_product(data.get("product_id", 0))
    if not product:
        await state.clear()
        await send_screen(message, "package_gone", reply_markup=main_menu_kb())
        return
    pid, game_key, title, price, _ = product

    await upsert_user(message)
    cur = await db.execute(
        """INSERT INTO orders (user_id, product_id, game_key, product_title, price_tjs, player_id,
                               status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?)""",
        (message.from_user.id, pid, game_key, title, price, player_id, now_iso(), now_iso()),
    )
    await db.commit()
    order_id = cur.lastrowid

    await state.clear()
    await send_payment_screen(message.bot, message.chat.id, order_id)


@router.message(StateFilter(OrderStates.waiting_player_id))
async def msg_player_id_invalid(message: Message):
    await send_screen(message, "player_id_not_text")


# ---------- Оплата ----------
@router.callback_query(F.data.startswith("cancel:"))
async def cb_cancel_order(cb: CallbackQuery, state: FSMContext):
    order_id = int(cb.data.split(":", 1)[1])
    order = await get_order(order_id)
    if not order or order[1] != cb.from_user.id:
        await cb.answer(tx("order_not_found"), show_alert=True)
        return
    await state.clear()
    if await set_status(order_id, ST_CANCELLED, ST_NEW):
        await release_promo(order_id)
        await replace_screen(cb.message, "order_cancelled", order_id=str(order_id))
    else:
        await cb.answer(tx("cannot_cancel"), show_alert=True)
        return
    await cb.answer()


@router.callback_query(F.data.startswith("paid:"))
async def cb_paid(cb: CallbackQuery, state: FSMContext):
    order_id = int(cb.data.split(":", 1)[1])
    order = await get_order(order_id)
    if not order or order[1] != cb.from_user.id:
        await cb.answer(tx("order_not_found"), show_alert=True)
        return
    if order[7] != ST_NEW:
        await cb.answer(tx("receipt_already"), show_alert=True)
        return
    await state.set_state(OrderStates.waiting_receipt)
    await state.update_data(order_id=order_id)
    await send_screen(cb.message, "ask_receipt", order_id=str(order_id))
    await cb.answer()


async def admin_caption(order_id: int, title: str) -> str:
    """Подпись к заказу для админ-чата (клиент, товар, сумма, промокод, история клиента)."""
    async with db.execute(
        """SELECT o.user_id, o.game_key, o.product_title, o.price_tjs, o.player_id, o.status,
                  o.promo_code, o.discount_tjs, u.username, u.full_name
           FROM orders o LEFT JOIN users u ON u.user_id = o.user_id WHERE o.id=?""",
        (order_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return f"<b>{title} №{order_id}</b>"
    uid, game_key, ptitle, price, player_id, status, promo, discount, username, full_name = row
    async with db.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND status=?", (uid, ST_DONE)) as cur:
        (done_cnt,) = await cur.fetchone()
    lines = [
        f"<b>{title} №{order_id}</b>",
        "",
        f"👤 Клиент: {esc(full_name)} ({esc('@' + username) if username else '—'})",
        f"🆔 Telegram ID: <code>{uid}</code>",
        f"🎮 Игра: {esc(GAMES.get(game_key, (game_key,))[0])}",
        f"📦 Товар: <b>{esc(ptitle)}</b>",
        f"💰 Сумма: <b>{fmt_price(price)} TJS</b>",
    ]
    if promo:
        lines.append(f"🎟 Промокод: {esc(promo)} (−{fmt_price(discount)} TJS)")
    lines += [
        f"🎯 Player ID: <code>{esc(player_id)}</code>",
        f"📈 Выполненных заказов у клиента: {done_cnt}",
    ]
    return "\n".join(lines)


async def send_receipt(bot: Bot, chat_id: int, file_id: str, caption: str, markup=None, is_photo: bool | None = None):
    """Отправляет чек как фото или документ (если тип неизвестен — пробует фото, затем документ)."""
    if is_photo is not False:
        try:
            return await bot.send_photo(chat_id, file_id, caption=caption, reply_markup=markup)
        except TelegramBadRequest:
            if is_photo:
                raise
    return await bot.send_document(chat_id, file_id, caption=caption, reply_markup=markup)


@router.message(StateFilter(OrderStates.waiting_receipt), F.photo | F.document)
async def msg_receipt(message: Message, state: FSMContext, bot: Bot):
    # Документ принимаем только если это изображение
    if message.document and not (message.document.mime_type or "").startswith("image/"):
        await send_screen(message, "receipt_not_image")
        return

    data = await state.get_data()
    order_id = data.get("order_id")
    order = await get_order(order_id) if order_id else None
    if not order or order[1] != message.from_user.id or order[7] != ST_NEW:
        await state.clear()
        await send_screen(message, "order_closed", reply_markup=main_menu_kb())
        return

    is_photo = bool(message.photo)
    file_id = message.photo[-1].file_id if is_photo else message.document.file_id

    # Переводим заказ в статус «на проверке»
    cur = await db.execute(
        "UPDATE orders SET status=?, receipt_file_id=?, updated_at=? WHERE id=? AND status=?",
        (ST_PENDING, file_id, now_iso(), order_id, ST_NEW),
    )
    await db.commit()
    if cur.rowcount == 0:
        await state.clear()
        await send_screen(message, "order_closed", reply_markup=main_menu_kb())
        return

    await upsert_user(message)  # обновим имя/username клиента перед отправкой админам
    caption = await admin_caption(order_id, "🆕 Новый заказ")
    try:
        await send_receipt(bot, ADMIN_CHAT_ID, file_id, caption, admin_kb(order_id), is_photo=is_photo)
    except TelegramAPIError as e:
        # Не удалось доставить админам — возвращаем заказ в «ожидает оплаты», чтобы клиент мог повторить
        log.error("Не удалось отправить заказ №%s в админ-чат: %s", order_id, e)
        await set_status(order_id, ST_NEW, ST_PENDING)
        await send_screen(message, "receipt_send_failed", reply_markup=payment_kb(order_id))
        await state.clear()
        return

    await state.clear()
    await send_screen(message, "receipt_accepted", reply_markup=main_menu_kb(), order_id=str(order_id))


@router.message(StateFilter(OrderStates.waiting_receipt))
async def msg_receipt_invalid(message: Message):
    # Пользователь прислал текст/стикер/etc. вместо фото
    await send_screen(message, "receipt_invalid")


# ---------- Промокоды (кнопка на экране оплаты) ----------
@router.callback_query(F.data.startswith("promo:"))
async def cb_promo(cb: CallbackQuery, state: FSMContext):
    order_id = int(cb.data.split(":", 1)[1])
    order = await get_order(order_id)
    if not order or order[1] != cb.from_user.id:
        await cb.answer(tx("order_not_found"), show_alert=True)
        return
    if order[7] != ST_NEW:
        await cb.answer(tx("promo_before_pay"), show_alert=True)
        return
    async with db.execute("SELECT promo_code FROM orders WHERE id=?", (order_id,)) as cur:
        row = await cur.fetchone()
    if row and row[0]:
        await cb.answer(tx("promo_already_applied"), show_alert=True)
        return
    await state.set_state(PromoStates.waiting_code)
    await state.update_data(promo_order_id=order_id, promo_msg_id=cb.message.message_id)
    await send_screen(cb.message, "promo_ask")
    await cb.answer()


@router.message(StateFilter(PromoStates.waiting_code), F.text)
async def msg_promo(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    order_id = data.get("promo_order_id")
    order = await get_order(order_id) if order_id else None
    if not order or order[1] != message.from_user.id or order[7] != ST_NEW:
        await state.clear()
        await send_screen(message, "order_closed", reply_markup=main_menu_kb())
        return

    code = message.text.strip().upper()
    if not re.fullmatch(r"[A-Z0-9_]{3,20}", code):
        await send_screen(message, "promo_bad_format")
        return

    async with db.execute("SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", (code, message.from_user.id)) as cur:
        if await cur.fetchone():
            await send_screen(message, "promo_used")
            return
    async with db.execute("SELECT discount_tjs FROM promocodes WHERE code=? AND active=1", (code,)) as cur:
        promo = await cur.fetchone()
    if not promo:
        await send_screen(message, "promo_not_found")
        return

    # Атомарно занимаем одно использование (защита от гонки при последних штуках)
    cur = await db.execute(
        "UPDATE promocodes SET used=used+1 WHERE code=? AND active=1 AND used<quantity", (code,)
    )
    if cur.rowcount == 0:
        await db.commit()
        await send_screen(message, "promo_exhausted")
        return

    price = order[5]
    new_price = round(max(price - promo[0], 1.0), 2)   # минимальная цена заказа — 1 TJS
    real_discount = round(price - new_price, 2)
    cur = await db.execute(
        "UPDATE orders SET price_tjs=?, promo_code=?, discount_tjs=?, updated_at=? "
        "WHERE id=? AND status=? AND promo_code IS NULL",
        (new_price, code, real_discount, now_iso(), order_id, ST_NEW),
    )
    if cur.rowcount == 0:  # заказ изменился — возвращаем использование промокода
        await db.execute("UPDATE promocodes SET used=used-1 WHERE code=?", (code,))
        await db.commit()
        await state.clear()
        await send_screen(message, "promo_failed", reply_markup=main_menu_kb())
        return
    await db.execute("INSERT INTO promo_uses (code, user_id) VALUES (?, ?)", (code, message.from_user.id))
    await db.commit()
    await state.clear()

    await send_screen(message, "promo_applied", discount=fmt_price(real_discount))
    # Прежний экран оплаты удаляем и показываем новый — уже с новой ценой
    try:
        await bot.delete_message(message.chat.id, data["promo_msg_id"])
    except TelegramAPIError:
        pass
    await send_payment_screen(bot, message.chat.id, order_id)


@router.message(StateFilter(PromoStates.waiting_code))
async def msg_promo_invalid(message: Message):
    await send_screen(message, "promo_not_text")


# ============================================================
# АВТООТМЕНА НЕОПЛАЧЕННЫХ ЗАКАЗОВ
# ============================================================
async def expire_unpaid_orders(bot: Bot) -> int:
    """Отменяет заказы в статусе «ожидает оплаты» старше UNPAID_TTL_HOURS и уведомляет клиентов."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=UNPAID_TTL_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    async with db.execute(
        "SELECT id, user_id FROM orders WHERE status=? AND created_at<=?", (ST_NEW, cutoff)
    ) as cur:
        rows = await cur.fetchall()
    cancelled = 0
    for order_id, user_id in rows:
        if await set_status(order_id, ST_CANCELLED, ST_NEW):   # если клиент успел оплатить — статус уже другой
            await release_promo(order_id)
            cancelled += 1
            await notify_user(bot, user_id, "order_expired", order_id=str(order_id), hours=f"{UNPAID_TTL_HOURS:g}")
    if cancelled:
        log.info("Автоотмена неоплаченных заказов: %s", cancelled)
    return cancelled


async def expire_loop(bot: Bot) -> None:
    while True:
        try:
            await expire_unpaid_orders(bot)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка автоотмены заказов")
        await asyncio.sleep(600)  # проверяем раз в 10 минут


# ============================================================
# АДМИН-ПАНЕЛЬ: кнопки под заказами (работает только в ADMIN_CHAT_ID)
# ============================================================
def is_admin_chat(chat_id: int) -> bool:
    return chat_id == ADMIN_CHAT_ID


async def notify_user(bot: Bot, user_id: int, keys, **fmt) -> bool:
    """Пишет клиенту по ключу текста (с картинкой, если есть). Заодно прикрепляет главное меню —
    клавиатура появится даже у тех, кто пользовался ботом до перехода на Reply-кнопки."""
    try:
        await send_screen_to(bot, user_id, keys, reply_markup=main_menu_kb(), **fmt)
        return True
    except TelegramAPIError as e:
        log.warning("Не удалось написать пользователю %s: %s", user_id, e)
        return False


@router.callback_query(F.data.startswith("adm_ok:"))
async def adm_ok(cb: CallbackQuery, bot: Bot):
    if not is_admin_chat(cb.message.chat.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    order_id = int(cb.data.split(":", 1)[1])
    order = await get_order(order_id)
    if not order:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    if not await set_status(order_id, ST_DONE, ST_PENDING):
        await cb.answer("Заказ уже обработан", show_alert=True)
        return

    sent = await notify_user(bot, order[1], "order_done", order_id=str(order_id))
    mark = "" if sent else "\n⚠️ Клиенту не удалось доставить уведомление."
    new_caption = (cb.message.html_text or "") + f"\n\n✅ <b>ВЫПОЛНЕНО</b> — {esc(cb.from_user.full_name)}{mark}"
    try:
        await cb.message.edit_caption(caption=new_caption, reply_markup=None)
    except TelegramAPIError as e:
        log.warning("edit_caption: %s", e)
    await cb.answer("Заказ выполнен")


@router.callback_query(F.data.startswith("adm_no:"))
async def adm_no(cb: CallbackQuery, state: FSMContext):
    if not is_admin_chat(cb.message.chat.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    order_id = int(cb.data.split(":", 1)[1])
    order = await get_order(order_id)
    if not order:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    if order[7] != ST_PENDING:
        await cb.answer("Заказ уже обработан", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_reject_reason)
    await state.update_data(order_id=order_id, admin_msg_id=cb.message.message_id)
    await cb.message.answer(
        f"✍️ {esc(cb.from_user.full_name)}, напишите причину отказа по заказу №{order_id} "
        f"(ответом на это сообщение). Отмена — /cancel."
    )
    await cb.answer()


@router.message(StateFilter(AdminStates.waiting_reject_reason), F.text)
async def adm_reject_reason(message: Message, state: FSMContext, bot: Bot):
    if not is_admin_chat(message.chat.id):
        return
    if message.text.startswith("/"):  # /cancel обрабатывается отдельным хендлером
        return
    data = await state.get_data()
    order_id = data["order_id"]
    reason = message.text.strip()

    order = await get_order(order_id)
    if not order or not await set_status(order_id, ST_REJECTED, ST_PENDING, reason):
        await state.clear()
        await message.answer("Заказ уже обработан.")
        return

    sent = await notify_user(bot, order[1], "order_rejected", order_id=str(order_id),
                             reason=esc(reason), support=esc(SUPPORT_USERNAME))
    try:
        await bot.edit_message_reply_markup(ADMIN_CHAT_ID, data["admin_msg_id"], reply_markup=None)
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"❌ Заказ №{order_id} отклонён. Причина: {esc(reason)}"
            + ("" if sent else "\n⚠️ Клиенту не удалось доставить уведомление."),
            reply_to_message_id=data["admin_msg_id"],
        )
    except TelegramAPIError as e:
        log.warning("Обновление админ-сообщения: %s", e)
    await state.clear()


@router.message(StateFilter(AdminStates.waiting_reject_reason))
async def adm_reject_reason_invalid(message: Message):
    if is_admin_chat(message.chat.id):
        await message.answer("⚠️ Причину нужно написать текстом.")


# ============================================================
# АДМИН-КОМАНДЫ (только ADMIN_CHAT_ID)
# ============================================================
async def answer_chunked(message: Message, lines: list[str], footer: str = "") -> None:
    """Отправляет длинный список частями (лимит Telegram — 4096 символов)."""
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) > 3800:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk or footer:
        await message.answer(chunk + footer)


# ---------- Подкатегории ----------
@admin_router.message(Command("addsubcat"))
async def adm_addsubcat(message: Message, command: CommandObject):
    parts = (command.args or "").split()
    if len(parts) < 3:
        await message.answer(
            "Формат: /addsubcat ИГРА КЛЮЧ Название\n"
            "Пример: /addsubcat ff vouchers 🎟 Ваучеры\n"
            f"Игры: {esc(', '.join(GAMES))}\n"
            "КЛЮЧ — латиницей без пробелов (2–20 символов). Название увидят клиенты на кнопке."
        )
        return
    game_key, subcat_key = parts[0].lower(), parts[1].lower()
    title = " ".join(parts[2:]).strip()
    if game_key not in GAMES:
        await message.answer(f"⚠️ Неизвестная игра «{esc(game_key)}». Игры: {esc(', '.join(GAMES))}")
        return
    if not re.fullmatch(r"[a-z0-9_]{2,20}", subcat_key):
        await message.answer("⚠️ КЛЮЧ: 2–20 символов, только маленькая латиница, цифры и «_».")
        return
    reserved = set(GAME_BUTTONS) | {tx("btn_back"), tx("btn_home"), tx("btn_games"), tx("btn_instruction"),
                                    tx("btn_support"), tx("btn_orders")}
    if not (1 <= len(title) <= 30) or title in reserved:
        await message.answer("⚠️ Название: до 30 символов и не совпадает с названиями кнопок меню или игр.")
        return
    async with db.execute("SELECT 1 FROM subcategories WHERE game_key=? AND title=?", (game_key, title)) as cur:
        if await cur.fetchone():
            await message.answer("⚠️ В этой игре уже есть подкатегория с таким названием.")
            return
    try:
        await db.execute(
            """INSERT INTO subcategories (game_key, subcat_key, title, sort_order)
               VALUES (?, ?, ?, (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM subcategories WHERE game_key=?))""",
            (game_key, subcat_key, title, game_key),
        )
        await db.commit()
    except sqlite3.IntegrityError:
        await message.answer(f"⚠️ Подкатегория с ключом «{esc(subcat_key)}» в этой игре уже есть. Список: /subcats {game_key}")
        return
    await message.answer(
        f"✅ Подкатегория <b>{esc(title)}</b> добавлена в «{esc(GAMES[game_key][0])}» (ключ <code>{subcat_key}</code>).\n"
        f"Клиенты увидят её, когда в ней появится хотя бы один товар:\n"
        f"/addproduct {game_key} {subcat_key} Название Цена"
    )


@admin_router.message(Command("subcats"))
async def adm_subcats(message: Message, command: CommandObject):
    game = (command.args or "").strip().lower()
    sql = """SELECT s.game_key, s.subcat_key, s.title, s.active,
                    (SELECT COUNT(*) FROM products p WHERE p.game_key=s.game_key
                       AND p.subcategory_key=s.subcat_key AND p.active=1)
             FROM subcategories s"""
    params: tuple = ()
    if game:
        sql += " WHERE s.game_key=?"
        params = (game,)
    sql += " ORDER BY s.game_key, s.sort_order, s.title"
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    if not rows:
        await message.answer("Подкатегорий не найдено.")
        return
    lines = []
    for game_key, subcat_key, title, active, cnt in rows:
        note = "" if not active else (" — ⚠️ нет товаров, клиентам не видна" if cnt == 0 else "")
        lines.append(f"{'🟢' if active else '⚫'} {esc(game_key)} / <code>{esc(subcat_key)}</code> — {esc(title)} "
                     f"(товаров: {cnt}){note}")
    await answer_chunked(message, lines, "\n🟢 — показана, ⚫ — скрыта. Скрыть/показать: /togglesubcat ИГРА КЛЮЧ")


@admin_router.message(Command("togglesubcat"))
async def adm_togglesubcat(message: Message, command: CommandObject):
    parts = (command.args or "").lower().split()
    if len(parts) != 2:
        await message.answer("Формат: /togglesubcat ИГРА КЛЮЧ\nПример: /togglesubcat ff vouchers")
        return
    async with db.execute(
        "SELECT title, active FROM subcategories WHERE game_key=? AND subcat_key=?", (parts[0], parts[1])
    ) as cur:
        row = await cur.fetchone()
    if not row:
        await message.answer("⚠️ Подкатегория не найдена. Список: /subcats")
        return
    new_value = 0 if row[1] else 1
    await db.execute("UPDATE subcategories SET active=? WHERE game_key=? AND subcat_key=?", (new_value, parts[0], parts[1]))
    await db.commit()
    await message.answer(f"Подкатегория «{esc(row[0])}» теперь {'🟢 показана' if new_value else '⚫ скрыта'}.")


# ---------- Товары ----------
@admin_router.message(Command("products"))
async def adm_products(message: Message, command: CommandObject):
    key = (command.args or "").strip().lower()
    sql = "SELECT id, game_key, subcategory_key, title, price_tjs, active FROM products"
    params: tuple = ()
    if key:
        sql += " WHERE game_key=?"
        params = (key,)
    sql += " ORDER BY game_key, subcategory_key, price_tjs"
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    if not rows:
        await message.answer("Товаров не найдено.")
        return
    manual = await manual_product_ids()
    lines = [
        f"{'🟢' if active else '⚫'} <b>{pid}</b> | {esc(game)}/{esc(sub or '—')} | {esc(title)} — {fmt_price(price)} TJS"
        + (" 📞" if pid in manual else "")
        for pid, game, sub, title, price, active in rows
    ]
    await answer_chunked(message, lines, "\n🟢 — показан, ⚫ — скрыт, 📞 — ручной режим")


@admin_router.message(Command("addproduct"))
async def adm_addproduct(message: Message, command: CommandObject):
    parts = (command.args or "").split()
    if len(parts) < 4:
        await message.answer(
            "Формат: /addproduct ИГРА ПОДКАТЕГОРИЯ Название ЦЕНА\n"
            "Пример: /addproduct pubg uc 3850 UC 480\n"
            f"Игры: {esc(', '.join(GAMES))}\n"
            "Ключи подкатегорий: /subcats"
        )
        return
    game_key, subcat_key = parts[0].lower(), parts[1].lower()
    price = parse_price(parts[-1])
    title = " ".join(parts[2:-1]).strip()
    if game_key not in GAMES:
        await message.answer(f"⚠️ Неизвестная игра «{esc(game_key)}». Игры: {esc(', '.join(GAMES))}")
        return
    async with db.execute(
        "SELECT 1 FROM subcategories WHERE game_key=? AND subcat_key=?", (game_key, subcat_key)
    ) as cur:
        if not await cur.fetchone():
            await message.answer(f"⚠️ Подкатегория «{esc(subcat_key)}» в этой игре не найдена. Список: /subcats {game_key}\n"
                                 f"Создать: /addsubcat {game_key} {esc(subcat_key)} Название")
            return
    if price is None:
        await message.answer("⚠️ Цена должна быть положительным числом (последний аргумент).")
        return
    if not title or len(title) > 60:
        await message.answer("⚠️ Название не должно быть пустым или длиннее 60 символов.")
        return
    cur = await db.execute(
        "INSERT INTO products (game_key, subcategory_key, title, price_tjs, active) VALUES (?, ?, ?, ?, 1)",
        (game_key, subcat_key, title, price),
    )
    await db.commit()
    await message.answer(
        f"✅ Добавлено: <b>{esc(title)}</b> ({esc(GAMES[game_key][0])} › {esc(await get_subcat_title(game_key, subcat_key))}) "
        f"— {fmt_price(price)} TJS\nID товара: <b>{cur.lastrowid}</b>"
    )


@admin_router.message(Command("setprice"))
async def adm_setprice(message: Message, command: CommandObject):
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].isdigit():
        await message.answer("Формат: /setprice ID НОВАЯ_ЦЕНА\nПример: /setprice 2 55")
        return
    price = parse_price(parts[1])
    if price is None:
        await message.answer("⚠️ Цена должна быть положительным числом.")
        return
    cur = await db.execute("UPDATE products SET price_tjs=? WHERE id=?", (price, int(parts[0])))
    await db.commit()
    if cur.rowcount == 0:
        await message.answer("⚠️ Товар с таким ID не найден. Посмотрите список: /products")
        return
    await message.answer(f"✅ Цена товара {parts[0]} изменена на <b>{fmt_price(price)} TJS</b>.")


@admin_router.message(Command("toggleproduct"))
async def adm_toggleproduct(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /toggleproduct ID\nПример: /toggleproduct 5")
        return
    pid = int(arg)
    async with db.execute("SELECT title, active FROM products WHERE id=?", (pid,)) as cur:
        row = await cur.fetchone()
    if not row:
        await message.answer("⚠️ Товар с таким ID не найден. Посмотрите список: /products")
        return
    title, active = row
    new_value = 0 if active else 1
    await db.execute("UPDATE products SET active=? WHERE id=?", (new_value, pid))
    await db.commit()
    state = "🟢 показан клиентам" if new_value else "⚫ скрыт от клиентов"
    await message.answer(f"Товар {pid} «{esc(title)}» теперь {state}.")


@admin_router.message(Command("setsubcat"))
async def adm_setsubcat(message: Message, command: CommandObject):
    parts = (command.args or "").lower().split()
    if len(parts) != 2 or not parts[0].isdigit():
        await message.answer("Формат: /setsubcat ID_ТОВАРА КЛЮЧ_ПОДКАТЕГОРИИ\nПример: /setsubcat 12 royale_pass")
        return
    async with db.execute("SELECT game_key, title FROM products WHERE id=?", (int(parts[0]),)) as cur:
        product = await cur.fetchone()
    if not product:
        await message.answer("⚠️ Товар с таким ID не найден. Посмотрите список: /products")
        return
    async with db.execute(
        "SELECT title FROM subcategories WHERE game_key=? AND subcat_key=?", (product[0], parts[1])
    ) as cur:
        sub = await cur.fetchone()
    if not sub:
        await message.answer(f"⚠️ У игры «{esc(product[0])}» нет подкатегории «{esc(parts[1])}». Список: /subcats {product[0]}")
        return
    await db.execute("UPDATE products SET subcategory_key=? WHERE id=?", (parts[1], int(parts[0])))
    await db.commit()
    await message.answer(f"✅ Товар «{esc(product[1])}» перенесён в «{esc(sub[0])}».")


# ---------- Ручной режим ----------
@admin_router.message(Command("manual"))
async def adm_manual(message: Message, command: CommandObject):
    parts = (command.args or "").lower().split()
    if not parts:
        lines = ["🔧 <b>Ручной режим</b>\n"]
        for key, (title, emoji) in GAMES.items():
            flag = await is_manual_game(key)
            lines.append(f"{emoji} {esc(title)} (<code>{key}</code>): {'🔧 ручной' if flag else '🟢 обычный'}")
        ids = sorted(await manual_product_ids())
        if ids:
            lines.append("\n📞 Отдельные товары в ручном режиме (ID): " + ", ".join(map(str, ids)))
        lines.append(
            "\n<b>Команды:</b>\n"
            "/manual roblox — переключить игру\n"
            "/manual 5 — переключить один товар (ID из /products)\n"
            "/manual roblox on — включить, /manual roblox off — выключить"
        )
        await message.answer("\n".join(lines))
        return

    target = parts[0]
    explicit = parts[1] if len(parts) > 1 else None
    if len(parts) > 2 or explicit not in (None, "on", "off", "вкл", "выкл"):
        await message.answer("Формат: /manual КЛЮЧ_ИГРЫ_или_ID_ТОВАРА [on|off]")
        return

    if target.isdigit():
        async with db.execute("SELECT title FROM products WHERE id=?", (int(target),)) as cur:
            row = await cur.fetchone()
        if not row:
            await message.answer("⚠️ Товар с таким ID не найден. Посмотрите список: /products")
            return
        setting_key, name = f"manual:product:{int(target)}", f"товар «{esc(row[0])}»"
    elif target in GAMES:
        setting_key, name = f"manual:game:{target}", f"игра «{esc(GAMES[target][0])}»"
    else:
        await message.answer(f"⚠️ Неизвестный ключ «{esc(target)}». Игры: {esc(', '.join(GAMES))}")
        return

    current = await get_setting(setting_key) == "1"
    new_value = (not current) if explicit is None else explicit in ("on", "вкл")
    await set_setting(setting_key, "1" if new_value else "0")
    if new_value:
        await message.answer(f"🔧 Ручной режим <b>включён</b>: {name}. Клиенты увидят сообщение про поддержку.")
    else:
        await message.answer(f"🟢 Ручной режим <b>выключен</b>: {name}. Всё работает как обычно.")


# ---------- Тексты и картинки ----------
@admin_router.message(Command("reloadtexts"))
async def adm_reloadtexts(message: Message):
    problems = load_texts()
    text = f"✅ Тексты перечитаны из {esc(os.path.basename(TEXTS_FILE))} (всего ключей: {len(_texts)})."
    if problems:
        text += "\n\n⚠️ Замечания:\n" + "\n".join("• " + esc(p) for p in problems[:20])
    await message.answer(text)


@admin_router.message(Command("texts"))
async def adm_texts(message: Message):
    lines = ["<b>Обложки игр</b> (images/ключ.jpg или /setimage ключ):"]
    for key, (title, emoji) in GAMES.items():
        lines.append(f"• <code>{key}</code> — {emoji} {esc(title)}{' 🖼' if find_image([key]) else ' (нет картинки)'}")
    lines.append("\n<b>Тексты</b> (файл texts.txt):")
    ordered = [k for k, _, _ in DEFAULTS] + sorted(k for k in _texts if k not in DEFAULT_TEXTS)
    lines += [
        f"• <code>{k}</code>{' 🖼' if find_image([k]) else ''}"
        for k in ordered if not k.startswith(("btn_", "placeholder_", "status_", "hint_", "uid_help"))
    ]
    await answer_chunked(
        message, lines,
        f"\n🖼 — картинка подвязана.\nФайл: {esc(os.path.basename(TEXTS_FILE))}. "
        "После правок — /reloadtexts. Картинку добавить: фото с подписью /setimage КЛЮЧ",
    )


@admin_router.message(Command("setimage"))
async def adm_setimage(message: Message, command: CommandObject, bot: Bot):
    key = (command.args or "").strip().lower()
    usage = ("Формат: отправьте фото с подписью <code>/setimage КЛЮЧ</code> "
             "или ответьте этой командой на фото.\nПример: /setimage ff — обложка Free Fire.\n"
             f"Ключи игр: {esc(', '.join(GAMES))}. Ключи сообщений — в /texts.")
    if not re.fullmatch(r"[a-z0-9_]{2,40}", key):
        await message.answer(usage)
        return
    source = message if (message.photo or message.document) else message.reply_to_message
    if source is None or not (source.photo or source.document):
        await message.answer(usage)
        return

    if source.photo:
        file_obj, ext = source.photo[-1], ".jpg"
    else:
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(source.document.mime_type or "")
        if not ext:
            await message.answer("⚠️ Нужна картинка в формате JPG, PNG или WEBP.")
            return
        file_obj = source.document

    try:
        os.makedirs(IMAGES_DIR, exist_ok=True)
        for old_ext in IMAGE_EXTS:  # убираем прежние варианты, чтобы не было конфликта
            old = os.path.join(IMAGES_DIR, key + old_ext)
            if os.path.isfile(old):
                os.remove(old)
        await bot.download(file_obj, destination=os.path.join(IMAGES_DIR, key + ext))
    except (TelegramAPIError, OSError) as e:
        await message.answer(f"⚠️ Не удалось сохранить картинку: {esc(str(e))}")
        return
    note = "" if (key in _texts or key in GAMES) else "\n⚠️ Такого ключа нет среди игр и сообщений (/texts) — проверьте название."
    await message.answer(f"🖼 Картинка для «<code>{key}</code>» сохранена. Уже работает, перезапуск не нужен.{note}")


@admin_router.message(Command("delimage"))
async def adm_delimage(message: Message, command: CommandObject):
    key = (command.args or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{2,40}", key):
        await message.answer("Формат: /delimage КЛЮЧ")
        return
    removed = False
    for ext in IMAGE_EXTS:
        path = os.path.join(IMAGES_DIR, key + ext)
        if os.path.isfile(path):
            try:
                os.remove(path)
                removed = True
            except OSError as e:
                await message.answer(f"⚠️ Не удалось удалить файл: {esc(str(e))}")
                return
    await message.answer(f"🗑 Картинка «<code>{key}</code>» удалена." if removed else "Для этого ключа картинки нет.")


# ---------- Очередь заказов ----------
def minutes_ago(iso: str | None) -> int:
    try:
        then = datetime.strptime(iso, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return 0
    return max(0, int((datetime.now(timezone.utc) - then).total_seconds() // 60))


@admin_router.message(Command("pending"))
async def adm_pending(message: Message):
    async with db.execute(
        """SELECT o.id, o.price_tjs, o.game_key, o.product_title, o.updated_at, u.username, u.full_name
           FROM orders o LEFT JOIN users u ON u.user_id = o.user_id
           WHERE o.status=? ORDER BY o.id""",
        (ST_PENDING,),
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        await message.answer("✅ Необработанных заказов нет.")
        return
    lines = [f"⏳ <b>Ждут проверки: {len(rows)}</b>\n"]
    for oid, price, game_key, title, updated, username, full_name in rows:
        who = f"@{username}" if username else (full_name or "—")
        lines.append(
            f"№{oid} | {esc(GAMES.get(game_key, (game_key,))[0])} {esc(title)} | {fmt_price(price)} TJS | "
            f"{esc(who)} | ждёт {minutes_ago(updated)} мин"
        )
    await answer_chunked(message, lines, "\nОткрыть заказ с кнопками: /order НОМЕР")


@admin_router.message(Command("order"))
async def adm_order(message: Message, command: CommandObject, bot: Bot):
    arg = (command.args or "").strip().lstrip("№#")
    if not arg.isdigit():
        await message.answer("Формат: /order НОМЕР\nПример: /order 15")
        return
    order = await get_order(int(arg))
    if not order:
        await message.answer("⚠️ Заказ не найден.")
        return
    order_id, status, file_id = order[0], order[7], order[8]
    caption = await admin_caption(order_id, "📋 Заказ") + f"\n\nСтатус: {status_text(status)}"
    if order[9]:
        caption += f"\nПричина отказа: {esc(order[9])}"
    markup = admin_kb(order_id) if status == ST_PENDING else None
    if file_id:
        try:
            await send_receipt(bot, ADMIN_CHAT_ID, file_id, caption, markup)
            return
        except TelegramAPIError as e:
            log.warning("Не удалось показать чек заказа %s: %s", order_id, e)
    await message.answer(caption, reply_markup=markup)


# ---------- Очистка тестовых заказов ----------
@admin_router.message(Command("clearorders"))
async def adm_clearorders(message: Message):
    async with db.execute(
        "SELECT COUNT(*), COALESCE(SUM(status='pending'), 0), COALESCE(SUM(status='done'), 0) FROM orders"
    ) as cur:
        total, pending, done = await cur.fetchone()
    if total == 0:
        await message.answer("Таблица заказов уже пуста.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"🗑 Да, удалить {total}", callback_data="clr:yes"),
        InlineKeyboardButton(text="Отмена", callback_data="clr:no"),
    ]])
    await message.answer(
        f"⚠️ <b>Очистка истории заказов</b>\n\n"
        f"Будут удалены ВСЕ заказы: {total} (выполненных: {done}, ждут проверки: {pending}).\n"
        f"Каталог, подкатегории, пользователи и промокоды <b>не затрагиваются</b>. "
        f"Нумерация заказов начнётся с №1, статистика /stats обнулится.\n"
        f"Перед удалением бот пришлёт копию базы данных.",
        reply_markup=kb,
    )


@router.callback_query(F.data == "clr:no")
async def cb_clear_no(cb: CallbackQuery):
    if not is_admin_chat(cb.message.chat.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await cb.message.edit_text("Очистка отменена.", reply_markup=None)
    await cb.answer()


@router.callback_query(F.data == "clr:yes")
async def cb_clear_yes(cb: CallbackQuery, bot: Bot):
    if not is_admin_chat(cb.message.chat.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await cb.message.edit_reply_markup(reply_markup=None)   # защита от двойного нажатия
    error = await send_db_backup(bot, "💾 Копия перед очисткой заказов")
    if error:
        await cb.message.edit_text(f"⚠️ Не удалось сделать копию базы, очистка отменена: {esc(error)}")
        await cb.answer()
        return
    cur = await db.execute("DELETE FROM orders")
    deleted = cur.rowcount
    await db.execute("DELETE FROM sqlite_sequence WHERE name='orders'")  # нумерация с №1
    await db.commit()
    await cb.message.edit_text(f"✅ Удалено заказов: {deleted}. Каталог, пользователи и промокоды сохранены.")
    await cb.answer()


# ---------- Статистика, рассылка, техработы ----------
@admin_router.message(Command("stats"))
async def adm_stats(message: Message):
    # Начало сегодняшних суток по местному времени (UTC+5), переведённое в UTC для сравнения с БД
    start_local = datetime.now(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    async with db.execute(
        "SELECT COUNT(*), COALESCE(SUM(price_tjs), 0) FROM orders WHERE status=?", (ST_DONE,)
    ) as cur:
        total_cnt, total_sum = await cur.fetchone()
    # «Сегодня» считаем по времени выполнения заказа (updated_at)
    async with db.execute(
        "SELECT COUNT(*), COALESCE(SUM(price_tjs), 0) FROM orders WHERE status=? AND updated_at>=?",
        (ST_DONE, start_utc),
    ) as cur:
        today_cnt, today_sum = await cur.fetchone()
    async with db.execute("SELECT COUNT(*) FROM orders WHERE status=?", (ST_PENDING,)) as cur:
        (pending,) = await cur.fetchone()
    async with db.execute("SELECT COUNT(*) FROM users") as cur:
        (users,) = await cur.fetchone()

    await message.answer(
        "📊 <b>Статистика</b>\n\n"
        f"<b>Сегодня:</b> {today_cnt} заказ(ов) на {fmt_price(today_sum)} TJS\n"
        f"<b>За всё время:</b> {total_cnt} заказ(ов) на {fmt_price(total_sum)} TJS\n\n"
        f"⏳ Ждут проверки: {pending}\n"
        f"👥 Пользователей в базе: {users}"
    )


@admin_router.message(Command("broadcast"))
async def adm_broadcast(message: Message, command: CommandObject, bot: Bot):
    text = (command.args or "").strip()
    if not text:
        await message.answer("Формат: /broadcast ТЕКСТ\nПример: /broadcast Скидка 10% на все пакеты PUBG до пятницы!")
        return

    async with db.execute("SELECT user_id FROM users") as cur:
        user_ids = [row[0] for row in await cur.fetchall()]
    await message.answer(f"📣 Рассылка запущена, получателей: {len(user_ids)}…")

    ok = fail = 0
    for uid in user_ids:
        try:
            # parse_mode=None: текст уходит как есть, символы < > не сломают рассылку
            await bot.send_message(uid, text, parse_mode=None)
            ok += 1
        except TelegramRetryAfter as e:      # Telegram просит притормозить
            await asyncio.sleep(e.retry_after)
            try:
                await bot.send_message(uid, text, parse_mode=None)
                ok += 1
            except TelegramAPIError:
                fail += 1
        except TelegramAPIError:             # пользователь заблокировал бота и т.п.
            fail += 1
        await asyncio.sleep(0.05)            # ~20 сообщений в секунду, в пределах лимитов

    await message.answer(f"✅ Рассылка завершена.\nДоставлено: {ok}\nНе доставлено: {fail}")


@admin_router.message(Command("maintenance"))
async def adm_maintenance(message: Message):
    new_value = "0" if await get_setting("maintenance") == "1" else "1"
    await set_setting("maintenance", new_value)
    if new_value == "1":
        await message.answer("🛠 Технические работы <b>ВКЛЮЧЕНЫ</b>. Клиенты видят сообщение о работах.\n"
                             "Админ-чат работает как обычно. Выключить: /maintenance")
    else:
        await message.answer("✅ Технические работы <b>ВЫКЛЮЧЕНЫ</b>. Бот работает в обычном режиме.")


# ---------- Промокоды ----------
@admin_router.message(Command("promocode"))
async def adm_promocode(message: Message, command: CommandObject):
    parts = (command.args or "").split()
    if len(parts) != 3:
        await message.answer(
            "Формат: /promocode НАЗВАНИЕ СКИДКА_TJS КОЛ_ВО\n"
            "Пример: /promocode SALE10 5 100 — скидка 5 TJS, на 100 использований"
        )
        return
    code = parts[0].upper()
    discount = parse_price(parts[1])
    if not re.fullmatch(r"[A-Z0-9_]{3,20}", code):
        await message.answer("⚠️ Название: 3–20 символов, только латиница, цифры и «_».")
        return
    if discount is None:
        await message.answer("⚠️ Скидка должна быть положительным числом.")
        return
    if not parts[2].isdigit() or not (0 < int(parts[2]) <= 1_000_000):
        await message.answer("⚠️ Количество использований — целое число от 1.")
        return
    try:
        await db.execute(
            "INSERT INTO promocodes (code, discount_tjs, quantity, created_at) VALUES (?, ?, ?, ?)",
            (code, discount, int(parts[2]), now_iso()),
        )
        await db.commit()
    except sqlite3.IntegrityError:
        await message.answer(f"⚠️ Промокод «{esc(code)}» уже существует.")
        return
    await message.answer(
        f"✅ Промокод <code>{esc(code)}</code> создан: скидка <b>{fmt_price(discount)} TJS</b>, "
        f"использований: {parts[2]} (по одному на клиента)."
    )


# ---------- Бэкап ----------
async def send_db_backup(bot: Bot, caption_prefix: str = "💾 Копия базы данных") -> str | None:
    """Отправляет копию shop.db в админ-чат. Возвращает текст ошибки или None, если всё хорошо."""
    stamp = datetime.now(LOCAL_TZ).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(tempfile.gettempdir(), f"shop_backup_{stamp}.db")
    try:
        await db.commit()
        # VACUUM INTO делает цельную согласованную копию, даже пока бот работает
        await db.execute(f"VACUUM INTO '{path.replace(chr(39), chr(39) * 2)}'")
        if os.path.getsize(path) > 49 * 1024 * 1024:
            return "Копия больше 50 МБ, Telegram не принимает такие файлы. Скопируйте shop.db вручную."
        await bot.send_document(
            ADMIN_CHAT_ID,
            FSInputFile(path, filename=f"shop_{stamp}.db"),
            caption=f"{caption_prefix} на {datetime.now(LOCAL_TZ):%d.%m.%Y %H:%M}",
        )
        return None
    except Exception as e:  # noqa: BLE001
        log.exception("Ошибка бэкапа")
        return str(e)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@admin_router.message(Command("dbbackup"))
async def adm_dbbackup(message: Message, bot: Bot):
    error = await send_db_backup(bot)
    if error:
        await message.answer(f"⚠️ Не удалось создать копию: {esc(error)}")


async def auto_backup_loop(bot: Bot, hours: float) -> None:
    """Необязательная автокопия: включается переменной AUTO_BACKUP_HOURS в .env (0 — выключено)."""
    while True:
        await asyncio.sleep(hours * 3600)
        error = await send_db_backup(bot, "🕑 Автокопия базы данных")
        if error:
            log.warning("Автокопия не удалась: %s", error)


# ---------- Пользователи ----------
@admin_router.message(Command("checkuser"))
async def adm_checkuser(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /checkuser USER_ID\nПример: /checkuser 123456789")
        return
    uid = int(arg)
    async with db.execute(
        "SELECT username, full_name, banned, created_at FROM users WHERE user_id=?", (uid,)
    ) as cur:
        user = await cur.fetchone()
    if not user:
        await message.answer("⚠️ Пользователь не найден в базе (он ещё не запускал бота).")
        return
    username, full_name, banned, created_at = user

    async with db.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (uid,)) as cur:
        (total,) = await cur.fetchone()
    async with db.execute(
        "SELECT COUNT(*), COALESCE(SUM(price_tjs), 0) FROM orders WHERE user_id=? AND status=?", (uid, ST_DONE)
    ) as cur:
        done_cnt, done_sum = await cur.fetchone()

    await message.answer(
        f"👤 <b>{esc(full_name)}</b> ({esc('@' + username) if username else '—'})\n"
        f"🆔 <code>{uid}</code>\n"
        f"📅 В базе с: {esc(created_at)} (UTC)\n\n"
        f"📦 Всего заказов: <b>{total}</b>\n"
        f"✅ Выполнено: <b>{done_cnt}</b> на сумму <b>{fmt_price(done_sum)} TJS</b>\n"
        f"Статус: {'🚫 забанен' if banned else '🟢 активен'}"
    )


async def _set_ban(message: Message, command: CommandObject, value: int):
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer(f"Формат: /{'ban' if value else 'unban'} USER_ID")
        return
    cur = await db.execute("UPDATE users SET banned=? WHERE user_id=?", (value, int(arg)))
    await db.commit()
    if cur.rowcount == 0:
        await message.answer("⚠️ Пользователь не найден в базе.")
        return
    await message.answer(f"{'🚫 Пользователь забанен' if value else '🟢 Пользователь разблокирован'}: <code>{arg}</code>")


@admin_router.message(Command("ban"))
async def adm_ban(message: Message, command: CommandObject):
    await _set_ban(message, command, 1)


@admin_router.message(Command("unban"))
async def adm_unban(message: Message, command: CommandObject):
    await _set_ban(message, command, 0)


# ============================================================
# ЗАПУСК
# ============================================================
async def setup_bot_commands(bot: Bot) -> None:
    """Меню «/»: обычным пользователям — только /start, в админ-чате — полный список админ-команд."""
    try:
        await bot.set_my_commands(
            [BotCommand(command="start", description="Главное меню")], scope=BotCommandScopeDefault()
        )
        await bot.set_my_commands(
            [
                BotCommand(command="pending", description="Заказы, ожидающие проверки"),
                BotCommand(command="order", description="Показать заказ: номер"),
                BotCommand(command="products", description="Список товаров и их ID"),
                BotCommand(command="subcats", description="Список подкатегорий"),
                BotCommand(command="addsubcat", description="Добавить подкатегорию: игра ключ название"),
                BotCommand(command="addproduct", description="Добавить товар: игра подкатегория название цена"),
                BotCommand(command="setprice", description="Изменить цену: ID цена"),
                BotCommand(command="toggleproduct", description="Скрыть/показать товар: ID"),
                BotCommand(command="togglesubcat", description="Скрыть/показать подкатегорию"),
                BotCommand(command="setsubcat", description="Перенести товар в подкатегорию"),
                BotCommand(command="manual", description="Ручной режим игры/товара"),
                BotCommand(command="stats", description="Статистика заказов и выручки"),
                BotCommand(command="promocode", description="Создать промокод: имя скидка кол-во"),
                BotCommand(command="texts", description="Ключи текстов и картинок"),
                BotCommand(command="reloadtexts", description="Перечитать texts.txt"),
                BotCommand(command="setimage", description="Картинка: ключ (игры: pubg, ff...)"),
                BotCommand(command="delimage", description="Убрать картинку: ключ"),
                BotCommand(command="broadcast", description="Рассылка всем клиентам"),
                BotCommand(command="maintenance", description="Вкл/выкл технические работы"),
                BotCommand(command="dbbackup", description="Скачать копию базы данных"),
                BotCommand(command="clearorders", description="Очистить историю заказов"),
                BotCommand(command="checkuser", description="Данные пользователя: user_id"),
                BotCommand(command="ban", description="Заблокировать пользователя: user_id"),
                BotCommand(command="unban", description="Разблокировать пользователя: user_id"),
                BotCommand(command="cancel", description="Отменить текущее действие"),
            ],
            scope=BotCommandScopeChat(chat_id=ADMIN_CHAT_ID),
        )
    except TelegramAPIError as e:
        log.warning("Не удалось установить меню команд: %s", e)


async def main() -> None:
    global db
    if not BOT_TOKEN or not ADMIN_CHAT_ID:
        raise SystemExit("Заполните BOT_TOKEN и ADMIN_CHAT_ID в файле .env")

    for problem in load_texts():
        log.warning("texts.txt: %s", problem)

    db = await aiosqlite.connect(DB_PATH)
    await init_db()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await setup_bot_commands(bot)
    tasks: list[asyncio.Task] = []
    if UNPAID_TTL_HOURS > 0:
        tasks.append(asyncio.create_task(expire_loop(bot)))
        log.info("Автоотмена неоплаченных заказов: через %s ч", UNPAID_TTL_HOURS)
    if AUTO_BACKUP_HOURS > 0:
        tasks.append(asyncio.create_task(auto_backup_loop(bot, AUTO_BACKUP_HOURS)))
        log.info("Автокопия базы включена: каждые %s ч", AUTO_BACKUP_HOURS)

    log.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        for task in tasks:
            task.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
