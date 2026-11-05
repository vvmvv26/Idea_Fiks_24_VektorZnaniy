"""Стенд: прогон всех сценариев бота через настоящий диспетчер maxapi без сети и без токена.
API МАКС подменён записью отправленных сообщений, Ollama — маленьким OpenAI-совместимым сервером.

Запуск из папки bot:  python tests/smoke_test.py"""
import asyncio
import io
import os
import re
import sys
import time
from html.parser import HTMLParser
from types import SimpleNamespace

os.environ["MAX_BOT_TOKEN"] = "test-token"
os.environ["LLM_API_KEY"] = ""  # python-dotenv не перезапишет уже заданную переменную из .env
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vektor_znaniy_max_bot as vb  # noqa: E402
import materials  # noqa: E402
import llm_client  # noqa: E402
from maxapi.methods.types.getted_updates import process_update_webhook  # noqa: E402
from maxapi.types.message import Message  # noqa: E402
from maxapi.types.updates.message_callback import MessageCallback  # noqa: E402
from aiohttp import web  # noqa: E402

SENT = []
FILES = []
_mid = [0]
FAILS = []


class StrictHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack, self.bad = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ("b", "pre", "i"):
            self.bad.append(f"tag <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.bad.append(f"unbalanced </{tag}>")


def check_html(text):
    if text is None:
        return
    p = StrictHTML()
    p.feed(text)
    p.close()
    if p.stack:
        p.bad.append(f"unclosed {p.stack}")
    if re.search(r"&(?!(amp|lt|gt|quot|#x27|#\d+);)", text):
        p.bad.append("raw &")
    if len(text) > 4000:
        p.bad.append(f"too long {len(text)}")
    if p.bad:
        FAILS.append(f"HTML problem {p.bad}: {text[:120]!r}")


def kb_payloads(attachments):
    out = []
    for a in attachments or []:
        try:
            for row in a.payload.buttons:
                for b in row:
                    out.append(getattr(b, "payload", None) or getattr(b, "url", None))
        except AttributeError:
            pass
    return out


ACTIVE = {}    # mid -> есть ли у сообщения кнопки
DELETED = []
TEXTS = {}


async def fake_send(chat_id=None, text=None, attachments=None, **kw):
    check_html(text)
    _mid[0] += 1
    mid = f"mid{_mid[0]}"
    for a in attachments or []:
        if hasattr(a, "buffer"):
            FILES.append(a)
    buttons = kb_payloads(attachments)
    if buttons:
        ACTIVE[mid] = True
    TEXTS[mid] = text or ""
    SENT.append({"kind": "send", "text": text or "", "buttons": buttons, "mid": mid})
    return SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(mid=mid)))


async def fake_edit(message_id=None, text=None, attachments=None, **kw):
    check_html(text)
    if not text:
        FAILS.append(f"edit_message без текста ({message_id}): API может стереть сообщение")
    buttons = kb_payloads(attachments)
    if buttons:
        ACTIVE[message_id] = True
    else:
        ACTIVE.pop(message_id, None)
    TEXTS[message_id] = text or ""
    SENT.append({"kind": "edit", "text": text or "", "buttons": buttons, "mid": message_id})


async def fake_delete(message_id, *a, **kw):
    ACTIVE.pop(message_id, None)
    DELETED.append(message_id)


async def fake_answer(self, text=None, attachments=None, **kw):
    return await fake_send(text=text, attachments=attachments)


async def fake_ack(self, notification=None, *a, **kw):
    # как настоящий API МАКС: ответ без сообщения и уведомления — ошибка 400
    if not notification:
        FAILS.append("пустой ack(): API МАКС отвечает 400 proto.payload")


DOWNLOADS = {}


async def fake_download(url, *a, **kw):
    return DOWNLOADS[url]


vb.bot.send_message = fake_send
vb.bot.edit_message = fake_edit
vb.bot.download_bytes = fake_download
vb.bot.delete_message = fake_delete

ACTIONS = []


async def fake_action(chat_id=None, action=None, **kw):
    ACTIONS.append(action)


vb.bot.send_action = fake_action
Message.answer = fake_answer
MessageCallback.ack = fake_ack
vb.dp.bot = vb.bot


async def fake_get_chat(chat_id, *a, **kw):
    return None


vb.bot.get_chat_by_id = fake_get_chat


async def fake_get_me(*a, **kw):
    return SimpleNamespace(username='vektor_test_bot', first_name='Вектор', user_id=1)


vb.bot.get_me = fake_get_me

USER = {"user_id": 7, "first_name": "Тест", "is_bot": False, "last_activity_time": 0}
_seq = [0]


def msg(text=None, attachments=None):
    _seq[0] += 1
    return {
        "sender": USER, "recipient": {"chat_id": 100, "chat_type": "dialog", "user_id": 7},
        "timestamp": int(time.time() * 1000),
        "body": {"mid": f"u{_seq[0]}", "seq": _seq[0], "text": text, "attachments": attachments or []},
    }


async def feed(update):
    ev = await process_update_webhook(update, vb.bot)
    assert ev is not None, f"не распознано: {update['update_type']}"
    await vb.dp.handle(ev)


def check_single_menu(event):
    if len(ACTIVE) > 1:
        FAILS.append(f"после «{event}» активных меню: {len(ACTIVE)} — " + " | ".join(TEXTS.get(m, "")[:40] for m in ACTIVE))


async def text(t):
    SENT.clear()
    await feed({"update_type": "message_created", "timestamp": int(time.time() * 1000), "message": msg(t)})
    check_single_menu(t[:30])
    return list(SENT)


async def file(name, data):
    SENT.clear()
    url = f"https://files.example/{name}"
    DOWNLOADS[url] = data
    att = [{"type": "file", "filename": name, "size": len(data), "payload": {"url": url, "token": "t"}}]
    await feed({"update_type": "message_created", "timestamp": int(time.time() * 1000), "message": msg(None, att)})
    check_single_menu(name)
    return list(SENT)


async def press(payload):
    SENT.clear()
    await feed({
        "update_type": "message_callback", "timestamp": int(time.time() * 1000),
        "callback": {"timestamp": int(time.time() * 1000), "callback_id": "cb", "payload": payload, "user": USER},
        "message": msg("menu"),
    })
    check_single_menu(payload)
    return list(SENT)


def expect(step, out, *needles, absent=()):
    joined = "\n".join(m["text"] + " " + " ".join(map(str, m["buttons"])) for m in out)
    ok = all(n in joined for n in needles) and not any(a in joined for a in absent)
    print(("OK   " if ok else "FAIL ") + step)
    if not ok:
        FAILS.append(step)
        print("     got:", joined[:900].replace("\n", " | "))


MATERIAL = """Обыкновенная дробь — это число вида a/b, где a — числитель, b — знаменатель.

Знаменатель показывает, на сколько равных частей разделили целое. Числитель показывает, сколько таких частей взяли.

Чтобы сложить дроби с одинаковыми знаменателями, складывают числители, а знаменатель оставляют прежним.

Закон Ома: сила тока на участке цепи прямо пропорциональна напряжению и обратно пропорциональна сопротивлению."""


# --- поддельная Ollama ---
REAL_MODEL_ANSWER = r"""**Вопрос 1:** Какой из следующих выражений равен значению синуса угла?

- A) $\cos(\frac{\pi}{2})$
- D) $\sin(\frac{\pi}{2})$

**Ключ ответа:** Вопрос 1: **D)**  
**Вопрос 2:** Какой угол имеет косинус $-\frac{1}{2}$?
- B) $\frac{2\pi}{3}$
а косинус -$\sqrt{\left(\sin^2(\theta)+\cos^2(\theta)\right)}$."""


LAST = []


async def bot_last():
    LAST[:] = list(SENT)


LONG_MATERIAL = """Фотосинтез — процесс образования органических веществ из углекислого газа и воды на свету.

Фотосинтез происходит в хлоропластах клеток листа. Хлоропласты содержат зелёный пигмент хлорофилл.

Хлорофилл поглощает энергию света, которая нужна растению для фотосинтеза.

В световой фазе фотосинтеза энергия света расщепляет воду, и выделяется кислород.

В темновой фазе фотосинтеза из углекислого газа образуется глюкоза, на это расходуется энергия.

Кислород, который выделяют растения, необходим для дыхания живых организмов на Земле.

Глюкоза, образованная при фотосинтезе, служит растению источником энергии и строительным материалом.

Интенсивность фотосинтеза зависит от освещённости, температуры и концентрации углекислого газа.

Растения поглощают углекислый газ через устьица — мелкие отверстия в кожице листа.

Без фотосинтеза на Земле не было бы кислорода в атмосфере и органических веществ для питания."""


LLM_REPLY = "Ответ модели <b>с разметкой</b> & символами < >"


MODELS = {"data": [{"id": "qwen2.5:3b-instruct", "object": "model", "created": 0, "owned_by": "x"}]}


async def models(request):
    return web.json_response({"object": "list", **MODELS})


LAST_REQUEST = {}


async def completions(request):
    body = await request.json()
    LAST_REQUEST.clear()
    LAST_REQUEST.update(body)
    assert body["model"] == "qwen2.5:3b-instruct"
    return web.json_response({
        "id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
        "choices": [{"index": 0, "message": {"role": "assistant", "content": LLM_REPLY}, "finish_reason": "stop"}],
    })


async def main():
    await vb.dp.startup(vb.bot)

    # ===== Без нейросети =====
    expect("сообщение до /start: кнопка «Начать»", await text("привет"), "Нажмите кнопку", "restart", absent=("to_menu",))
    expect("кнопка «Начать»", await press("restart"), "Вектор Знаний", "Кто вы", "role_teacher")
    expect("/start без нейросети: отметка", await text("/start"), "Вектор Знаний", "работаю без нейросети", "role_teacher")
    expect("роль: учитель", await press("role_teacher"), "Учитель", "methodist")
    expect("текст вместо кнопки", await text("ммм"), "кнопкой")
    expect("методист → меню", await press("methodist"), "Учитель · Методист", "Что делаем", "feature_search", "back_to_subrole")
    expect("учебный материал", await press("feature_generate"), "Что подготовить", "lesson_plan", "to_menu")
    expect("план урока", await press("lesson_plan"), "Предмет", "math", "physics")
    expect("математика", await press("math"), "Математика", "Шаг 1 из 4", "В каком классе")
    expect("класс 12 — ошибка", await text("12"), "от 1 до 11")
    expect("класс «5 класс»", await text("5 класс"), "Шаг 2 из 4", "Тема")
    expect("тема с HTML", await text("Дроби <b>&amp; доли"), "Шаг 3 из 4")
    expect("тип урока", await text("изучение нового"), "Шаг 4 из 4")
    expect("длительность словами — ошибка", await text("сорок пять"), "минут числом")
    out = await text("45 минут")
    expect("промт готов, без кнопки нейросети", out, "Промт готов", "Дроби &lt;b&gt;&amp;amp; доли", "Длительность: 45 минут",
           "https://giga.chat", "add_favorite", "Скопируйте промт", "feature_ask", absent=("ai_generate_now",))
    expect("в избранное из главного меню", await press("add_favorite"), "Добавлено в избранное", "всего: 1")
    expect("повторно в избранное", await press("add_favorite"), "уже в избранном")
    expect("список избранного", await press("feature_favorites"), "Избранное</b> (1)", "fav:open:0", "fav:del:0", "fav:clear")
    expect("избранное: открыть промт", await press("fav:open:0"), "Дроби", "СИСТЕМНЫЙ ПРОМТ", "https://giga.chat", "feature_favorites")
    expect("избранное: назад к списку", await press("feature_favorites"), "Избранное</b> (1)")
    expect("избранное: несуществующий номер", await press("fav:open:9"), "уже нет в избранном", "fav:open:0")
    await press("to_menu")
    expect("примеры", await press("feature_examples"), "Примеры промтов", "Закон Ома")
    idea1, idea2 = await press("feature_idea"), await press("feature_idea")
    expect("идея дня одна на день", idea1, "Идея дня")
    if idea1[0]["text"] != idea2[0]["text"]:
        FAILS.append("идея дня меняется в течение дня")
    expect("о проекте", await press("feature_help"), "24 педагога и 56 учеников", "Вектор 26", absent=("91",))
    expect("/help", await text("/help"), "Как пользоваться", "не подключена")
    expect("/помощь", await text("/помощь"), "Как пользоваться")
    expect("/info", await text("/info"), "Портал в будущее")
    expect("старая кнопка", await press("check_skip_task"), absent=("Что-то пошло не так",))

    # выход в меню посреди ввода полей
    await press("feature_generate")
    await press("explain_topic")
    await press("biology")
    expect("в меню с шага ввода", await press("to_menu"), "Что дальше")
    expect("после выхода снова меню", await press("feature_idea"), "Идея дня")

    # поиск: текст
    expect("поиск: начало", await press("feature_search"), "Поиск по материалу", "to_menu")
    expect("поиск: короткий текст", await text("мало"), "Слишком короткий")
    expect("поиск: материал", await text(MATERIAL), "абзацев — 4")
    out = await text("Что показывает знаменатель дроби?")
    expect("поиск без нейросети: абзац 2", out, "Абзац 2", "на сколько равных частей", "Промт с найденными абзацами", "to_menu")
    first = next(m["text"] for m in out if "Нашёл" in m["text"])
    if first.split("Абзац")[1].split("<")[0].strip() != "2":
        FAILS.append("поиск: лучший абзац не первый")
    expect("поиск: в избранное", await press("add_favorite"), "Добавлено в избранное")
    expect("поиск: нет совпадений", await text("Кто написал Войну и мир?"), "ничего похожего")
    expect("поиск: новый файл вместо старого", await file("ома.txt", "Закон Ома.\n\nСила тока равна напряжению, делённому на сопротивление.".encode()), "абзацев — 2")
    expect("поиск: вопрос по новому файлу", await text("сила тока сопротивление"), "Абзац 2")
    expect("поиск: в меню", await press("to_menu"), "Что дальше")

    # поиск: файлы
    from docx import Document
    d = Document()
    for para in MATERIAL.split("\n\n"):
        d.add_paragraph(para)
    buf = io.BytesIO()
    d.save(buf)
    await press("feature_search")
    expect("поиск: docx", await file("конспект.docx", buf.getvalue()), "абзацев — 4")
    expect("поиск: закон Ома", await text("Закон Ома сопротивление"), "Абзац 4")
    await press("to_menu")
    await press("feature_search")
    expect("поиск: txt в cp1251", await file("a.txt", MATERIAL.encode("cp1251")), "абзацев — 4")
    expect("поиск: cp1251 читается", await text("знаменатель показывает"), "Абзац 2")
    await press("to_menu")
    await press("feature_search")
    expect("поиск: неверный формат", await file("a.exe", b"MZ"), "Подходят только")
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(100, 100)
    pb = io.BytesIO()
    w.write(pb)
    expect("поиск: pdf без текста", await file("scan.pdf", pb.getvalue()), "нет текста")
    expect("поиск: длинный материал", await text(("Абзац про дроби и числа. " * 40 + "\n\n") * 30), "взяты первые 20 000")

    # срок хранения материала
    await text(MATERIAL)
    materials.TTL_SECONDS = 0
    expect("поиск: срок истёк", await text("знаменатель"), "Материал удалён")
    materials.TTL_SECONDS = 0.2
    await text(MATERIAL)
    await asyncio.sleep(0.5)
    expect("поиск: удаление в фоне", await text("знаменатель"), "Материал удалён")
    materials.TTL_SECONDS = 30 * 60
    await press("to_menu")

    # проверка ответа
    expect("проверка: начало", await press("feature_check"), "Проверка ответа", "check_skip_task", "to_menu")
    expect("проверка: задание", await text("Объясните, что такое числитель и знаменатель дроби"), "пришлите ответ")
    out = await text("Числитель это верхнее число. Числитель числитель числитель показывает части.")
    expect("проверка: показатели и промт", out, "Быстрая проверка", "Ключевые слова задания в ответе", "знаменатель",
           "Часто повторяется: числитель", "Промт для проверки", "Скопируйте промт", "fb:up")
    await press("feature_check")
    await press("check_skip_task")
    expect("проверка: без критериев", await text("Короткий ответ."), "очень короткий", "Промт для проверки")

    # тест
    expect("тест: выбор источника", await press("feature_quiz"), "По моему материалу", "quiz_material", "quiz_topic")
    expect("тест по теме: начало", await press("quiz_topic"), "Тест или викторина", "Шаг 1 из 4")
    await text("Пётр I")
    expect("тест: класс словом — ошибка", await text("седьмой"), "от 1 до 11")
    await text("7")
    expect("тест: 50 вопросов — ошибка", await text("50"), "от 1 до 30")
    expect("тест: кнопки типа", await text("10"), "quiz_type:тест", "quiz_type:открытые")
    expect("тест: неверный тип", await text("эссе"), "кнопкой")
    expect("тест: тип кнопкой", await press("quiz_type:открытые"), "Нейросеть к боту сейчас не подключена",
           "Промт для теста", "Количество вопросов: 10", "открытых вопросов")
    expect("кнопка нейросети без модели", await press("ai_generate_now"), "не подключена")

    expect("/cancel: кнопки", await text("/cancel"), "Отменено", "to_menu", "restart")
    expect("после отмены — текст", await text("что-нибудь"), "Нажмите кнопку", "to_menu")
    expect("«Продолжить» после отмены", await press("to_menu"), "Что дальше", "feature_search")
    expect("роль и избранное сохранились", await press("feature_favorites"), "Избранное</b> (2)", "fav:open:1")
    expect("избранное: удалить один", await press("fav:del:0"), "Избранное</b> (1)", "fav:open:0", absent=("fav:open:1",))
    expect("избранное: очистить всё", await press("fav:clear"), "Избранное пусто", "to_menu")
    await press("to_menu")
    await text("/cancel")
    expect("«Начать заново» после отмены", await press("restart"), "Кто вы", "role_teacher")
    await press("role_teacher"); await press("methodist")
    expect("после /start избранное очищено", await press("feature_favorites"), "Избранное пусто")
    await press("feature_search"); await text(MATERIAL); await text("/cancel")
    await press("to_menu"); await press("feature_search")
    expect("после отмены материал удалён", await text("знаменатель"), "Слишком короткий")

    # ===== Тест по своему материалу, .docx, оценки, /stats, запоминание класса =====
    await text("/start"); await press("role_teacher"); await press("methodist")
    await press("feature_quiz")
    expect("тест по материалу: приглашение", await press("quiz_material"), "Тест по материалу", "to_menu")
    out = await file("параграф.txt", LONG_MATERIAL.encode())
    expect("тест по материалу: сразу после загрузки", out, "Тест по материалу (", "а)", "б)", "в)", "г)", "Ключ ответов", "абзац",
           "export_docx", "fb:up")
    body = next(m["text"] for m in out if m["text"].startswith("🧩 Тест по материалу"))
    n_questions = len(re.findall(r"^\d+\. ", body, re.M))
    if n_questions < 5:
        FAILS.append(f"тест по материалу: мало вопросов ({n_questions})")
    else:
        print(f"OK   тест по материалу: {n_questions} вопросов")
    key = next(m["text"] for m in out if "Ключ ответов" in m["text"])
    if len(re.findall(r"\d+ — [абвг]", key)) != n_questions:
        FAILS.append("ключ ответов не совпадает с числом вопросов")
    FILES.clear()
    expect("скачать .docx", await press("export_docx"), "Файл для печати")
    if not FILES:
        FAILS.append(".docx не отправлен")
    else:
        from docx import Document
        doc = Document(io.BytesIO(FILES[-1].buffer))
        doc_text = "\n".join(p.text for p in doc.paragraphs)
        ok = FILES[-1].filename.endswith(".docx") and "Ключ ответов" in doc_text and "_____" in doc_text
        print(("OK   " if ok else "FAIL ") + f".docx открывается: {FILES[-1].filename}")
        if not ok:
            FAILS.append(".docx: неверное содержимое")
    expect("оценка 👍 и ссылка на бота", await press("fb:up"), "поделитесь", "https://max.ru/vektor_test_bot",
           "to_menu", "export_docx", "material_test")
    expect("оценка 👎: кнопка «В меню»", await press("fb:down"), "Спасибо за оценку", "to_menu", "export_docx")
    expect("из ответа на оценку — в главное меню", await press("to_menu"), "Что дальше", "feature_ask")
    await press("feature_search"); await text(MATERIAL)
    expect("кнопка теста под загруженным материалом", await press("material_test"), "Тест по материалу (")
    await press("to_menu"); await press("feature_quiz"); await press("quiz_material")
    expect("тест по короткому материалу", await text("Дробь — это часть целого. Числитель сверху, знаменатель снизу."), "не получается тест")
    expect("/stats", await text("/stats"), "Статистика", "тестов по материалу: 2", "выгрузок .docx: 1", "оценок 👍: 1",
           "Доля полезных ответов: 50%", "анонимные")
    expect("/info: ссылка поделиться", await text("/info"), "Поделиться ботом: https://max.ru/vektor_test_bot")
    await press("to_menu"); await press("feature_generate"); await press("homework"); await press("math")
    expect("класс: кнопка «как в прошлый раз»", await text("6"), "Шаг 2 из 2")
    await text("Проценты")
    await press("feature_generate"); await press("explain_topic"); await press("physics")
    await bot_last()
    expect("класс запомнился", LAST, "class:6")
    expect("класс выбран кнопкой", await press("class:6"), "Шаг 2 из 2")
    await text("Сила тяжести")
    await text("/cancel")
    await press("to_menu"); await press("feature_generate"); await press("worksheet"); await press("history")
    await bot_last()
    expect("класс помнится и после отмены", LAST, "class:6")
    expect("пустой .docx до результата после /start", (await text("/start")) and await press("export_docx"), "Пока нечего сохранять")

    # ===== Новый сценарий — только запись в prompts.py, без правки обработчиков =====
    from prompts import TEACHER_PROMPT_LIBRARY
    TEACHER_PROMPT_LIBRARY["user_templates"]["olympiad"] = {
        "name": "Олимпиадные задачи",
        "template": "Составь {count} олимпиадных задач по теме «{topic}» для {class_level} класса.",
        "required_fields": ["class_level", "topic", "count"],
        "questions": {"count": "Сколько задач нужно?"},
    }
    await text("/start"); await press("role_teacher"); await press("methodist")
    expect("новый сценарий: кнопка появилась", await press("feature_generate"), "olympiad")
    await press("olympiad"); await press("math"); await text("8")
    expect("новый сценарий: свой вопрос", await text("Делимость"), "Сколько задач нужно?", "Шаг 3 из 3")
    expect("новый сценарий: промт собран", await text("5"), "Составь 5 олимпиадных задач по теме «Делимость» для 8 класса")
    del TEACHER_PROMPT_LIBRARY["user_templates"]["olympiad"]

    expect("ассистент без нейросети: промт", (await press("to_menu")) and await press("feature_ask"), "Нейросеть сейчас не подключена")
    expect("ассистент без нейросети: вопрос", await text("Как объяснить дроби пятикласснику?"), "Промт с вашим вопросом",
           "Как объяснить дроби пятикласснику?", "https://giga.chat")
    expect("уточнение без нейросети", await press("fu:simple"), "когда подключена нейросеть")
    await press("to_menu")

    # оформление ответа нейросети
    from formatting import markdown_to_html
    sample = markdown_to_html("### Итог\n**Бетховен** — композитор.\n- пункт < 5 & > 2\n---\n2*3*4 = 24")
    if sample != "<b>Итог</b>\n<b>Бетховен</b> — композитор.\n• пункт &lt; 5 &amp; &gt; 2\n\n2*3*4 = 24":
        FAILS.append(f"оформление Markdown: {sample!r}")
    else:
        print("OK   Markdown нейросети превращается в оформление МАКС")
    latex = markdown_to_html(r"**Ответ:** $\sin B = \frac{\sqrt{3}}{2}$, $\tan C = \frac{1}{\sqrt{3}}$, $x^2$, $30^\circ$")
    if latex != "<b>Ответ:</b> sin B = √3/2, tg C = 1/√3, x², 30°":
        FAILS.append(f"формулы LaTeX: {latex!r}")
    else:
        print("OK   формулы LaTeX превращаются в читаемый текст")
    real = markdown_to_html(REAL_MODEL_ANSWER)
    leftovers = [x for x in ("**", "$", "\\frac", "\\pi", "\\sqrt", "\\left") if x in real]
    if leftovers or "cos(π/2)" not in real or "2π/3" not in real or "-√(sin²(θ)+cos²(θ))" not in real:
        FAILS.append(f"реальный ответ модели: остались {leftovers}")
    else:
        print("OK   реальный ответ модели с тригонометрией оформлен чисто")

    # ===== Ученик =====
    await text("/start")
    await press("role_student")
    expect("ученик: тьютор", await press("tutor"), "Что делаем")
    await press("feature_generate")
    await press("solve_task")
    await press("other")
    await text("6")
    await text("Пропорции")
    expect("ученик: разбор задачи", await text("За 3 часа 45 км, сколько за 5?"), "Промт готов", "сколько за 5")
    expect("ученик: сменить роль", await press("back_to_subrole"), "exam_coach")

    # ===== Ошибка в обработчике =====
    original = vb.materials.quick_check
    vb.materials.quick_check = lambda *a: 1 / 0
    await text("/start"); await press("role_teacher"); await press("methodist")
    await press("feature_check"); await press("check_skip_task")
    expect("ошибка: пользователь получил сообщение", await text("ответ"), "Что-то пошло не так", "to_menu")
    vb.materials.quick_check = original

    # ===== С нейросетью (поддельная Ollama) =====
    app = web.Application()
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/chat/completions", completions)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 11555).start()
    os.environ.update(LLM_API_KEY="ollama", LLM_API_BASE="http://127.0.0.1:11555/v1")
    llm_client._client = None

    MODELS["data"] = None  # так отвечает Ollama, когда ни одна модель не скачана
    if await llm_client.check_llm_health():
        FAILS.append("health: без моделей считается доступной")
    MODELS["data"] = [{"id": "qwen2.5:3b-instruct", "object": "model", "created": 0, "owned_by": "x"}]
    if not await llm_client.check_llm_health():
        FAILS.append("health: модель не найдена")
    print("OK   проверка доступности модели")

    expect("/start с нейросетью: без отметки", await text("/start"), "Кто вы", absent=("без нейросети",))
    await press("role_teacher")
    await press("methodist")
    await press("feature_quiz")
    await press("quiz_topic")
    for t in ("Пётр I", "7", "5"):
        await text(t)
    out = await press("quiz_type:тест")
    expect("тест: выбор после ввода данных", out, "Как получить результат", "ai_generate_now", "show_prompt",
           "Ответ здесь", absent=("Промт для теста", "с разметкой"))
    expect("тест: промт для другой нейросети", await press("show_prompt"), "Промт для теста", "Количество вопросов: 5",
           "https://giga.chat", "ai_generate_now", "add_favorite")
    ACTIONS.clear()
    out = await press("ai_generate_now")
    expect("нейросеть: тест", out, "Тест по теме «Пётр I»", "проверьте ключ ответов", "&lt;b&gt;с разметкой&lt;/b&gt; &amp; символами",
           "export_docx", "show_prompt", "fu:simple", "fb:up")
    if LAST_REQUEST.get("max_tokens") != 1200:
        FAILS.append(f"тест: нужен увеличенный лимит длины ({LAST_REQUEST.get('max_tokens')})")
    if not ACTIONS:
        FAILS.append("нет индикатора «печатает»")
    else:
        print("OK   индикатор «печатает» во время ответа")
    await press("feature_generate")
    await press("explain_topic")
    await press("physics")
    await text("8")
    expect("учебный материал: выбор", await text("Закон Ома"), "Как получить результат", "ai_generate_now", "show_prompt",
           absent=("Промт готов",))
    out = await press("ai_generate_now")
    expect("нейросеть: ответ по кнопке из меню", out, "Объяснение сложной темы: Закон Ома", "с разметкой",
           "Примерное время ожидания", "fu:simple", "export_docx", "show_prompt")
    expect("после ответа — промт для другой нейросети", await press("show_prompt"), "Промт готов", "Закон Ома", "https://giga.chat")
    notice = next(m["mid"] for m in out if "Примерное время ожидания" in m["text"])
    if notice not in DELETED:
        FAILS.append("сообщение об ожидании не удалено после ответа")
    else:
        print("OK   сообщение об ожидании удаляется после ответа")
    if LAST_REQUEST.get("temperature", 1) > 0.5 or not LAST_REQUEST.get("max_tokens"):
        FAILS.append("параметры генерации: нужна низкая температура и ограничение длины")
    if "Не выдумывай" not in LAST_REQUEST["messages"][0]["content"]:
        FAILS.append("в системном промте нет правил точности")
    else:
        print("OK   правила точности и низкая температура в запросе")
    expect("уточнение «Проще»", await press("fu:simple"), "Проще", "с разметкой")
    roles = [m["role"] for m in LAST_REQUEST["messages"]]
    if roles != ["system", "user", "assistant", "user"] or "проще" not in LAST_REQUEST["messages"][-1]["content"].lower():
        FAILS.append(f"уточнение: в запросе нет предыдущего ответа ({roles})")
    else:
        print("OK   уточнение отправляет предыдущий ответ модели")

    # вопрос ассистенту с памятью разговора
    await press("to_menu")
    expect("ассистент: приглашение", await press("feature_ask"), "Вопрос ассистенту", "Я помню предыдущий вопрос")
    expect("ассистент: выбор", await text("Что такое фотосинтез?"), "Как получить результат", "ai_generate_now")
    expect("ассистент: ответ", await press("ai_generate_now"), "Ответ ассистента", "с разметкой", "fu:more", "Задайте следующий вопрос")
    await text("А где он происходит?")
    expect("ассистент: промт с контекстом", await press("show_prompt"), "Предыдущий вопрос: Что такое фотосинтез?",
           "Новый вопрос: А где он происходит?")
    await press("ai_generate_now")
    roles = [m["role"] for m in LAST_REQUEST["messages"]]
    if roles != ["system", "user", "assistant", "user"] or LAST_REQUEST["messages"][1]["content"] != "Что такое фотосинтез?":
        FAILS.append(f"ассистент: второй вопрос без контекста первого ({roles})")
    else:
        print("OK   ассистент помнит предыдущий вопрос")
    await press("to_menu")
    await press("feature_search")
    await text(MATERIAL)
    expect("поиск: абзацы сразу, затем выбор", await text("Что показывает числитель?"), "Абзац 2", "Как получить результат",
           "material_test")
    expect("нейросеть: поиск", await press("ai_generate_now"), "Ответ по материалу", "ещё вопрос", "material_test", "export_docx")
    await press("to_menu")
    await press("feature_check")
    await press("check_skip_task")
    expect("проверка: быстрая сразу, затем выбор", await text("Дробь — это часть целого. Числитель сверху."), "Быстрая проверка",
           "Как получить результат")
    expect("нейросеть: проверка", await press("ai_generate_now"), "Разбор ответа", "export_docx")

    await runner.cleanup()
    await press("feature_check")
    await press("check_skip_task")
    await text("Ответ ученика про дроби.")
    expect("нейросеть упала: выдаём промт", await press("ai_generate_now"), "недоступна", "Промт для проверки")
    if await llm_client.check_llm_health():
        FAILS.append("health: упавший сервер считается доступным")
    await press("feature_quiz")
    await press("quiz_topic")
    for t in ("Дроби", "5", "3"):
        await text(t)
    expect("после падения — режим промтов", await press("quiz_type:тест"), "не подключена", absent=("ai_generate_now",))

    if not vb.dp.use_create_task:
        FAILS.append("use_create_task выключен")
    if {c.name for c in vb.COMMANDS} != {"start", "help", "info", "cancel", "stats"}:
        FAILS.append("меню команд")

    print("\nИТОГ:", "все проверки пройдены" if not FAILS else f"ошибок: {len(FAILS)}")
    for f in FAILS:
        print("  -", f)
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
