"""The Turkish interface: every text that goes through t() has a translation, translations keep their placeholders
and markup, the same English has one Turkish, and the Turkish reads as if written for a student ("sen", no
machine-translation tells). Dates, numbers and plurals follow the language; the language follows Windows."""

import ast
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from studia import i18n

SRC = Path(i18n.__file__).parent
WEB = SRC / "web"
TR = SRC / "locales" / "tr"
STR = r'"((?:[^"\\]|\\.)*)"'          # a double-quoted JS string (the UI code writes text in double quotes)


@pytest.fixture
def turkish():
    i18n.set_language("tr")
    yield
    i18n.set_language("en")


def _js_unescape(s: str) -> str:
    return json.loads(f'"{s}"')


def catalog() -> dict[str, list[tuple[str, str]]]:
    """English → [(Turkish, file)], from every locales/tr/*.json."""
    out: dict[str, list[tuple[str, str]]] = {}
    for f in sorted(TR.glob("*.json")):
        for k, v in json.loads(f.read_text(encoding="utf-8")).items():
            if not k.startswith("_"):
                out.setdefault(k, []).append((v, f.name))
    return out


def pending_files() -> set[str]:
    return {f.name for f in TR.glob("*.json") if json.loads(f.read_text(encoding="utf-8")).get("_pending")}


def py_keys() -> dict[str, str]:
    """Keys of t/tn/tc/N_ calls with literal text in the Python code → where."""
    keys: dict[str, str] = {}
    for p in sorted(SRC.rglob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            args = [a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None for a in node.args]
            where = f"{p.relative_to(SRC).as_posix()}:{node.lineno}"
            if name in ("t", "N_") and args and args[0]:
                keys[args[0]] = where
            elif name == "tn" and len(args) >= 3 and args[1]:
                keys[args[1]] = where                  # Turkish has one form, under the singular English
            elif name == "tc" and len(args) >= 2 and args[0] and args[1]:
                keys[f"{args[0]}|{args[1]}"] = where
    return keys


def js_keys() -> dict[str, str]:
    """Keys of t("…"), t.html("…"), t.n(n, "…", "…") and t.c("ctx", "…") in the page."""
    keys: dict[str, str] = {}
    pats = [(re.compile(r"\bt(?:\.html)?\(\s*" + STR), lambda m: _js_unescape(m.group(1))),
            (re.compile(r"\bt\.n\([^,()]+(?:\([^()]*\)[^,()]*)*,\s*" + STR), lambda m: _js_unescape(m.group(1))),
            (re.compile(r"\bt\.c\(\s*" + STR + r"\s*,\s*" + STR), lambda m: f"{_js_unescape(m.group(1))}|{_js_unescape(m.group(2))}")]
    for p in sorted(WEB.glob("*.js")):
        src = re.sub(r"/\*.*?\*/", lambda m: chr(10) * m.group().count(chr(10)), p.read_text(encoding="utf-8"), flags=re.S)
        src = re.sub(r"(?m)^\s*//.*$", "", src)       # comments are not text on the page
        for rx, key in pats:
            for m in rx.finditer(src):
                keys[key(m)] = f"{p.name}:{src.count(chr(10), 0, m.start()) + 1}"
    return keys


def splash_keys() -> set[str]:
    return set(re.findall(r'\b_\("([^"]+)"\)', (SRC / "splash.py").read_text(encoding="utf-8")))


# ------------------------------------------------------------------ completeness
def test_every_text_in_the_code_has_a_turkish_translation():
    have = catalog()
    missing = {k: w for k, w in {**py_keys(), **js_keys()}.items() if k not in have}
    assert not missing, "No Turkish for:\n" + "\n".join(f"  {w}: {k!r}" for k, w in sorted(missing.items(), key=lambda x: x[1]))


def test_no_translation_is_left_over():
    used = set(py_keys()) | set(js_keys())
    pend = pending_files()
    stale = {k: [f for _, f in v] for k, v in catalog().items() if k not in used and not all(f in pend for _, f in v)}
    assert not stale, "Not in the code any more (or the English changed):\n" + "\n".join(f"  {v}: {k!r}" for k, v in stale.items())


@pytest.mark.skipif(sys.platform != "win32", reason="the start-up window is plain Win32")
def test_the_start_up_window_has_its_turkish():
    from studia import splash

    assert splash_keys() and splash_keys() == set(splash.TEXT_TR)


def test_the_page_says_it_could_not_connect_in_the_catalogs_turkish():
    """Before Python's table arrives the page carries its own few Turkish lines (the window that never got the API)."""
    m = re.search(r't\.load\(\{ lang: "tr", table: (\{.*?\}) \}\);', (WEB / "app.js").read_text(encoding="utf-8"))
    inline, have = json.loads(m.group(1)), catalog()
    assert inline and all({tr for tr, _ in have.get(en, [])} == {tr} for en, tr in inline.items())


LOCAL_T = re.compile(r"\b(?:const|let|var) t\b|\(t\)\s*=>|\(t,|, t\)\s*=>|\bt =>|,\s*t\s*=[^=>]")
LOCAL_T_LEFT: dict[str, int] = {}     # every page is converted: none left


def _scope(fn: ast.AST, comprehensions: bool) -> list[ast.AST]:
    """The nodes of a function's own scope (not nested functions or classes; comprehensions only if asked)."""
    out, todo = [], list(ast.iter_child_nodes(fn))
    nested = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
    comps = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    while todo:
        node = todo.pop()
        if isinstance(node, nested) or (isinstance(node, comps) and not comprehensions):
            continue
        out.append(node)
        todo.extend(ast.iter_child_nodes(node))
    return out


def _binds_t(fn: ast.AST) -> bool:
    """Does this function have a parameter or a local variable named t?"""
    a = fn.args
    if "t" in [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs] + [x.arg for x in (a.vararg, a.kwarg) if x]:
        return True
    return any(isinstance(n, ast.Name) and n.id == "t" and isinstance(n.ctx, ast.Store) for n in _scope(fn, False))


def _calls_t(fn: ast.AST) -> bool:
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "t" for n in _scope(fn, True))


def test_no_python_function_hides_the_translate_function():
    """A function with a parameter or local named t can't call t() (it calls the value, or fails as unbound): two such
    functions crashed during the migration. In a module that imports t at the top, no function may have one (the next
    edit may call t() there); elsewhere, no function that calls t() may."""
    bad = []
    for p in sorted(SRC.rglob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        on_top = any(isinstance(n, ast.ImportFrom) and (n.module or "").endswith("i18n")
                     and any(x.name == "t" and not x.asname for x in n.names) for n in tree.body)
        bad += [f"{p.relative_to(SRC).as_posix()}:{fn.lineno}" for fn in ast.walk(tree)
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and _binds_t(fn)
                and (on_top or _calls_t(fn))]
    assert not bad, "a local t hides t() in: " + ", ".join(bad)


def test_no_new_local_t_hides_the_translate_function():
    """A local variable named t hides t(): `const t = prompt(t("Name this chat"))` breaks. Each file's migration
    renames its own (the counts here go down to 0); no new ones."""
    for p in WEB.glob("*.js"):
        if p.name == "i18n.js":
            continue
        n = len(LOCAL_T.findall(p.read_text(encoding="utf-8")))
        assert n <= LOCAL_T_LEFT.get(p.name, 0), f"{p.name}: {n} local t (rename them, e.g. to text/el/sec)"


def test_t_is_given_text_not_a_template():
    """t(`…${x}…`) would make a new key for every value: placeholders go in the second argument."""
    for p in WEB.glob("*.js"):
        bad = re.findall(r"\bt(?:\.html|\.n|\.c)?\(\s*`", p.read_text(encoding="utf-8"))
        assert not bad, p.name


# ------------------------------------------------------------------ translations keep what the code fills in
def _tags(s: str) -> list[str]:
    return sorted(re.findall(r"</?\w+", s))


@pytest.mark.parametrize("key", sorted(catalog()))
def test_placeholders_and_markup_survive(key):
    for tr, f in catalog()[key]:
        assert set(re.findall(r"\{\w+\}", tr)) == set(re.findall(r"\{\w+\}", key)), f"{f}: {key!r}"
        assert _tags(tr) == _tags(key), f"{f}: {key!r}"
        assert tr.strip() == tr and "  " not in tr, f"{f}: spaces in {tr!r}"


def test_the_same_english_has_one_turkish():
    clash = {k: v for k, v in catalog().items() if len({tr for tr, _ in v}) > 1}
    assert not clash, clash


# ------------------------------------------------------------------ style: "sen", natural, consistent
def _lower(s: str) -> str:
    return s.replace("İ", "i").replace("I", "ı").lower()


# "siz": verbs (giriniz, değiştirebilirsiniz, tıklayınız) and possessives (şifreniz, ayarlarınızı, hesabınızla)
SIZ = re.compile(r"\w*n[ıiuü]z(?:[ıiuü]|[ae]|[dt][ae]n?|[ıiuü]n|l[ae])?\b")
SIZ_OK = {"henüz", "deniz", "denizi", "denize", "yalnız", "beniz", "boynuz"}
TELLS = {                                    # machine translation and AI-text tells, and words the glossary replaced
    r"\bbaşarıyla\b": "say what happened: “Kaydedildi”, not “Başarıyla kaydedildi”",
    r"gerçekleştir": "yap-",
    r"\bmevcut değil": "yok",
    r"m[ae]kted[ıi]r\b": "-iyor / -ir, not the formal -mektedir",
    r"olanak (sağla|tanı)": "-ebilirsin",
    r"\blütfen\b": "a plain imperative is polite enough in “sen”",
    r"\bkullanıcı(?! ad)": "talk to the student: “sen” (“kullanıcı adı”, username, is fine)",
    r"\.\.\.": "the ellipsis character …",
    r"\btranskript": "ders metni (transkript is the grade transcript)",
    r"\bsenkroniz": "eşitle",
    r"\bvize\b": "ara sınav",
    r"yapay zekâ": "yapay zeka (one spelling everywhere)",
    r"\bparola": "şifre",
    r"\bsiz\b|\bsizin\b": "sen",
}


@pytest.mark.parametrize("key", sorted(catalog()))
def test_turkish_says_sen_and_reads_naturally(key):
    for tr, f in catalog()[key]:
        low = _lower(re.sub(r"\{\w+\}|<[^>]+>", " ", tr))
        siz = [w for w in SIZ.findall(low) if w not in SIZ_OK]
        assert not siz, f"{f}: “siz” form {siz} in {tr!r}"
        for rx, why in TELLS.items():
            assert not re.search(rx, low), f"{f}: {rx} ({why}) in {tr!r}"
        if "—" in tr and "—" not in key:
            pytest.fail(f"{f}: an em dash the English doesn't have: {tr!r}")


# ------------------------------------------------------------------ the mechanism
def test_turkish_and_english_lookups(turkish):
    assert i18n.t("Part {n}", n=2) == "Bölüm 2"
    assert i18n.t("A sentence nobody translated, {x}.", x=1) == "A sentence nobody translated, 1."
    assert i18n.tn(1, "{n} lecture transcribed", "{n} lectures transcribed") == "1 ders yazıya döküldü"
    assert i18n.tn(3, "{n} lecture transcribed", "{n} lectures transcribed") == "3 ders yazıya döküldü"
    i18n.set_language("en")
    assert i18n.tn(1, "{n} lecture transcribed", "{n} lectures transcribed") == "1 lecture transcribed"
    assert i18n.tn(3, "{n} lecture transcribed", "{n} lectures transcribed") == "3 lectures transcribed"


def test_dates_and_numbers(turkish):
    d = dt.date(2026, 10, 14)
    assert (i18n.date_long(d), i18n.date_short(d), i18n.day_month(d)) == ("14 Ekim 2026 Çarşamba", "14 Eki Çar", "14 Eki")
    assert (i18n.num(31200.5, 1), i18n.pct(0.45), i18n.duration(3900), i18n.join(["A", "B", "C"])) == \
        ("31.200,5", "%45", "1 sa 5 dk", "A, B ve C")
    i18n.set_language("en")
    assert (i18n.date_long(d), i18n.date_short(d), i18n.day_month(d)) == ("Wednesday, 14 October 2026", "Wed 14 Oct", "14 Oct")
    assert (i18n.num(31200.5, 1), i18n.pct(0.45), i18n.duration(3900), i18n.join(["A", "B"], "or")) == \
        ("31,200.5", "45%", "1 h 5 min", "A or B")


def test_the_language_follows_windows_until_one_is_chosen(monkeypatch):
    monkeypatch.delenv("STUDIA_LANG", raising=False)
    monkeypatch.setattr(i18n, "windows_language", lambda: "tr")
    assert i18n.resolve("") == "tr" and i18n.resolve("en") == "en"
    monkeypatch.setattr(i18n, "windows_language", lambda: "en")
    assert i18n.resolve("") == "en" and i18n.resolve("tr") == "tr"
    monkeypatch.setenv("STUDIA_LANG", "pseudo")
    assert i18n.resolve("tr") == "pseudo"


def test_the_choice_is_kept_per_pc():
    from studia import appconfig

    assert appconfig.load().language == ""
    assert appconfig.update({"language": "tr"}).language == "tr"
    assert appconfig.update({"language": "xx"}).language == ""          # anything else: as Windows


def test_messages_from_background_code_come_in_the_language(turkish):
    import requests

    from studia.net import plain_error

    exc = requests.ConnectionError("boom")
    assert plain_error(exc, "https://learn.ieu.edu.tr/x").startswith("learn.ieu.edu.tr ile bağlantı kurulamıyor.")
    assert plain_error(exc).startswith("Sunucuyla bağlantı kurulamıyor.")


def test_the_tray_menu_follows_a_change_in_settings_at_once(monkeypatch):
    import types

    from studia import agent, appconfig, launch

    monkeypatch.delenv("STUDIA_LANG", raising=False)
    monkeypatch.setattr(agent, "ui_running", lambda: True)           # the window is open: the helper checks nothing
    monkeypatch.setattr(launch, "startup_enabled", lambda: False)
    helper = types.SimpleNamespace(paused=False)
    try:
        for lang, first in (("en", "Open IEUStudia"), ("tr", "IEUStudia'yı aç"), ("en", "Open IEUStudia")):
            appconfig.update({"language": lang})                     # Settings → Language, in the app's process
            assert agent.Helper._menu(helper)[0][1] == first
    finally:
        i18n.set_language("en")


def test_pseudo_language_keeps_placeholders_and_markup():
    p = i18n.pseudo("Pick <b>{name}</b> now")
    assert "{name}" in p and "<b>" in p and "</b>" in p and "Pick" not in p and len(p) > len("Pick <b>{name}</b> now")


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_the_page_writes_dates_like_python(tmp_path):
    """i18n.js (Intl, as in WebView2) and i18n.py (tables) agree."""
    script = tmp_path / "run.js"
    script.write_text(f"""
globalThis.window = globalThis; globalThis.document = {{ documentElement: {{}} }}; globalThis.localStorage = {{ setItem() {{}} }};
require({json.dumps(str(WEB / "i18n.js"))});
const out = {{}};
for (const lang of ["en", "tr"]) {{
  t.load({{ lang, table: {{ "{{m}} min": "{{m}} dk", "{{h}} h {{m}} min": "{{h}} sa {{m}} dk" }} }});
  out[lang] = [t.pct(0.45), t.num(31200.5, 1), t.dur(3900), t.list(["A", "B", "C"])];
  for (let m = 1; m <= 12; m++) {{
    const iso = `2026-${{String(m).padStart(2, "0")}}-14`;
    out[lang].push(t.date(iso, "long"), t.date(iso, "short"), t.date(iso, "dm"));
  }}
}}
console.log(JSON.stringify(out));
""", encoding="utf-8")
    got = json.loads(subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8", check=True).stdout)
    for lang in ("en", "tr"):
        i18n.set_language(lang)
        try:
            want = [i18n.pct(0.45), i18n.num(31200.5, 1), i18n.duration(3900), i18n.join(["A", "B", "C"])]
            for d in (dt.date(2026, m, 14) for m in range(1, 13)):           # every month (en-GB Intl writes "Sept")
                want += [i18n.date_long(d), i18n.date_short(d), i18n.day_month(d)]
            assert got[lang] == want
        finally:
            i18n.set_language("en")
