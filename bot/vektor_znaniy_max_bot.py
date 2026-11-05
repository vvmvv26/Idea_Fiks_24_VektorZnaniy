"""Вектор Знаний — бот для мессенджера МАКС.

Один вход для учителя и ученика: ассистент для свободных вопросов, учебные материалы по
библиотеке промтов, поиск по загруженному конспекту, проверка ответа и генерация тестов.
Без нейросети бот выдаёт готовый промт и ссылки на бесплатные чаты, с локальной моделью
(Ollama) отвечает сам. Состояние хранится только в памяти сессии.

Запуск: python vektor_znaniy_max_bot.py (токен — в MAX_BOT_TOKEN или в файле .env).
"""
import asyncio
import datetime
import html
import logging
import os
import re
import time
from collections import Counter
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from maxapi import Bot, Dispatcher, F
from maxapi.context import SimpleEventIsolation
from maxapi.context.base import BaseContext
from maxapi.context.state_machine import State, StatesGroup
from maxapi.enums.attachment import AttachmentType
from maxapi.enums.parse_mode import ParseMode
from maxapi.enums.sender_action import SenderAction
from maxapi.filters.command import Command, CommandStart
from maxapi.types.attachments.buttons.callback_button import CallbackButton
from maxapi.types.command import BotCommand
from maxapi.types.error_event import ErrorEvent
from maxapi.types.input_media import InputMediaBuffer
from maxapi.types.updates.bot_started import BotStarted
from maxapi.types.updates.message_callback import MessageCallback
from maxapi.types.updates.message_created import MessageCreated

import exporter
import keyboards
import materials
import texts
from formatting import latex_to_text, markdown_to_html
from llm_client import ask_llm, check_llm_health, estimate_wait, is_llm_configured, llm_health_loop, model_name
from prompts import (
    CHECK_SYSTEM_PROMPTS, CHECK_USER_TEMPLATE, EXAMPLE_LIBRARY, IDEA_OF_DAY_LIST, QUIZ_FIELDS,
    QUIZ_SYSTEM_PROMPTS, QUIZ_TYPE_TEXT, QUIZ_USER_TEMPLATE, SEARCH_SYSTEM_PROMPT, SEARCH_USER_TEMPLATE,
)

load_dotenv()

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("maxapi").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

MESSAGE_LIMIT = 3000  # у МАКС предел 4000 символов, оставляем запас на разметку
QUIZ_MAX_TOKENS = 1200  # тесту с ключом нужно больше места, чем обычному ответу

# Что сохраняется в сессии при выходе в меню и отмене. Материал и введённые данные сюда не входят.
SESSION_KEEP = (
    "user_type", "role_key", "favorites", "last_system_prompt", "last_user_prompt", "last_answer",
    "last_class", "last_doc", "request",
)
MATERIAL_MAX_TOKENS = 1000  # план урока или рабочий лист длиннее обычного ответа
# Активное меню: его сообщение и текст, чтобы при показе следующего снять кнопки или удалить
MENU_KEYS = ("menu_mid", "menu_text", "menu_keep")

# Анонимные счётчики использования для /stats: только количества, без идентификаторов пользователей.
# Живут в памяти процесса и обнуляются при перезапуске.
STATS = Counter()
STARTED_AT = time.time()

if not os.environ.get("MAX_BOT_TOKEN"):
    raise SystemExit("Не задан MAX_BOT_TOKEN. Укажите его в bot/.env или передайте в контейнер: -e MAX_BOT_TOKEN=...")

bot = Bot(format=ParseMode.HTML)
# Каждое событие — отдельная задача, чтобы ответ модели (до нескольких минут) не держал остальных.
# События одного пользователя при этом обрабатываются по очереди.
dp = Dispatcher(use_create_task=True, event_isolation=SimpleEventIsolation())


class States(StatesGroup):
    selecting_role = State()
    selecting_subrole = State()
    main_menu = State()
    asking = State()
    selecting_task = State()
    selecting_subject = State()
    awaiting_info = State()
    search_upload = State()
    search_question = State()
    check_awaiting_task = State()
    check_awaiting_answer = State()


# ---------- отправка сообщений ----------

def split_text(text, limit=MESSAGE_LIMIT):
    """Режет текст на части не длиннее limit, по возможности по строкам."""
    parts, buf = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{buf}\n{line}" if buf else line
        if len(candidate) > limit:
            parts.append(buf)
            buf = line
        else:
            buf = candidate
    if buf:
        parts.append(buf)
    return parts or [""]


async def send_plain(chat_id, text):
    """Обычный текст без разметки. Экранируем: символы < > & ломают HTML."""
    for part in split_text(text or "Пустой ответ."):
        await bot.send_message(chat_id=chat_id, text=html.escape(part))


async def send_answer(chat_id, text, title=""):
    """Ответ нейросети: её Markdown превращается в оформление МАКС."""
    header = f"<b>{html.escape(title)}</b>\n\n" if title else ""
    for part in split_text(text or "Пустой ответ."):
        await bot.send_message(chat_id=chat_id, text=header + markdown_to_html(part))
        header = ""


async def send_prompt(chat_id, header, system_prompt, user_prompt):
    prompt = f"СИСТЕМНЫЙ ПРОМТ:\n{system_prompt}\n\nЗАПРОС:\n{user_prompt}"
    for part in split_text(prompt):
        await bot.send_message(chat_id=chat_id, text=f"{header}\n\n<pre>{html.escape(part)}</pre>")
        header = "📄 Продолжение промта"


async def delete_quietly(mid):
    if not mid:
        return
    try:
        await bot.delete_message(mid)
    except Exception as e:
        logger.debug(f"delete_message failed: {e}")


@asynccontextmanager
async def typing(chat_id):
    """Показывает «печатает…», пока модель думает."""
    async def keep_typing():
        while True:
            try:
                await bot.send_action(chat_id=chat_id, action=SenderAction.TYPING_ON)
            except Exception as e:
                logger.debug(f"send_action failed: {e}")
            await asyncio.sleep(4)

    task = asyncio.create_task(keep_typing())
    try:
        yield
    finally:
        task.cancel()


async def ask_model(chat_id, context, system_prompt, user_prompt, *, title="", history=None, max_tokens=None):
    """Запрос к нейросети с понятным ожиданием: примерное время, «печатает…», потом ответ.
    Удачный ответ запоминается для .docx и уточнений «проще / подробнее»."""
    seconds, ahead = estimate_wait()
    notice = await bot.send_message(chat_id=chat_id, text=texts.wait_text(seconds, ahead))
    kwargs = {"history": history}
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    async with typing(chat_id):
        answer = await ask_llm(system_prompt, user_prompt, **kwargs)
    await delete_quietly(notice.message.body.mid if notice and notice.message and notice.message.body else None)
    if answer.ok:
        STATS["ответов нейросети"] += 1
        await send_answer(chat_id, answer.text, title)
        await context.update_data(
            last_answer=answer.text[:4000],
            last_doc={"title": title or "Ответ ассистента", "body": latex_to_text(answer.text), "key": None},
        )
    return answer


# ---------- выбор: ответ здесь или промт для другой нейросети ----------
# Каждый сценарий собирает запрос (request) и отдаёт его в offer_choice. Пользователь решает,
# получить ответ здесь (потом — файл Word, уточнения) или забрать промт в другую нейросеть.

def make_request(kind, feature, header, title, system_prompt, user_prompt, *, max_tokens=None, history=None, prompt_text=None, question=None):
    return {
        "kind": kind, "feature": feature, "header": header, "title": title,
        "system": system_prompt, "user": user_prompt, "max_tokens": max_tokens,
        "history": history or [], "prompt_text": prompt_text or user_prompt, "question": question,
    }


async def offer_choice(chat_id, context, request):
    """Данные собраны — предлагаем выбор. Без нейросети выбирать не из чего: сразу промт."""
    await context.update_data(request=request, last_system_prompt=request["system"], last_user_prompt=request["user"])
    if not is_llm_configured():
        await bot.send_message(chat_id=chat_id, text=texts.NO_LLM.format(feature=request["feature"]))
        await give_prompt(chat_id, context, request)
        return
    seconds, _ = estimate_wait()
    await show_in_mode(chat_id, context, texts.CHOOSE_RESULT, keyboards.choice_rows(texts.eta(seconds)))


async def answer_here(chat_id, context, request):
    answer = await ask_model(
        chat_id, context, request["system"], request["user"],
        title=request["title"], history=request.get("history"), max_tokens=request.get("max_tokens"),
    )
    if not answer.ok:
        await bot.send_message(chat_id=chat_id, text=f"{html.escape(answer.text)}\n\nНиже промт, чтобы получить ответ в другой нейросети.")
        await give_prompt(chat_id, context, request)
        return
    if request["kind"] == "ask":
        conversation = (await context.get_data()).get("conversation") or []
        conversation = (conversation + [[request["question"], answer.text[:2000]]])[-2:]
        await context.update_data(conversation=conversation)
    text = f"{texts.LLM_TEST_CHECK}\n\n{texts.WHAT_NEXT}" if request["kind"] == "quiz" else texts.WHAT_NEXT
    await show_in_mode(chat_id, context, text, keyboards.result_rows(docx=True, followups=True, other_ai=True, feedback=True))


async def give_prompt(chat_id, context, request):
    await send_prompt(chat_id, request["header"], request["system"], request["prompt_text"])
    text = texts.PASTE_PROMPT
    if request["kind"] == "quiz" and not is_llm_configured():
        text = f"{text}\n\n{texts.QUIZ_TOPIC_NO_LLM_HINT}"
    await show_in_mode(chat_id, context, text, keyboards.result_rows(ai_now=True, chats=True, favorite=True, feedback=True))


# ---------- одно активное меню ----------
# В чате всегда одно сообщение с кнопками. Когда появляется новое меню, у прежнего кнопки
# снимаются (если его текст важен для истории, например вопрос «В каком классе?»)
# или оно удаляется целиком (навигация вроде «Что дальше?»).

async def retire_menu(context):
    data = await context.get_data()
    mid = data.get("menu_mid")
    if not mid:
        return
    try:
        if data.get("menu_keep"):
            await bot.edit_message(message_id=mid, text=data.get("menu_text") or "…", attachments=[])
        else:
            await bot.delete_message(mid)
    except Exception as e:
        logger.debug(f"cannot retire menu: {e}")
    await context.update_data(menu_mid=None)


async def send_menu(chat_id, context, text, keyboard, keep=False):
    await retire_menu(context)
    sent = await bot.send_message(chat_id=chat_id, text=text, attachments=[keyboard])
    if sent and sent.message and sent.message.body:
        await context.update_data(menu_mid=sent.message.body.mid, menu_text=text, menu_keep=keep)


async def edit_menu(chat_id, context, text, keyboard, keep=False):
    mid = (await context.get_data()).get("menu_mid")
    if not mid:
        await send_menu(chat_id, context, text, keyboard, keep)
        return
    await bot.edit_message(message_id=mid, text=text, attachments=[keyboard])
    await context.update_data(menu_text=text, menu_keep=keep)


async def reset_session(context, keep_keys=SESSION_KEEP):
    data = await context.get_data()
    keep = {k: data[k] for k in (*keep_keys, *MENU_KEYS) if data.get(k) is not None}
    await context.clear()
    await context.update_data(**keep)


async def back_to_menu(chat_id, context, text=texts.WHAT_NEXT, extra_rows=None, system_prompt=None, user_prompt=None):
    """Главное меню. Роль, избранное и последний результат сохраняются,
    временные данные сценария удаляются. Полный сброс — только по /start."""
    await reset_session(context)
    if system_prompt:
        await context.update_data(last_system_prompt=system_prompt, last_user_prompt=user_prompt)
    await context.set_state(States.main_menu)
    if text in (texts.WHAT_NEXT, texts.MAIN_MENU):
        data = await context.get_data()
        text = f"{keyboards.role_title(data.get('user_type'), data.get('role_key'))}\n\n{text}"
    await send_menu(chat_id, context, text, keyboards.main_menu(extra_rows))


async def show_in_mode(chat_id, context, text, extra_rows):
    """Меню после шага: в поиске и в вопросах ассистенту остаёмся в том же режиме,
    в остальных сценариях — главное меню с кнопками результата сверху."""
    state = await context.get_state()
    if state == States.search_question:
        mode_text = texts.SEARCH_MORE if text == texts.WHAT_NEXT else text
        await send_menu(chat_id, context, mode_text, keyboards.material_menu(extra_rows))
    elif state == States.asking:
        mode_text = texts.ASK_MORE if text == texts.WHAT_NEXT else text
        await send_menu(chat_id, context, mode_text, keyboards.to_menu(extra_rows))
    else:
        await back_to_menu(chat_id, context, text, extra_rows)


def bot_link():
    username = getattr(bot.me, "username", None) if bot.me else None
    return f"https://max.ru/{username}" if username else "https://max.ru/ideya_fiks_24_bot"


async def show_start(chat_id, context):
    STATS["запусков /start"] += 1
    await retire_menu(context)
    await context.clear()
    await context.set_state(States.selecting_role)
    await send_menu(chat_id, context, texts.start_text(is_llm_configured()), keyboards.roles())


async def has_role(context):
    data = await context.get_data()
    return bool(data.get("user_type") and data.get("role_key"))


async def cancel(chat_id, context):
    """Прерывает текущий сценарий. Роль и избранное остаются, чтобы можно было продолжить
    одной кнопкой; загруженный материал и введённые данные удаляются."""
    await reset_session(context)
    await send_menu(chat_id, context, texts.CANCELLED, keyboards.resume(await has_role(context)))


def callback_chat(event):
    return event.message.recipient.chat_id if event.message else None


# ---------- кнопки, которые работают в любом месте диалога ----------
# maxapi отдаёт событие первому подходящему обработчику, поэтому эти идут раньше остальных.
# Отдельно подтверждать нажатие (event.ack()) не нужно: бот и так отвечает правкой меню или
# новым сообщением, а пустой ack() API МАКС отклоняет с ошибкой 400.

@dp.message_callback(F.callback.payload == "ai_generate_now")
async def on_ai_generate_now(event: MessageCallback, context: BaseContext):
    chat_id = callback_chat(event)
    if not is_llm_configured():
        await bot.send_message(chat_id=chat_id, text="Нейросеть сейчас не подключена. Воспользуйтесь промтом и кнопками чатов.")
        return
    request = await current_request(context)
    if not request:
        await bot.send_message(chat_id=chat_id, text="Запрос не найден. Соберите новый через меню.")
        return
    await answer_here(chat_id, context, request)


@dp.message_callback(F.callback.payload == "show_prompt")
async def on_show_prompt(event: MessageCallback, context: BaseContext):
    chat_id = callback_chat(event)
    request = await current_request(context)
    if not request:
        await bot.send_message(chat_id=chat_id, text="Запрос не найден. Соберите новый через меню.")
        return
    STATS["промтов для других нейросетей"] += 1
    await give_prompt(chat_id, context, request)


async def current_request(context):
    data = await context.get_data()
    if data.get("request"):
        return data["request"]
    if data.get("last_user_prompt"):
        return make_request("prompt", "Ответ", "📄 <b>Промт</b>", "🤖 Ответ", data.get("last_system_prompt"), data["last_user_prompt"])
    return None


@dp.message_callback(F.callback.payload.startswith("fu:"))
async def on_followup(event: MessageCallback, context: BaseContext):
    """«Проще», «Подробнее», «Ещё пример» — продолжение разговора с учётом предыдущего ответа."""
    chat_id = callback_chat(event)
    kind = event.callback.payload.split(":", 1)[1]
    if kind not in texts.FOLLOWUPS:
        return
    if not is_llm_configured():
        await bot.send_message(chat_id=chat_id, text=texts.FOLLOWUP_NEEDS_LLM)
        return
    data = await context.get_data()
    if not data.get("last_answer") or not data.get("last_user_prompt"):
        await bot.send_message(chat_id=chat_id, text=texts.FOLLOWUP_NOTHING)
        return
    title, instruction = texts.FOLLOWUPS[kind]
    STATS["уточнений ответа"] += 1
    history = [("user", data["last_user_prompt"]), ("assistant", data["last_answer"])]
    answer = await ask_model(chat_id, context, data.get("last_system_prompt"), instruction, title=title, history=history)
    if answer.ok:
        await show_in_mode(chat_id, context, texts.WHAT_NEXT, keyboards.result_rows(docx=True, followups=True, other_ai=True, feedback=True))
    else:
        await send_plain(chat_id, answer.text)


@dp.message_callback(F.callback.payload == "add_favorite")
async def on_add_favorite(event: MessageCallback, context: BaseContext):
    chat_id = callback_chat(event)
    data = await context.get_data()
    prompt = data.get("last_user_prompt")
    if not prompt:
        await bot.send_message(chat_id=chat_id, text="Сначала соберите промт.")
        return
    favorites = list(data.get("favorites") or [])
    if any(f["user"] == prompt for f in favorites):
        await bot.send_message(chat_id=chat_id, text=texts.FAVORITE_EXISTS)
        return
    title = (data.get("request") or {}).get("title")
    favorites.append({"system": data.get("last_system_prompt"), "user": prompt, "title": title})
    await context.update_data(favorites=favorites)
    await bot.send_message(chat_id=chat_id, text=texts.FAVORITE_ADDED.format(count=len(favorites)))


async def show_favorites(chat_id, context):
    """Избранное — меню на месте текущего: открыть промт или удалить его."""
    favorites = (await context.get_data()).get("favorites") or []
    await context.set_state(States.main_menu)
    if favorites:
        await edit_menu(chat_id, context, texts.FAVORITES_LIST.format(count=len(favorites)), keyboards.favorites_menu(favorites))
    else:
        await edit_menu(chat_id, context, texts.FAVORITES_EMPTY, keyboards.to_menu())


@dp.message_callback(F.callback.payload.startswith("fav:"))
async def on_favorite(event: MessageCallback, context: BaseContext):
    chat_id = callback_chat(event)
    parts = event.callback.payload.split(":")
    action = parts[1]
    favorites = list((await context.get_data()).get("favorites") or [])
    if action == "clear":
        await context.update_data(favorites=[])
        await show_favorites(chat_id, context)
        return
    index = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else -1
    if not 0 <= index < len(favorites):
        await bot.send_message(chat_id=chat_id, text=texts.FAVORITE_GONE)
        await show_favorites(chat_id, context)
        return
    if action == "del":
        favorites.pop(index)
        await context.update_data(favorites=favorites)
        await show_favorites(chat_id, context)
        return
    fav = favorites[index]
    title = keyboards.favorite_title(fav, limit=80)
    request = make_request("prompt", "Избранное", f"⭐ <b>{html.escape(title)}</b>", title, fav["system"], fav["user"])
    await context.update_data(request=request, last_system_prompt=fav["system"], last_user_prompt=fav["user"])
    await send_prompt(chat_id, request["header"], fav["system"], fav["user"])
    rows = keyboards.result_rows(ai_now=True, chats=True)
    rows.append([CallbackButton(text="⭐ К избранному", payload="feature_favorites")])
    await back_to_menu(chat_id, context, texts.PASTE_PROMPT, rows)


@dp.message_callback(F.callback.payload == "export_docx")
async def on_export_docx(event: MessageCallback, context: BaseContext):
    chat_id = callback_chat(event)
    doc = (await context.get_data()).get("last_doc")
    if not doc:
        await bot.send_message(chat_id=chat_id, text=texts.NOTHING_TO_EXPORT)
        return
    data = exporter.to_docx(doc["title"], doc["body"], doc.get("key"))
    try:
        await bot.send_message(
            chat_id=chat_id, text="📄 Файл для печати или публикации в Сферуме",
            attachments=[InputMediaBuffer(data, filename=exporter.filename(doc["title"]))],
        )
        STATS["выгрузок .docx"] += 1
    except Exception as e:
        logger.error(f"Cannot send docx: {e!r}")
        await bot.send_message(chat_id=chat_id, text="Не получилось отправить файл. Попробуйте ещё раз чуть позже.")


@dp.message_callback(F.callback.payload.startswith("fb:"))
async def on_feedback(event: MessageCallback, context: BaseContext):
    """Ответ на оценку становится активным меню: «В меню», а если есть что скачать — «.docx».
    В поиске по материалу остаётся и кнопка теста."""
    positive = event.callback.payload == "fb:up"
    STATS["оценок 👍" if positive else "оценок 👎"] += 1
    text = texts.FEEDBACK_UP.format(link=bot_link()) if positive else texts.FEEDBACK_DOWN
    extra = keyboards.result_rows(docx=True) if (await context.get_data()).get("last_doc") else None
    if await context.get_state() == States.search_question:
        keyboard = keyboards.material_menu(extra)
    else:
        keyboard = keyboards.to_menu(extra)
    await send_menu(callback_chat(event), context, text, keyboard, keep=True)


@dp.message_callback(F.callback.payload == "to_menu")
async def on_to_menu(event: MessageCallback, context: BaseContext):
    chat_id = callback_chat(event)
    if await has_role(context):
        await back_to_menu(chat_id, context)
    else:
        await show_start(chat_id, context)


@dp.message_callback(F.callback.payload == "restart")
async def on_restart(event: MessageCallback, context: BaseContext):
    await show_start(callback_chat(event), context)


@dp.message_callback(F.callback.payload == "cancel")
async def on_cancel(event: MessageCallback, context: BaseContext):
    await cancel(callback_chat(event), context)


# ---------- команды ----------

@dp.message_created(CommandStart())
async def cmd_start(event: MessageCreated, context: BaseContext):
    await show_start(event.message.recipient.chat_id, context)


@dp.bot_started()
async def on_bot_started(event: BotStarted, context: BaseContext):
    await show_start(event.chat_id, context)


@dp.message_created(Command("cancel"))
async def cmd_cancel(event: MessageCreated, context: BaseContext):
    await cancel(event.message.recipient.chat_id, context)


@dp.message_created(Command(["help", "помощь"]))
async def cmd_help(event: MessageCreated):
    await event.message.answer(texts.help_text(is_llm_configured(), model_name()))


def about_with_link():
    return f"{texts.about_text(is_llm_configured())}\n\n📤 Поделиться ботом: {bot_link()}"


@dp.message_created(Command("info"))
async def cmd_info(event: MessageCreated):
    await event.message.answer(about_with_link())


@dp.message_created(Command("stats"))
async def cmd_stats(event: MessageCreated):
    hours = (time.time() - STARTED_AT) / 3600
    lines = [f"📊 <b>Статистика с последнего запуска</b> ({hours:.0f} ч)", ""]
    lines += [f"• {name}: {count}" for name, count in sorted(STATS.items())] or ["• пока пусто"]
    up, down = STATS["оценок 👍"], STATS["оценок 👎"]
    if up + down:
        lines += ["", f"Доля полезных ответов: {up / (up + down):.0%}"]
    lines += ["", "Счётчики анонимные: без имён и идентификаторов пользователей."]
    await event.message.answer("\n".join(lines))


# ---------- роль ----------

@dp.message_callback(States.selecting_role)
async def select_role(event: MessageCallback, context: BaseContext):
    if event.callback.payload not in ("role_teacher", "role_student"):
        return
    user_type = "teacher" if event.callback.payload == "role_teacher" else "student"
    await context.update_data(user_type=user_type)
    await context.set_state(States.selecting_subrole)
    who = "👩‍🏫 Учитель" if user_type == "teacher" else "🎓 Ученик"
    await edit_menu(callback_chat(event), context, f"{who}\n\n{texts.CHOOSE_SUBROLE}", keyboards.subroles(user_type))


@dp.message_callback(States.selecting_subrole)
async def select_subrole(event: MessageCallback, context: BaseContext):
    chat_id = callback_chat(event)
    if event.callback.payload == "back_to_role":
        await context.set_state(States.selecting_role)
        await edit_menu(chat_id, context, texts.CHOOSE_ROLE, keyboards.roles())
        return
    await context.update_data(role_key=event.callback.payload)
    await context.set_state(States.main_menu)
    data = await context.get_data()
    title = keyboards.role_title(data["user_type"], event.callback.payload)
    await edit_menu(chat_id, context, f"{title}\n\n{texts.MAIN_MENU}", keyboards.main_menu())


# ---------- главное меню ----------

@dp.message_callback(States.main_menu)
async def select_feature(event: MessageCallback, context: BaseContext):
    chat_id = callback_chat(event)
    payload = event.callback.payload
    data = await context.get_data()
    user_type = data["user_type"]

    if payload == "back_to_subrole":
        await context.set_state(States.selecting_subrole)
        await edit_menu(chat_id, context, texts.CHOOSE_SUBROLE, keyboards.subroles(user_type))
    elif payload == "feature_ask":
        await context.update_data(conversation=[])
        await context.set_state(States.asking)
        intro = texts.ASK_INTRO if is_llm_configured() else texts.ASK_INTRO_NO_LLM
        await edit_menu(chat_id, context, intro, keyboards.to_menu(), keep=True)
    elif payload == "feature_generate":
        await context.set_state(States.selecting_task)
        await edit_menu(chat_id, context, texts.CHOOSE_TASK, keyboards.tasks(user_type))
    elif payload == "feature_search":
        await context.update_data(material=None, material_loaded_at=None)
        await context.set_state(States.search_upload)
        await edit_menu(chat_id, context, texts.SEARCH_INTRO, keyboards.to_menu(), keep=True)
    elif payload == "feature_check":
        await context.set_state(States.check_awaiting_task)
        await edit_menu(chat_id, context, texts.CHECK_INTRO, keyboards.check_task(), keep=True)
    elif payload == "feature_quiz":
        await edit_menu(chat_id, context, texts.QUIZ_SOURCE, keyboards.quiz_source())
    elif payload == "quiz_topic":
        await context.update_data(
            flow="quiz", fields=list(QUIZ_FIELDS), answers={}, fields_total=len(QUIZ_FIELDS), step_intro=texts.QUIZ_INTRO,
        )
        await ask_next_field(chat_id, context)
    elif payload == "quiz_material":
        await context.update_data(material=None, material_loaded_at=None, after_load="test")
        await context.set_state(States.search_upload)
        await edit_menu(chat_id, context, texts.QUIZ_MATERIAL_INTRO, keyboards.to_menu(), keep=True)
    elif payload == "feature_examples":
        await send_examples(chat_id)
        await back_to_menu(chat_id, context)
    elif payload == "feature_favorites":
        await show_favorites(chat_id, context)
    elif payload == "feature_idea":
        idea = IDEA_OF_DAY_LIST[datetime.date.today().toordinal() % len(IDEA_OF_DAY_LIST)]
        await bot.send_message(chat_id=chat_id, text=f"🎲 <b>Идея дня</b>\n\n{idea}")
        await back_to_menu(chat_id, context)
    elif payload == "feature_help":
        await bot.send_message(chat_id=chat_id, text=about_with_link())
        await back_to_menu(chat_id, context)


async def send_examples(chat_id):
    await bot.send_message(chat_id=chat_id, text="📚 <b>Примеры промтов из библиотеки</b>")
    for example in EXAMPLE_LIBRARY:
        await bot.send_message(
            chat_id=chat_id,
            text=f"<b>{html.escape(example['title'])}</b>\n\n<pre>{html.escape(example['text'])}</pre>",
        )


# ---------- вопрос ассистенту ----------

@dp.message_created(States.asking, F.message.body.text)
async def ask_assistant(event: MessageCreated, context: BaseContext):
    chat_id = event.message.recipient.chat_id
    question = event.message.body.text.strip()
    if not question:
        return
    data = await context.get_data()
    lib = keyboards.library(data["user_type"])
    system_prompt = lib["system_prompts"][data["role_key"]]
    conversation = data.get("conversation") or []
    STATS["вопросов ассистенту"] += 1
    history = [(role, text) for q, a in conversation for role, text in (("user", q), ("assistant", a))]
    # в промт для другой нейросети предыдущий вопрос и ответ попадают текстом
    context_text = "\n\n".join(f"Предыдущий вопрос: {q}\nОтвет: {a}" for q, a in conversation)
    prompt_text = f"{context_text}\n\nНовый вопрос: {question}" if context_text else question
    await offer_choice(chat_id, context, make_request(
        "ask", "Вопрос ассистенту", "📄 <b>Промт с вашим вопросом</b>", "💬 Ответ ассистента",
        system_prompt, question, history=history, prompt_text=prompt_text, question=question,
    ))


# ---------- учебный материал: тип → предмет → поля ----------

@dp.message_callback(States.selecting_task)
async def select_task(event: MessageCallback, context: BaseContext):
    payload = event.callback.payload
    user_type = (await context.get_data())["user_type"]
    task = keyboards.library(user_type)["user_templates"].get(payload)
    if not task:
        return
    await context.update_data(task_key=payload)
    await context.set_state(States.selecting_subject)
    emoji = keyboards.task_emoji(user_type).get(payload, "📌")
    await edit_menu(
        callback_chat(event), context,
        f"{emoji} <b>{html.escape(task['name'])}</b>\n\nПредмет?",
        keyboards.subject_list(user_type),
    )


@dp.message_callback(States.selecting_subject)
async def select_subject(event: MessageCallback, context: BaseContext):
    chat_id = callback_chat(event)
    payload = event.callback.payload
    data = await context.get_data()
    user_type = data["user_type"]

    if payload == "back_to_task":
        await context.set_state(States.selecting_task)
        await edit_menu(chat_id, context, texts.CHOOSE_TASK, keyboards.tasks(user_type))
        return
    if payload not in keyboards.subjects(user_type):
        return

    task = keyboards.library(user_type)["user_templates"][data["task_key"]]
    emoji, name = keyboards.subjects(user_type)[payload]
    await context.update_data(
        subject_key=payload, flow="prompt", fields=list(task["required_fields"]), answers={},
        fields_total=len(task["required_fields"]),
        step_intro=f"{emoji} <b>{html.escape(task['name'])}</b> · {html.escape(name)}",
    )
    await ask_next_field(chat_id, context)


async def ask_next_field(chat_id, context):
    data = await context.get_data()
    fields = data.get("fields") or []
    if not fields:
        if data.get("flow") == "quiz":
            await make_quiz(chat_id, context)
        else:
            await make_prompt(chat_id, context)
        return
    step = data["fields_total"] - len(fields) + 1
    intro = data.get("step_intro")
    text = f"{html.escape(field_question(data, fields[0]))}\n\n<i>Шаг {step} из {data['fields_total']}</i>"
    if intro:
        text = f"{intro}\n\n{text}"
        await context.update_data(step_intro=None)
    await context.set_state(States.awaiting_info)
    # каждый вопрос — новым сообщением под ответом пользователя; у прежнего снимаются кнопки
    await send_menu(chat_id, context, text, keyboards.step(fields[0], data.get("last_class")), keep=True)


def field_question(data, field):
    """Вопрос для поля: из общего списка или из самого шаблона (ключ "questions"),
    так новый сценарий в prompts.py может вводить свои поля без правки кода."""
    if field in texts.FIELD_QUESTIONS:
        return texts.FIELD_QUESTIONS[field]
    template = keyboards.library(data["user_type"])["user_templates"].get(data.get("task_key"), {})
    return template.get("questions", {}).get(field, f"{field}?")


def parse_field(field, text):
    """Проверяет ответ пользователя. Возвращает значение или None, если ответ не подходит."""
    text = text.strip()
    if field in ("class_level", "duration", "num_questions"):
        match = re.search(r"\d+", text)
        number = int(match.group()) if match else 0
        low, high = {"class_level": (1, 11), "duration": (1, 240), "num_questions": (1, 30)}[field]
        return str(number) if low <= number <= high else None
    if field == "quiz_type":
        lowered = text.lower()
        if "откр" in lowered:
            return "открытые"
        if "тест" in lowered or "вариант" in lowered:
            return "тест"
        return None
    return text or None


async def accept_field(chat_id, context, value_text):
    data = await context.get_data()
    field = data["fields"][0]
    value = parse_field(field, value_text)
    if value is None:
        await bot.send_message(chat_id=chat_id, text=texts.FIELD_ERRORS.get(field, "Пришлите непустой ответ."))
        return
    answers = dict(data["answers"], **{field: value})
    await context.update_data(fields=data["fields"][1:], answers=answers)
    if field == "class_level":
        await context.update_data(last_class=value)
    await ask_next_field(chat_id, context)


@dp.message_created(States.awaiting_info, F.message.body.text)
async def on_field_text(event: MessageCreated, context: BaseContext):
    await accept_field(event.message.recipient.chat_id, context, event.message.body.text or "")


@dp.message_callback(States.awaiting_info)
async def on_field_button(event: MessageCallback, context: BaseContext):
    payload = event.callback.payload or ""
    if payload.startswith(("quiz_type:", "class:")):
        await accept_field(callback_chat(event), context, payload.split(":", 1)[1])


def build_prompt(data):
    lib = keyboards.library(data["user_type"])
    system_prompt = lib["system_prompts"][data["role_key"]]
    subject_prompt = lib["subject_prompts"].get(data["subject_key"])
    if subject_prompt:
        system_prompt = f"{system_prompt}\n\nПредметная специализация:\n{subject_prompt}"
    user_prompt = lib["user_templates"][data["task_key"]]["template"].format(**data["answers"])
    return system_prompt, user_prompt


async def make_prompt(chat_id, context):
    STATS["подготовлено материалов"] += 1
    data = await context.get_data()
    system_prompt, user_prompt = build_prompt(data)
    task = keyboards.library(data["user_type"])["user_templates"][data["task_key"]]
    topic = data["answers"].get("topic")
    title = f"{keyboards.task_emoji(data['user_type']).get(data['task_key'], '📌')} {task['name']}" + (f": {topic}" if topic else "")
    await offer_choice(chat_id, context, make_request(
        "material", "Учебный материал", "📄 <b>Промт готов</b>", title, system_prompt, user_prompt,
        max_tokens=MATERIAL_MAX_TOKENS,
    ))


async def make_quiz(chat_id, context):
    data = await context.get_data()
    answers = data["answers"]
    system_prompt = QUIZ_SYSTEM_PROMPTS[data["user_type"]]
    user_prompt = QUIZ_USER_TEMPLATE.format(
        quiz_type_text=QUIZ_TYPE_TEXT[answers["quiz_type"]], topic=answers["topic"],
        class_level=answers["class_level"], num_questions=answers["num_questions"],
    )
    STATS["тестов по теме"] += 1
    await offer_choice(chat_id, context, make_request(
        "quiz", "Тест по теме", "📄 <b>Промт для теста</b>", f"🧩 Тест по теме «{answers['topic']}»",
        system_prompt, user_prompt, max_tokens=QUIZ_MAX_TOKENS,
    ))


# ---------- проверка ответа ----------

@dp.message_callback(States.check_awaiting_task)
async def check_without_task(event: MessageCallback, context: BaseContext):
    if event.callback.payload != "check_skip_task":
        return
    await context.update_data(check_task=None)
    await context.set_state(States.check_awaiting_answer)
    await send_menu(callback_chat(event), context, texts.CHECK_SEND_ANSWER, keyboards.to_menu(), keep=True)


@dp.message_created(States.check_awaiting_task, F.message.body.text)
async def check_task_text(event: MessageCreated, context: BaseContext):
    await context.update_data(check_task=event.message.body.text.strip())
    await context.set_state(States.check_awaiting_answer)
    await send_menu(event.message.recipient.chat_id, context, texts.CHECK_SEND_ANSWER, keyboards.to_menu(), keep=True)


@dp.message_created(States.check_awaiting_answer, F.message.body.text)
async def check_answer_text(event: MessageCreated, context: BaseContext):
    chat_id = event.message.recipient.chat_id
    answer = event.message.body.text.strip()
    if not answer:
        await event.message.answer("Пришлите текст ответа.")
        return
    data = await context.get_data()
    task = data.get("check_task")
    system_prompt = CHECK_SYSTEM_PROMPTS[data["user_type"]]
    user_prompt = CHECK_USER_TEMPLATE.format(
        task=task or "не указаны, оцени по общим требованиям к работе такого типа", answer=answer,
    )
    STATS["проверено ответов"] += 1
    await bot.send_message(chat_id=chat_id, text=materials.quick_check(task, answer))
    await offer_choice(chat_id, context, make_request(
        "check", "Проверка ответа", "📄 <b>Промт для проверки</b>", "✅ Разбор ответа", system_prompt, user_prompt,
    ))


# ---------- поиск по материалу ----------

_cleanup_tasks = set()


async def forget_material_later(context, loaded_at):
    """Удаляет материал из памяти по истечении срока, даже если пользователь больше не пишет."""
    await asyncio.sleep(materials.TTL_SECONDS)
    if (await context.get_data()).get("material_loaded_at") == loaded_at:
        await context.update_data(material=None, material_loaded_at=None)
        logger.info("Material expired and was removed from memory")


async def load_material(chat_id, context, text):
    chunks = materials.split_paragraphs(text)
    if not chunks:
        await bot.send_message(chat_id=chat_id, text=texts.SEARCH_NO_TEXT)
        return
    loaded_at = time.time()
    await context.update_data(material=chunks, material_loaded_at=loaded_at)
    await context.set_state(States.search_question)
    task = asyncio.create_task(forget_material_later(context, loaded_at))
    _cleanup_tasks.add(task)
    task.add_done_callback(_cleanup_tasks.discard)

    paragraphs = len({number for number, _ in chunks})
    note = ""
    if len(text.strip()) > materials.MAX_CHARS:
        note = f"\nМатериал длинный, взяты первые {materials.MAX_CHARS:,} символов.".replace(",", " ")
    data = await context.get_data()
    if data.get("after_load") == "test":
        await context.update_data(after_load=None)
        await bot.send_message(chat_id=chat_id, text=f"📚 Материал загружен: абзацев — {paragraphs}.{note}")
        await send_material_test(chat_id, context, chunks)
        return
    await send_menu(
        chat_id, context,
        f"📚 Материал загружен: абзацев — {paragraphs}.{note}\n\nЗадайте вопрос по нему или составьте тест.",
        keyboards.material_menu(), keep=True,
    )


async def send_material_test(chat_id, context, chunks):
    questions = materials.make_test(chunks)
    if len(questions) < 3:
        await send_menu(chat_id, context, texts.TEST_TOO_SHORT, keyboards.material_menu())
        return
    STATS["тестов по материалу"] += 1
    body, key = materials.format_test(questions)
    title = f"Тест по материалу ({len(questions)} вопросов)"
    await send_plain(chat_id, f"🧩 {title}\n\n{body}")
    await send_plain(chat_id, f"🔑 Ключ ответов: {key}")
    await context.update_data(last_doc={"title": title, "body": body, "key": key})
    await send_menu(chat_id, context, texts.TEST_READY, keyboards.material_menu(keyboards.result_rows(docx=True, feedback=True)))


@dp.message_callback(States.search_question)
async def on_material_button(event: MessageCallback, context: BaseContext):
    if event.callback.payload != "material_test":
        return
    chat_id = callback_chat(event)
    data = await context.get_data()
    chunks = data.get("material")
    if not chunks or time.time() - (data.get("material_loaded_at") or 0) > materials.TTL_SECONDS:
        await context.update_data(material=None, material_loaded_at=None)
        await context.set_state(States.search_upload)
        await send_menu(chat_id, context, texts.SEARCH_EXPIRED, keyboards.to_menu(), keep=True)
        return
    await send_material_test(chat_id, context, chunks)


@dp.message_created(States.search_upload, States.search_question, F.message.body.attachments)
async def search_file(event: MessageCreated, context: BaseContext):
    chat_id = event.message.recipient.chat_id
    document = next((a for a in event.message.body.attachments if a.type == AttachmentType.FILE), None)
    if document is None:
        await event.message.answer(texts.SEARCH_BAD_FORMAT)
        return
    try:
        data = await bot.download_bytes(document.payload.url)
        text = materials.extract_text(document.filename or "", data)
    except ValueError:
        await event.message.answer(texts.SEARCH_BAD_FORMAT)
        return
    except Exception as e:
        logger.error(f"Cannot read uploaded file: {e}")
        await event.message.answer(texts.SEARCH_READ_ERROR)
        return
    if not text.strip():
        await event.message.answer(texts.SEARCH_NO_TEXT)
        return
    await load_material(chat_id, context, text)


@dp.message_created(States.search_upload, F.message.body.text)
async def search_text(event: MessageCreated, context: BaseContext):
    text = event.message.body.text.strip()
    if len(text) < 30:
        await event.message.answer(texts.SEARCH_TOO_SHORT)
        return
    await load_material(event.message.recipient.chat_id, context, text)


@dp.message_created(States.search_question, F.message.body.text)
async def search_question(event: MessageCreated, context: BaseContext):
    chat_id = event.message.recipient.chat_id
    data = await context.get_data()
    chunks = data.get("material")
    loaded_at = data.get("material_loaded_at") or 0
    if not chunks or time.time() - loaded_at > materials.TTL_SECONDS:
        await context.update_data(material=None, material_loaded_at=None)
        await context.set_state(States.search_upload)
        await send_menu(chat_id, context, texts.SEARCH_EXPIRED, keyboards.to_menu(), keep=True)
        return

    question = event.message.body.text.strip()
    found = materials.search([text for _, text in chunks], question)
    if not found:
        await event.message.answer(texts.SEARCH_NOT_FOUND)
        return

    STATS["поисков по материалу"] += 1
    lines = ["🔎 <b>Нашёл в материале</b>"]
    for i, chunk, score in found:
        quote = html.escape(materials.shorten(chunk, materials.QUOTE_MAX_CHARS))
        lines.append(f"\n<b>Абзац {chunks[i][0]}</b> · совпадение {score:.0%}\n«{quote}»")
    await bot.send_message(chat_id=chat_id, text="\n".join(lines))

    fragments = "\n\n".join(f"[Абзац {chunks[i][0]}] {chunk}" for i, chunk, _ in found)
    user_prompt = SEARCH_USER_TEMPLATE.format(question=question, fragments=fragments)
    await offer_choice(chat_id, context, make_request(
        "search", "Ответ по материалу", "📄 <b>Промт с найденными абзацами</b>", "💬 Ответ по материалу",
        SEARCH_SYSTEM_PROMPT, user_prompt,
    ))


# ---------- всё остальное ----------

@dp.message_created()
async def fallback(event: MessageCreated, context: BaseContext):
    if await context.get_state() is None:
        await send_menu(event.message.recipient.chat_id, context, texts.NEED_START, keyboards.resume(await has_role(context)))
    else:
        await event.message.answer(texts.PRESS_BUTTON)


@dp.message_callback()
async def stale_button(event: MessageCallback):
    # кнопка из старого сообщения, которая в текущем шаге ничего не делает
    pass


@dp.errors()
async def on_error(event: ErrorEvent):
    logger.error(f"Unhandled error: {event.exception!r}", exc_info=event.exception)
    chat_id = event.update.get_ids()[0]
    if not chat_id:
        return
    try:
        if event.context is not None:
            await send_menu(chat_id, event.context, texts.SOMETHING_WRONG, keyboards.resume(True))
        else:
            await bot.send_message(chat_id=chat_id, text=texts.SOMETHING_WRONG, attachments=[keyboards.resume(True)])
    except Exception:
        pass


COMMANDS = [
    BotCommand(name="start", description="Начать заново"),
    BotCommand(name="help", description="Как пользоваться"),
    BotCommand(name="info", description="О проекте"),
    BotCommand(name="cancel", description="Отменить действие"),
    BotCommand(name="stats", description="Статистика использования"),
]


async def run():
    if await check_llm_health():
        logger.info("Нейросеть подключена: ответы приходят прямо в бот")
    else:
        logger.info("Нейросеть не подключена: бот выдаёт промты и ссылки на бесплатные чаты")
    try:
        await bot.set_commands(*COMMANDS)
    except Exception as e:
        logger.warning(f"Cannot set bot commands: {e}")
    health = asyncio.create_task(llm_health_loop())
    try:
        await dp.start_polling(bot)
    finally:
        health.cancel()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
