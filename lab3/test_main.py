from main import tokenize


def toks(text, **kw):
    return [t for _, t in tokenize(text, **kw)]


def kinds(text):
    return [k for k, _ in tokenize(text)]


CASES = [
    ("Привет, мир! (тест)", ["привет", "мир", "тест"]),                          # punctuation dropped
    ("Он ушёл", ["он", "ушел"]),                                                  # ё -> е
    ("Лин Гла́у", ["лин", "глау"]),                                               # stress mark removed
    ("северо-запад, Ростов-на-Дону, кто-то", ["северо-запад", "ростов-на-дону", "кто-то"]),
    ("Это — тире, а это - дефис", ["это", "тире", "а", "это", "дефис"]),          # dashes with spaces split
    ("1990-2000", ["1990", "2000"]),                                              # numeric range
    ("1990-е, F-16, 3D", ["1990-е", "f-16", "3d"]),                               # mixed alnum stays whole
    ("3,14 и 12.03.2020 в 10:30", ["3,14", "и", "12.03.2020", "в", "10:30"]),
    ("10 000 экз.", ["10 000", "экз."]),
    ("в 2020 2021", ["в", "2020", "2021"]),                                       # 4-digit groups are not thousands
    ("в 1990 г. Он", ["в", "1990", "г.", "он"]),
    ("XX в. н. э.", ["xx", "в.", "н.э."]),
    ("т. е. и т.д.", ["т.е.", "и", "т.д."]),
    ("А.С. Пушкин, А. С. Пушкин", ["а.с.", "пушкин", "а.с.", "пушкин"]),
    ("Пётр I. Он", ["петр", "i", "он"]),                                          # Roman numeral is not an initial
    ("Гусь-Хрустальный р-н", ["гусь-хрустальный", "р-н"]),
    ("O'Neil и don’t", ["o'neil", "и", "don’t"]),
    ("см. https://doi.org/10.1/x. И a@b.ru", ["см.", "https://doi.org/10.1/x", "и", "a@b.ru"]),
]

for text, want in CASES:
    assert toks(text) == want, (text, toks(text), want)

assert toks("Ростов-на-Дону", split_compounds=True) == ["ростов", "на", "дону"]
assert toks("Привет, мир", lower=False) == ["Привет", "мир"]
assert [t for t in toks("Привет, мир.", punct=True)] == ["привет", ",", "мир", "."]
assert kinds("1,5 кг. и т.д.") == ["number", "abbr", "word", "abbr"]
assert tokenize("") == []
print("ok")
