"""Работа с учебным материалом: чтение файлов, разбиение на абзацы, поиск по вопросу,
тест по материалу и быстрая проверка ответа. Всё считается локально, без нейросети и без записи на диск."""
import html
import io
import math
import re

MAX_CHARS = 20000
CHUNK_MAX_CHARS = 700
TTL_SECONDS = 30 * 60
TOP_K = 3
QUOTE_MAX_CHARS = 400

STOP_WORDS = {
    "что", "как", "такое", "это", "этот", "эта", "эти", "где", "когда", "почему", "зачем", "какой", "какая",
    "какое", "какие", "который", "которая", "которые", "или", "для", "при", "про", "над", "под", "без", "его",
    "её", "них", "она", "они", "оно", "был", "была", "были", "быть", "есть", "так", "также", "тоже", "уже",
    "ещё", "еще", "все", "всё", "весь", "вся", "сколько", "столько", "чем", "чтобы", "если", "только", "можно", "нужно", "очень",
    "между", "через", "после", "перед", "потому", "поэтому", "свой", "своя", "свои", "себя", "кто", "чей",
    "которого", "которой", "котором", "которую", "которым", "которыми", "которых", "такой", "такая", "такое",
    "таких", "такие", "такими", "этого", "этой", "этом", "этим", "этими", "этих", "тоже", "более", "менее",
    "самый", "самая", "самое", "самые", "каждый", "каждая", "каждое", "другой", "другая", "другие", "других",
}

# Грубый стемминг: отрезаем окончание, чтобы «дробь», «дроби» и «дробей» совпали.
ENDINGS = sorted([
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "иях", "ией", "ием", "ость", "ости",
    "ах", "ях", "ой", "ей", "ий", "ый", "ая", "яя", "ое", "ее", "ые", "ие", "ом", "ем", "ую", "юю", "ов",
    "ев", "ам", "ям", "ия", "ию", "ть", "а", "я", "о", "е", "ы", "и", "у", "ю", "ь", "й",
], key=len, reverse=True)

SUPPORTED_FORMATS = ("txt", "pdf", "docx")


def extract_text(filename, data):
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext not in SUPPORTED_FORMATS:
        raise ValueError(f"unsupported format: {ext or 'none'}")
    if ext == "txt":
        for encoding in ("utf-8", "cp1251"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="ignore")
    if ext == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    from docx import Document
    doc = Document(io.BytesIO(data))
    return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())


def split_paragraphs(text):
    """Возвращает список [номер абзаца, текст]. Длинный абзац режется по предложениям,
    куски сохраняют номер исходного абзаца — на него бот и ссылается."""
    text = text.strip()[:MAX_CHARS]
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paragraphs) == 1 and "\n" in paragraphs[0]:
        # в сообщениях абзацы часто разделены одним переводом строки
        paragraphs = [p.strip() for p in paragraphs[0].split("\n") if p.strip()]

    chunks = []
    for number, para in enumerate(paragraphs, start=1):
        if len(para) <= CHUNK_MAX_CHARS:
            chunks.append([number, para])
            continue
        buf = ""
        for sentence in re.split(r"(?<=[.!?])\s+", para):
            if buf and len(buf) + len(sentence) > CHUNK_MAX_CHARS:
                chunks.append([number, buf])
                buf = sentence
            else:
                buf = f"{buf} {sentence}".strip()
        if buf:
            chunks.append([number, buf])
    return chunks


def stem(word):
    for ending in ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            return word[:-len(ending)]
    return word


def tokenize(text):
    words = re.findall(r"\w+", text.lower().replace("ё", "е"))
    return [stem(w) for w in words if len(w) > 2 and w not in STOP_WORDS]


def search(chunks, question, top_k=TOP_K):
    """Ищет абзацы, где больше всего слов из вопроса. Редкие в материале слова весят больше
    (как в IDF), при равенстве выше абзац, где они встречаются плотнее.
    Возвращает [(индекс, текст, оценка 0..1)]."""
    terms = set(tokenize(question))
    if not terms or not chunks:
        return []
    tokens = [tokenize(c) for c in chunks]
    sets = [set(t) for t in tokens]
    idf = {t: math.log((1 + len(chunks)) / (1 + sum(t in s for s in sets))) + 1 for t in terms}
    total = sum(idf.values())

    scored = []
    for i, (toks, s) in enumerate(zip(tokens, sets)):
        coverage = sum(idf[t] for t in terms & s) / total
        density = sum(t in terms for t in toks) / max(len(toks), 1)
        scored.append((coverage, density, i))
    scored.sort(reverse=True)
    return [(i, chunks[i], cov) for cov, _, i in scored[:top_k] if cov > 0]


def shorten(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def quick_check(task, answer):
    """Формальные показатели ответа: объём, длина предложений, повторы, ключевые слова задания."""
    words = re.findall(r"\w+", answer)
    sentences = [s for s in re.split(r"[.!?]+", answer) if re.search(r"\w", s)]
    avg = len(words) / max(len(sentences), 1)
    lines = [f"Объём: {len(words)} слов, {len(sentences)} предложений, в среднем {avg:.0f} слов в предложении."]
    if len(words) < 15:
        lines.append("Ответ очень короткий — скорее всего, мысль не раскрыта.")
    if avg > 25:
        lines.append("Предложения длинные, часть стоит разбить.")

    first_form = {}
    for w in re.findall(r"\w+", answer.lower()):
        first_form.setdefault(stem(w.replace("ё", "е")), w)
    stems = tokenize(answer)
    counts = {}
    for s in stems:
        if len(s) >= 4:
            counts[s] = counts.get(s, 0) + 1
    limit = max(3, int(len(stems) * 0.06) + 1)
    repeats = [first_form.get(s, s) for s, c in sorted(counts.items(), key=lambda x: -x[1]) if c >= limit][:5]
    if repeats:
        lines.append(f"Часто повторяется: {', '.join(repeats)}. Попробуйте заменить синонимами.")

    if task:
        keywords = {}
        for w in re.findall(r"\w+", task.lower()):
            if len(w) > 3 and w not in STOP_WORDS:
                keywords.setdefault(stem(w.replace("ё", "е")), w)
        if keywords:
            present = set(stems)
            missing = [w for s, w in keywords.items() if s not in present]
            lines.append(f"Ключевые слова задания в ответе: {len(keywords) - len(missing)} из {len(keywords)}.")
            if missing:
                lines.append(f"Не упомянуты: {', '.join(missing[:8])}.")

    body = "\n".join(f"• {html.escape(line)}" for line in lines)
    return f"📏 <b>Быстрая проверка</b>\n\n{body}"


WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+")
LETTERS = "абвг"


def make_test(chunks, count=8, rng=None):
    """Тест с выбором ответа по загруженному материалу, без нейросети.

    В предложении из материала пропускается ключевое слово (термин, который встречается
    в тексте не один раз), неверные варианты — другие термины того же материала с похожим
    окончанием. Возвращает [{"question", "options", "answer", "paragraph"}]."""
    import random
    rng = rng or random.Random()

    sentences = []
    for number, text in chunks:
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            sentence = sentence.strip()
            if 40 <= len(sentence) <= 260 and len(sentence.split()) >= 6:
                sentences.append((number, sentence))

    def candidates(sentence):
        # первое слово пропускаем: его заглавная буква выдавала бы правильный вариант
        return [m for m in WORD_RE.finditer(sentence)][1:]

    freq, forms = {}, {}
    for _, sentence in sentences:
        for m in candidates(sentence):
            word = m.group()
            low = word.lower()
            if len(low) < 5 or low in STOP_WORDS or word != low:
                continue
            s = stem(low.replace("ё", "е"))
            freq[s] = freq.get(s, 0) + 1
            forms.setdefault(s, set()).add(low)

    # термины — слова, которые встречаются в материале хотя бы дважды; из них и вопросы, и варианты
    terms = {s for s, n in freq.items() if n >= 2}
    pool = [w for s in terms for w in forms[s]] or [w for ws in forms.values() for w in ws]
    # для каждого предложения — все подходящие термины, от лучшего к худшему
    options_by_sentence = []
    for number, sentence in sentences:
        found = []
        for m in candidates(sentence):
            low = m.group()
            if low != low.lower() or len(low) < 5 or low in STOP_WORDS:
                continue
            s = stem(low.replace("ё", "е"))
            if s in terms:
                found.append((min(freq[s], 4) + len(low) / 10, m, s))
        if found:
            found.sort(key=lambda x: -x[0])
            options_by_sentence.append((found[0][0], number, sentence, found))
    options_by_sentence.sort(key=lambda x: -x[0])

    questions, used_stems = [], set()
    for _, number, sentence, found in options_by_sentence:
        if len(questions) >= count:
            break
        pick = next(((m, s) for _, m, s in found if s not in used_stems), None)
        if pick is None:
            continue
        match, s = pick
        answer = match.group()
        in_sentence = {stem(w.lower().replace("ё", "е")) for w in WORD_RE.findall(sentence)}
        others = [w for w in pool if stem(w.replace("ё", "е")) not in in_sentence]
        # по одной форме на слово, с похожим окончанием в первую очередь
        by_stem = {}
        for w in sorted(others, key=lambda w: (w[-2:] != answer[-2:], w)):
            by_stem.setdefault(stem(w.replace("ё", "е")), w)
        same_ending = [w for w in by_stem.values() if w[-2:] == answer[-2:]]
        if len(same_ending) >= 3:
            distractors = rng.sample(same_ending, 3)
        elif len(by_stem) >= 3:
            distractors = same_ending + rng.sample([w for w in by_stem.values() if w not in same_ending], 3 - len(same_ending))
        else:
            continue
        options = distractors + [answer]
        rng.shuffle(options)
        question = sentence[:match.start()] + "_____" + sentence[match.end():]
        questions.append({"question": question, "options": options, "answer": options.index(answer), "paragraph": number})
        used_stems.add(s)
    questions.sort(key=lambda q: q["paragraph"])
    return questions


def format_test(questions):
    """Текст теста и отдельно ключ ответов — обычный текст без разметки."""
    lines = []
    for i, q in enumerate(questions, start=1):
        lines.append(f"{i}. {q['question']}")
        lines.append("   " + "   ".join(f"{LETTERS[j]}) {opt}" for j, opt in enumerate(q["options"])))
        lines.append("")
    key = "; ".join(f"{i} — {LETTERS[q['answer']]} (абзац {q['paragraph']})" for i, q in enumerate(questions, start=1))
    return "\n".join(lines).rstrip(), key
