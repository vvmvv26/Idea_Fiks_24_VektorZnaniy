"""Инлайн-клавиатуры бота.

В чате всегда одно активное меню, поэтому кнопки результата (скачать, оценить, бесплатные
чаты, «проще/подробнее») не живут отдельным сообщением, а добавляются рядами сверху к меню
следующего шага. Функции *_rows возвращают ряды кнопок, build() собирает из них клавиатуру.
"""
import re

from maxapi.types.attachments.buttons.callback_button import CallbackButton
from maxapi.types.attachments.buttons.link_button import LinkButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from llm_client import is_llm_configured
from prompts import (
    ROLE_SUBROLES, STUDENT_PROMPT_LIBRARY, STUDENT_SUBJECTS, STUDENT_TASK_EMOJI,
    TEACHER_PROMPT_LIBRARY, TEACHER_SUBJECTS, TEACHER_TASK_EMOJI,
)

FREE_CHATS = [
    ("🤖 ChatGPT", "https://chat.openai.com"),
    ("🔵 GigaChat", "https://giga.chat"),
    ("🎯 YandexGPT", "https://alice.yandex.ru/"),
    ("🐋 DeepSeek", "https://chat.deepseek.com"),
]

TO_MENU = CallbackButton(text="⬅️ В меню", payload="to_menu")
CANCEL = CallbackButton(text="❌ Отмена", payload="cancel")


def library(user_type):
    return TEACHER_PROMPT_LIBRARY if user_type == "teacher" else STUDENT_PROMPT_LIBRARY


def task_emoji(user_type):
    return TEACHER_TASK_EMOJI if user_type == "teacher" else STUDENT_TASK_EMOJI


def subjects(user_type):
    return TEACHER_SUBJECTS if user_type == "teacher" else STUDENT_SUBJECTS


def build(*row_groups):
    kb = InlineKeyboardBuilder()
    for rows in row_groups:
        for row in rows or []:
            kb.row(*row)
    return kb.as_markup()


def choice_rows(wait=""):
    """Данные собраны: ответ здесь или промт для другой нейросети."""
    label = f"🤖 Ответ здесь ({wait})" if wait else "🤖 Ответ здесь"
    return [
        [CallbackButton(text=label, payload="ai_generate_now")],
        [CallbackButton(text="📋 Промт для другой нейросети", payload="show_prompt")],
    ]


def result_rows(*, docx=False, feedback=False, followups=False, ai_now=False, chats=False, favorite=False, other_ai=False):
    """Кнопки под готовым результатом."""
    rows = []
    if docx:
        rows.append([CallbackButton(text="📄 Скачать файл Word", payload="export_docx")])
    if followups:
        rows.append([
            CallbackButton(text="🧒 Проще", payload="fu:simple"),
            CallbackButton(text="➕ Подробнее", payload="fu:more"),
            CallbackButton(text="🔁 Пример", payload="fu:example"),
        ])
    if ai_now and is_llm_configured():
        rows.append([CallbackButton(text="🤖 Ответ здесь", payload="ai_generate_now")])
    if other_ai:
        rows.append([CallbackButton(text="📋 Промт для другой нейросети", payload="show_prompt")])
    if chats:
        rows.append([LinkButton(text=name, url=url) for name, url in FREE_CHATS[:2]])
        rows.append([LinkButton(text=name, url=url) for name, url in FREE_CHATS[2:]])
    if favorite:
        rows.append([CallbackButton(text="⭐ В избранное", payload="add_favorite")])
    if feedback:
        rows.append([
            CallbackButton(text="👍 Полезно", payload="fb:up"),
            CallbackButton(text="👎 Не помогло", payload="fb:down"),
        ])
    return rows


def roles():
    return build([
        [CallbackButton(text="👩‍🏫 Я учитель", payload="role_teacher")],
        [CallbackButton(text="🎓 Я ученик", payload="role_student")],
    ])


def subroles(user_type):
    rows = [[CallbackButton(text=f"{emoji} {name}", payload=key)] for key, emoji, name in ROLE_SUBROLES[user_type]]
    rows.append([CallbackButton(text="⬅️ Назад", payload="back_to_role"), CANCEL])
    return build(rows)


def main_menu(extra_rows=None):
    return build(extra_rows, [
        [CallbackButton(text="💬 Спросить ассистента", payload="feature_ask")],
        [
            CallbackButton(text="📝 Учебный материал", payload="feature_generate"),
            CallbackButton(text="🧩 Тест", payload="feature_quiz"),
        ],
        [
            CallbackButton(text="🔎 Поиск по материалу", payload="feature_search"),
            CallbackButton(text="✅ Проверка ответа", payload="feature_check"),
        ],
        [
            CallbackButton(text="📚 Примеры", payload="feature_examples"),
            CallbackButton(text="⭐ Избранное", payload="feature_favorites"),
        ],
        [
            CallbackButton(text="🎲 Идея дня", payload="feature_idea"),
            CallbackButton(text="❓ О проекте", payload="feature_help"),
        ],
        [CallbackButton(text="🔄 Сменить роль", payload="back_to_subrole")],
    ])


def tasks(user_type):
    emoji = task_emoji(user_type)
    rows = [
        [CallbackButton(text=f"{emoji.get(key, '📌')} {task['name']}", payload=key)]
        for key, task in library(user_type)["user_templates"].items()
    ]
    rows.append([TO_MENU])
    return build(rows)


def subject_list(user_type):
    items = list(subjects(user_type).items())
    # по два предмета в ряд, чтобы список из 16 строк не уезжал за экран
    rows = [
        [CallbackButton(text=f"{emoji} {name}", payload=key) for key, (emoji, name) in items[i:i + 2]]
        for i in range(0, len(items), 2)
    ]
    rows.append([CallbackButton(text="⬅️ Назад", payload="back_to_task"), TO_MENU])
    return build(rows)


def step(field, last_class=None):
    rows = []
    if field == "class_level" and last_class:
        rows.append([CallbackButton(text=f"{last_class} класс, как в прошлый раз", payload=f"class:{last_class}")])
    if field == "quiz_type":
        rows.append([CallbackButton(text="С вариантами ответа", payload="quiz_type:тест")])
        rows.append([CallbackButton(text="Открытые вопросы", payload="quiz_type:открытые")])
    rows.append([TO_MENU])
    return build(rows)


def check_task():
    return build([[CallbackButton(text="⏭ Без критериев", payload="check_skip_task")], [TO_MENU]])


def material_menu(extra_rows=None):
    return build(extra_rows, [[CallbackButton(text="🧩 Тест по этому материалу", payload="material_test")], [TO_MENU]])


def quiz_source():
    return build([
        [CallbackButton(text="📎 По моему материалу", payload="quiz_material")],
        [CallbackButton(text="📝 По теме", payload="quiz_topic")],
        [TO_MENU],
    ])


def favorite_title(fav, limit=34):
    title = fav.get("title") or " ".join(fav["user"].split())
    title = re.sub(r"^[^\w«(]+", "", title)
    return title if len(title) <= limit else title[:limit - 1].rstrip() + "…"


def favorites_menu(favorites):
    """Список избранного: открыть промт или удалить его."""
    rows = [
        [
            CallbackButton(text=f"{i}. {favorite_title(fav)}", payload=f"fav:open:{i - 1}"),
            CallbackButton(text="🗑", payload=f"fav:del:{i - 1}"),
        ]
        for i, fav in enumerate(favorites, start=1)
    ]
    if favorites:
        rows.append([CallbackButton(text="🗑 Очистить всё", payload="fav:clear")])
    rows.append([TO_MENU])
    return build(rows)


def role_title(user_type, role_key):
    who = "👩‍🏫 Учитель" if user_type == "teacher" else "🎓 Ученик"
    name = next((n for key, _, n in ROLE_SUBROLES.get(user_type, []) if key == role_key), "")
    return f"{who} · {name.replace(' (рекомендуется)', '')}" if name else who


def resume(has_role):
    """Кнопки после отмены или ошибки: вернуться к работе без ввода /start."""
    if has_role:
        return build([
            [CallbackButton(text="▶️ Продолжить", payload="to_menu")],
            [CallbackButton(text="🔄 Начать заново", payload="restart")],
        ])
    return build([[CallbackButton(text="▶️ Начать", payload="restart")]])


def to_menu(extra_rows=None):
    return build(extra_rows, [[TO_MENU]])
