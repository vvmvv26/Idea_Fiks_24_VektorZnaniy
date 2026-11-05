"""Оформление ответов нейросети для МАКС.

Модели пишут в Markdown (**жирный**, ### заголовки, списки через «-» и «*»), а бот отправляет
сообщения с HTML-разметкой. Здесь Markdown переводится в HTML построчно: теги не переходят
через строку, поэтому длинный ответ можно резать по строкам без поломки разметки.
"""
import html
import re

# LaTeX, который модели вставляют в формулы, переводим в обычный текст: √3/2, x², sin α
LATEX_SYMBOLS = {
    r"\cdot": "·", r"\times": "×", r"\div": ":", r"\pm": "±", r"\mp": "∓", r"\leq": "≤", r"\le": "≤",
    r"\geq": "≥", r"\ge": "≥", r"\neq": "≠", r"\ne": "≠", r"\approx": "≈", r"\infty": "∞", r"\circ": "°",
    r"\degree": "°", r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ", r"\Delta": "Δ",
    r"\theta": "θ", r"\varphi": "φ", r"\phi": "φ", r"\pi": "π", r"\lambda": "λ", r"\mu": "μ",
    r"\omega": "ω", r"\sigma": "σ", r"\rightarrow": "→", r"\Rightarrow": "⇒", r"\to": "→",
    r"\in": "∈", r"\angle": "∠", r"\perp": "⊥", r"\parallel": "∥",
    r"\sin": "sin", r"\cos": "cos", r"\tan": "tg", r"\tg": "tg", r"\cot": "ctg", r"\ctg": "ctg",
    r"\arcsin": "arcsin", r"\arccos": "arccos", r"\arctan": "arctg", r"\log": "log", r"\ln": "ln", r"\lg": "lg",
}
SUPERSCRIPT = str.maketrans("0123456789+-n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻ⁿ")
GROUP = r"\{([^{}]*)\}"


def _wrap(expr):
    """Скобки только там, где без них формула прочитается неверно: 2π/3, но (a+b)/2."""
    expr = expr.strip()
    if re.fullmatch(r"-?[\w√π°.,²³⁰¹⁴⁵⁶⁷⁸⁹αβγθφλμωσ]+", expr):
        return expr
    if _enclosed(expr):
        return expr
    return f"({expr})"


def _enclosed(expr):
    """Всё выражение уже в одних внешних скобках: (a+b), но не (a)+(b)."""
    if not (expr.startswith("(") and expr.endswith(")")):
        return False
    depth = 0
    for i, ch in enumerate(expr):
        depth += ch == "("
        depth -= ch == ")"
        if depth == 0 and i < len(expr) - 1:
            return False
    return True


def latex_to_text(text):
    if "\\" not in text and "$" not in text:
        return text
    text = re.sub(r"\$\$?|\\\(|\\\)|\\\[|\\\]", "", text)
    text = re.sub(r"\\(?:text|mathrm|mathbf|operatorname)" + GROUP, r"\1", text)
    text = re.sub(r"\\(?:left|right|displaystyle)\b", "", text)
    text = re.sub(r"\\[,;:! ]", " ", text)
    # сначала буквы и функции (\pi → π), чтобы дроби ниже собирались без лишних скобок: 2π/3
    for command in sorted(LATEX_SYMBOLS, key=len, reverse=True):
        text = re.sub(re.escape(command) + r"(?![A-Za-z])", LATEX_SYMBOLS[command], text)
    text = re.sub(r"\^\{([0-9+\-n]+)\}|\^([0-9n])", lambda m: (m.group(1) or m.group(2)).translate(SUPERSCRIPT), text)
    for _ in range(5):  # вложенные \frac и \sqrt раскрываются изнутри наружу
        new = re.sub(r"\\[dt]?frac" + GROUP + GROUP, lambda m: f"{_wrap(m.group(1))}/{_wrap(m.group(2))}", text)
        new = re.sub(r"\\sqrt" + GROUP, lambda m: f"√{_wrap(m.group(1))}", new)
        if new == text:
            break
        text = new
    text = re.sub(r"\^\{\\?circ\}|\^°", "°", text)
    text = re.sub(r"\\sqrt\s*(\w)", r"√\1", text)
    return text.replace("{", "").replace("}", "")


HEADING = re.compile(r"^\s*#{1,6}\s+(.+?)\s*#*\s*$")
BULLET = re.compile(r"^(\s*)[-*•+]\s+")
NUMBERED = re.compile(r"^(\s*)(\d+)[.)]\s+")
BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
RULE = re.compile(r"^\s*([-*_])\1{2,}\s*$")


def _line(line):
    if RULE.match(line):
        return ""
    heading = HEADING.match(line)
    if heading:
        title = heading.group(1).replace("**", "").strip()
        return f"<b>{html.escape(title)}</b>"
    prefix = ""
    bullet = BULLET.match(line)
    if bullet:
        prefix = bullet.group(1) + "• "
        line = line[bullet.end():]
    else:
        numbered = NUMBERED.match(line)
        if numbered:
            prefix = f"{numbered.group(1)}{numbered.group(2)}. "
            line = line[numbered.end():]
    text = html.escape(line.replace("`", ""))
    text = BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    return prefix + text


def markdown_to_html(text):
    lines = [_line(line.rstrip()) for line in latex_to_text(text).strip().split("\n")]
    # не больше одной пустой строки подряд
    result = []
    for line in lines:
        if line.strip() or (result and result[-1].strip()):
            result.append(line)
    return "\n".join(result).strip()
