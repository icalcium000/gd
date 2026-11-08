import asyncio
import html
import json
import logging
import os
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
import gspread_asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputMediaPhoto,
    FSInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# =====================================================================
# 1. НАСТРОЙКИ И КОНФИГУРАЦИЯ
# =====================================================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН_БОТА")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "ВАШ_ID_ГУГЛ_ТАБЛИЦЫ")
CREDENTIALS_FILE = "service_account.json"

# Список Telegram ID руководителей клуба
ADMIN_IDS = [6343645479, 119760250]

# Часовой пояс (например: "Europe/Moscow" или "Asia/Yekaterinburg")
TIMEZONE = "Europe/Moscow"
TZ = ZoneInfo(TIMEZONE)
CHAT_CONFIG_FILE = "chat_config.json"

# 🖼 БАННЕРЫ ДЛЯ РАЗДЕЛОВ БОТА
# Сюда можно вставить:
# 1) Прямую ссылку на фото (https://...)
# 2) file_id фотографии из Telegram (самый быстрый вариант!)
# 3) Локальный файл через FSInputFile("img/main.jpg")
BANNERS = {
    "main": "https://images.unsplash.com/photo-1523240795612-9a054b0db644?w=1000&q=80",       # Главное меню
    "events": "https://images.unsplash.com/photo-1511578314322-379afb476865?w=1000&q=80",     # Список мероприятий
    "passport": "https://images.unsplash.com/photo-1540575467063-178a50c2df87?w=1000&q=80",   # Паспорт мероприятия
    "settings": "https://images.unsplash.com/photo-1454165804606-c3d57bc86b40?w=1000&q=80",   # Настройки меро
    "tasks": "https://images.unsplash.com/photo-1484480974693-6ca0a78fb36b?w=1000&q=80",      # Задачи и Мои задачи
    "status": "https://images.unsplash.com/photo-1551288049-bebda4e38f71?w=1000&q=80",        # Сводка в чате
    "alert": "https://images.unsplash.com/photo-1508739773434-c26b3d09e071?w=1000&q=80",       # Горящий дедлайн
}

logging.basicConfig(level=logging.INFO)
router = Router()


# =====================================================================
# 2. МОДУЛЬ РАБОТЫ С GOOGLE ТАБЛИЦАМИ
# =====================================================================
def get_creds():
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE)
    return creds.with_scopes([
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])


agcm = gspread_asyncio.AsyncioGspreadClientManager(get_creds)


class SheetsManager:
    def __init__(self, spreadsheet_id: str):
        self.spreadsheet_id = spreadsheet_id

    async def _get_spreadsheet(self):
        client = await agcm.authorize()
        return await client.open_by_key(self.spreadsheet_id)

    async def init_sheets(self):
        """Автоматически создает листы 'Мероприятия' и 'Задачи', если их нет."""
        ss = await self._get_spreadsheet()
        worksheets = await ss.worksheets()
        titles = [ws.title for ws in worksheets]

        if "Мероприятия" not in titles:
            ws_ev = await ss.add_worksheet(title="Мероприятия", rows=100, cols=9)
            await ws_ev.append_row([
                "ID", "Название", "Дата", "Организатор", "Миньоны",
                "Плакат", "Преза", "Доп. материалы", "Статус"
            ])

        if "Задачи" not in titles:
            ws_t = await ss.add_worksheet(title="Задачи", rows=500, cols=9)
            await ws_t.append_row([
                "ID", "ID Мероприятия", "Мероприятие", "Задача",
                "Исполнитель", "TG ID", "Дедлайн", "Статус", "Напоминание"
            ])

    # --- МЕРОПРИЯТИЯ ---
    async def get_active_events(self) -> list[dict]:
        ss = await self._get_spreadsheet()
        ws = await ss.worksheet("Мероприятия")
        records = await ws.get_all_records()
        return [r for r in records if r.get("Статус") != "Завершено"]

    async def get_event_by_id(self, event_id: str) -> dict | None:
        ss = await self._get_spreadsheet()
        ws = await ss.worksheet("Мероприятия")
        records = await ws.get_all_records()
        for r in records:
            if str(r.get("ID")) == event_id:
                return r
        return None

    async def add_event(self, title: str, date: str, organizer: str) -> str:
        ss = await self._get_spreadsheet()
        ws = await ss.worksheet("Мероприятия")
        event_id = f"ev_{uuid.uuid4().hex[:5]}"
        await ws.append_row([
            event_id, title, date, organizer, "Пока нет",
            "Нет", "Нет", "Пусто", "В работе"
        ])
        return event_id

    async def update_event_field(self, event_id: str, col_index: int, value: str) -> bool:
        """
        Индексы столбцов (1-based):
        4 - Организатор, 5 - Миньоны, 6 - Плакат, 7 - Преза, 8 - Доп. материалы, 9 - Статус
        """
        ss = await self._get_spreadsheet()
        ws = await ss.worksheet("Мероприятия")
        cell = await ws.find(event_id, in_column=1)
        if not cell:
            return False
        await ws.update_cell(cell.row, col_index, value)
        return True

    async def add_minion(self, event_id: str, username: str) -> str:
        ev = await self.get_event_by_id(event_id)
        if not ev:
            return "Мероприятие не найдено"
        current = str(ev.get("Миньоны", "")).strip()
        user_tag = f"@{username}" if not username.startswith("@") else username

        if current in ("", "Пока нет", "Нет"):
            new_val = user_tag
        elif user_tag.lower() in current.lower():
            return "Вы уже в команде миньонов!"
        else:
            new_val = f"{current}, {user_tag}"

        await self.update_event_field(event_id, 5, new_val)
        return new_val

    async def append_material(self, event_id: str, new_item: str) -> str:
        ev = await self.get_event_by_id(event_id)
        if not ev:
            return ""
        current = str(ev.get("Доп. материалы", "")).strip()
        if current in ("", "Пусто", "Нет"):
            updated = f"• {new_item}"
        else:
            updated = f"{current}\n• {new_item}"
        await self.update_event_field(event_id, 8, updated)
        return updated

    # --- ЗАДАЧИ ---
    async def get_all_active_tasks(self) -> list[dict]:
        ss = await self._get_spreadsheet()
        ws = await ss.worksheet("Задачи")
        records = await ws.get_all_records()
        return [r for r in records if r.get("Статус") not in ("Готово", "Отменена")]

    async def get_event_tasks(self, event_id: str) -> list[dict]:
        ss = await self._get_spreadsheet()
        ws = await ss.worksheet("Задачи")
        records = await ws.get_all_records()
        return [r for r in records if str(r.get("ID Мероприятия")) == event_id]

    async def get_user_tasks(self, tg_id: int, username: str | None) -> list[dict]:
        tasks = await self.get_all_active_tasks()
        user_tasks = []
        uname_clean = f"@{username.lower()}" if username else None

        for r in tasks:
            row_tg_id = str(r.get("TG ID", "")).strip()
            row_uname = str(r.get("Исполнитель", "")).strip().lower()
            if row_tg_id == str(tg_id) or (uname_clean and row_uname == uname_clean):
                user_tasks.append(r)
        return user_tasks

    async def get_task_by_id(self, task_id: str) -> dict | None:
        ss = await self._get_spreadsheet()
        ws = await ss.worksheet("Задачи")
        records = await ws.get_all_records()
        for r in records:
            if str(r.get("ID")) == task_id:
                return r
        return None

    async def add_task(
        self, event_id: str, event_title: str, task_text: str,
        deadline: str, assignee: str = "Свободно", tg_id: str = ""
    ) -> str:
        ss = await self._get_spreadsheet()
        ws = await ss.worksheet("Задачи")
        task_id = f"t_{uuid.uuid4().hex[:5]}"
        status = "Свободна" if assignee == "Свободно" else "В работе"
        await ws.append_row([
            task_id, event_id, event_title, task_text,
            assignee, tg_id, deadline, status, ""
        ])
        return task_id

    async def assign_task(self, task_id: str, username: str | None, tg_id: int) -> bool:
        ss = await self._get_spreadsheet()
        ws = await ss.worksheet("Задачи")
        cell = await ws.find(task_id, in_column=1)
        if not cell:
            return False

        display_name = f"@{username}" if username else f"ID:{tg_id}"
        await ws.update_cell(cell.row, 5, display_name)
        await ws.update_cell(cell.row, 6, str(tg_id))
        await ws.update_cell(cell.row, 8, "В работе")
        return True

    async def update_task_status(self, task_id: str, new_status: str) -> bool:
        ss = await self._get_spreadsheet()
        ws = await ss.worksheet("Задачи")
        cell = await ws.find(task_id, in_column=1)
        if not cell:
            return False
        await ws.update_cell(cell.row, 8, new_status)
        return True

    async def mark_reminder_sent(self, task_id: str, tag: str):
        ss = await self._get_spreadsheet()
        ws = await ss.worksheet("Задачи")
        cell = await ws.find(task_id, in_column=1)
        if cell:
            await ws.update_cell(cell.row, 9, tag)


sheets = SheetsManager(SPREADSHEET_ID)


# =====================================================================
# 3. УТИЛИТЫ ОФОРМЛЕНИЯ, КАРТИНОК И ФОРМАТИРОВАНИЯ
# =====================================================================
def save_chat_config(chat_id: int, thread_id: int | None):
    with open(CHAT_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"chat_id": chat_id, "thread_id": thread_id}, f)


def load_chat_config() -> dict | None:
    if not os.path.exists(CHAT_CONFIG_FILE):
        return None
    with open(CHAT_CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


async def render_screen(
    target: Message | CallbackQuery,
    text: str,
    banner_key: str = "main",
    kb=None
):
    """
    Универсальная функция отрисовки экрана с фотографией и красивой HTML-подписью.
    Если вызвана из CallbackQuery — плавно меняет фото и текст текущего сообщения.
    """
    photo = BANNERS.get(banner_key)
    markup = kb.as_markup() if hasattr(kb, "as_markup") else kb

    if isinstance(target, Message):
        if photo:
            try:
                await target.answer_photo(photo=photo, caption=text, reply_markup=markup)
                return
            except Exception as e:
                logging.warning(f"Ошибка отправки фото ({banner_key}): {e}")
        await target.answer(text=text, reply_markup=markup, disable_web_page_preview=True)

    elif isinstance(target, CallbackQuery):
        msg = target.message
        if photo and msg.photo:
            try:
                await msg.edit_media(
                    media=InputMediaPhoto(media=photo, caption=text, parse_mode=ParseMode.HTML),
                    reply_markup=markup
                )
                return
            except TelegramBadRequest as e:
                if "message is not modified" in str(e):
                    return
            except Exception as e:
                logging.warning(f"Не удалось обновить медиа: {e}")

        # Фолбэк, если сообщение было текстовым или фото не загрузилось
        try:
            if msg.photo:
                await msg.edit_caption(caption=text, reply_markup=markup)
            else:
                await msg.edit_text(text=text, reply_markup=markup, disable_web_page_preview=True)
        except TelegramBadRequest:
            pass


def format_asset_link(raw_val: str) -> str:
    """Превращает строку 'Есть (https://...)' в красивую кликабельную HTML-ссылку."""
    val = str(raw_val).strip()
    if not val or val.lower() == "нет":
        return "❌ <i>Нет</i>"
    if val.startswith("Есть (") and val.endswith(")"):
        url = val[6:-1].strip()
        if url.startswith("http"):
            return f'✅ <b>Есть</b> (<a href="{html.escape(url)}">Открыть ссылку 🔗</a>)'
    return f"✅ {html.escape(val)}"


def format_event_card(ev: dict, tasks: list[dict]) -> str:
    """Красивый HTML-паспорт мероприятия."""
    total = len(tasks)
    done = sum(1 for t in tasks if t["Статус"] == "Готово")
    free = sum(1 for t in tasks if t["Статус"] == "Свободна")
    in_work = total - done - free

    title = html.escape(str(ev["Название"]))
    ev_id = html.escape(str(ev["ID"]))
    date = html.escape(str(ev["Дата"]))
    org = html.escape(str(ev["Организатор"]))
    minions = html.escape(str(ev["Миньоны"]))
    materials = html.escape(str(ev["Доп. материалы"]))

    poster_str = format_asset_link(ev.get("Плакат", "Нет"))
    pres_str = format_asset_link(ev.get("Преза", "Нет"))

    return (
        f"🎪 <b>{title.upper()}</b>  <code>[{ev_id}]</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🗓 <b>Дата проведения:</b> {date}\n"
        f"👑 <b>Организатор:</b> {org}\n"
        f"🍌 <b>Миньоны:</b> {minions}\n\n"
        f"🎨 <b>Готовность материалов:</b>\n"
        f"├ 🖼 <b>Плакат:</b> {poster_str}\n"
        f"└ 📊 <b>Преза:</b> {pres_str}\n\n"
        f"📂 <b>Дополнительные материалы:</b>\n"
        f"<blockquote>{materials}</blockquote>\n"
        f"📋 <b>Прогресс по задачам:</b>\n"
        f"🟢 Свободно: <b>{free}</b>  |  🟡 В работе: <b>{in_work}</b>  |  ✅ Готово: <b>{done}</b>"
    )


def format_task_card(t: dict) -> str:
    """Красивая HTML-карточка отдельной задачи."""
    status_badges = {
        "Свободна": "🟢 <b>Свободна (ждет героя!)</b>",
        "В работе": "🟡 <b>В работе</b>",
        "Готово": "✅ <b>Выполнена</b>"
    }
    badge = status_badges.get(t["Статус"], html.escape(str(t["Статус"])))

    return (
        f"📌 <b>КАРТОЧКА ЗАДАЧИ</b>  <code>[{html.escape(str(t['ID']))}]</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎪 <b>Мероприятие:</b> {html.escape(str(t['Мероприятие']))}\n"
        f"📊 <b>Статус:</b> {badge}\n"
        f"👤 <b>Исполнитель:</b> {html.escape(str(t['Исполнитель']))}\n"
        f"⏳ <b>Дедлайн:</b> <code>{html.escape(str(t['Дедлайн']))}</code>\n\n"
        f"📝 <b>Что нужно сделать:</b>\n"
        f"<blockquote>{html.escape(str(t['Задача']))}</blockquote>"
    )


# =====================================================================
# 4. КЛАВИАТУРЫ И СОСТОЯНИЯ FSM
# =====================================================================
class CreateEventSG(StatesGroup):
    title = State()
    date = State()
    organizer = State()


class EditEventPropSG(StatesGroup):
    waiting_value = State()


class CreateTaskSG(StatesGroup):
    event_id = State()
    event_title = State()
    text = State()
    deadline = State()


def main_menu_kb(user_id: int) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="📅 Мероприятия"), KeyboardButton(text="🎯 Мои задачи")]
    ]
    if user_id in ADMIN_IDS:
        buttons.append([KeyboardButton(text="➕ Создать мероприятие")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def event_card_kb(event_id: str, event_title: str) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="📋 Задачи меро", callback_data=f"evtasks:{event_id}"),
        InlineKeyboardButton(text="⚙️ Настройки меро", callback_data=f"evcfg:{event_id}")
    )
    kb.row(
        InlineKeyboardButton(text="🍌 Стать миньоном", callback_data=f"joinmin:{event_id}"),
        InlineKeyboardButton(text="➕ Добавить задачу", callback_data=f"addtask:{event_id}:{event_title[:16]}")
    )
    kb.row(InlineKeyboardButton(text="🔙 Ко всем мероприятиям", callback_data="back_events"))
    return kb


def event_settings_kb(event_id: str) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🖼 Плакат (Есть/Нет)", callback_data=f"setasset:{event_id}:poster"),
        InlineKeyboardButton(text="📊 Преза (Есть/Нет)", callback_data=f"setasset:{event_id}:pres")
    )
    kb.row(
        InlineKeyboardButton(text="👑 Сменить орга", callback_data=f"editprop:{event_id}:org"),
        InlineKeyboardButton(text="🍌 Задать миньонов", callback_data=f"editprop:{event_id}:minions")
    )
    kb.row(
        InlineKeyboardButton(text="➕ Добавить материал", callback_data=f"editprop:{event_id}:mat_add"),
        InlineKeyboardButton(text="🗑 Очистить материалы", callback_data=f"clearmat:{event_id}")
    )
    kb.row(InlineKeyboardButton(text="🔙 Назад к паспорту меро", callback_data=f"ev:{event_id}"))
    return kb


# =====================================================================
# 5. ОБРАБОТЧИКИ КОМАНД И ЧАТА
# =====================================================================
@router.message(CommandStart())
async def cmd_start(message: Message):
    kb = main_menu_kb(message.from_user.id) if message.chat.type == "private" else None
    welcome_text = (
        f"✨ <b>Привет, {html.escape(message.from_user.first_name)}!</b>\n"
        f"Добро пожаловать в центр управления мероприятиями нашего студклуба.\n\n"
        f"<blockquote>Здесь мы планируем ивенты, собираем команды миньонов, храним ссылки на плакаты и презы и следим, чтобы ни один дедлайн не сгорел.</blockquote>\n\n"
        f"🧭 <b>Навигация:</b>\n"
        f"• <b>📅 Мероприятия</b> — паспорта ивентов, материалы и биржа свободных задач.\n"
        f"• <b>🎯 Мои задачи</b> — твой личный чек-лист поручений.\n"
        f"• В рабочем чате доступны: <code>/events</code>, <code>/status</code>, <code>/newtask</code> и <code>/get_file_id</code>."
    )
    await render_screen(message, welcome_text, banner_key="main", kb=kb)


@router.message(Command("get_file_id"))
async def cmd_get_file_id(message: Message):
    """Помощник: отправьте фото с подписью /get_file_id или ответьте на фото, чтобы узнать его file_id."""
    photo = message.photo[-1] if message.photo else (
        message.reply_to_message.photo[-1] if message.reply_to_message and message.reply_to_message.photo else None
    )
    if not photo:
        await message.reply("📸 Прикрепите картинку к команде <code>/get_file_id</code> (или ответьте ей на фото), и я пришлю её <code>file_id</code> для словаря <code>BANNERS</code>.")
        return
    await message.reply(f"🖼 <b>File ID этой фотографии:</b>\n\n<code>{photo.file_id}</code>\n\n<i>Скопируйте и вставьте его в словарь BANNERS в коде!</i>")


@router.message(Command("bind_chat"))
async def cmd_bind_chat(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply("⛔ Привязать рабочий чат может только руководитель клуба.")
        return
    save_chat_config(message.chat.id, message.message_thread_id)
    thread_str = f" (Топик <code>{message.message_thread_id}</code>)" if message.message_thread_id else ""
    await message.reply(f"✅ <b>Рабочий чат привязан!</b>{thread_str}\nСюда будут приходить сводки и уведомления о горящих дедлайнах.")


@router.message(Command("status"))
async def cmd_chat_status(message: Message):
    events = await sheets.get_active_events()
    all_tasks = await sheets.get_all_active_tasks()

    if not events:
        await message.reply("✨ Сейчас нет активных мероприятий в работе.")
        return

    lines = [
        "📊 <b>ОПЕРАТИВНАЯ СВОДКА ПО КЛУБУ</b>",
        "━━━━━━━━━━━━━━━━━━━━"
    ]
    for ev in events:
        ev_tasks = [t for t in all_tasks if str(t["ID Мероприятия"]) == str(ev["ID"])]
        free_cnt = sum(1 for t in ev_tasks if t["Статус"] == "Свободна")
        poster_ok = "✅" if str(ev["Плакат"]).startswith("Есть") else "❌"
        pres_ok = "✅" if str(ev["Преза"]).startswith("Есть") else "❌"

        lines.append(
            f"🎪 <b>{html.escape(str(ev['Название']))}</b> (🗓 {html.escape(str(ev['Дата']))}) <code>[{ev['ID']}]</code>\n"
            f"├ 👑 Орг: {html.escape(str(ev['Организатор']))} | 🍌 Миньоны: {html.escape(str(ev['Миньоны']))}\n"
            f"├ 🖼 Плакат: {poster_ok}  |  📊 Преза: {pres_ok}\n"
            f"└ 📌 Активных задач: <b>{len(ev_tasks)}</b> (🟢 свободно: <b>{free_cnt}</b>)\n"
        )

    await render_screen(message, "\n".join(lines), banner_key="status")


@router.message(Command("newtask"))
async def cmd_quick_newtask(message: Message):
    args = message.text.removeprefix("/newtask").strip()
    if not args:
        await message.reply(
            "💡 <b>Быстрое создание задачи прямо в чате:</b>\n"
            "<code>/newtask ID_меро Текст задачи | ДД.ММ ЧЧ:ММ | @исполнитель</code>\n\n"
            "<blockquote>Пример:\n/newtask ev_a1b2c Заказать пиццу и стаканчики | 24.11 17:00 | @ivan</blockquote>"
        )
        return

    parts = [p.strip() for p in args.split("|")]
    first_part = parts[0].split(maxsplit=1)
    if len(first_part) < 2 or len(parts) < 2:
        await message.reply("⚠️ Формат: <code>/newtask ID_меро Задача | ДД.ММ ЧЧ:ММ | @юзер</code>")
        return

    ev_id, task_text = first_part[0], first_part[1]
    deadline = parts[1]
    assignee = parts[2] if len(parts) > 2 else "Свободно"

    ev = await sheets.get_event_by_id(ev_id)
    if not ev:
        await message.reply(f"❌ Мероприятие с ID <code>{html.escape(ev_id)}</code> не найдено.")
        return

    t_id = await sheets.add_task(ev_id, ev["Название"], task_text, deadline, assignee=assignee)
    await message.reply(
        f"✅ <b>Задача добавлена в таблицу!</b>  <code>[{t_id}]</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎪 <b>Меро:</b> {html.escape(str(ev['Название']))}\n"
        f"📌 <b>Задача:</b> {html.escape(task_text)}\n"
        f"👤 <b>Исполнитель:</b> {html.escape(assignee)}\n"
        f"⏳ <b>Дедлайн:</b> <code>{html.escape(deadline)}</code>"
    )


# =====================================================================
# 6. РАЗДЕЛ «МЕРОПРИЯТИЯ», ПАСПОРТ И НАСТРОЙКИ
# =====================================================================
async def build_events_list_view():
    events = await sheets.get_active_events()
    if not events:
        return "✨ <b>Сейчас нет активных мероприятий.</b>\nСамое время придумать новый ивент!", None

    kb = InlineKeyboardBuilder()
    for ev in events:
        kb.row(InlineKeyboardButton(
            text=f"🎪 {ev['Название']}  •  🗓 {ev['Дата']}",
            callback_data=f"ev:{ev['ID']}"
        ))

    text = (
        "📅 <b>АКТИВНЫЕ МЕРОПРИЯТИЯ КЛУБА</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Выберите мероприятие ниже, чтобы открыть его <b>паспорт</b>, посмотреть ссылки на материалы, записаться в <b>миньоны</b> или разобрать задачи:"
    )
    return text, kb


@router.message(F.text == "📅 Мероприятия")
@router.message(Command("events"))
async def show_events(message: Message):
    text, kb = await build_events_list_view()
    await render_screen(message, text, banner_key="events", kb=kb)


@router.callback_query(F.data == "back_events")
async def cb_back_events(callback: CallbackQuery):
    text, kb = await build_events_list_view()
    await render_screen(callback, text, banner_key="events", kb=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("ev:"))
async def open_event_passport(callback: CallbackQuery):
    event_id = callback.data.split(":")[1]
    ev = await sheets.get_event_by_id(event_id)
    if not ev:
        await callback.answer("Мероприятие не найдено!", show_alert=True)
        return

    tasks = await sheets.get_event_tasks(event_id)
    text = format_event_card(ev, tasks)
    kb = event_card_kb(event_id, ev["Название"])
    await render_screen(callback, text, banner_key="passport", kb=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("joinmin:"))
async def cb_join_minions(callback: CallbackQuery):
    event_id = callback.data.split(":")[1]
    uname = callback.from_user.username or f"id{callback.from_user.id}"
    res = await sheets.add_minion(event_id, uname)
    await callback.answer(f"🍌 {res}", show_alert=True)
    await open_event_passport(callback)


@router.callback_query(F.data.startswith("evcfg:"))
async def open_event_settings(callback: CallbackQuery):
    event_id = callback.data.split(":")[1]
    ev = await sheets.get_event_by_id(event_id)
    if not ev:
        return

    tasks = await sheets.get_event_tasks(event_id)
    text = (
        format_event_card(ev, tasks)
        + "\n\n⚙️ <b>РЕЖИМ НАСТРОЙКИ МЕРОПРИЯТИЯ</b>\n"
        + "<i>Выберите параметр на клавиатуре ниже, чтобы обновить данные в Google Таблице:</i>"
    )
    await render_screen(callback, text, banner_key="settings", kb=event_settings_kb(event_id))
    await callback.answer()


@router.callback_query(F.data.startswith("setasset:"))
async def choose_asset_status(callback: CallbackQuery):
    _, event_id, asset_type = callback.data.split(":")
    label = "🖼 Плаката" if asset_type == "poster" else "📊 Презентации"

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="❌ Статус: Нет", callback_data=f"assetno:{event_id}:{asset_type}"),
        InlineKeyboardButton(text="🔗 Есть (прикрепить ссылку)", callback_data=f"editprop:{event_id}:{asset_type}")
    )
    kb.row(InlineKeyboardButton(text="🔙 Назад к настройкам", callback_data=f"evcfg:{event_id}"))

    text = f"⚙️ <b>Настройка готовности {label}</b>\n\nВыберите текущий статус или прикрепите ссылку на готовый файл (Figma, Google Drive, Яндекс.Диск):"
    await render_screen(callback, text, banner_key="settings", kb=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("assetno:"))
async def set_asset_no(callback: CallbackQuery):
    _, event_id, asset_type = callback.data.split(":")
    col = 6 if asset_type == "poster" else 7
    await sheets.update_event_field(event_id, col, "Нет")
    await callback.answer("Статус изменен на «Нет»")
    callback.data = f"evcfg:{event_id}"
    await open_event_settings(callback)


@router.callback_query(F.data.startswith("clearmat:"))
async def clear_materials(callback: CallbackQuery):
    event_id = callback.data.split(":")[1]
    await sheets.update_event_field(event_id, 8, "Пусто")
    await callback.answer("🗑 Список доп. материалов очищен!")
    callback.data = f"evcfg:{event_id}"
    await open_event_settings(callback)


@router.callback_query(F.data.startswith("editprop:"))
async def start_edit_property(callback: CallbackQuery, state: FSMContext):
    _, event_id, prop_type = callback.data.split(":")
    await state.update_data(event_id=event_id, prop_type=prop_type)
    await state.set_state(EditEventPropSG.waiting_value)

    prompts = {
        "poster": "🖼 Отправьте <b>ссылку на Плакат</b> (Figma, Диск, пост):",
        "pres": "📊 Отправьте <b>ссылку на Презентацию</b>:",
        "org": "👑 Введите <code>@username</code> главного <b>Организатора</b>:",
        "minions": "🍌 Введите список <b>Миньонов</b> через запятую (например: <code>@alisa, @bob</code>):",
        "mat_add": "📂 Отправьте название и ссылку для <b>Доп. материалов</b>\n(например: <code>Смета расходов — https://...</code>):"
    }
    await callback.message.answer(prompts[prop_type])
    await callback.answer()


@router.message(EditEventPropSG.waiting_value)
async def process_edit_property(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()

    event_id = data["event_id"]
    prop_type = data["prop_type"]
    val = message.text.strip()

    if prop_type == "poster":
        await sheets.update_event_field(event_id, 6, f"Есть ({val})")
    elif prop_type == "pres":
        await sheets.update_event_field(event_id, 7, f"Есть ({val})")
    elif prop_type == "org":
        await sheets.update_event_field(event_id, 4, val)
    elif prop_type == "minions":
        await sheets.update_event_field(event_id, 5, val)
    elif prop_type == "mat_add":
        await sheets.append_material(event_id, val)

    ev = await sheets.get_event_by_id(event_id)
    tasks = await sheets.get_event_tasks(event_id)
    text = "✅ <b>Данные в Google Таблице обновлены!</b>\n\n" + format_event_card(ev, tasks)
    await render_screen(message, text, banner_key="passport", kb=event_card_kb(event_id, ev["Название"]))


# =====================================================================
# 7. РАЗДЕЛ «ЗАДАЧИ» И «МОИ ЗАДАЧИ»
# =====================================================================
@router.callback_query(F.data.startswith("evtasks:"))
async def show_event_tasks(callback: CallbackQuery):
    event_id = callback.data.split(":")[1]
    ev = await sheets.get_event_by_id(event_id)
    tasks = await sheets.get_event_tasks(event_id)

    kb = InlineKeyboardBuilder()
    status_emoji = {"Свободна": "🟢", "В работе": "🟡", "Готово": "✅"}

    for t in tasks:
        emoji = status_emoji.get(t["Статус"], "🔹")
        kb.row(InlineKeyboardButton(
            text=f"{emoji} {t['Задача']} ({t['Исполнитель']})",
            callback_data=f"task:{t['ID']}"
        ))

    kb.row(InlineKeyboardButton(text="➕ Добавить задачу", callback_data=f"addtask:{event_id}:{ev['Название'][:16]}"))
    kb.row(InlineKeyboardButton(text="🔙 К паспорту мероприятия", callback_data=f"ev:{event_id}"))

    text = (
        f"📋 <b>ЗАДАЧИ: {html.escape(str(ev['Название']).upper())}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<blockquote>🟢 — свободна (нажми, чтобы забрать себе)\n"
        f"🟡 — уже в работе\n"
        f"✅ — задача закрыта</blockquote>\n"
        f"Всего задач в списке: <b>{len(tasks)}</b>"
    )
    await render_screen(callback, text, banner_key="tasks", kb=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("task:"))
async def task_detail(callback: CallbackQuery):
    task_id = callback.data.split(":")[1]
    t = await sheets.get_task_by_id(task_id)
    if not t:
        await callback.answer("Задача не найдена!", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    if t["Статус"] == "Свободна":
        kb.row(InlineKeyboardButton(text="🙋‍♂️ Забрать задачу себе", callback_data=f"take:{task_id}"))
    elif t["Статус"] != "Готово":
        kb.row(InlineKeyboardButton(text="✅ Отметить выполненной", callback_data=f"done:{task_id}"))

    kb.row(InlineKeyboardButton(text="🔙 К списку задач мероприятия", callback_data=f"evtasks:{t['ID Мероприятия']}"))
    await render_screen(callback, format_task_card(t), banner_key="tasks", kb=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("take:"))
async def take_task(callback: CallbackQuery):
    task_id = callback.data.split(":")[1]
    await sheets.assign_task(task_id, callback.from_user.username, callback.from_user.id)
    await callback.answer("🔥 Задача записана на вас!")
    callback.data = f"task:{task_id}"
    await task_detail(callback)


@router.callback_query(F.data.startswith("done:"))
async def complete_task(callback: CallbackQuery):
    task_id = callback.data.split(":")[1]
    await sheets.update_task_status(task_id, "Готово")
    await callback.answer("🎉 Поздравляем! Задача закрыта!", show_alert=True)
    callback.data = f"task:{task_id}"
    await task_detail(callback)


@router.message(F.text == "🎯 Мои задачи")
async def my_tasks(message: Message):
    tasks = await sheets.get_user_tasks(message.from_user.id, message.from_user.username)
    if not tasks:
        text = (
            "🎯 <b>ТВОЙ СПИСОК ЗАДАЧ ПУСТ!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "За тобой сейчас не числится активных поручений. Можно передохнуть или заглянуть в <b>📅 Мероприятия</b> и взять интересную задачу!"
        )
        await render_screen(message, text, banner_key="tasks")
        return

    kb = InlineKeyboardBuilder()
    for t in tasks:
        kb.row(InlineKeyboardButton(
            text=f"⏳ {t['Дедлайн']}  |  {t['Задача']} ({t['Мероприятие']})",
            callback_data=f"task:{t['ID']}"
        ))

    text = (
        f"🎯 <b>ТВОИ АКТИВНЫЕ ЗАДАЧИ ({len(tasks)} шт.)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Нажми на задачу, чтобы посмотреть подробности или отметить её готовой:</i>"
    )
    await render_screen(message, text, banner_key="tasks", kb=kb)


# =====================================================================
# 8. СОЗДАНИЕ МЕРОПРИЯТИЙ И ЗАДАЧ (ПОШАГОВЫЙ ДИАЛОГ FSM)
# =====================================================================
@router.message(F.text == "➕ Создать мероприятие")
async def start_create_event(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(CreateEventSG.title)
    await message.answer("🎪 Введите <b>название</b> нового мероприятия:")


@router.message(CreateEventSG.title)
async def process_event_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text)
    await state.set_state(CreateEventSG.date)
    await message.answer("🗓 Введите <b>дату проведения</b> (например, <code>28.11.2026</code>):")


@router.message(CreateEventSG.date)
async def process_event_date(message: Message, state: FSMContext):
    await state.update_data(date=message.text)
    await state.set_state(CreateEventSG.organizer)
    await message.answer("👑 Укажите <code>@username</code> главного организатора (или отправьте точку <code>.</code>, чтобы назначить себя):")


@router.message(CreateEventSG.organizer)
async def process_event_org(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()

    org = message.text.strip()
    if org == ".":
        org = f"@{message.from_user.username}" if message.from_user.username else str(message.from_user.id)

    ev_id = await sheets.add_event(data["title"], data["date"], org)
    ev = await sheets.get_event_by_id(ev_id)

    text = "🎉 <b>Мероприятие создано и добавлено в Google Таблицу!</b>\n\n" + format_event_card(ev, [])
    await render_screen(message, text, banner_key="passport", kb=event_card_kb(ev_id, ev["Название"]))


@router.callback_query(F.data.startswith("addtask:"))
async def start_create_task(callback: CallbackQuery, state: FSMContext):
    _, event_id, event_title = callback.data.split(":", 2)
    await state.update_data(event_id=event_id, event_title=event_title)
    await state.set_state(CreateTaskSG.text)
    await callback.message.answer(f"📌 Напишите <b>суть задачи</b> для мероприятия «{html.escape(event_title)}»:")
    await callback.answer()


@router.message(CreateTaskSG.text)
async def process_task_text(message: Message, state: FSMContext):
    await state.update_data(text=message.text)
    await state.set_state(CreateTaskSG.deadline)
    await message.answer("⏳ Укажите <b>дедлайн</b> в формате <code>ДД.ММ ЧЧ:ММ</code> (например, <code>25.11 18:00</code>):")


@router.message(CreateTaskSG.deadline)
async def process_task_deadline(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()

    t_id = await sheets.add_task(data["event_id"], data["event_title"], data["text"], message.text.strip())
    ev = await sheets.get_event_by_id(data["event_id"])
    tasks = await sheets.get_event_tasks(data["event_id"])

    text = (
        f"✅ <b>Задача «{html.escape(data['text'])}» (<code>{t_id}</code>) добавлена!</b>\n\n"
        + format_event_card(ev, tasks)
    )
    await render_screen(message, text, banner_key="passport", kb=event_card_kb(data["event_id"], ev["Название"]))


# =====================================================================
# 9. ФОНОВЫЙ ПЛАНИРОВЩИК НАПОМИНАНИЙ О ДЕДЛАЙНАХ
# =====================================================================
def parse_deadline(deadline_str: str, now: datetime) -> datetime | None:
    raw = str(deadline_str).strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m %H:%M"):
        try:
            dt = datetime.strptime(raw, fmt)
            if fmt == "%d.%m %H:%M":
                dt = dt.replace(year=now.year)
            return dt.replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


async def check_deadlines_job(bot: Bot):
    """Каждые 10 минут проверяет задачи и рассылает пинги с баннерами."""
    now = datetime.now(TZ)
    tasks = await sheets.get_all_active_tasks()
    chat_cfg = load_chat_config()

    for t in tasks:
        dt = parse_deadline(t.get("Дедлайн", ""), now)
        if not dt:
            continue

        hours_left = (dt - now).total_seconds() / 3600
        rem_stage = str(t.get("Напоминание", "")).strip()
        task_id = str(t["ID"])
        tg_id_str = str(t.get("TG ID", "")).strip()
        assignee = html.escape(str(t.get("Исполнитель", "Свободно")))

        # 1. МЕНЕЕ 2 ЧАСОВ ДО ДЕДЛАЙНА — АЛЕРТ В ОБЩИЙ ЧАТ И В ЛС
        if -5 <= hours_left <= 2 and rem_stage != "2h":
            alert_text = (
                f"🔥 <b>ГОРИТ ДЕДЛАЙН ({html.escape(str(t['Дедлайн']))})!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎪 <b>Мероприятие:</b> {html.escape(str(t['Мероприятие']))}\n"
                f"👤 <b>Ответственный:</b> {assignee}\n\n"
                f"📌 <b>Задача:</b>\n"
                f"<blockquote>{html.escape(str(t['Задача']))}</blockquote>"
            )
            if chat_cfg:
                try:
                    if BANNERS.get("alert"):
                        await bot.send_photo(
                            chat_id=chat_cfg["chat_id"],
                            message_thread_id=chat_cfg.get("thread_id"),
                            photo=BANNERS["alert"],
                            caption=alert_text
                        )
                    else:
                        await bot.send_message(
                            chat_id=chat_cfg["chat_id"],
                            message_thread_id=chat_cfg.get("thread_id"),
                            text=alert_text
                        )
                except Exception as e:
                    logging.warning(f"Ошибка отправки алерта в чат: {e}")

            if tg_id_str.isdigit():
                try:
                    await bot.send_message(int(tg_id_str), alert_text)
                except Exception:
                    pass

            await sheets.mark_reminder_sent(task_id, "2h")

        # 2. МЕНЕЕ 24 ЧАСОВ — МЯГКОЕ НАПОМИНАНИЕ В ЛС
        elif 2 < hours_left <= 24 and rem_stage not in ("24h", "2h"):
            remind_text = (
                f"⏰ <b>НАПОМИНАНИЕ О ЗАДАЧЕ (осталось меньше суток)</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎪 <b>Мероприятие:</b> {html.escape(str(t['Мероприятие']))}\n"
                f"⏳ <b>Дедлайн:</b> <code>{html.escape(str(t['Дедлайн']))}</code>\n\n"
                f"📌 <b>Задача:</b>\n"
                f"<blockquote>{html.escape(str(t['Задача']))}</blockquote>"
            )
            sent = False
            if tg_id_str.isdigit():
                try:
                    await bot.send_message(int(tg_id_str), remind_text)
                    sent = True
                except Exception:
                    pass

            if not sent and chat_cfg:
                try:
                    await bot.send_message(
                        chat_id=chat_cfg["chat_id"],
                        message_thread_id=chat_cfg.get("thread_id"),
                        text=f"🔔 Напоминание для {assignee}!\n\n" + remind_text
                    )
                except Exception:
                    pass

            await sheets.mark_reminder_sent(task_id, "24h")


# =====================================================================
# 10. ТОЧКА ВХОДА И ЗАПУСК
# =====================================================================
async def main():
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()
    dp.include_router(router)

    print("🔄 Проверка и инициализация Google Таблицы...")
    await sheets.init_sheets()

    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(check_deadlines_job, "interval", minutes=10, kwargs={"bot": bot})
    scheduler.start()

    print("🚀 Бот студклуба успешно запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())