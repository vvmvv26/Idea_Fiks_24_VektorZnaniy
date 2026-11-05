"""Обращение к локальной нейросети через Ollama.

Ollama отдаёт API в формате OpenAI, поэтому библиотека openai используется просто как
HTTP-клиент: запросы идут на localhost (или на сервис ollama в docker compose), в облако
ничего не уходит. Адрес, модель и ключ-заглушка берутся из переменных окружения.
"""
import asyncio
import logging
import os
import time
from collections import deque
from typing import NamedTuple

from openai import AsyncOpenAI, APIConnectionError, APIError, APITimeoutError

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "http://localhost:11434/v1"
DEFAULT_MODEL = "qwen2.5:3b-instruct"
REQUEST_TIMEOUT = 180  # на процессоре без видеокарты длинный ответ идёт минуту-две; LLM_TIMEOUT меняет
HEALTH_CHECK_INTERVAL = 60
DEFAULT_MAX_TOKENS = 700   # ~2500 знаков: ответ укладывается в сообщение и приходит быстрее
TEMPERATURE = 0.3          # меньше фантазии — меньше выдуманных фактов

# Дописывается к системному промту каждого запроса: маленькие модели склонны выдумывать
ACCURACY_RULES = (
    "\n\nПравила ответа: отвечай по-русски, по делу и без вступлений. Пиши только то, в чём уверен. "
    "Не выдумывай факты, даты, имена, названия произведений и формулы; если не уверен — прямо скажи, "
    "что это нужно проверить в учебнике. Оформление: короткие абзацы, нумерованные или маркированные "
    "списки, **жирным** — только ключевые термины. Формулы пиши обычным текстом (√3/2, x², sin α), "
    "без LaTeX и без знаков $."
)

# Время последних ответов — для оценки ожидания. В очереди Ollama запросы идут по одному.
_durations = deque(maxlen=8)
_in_progress = 0

_client = None
_available = False
_checked = False


class LLMAnswer(NamedTuple):
    ok: bool
    text: str  # ответ модели или, если ok=False, сообщение об ошибке для пользователя


def model_name():
    return _model()


def _model():
    return os.environ.get("LLM_MODEL") or DEFAULT_MODEL


def _get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("LLM_API_KEY")
        if not api_key:
            raise RuntimeError("LLM_API_KEY не задан")
        base_url = os.environ.get("LLM_API_BASE") or DEFAULT_API_BASE
        _client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=1)
    return _client


def is_llm_configured():
    """Включён ли режим ответов нейросети: ключ задан и последняя проверка застала модель живой."""
    return bool(os.environ.get("LLM_API_KEY")) and _available


async def check_llm_health():
    """Проверяет, что Ollama отвечает и нужная модель скачана. В лог пишет только изменения."""
    global _available, _checked
    if not os.environ.get("LLM_API_KEY"):
        _available = _checked = False
        return False

    model, reason = _model(), ""
    try:
        models = await asyncio.wait_for(_get_client().models.list(), timeout=10)
        names = {m.id for m in (models.data or [])}  # без скачанных моделей Ollama отдаёт "data": null
        available = model in names or f"{model}:latest" in names
        if not available:
            reason = f"модель {model} не скачана (ollama pull {model})"
    except Exception as e:
        available, reason = False, f"сервер не отвечает ({type(e).__name__})"

    if available != _available or not _checked:
        if available:
            logger.info(f"LLM available: {model}")
        else:
            logger.warning(f"LLM unavailable: {reason}")
    _available, _checked = available, True
    return available


async def llm_health_loop():
    """Если Ollama запустили позже бота, ответы включатся сами в течение минуты."""
    while True:
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)
        await check_llm_health()


def _default_duration():
    model = _model()
    if "0.5b" in model or "1.5b" in model:
        return 60
    if "3b" in model:
        return 100
    return 150


def estimate_wait():
    """Примерное время ожидания в секундах и сколько запросов впереди в очереди."""
    average = sum(_durations) / len(_durations) if _durations else _default_duration()
    return int(average * (_in_progress + 1)), _in_progress


async def ask_llm(system_prompt, user_prompt, history=None, max_tokens=DEFAULT_MAX_TOKENS):
    """history — предыдущие реплики [(role, text)] для продолжения разговора.
    Никогда не бросает исключений: любая ошибка превращается в LLMAnswer(ok=False, ...)."""
    global _in_progress
    try:
        client = _get_client()
    except RuntimeError as e:
        logger.error(f"LLM config error: {e}")
        return LLMAnswer(False, "Нейросеть не настроена. Обратитесь к администратору бота.")

    messages = [{"role": "system", "content": (system_prompt or "") + ACCURACY_RULES}]
    messages += [{"role": role, "content": text} for role, text in history or []]
    messages.append({"role": "user", "content": user_prompt})
    started = time.monotonic()
    _in_progress += 1
    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=_model(),
                messages=messages,
                temperature=TEMPERATURE,
                max_tokens=max_tokens,
            ),
            timeout=int(os.environ.get("LLM_TIMEOUT") or REQUEST_TIMEOUT),
        )
        _durations.append(time.monotonic() - started)
    except (asyncio.TimeoutError, APITimeoutError):
        logger.error("LLM request timed out")
        return LLMAnswer(False, "Нейросеть не успела ответить. Попробуйте ещё раз чуть позже.")
    except APIConnectionError as e:
        logger.error(f"LLM connection error: {e}")
        return LLMAnswer(False, "Нейросеть сейчас недоступна.")
    except APIError as e:
        logger.error(f"LLM API error: {e}")
        return LLMAnswer(False, "Нейросеть вернула ошибку.")
    except Exception as e:
        logger.error(f"Unexpected LLM error: {e!r}")
        return LLMAnswer(False, "Не удалось получить ответ нейросети.")
    finally:
        _in_progress -= 1

    content = response.choices[0].message.content if response.choices else None
    if not content or not content.strip():
        return LLMAnswer(False, "Нейросеть вернула пустой ответ. Попробуйте переформулировать запрос.")
    return LLMAnswer(True, content.strip())
