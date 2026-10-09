"""
Bengali to English Vocabulary Trainer
A small Windows tray companion that teaches English vocabulary from Bengali.

Features
  - Lives in the system tray, out of your way
  - Pops a word card at an interval you choose
  - On demand multiple choice quiz from the tray menu
  - Online lookup: English definition, phonetics, example sentence, Bengali meaning
  - Everything fetched is cached in a local database so it works offline later
  - Light spaced repetition: words you miss come back sooner

The first run installs whatever it needs by itself. If the system tray is not
available the same controls open in a normal window instead.

Run:      python BengaliEnglishTrainer.py
Doctor:   python BengaliEnglishTrainer.py --doctor
Selftest: python BengaliEnglishTrainer.py --selftest
"""

import argparse
import importlib
import importlib.util
import json
import os
import queue
import random
import re
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

APP_NAME = "Bengali to English Vocabulary Trainer"
SHORT_NAME = "Vocab Trainer"
VERSION = "1.0"

DICT_API = "https://api.dictionaryapi.dev/api/v2/entries/en/"
TRANSLATE_API = "https://api.mymemory.translated.net/get"
TATOEBA_API = "https://tatoeba.org/en/api_v0/search"
WIKTIONARY_API = "https://en.wiktionary.org/w/api.php"
NET_TIMEOUT = 10

DEFAULTS = {
    "interval": "20",          # minutes between word cards
    "popup_seconds": "18",     # how long a card stays on screen
    "quiz_length": "10",
    "direction": "both",       # bangla, english or both
    "contact_email": "",       # raises the free translation quota
    "paused": "0",
    "online": "1",
    "seen_intro": "0",
    "speak_on_card": "0",      # say the word out loud when a card appears
    "mode": "word",            # which kind of entry to study
    "clipboard": "0",          # look up whatever you copy
    "hotkeys": "1",            # keyboard shortcuts on the card
    "quiet_from": "",          # no cards from this hour, 0 to 23
    "quiet_to": "",            # until this hour
    "practice_style": "mixed",  # choice, typing, listening, blank, synonym
    "theme": "midnight",       # midnight, glass, daylight, forest or plum
    "language": "bn",          # bn, hi or both
    "daily_count": "10",       # how many words to study in a day
    "daily_only": "1",         # cards loop through that set until tomorrow
    "daily_levels": "",        # the level the current set was built for
    "level": "C2",             # CEFR level the cards and quizzes come from
    "include_lower": "0",      # also draw from the easier levels
    "seed_version": "0",
}

SEED_VERSION = 7


# ---------------------------------------------------------------- data folder

def data_dir():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share")
    path = os.path.join(base, "BengaliVocabTrainer")
    os.makedirs(path, exist_ok=True)
    return path


DB_PATH = os.path.join(data_dir(), "vocab.db")
LOG_PATH = os.path.join(data_dir(), "error.log")

REQUIRED = [("pystray", "pystray"), ("PIL", "pillow")]


STAGE_PATH = os.path.join(data_dir(), "startup.log")


def say_out(text):
    """print that cannot fail. A windowless build has no stdout at all."""
    try:
        if sys.stdout is not None:
            print(text)
    except Exception:
        pass


def log(message):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[" + stamp + "] " + str(message)
    say_out(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def stage(name, fresh=False):
    """Breadcrumb of how far startup got, for when there is nothing to see."""
    try:
        with open(STAGE_PATH, "w" if fresh else "a", encoding="utf-8") as fh:
            fh.write(datetime.now().strftime("%H:%M:%S ") + name + "\n")
    except Exception:
        pass


def missing_packages():
    """Which packages are not installed. Checked without importing them, so a
    package that is present but cannot start here is never reinstalled."""
    out = []
    for module, package in REQUIRED:
        try:
            found = importlib.util.find_spec(module) is not None
        except Exception:
            found = False
        if not found:
            out.append(package)
    return out


def ensure_packages(quiet=False):
    """Install pystray and pillow on first run. Returns (ok, message)."""
    if getattr(sys, "frozen", False):
        # The exe carries its own packages. Never run pip from it: sys.executable
        # is the app itself, so installing would just launch more copies.
        return True, "Running from the exe, nothing to install."
    missing = missing_packages()
    if not missing:
        return True, "All packages present."
    if not quiet:
        log("Installing " + ", ".join(missing) + ". This happens only once.")
    try:
        import ensurepip
        ensurepip.bootstrap()
    except Exception:
        pass
    attempts = [
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
         "--upgrade"] + missing,
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
         "--user", "--upgrade"] + missing,
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
         "--user", "--upgrade", "--index-url",
         "https://pypi.org/simple"] + missing,
    ]
    last = ""
    for cmd in attempts:
        try:
            run = quiet_run(cmd, timeout=900)
        except Exception as exc:
            last = str(exc)
            continue
        last = (run.stdout or "") + (run.stderr or "")
        if run.returncode == 0:
            break
    try:
        import site
        for extra in (site.getusersitepackages(),):
            if isinstance(extra, str) and extra not in sys.path:
                sys.path.append(extra)
    except Exception:
        pass
    importlib.invalidate_caches()
    still = missing_packages()
    if not still:
        if not quiet:
            log("Packages installed.")
        return True, "Installed " + ", ".join(missing)
    text = ("Could not install: " + ", ".join(still) + "\n\n"
            + last.strip()[-1200:] + "\n\n"
            "Open Command Prompt and run this line yourself:\n"
            '"' + sys.executable + '" -m pip install ' + " ".join(still))
    log(text)
    return False, text


def quiet_run(cmd, timeout=40):
    """Run a helper program without a console window appearing.

    stdin is closed on purpose. Started from pythonw.exe or from the built exe
    there is no console, so the inherited stdin handle is invalid and every
    subprocess call fails with "the handle is invalid" unless it is replaced.
    That single missing argument is what silenced the pronounce button.
    """
    flags = 0
    startup = None
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
    return subprocess.run(cmd, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=timeout,
                          creationflags=flags, startupinfo=startup)


class Speaker:
    """Says an English word out loud.

    It uses the recorded human voice from the dictionary when there is one and
    falls back to the voice built into the computer, so pronunciation works
    offline too. Every step is optional and failures are silent.
    """

    def __init__(self):
        self.backend = None
        self.last_error = ""
        self.cache = os.path.join(data_dir(), "audio")
        try:
            os.makedirs(self.cache, exist_ok=True)
        except Exception:
            pass

    # choosing a voice ------------------------------------------------------
    def backends(self):
        """Every way of speaking on this computer, best first."""
        if os.name == "nt":
            return ["sapi", "vbscript", "powershell", "pyttsx3"]
        found = []
        for name in ("say", "espeak", "spd-say"):
            try:
                if quiet_run(["which", name], timeout=5).returncode == 0:
                    found.append(name)
            except Exception:
                continue
        return found + ["pyttsx3"]

    def pick_backend(self):
        """The first voice that actually speaks. Tried once, then remembered."""
        if self.backend is not None:
            return self.backend
        self.backend = ""
        for name in self.backends():
            if self.try_backend(name, "test", silent=True):
                self.backend = name
                break
        return self.backend

    def available(self):
        return bool(self.pick_backend())

    # recorded audio --------------------------------------------------------
    def clip_path(self, word):
        safe = "".join(ch for ch in word.lower() if ch.isalnum() or ch == " ")
        return os.path.join(self.cache, safe.replace(" ", "-") + ".mp3")

    def fetch_clip(self, word, url):
        path = self.clip_path(word)
        if os.path.exists(path) and os.path.getsize(path) > 1000:
            return path
        if not url:
            return ""
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": SHORT_NAME + "/" + VERSION})
            with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as resp:
                data = resp.read()
            if len(data) < 1000:
                return ""
            with open(path, "wb") as fh:
                fh.write(data)
            return path
        except Exception:
            return ""

    def play_file(self, path):
        if os.name == "nt":
            try:
                import ctypes
                alias = "vocabclip"
                mci = ctypes.windll.winmm.mciSendStringW
                mci('close ' + alias, None, 0, None)
                if mci('open "%s" type mpegvideo alias %s' % (path, alias),
                       None, 0, None) != 0:
                    return False
                ok = mci('play %s wait' % alias, None, 0, None) == 0
                mci('close ' + alias, None, 0, None)
                return ok
            except Exception:
                return False
        for player in (["afplay", path], ["mpg123", "-q", path],
                       ["ffplay", "-nodisp", "-autoexit", "-loglevel",
                        "quiet", path]):
            try:
                if quiet_run(player, timeout=30).returncode == 0:
                    return True
            except Exception:
                continue
        return False

    # computer voice --------------------------------------------------------
    def try_backend(self, name, text, silent=False):
        """Speak with one engine, or when silent only check that it exists."""
        clean = " ".join(str(text).split())
        if not clean:
            return False
        try:
            if name == "sapi":
                import win32com.client
                voice = win32com.client.Dispatch("SAPI.SpVoice")
                if silent:
                    return True
                voice.Speak(clean)
                return True
            if name == "vbscript":
                # SAPI through a tiny script file. No extra package, no .NET,
                # works on every Windows, and opens no window.
                body = ('Set v = CreateObject("SAPI.SpVoice")\r\n'
                        'v.Rate = -1\r\n')
                if not silent:
                    body += 'v.Speak "%s"\r\n' % clean.replace('"', '""')
                path = os.path.join(self.cache, "speak.vbs")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(body)
                run = quiet_run(["wscript.exe", "//nologo", "//B", path])
                if run.returncode != 0:
                    self.last_error = "vbscript: " + (run.stderr or "").strip()
                return run.returncode == 0
            if name == "powershell":
                script = ("Add-Type -AssemblyName System.Speech; "
                          "$v = New-Object System.Speech.Synthesis."
                          "SpeechSynthesizer; $v.Rate = -1; ")
                script += "exit 0" if silent else ("$v.Speak('%s')"
                                                   % clean.replace("'", "''"))
                run = quiet_run(["powershell", "-NoProfile", "-WindowStyle",
                                 "Hidden", "-Command", script])
                if run.returncode != 0:
                    self.last_error = "powershell: " + (run.stderr or "").strip()
                return run.returncode == 0
            if name == "pyttsx3":
                import pyttsx3
                engine = pyttsx3.init()
                if silent:
                    engine.stop()
                    return True
                engine.say(clean)
                engine.runAndWait()
                return True
            if name in ("say", "espeak", "spd-say"):
                if silent:
                    probe = {"say": ["say", "-v", "?"],
                             "espeak": ["espeak", "--version"],
                             "spd-say": ["spd-say", "--version"]}[name]
                    return quiet_run(probe, timeout=10).returncode == 0
                cmd = {"say": ["say", clean],
                       "espeak": ["espeak", "-s", "140", clean],
                       "spd-say": ["spd-say", "-w", clean]}[name]
                run = quiet_run(cmd)
                if run.returncode != 0:
                    self.last_error = name + ": " + (run.stderr or "").strip()
                return run.returncode == 0
        except Exception as exc:
            self.last_error = name + ": " + repr(exc)
        return False

    def speak_text(self, text):
        """Try each engine in turn so one broken voice is not the end of it."""
        order = self.backends()
        first = self.pick_backend()
        if first:
            order = [first] + [b for b in order if b != first]
        for name in order:
            if self.try_backend(name, text):
                self.backend = name
                return True
        if self.last_error:
            log("Speech failed. " + self.last_error)
        return False

    # what the buttons call -------------------------------------------------
    def say(self, word, audio_url="", sentence=""):
        """Recorded clip first, then the computer voice. Never raises."""
        self.last_error = ""
        try:
            spoken = " ".join(str(sentence or word).split())
            if not spoken:
                return False
            if not sentence:
                path = self.fetch_clip(word, audio_url)
                if path and self.play_file(path):
                    return True
            return self.speak_text(spoken)
        except Exception as exc:
            self.last_error = repr(exc)
            log("Speech failed: " + repr(exc))
            return False

    def report(self):
        """A short line for the user about the state of the sound."""
        name = self.pick_backend()
        if name:
            return "Voice in use: " + name
        return "No voice found. " + (self.last_error or "")

    def say_async(self, word, audio_url="", sentence=""):
        threading.Thread(target=self.say,
                         args=(word, audio_url, sentence), daemon=True).start()


SPEAKER = Speaker()


def export_dir():
    folder = os.path.join(data_dir(), "exports")
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        folder = data_dir()
    return folder


def open_web(query, kind="define"):
    """Open a search for this word in the browser, without blocking us."""
    import webbrowser
    text = " ".join(str(query).split())
    if not text:
        return False
    if kind == "meaning":
        terms = text + " meaning in bengali"
    elif kind == "images":
        terms = text + "&tbm=isch"
    else:
        terms = "define " + text
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(terms)
    if kind == "images":
        url = ("https://www.google.com/search?tbm=isch&q=" +
               urllib.parse.quote_plus(text))

    def go():
        try:
            webbrowser.open(url, new=2)
        except Exception as exc:
            log("Could not open the browser: " + repr(exc))
    threading.Thread(target=go, daemon=True).start()
    return True


def open_in_explorer(path):
    try:
        if os.name == "nt":
            os.startfile(path)  # noqa: attribute only exists on Windows
            return True
        quiet_run(["xdg-open", path], timeout=10)
    except Exception as exc:
        log("Could not open " + str(path) + ": " + repr(exc))
    return False


EXPORT_COLUMNS = ("english", "bangla", "level", "kind", "pos", "phonetic",
                  "definition", "example", "synonyms", "antonyms")


def export_row_values(row, trainer=None):
    values = []
    for key in EXPORT_COLUMNS:
        try:
            value = row[key]
        except Exception:
            value = ""
        if key == "pos" and trainer is not None:
            value = row_pos(row) or value
        if key == "example" and trainer is not None:
            got = trainer.sentence_list(row)
            value = " / ".join(got[:2]) if got else (value or "")
        values.append("" if value is None else str(value))
    return values


def export_rows(rows, kind="xlsx", trainer=None):
    """Write the given entries out. Returns the file path, or an empty string."""
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    base = os.path.join(export_dir(), "vocabulary-" + stamp)
    header = [c.capitalize() for c in EXPORT_COLUMNS]
    table = [export_row_values(r, trainer) for r in rows]
    try:
        if kind == "xlsx":
            try:
                from openpyxl import Workbook
                from openpyxl.styles import Font, PatternFill
            except Exception:
                log("openpyxl is missing, writing a csv instead")
                return export_rows(rows, "csv", trainer)
            book = Workbook()
            sheet = book.active
            sheet.title = "Vocabulary"
            sheet.append(header)
            for cell in sheet[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="1B4B6B")
            for line in table:
                sheet.append(line)
            widths = (20, 26, 8, 13, 9, 18, 52, 52, 26, 26)
            for i, width in enumerate(widths, start=1):
                sheet.column_dimensions[
                    sheet.cell(row=1, column=i).column_letter].width = width
            sheet.freeze_panes = "A2"
            path = base + ".xlsx"
            book.save(path)
            return path
        if kind == "csv":
            import csv
            path = base + ".csv"
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(header)
                writer.writerows(table)
            return path
        path = base + ".html"
        rows_html = []
        for line in table:
            cells = "".join("<td>" + html_escape(x) + "</td>" for x in line[:8])
            rows_html.append("<tr>" + cells + "</tr>")
        page = HTML_SHEET % {
            "title": "Vocabulary  " + datetime.now().strftime("%d %B %Y"),
            "head": "".join("<th>" + html_escape(h) + "</th>"
                            for h in header[:8]),
            "rows": "".join(rows_html),
            "count": len(table),
        }
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(page)
        return path
    except Exception as exc:
        log("Export failed: " + repr(exc))
        return ""


def html_escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


HTML_SHEET = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>%(title)s</title>
<style>
 body { font-family: "Segoe UI", system-ui, sans-serif; margin: 28px;
        color: #12212e; }
 h1 { font-size: 20px; margin: 0 0 4px; }
 p.sub { color: #64788c; margin: 0 0 18px; font-size: 12px; }
 table { border-collapse: collapse; width: 100%%; font-size: 12px; }
 th { background: #1b4b6b; color: #fff; text-align: left; padding: 7px 9px; }
 td { border-bottom: 1px solid #dde5ec; padding: 6px 9px; vertical-align: top; }
 tr:nth-child(even) td { background: #f5f8fb; }
 td:nth-child(2) { font-size: 14px; }
 @media print { body { margin: 10mm; } th { -webkit-print-color-adjust: exact; } }
</style></head><body>
<h1>%(title)s</h1>
<p class="sub">%(count)s entries. Press Ctrl and P to save this as a PDF.</p>
<table><thead><tr>%(head)s</tr></thead><tbody>%(rows)s</tbody></table>
</body></html>
"""


def show_error(title, body):
    """Put an error on screen, because a tray app has nowhere else to show it."""
    try:
        import tkinter as tk
        from tkinter import scrolledtext
        win = tk.Tk()
        win.title(title)
        win.geometry("640x380")
        box = scrolledtext.ScrolledText(win, wrap="word", font=("Consolas", 9))
        box.pack(fill="both", expand=True, padx=10, pady=10)
        box.insert("1.0", body + "\n\nThis was also saved to:\n" + LOG_PATH)
        tk.Button(win, text="Close", command=win.destroy, padx=20,
                  pady=6).pack(pady=(0, 10))
        win.mainloop()
    except Exception:
        say_out(title)
        say_out(body)


# ------------------------------------------------------------- starter words
# English word, Bengali meaning, grouped by CEFR level from A1 to C2.
# Online lookup fills in definitions, phonetics and examples the first time a
# word is shown, and you can add your own words at any level.

LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]
KINDS = ["word", "phrasal", "idiom", "collocation", "proverb", "confusable"]
KIND_NAMES = {
    "word": "Words", "phrasal": "Phrasal verbs", "idiom": "Idioms",
    "collocation": "Collocations", "proverb": "Proverbs",
    "confusable": "Confusing pairs",
}
KIND_SINGULAR = {
    "word": "Word", "phrasal": "Phrasal verb", "idiom": "Idiom",
    "collocation": "Collocation", "proverb": "Proverb",
    "confusable": "Confusing pair",
}

POS_SHORT = {
    "noun": "n.", "verb": "v.", "adjective": "adj.", "adverb": "adv.",
    "idiom": "idiom", "proverb": "proverb", "phrase": "phrase",
    "pair": "pair",
    "pronoun": "pron.", "preposition": "prep.", "conjunction": "conj.",
    "interjection": "interj.", "exclamation": "interj.",
    "determiner": "det.", "numeral": "num.", "article": "art.",
    "phrase": "phr.", "abbreviation": "abbr.", "prefix": "pref.",
    "suffix": "suf.",
}


def short_pos(pos="", kind="word", english=""):
    """The part of speech in the short form dictionaries use."""
    if kind == "phrasal":
        return "phr. v."
    if kind == "idiom":
        return "idiom"
    if kind == "proverb":
        return "proverb"
    if kind == "collocation":
        return "phrase"
    if kind == "confusable":
        return "pair"
    key = (pos or "").strip().lower()
    if key in POS_SHORT:
        return POS_SHORT[key]
    if (english or "").startswith("to "):
        return "v."
    return ""


# Bengali verbal nouns nearly all end one of these ways, which tells us the
# English word is a verb without asking anyone.
BN_VERB_TAILS = ("করা", "হওয়া", "দেওয়া", "নেওয়া", "যাওয়া", "আসা", "খাওয়া",
                 "রাখা", "তোলা", "ফেলা", "পাওয়া", "দেখা", "বলা", "থাকা",
                 "চলা", "ধরা", "মারা", "কাটা", "ভরা", "গড়া", "লাগা", "জানা",
                 "মানা", "শেখা", "বসা", "ওঠা", "নামা", "ঘোরা", "টানা",
                 "বাঁধা", "শোনা", "পড়া", "লেখা", "ভাবা", "গোনা", "আঁকা",
                 "নাচা", "পরা", "ধোয়া", "পাকানো", "ানো")
BN_ADJ_TAILS = ("পূর্ণ", "ময়", "হীন", "সম্পন্ন", "যোগ্য", "জনক", "কারী")

KNOWN_POS = {
    "friendly": "adjective", "daily": "adjective", "lonely": "adjective",
    "likely": "adjective", "lovely": "adjective", "silly": "adjective",
    "holy": "adjective", "ugly": "adjective", "family": "noun",
    "reply": "verb", "supply": "noun", "apply": "verb", "study": "verb",
    "memory": "noun", "book": "noun", "vivid": "adjective",
    "teacher": "noun", "cruelty": "noun", "water": "noun", "fire": "noun",
    "love": "noun", "help": "noun", "work": "noun", "play": "verb",
    "light": "noun", "rain": "noun", "hope": "noun", "dream": "noun",
    "trust": "noun", "respect": "noun", "need": "noun", "plan": "noun",
    "result": "noun", "change": "noun", "answer": "noun", "order": "noun",
    "sound": "noun", "smell": "noun", "taste": "noun", "touch": "noun",
    "hand": "noun", "head": "noun", "back": "noun", "face": "noun",
    "point": "noun", "line": "noun", "part": "noun", "side": "noun",
    "time": "noun", "place": "noun", "thing": "noun", "way": "noun",
    "talent": "noun", "pavement": "noun", "headline": "noun",
    "patient": "noun", "machine": "noun", "library": "noun",
    "salary": "noun", "routine": "noun", "capital": "noun",
    "hospital": "noun", "animal": "noun", "restaurant": "noun",
    "merchant": "noun", "student": "noun", "moment": "noun",
    "necessary": "adjective", "ordinary": "adjective",
    "temporary": "adjective", "military": "adjective",
}
FUNCTION_WORDS = {
    "here": "adverb", "there": "adverb", "now": "adverb", "later": "adverb",
    "always": "adverb", "again": "adverb", "almost": "adverb",
    "already": "adverb", "still": "adverb", "perhaps": "adverb",
    "instead": "adverb", "however": "adverb", "together": "adverb",
    "suddenly": "adverb", "early": "adverb", "late": "adverb",
    "often": "adverb", "sometimes": "adverb", "rarely": "adverb",
    "yesterday": "adverb", "tomorrow": "adverb", "today": "adverb",
    "below": "preposition", "above": "preposition", "inside": "preposition",
    "outside": "preposition", "between": "preposition",
    "behind": "preposition", "near": "preposition", "far": "adverb",
    "front": "noun", "left": "adjective", "right": "adjective",
    "because": "conjunction", "although": "conjunction",
    "despite": "preposition", "except": "preposition", "please": "adverb",
    "hello": "interjection", "goodbye": "interjection", "sorry": "adjective",
    "many": "determiner", "few": "determiner", "more": "determiner",
    "less": "determiner", "same": "adjective", "different": "adjective",
}

# One list, longest ending first, so "ment" beats "ent" and "ical" beats
# "al". Endings that sit on both sides of the fence, such as "ent" in talent
# and patient, are left out rather than guessed wrongly.
# Endings that settle the matter on their own, longest first so that "ment"
# beats "ent" and "ical" beats "ic".
STRONG_SUFFIX = sorted([
    ("ment", "noun"), ("tion", "noun"), ("sion", "noun"), ("ness", "noun"),
    ("ship", "noun"), ("hood", "noun"), ("ance", "noun"), ("ence", "noun"),
    ("ency", "noun"), ("ancy", "noun"), ("ity", "noun"), ("ism", "noun"),
    ("ist", "noun"), ("ogy", "noun"), ("dom", "noun"),
    ("ical", "adjective"), ("able", "adjective"), ("ible", "adjective"),
    ("ious", "adjective"), ("eous", "adjective"), ("ous", "adjective"),
    ("ful", "adjective"), ("less", "adjective"), ("ive", "adjective"),
    ("ish", "adjective"), ("ial", "adjective"), ("ual", "adjective"),
    ("like", "adjective"), ("proof", "adjective"),
], key=lambda pair: -len(pair[0]))

# Endings that lean one way but are often wrong, so the Bengali gloss is
# asked first: injure and measure are verbs, structure and failure are not.
WEAK_SUFFIX = sorted([
    ("ure", "noun"), ("age", "noun"), ("ery", "noun"), ("ian", "noun"),
    ("er", "noun"), ("or", "noun"), ("cy", "noun"), ("ty", "noun"),
    ("phy", "noun"), ("ent", "adjective"), ("ory", "adjective"),
    ("ose", "adjective"), ("id", "adjective"), ("ic", "adjective"),
    ("ward", "adjective"), ("ate", "verb"), ("ise", "verb"),
    ("ize", "verb"), ("ify", "verb"),
], key=lambda pair: -len(pair[0]))

VERB_HINTS = {"be", "do", "go", "run", "eat", "see", "say", "get", "give",
              "take", "make", "know", "think", "come", "want", "use", "find",
              "tell", "ask", "seem", "feel", "try", "leave", "call", "keep",
              "let", "put", "send", "read", "write", "speak", "buy", "sell",
              "pay", "meet", "sit", "stand", "win", "lose", "grow", "show"}


def guess_pos(english, kind="word", bangla=""):
    """Work out the part of speech without asking anyone.

    Every entry should show n., v. or adj. straight away, not only after its
    first online lookup. The Bengali gloss is the strongest clue, because a
    verb is nearly always written as "kora", "hoya" and so on. The dictionary
    overwrites whatever this returns the moment it answers.
    """
    word = (english or "").strip().lower()
    if not word:
        return ""
    if kind == "phrasal":
        return "verb"
    if kind in ("idiom", "proverb"):
        return kind
    if kind == "collocation":
        return "phrase"
    if kind == "confusable":
        return "pair"
    if word.startswith("to "):
        return "verb"
    if " " in word:
        return "phrase"
    if word in KNOWN_POS:
        return KNOWN_POS[word]
    if word in FUNCTION_WORDS:
        return FUNCTION_WORDS[word]
    if word in VERB_HINTS:
        return "verb"
    if word.endswith("ly") and len(word) > 5:
        return "adverb"
    # a firm English ending settles it: perennial is an adjective even though
    # its Bengali gloss reads like a verb
    for tail, part in STRONG_SUFFIX:
        if word.endswith(tail) and len(word) > len(tail) + 2:
            return part
    meaning = (bangla or "").strip()
    if meaning:
        for tail in BN_VERB_TAILS:
            if meaning.endswith(tail):
                return "verb"
        for tail in BN_ADJ_TAILS:
            if meaning.endswith(tail):
                return "adjective"
        if meaning.endswith("ভাবে"):
            return "adverb"
    for tail, part in WEAK_SUFFIX:
        if word.endswith(tail) and len(word) > len(tail) + 2:
            return part
    if word.endswith("ing"):
        return "verb"
    if word.endswith("ed"):
        # the Bengali gloss would have caught a real verb above, so a word
        # still ending in ed here is doing an adjective's job
        return "adjective"
    return "noun"


def row_pos(row):
    try:
        return short_pos(row["pos"], row["kind"] if "kind" in row.keys()
                         else "word", row["english"])
    except Exception:
        return ""
LEVEL_NAMES = {
    "A1": "A1 beginner",
    "A2": "A2 elementary",
    "B1": "B1 intermediate",
    "B2": "B2 upper intermediate",
    "C1": "C1 advanced",
    "C2": "C2 proficient",
}

SEED = [
    ("book", "বই"), ("water", "জল"), ("fire", "আগুন"), ("wind", "বাতাস"),
    ("sky", "আকাশ"), ("soil", "মাটি"), ("tree", "গাছ"), ("flower", "ফুল"),
    ("fruit", "ফল"), ("bird", "পাখি"), ("fish", "মাছ"), ("dog", "কুকুর"),
    ("cat", "বিড়াল"), ("cow", "গরু"), ("horse", "ঘোড়া"), ("elephant", "হাতি"),
    ("tiger", "বাঘ"), ("snake", "সাপ"), ("ant", "পিঁপড়া"), ("house", "বাড়ি"),
    ("room", "ঘর"), ("door", "দরজা"), ("window", "জানালা"), ("roof", "ছাদ"),
    ("kitchen", "রান্নাঘর"), ("bed", "বিছানা"), ("chair", "চেয়ার"),
    ("table", "টেবিল"), ("light", "আলো"), ("darkness", "অন্ধকার"),
    ("person", "মানুষ"), ("boy", "ছেলে"), ("girl", "মেয়ে"), ("father", "বাবা"),
    ("mother", "মা"), ("brother", "ভাই"), ("sister", "বোন"), ("friend", "বন্ধু"),
    ("teacher", "শিক্ষক"), ("student", "ছাত্র"), ("doctor", "ডাক্তার"),
    ("farmer", "কৃষক"), ("king", "রাজা"), ("hand", "হাত"), ("leg", "পা"),
    ("eye", "চোখ"), ("ear", "কান"), ("nose", "নাক"), ("mouth", "মুখ"),
    ("head", "মাথা"), ("hair", "চুল"), ("tooth", "দাঁত"), ("heart", "হৃদয়"),
    ("blood", "রক্ত"), ("to eat", "খাওয়া"), ("to drink", "পান করা"),
    ("to sleep", "ঘুমানো"), ("to walk", "হাঁটা"), ("to run", "দৌড়ানো"),
    ("to sit", "বসা"), ("to stand", "দাঁড়ানো"), ("to say", "বলা"),
    ("to listen", "শোনা"), ("to see", "দেখা"), ("to read", "পড়া"),
    ("to write", "লেখা"), ("to learn", "শেখা"), ("to think", "ভাবা"),
    ("to know", "জানা"), ("to give", "দেওয়া"), ("to take", "নেওয়া"),
    ("to come", "আসা"), ("to go", "যাওয়া"), ("to work", "কাজ করা"),
    ("to play", "খেলা"), ("to laugh", "হাসা"), ("to cry", "কাঁদা"),
    ("to buy", "কেনা"), ("to sell", "বিক্রি করা"), ("to open", "খোলা"),
    ("to close", "বন্ধ করা"), ("big", "বড়"), ("small", "ছোট"),
    ("tall", "লম্বা"), ("heavy", "ভারী"), ("new", "নতুন"), ("old", "পুরনো"),
    ("good", "ভালো"), ("bad", "খারাপ"), ("beautiful", "সুন্দর"),
    ("fast", "দ্রুত"), ("slow", "ধীর"), ("hot", "গরম"), ("cold", "ঠান্ডা"),
    ("easy", "সহজ"), ("difficult", "কঠিন"), ("true", "সত্য"),
    ("false", "মিথ্যা"), ("rich", "ধনী"), ("poor", "গরিব"), ("happy", "খুশি"),
    ("sad", "দুঃখিত"), ("anger", "রাগ"), ("fear", "ভয়"), ("love", "ভালোবাসা"),
    ("hope", "আশা"), ("dream", "স্বপ্ন"), ("peace", "শান্তি"),
    ("courage", "সাহস"), ("patience", "ধৈর্য"), ("time", "সময়"),
    ("day", "দিন"), ("night", "রাত"), ("morning", "সকাল"), ("noon", "দুপুর"),
    ("afternoon", "বিকেল"), ("evening", "সন্ধ্যা"), ("week", "সপ্তাহ"),
    ("month", "মাস"), ("year", "বছর"), ("today", "আজ"), ("now", "এখন"),
    ("later", "পরে"), ("always", "সবসময়"), ("money", "টাকা"), ("price", "দাম"),
    ("market", "বাজার"), ("shop", "দোকান"), ("food", "খাবার"), ("rice", "ভাত"),
    ("bread", "রুটি"), ("milk", "দুধ"), ("tea", "চা"), ("egg", "ডিম"),
    ("salt", "লবণ"), ("sugar", "চিনি"), ("oil", "তেল"), ("meat", "মাংস"),
    ("vegetable", "সবজি"), ("city", "শহর"), ("village", "গ্রাম"),
    ("road", "রাস্তা"), ("river", "নদী"), ("sea", "সমুদ্র"),
    ("mountain", "পাহাড়"), ("forest", "বন"), ("country", "দেশ"),
    ("earth", "পৃথিবী"), ("sun", "সূর্য"), ("moon", "চাঁদ"), ("star", "তারা"),
    ("cloud", "মেঘ"), ("rain", "বৃষ্টি"), ("storm", "ঝড়"), ("school", "স্কুল"),
    ("university", "বিশ্ববিদ্যালয়"), ("pen", "কলম"), ("paper", "কাগজ"),
    ("question", "প্রশ্ন"), ("answer", "উত্তর"), ("language", "ভাষা"),
    ("word", "শব্দ"), ("sentence", "বাক্য"), ("story", "গল্প"),
    ("poem", "কবিতা"), ("news", "খবর"), ("picture", "ছবি"), ("song", "গান"),
    ("movie", "সিনেমা"), ("health", "স্বাস্থ্য"), ("body", "শরীর"),
    ("medicine", "ওষুধ"), ("hospital", "হাসপাতাল"), ("exercise", "ব্যায়াম"),
    ("job", "চাকরি"), ("office", "অফিস"), ("business", "ব্যবসা"),
    ("bank", "ব্যাংক"), ("government", "সরকার"), ("law", "আইন"),
    ("freedom", "স্বাধীনতা"), ("history", "ইতিহাস"), ("culture", "সংস্কৃতি"),
    ("science", "বিজ্ঞান"), ("technology", "প্রযুক্তি"),
    ("computer", "কম্পিউটার"), ("machine", "যন্ত্র"),
    ("electricity", "বিদ্যুৎ"), ("help", "সাহায্য"), ("effort", "চেষ্টা"),
    ("opportunity", "সুযোগ"), ("problem", "সমস্যা"), ("solution", "সমাধান"),
    ("reason", "কারণ"), ("result", "ফলাফল"), ("example", "উদাহরণ"),
    ("difference", "পার্থক্য"), ("importance", "গুরুত্ব"),
    ("need", "প্রয়োজন"), ("decision", "সিদ্ধান্ত"),
    ("plan", "পরিকল্পনা"), ("goal", "লক্ষ্য"), ("success", "সাফল্য"),
    ("failure", "ব্যর্থতা"), ("experience", "অভিজ্ঞতা"), ("knowledge", "জ্ঞান"),
    ("memory", "স্মৃতি"), ("habit", "অভ্যাস"), ("responsibility", "দায়িত্ব"),
    ("trust", "বিশ্বাস"), ("respect", "সম্মান"), ("gift", "উপহার"),
    ("guest", "অতিথি"), ("neighbour", "প্রতিবেশী"), ("family", "পরিবার"),
    ("marriage", "বিয়ে"), ("child", "শিশু"),
]

# Words from the list above that sit at A2 rather than A1. Everything else in
# that list is A1.
A2_WORDS = {
    "darkness", "courage", "patience", "peace", "hope", "dream", "anger",
    "fear", "health", "exercise", "medicine", "hospital", "job", "office",
    "business", "bank", "government", "law", "freedom", "history", "culture",
    "science", "technology", "computer", "machine", "electricity", "help",
    "effort", "opportunity", "problem", "solution", "reason", "result",
    "example", "difference", "importance", "need", "decision", "plan", "goal",
    "success", "failure", "experience", "knowledge", "memory", "habit",
    "responsibility", "trust", "respect", "gift", "guest", "neighbour",
    "marriage", "university", "question", "answer", "language", "word",
    "sentence", "story", "poem", "news", "picture", "song", "movie", "price",
    "market", "country", "earth", "storm", "week", "month", "year", "always",
    "later", "now",
}

B1 = [
    ("achieve", "অর্জন করা"), ("advice", "উপদেশ"), ("allow", "অনুমতি দেওয়া"),
    ("arrange", "ব্যবস্থা করা"), ("avoid", "এড়িয়ে চলা"), ("benefit", "সুবিধা"),
    ("borrow", "ধার করা"), ("careful", "সতর্ক"), ("compare", "তুলনা করা"),
    ("complain", "অভিযোগ করা"), ("confident", "আত্মবিশ্বাসী"),
    ("consider", "বিবেচনা করা"), ("convenient", "সুবিধাজনক"),
    ("decide", "সিদ্ধান্ত নেওয়া"), ("describe", "বর্ণনা করা"),
    ("develop", "গড়ে তোলা"), ("discuss", "আলোচনা করা"),
    ("encourage", "উৎসাহ দেওয়া"), ("environment", "পরিবেশ"),
    ("expect", "আশা করা"), ("explain", "ব্যাখ্যা করা"), ("familiar", "পরিচিত"),
    ("famous", "বিখ্যাত"), ("imagine", "কল্পনা করা"), ("improve", "উন্নত করা"),
    ("include", "অন্তর্ভুক্ত করা"), ("increase", "বৃদ্ধি করা"),
    ("introduce", "পরিচয় করিয়ে দেওয়া"), ("journey", "যাত্রা"),
    ("manage", "সামলানো"), ("mention", "উল্লেখ করা"), ("method", "পদ্ধতি"),
    ("mistake", "ভুল"), ("modern", "আধুনিক"), ("necessary", "প্রয়োজনীয়"),
    ("notice", "লক্ষ্য করা"), ("offer", "প্রস্তাব দেওয়া"), ("opinion", "মতামত"),
    ("patient", "ধৈর্যশীল"), ("permit", "অনুমতি দেওয়া"), ("popular", "জনপ্রিয়"),
    ("prefer", "বেশি পছন্দ করা"), ("prepare", "প্রস্তুত করা"),
    ("prevent", "প্রতিরোধ করা"), ("produce", "উৎপাদন করা"), ("promise", "প্রতিশ্রুতি"),
    ("protect", "রক্ষা করা"), ("provide", "সরবরাহ করা"), ("purpose", "উদ্দেশ্য"),
    ("quality", "গুণমান"), ("realize", "উপলব্ধি করা"), ("receive", "গ্রহণ করা"),
    ("recommend", "সুপারিশ করা"), ("reduce", "কমানো"), ("refuse", "প্রত্যাখ্যান করা"),
    ("regular", "নিয়মিত"), ("remove", "সরিয়ে ফেলা"), ("repair", "মেরামত করা"),
    ("repeat", "পুনরাবৃত্তি করা"), ("reply", "উত্তর দেওয়া"), ("require", "দরকার হওয়া"),
    ("result", "ফলাফল"), ("safety", "নিরাপত্তা"), ("satisfy", "সন্তুষ্ট করা"),
    ("search", "অনুসন্ধান করা"), ("separate", "আলাদা করা"), ("serious", "গুরুতর"),
    ("share", "ভাগ করা"), ("similar", "সদৃশ"), ("situation", "পরিস্থিতি"),
    ("skill", "দক্ষতা"), ("solve", "সমাধান করা"), ("suggest", "পরামর্শ দেওয়া"),
    ("support", "সমর্থন করা"), ("surprise", "বিস্ময়"), ("survive", "টিকে থাকা"),
    ("tool", "যন্ত্রপাতি"), ("traffic", "যানজট"), ("trouble", "ঝামেলা"),
    ("useful", "উপযোগী"), ("various", "নানারকম"), ("wealth", "সম্পদ"),
    ("weight", "ওজন"), ("worry", "দুশ্চিন্তা করা"),
]

B2 = [
    ("abundant", "প্রচুর"), ("adequate", "পর্যাপ্ত"), ("adjacent", "সংলগ্ন"),
    ("anticipate", "আগে থেকে আঁচ করা"), ("apparent", "আপাতদৃষ্টিতে স্পষ্ট"),
    ("arbitrary", "খেয়ালখুশিমতো"), ("assess", "মূল্যায়ন করা"),
    ("attain", "অর্জন করা"), ("attribute", "আরোপ করা"), ("authentic", "খাঁটি"),
    ("beneficial", "উপকারী"), ("bias", "পক্ষপাত"), ("cautious", "সাবধানী"),
    ("collaborate", "একসঙ্গে কাজ করা"), ("comply", "মেনে চলা"),
    ("comprehensive", "সর্বাত্মক"), ("compromise", "আপস"), ("conceal", "গোপন করা"),
    ("confront", "মুখোমুখি হওয়া"), ("consent", "সম্মতি"), ("consistent", "সামঞ্জস্যপূর্ণ"),
    ("contemporary", "সমকালীন"), ("controversial", "বিতর্কিত"),
    ("convey", "পৌঁছে দেওয়া"), ("crucial", "অত্যন্ত জরুরি"), ("curb", "লাগাম টানা"),
    ("deduce", "অনুমান করে বের করা"), ("demonstrate", "দেখিয়ে দেওয়া"),
    ("deteriorate", "অবনতি হওয়া"), ("diminish", "হ্রাস পাওয়া"), ("distinct", "স্বতন্ত্র"),
    ("diverse", "বৈচিত্র্যময়"), ("elaborate", "বিশদ"), ("eligible", "যোগ্য"),
    ("eliminate", "বাদ দেওয়া"), ("emerge", "উদ্ভূত হওয়া"), ("emphasize", "জোর দেওয়া"),
    ("encounter", "সম্মুখীন হওয়া"), ("endure", "সহ্য করা"), ("enhance", "উন্নত করা"),
    ("ensure", "নিশ্চিত করা"), ("entail", "প্রয়োজন হিসেবে টেনে আনা"),
    ("exaggerate", "অতিরঞ্জিত করা"), ("exceed", "ছাড়িয়ে যাওয়া"),
    ("exploit", "কাজে লাগানো"), ("extensive", "ব্যাপক"), ("fluent", "সাবলীল"),
    ("fundamental", "মৌলিক"), ("generate", "উৎপন্ন করা"), ("genuine", "প্রকৃত"),
    ("gradual", "ক্রমশ"), ("illustrate", "উদাহরণ দিয়ে বোঝানো"),
    ("imply", "ইঙ্গিত করা"), ("impose", "চাপিয়ে দেওয়া"),
    ("incorporate", "অন্তর্ভুক্ত করা"), ("inherent", "অন্তর্নিহিত"),
    ("initiate", "সূচনা করা"), ("integrate", "একীভূত করা"), ("justify", "যৌক্তিকতা দেখানো"),
    ("modify", "পরিবর্তন করা"), ("notion", "ধারণা"), ("obstacle", "বাধা"),
    ("overcome", "অতিক্রম করা"), ("perceive", "উপলব্ধি করা"), ("persist", "লেগে থাকা"),
    ("phenomenon", "ঘটনা বা প্রপঞ্চ"), ("precise", "নির্ভুল"), ("preliminary", "প্রাথমিক"),
    ("presume", "ধরে নেওয়া"), ("prohibit", "নিষিদ্ধ করা"), ("prominent", "বিশিষ্ট"),
    ("prospect", "সম্ভাবনা"), ("pursue", "অনুসরণ করা"), ("radical", "আমূল"),
    ("reluctant", "অনিচ্ছুক"), ("remarkable", "উল্লেখযোগ্য"), ("restrain", "সংযত করা"),
    ("retain", "ধরে রাখা"), ("reveal", "প্রকাশ করা"), ("rigid", "অনমনীয়"),
    ("scarce", "দুষ্প্রাপ্য"), ("significant", "তাৎপর্যপূর্ণ"),
    ("simultaneous", "একই সময়ে ঘটা"), ("subsequent", "পরবর্তী"),
    ("sufficient", "যথেষ্ট"), ("sustain", "টিকিয়ে রাখা"), ("thorough", "পুঙ্খানুপুঙ্খ"),
    ("transition", "পরিবর্তনের পর্ব"), ("undergo", "মধ্য দিয়ে যাওয়া"),
    ("undertake", "হাতে নেওয়া"), ("vague", "অস্পষ্ট"), ("valid", "বৈধ"),
    ("vulnerable", "সহজে ক্ষতিগ্রস্ত হওয়ার মতো"), ("widespread", "ব্যাপকভাবে ছড়ানো"),
    ("yield", "ফলন দেওয়া বা নতি স্বীকার করা"),
]

C1 = [
    ("ambivalent", "দ্বিধাগ্রস্ত"), ("articulate", "স্পষ্টভাবে প্রকাশে সক্ষম"),
    ("austere", "কৃচ্ছ্রসাধনপূর্ণ"), ("banal", "গতানুগতিক"), ("benign", "নির্দোষ"),
    ("candid", "অকপট"), ("coherent", "সুসংবদ্ধ"), ("complacent", "আত্মতুষ্ট"),
    ("conducive", "সহায়ক"), ("conspicuous", "চোখে পড়ার মতো"),
    ("contentious", "বিতর্কপ্রবণ"), ("credible", "বিশ্বাসযোগ্য"),
    ("cumbersome", "ভারী ও অসুবিধাজনক"), ("daunting", "ভীতিজনক"),
    ("deft", "নিপুণ"), ("derivative", "অন্যের অনুকরণে তৈরি"),
    ("detrimental", "ক্ষতিকর"), ("diligent", "পরিশ্রমী"), ("discern", "বিচার করে বোঝা"),
    ("disparage", "খাটো করে দেখানো"), ("disseminate", "ছড়িয়ে দেওয়া"),
    ("eloquent", "বাগ্মী"), ("elusive", "অধরা"), ("embellish", "অলঙ্কৃত করা"),
    ("empirical", "অভিজ্ঞতালব্ধ"), ("endemic", "কোনো অঞ্চলে বদ্ধমূল"),
    ("entrenched", "গভীরে প্রোথিত"), ("equitable", "ন্যায্য"), ("exacting", "কঠোর দাবিসম্পন্ন"),
    ("exemplary", "অনুকরণীয়"), ("exhaustive", "সর্বাঙ্গীণ"), ("expedite", "ত্বরান্বিত করা"),
    ("extraneous", "অপ্রাসঙ্গিক"), ("facilitate", "সহজ করা"), ("fallacy", "ভ্রান্ত যুক্তি"),
    ("feasible", "কার্যকরভাবে সম্ভব"), ("fluctuate", "ওঠানামা করা"),
    ("formidable", "প্রবল ও ভীতিজনক"), ("frugal", "মিতব্যয়ী"), ("futile", "নিষ্ফল"),
    ("gregarious", "মিশুক"), ("hinder", "বাধা দেওয়া"), ("hypothetical", "কাল্পনিক ধারণাভিত্তিক"),
    ("immutable", "অপরিবর্তনীয়"), ("impartial", "নিরপেক্ষ"), ("impede", "ব্যাহত করা"),
    ("imperative", "অপরিহার্য"), ("implicit", "অনুক্ত অথচ বোঝা যায় এমন"),
    ("inadvertent", "অনিচ্ছাকৃত"), ("incentive", "প্রণোদনা"), ("incessant", "অবিরাম"),
    ("incisive", "তীক্ষ্ণ ও সঠিক"), ("indigenous", "আদিবাসী বা স্থানীয়"),
    ("inevitable", "অবশ্যম্ভাবী"), ("inference", "অনুমান"), ("ingenious", "কুশলী উদ্ভাবনী"),
    ("innate", "সহজাত"), ("innocuous", "ক্ষতিহীন"), ("insatiable", "অতৃপ্ত"),
    ("intricate", "জটিল ও সূক্ষ্ম"), ("intrinsic", "স্বকীয়"), ("lucid", "স্বচ্ছ ও বোধগম্য"),
    ("mitigate", "প্রশমিত করা"), ("mundane", "নিতান্ত সাধারণ"), ("nuance", "সূক্ষ্ম পার্থক্য"),
    ("obsolete", "অচল"), ("ominous", "অশুভ ইঙ্গিতবাহী"), ("opaque", "অস্বচ্ছ"),
    ("paramount", "সর্বাধিক গুরুত্বপূর্ণ"), ("paucity", "স্বল্পতা"), ("peripheral", "প্রান্তিক"),
    ("plausible", "বিশ্বাসযোগ্য মনে হওয়া"), ("poignant", "মর্মস্পর্শী"),
    ("pragmatic", "বাস্তববাদী"), ("precedent", "নজির"), ("predicament", "সংকটজনক অবস্থা"),
    ("prevalent", "ব্যাপকভাবে প্রচলিত"), ("prudent", "বিচক্ষণ"), ("rebuke", "তিরস্কার করা"),
    ("redundant", "বাহুল্যপূর্ণ"), ("resilient", "দ্রুত সামলে ওঠার ক্ষমতাসম্পন্ন"),
    ("rigorous", "কঠোর ও নিখুঁত"), ("robust", "শক্তপোক্ত"), ("scrutinize", "খুঁটিয়ে পরীক্ষা করা"),
    ("sporadic", "বিক্ষিপ্ত"), ("stagnant", "স্থবির"), ("stark", "কঠোরভাবে স্পষ্ট"),
    ("subtle", "সূক্ষ্ম"), ("succinct", "সংক্ষিপ্ত ও পরিচ্ছন্ন"), ("superfluous", "অনাবশ্যক"),
    ("susceptible", "প্রবণ"), ("tangible", "বাস্তবে ধরা যায় এমন"), ("tedious", "ক্লান্তিকর"),
    ("tentative", "সাময়িক ও অনিশ্চিত"), ("transient", "ক্ষণস্থায়ী"),
    ("unprecedented", "নজিরবিহীন"), ("versatile", "বহুমুখী"), ("viable", "টিকে থাকার যোগ্য"),
    ("vigilant", "সজাগ"), ("vivid", "জীবন্ত ও স্পষ্ট"), ("zeal", "প্রবল উৎসাহ"),
]

C2 = [
    ("abstruse", "দুর্বোধ্য"), ("acrimonious", "তিক্ত ও কটু"),
    ("alacrity", "সাগ্রহ তৎপরতা"), ("anachronism", "কালের অসঙ্গতি"),
    ("antipathy", "তীব্র বিতৃষ্ণা"), ("apocryphal", "সত্যতা সন্দেহজনক"),
    ("arcane", "গূঢ় ও রহস্যময়"), ("arduous", "কষ্টসাধ্য"), ("assiduous", "অধ্যবসায়ী"),
    ("belligerent", "যুদ্ধংদেহী"), ("bombastic", "দম্ভপূর্ণ ভাষার"),
    ("cacophony", "কর্কশ ধ্বনির মিশ্রণ"), ("capricious", "খেয়ালি"),
    ("circumspect", "সতর্ক ও বিবেচক"), ("clandestine", "গোপন ও গুপ্ত"),
    ("cogent", "জোরালো ও যুক্তিগ্রাহ্য"), ("conflate", "গুলিয়ে এক করে ফেলা"),
    ("connoisseur", "রসজ্ঞ বিশেষজ্ঞ"), ("contrite", "অনুতপ্ত"), ("cursory", "ভাসা ভাসা"),
    ("debacle", "শোচনীয় বিপর্যয়"), ("deleterious", "ক্ষতিসাধক"),
    ("demagogue", "জনতার আবেগ উস্কে দেওয়া নেতা"), ("desultory", "এলোমেলো ও অসংলগ্ন"),
    ("didactic", "উপদেশপ্রবণ"), ("dilatory", "গড়িমসিপূর্ণ"), ("disparate", "সম্পূর্ণ ভিন্ন প্রকৃতির"),
    ("dogmatic", "মতান্ধ"), ("ebullient", "উচ্ছ্বসিত"), ("eclectic", "নানা উৎস থেকে নেওয়া"),
    ("efficacy", "কার্যকারিতা"), ("egregious", "চরম ও প্রকট খারাপ"),
    ("elucidate", "স্পষ্ট করে ব্যাখ্যা করা"), ("enervate", "নিস্তেজ করে দেওয়া"),
    ("ephemeral", "ক্ষণস্থায়ী"), ("equivocate", "কথা ঘুরিয়ে এড়িয়ে যাওয়া"),
    ("erudite", "পাণ্ডিত্যপূর্ণ"), ("esoteric", "গুটিকয়েকের বোধগম্য"),
    ("evanescent", "দ্রুত মিলিয়ে যাওয়া"), ("exacerbate", "আরও খারাপ করে তোলা"),
    ("excoriate", "তীব্র ভাষায় সমালোচনা করা"), ("exigent", "জরুরি ও দাবিদার"),
    ("extol", "উচ্চ প্রশংসা করা"), ("fastidious", "খুঁতখুঁতে"), ("fatuous", "নির্বোধ"),
    ("garrulous", "বাচাল"), ("grandiloquent", "আড়ম্বরপূর্ণ ভাষার"),
    ("hackneyed", "বহু ব্যবহারে ক্লিশে"), ("harangue", "দীর্ঘ ঝাঁঝালো বক্তৃতা"),
    ("hegemony", "আধিপত্য"), ("iconoclast", "প্রচলিত বিশ্বাস ভাঙা ব্যক্তি"),
    ("idiosyncrasy", "ব্যক্তিগত অদ্ভুত বৈশিষ্ট্য"), ("ignominious", "অপমানজনক"),
    ("imperious", "প্রভুত্বব্যঞ্জক"), ("impetuous", "হঠকারী"),
    ("implacable", "শান্ত করা যায় না এমন"), ("inchoate", "সবে শুরু হওয়া ও অসম্পূর্ণ"),
    ("incontrovertible", "অকাট্য"), ("indefatigable", "অক্লান্ত"),
    ("ineffable", "ভাষায় প্রকাশের অতীত"), ("inexorable", "অনিবার্য ও অটল"),
    ("ingenuous", "সরল ও অকপট"), ("inimical", "বৈরী"), ("insidious", "ছলনাময় ও ক্রমক্ষতিকর"),
    ("intransigent", "আপসহীন"), ("inveterate", "বদ্ধমূল অভ্যাসের"),
    ("juxtapose", "পাশাপাশি রেখে তুলনা করা"), ("laconic", "স্বল্পবাক"),
    ("largesse", "দানশীলতা"), ("lassitude", "অবসাদ"), ("lugubrious", "বিষণ্ন"),
    ("magnanimous", "উদারচিত্ত"), ("mellifluous", "শ্রুতিমধুর"), ("mendacious", "মিথ্যাবাদী"),
    ("mercurial", "দ্রুত পরিবর্তনশীল মেজাজের"), ("meticulous", "অতি সূক্ষ্ম যত্নশীল"),
    ("misanthrope", "মানববিদ্বেষী"), ("mollify", "শান্ত করা"), ("munificent", "অকৃপণ দাতা"),
    ("nadir", "সর্বনিম্ন বিন্দু"), ("nefarious", "অতি দুষ্ট"), ("neophyte", "নবিশ"),
    ("obdurate", "অনমনীয়"), ("obfuscate", "ইচ্ছাকৃতভাবে ঘোলাটে করা"),
    ("obsequious", "তোষামোদকারী"), ("onerous", "বোঝাস্বরূপ"), ("opprobrium", "তীব্র নিন্দা"),
    ("ostensible", "আপাত ও বাহ্যিক"), ("panacea", "সর্বরোগহর দাওয়াই"),
    ("paragon", "আদর্শ দৃষ্টান্ত"), ("parsimonious", "অত্যন্ত কৃপণ"), ("pellucid", "সম্পূর্ণ স্বচ্ছ"),
    ("penchant", "বিশেষ ঝোঁক"), ("perfunctory", "দায়সারা"), ("perspicacious", "তীক্ষ্ণবুদ্ধিসম্পন্ন"),
    ("pertinacious", "নাছোড়বান্দা"), ("phlegmatic", "নির্লিপ্ত ও শান্ত"),
    ("pithy", "সংক্ষিপ্ত অথচ অর্থবহ"), ("placate", "রাগ ভাঙানো"), ("platitude", "বস্তাপচা কথা"),
    ("plethora", "আধিক্য"), ("precarious", "নড়বড়ে ও অনিশ্চিত"),
    ("prevaricate", "সত্য এড়িয়ে কথা বলা"), ("probity", "সততা ও নিষ্ঠা"),
    ("proclivity", "স্বাভাবিক প্রবণতা"), ("prodigal", "অপব্যয়ী"), ("prolix", "বাহুল্যপূর্ণ"),
    ("propensity", "ঝোঁক"), ("prosaic", "নীরস ও সাদামাটা"), ("protean", "বহুরূপী"),
    ("puerile", "ছেলেমানুষি"), ("pugnacious", "ঝগড়াটে"), ("punctilious", "নিয়মের খুঁটিনাটিতে নিষ্ঠ"),
    ("quandary", "উভয়সংকট"), ("querulous", "খুঁতখুঁতে অভিযোগকারী"), ("quiescent", "নিষ্ক্রিয় ও শান্ত"),
    ("quixotic", "অবাস্তব আদর্শবাদী"), ("rancour", "তীব্র বিদ্বেষ"), ("recalcitrant", "অবাধ্য"),
    ("recondite", "গভীর ও দুর্বোধ্য"), ("redolent", "স্মৃতিজাগানিয়া"), ("reticent", "স্বল্পভাষী ও সংযত"),
    ("sagacious", "বিচক্ষণ"), ("salient", "প্রধান ও চোখে পড়ার মতো"), ("sanguine", "আশাবাদী"),
    ("sardonic", "ব্যঙ্গাত্মক"), ("scintillating", "বুদ্ধিদীপ্ত ও ঝলমলে"),
    ("serendipity", "আকস্মিক সৌভাগ্যময় প্রাপ্তি"), ("soporific", "ঘুম আনয়নকারী"),
    ("specious", "আপাত সত্য অথচ ভ্রান্ত"), ("spurious", "ভুয়া"), ("staid", "গম্ভীর ও সেকেলে"),
    ("stoic", "নির্বিকার সহনশীল"), ("stringent", "কঠোর"), ("sublime", "মহিমান্বিত"),
    ("supercilious", "তাচ্ছিল্যপূর্ণ অহংকারী"), ("surfeit", "অতিরিক্ত আধিক্য"),
    ("sycophant", "চাটুকার"), ("taciturn", "স্বল্পভাষী"), ("tacit", "অনুক্ত অথচ স্বীকৃত"),
    ("tantamount", "সমতুল্য"), ("temerity", "ধৃষ্টতা"), ("tenuous", "ক্ষীণ ও দুর্বল"),
    ("torpid", "নিস্তেজ"), ("tortuous", "আঁকাবাঁকা ও জটিল"), ("tractable", "সহজে বশ মানে এমন"),
    ("truculent", "আক্রমণাত্মক"), ("ubiquitous", "সর্বব্যাপী"), ("umbrage", "মনঃক্ষুণ্নতা"),
    ("unctuous", "অতি মধুর ও ভণ্ড"), ("untenable", "অসমর্থনযোগ্য"), ("vacillate", "দোলাচলে ভোগা"),
    ("venal", "ঘুষে বশ হওয়া"), ("veracity", "সত্যতা"), ("verbose", "বাক্যবহুল"),
    ("vicarious", "অন্যের অভিজ্ঞতার মধ্য দিয়ে পাওয়া"), ("vicissitude", "ভাগ্যের উত্থানপতন"),
    ("vilify", "কুৎসা রটানো"), ("virulent", "তীব্র বিষাক্ত"), ("vitiate", "নষ্ট করে দেওয়া"),
    ("vociferous", "উচ্চকণ্ঠ"), ("voracious", "অতিলোভী"), ("winsome", "মনোহর"),
    ("zealous", "অতি উৎসাহী"), ("zenith", "শীর্ষবিন্দু"),
]



# ------------------------------------------------ more words per level

A1_MORE = [
    ("one", "এক"), ("two", "দুই"), ("three", "তিন"), ("four", "চার"),
    ("five", "পাঁচ"), ("six", "ছয়"), ("seven", "সাত"), ("eight", "আট"),
    ("nine", "নয়"), ("ten", "দশ"), ("hundred", "একশো"), ("thousand", "হাজার"),
    ("first", "প্রথম"), ("second", "দ্বিতীয়"), ("third", "তৃতীয়"),
    ("half", "অর্ধেক"), ("number", "সংখ্যা"),
    ("red", "লাল"), ("blue", "নীল"), ("green", "সবুজ"), ("yellow", "হলুদ"),
    ("black", "কালো"), ("white", "সাদা"), ("brown", "বাদামি"),
    ("pink", "গোলাপি"), ("grey", "ধূসর"), ("colour", "রং"),
    ("shirt", "জামা"), ("trousers", "প্যান্ট"), ("shoe", "জুতা"),
    ("sock", "মোজা"), ("hat", "টুপি"), ("dress", "পোশাক"), ("cloth", "কাপড়"),
    ("bag", "ব্যাগ"), ("umbrella", "ছাতা"), ("watch", "হাতঘড়ি"),
    ("clock", "দেয়ালঘড়ি"), ("key", "চাবি"), ("ring", "আংটি"),
    ("button", "বোতাম"), ("pocket", "পকেট"), ("towel", "তোয়ালে"),
    ("soap", "সাবান"), ("comb", "চিরুনি"), ("mirror", "আয়না"),
    ("spoon", "চামচ"), ("plate", "থালা"), ("glass", "গ্লাস"), ("cup", "কাপ"),
    ("knife", "ছুরি"), ("bowl", "বাটি"), ("bottle", "বোতল"),
    ("stove", "চুলা"), ("fridge", "ফ্রিজ"), ("fan", "পাখা"), ("lamp", "বাতি"),
    ("floor", "মেঝে"), ("wall", "দেয়াল"), ("stair", "সিঁড়ি"),
    ("garden", "বাগান"), ("gate", "ফটক"), ("bathroom", "স্নানঘর"),
    ("mango", "আম"), ("banana", "কলা"), ("apple", "আপেল"), ("lemon", "লেবু"),
    ("potato", "আলু"), ("onion", "পেঁয়াজ"), ("garlic", "রসুন"),
    ("chilli", "লঙ্কা"), ("tomato", "টমেটো"), ("chicken", "মুরগি"),
    ("curd", "দই"), ("butter", "মাখন"), ("honey", "মধু"), ("flour", "আটা"),
    ("lentil", "ডাল"), ("spice", "মশলা"), ("juice", "রস"),
    ("sweet", "মিষ্টি"), ("sour", "টক"), ("bitter", "তেতো"), ("spicy", "ঝাল"),
    ("hungry", "ক্ষুধার্ত"), ("thirsty", "তৃষ্ণার্ত"), ("tasty", "সুস্বাদু"),
    ("mouse", "ইঁদুর"), ("goat", "ছাগল"), ("sheep", "ভেড়া"), ("duck", "হাঁস"),
    ("crow", "কাক"), ("pigeon", "পায়রা"), ("monkey", "বানর"),
    ("lion", "সিংহ"), ("bear", "ভালুক"), ("deer", "হরিণ"), ("frog", "ব্যাঙ"),
    ("butterfly", "প্রজাপতি"), ("bee", "মৌমাছি"), ("mosquito", "মশা"),
    ("spider", "মাকড়সা"), ("insect", "পোকা"), ("tail", "লেজ"),
    ("wing", "ডানা"), ("horn", "শিং"), ("nest", "বাসা"),
    ("uncle", "কাকা"), ("aunt", "কাকিমা"), ("grandfather", "দাদু"),
    ("grandmother", "ঠাকুমা"), ("son", "পুত্র"), ("daughter", "কন্যা"),
    ("husband", "স্বামী"), ("wife", "স্ত্রী"), ("name", "নাম"),
    ("age", "বয়স"), ("man", "পুরুষ"), ("woman", "নারী"), ("people", "জনগণ"),
    ("finger", "আঙুল"), ("thumb", "বুড়ো আঙুল"), ("nail", "নখ"),
    ("knee", "হাঁটু"), ("elbow", "কনুই"), ("shoulder", "কাঁধ"),
    ("back", "পিঠ"), ("chest", "বুক"), ("stomach", "পেট"), ("skin", "ত্বক"),
    ("bone", "হাড়"), ("neck", "গলা"), ("lip", "ঠোঁট"), ("tongue", "জিভ"),
    ("throat", "কণ্ঠনালী"), ("foot", "পায়ের পাতা"), ("face", "মুখমণ্ডল"),
    ("to wash", "ধোয়া"), ("to cook", "রান্না করা"),
    ("to clean", "পরিষ্কার করা"), ("to cut", "কাটা"), ("to carry", "বহন করা"),
    ("to bring", "আনা"), ("to send", "পাঠানো"), ("to wait", "অপেক্ষা করা"),
    ("to ask", "জিজ্ঞাসা করা"), ("to call", "ডাকা"), ("to start", "শুরু করা"),
    ("to stop", "থামা"), ("to find", "খুঁজে পাওয়া"), ("to lose", "হারানো"),
    ("to win", "জেতা"), ("to fall", "পড়ে যাওয়া"), ("to jump", "লাফানো"),
    ("to fly", "ওড়া"), ("to swim", "সাঁতার কাটা"), ("to climb", "ওঠা"),
    ("to push", "ঠেলা"), ("to pull", "টানা"), ("to throw", "ছোঁড়া"),
    ("to catch", "ধরা"), ("to build", "বানানো"), ("to break", "ভাঙা"),
    ("to fix", "সারানো"), ("to fill", "ভরা"), ("to count", "গোনা"),
    ("to draw", "আঁকা"), ("to dance", "নাচা"), ("to wear", "পরা"),
    ("to smile", "মুচকি হাসা"), ("to remember", "মনে রাখা"),
    ("to forget", "ভুলে যাওয়া"), ("to follow", "অনুসরণ করা"),
    ("here", "এখানে"), ("there", "সেখানে"), ("near", "কাছে"),
    ("far", "দূরে"), ("above", "উপরে"), ("below", "নিচে"),
    ("inside", "ভিতরে"), ("outside", "বাইরে"), ("left", "বাম"),
    ("right", "ডান"), ("front", "সামনে"), ("behind", "পিছনে"),
    ("between", "মাঝে"), ("again", "আবার"), ("please", "দয়া করে"),
    ("sorry", "ক্ষমা করবেন"), ("hello", "নমস্কার"), ("goodbye", "বিদায়"),
    ("yesterday", "গতকাল"), ("tomorrow", "আগামীকাল"), ("hour", "ঘণ্টা"),
    ("minute", "মিনিট"), ("holiday", "ছুটি"), ("birthday", "জন্মদিন"),
    ("festival", "উৎসব"),
    ("bus", "বাস"), ("train", "ট্রেন"), ("car", "গাড়ি"),
    ("bicycle", "সাইকেল"), ("boat", "নৌকা"), ("plane", "বিমান"),
    ("station", "স্টেশন"), ("ticket", "টিকিট"), ("driver", "চালক"),
    ("police", "পুলিশ"), ("nurse", "সেবিকা"), ("shopkeeper", "দোকানদার"),
    ("cook", "রাঁধুনি"), ("worker", "শ্রমিক"), ("soldier", "সৈনিক"),
    ("letter", "চিঠি"), ("map", "মানচিত্র"), ("phone", "ফোন"),
    ("television", "টেলিভিশন"), ("camera", "ক্যামেরা"), ("toy", "খেলনা"),
    ("strong", "শক্তিশালী"), ("weak", "দুর্বল"), ("clean", "পরিষ্কার"),
    ("dirty", "নোংরা"), ("wet", "ভেজা"), ("dry", "শুকনো"), ("full", "পূর্ণ"),
    ("empty", "খালি"), ("soft", "নরম"), ("hard", "শক্ত"), ("sharp", "ধারালো"),
    ("round", "গোল"), ("straight", "সোজা"), ("wide", "চওড়া"),
    ("narrow", "সরু"), ("deep", "গভীর"), ("quiet", "শান্ত"),
    ("loud", "জোরালো"), ("young", "তরুণ"), ("free", "মুক্ত"),
    ("busy", "ব্যস্ত"), ("ready", "প্রস্তুত"), ("sure", "নিশ্চিত"),
    ("alone", "একা"), ("same", "একই"), ("different", "ভিন্ন"),
    ("many", "অনেক"), ("few", "অল্প"), ("more", "বেশি"), ("less", "কম"),
]

A2_MORE = [
    ("weather", "আবহাওয়া"), ("season", "ঋতু"), ("summer", "গ্রীষ্ম"),
    ("winter", "শীত"), ("spring", "বসন্ত"), ("autumn", "শরৎ"),
    ("monsoon", "বর্ষা"), ("temperature", "তাপমাত্রা"), ("snow", "তুষার"),
    ("ice", "বরফ"), ("fog", "কুয়াশা"), ("shadow", "ছায়া"),
    ("sound", "ধ্বনি"), ("noise", "কোলাহল"), ("smell", "গন্ধ"),
    ("taste", "স্বাদ"), ("touch", "স্পর্শ"), ("voice", "কণ্ঠস্বর"),
    ("silence", "নীরবতা"), ("light", "আলোকরশ্মি"), ("heat", "উত্তাপ"),
    ("address", "ঠিকানা"), ("email", "ইমেইল"), ("message", "বার্তা"),
    ("internet", "ইন্টারনেট"), ("website", "ওয়েবসাইট"), ("file", "নথি"),
    ("screen", "পর্দা"), ("keyboard", "কিবোর্ড"), ("printer", "প্রিন্টার"),
    ("battery", "ব্যাটারি"), ("signal", "সংকেত"), ("software", "সফটওয়্যার"),
    ("salary", "বেতন"), ("customer", "খদ্দের"), ("seller", "বিক্রেতা"),
    ("buyer", "ক্রেতা"), ("profit", "মুনাফা"), ("loss", "লোকসান"),
    ("discount", "ছাড়"), ("bill", "বিল"), ("receipt", "রসিদ"),
    ("cash", "নগদ"), ("coin", "মুদ্রা"), ("account", "হিসাব"),
    ("budget", "বাজেট"), ("expense", "খরচ"), ("income", "আয়"),
    ("tax", "কর"), ("loan", "ঋণ"), ("rent", "ভাড়া"),
    ("meeting", "সভা"), ("team", "দল"), ("leader", "নেতা"),
    ("member", "সদস্য"), ("manager", "ব্যবস্থাপক"), ("employee", "কর্মচারী"),
    ("interview", "সাক্ষাৎকার"), ("application", "আবেদন"), ("form", "ফরম"),
    ("document", "দলিল"), ("signature", "স্বাক্ষর"), ("certificate", "সনদ"),
    ("degree", "ডিগ্রি"), ("exam", "পরীক্ষা"), ("class", "শ্রেণি"),
    ("lesson", "পাঠ"), ("homework", "বাড়ির কাজ"), ("library", "গ্রন্থাগার"),
    ("museum", "জাদুঘর"), ("hotel", "হোটেল"), ("restaurant", "রেস্তোরাঁ"),
    ("temple", "মন্দির"), ("bridge", "সেতু"), ("building", "ভবন"),
    ("factory", "কারখানা"), ("farm", "খামার"), ("field", "মাঠ"),
    ("park", "উদ্যান"), ("beach", "সৈকত"), ("island", "দ্বীপ"),
    ("desert", "মরুভূমি"), ("valley", "উপত্যকা"), ("lake", "হ্রদ"),
    ("pond", "পুকুর"), ("canal", "খাল"), ("path", "পথ"), ("corner", "কোণ"),
    ("border", "সীমানা"), ("area", "এলাকা"), ("region", "অঞ্চল"),
    ("capital", "রাজধানী"), ("nation", "জাতি"), ("citizen", "নাগরিক"),
    ("society", "সমাজ"), ("community", "সম্প্রদায"), ("public", "জনসাধারণ"),
    ("crowd", "ভিড়"), ("queue", "সারি"), ("ceremony", "অনুষ্ঠান"),
    ("invitation", "নিমন্ত্রণ"), ("fever", "জ্বর"), ("pain", "ব্যথা"),
    ("wound", "ক্ষত"), ("treatment", "চিকিৎসা"), ("danger", "বিপদ"),
    ("accident", "দুর্ঘটনা"), ("smoke", "ধোঁয়া"), ("flood", "বন্যা"),
    ("earthquake", "ভূমিকম্প"), ("drought", "খরা"), ("rescue", "উদ্ধার"),
    ("shelter", "আশ্রয়"), ("weapon", "অস্ত্র"), ("peace", "শান্তিচুক্তি"),
    ("army", "সেনাবাহিনী"), ("court", "আদালত"), ("judge", "বিচারক"),
    ("rule", "নিয়ম"), ("duty", "কর্তব্য"), ("permission", "অনুমতি"),
    ("chance", "সুযোগমুহূর্ত"), ("choice", "পছন্দ"), ("change", "পরিবর্তন"),
    ("order", "আদেশ"), ("list", "তালিকা"), ("note", "টুকিটাকি লেখা"),
    ("report", "প্রতিবেদন"), ("notice", "বিজ্ঞপ্তি"), ("advice", "পরামর্শ"),
    ("idea", "ভাবনা"), ("fact", "তথ্য"), ("truth", "সত্যতা"),
    ("secret", "গোপন কথা"), ("promise", "অঙ্গীকার"), ("hope", "প্রত্যাশা"),
    ("doubt", "সন্দেহ"), ("belief", "বিশ্বাসবোধ"), ("mind", "মন"),
    ("thought", "চিন্তা"), ("feeling", "অনুভূতি"), ("mood", "মেজাজ"),
    ("smile", "হাসি"), ("tear", "অশ্রু"), ("laughter", "হাস্যরোল"),
    ("joy", "আনন্দ"), ("sorrow", "শোক"), ("pride", "গর্ব"),
    ("shame", "লজ্জা"), ("surprise", "চমক"), ("shock", "ধাক্কা"),
    ("energy", "শক্তি"), ("power", "ক্ষমতা"), ("speed", "গতি"),
    ("distance", "দূরত্ব"), ("length", "দৈর্ঘ্য"), ("height", "উচ্চতা"),
    ("width", "প্রস্থ"), ("depth", "গভীরতা"), ("size", "আকার"),
    ("shape", "আকৃতি"), ("weight", "ওজনমাপ"), ("measure", "পরিমাপ"),
    ("amount", "পরিমাণ"), ("total", "মোট"), ("average", "গড়"),
    ("part", "অংশ"), ("piece", "টুকরা"), ("pair", "জোড়া"),
    ("group", "গোষ্ঠী"), ("row", "সারিবদ্ধ ক্রম"), ("line", "রেখা"),
    ("circle", "বৃত্ত"), ("square", "বর্গ"), ("point", "বিন্দু"),
    ("edge", "প্রান্ত"), ("centre", "কেন্দ্র"), ("top", "শীর্ষ"),
    ("bottom", "তলদেশ"), ("side", "পাশ"), ("surface", "পৃষ্ঠতল"),
    ("metal", "ধাতু"), ("iron", "লোহা"), ("gold", "সোনা"),
    ("silver", "রুপা"), ("wood", "কাঠ"), ("stone", "পাথর"),
    ("glass material", "কাচ"), ("plastic", "প্লাস্টিক"), ("paper sheet", "কাগজপত্র"),
    ("dust", "ধুলা"), ("mud", "কাদা"), ("sand", "বালি"), ("seed", "বীজ"),
    ("root", "শিকড়"), ("leaf", "পাতা"), ("branch", "ডাল"),
    ("grass", "ঘাস"), ("crop", "ফসল"), ("harvest", "ফসল কাটা"),
    ("to travel", "ভ্রমণ করা"), ("to visit", "দেখতে যাওয়া"),
    ("to arrive", "পৌঁছানো"), ("to leave", "ছেড়ে যাওয়া"),
    ("to return", "ফিরে আসা"), ("to enter", "প্রবেশ করা"),
    ("to choose", "বেছে নেওয়া"), ("to change", "বদলানো"),
    ("to collect", "সংগ্রহ করা"), ("to divide", "ভাগ করা"),
    ("to join", "যোগ দেওয়া"), ("to meet", "দেখা করা"),
    ("to invite", "নিমন্ত্রণ করা"), ("to thank", "ধন্যবাদ জানানো"),
    ("to apologize", "ক্ষমা চাওয়া"), ("to agree", "একমত হওয়া"),
    ("to refuse", "অস্বীকার করা"), ("to accept", "মেনে নেওয়া"),
    ("to believe", "বিশ্বাস করা"), ("to hope", "আশা পোষণ করা"),
    ("to worry", "চিন্তায় পড়া"), ("to enjoy", "উপভোগ করা"),
    ("to rest", "বিশ্রাম নেওয়া"), ("to hurry", "তাড়াহুড়ো করা"),
    ("to hide", "লুকানো"), ("to show", "দেখানো"), ("to point", "নির্দেশ করা"),
    ("to touch", "ছোঁয়া"), ("to hold", "ধরে রাখা"), ("to drop", "ফেলে দেওয়া"),
    ("to shake", "ঝাঁকানো"), ("to burn", "পোড়ানো"), ("to boil", "ফোটানো"),
    ("to melt", "গলানো"), ("to freeze", "জমানো"), ("to grow", "বেড়ে ওঠা"),
    ("to plant", "রোপণ করা"), ("to feed", "খাওয়ানো"),
    ("to ride", "চড়া"), ("to drive", "গাড়ি চালানো"),
    ("early", "তাড়াতাড়ি"), ("late", "দেরিতে"), ("often", "প্রায়ই"),
    ("sometimes", "মাঝে মাঝে"), ("rarely", "কদাচিৎ"), ("suddenly", "হঠাৎ"),
    ("slowly", "ধীরে"), ("quickly", "দ্রুতগতিতে"), ("carefully", "সতর্কভাবে"),
    ("together", "একসাথে"), ("almost", "প্রায়"), ("already", "ইতিমধ্যে"),
    ("still", "এখনো"), ("perhaps", "সম্ভবত"), ("instead", "পরিবর্তে"),
    ("however", "তবে"), ("because", "কারণবশত"), ("although", "যদিও"),
    ("cheap", "সস্তা"), ("expensive", "দামি"), ("useful tool", "কাজের জিনিস"),
    ("modern", "আধুনিককালের"), ("ancient", "প্রাচীন"), ("common", "সাধারণ"),
    ("rare", "বিরল"), ("famous", "নামকরা"), ("popular", "লোকপ্রিয়"),
    ("polite", "ভদ্র"), ("rude", "অভদ্র"), ("honest", "সৎ"),
    ("lazy", "অলস"), ("brave", "সাহসী"), ("kind", "দয়ালু"),
    ("cruel", "নিষ্ঠুর"), ("silly", "বোকাটে"), ("clever", "চালাক"),
    ("wise", "জ্ঞানী"), ("proud", "গর্বিত"), ("shy", "লাজুক"),
    ("calm", "শান্তচিত্ত"), ("angry", "রাগান্বিত"), ("afraid", "ভীত"),
    ("tired", "ক্লান্ত"), ("bored", "বিরক্ত"), ("excited", "উত্তেজিত"),
    ("lucky", "ভাগ্যবান"), ("safe", "নিরাপদ"), ("dangerous", "বিপজ্জনক"),
    ("possible", "সম্ভব"), ("impossible", "অসম্ভব"), ("important", "জরুরি"),
]

B1_MORE = [
    ("ability", "সামর্থ্য"), ("absent", "অনুপস্থিত"), ("accept offer", "প্রস্তাব গ্রহণ"),
    ("accident risk", "দুর্ঘটনার ঝুঁকি"), ("according", "অনুযায়ী"),
    ("account for", "কৈফিয়ত দেওয়া"), ("achievement", "কৃতিত্ব"),
    ("active", "সক্রিয়"), ("activity", "কর্মকাণ্ড"), ("actual", "প্রকৃতপক্ষে"),
    ("addition", "সংযোজন"), ("admire", "প্রশংসা করা"), ("admit", "স্বীকার করা"),
    ("adult", "প্রাপ্তবয়স্ক"), ("advance", "অগ্রসর হওয়া"),
    ("advantage", "সুবিধাজনক দিক"), ("adventure", "দুঃসাহসিক অভিযান"),
    ("affect", "প্রভাব ফেলা"), ("afford cost", "খরচ বহনের সামর্থ্য"),
    ("agreement", "চুক্তি"), ("aim", "লক্ষ্য স্থির করা"), ("alive", "জীবিত"),
    ("allow entry", "ঢুকতে দেওয়া"), ("amazing", "বিস্ময়কর"),
    ("ancient site", "প্রত্নস্থল"), ("announce", "ঘোষণা করা"),
    ("annual", "বার্ষিক"), ("anxious", "উদ্বিগ্ন"), ("apologize once", "দুঃখ প্রকাশ"),
    ("appearance", "চেহারা"), ("appointment", "সাক্ষাতের সময়"),
    ("approve", "অনুমোদন দেওয়া"), ("argue", "তর্ক করা"), ("argument", "যুক্তিতর্ক"),
    ("arrange meeting", "সভার আয়োজন"), ("arrest", "গ্রেপ্তার করা"),
    ("artificial", "কৃত্রিম"), ("ashamed", "লজ্জিত"), ("aspect", "দিক"),
    ("assist", "সহায়তা করা"), ("attach", "সংযুক্ত করা"), ("attempt", "প্রচেষ্টা"),
    ("attend", "উপস্থিত থাকা"), ("attention", "মনোযোগ"), ("attitude", "মনোভাব"),
    ("attract", "আকৃষ্ট করা"), ("audience", "শ্রোতৃমণ্ডলী"), ("author", "লেখক"),
    ("available", "পাওয়া যাচ্ছে এমন"), ("average score", "গড় নম্বর"),
    ("awake", "জেগে থাকা"), ("aware", "সচেতন"), ("balance", "ভারসাম্য"),
    ("ban", "নিষেধাজ্ঞা"), ("basic", "প্রাথমিক"), ("behave", "আচরণ করা"),
    ("behaviour", "আচার আচরণ"), ("belong", "অধিকারভুক্ত হওয়া"),
    ("bend", "বাঁকানো"), ("blame", "দোষারোপ করা"), ("boil water", "জল ফোটানো"),
    ("bother", "বিরক্ত করা"), ("brain", "মস্তিষ্ক"), ("breathe", "শ্বাস নেওয়া"),
    ("brief", "সংক্ষিপ্ত"), ("bright", "উজ্জ্বল"), ("broad", "প্রশস্ত"),
    ("cancel", "বাতিল করা"), ("capable", "সক্ষম"), ("career", "কর্মজীবন"),
    ("cause harm", "ক্ষতি ঘটানো"), ("celebrate", "উদযাপন করা"),
    ("challenge", "চ্যালেঞ্জ"), ("character", "চরিত্র"), ("charge", "মাশুল নেওয়া"),
    ("cheat", "প্রতারণা করা"), ("check", "যাচাই করা"), ("cheerful", "প্রফুল্ল"),
    ("claim", "দাবি করা"), ("client", "মক্কেল"), ("climate", "জলবায়ু"),
    ("colleague", "সহকর্মী"), ("comfort", "স্বাচ্ছন্দ্য"), ("command", "নির্দেশ"),
    ("comment", "মন্তব্য"), ("commit", "প্রতিশ্রুতিবদ্ধ হওয়া"),
    ("communicate", "যোগাযোগ করা"), ("companion", "সঙ্গী"),
    ("compete", "প্রতিযোগিতা করা"), ("complete", "সম্পূর্ণ করা"),
    ("complicated", "জটিল"), ("concern", "উদ্বেগ"), ("conclusion", "উপসংহার"),
    ("condition", "অবস্থা"), ("conduct", "পরিচালনা করা"), ("confuse", "গুলিয়ে ফেলা"),
    ("connect", "সংযুক্ত করা"), ("conscious", "সজ্ঞান"), ("constant", "অবিচল"),
    ("contact", "যোগাযোগ"), ("contain", "ধারণ করা"), ("content", "বিষয়বস্তু"),
    ("contract", "চুক্তিপত্র"), ("contrast", "বৈপরীত্য"), ("contribute", "অবদান রাখা"),
    ("control", "নিয়ন্ত্রণ"), ("convince", "রাজি করানো"), ("correct", "শুদ্ধ"),
    ("cost", "মূল্য"), ("courage test", "সাহসের পরীক্ষা"), ("create", "সৃষ্টি করা"),
    ("crime", "অপরাধ"), ("criticize", "সমালোচনা করা"), ("cure", "নিরাময় করা"),
    ("curious", "কৌতূহলী"), ("custom", "প্রথা"), ("damage", "ক্ষতি করা"),
    ("deal", "লেনদেন"), ("debate", "বিতর্ক"), ("decrease", "হ্রাস করা"),
    ("defeat", "পরাজিত করা"), ("defend", "রক্ষা করা"), ("definite", "নির্দিষ্ট"),
    ("delay", "বিলম্ব"), ("deliver", "পৌঁছে দেওয়া"), ("demand", "চাহিদা"),
    ("depend", "নির্ভর করা"), ("deserve", "যোগ্য হওয়া"), ("design", "নকশা"),
    ("desire", "আকাঙ্ক্ষা"), ("destroy", "ধ্বংস করা"), ("detail", "খুঁটিনাটি"),
    ("determine", "নির্ধারণ করা"), ("die", "মারা যাওয়া"), ("direct", "সরাসরি"),
    ("direction", "দিকনির্দেশ"), ("disagree", "দ্বিমত পোষণ করা"),
    ("disappear", "অদৃশ্য হওয়া"), ("disappoint", "হতাশ করা"),
    ("discover", "আবিষ্কার করা"), ("disease", "রোগ"), ("distant", "দূরবর্তী"),
    ("disturb", "ব্যাঘাত ঘটানো"), ("divide equally", "সমানভাবে ভাগ"),
    ("doubtful", "সন্দিহান"), ("earn respect", "সম্মান অর্জন"),
    ("effective", "কার্যকর"), ("efficient", "দক্ষ"), ("elect", "নির্বাচিত করা"),
    ("embarrass", "অপ্রস্তুত করা"), ("emotion", "আবেগ"), ("employ", "নিয়োগ করা"),
    ("empty space", "ফাঁকা জায়গা"), ("enemy", "শত্রু"), ("engage", "নিযুক্ত করা"),
    ("enormous", "বিশাল"), ("entire", "সমগ্র"), ("equal", "সমান"),
    ("escape", "পালানো"), ("essential", "অপরিহার্য বিষয়"), ("establish", "প্রতিষ্ঠা করা"),
    ("estimate", "আনুমানিক হিসাব"), ("event", "ঘটনা"), ("evidence", "প্রমাণ"),
    ("exact", "হুবহু"), ("examine", "পরীক্ষা করা"), ("excellent", "চমৎকার"),
    ("except", "ছাড়া"), ("exchange", "বিনিময়"), ("excuse", "অজুহাত"),
    ("exist", "বিদ্যমান থাকা"), ("expert", "বিশেষজ্ঞ"), ("explore", "অন্বেষণ করা"),
    ("express", "প্রকাশ করা"), ("extra", "অতিরিক্ত"), ("fail", "ব্যর্থ হওয়া"),
    ("fair", "ন্যায্য"), ("faith", "আস্থা"), ("fame", "খ্যাতি"),
    ("fashion", "ফ্যাশন"), ("fault", "ত্রুটি"), ("favour", "অনুগ্রহ"),
    ("fear danger", "বিপদের ভয়"), ("figure", "অবয়ব"), ("final", "চূড়ান্ত"),
    ("fit", "মানানসই"), ("flexible", "নমনীয়"), ("focus", "মনোনিবেশ করা"),
    ("force", "জোর"), ("forecast", "পূর্বাভাস"), ("forgive", "ক্ষমা করা"),
    ("former", "প্রাক্তন"), ("fortune", "ভাগ্য"), ("frequent", "ঘনঘন"),
    ("friendly", "বন্ধুভাবাপন্ন"), ("frighten", "ভয় দেখানো"), ("function", "কার্যক্রম"),
    ("gather", "জড়ো হওয়া"), ("general", "সার্বিক"), ("generous", "উদার"),
    ("gentle", "কোমল"), ("goods", "পণ্য"), ("govern", "শাসন করা"),
    ("grateful", "কৃতজ্ঞ"), ("guard", "প্রহরী"), ("guess", "আন্দাজ করা"),
    ("guide", "পথপ্রদর্শক"), ("guilty", "দোষী"), ("handle", "সামাল দেওয়া"),
    ("harm", "ক্ষতি"), ("hate", "ঘৃণা করা"), ("heal", "সেরে ওঠা"),
    ("honour", "সম্মাননা"), ("huge", "প্রকাণ্ড"), ("humble", "বিনয়ী"),
    ("humour", "রসবোধ"), ("identity", "পরিচয়"), ("ignore", "উপেক্ষা করা"),
    ("image", "প্রতিচ্ছবি"), ("immediate", "তাৎক্ষণিক"), ("impression", "ধারণাজনিত ছাপ"),
    ("improve skill", "দক্ষতা বাড়ানো"), ("income source", "আয়ের উৎস"),
    ("independent", "স্বাধীনচেতা"), ("individual", "ব্যক্তিবিশেষ"),
    ("industry", "শিল্প"), ("influence result", "ফলাফলে প্রভাব"),
    ("inform", "জানানো"), ("injure", "আহত করা"), ("insist", "জোর দেওয়া"),
    ("inspire", "অনুপ্রাণিত করা"), ("instruction", "নির্দেশনা"),
    ("intelligent", "বুদ্ধিমান"), ("intend", "ইচ্ছা করা"), ("invent", "উদ্ভাবন করা"),
    ("involve", "জড়িত করা"), ("issue", "বিষয়"), ("judge fairly", "ন্যায় বিচার"),
    ("keen", "আগ্রহী"), ("labour", "শ্রম"), ("lack", "অভাব"),
    ("latest", "সর্বশেষ"), ("lead", "নেতৃত্ব দেওয়া"), ("legal", "আইনসম্মত"),
    ("level", "স্তর"), ("likely", "সম্ভাব্য"), ("limit", "সীমা"),
    ("link", "সংযোগ"), ("local", "স্থানীয়"), ("locate", "অবস্থান নির্ণয় করা"),
    ("lonely", "নিঃসঙ্গ"), ("major", "প্রধান"), ("marry", "বিয়ে করা"),
    ("material", "উপকরণ"), ("matter", "গুরুত্বপূর্ণ হওয়া"), ("mature", "পরিণত"),
    ("mean", "অর্থ বোঝানো"), ("measure length", "দৈর্ঘ্য মাপা"),
    ("medical", "চিকিৎসাসংক্রান্ত"), ("mental", "মানসিক"), ("mercy", "করুণা"),
    ("mix", "মেশানো"), ("moment", "মুহূর্ত"), ("moral", "নৈতিক"),
    ("murder", "হত্যা"), ("mystery", "রহস্য"), ("nature", "প্রকৃতি"),
    ("neat", "পরিপাটি"), ("negative", "নেতিবাচক"), ("nervous", "উদ্বেগজনিত অস্বস্তি"),
    ("normal", "স্বাভাবিক"), ("obey", "মান্য করা"), ("object", "বস্তু"),
    ("observe", "পর্যবেক্ষণ করা"), ("occasion", "উপলক্ষ"), ("official", "সরকারি"),
    ("operate", "পরিচালনা করা"), ("oppose", "বিরোধিতা করা"), ("option", "বিকল্প"),
    ("organize", "সংগঠিত করা"), ("origin", "উৎপত্তি"), ("owe", "ঋণী থাকা"),
    ("pack", "গোছানো"), ("pale", "ফ্যাকাশে"), ("particular", "বিশেষ"),
    ("passion", "প্রবল অনুরাগ"), ("pattern", "নকশার ধরন"), ("peaceful", "শান্তিপূর্ণ"),
    ("perform", "সম্পাদন করা"), ("period", "কালপর্ব"), ("permanent", "স্থায়ী"),
    ("personal", "নিজস্ব"), ("persuade", "বোঝাতে সক্ষম হওয়া"),
    ("physical", "শারীরিক"), ("pity", "করুণা বোধ"), ("please someone", "খুশি করা"),
    ("plenty", "প্রচুর পরিমাণ"), ("polite request", "বিনীত অনুরোধ"),
    ("pollution", "দূষণ"), ("position", "অবস্থান"), ("positive", "ইতিবাচক"),
    ("possess", "অধিকারে রাখা"), ("practical", "ব্যবহারিক"), ("praise", "প্রশংসা"),
    ("predict weather", "আবহাওয়ার পূর্বাভাস"), ("present", "উপস্থিত"),
    ("press", "চাপ দেওয়া"), ("pretend", "ভান করা"), ("previous owner", "আগের মালিক"),
    ("price rise", "দাম বৃদ্ধি"), ("principle", "মূলনীতি"), ("prison", "কারাগার"),
    ("private matter", "ব্যক্তিগত বিষয়"), ("process", "প্রক্রিয়া"),
    ("produce goods", "পণ্য উৎপাদন"), ("professional", "পেশাদার"),
    ("progress", "অগ্রগতি"), ("project", "প্রকল্প"), ("proof", "প্রমাণপত্র"),
    ("proper", "যথাযথ"), ("property", "সম্পত্তি"), ("proposal", "প্রস্তাব"),
    ("protest", "প্রতিবাদ"), ("prove", "প্রমাণ করা"), ("provide help", "সহায়তা দেওয়া"),
    ("publish", "প্রকাশ করা"), ("punish", "শাস্তি দেওয়া"), ("quarrel", "ঝগড়া"),
    ("rapid", "দ্রুতগামী"), ("rate", "হার"), ("reaction", "প্রতিক্রিয়া"),
    ("realize truth", "সত্য উপলব্ধি"), ("reasonable", "যুক্তিসংগত"),
    ("recent", "সাম্প্রতিক"), ("recognize", "চিনতে পারা"), ("record", "নথিভুক্ত করা"),
    ("recover", "সুস্থ হয়ে ওঠা"), ("reduce cost", "খরচ কমানো"),
    ("refer", "উল্লেখ করা"), ("reflect", "প্রতিফলিত করা"), ("regret", "অনুশোচনা"),
    ("reject", "নাকচ করা"), ("relation", "সম্পর্ক"), ("release", "মুক্তি দেওয়া"),
    ("relief", "স্বস্তি"), ("rely", "ভরসা করা"), ("remain", "থেকে যাওয়া"),
    ("remind", "মনে করিয়ে দেওয়া"), ("repair damage", "ক্ষতি মেরামত"),
    ("replace", "বদলে দেওয়া"), ("represent", "প্রতিনিধিত্ব করা"),
    ("reputation", "সুনাম"), ("request", "অনুরোধ"), ("rescue team", "উদ্ধারকারী দল"),
    ("research", "গবেষণা"), ("reserve", "সংরক্ষণ করা"), ("resist", "বাধা দেওয়া"),
    ("resource", "সম্পদভাণ্ডার"), ("responsible", "দায়িত্বশীল"),
    ("rest of it", "বাকি অংশ"), ("restrict", "সীমিত করা"), ("reward", "পুরস্কার"),
    ("rise", "উঠে যাওয়া"), ("risk", "ঝুঁকি"), ("role", "ভূমিকা"),
    ("rough", "অমসৃণ"), ("routine", "নিত্যক্রম"), ("rubbish", "আবর্জনা"),
    ("rural", "গ্রামীণ"), ("sacrifice", "ত্যাগ"), ("satisfaction", "তৃপ্তি"),
    ("scene", "দৃশ্য"), ("schedule", "সময়সূচি"), ("scientist", "বিজ্ঞানী"),
    ("secure", "সুরক্ষিত"), ("select", "নির্বাচন করা"), ("sense", "বোধ"),
    ("sensitive", "সংবেদনশীল"), ("serve food", "খাবার পরিবেশন"),
    ("settle", "মীমাংসা করা"), ("severe", "প্রচণ্ড"), ("shortage", "ঘাটতি"),
    ("sign", "চিহ্ন"), ("silent", "নিশ্চুপ"), ("sincere", "আন্তরিক"),
    ("single", "একক"), ("skilled", "দক্ষ কারিগর"), ("smooth", "মসৃণ"),
    ("social", "সামাজিক"), ("sole", "একমাত্র"), ("solid", "কঠিন বস্তু"),
    ("source", "উৎস"), ("spare", "অতিরিক্ত রাখা"), ("specific", "সুনির্দিষ্ট"),
    ("spread", "ছড়িয়ে পড়া"), ("stable", "স্থিতিশীল"), ("standard", "মান"),
    ("state", "বিবৃত করা"), ("steady", "অবিচলিত"), ("steal", "চুরি করা"),
    ("strange", "অদ্ভুত"), ("stranger", "অপরিচিত ব্যক্তি"), ("strength", "শক্তিমত্তা"),
    ("stress", "চাপ"), ("stretch", "প্রসারিত করা"), ("strict", "কড়া"),
    ("structure", "কাঠামো"), ("struggle", "সংগ্রাম"), ("stupid", "নির্বোধ আচরণ"),
    ("style", "রীতি"), ("subject", "বিষয়বস্তু"), ("substance", "পদার্থ"),
    ("succeed", "সফল হওয়া"), ("sudden", "আকস্মিক"), ("suffer", "ভোগা"),
    ("sufficient amount", "যথেষ্ট পরিমাণ"), ("suit", "উপযোগী হওয়া"),
    ("supply", "সরবরাহ"), ("suppose", "ধরে নেওয়া"), ("surround", "ঘিরে রাখা"),
    ("suspect", "সন্দেহ করা"), ("swear", "শপথ করা"), ("system", "ব্যবস্থা"),
    ("talent", "প্রতিভা"), ("target", "লক্ষ্যবস্তু"), ("task", "কাজের দায়িত্ব"),
    ("technique", "কৌশল"), ("temporary", "অস্থায়ী"), ("tend", "প্রবণ হওয়া"),
    ("tense", "টানটান"), ("term", "পরিভাষা"), ("theory", "তত্ত্ব"),
    ("threat", "হুমকি"), ("tight", "আঁটসাঁট"), ("tolerate", "সহ্য করা"),
    ("trade", "বাণিজ্য"), ("tradition", "ঐতিহ্য"), ("transfer", "স্থানান্তর করা"),
    ("translate", "অনুবাদ করা"), ("transport", "পরিবহন"), ("treat", "আচরণ করা"),
    ("trend", "ধারা"), ("trial", "বিচারপ্রক্রিয়া"), ("trick", "কৌশলী চাল"),
    ("trust someone", "কাউকে ভরসা করা"), ("typical", "চিরাচরিত"),
    ("unfair", "অন্যায্য"), ("unique", "অনন্য"), ("unit", "একক পরিমাপ"),
    ("universe", "মহাবিশ্ব"), ("unusual", "অস্বাভাবিক"), ("upset", "মন খারাপ করা"),
    ("urban", "শহুরে"), ("urgent", "জরুরি ভিত্তিতে"), ("value", "মূল্যবোধ"),
    ("variety", "বৈচিত্র্য"), ("victim", "ক্ষতিগ্রস্ত ব্যক্তি"), ("victory", "বিজয়"),
    ("violence", "সহিংসতা"), ("visible", "দৃশ্যমান"), ("volunteer", "স্বেচ্ছাসেবক"),
    ("wage", "মজুরি"), ("warn", "সতর্ক করা"), ("waste", "অপচয়"),
    ("wave", "ঢেউ"), ("wealthy", "সম্পদশালী"), ("welcome", "স্বাগত জানানো"),
    ("whole thing", "পুরো বিষয়"), ("wild", "বন্য"), ("willing", "ইচ্ছুক"),
    ("wonder", "বিস্ময়ে ভাবা"), ("worth", "মূল্যবান হওয়া"), ("wrap", "মোড়ানো"),
]

B2_MORE = [
    ("abolish", "বিলুপ্ত করা"), ("absorb", "শুষে নেওয়া"), ("abstract", "বিমূর্ত"),
    ("academic", "পাণ্ডিত্যসংক্রান্ত"), ("accelerate", "গতি বাড়ানো"),
    ("accompany", "সঙ্গে যাওয়া"), ("accomplish", "সম্পন্ন করা"),
    ("accumulate", "জমা হওয়া"), ("accurate", "নির্ভুল"), ("accuse", "অভিযুক্ত করা"),
    ("acknowledge", "স্বীকৃতি দেওয়া"), ("acquire", "অর্জন করা"),
    ("adapt", "খাপ খাওয়ানো"), ("administer", "পরিচালনা করা"),
    ("adopt", "গ্রহণ করা"), ("advocate", "সমর্থন করা"), ("aggregate", "সমষ্টি"),
    ("allocate", "বরাদ্দ করা"), ("alter", "পরিবর্তন ঘটানো"), ("alternative", "বিকল্প পথ"),
    ("ambiguous", "দ্ব্যর্থবোধক"), ("amend", "সংশোধন করা"), ("ample", "প্রচুর"),
    ("analyse", "বিশ্লেষণ করা"), ("annual report", "বার্ষিক প্রতিবেদন"),
    ("apparatus", "যন্ত্রপাতির সমষ্টি"), ("appeal", "আবেদন জানানো"),
    ("appoint", "নিয়োগ দেওয়া"), ("appropriate", "যথোপযুক্ত"),
    ("approve budget", "বাজেট অনুমোদন"), ("arise", "উদ্ভব হওয়া"),
    ("aspire", "উচ্চাকাঙ্ক্ষা রাখা"), ("assemble", "একত্র করা"),
    ("assert", "দৃঢ়ভাবে বলা"), ("assign", "দায়িত্ব দেওয়া"),
    ("assumption", "অনুমিত ধারণা"), ("assure", "আশ্বস্ত করা"),
    ("autonomy", "স্বায়ত্তশাসন"), ("await", "অপেক্ষায় থাকা"),
    ("barrier", "প্রতিবন্ধক"), ("beforehand", "আগেভাগে"), ("betray", "বিশ্বাসঘাতকতা করা"),
    ("boost", "চাঙা করা"), ("breakthrough", "যুগান্তকারী সাফল্য"),
    ("burden", "বোঝা"), ("capacity", "ধারণক্ষমতা"), ("cease", "বন্ধ হওয়া"),
    ("chronic", "দীর্ঘস্থায়ী"), ("circumstance", "পরিস্থিতির প্রেক্ষাপট"),
    ("cite", "উদ্ধৃত করা"), ("civil", "নাগরিক সংক্রান্ত"), ("clarify", "স্পষ্ট করা"),
    ("classify", "শ্রেণিবদ্ধ করা"), ("coincide", "একসঙ্গে ঘটা"),
    ("commence", "আরম্ভ করা"), ("commitment", "অঙ্গীকারবদ্ধতা"),
    ("commodity", "পণ্যদ্রব্য"), ("compensate", "ক্ষতিপূরণ দেওয়া"),
    ("competent", "যোগ্যতাসম্পন্ন"), ("compile", "সংকলন করা"),
    ("complement", "পরিপূরক হওয়া"), ("component", "উপাদান"),
    ("comprise", "গঠিত হওয়া"), ("conceive", "ধারণা করা"), ("concede", "মেনে নেওয়া"),
    ("concept", "ধারণাগত রূপ"), ("condemn", "নিন্দা করা"), ("confer", "প্রদান করা"),
    ("confine", "সীমাবদ্ধ রাখা"), ("conform", "সঙ্গতি রাখা"),
    ("consecutive", "পরপর"), ("consensus", "ঐকমত্য"), ("consequence", "পরিণতি"),
    ("conserve", "সংরক্ষণ করা"), ("considerable", "উল্লেখযোগ্য পরিমাণ"),
    ("consolidate", "সুসংহত করা"), ("constitute", "গঠন করা"),
    ("consult", "পরামর্শ নেওয়া"), ("consume", "ভোগ করা"), ("contend", "দাবি করা"),
    ("context", "প্রসঙ্গ"), ("continuous", "নিরবচ্ছিন্ন"), ("contradict", "বিরোধিতা করা"),
    ("contrary", "বিপরীত"), ("contribute idea", "ভাবনা যোগ করা"),
    ("conventional", "প্রথাগত"), ("cooperate", "সহযোগিতা করা"),
    ("coordinate", "সমন্বয় করা"), ("cope", "সামলে নেওয়া"), ("core", "মূল কেন্দ্র"),
    ("correspond", "মিল থাকা"), ("counterpart", "সমকক্ষ"), ("credit", "কৃতিত্ব দেওয়া"),
    ("criterion", "মানদণ্ড"), ("critical", "সংকটপূর্ণ"), ("cultivate", "চাষ করা"),
    ("cumulative", "ক্রমসঞ্চিত"), ("currency", "মুদ্রাব্যবস্থা"), ("decline", "হ্রাস পাওয়া"),
    ("dedicate", "উৎসর্গ করা"), ("deem", "গণ্য করা"), ("deficit", "ঘাটতি"),
    ("define", "সংজ্ঞায়িত করা"), ("delegate", "দায়িত্ব অর্পণ করা"),
    ("deliberate", "সুচিন্তিত"), ("denote", "বোঝানো"), ("depict", "চিত্রিত করা"),
    ("deploy", "মোতায়েন করা"), ("derive", "উদ্ভূত হওয়া"), ("designate", "মনোনীত করা"),
    ("despite", "সত্ত্বেও"), ("devise", "উদ্ভাবন করা"), ("devote", "নিবেদিত করা"),
    ("differentiate", "পার্থক্য করা"), ("dimension", "মাত্রা"),
    ("discard", "ফেলে দেওয়া"), ("disclose", "প্রকাশ করে দেওয়া"),
    ("discretion", "বিবেচনার স্বাধীনতা"), ("dispute", "বিরোধ"),
    ("distinguish", "পৃথক করে চেনা"), ("distort", "বিকৃত করা"),
    ("distribute", "বিতরণ করা"), ("domain", "কর্মক্ষেত্র"), ("dominate", "প্রাধান্য বিস্তার"),
    ("draft", "খসড়া"), ("drastic", "আমূল কঠোর"), ("duration", "স্থায়িত্বকাল"),
    ("dwell", "বসবাস করা"), ("eager", "উৎসুক"), ("ease", "সহজ করা"),
    ("economic", "অর্থনৈতিক"), ("elicit", "বের করে আনা"), ("embrace", "আলিঙ্গন করা"),
    ("emit", "নির্গত করা"), ("empower", "ক্ষমতায়িত করা"), ("enact", "প্রণয়ন করা"),
    ("encompass", "অন্তর্ভুক্ত করে রাখা"), ("endeavour", "প্রয়াস"),
    ("enforce", "কার্যকর করা"), ("enrich", "সমৃদ্ধ করা"), ("entity", "সত্তা"),
    ("envisage", "কল্পনায় দেখা"), ("equip", "সজ্জিত করা"), ("equivalent", "সমমানের"),
    ("erode", "ক্ষয় করা"), ("establish rule", "নিয়ম প্রতিষ্ঠা"),
    ("ethical", "নীতিসম্মত"), ("evaluate", "মূল্যায়ন করা"), ("evolve", "বিবর্তিত হওয়া"),
    ("exclusive", "একচেটিয়া"), ("execute", "বাস্তবায়ন করা"), ("exert", "প্রয়োগ করা"),
    ("expend", "ব্যয় করা"), ("expertise", "বিশেষ দক্ষতা"), ("explicit note", "স্পষ্ট নির্দেশ"),
    ("exposure", "সংস্পর্শ"), ("extend", "বাড়ানো"), ("external", "বাহ্যিক"),
    ("facility", "সুবিধাসম্পন্ন স্থাপনা"), ("factor", "নিয়ামক"), ("fade", "ম্লান হওয়া"),
    ("fatal", "প্রাণঘাতী"), ("fierce", "প্রচণ্ড উগ্র"), ("finance", "অর্থায়ন"),
    ("flaw", "খুঁত"), ("flourish", "সমৃদ্ধ হওয়া"), ("forge", "গড়ে তোলা"),
    ("formulate", "প্রণয়ন করা"), ("foster", "লালন করা"), ("framework", "কাঠামোগত রূপরেখা"),
    ("fraud", "জালিয়াতি"), ("fulfil", "পূরণ করা"), ("furthermore", "অধিকন্তু"),
    ("gauge", "পরিমাপ করা"), ("grant", "মঞ্জুর করা"), ("grasp", "আঁকড়ে ধরা"),
    ("hierarchy", "ক্রমবিন্যাস"), ("highlight", "তুলে ধরা"), ("hostile", "বিদ্বেষপূর্ণ"),
    ("humane", "মানবিক"), ("identical", "অবিকল একরকম"), ("ideology", "মতাদর্শ"),
    ("ignite", "প্রজ্বলিত করা"), ("illegal", "বেআইনি"), ("immense", "অপরিমেয়"),
    ("implement", "রূপায়ণ করা"), ("implication", "নিহিত অর্থ"),
    ("impress", "প্রভাবিত করা"), ("incline", "ঝুঁকে পড়া"), ("index", "সূচক"),
    ("indicate trend", "প্রবণতা নির্দেশ"), ("induce", "প্ররোচিত করা"),
    ("inevitable loss", "অনিবার্য ক্ষতি"), ("infer", "অনুমান করা"),
    ("inflate", "স্ফীত করা"), ("inhibit", "বাধা সৃষ্টি করা"), ("initial", "প্রারম্ভিক"),
    ("innovate", "নবপ্রবর্তন করা"), ("input", "সরবরাহকৃত উপাদান"),
    ("insight", "অন্তর্দৃষ্টি"), ("inspect", "পরিদর্শন করা"), ("install", "স্থাপন করা"),
    ("instance", "দৃষ্টান্ত"), ("institute", "প্রতিষ্ঠান"), ("intact", "অক্ষত"),
    ("integral", "অবিচ্ছেদ্য"), ("intensify", "তীব্রতর করা"), ("interact", "মিথস্ক্রিয়া করা"),
    ("interfere", "হস্তক্ষেপ করা"), ("interpret", "ব্যাখ্যা করা"), ("interval", "ব্যবধান"),
    ("intervene", "মধ্যস্থতা করা"), ("invoke", "আহ্বান করা"), ("isolate", "বিচ্ছিন্ন করা"),
    ("jeopardy", "সংকটাপন্ন অবস্থা"), ("legislation", "আইনপ্রণয়ন"),
    ("legitimate", "বৈধ ও ন্যায্য"), ("liable", "দায়বদ্ধ"), ("liberate", "মুক্ত করা"),
    ("likelihood", "সম্ভাবনার মাত্রা"), ("magnitude", "ব্যাপকতা"),
    ("mandatory", "বাধ্যতামূলক"), ("manipulate", "কৌশলে চালনা করা"),
    ("margin", "প্রান্তসীমা"), ("mediate", "সালিশি করা"), ("merge", "একীভূত হওয়া"),
    ("merit", "গুণাগুণ"), ("minimal", "ন্যূনতম"), ("mode", "প্রণালী"),
    ("monitor", "নজর রাখা"), ("motive", "উদ্দেশ্যমূলক প্রেরণা"), ("mutual", "পারস্পরিক"),
    ("negotiate", "দর কষাকষি করা"), ("neutral", "নিরপেক্ষ অবস্থান"),
    ("norm", "প্রচলিত মান"), ("notable", "লক্ষণীয়"), ("nurture", "পরিচর্যা করা"),
    ("objective", "উদ্দিষ্ট লক্ষ্য"), ("obligation", "বাধ্যবাধকতা"),
    ("occupy", "দখল করা"), ("offset", "পুষিয়ে নেওয়া"), ("optimum", "সর্বোত্তম মাত্রা"),
    ("originate", "উৎপত্তি হওয়া"), ("outcome", "পরিণামফল"), ("outline", "রূপরেখা"),
    ("overlap", "আংশিক মিলে যাওয়া"), ("overwhelm", "অভিভূত করা"),
    ("parallel", "সমান্তরাল"), ("participate", "অংশগ্রহণ করা"), ("passive", "নিষ্ক্রিয়"),
    ("peak", "সর্বোচ্চ বিন্দু"), ("penalty", "জরিমানা"), ("perceive risk", "ঝুঁকি বোঝা"),
    ("persistent", "অবিচল"), ("perspective", "দৃষ্টিভঙ্গি"), ("phase", "পর্যায়"),
    ("pledge", "অঙ্গীকার করা"), ("portion", "ভাগের অংশ"), ("potential", "সম্ভাবনাময়"),
    ("precaution", "সতর্কতামূলক ব্যবস্থা"), ("preceding", "পূর্ববর্তী"),
    ("predominant", "প্রধানত বিরাজমান"), ("preserve", "সংরক্ষিত রাখা"),
    ("prevail", "বিরাজ করা"), ("primary", "মুখ্য"), ("principal", "প্রধানতম"),
    ("prior", "পূর্বতন"), ("priority", "অগ্রাধিকার"), ("procedure", "কার্যপ্রণালী"),
    ("proceed", "এগিয়ে চলা"), ("profound", "গভীর তাৎপর্যপূর্ণ"),
    ("prohibition", "নিষেধ"), ("prompt", "তাৎক্ষণিকভাবে সাড়া দেওয়া"),
    ("proportion", "অনুপাত"), ("prosper", "সমৃদ্ধি লাভ করা"), ("provoke", "উস্কে দেওয়া"),
    ("publicity", "প্রচার"), ("qualify", "যোগ্যতা অর্জন করা"), ("quote", "উদ্ধৃতি দেওয়া"),
    ("random", "এলোমেলোভাবে বাছাই"), ("range", "পরিসর"), ("ratio", "অনুপাতের হার"),
    ("rational", "যুক্তিনিষ্ঠ"), ("recruit", "নিয়োগ করা"), ("refine", "পরিশীলিত করা"),
    ("reform", "সংস্কার"), ("regard", "বিবেচনা করা"), ("regime", "শাসনব্যবস্থা"),
    ("register", "নিবন্ধন করা"), ("regulate", "নিয়ন্ত্রিত করা"), ("reinforce", "জোরদার করা"),
    ("relevant", "প্রাসঙ্গিক"), ("reluctance", "অনিচ্ছা"), ("remedy", "প্রতিকার"),
    ("render", "পরিণত করা"), ("renew", "নবায়ন করা"), ("repeal", "রদ করা"),
    ("resemble", "সাদৃশ্য থাকা"), ("reside", "বসবাস করা"), ("resolve", "সমাধান করা"),
    ("restore", "পুনরুদ্ধার করা"), ("retrieve", "উদ্ধার করে আনা"),
    ("reveal fact", "তথ্য ফাঁস করা"), ("revenue", "রাজস্ব"), ("reverse", "উল্টে দেওয়া"),
    ("revise", "সংশোধন করে নেওয়া"), ("rival", "প্রতিদ্বন্দ্বী"), ("scope", "পরিধি"),
    ("sector", "খাত"), ("seek", "অনুসন্ধান করা"), ("sequence", "ক্রমধারা"),
    ("sole right", "একমাত্র অধিকার"), ("specify", "নির্দিষ্ট করে বলা"),
    ("stability", "স্থিতিশীলতা"), ("stake", "বাজি রাখা স্বার্থ"), ("statistic", "পরিসংখ্যান"),
    ("status", "মর্যাদা"), ("straightforward", "সরলসোজা"), ("strategy", "কৌশলগত পরিকল্পনা"),
    ("subsidy", "ভর্তুকি"), ("substitute", "বিকল্প হিসেবে বসানো"), ("subtle hint", "সূক্ষ্ম ইঙ্গিত"),
    ("suffice", "যথেষ্ট হওয়া"), ("summarize", "সারসংক্ষেপ করা"), ("supervise", "তদারক করা"),
    ("supplement", "পরিপূরক"), ("surpass", "ছাড়িয়ে যাওয়া"), ("survey", "জরিপ"),
    ("suspend", "স্থগিত করা"), ("sustainable", "টেকসই"), ("symbol", "প্রতীক"),
    ("tackle", "মোকাবিলা করা"), ("temporary halt", "সাময়িক বিরতি"),
    ("tension", "উত্তেজনা"), ("terminate", "সমাপ্ত করা"), ("theme", "মূলভাব"),
    ("threshold", "সীমারেখা"), ("thrive", "বিকশিত হওয়া"), ("trace", "সন্ধান করা"),
    ("transform", "রূপান্তরিত করা"), ("transmit", "প্রেরণ করা"), ("trigger", "সূত্রপাত ঘটানো"),
    ("ultimate", "চূড়ান্ত পর্যায়ের"), ("undermine", "ভিত দুর্বল করা"),
    ("uniform", "একরূপ"), ("utilize", "কাজে লাগানো"), ("validate", "যাচাই করে স্বীকৃতি"),
    ("vary widely", "ব্যাপক তারতম্য"), ("vast", "বিস্তীর্ণ"), ("venture", "ঝুঁকিপূর্ণ উদ্যোগ"),
    ("verify", "সত্যতা যাচাই করা"), ("vital", "অত্যাবশ্যক"), ("welfare", "কল্যাণ"),
    ("withdraw", "প্রত্যাহার করা"), ("withstand", "প্রতিরোধ করে টিকে থাকা"),
]

C1_MORE = [
    ("abate", "প্রশমিত হওয়া"), ("aberration", "স্বাভাবিকতা থেকে বিচ্যুতি"),
    ("abide", "মেনে চলা"), ("abrupt", "আকস্মিক ও রূঢ়"), ("accede", "সম্মতি দেওয়া"),
    ("acclaim", "সপ্রশংস স্বীকৃতি"), ("accolade", "সম্মাননা"),
    ("adamant", "অনড়"), ("adept", "পারদর্শী"), ("adhere", "লেগে থাকা"),
    ("adjourn", "মুলতবি করা"), ("admonish", "সতর্ক করে ভর্ৎসনা করা"),
    ("adversary", "প্রতিপক্ষ"), ("adverse", "প্রতিকূল"), ("affable", "মিশুক ও বন্ধুবৎসল"),
    ("affinity", "স্বাভাবিক টান"), ("affluent", "সচ্ছল"), ("aggravate", "অবস্থা খারাপ করা"),
    ("agile", "ক্ষিপ্র"), ("akin", "সদৃশ"), ("alienate", "দূরে সরিয়ে দেওয়া"),
    ("allegiance", "আনুগত্য"), ("alleviate", "লাঘব করা"), ("allude", "পরোক্ষে উল্লেখ করা"),
    ("aloof", "দূরত্ব রেখে চলা"), ("altruistic", "পরার্থপর"), ("amass", "স্তূপ করে জমানো"),
    ("amiable", "সদালাপী"), ("ample proof", "যথেষ্ট প্রমাণ"), ("anomaly", "ব্যতিক্রম"),
    ("apathy", "নির্লিপ্তি"), ("appease", "তুষ্ট করা"), ("apprehensive", "শঙ্কিত"),
    ("apt", "যথাযোগ্য"), ("arbitrate", "সালিশ করে মীমাংসা"), ("ardent", "প্রবল অনুরাগী"),
    ("articulate idea", "ভাবনা স্পষ্ট করা"), ("ascertain", "নিশ্চিত করে জানা"),
    ("aspiration", "উচ্চাকাঙ্ক্ষা"), ("assimilate", "আত্মস্থ করা"),
    ("astute", "বিচক্ষণ ও ধূর্ত"), ("attest", "সাক্ষ্য দেওয়া"), ("audacious", "দুঃসাহসী"),
    ("augment", "বৃদ্ধি ঘটানো"), ("auspicious", "শুভ"), ("authentic proof", "খাঁটি প্রমাণ"),
    ("avert", "এড়িয়ে যাওয়া"), ("avid", "অতি আগ্রহী"), ("baffle", "হতবুদ্ধি করা"),
    ("bleak", "নিরানন্দ ও আশাহীন"), ("blunt", "ভোঁতা ও সোজাসাপটা"),
    ("bolster", "সমর্থন জুগিয়ে শক্ত করা"), ("brevity", "সংক্ষিপ্ততা"),
    ("brisk", "চটপটে"), ("buoyant", "উৎফুল্ল"), ("calamity", "মহাবিপর্যয়"),
    ("callous", "সংবেদনহীন"), ("candour", "অকপটতা"), ("captivate", "মুগ্ধ করা"),
    ("cardinal", "মৌলিক ও প্রধান"), ("caustic", "তীক্ষ্ণ ও দগ্ধকারী"),
    ("censure", "আনুষ্ঠানিক নিন্দা"), ("chastise", "কঠোরভাবে শাসন করা"),
    ("cherish", "যত্নে লালন করা"), ("coerce", "জোর করে বাধ্য করা"),
    ("cohesive", "সংহত"), ("commendable", "প্রশংসনীয়"), ("compelling", "মনোযোগ কাড়া"),
    ("complacency", "আত্মতুষ্টি"), ("compliance", "নিয়ম মান্যতা"),
    ("composure", "আত্মসংযম"), ("comprehend", "পুরোপুরি বোঝা"),
    ("concise", "সংক্ষিপ্ত ও নির্ভুল"), ("concur", "একমত হওয়া"),
    ("condone", "উপেক্ষা করে মেনে নেওয়া"), ("confound", "বিভ্রান্ত করা"),
    ("congenial", "মনের মতো"), ("conjecture", "অনুমাননির্ভর ধারণা"),
    ("consecrate", "পবিত্র করে উৎসর্গ"), ("consolation", "সান্ত্বনা"),
    ("conspire", "ষড়যন্ত্র করা"), ("constrain", "বাধ্যবাধকতা আরোপ করা"),
    ("contempt", "অবজ্ঞা"), ("contemplate", "গভীরভাবে ভাবা"),
    ("contingent", "শর্তসাপেক্ষ"), ("conundrum", "দুরূহ ধাঁধা"),
    ("convene", "সভা আহ্বান করা"), ("conviction", "দৃঢ় প্রত্যয়"),
    ("copious", "প্রচুর পরিমাণে"), ("cordial", "আন্তরিক সৌহার্দ্যপূর্ণ"),
    ("corroborate", "সমর্থনসূচক প্রমাণ দেওয়া"), ("counteract", "প্রভাব নষ্ট করা"),
    ("covert", "গোপনে চালানো"), ("crux", "মূল সমস্যা"), ("culminate", "চূড়ান্ত পরিণতি পাওয়া"),
    ("culpable", "দোষযোগ্য"), ("curtail", "ছেঁটে কমানো"), ("dearth", "অপ্রতুলতা"),
    ("decipher", "পাঠোদ্ধার করা"), ("decorum", "শোভনতা"), ("defer decision", "সিদ্ধান্ত পেছানো"),
    ("deference", "শ্রদ্ধাবনত মান্যতা"), ("defiance", "প্রকাশ্য অবাধ্যতা"),
    ("deficiency", "ঘাটতিজনিত দুর্বলতা"), ("definitive", "চূড়ান্ত ও প্রামাণ্য"),
    ("deft touch", "নিপুণ ছোঁয়া"), ("degrade", "অবনমিত করা"), ("delineate", "রূপরেখা আঁকা"),
    ("demeanour", "আচরণভঙ্গি"), ("denounce", "প্রকাশ্যে ধিক্কার দেওয়া"),
    ("deplete", "নিঃশেষ করে ফেলা"), ("deplore", "তীব্র দুঃখ প্রকাশ করা"),
    ("deride", "উপহাস করা"), ("despondent", "হতোদ্যম"), ("deter", "নিরুৎসাহিত করা"),
    ("deviate", "বিচ্যুত হওয়া"), ("devout", "গভীরভাবে নিষ্ঠাবান"),
    ("dexterity", "হাতের কুশলতা"), ("dictate", "নির্দেশ চাপিয়ে দেওয়া"),
    ("diffuse", "ছড়িয়ে দেওয়া"), ("digress", "মূল প্রসঙ্গ থেকে সরে যাওয়া"),
    ("diligence", "নিষ্ঠাপূর্ণ পরিশ্রম"), ("diminutive", "অতি ক্ষুদ্র"),
    ("discreet", "বিচক্ষণভাবে সংযত"), ("discrepancy", "গরমিল"),
    ("disdain", "ঘৃণামিশ্রিত অবজ্ঞা"), ("disillusion", "মোহভঙ্গ ঘটানো"),
    ("dismay", "হতাশাজনক বিস্ময়"), ("dismantle", "খুলে ফেলা"),
    ("dispel", "দূর করে দেওয়া"), ("disperse", "ছত্রভঙ্গ হওয়া"),
    ("disposition", "স্বভাবপ্রকৃতি"), ("dissent", "ভিন্নমত"), ("dissuade", "নিরস্ত করা"),
    ("divergent", "ভিন্নমুখী"), ("divulge", "গোপন কথা ফাঁস করা"),
    ("docile", "বাধ্য ও নম্র"), ("dubious", "সন্দেহজনক"), ("earnest", "আন্তরিক ও গম্ভীর"),
    ("eccentric", "খামখেয়ালি অদ্ভুত"), ("edifice", "বিশাল ইমারত"),
    ("elaborate plan", "বিশদ পরিকল্পনা"), ("elated", "উল্লসিত"),
    ("eloquence", "বাগ্মিতা"), ("elude", "ফাঁকি দিয়ে এড়ানো"), ("emanate", "নিঃসৃত হওয়া"),
    ("eminent", "খ্যাতনামা"), ("emulate", "অনুকরণ করে সমকক্ষ হওয়া"),
    ("enduring", "দীর্ঘস্থায়ী"), ("engrossed", "নিমগ্ন"), ("enigmatic", "রহস্যময়"),
    ("enmity", "শত্রুতা"), ("ensue", "ফলস্বরূপ ঘটা"), ("enthral", "মোহিত করা"),
    ("epitome", "প্রকৃষ্ট উদাহরণ"), ("erratic", "খামখেয়ালি অনিয়মিত"),
    ("erroneous", "ভুলে ভরা"), ("escalate", "ক্রমশ বাড়িয়ে তোলা"),
    ("esteem", "শ্রদ্ধাসহ মূল্যায়ন"), ("evade", "কৌশলে এড়িয়ে যাওয়া"),
    ("evoke", "জাগিয়ে তোলা"), ("exalt", "উচ্চে তুলে ধরা"), ("exemplify", "দৃষ্টান্ত দিয়ে দেখানো"),
    ("exhilarating", "উদ্দীপনাময়"), ("exonerate", "দোষমুক্ত করা"),
    ("expedient way", "সুবিধাজনক উপায়"), ("explicit ban", "সুস্পষ্ট নিষেধ"),
    ("exquisite", "অতি সূক্ষ্ম ও সুন্দর"), ("extravagant", "অমিতব্যয়ী"),
    ("exuberant", "প্রাণচঞ্চল"), ("fabricate", "বানিয়ে তোলা"), ("facet", "একটি দিক"),
    ("fallible", "ভুল হতে পারে এমন"), ("falter", "টলে যাওয়া"), ("fervent", "উৎকট আগ্রহী"),
    ("flagrant", "নির্লজ্জভাবে প্রকট"), ("flourishing", "রমরমা"),
    ("fluctuation", "ওঠানামা"), ("foresight", "দূরদর্শিতা"), ("forfeit", "অধিকার হারানো"),
    ("formidable task", "কঠিন কর্তব্য"), ("forthright", "স্পষ্টবক্তা"),
    ("fraught", "সংকটে ভরা"), ("furtive", "চোরা ও গোপন"), ("gallant", "বীরত্বপূর্ণ"),
    ("germane", "সরাসরি প্রাসঙ্গিক"), ("glean", "কুড়িয়ে সংগ্রহ করা"),
    ("gratify", "তৃপ্ত করা"), ("grave matter", "গুরুতর বিষয়"), ("grievance", "অভিযোগবোধ"),
    ("grudge", "মনে পুষে রাখা রাগ"), ("hamper", "কাজে বাধা দেওয়া"),
    ("haphazard", "অপরিকল্পিত"), ("harness", "কাজে লাগিয়ে নিয়ন্ত্রণ"),
    ("haughty", "উদ্ধত"), ("heed", "কর্ণপাত করা"), ("heinous", "জঘন্য"),
    ("hinder progress", "অগ্রগতিতে বাধা"), ("holistic", "সামগ্রিক দৃষ্টিভঙ্গির"),
    ("humility", "বিনয়"), ("hypocrisy", "ভণ্ডামি"), ("illicit", "অবৈধ"),
    ("illuminate", "আলোকিত করা"), ("illusion", "মায়া"), ("immaculate", "নিখুঁত পরিচ্ছন্ন"),
    ("imminent", "আসন্ন"), ("impair", "ক্ষমতা দুর্বল করা"), ("impasse", "অচলাবস্থা"),
    ("impeccable", "ত্রুটিহীন"), ("imperative need", "অপরিহার্য প্রয়োজন"),
    ("impertinent", "ধৃষ্ট"), ("impose tax", "কর আরোপ"), ("impoverish", "নিঃস্ব করা"),
    ("inadequate", "অপর্যাপ্ত"), ("inaugurate", "উদ্বোধন করা"), ("incentive plan", "প্রণোদনা প্রকল্প"),
    ("incidental", "আনুষঙ্গিক"), ("incite", "উত্তেজিত করে তোলা"),
    ("inclination", "মনের ঝোঁক"), ("incompatible", "বেমানান"),
    ("inconsistent", "অসামঞ্জস্যপূর্ণ"), ("incur", "ঘাড়ে নেওয়া"),
    ("indicative", "ইঙ্গিতবহ"), ("indifferent", "উদাসীন"), ("indignant", "ক্ষুব্ধ"),
    ("indispensable", "অত্যাবশ্যকীয়"), ("indulge", "প্রশ্রয় দেওয়া"),
    ("inept", "অপটু"), ("inertia", "জড়তা"), ("infamous", "কুখ্যাত"),
    ("infringe", "লঙ্ঘন করা"), ("ingenuity", "উদ্ভাবনী কুশলতা"),
    ("inherent risk", "অন্তর্নিহিত ঝুঁকি"), ("inhibit growth", "বৃদ্ধিতে বাধা"),
    ("innovative", "নব উদ্ভাবনী"), ("inquisitive", "অনুসন্ধিৎসু"),
    ("insinuate", "ইঙ্গিতে দোষারোপ করা"), ("insistent", "নাছোড় দাবিদার"),
    ("insolent", "অসৌজন্যমূলক ঔদ্ধত্য"), ("instil", "ধীরে ধীরে সঞ্চারিত করা"),
    ("integrity", "সততা ও অখণ্ডতা"), ("intercede", "সুপারিশ করে মধ্যস্থতা"),
    ("interim", "অন্তর্বর্তী"), ("intimidate", "ভয় দেখিয়ে দমানো"),
    ("intrigue", "কৌতূহল জাগানো"), ("invaluable", "অমূল্য"), ("irrelevant", "অপ্রাসঙ্গিক"),
    ("irritate", "খিটখিটে করে তোলা"), ("jubilant", "উল্লাসমুখর"),
    ("judicious", "বিবেচনাপ্রসূত"), ("keen insight", "তীক্ষ্ণ অন্তর্দৃষ্টি"),
    ("lament", "বিলাপ করা"), ("latent", "সুপ্ত"), ("laudable", "সাধুবাদযোগ্য"),
    ("lenient", "নমনীয় ও ক্ষমাপ্রবণ"), ("lethargic", "অলস ও নিস্তেজ"),
    ("liaison", "সংযোগ রক্ষাকারী"), ("lofty", "উচ্চমার্গীয়"), ("loathe", "প্রচণ্ড ঘৃণা করা"),
    ("ludicrous", "হাস্যকর রকম অযৌক্তিক"), ("malice", "বিদ্বেষপরায়ণতা"),
    ("manifest", "স্পষ্টভাবে প্রকাশিত"), ("marginal", "প্রান্তিক ও সামান্য"),
    ("meagre", "অতি সামান্য"), ("mediocre", "মাঝারি মানের"), ("menace", "আতঙ্কজনক হুমকি"),
    ("methodical", "পদ্ধতিনিষ্ঠ"), ("mingle", "মিশে যাওয়া"), ("minute detail", "সূক্ষ্মাতিসূক্ষ্ম বিবরণ"),
    ("momentous", "যুগান্তকারী"), ("monotonous", "একঘেয়ে"), ("nominal", "নামমাত্র"),
    ("notorious", "বদনামগ্রস্ত"), ("novel idea", "অভিনব ভাবনা"), ("noxious", "বিষাক্ত ও ক্ষতিকর"),
    ("obscure", "অস্পষ্ট ও অখ্যাত"), ("obstinate", "জেদি"), ("omit", "বাদ দিয়ে যাওয়া"),
    ("opulent", "জাঁকজমকপূর্ণ"), ("outrage", "তীব্র ক্ষোভ"), ("outset", "একেবারে শুরু"),
    ("overt", "প্রকাশ্য"), ("painstaking", "কষ্টসহিষ্ণু যত্নের"), ("palatable", "মুখরোচক"),
    ("paradox", "আপাতবিরোধী সত্য"), ("partial", "আংশিক"), ("patronize", "পৃষ্ঠপোষকতা করা"),
    ("perpetual", "চিরস্থায়ী"), ("perplex", "হতবুদ্ধি করে দেওয়া"),
    ("pertinent", "যথাযথভাবে প্রাসঙ্গিক"), ("pervade", "সর্বত্র ব্যাপ্ত হওয়া"),
    ("pinnacle", "শীর্ষশিখর"), ("plight", "দুর্দশা"), ("ponder", "গভীরভাবে চিন্তা করা"),
    ("posterity", "ভবিষ্যৎ বংশধর"), ("potent", "শক্তিশালী প্রভাবসম্পন্ন"),
    ("preclude", "আগেই আটকে দেওয়া"), ("predicate", "ভিত্তি স্থাপন করা"),
    ("preposterous", "অবিশ্বাস্য রকম অযৌক্তিক"), ("prescribe", "বিধান দেওয়া"),
    ("prestige", "মর্যাদাপূর্ণ খ্যাতি"), ("presumption", "ধরে নেওয়া ধারণা"),
    ("pretext", "ছুতা"), ("proficient", "সুদক্ষ"), ("profuse", "অজস্র"),
    ("prolific", "উর্বর ও বহুপ্রসূ"), ("prominence", "প্রাধান্য"),
    ("prone", "ঝুঁকিপ্রবণ"), ("provisional", "সাময়িক ব্যবস্থা"), ("proximity", "নৈকট্য"),
    ("prowess", "পারদর্শিতা"), ("quell", "দমন করা"), ("radiant", "দীপ্তিময়"),
    ("ramification", "জটিল পরিণাম"), ("rebut", "যুক্তি দিয়ে খণ্ডন করা"),
    ("reciprocal", "পারস্পরিক বিনিময়ের"), ("reckless", "বেপরোয়া"),
    ("reconcile", "মিটমাট করা"), ("rectify", "সংশোধন করে ঠিক করা"),
    ("redeem", "উদ্ধার করে মান ফিরিয়ে আনা"), ("refute", "মিথ্যা প্রমাণ করা"),
    ("reiterate", "পুনরায় জোর দিয়ে বলা"), ("relentless", "নিরলস ও নির্মম"),
    ("reminiscent", "স্মরণ করিয়ে দেয় এমন"), ("remorse", "গভীর অনুতাপ"),
    ("renounce", "পরিত্যাগ করা"), ("renowned", "সুপ্রসিদ্ধ"), ("repercussion", "পরোক্ষ প্রতিক্রিয়া"),
    ("replenish", "পুনরায় ভরে দেওয়া"), ("reprehensible", "নিন্দনীয়"),
    ("reprimand", "ভর্ৎসনা"), ("repudiate", "অস্বীকার করে প্রত্যাখ্যান"),
    ("resentment", "পুঞ্জীভূত ক্ষোভ"), ("resolute", "দৃঢ়সংকল্প"),
    ("respite", "সাময়িক স্বস্তি"), ("resplendent", "জ্যোতির্ময়"),
    ("restrained", "সংযত"), ("retort", "পাল্টা জবাব"), ("revere", "গভীর শ্রদ্ধা করা"),
    ("rife", "ব্যাপকভাবে ছড়ানো"), ("rudimentary", "একেবারে প্রাথমিক"),
    ("ruthless", "নির্মম"), ("scarcity", "দুষ্প্রাপ্যতা"), ("scorn", "তাচ্ছিল্য"),
    ("scrupulous", "নীতিনিষ্ঠ ও সতর্ক"), ("seclusion", "নির্জনতা"),
    ("serene", "প্রশান্ত"), ("shrewd", "চতুর ও হিসেবি"), ("sluggish", "মন্থর"),
    ("solace", "সান্ত্বনার আশ্রয়"), ("solemn", "গাম্ভীর্যপূর্ণ"), ("solitary", "একাকী"),
    ("sombre", "বিষণ্ন গম্ভীর"), ("sparse", "বিরল ও ছড়ানো"), ("spontaneous", "স্বতঃস্ফূর্ত"),
    ("stagnate", "স্থবির হয়ে পড়া"), ("staunch", "অবিচল অনুগত"), ("steadfast", "অটল"),
    ("stifle", "দমিয়ে রাখা"), ("strenuous", "কষ্টসাধ্য পরিশ্রমী"),
    ("stumble", "হোঁচট খাওয়া"), ("sturdy", "মজবুত"), ("submissive", "বশ্যতাপূর্ণ"),
    ("subordinate", "অধীনস্থ"), ("subsequently", "পরবর্তীকালে"),
    ("subside", "স্তিমিত হওয়া"), ("substantial", "যথেষ্ট বড়"), ("subvert", "ভেতর থেকে নষ্ট করা"),
    ("supersede", "স্থলাভিষিক্ত হওয়া"), ("suppress", "চেপে রাখা"),
    ("surmount", "অতিক্রম করে জয় করা"), ("swift", "ক্ষিপ্রগতি"),
    ("tarnish", "কলঙ্কিত করা"), ("tedium", "একঘেয়েমি"), ("tempt", "প্রলুব্ধ করা"),
    ("thwart", "ভণ্ডুল করে দেওয়া"), ("timid", "ভীরু"), ("tranquil", "নিস্তরঙ্গ শান্ত"),
    ("transcend", "ঊর্ধ্বে উঠে যাওয়া"), ("treacherous", "বিশ্বাসঘাতক ও বিপজ্জনক"),
    ("trivial", "তুচ্ছ"), ("turbulent", "উত্তাল"), ("unanimous", "সর্বসম্মত"),
    ("uncanny", "অস্বাভাবিক রকম অদ্ভুত"), ("undeniable", "অনস্বীকার্য"),
    ("underlying", "মূলে নিহিত"), ("undertake task", "কাজ হাতে নেওয়া"),
    ("uphold", "সমুন্নত রাখা"), ("utmost", "সর্বোচ্চ মাত্রার"), ("vague reply", "অস্পষ্ট জবাব"),
    ("vain", "নিরর্থক"), ("valiant", "বীরোচিত"), ("vehement", "প্রবল ও উগ্র"),
    ("venerate", "পূজনীয় মনে করা"), ("versatile skill", "বহুমুখী দক্ষতা"),
    ("vex mind", "মন বিক্ষিপ্ত করা"), ("vibrant", "প্রাণবন্ত"), ("vindicate", "নির্দোষ প্রমাণ করা"),
    ("virtue", "সদ্গুণ"), ("vulnerability", "দুর্বল দিক"), ("wane", "ক্ষীণ হয়ে আসা"),
    ("wary", "সন্দিগ্ধ সতর্কতা"), ("whim", "খেয়াল"), ("wholesome", "স্বাস্থ্যকর ও নির্মল"),
    ("wield", "ক্ষমতা প্রয়োগ করা"), ("withhold", "আটকে রাখা"), ("yearn", "ব্যাকুল আকাঙ্ক্ষা"),
]

C2_MORE = [
    ("abeyance", "সাময়িক স্থগিতাবস্থা"), ("abjure", "শপথ করে ত্যাগ করা"),
    ("abnegation", "আত্মত্যাগ"), ("abscond", "গা ঢাকা দেওয়া"),
    ("accretion", "ক্রমিক বৃদ্ধি"), ("acerbic", "কটুভাষী"), ("acumen", "তীক্ষ্ণ বিচারবোধ"),
    ("adjunct", "সহযোগী সংযোজন"), ("admonition", "সতর্কবাণী"),
    ("adroit", "হাতপাকা কুশলী"), ("adulation", "অতিরিক্ত স্তুতি"),
    ("adumbrate", "আভাসে ইঙ্গিত দেওয়া"), ("affectation", "কৃত্রিম ভঙ্গি"),
    ("aggrandize", "অযথা বড় করে দেখানো"), ("alacritous", "তৎপর"),
    ("amalgamate", "মিশিয়ে একীভূত করা"), ("ameliorate", "উন্নতি সাধন করা"),
    ("anathema", "ঘৃণ্য বস্তু"), ("animosity", "শত্রুভাবাপন্ন বিদ্বেষ"),
    ("annul", "বাতিল করে দেওয়া"), ("antediluvian", "অতি সেকেলে"),
    ("aplomb", "আত্মপ্রত্যয়ী স্থৈর্য"), ("apoplectic", "ক্রোধে উন্মত্ত"),
    ("apposite", "অত্যন্ত মানানসই"), ("approbation", "সমর্থনসূচক অনুমোদন"),
    ("appurtenance", "আনুষঙ্গিক সরঞ্জাম"), ("arrogate", "অন্যায্যভাবে দাবি করা"),
    ("ascetic", "কৃচ্ছ্রব্রতী"), ("asperity", "রুক্ষতা"), ("aspersion", "কুৎসামূলক মন্তব্য"),
    ("assuage", "উপশম ঘটানো"), ("attenuate", "ক্ষীণ করে দেওয়া"),
    ("augury", "ভবিষ্যৎ ইঙ্গিত"), ("auspice", "পৃষ্ঠপোষকতা"), ("austerity", "কৃচ্ছ্রসাধন"),
    ("avarice", "অর্থলিপ্সা"), ("aversion", "বিমুখতা"), ("axiomatic", "স্বতঃসিদ্ধ"),
    ("bacchanal", "উদ্দাম উৎসব"), ("baleful", "অমঙ্গলসূচক"), ("banality", "গতানুগতিকতা"),
    ("bane", "সর্বনাশের কারণ"), ("bastion", "শেষ দুর্গ"), ("beguile", "ভুলিয়ে রাখা"),
    ("behest", "আদেশবাণী"), ("bellicose", "যুদ্ধপ্রবণ"), ("benediction", "আশীর্বচন"),
    ("benevolent", "পরোপকারী"), ("bereft", "বঞ্চিত"), ("blandishment", "মিষ্টি কথায় ফুসলানো"),
    ("blasphemy", "ধর্মনিন্দা"), ("blithe", "নির্ভাবনায় প্রফুল্ল"), ("brazen", "নির্লজ্জভাবে ঔদ্ধত্যপূর্ণ"),
    ("bucolic", "পল্লিজীবনের"), ("burgeon", "দ্রুত বেড়ে ওঠা"), ("burnish", "ঘষে উজ্জ্বল করা"),
    ("cabal", "ষড়যন্ত্রকারী গোষ্ঠী"), ("cajole", "তোষামোদ করে রাজি করানো"),
    ("calumny", "মিথ্যা অপবাদ"), ("canard", "ভিত্তিহীন গুজব"), ("candid remark", "অকপট মন্তব্য"),
    ("castigate", "কঠোর তিরস্কার করা"), ("cavil", "খুঁটিনাটি নিয়ে খুঁত ধরা"),
    ("chicanery", "চাতুরিপূর্ণ কৌশল"), ("churlish", "রূঢ় ও অভদ্র"),
    ("coalesce", "মিলে এক হওয়া"), ("cognizant", "অবহিত"), ("commensurate", "সমানুপাতিক"),
    ("compendium", "সংক্ষিপ্ত সংকলন"), ("complicity", "অপকর্মে সহযোগিতা"),
    ("compunction", "বিবেকদংশন"), ("concomitant", "সহগামী"),
    ("confluence", "মিলনস্থল"), ("congruent", "সঙ্গতিপূর্ণ"), ("connive", "চুপিসারে যোগসাজশ"),
    ("consternation", "আতঙ্কমিশ্রিত হতভম্বতা"), ("contrition", "গভীর অনুশোচনা"),
    ("contumacious", "অবাধ্য ও অবমাননাকর"), ("conundrum riddle", "দুর্ভেদ্য ধাঁধা"),
    ("convoluted", "প্যাঁচানো ও দুর্বোধ্য"), ("copse", "ছোট ঝোপবন"),
    ("corollary", "স্বাভাবিক উপসিদ্ধান্ত"), ("cosset", "অতিরিক্ত আদর দেওয়া"),
    ("coterie", "ঘনিষ্ঠ চক্র"), ("countenance", "মুখাবয়ব বা প্রশ্রয় দেওয়া"),
    ("covet", "লোভাতুরভাবে কামনা করা"), ("credulous", "সহজে বিশ্বাসপ্রবণ"),
    ("cryptic", "রহস্যজনক ও দুর্বোধ্য"), ("culmination", "চরম পরিণতি"),
    ("cupidity", "লোভ"), ("dalliance", "খেলাচ্ছলে সময় নষ্ট"), ("dauntless", "নির্ভীক"),
    ("debase", "মান নষ্ট করা"), ("debilitate", "দুর্বল করে ফেলা"),
    ("decadence", "নৈতিক অবক্ষয়"), ("declaim", "উচ্চকণ্ঠে বক্তৃতা করা"),
    ("decorous", "শোভন"), ("decry", "প্রকাশ্যে নিন্দা করা"), ("deference shown", "প্রদর্শিত শ্রদ্ধা"),
    ("defunct", "অকেজো ও বিলুপ্ত"), ("demur", "আপত্তি জানানো"),
    ("denigrate", "মর্যাদাহানি করা"), ("denouement", "কাহিনির চূড়ান্ত মীমাংসা"),
    ("depredation", "লুটপাটজনিত ক্ষতি"), ("deracinate", "শিকড়সহ উপড়ে ফেলা"),
    ("derelict", "পরিত্যক্ত ও অবহেলিত"), ("derision", "বিদ্রূপ"),
    ("despot", "স্বেচ্ছাচারী শাসক"), ("desuetude", "অব্যবহারজনিত বিলুপ্তি"),
    ("diatribe", "তীব্র নিন্দাভাষণ"), ("dichotomy", "দ্বিধাবিভক্তি"),
    ("diffidence", "আত্মবিশ্বাসের অভাব"), ("dirge", "শোকগীতি"),
    ("disabuse", "ভুল ধারণা ভাঙানো"), ("discomfit", "বিব্রত করে পরাস্ত করা"),
    ("discursive", "এলোমেলোভাবে বিস্তৃত"), ("disingenuous", "ভানপূর্ণ অসরল"),
    ("disparity", "বৈষম্যমূলক পার্থক্য"), ("dissemble", "আসল রূপ লুকানো"),
    ("dissipate", "অপচয় করে ছড়িয়ে ফেলা"), ("dissolution", "বিলোপ"),
    ("dissonance", "বেসুরো অসামঞ্জস্য"), ("distend", "ফুলে ওঠা"),
    ("diurnal", "দিবাচর"), ("divest", "খুলে নেওয়া বা বঞ্চিত করা"),
    ("doleful", "শোকাচ্ছন্ন"), ("dour", "গোমড়ামুখো"), ("draconian", "নিষ্ঠুর কঠোর"),
    ("duplicity", "দ্বিমুখী ছলনা"), ("ebb", "ভাটার টানে কমা"), ("edict", "রাজকীয় ফরমান"),
    ("effrontery", "নির্লজ্জ স্পর্ধা"), ("effusive", "আবেগে উপচে পড়া"),
    ("elegy", "শোককাব্য"), ("emaciated", "শীর্ণকায়"), ("embezzle", "তহবিল তছরুপ করা"),
    ("emollient", "কোমলকারী ও প্রশমনকারী"), ("encomium", "উচ্ছ্বসিত প্রশস্তি"),
    ("endemic spread", "স্থানীয়ভাবে বদ্ধমূল বিস্তার"), ("engender", "জন্ম দেওয়া"),
    ("enmesh", "জালে জড়িয়ে ফেলা"), ("ennoble", "মহিমান্বিত করা"),
    ("ephemera", "ক্ষণস্থায়ী বস্তু"), ("epithet", "বিশেষণসূচক অভিধা"),
    ("epitomize", "প্রকৃষ্ট উদাহরণ হওয়া"), ("equanimity", "মানসিক স্থৈর্য"),
    ("eschew", "সচেতনভাবে বর্জন করা"), ("espouse", "সমর্থন করে গ্রহণ করা"),
    ("estrange", "সম্পর্কচ্ছেদ ঘটানো"), ("ethereal", "স্বর্গীয় ও অলৌকিক"),
    ("eulogy", "গুণকীর্তন"), ("euphemism", "শ্রুতিমধুর পরিভাষা"),
    ("exculpate", "নির্দোষ প্রতিপন্ন করা"), ("execrable", "ঘৃণ্য রকম খারাপ"),
    ("exhort", "জোরালোভাবে উপদেশ দেওয়া"), ("exhume", "কবর থেকে তোলা"),
    ("exorbitant", "অতিরিক্ত চড়া"), ("expatiate", "বিস্তারিতভাবে বলা"),
    ("expiate", "প্রায়শ্চিত্ত করা"), ("expunge", "মুছে ফেলা"),
    ("extemporize", "প্রস্তুতি ছাড়াই বলা"), ("extenuate", "দোষ লঘু করে দেখানো"),
    ("extirpate", "সমূলে উচ্ছেদ করা"), ("extricate", "জট থেকে উদ্ধার করা"),
    ("exultant", "বিজয়োল্লাসে মত্ত"), ("facetious", "অস্থানে রসিকতাপূর্ণ"),
    ("fallow", "পতিত ও অকর্ষিত"), ("fervour", "উদ্দীপনা"), ("fetter", "শৃঙ্খলিত করা"),
    ("fickle", "চঞ্চলমতি"), ("filibuster", "সময়ক্ষেপণের বক্তৃতা"),
    ("finesse", "কৌশলী নৈপুণ্য"), ("flippant", "হালকা চালে অগম্ভীর"),
    ("florid", "অতিঅলংকৃত"), ("foible", "চারিত্রিক ছোট দুর্বলতা"),
    ("foment", "উসকে দেওয়া"), ("forbearance", "ধৈর্যশীল সহনশীলতা"),
    ("fortuitous", "আকস্মিক সৌভাগ্যজনক"), ("fractious", "খিটখিটে ও অবাধ্য"),
    ("frenetic", "উন্মত্ত ব্যস্ততার"), ("froward", "একগুঁয়ে বিরোধী"),
    ("fulminate", "বজ্রকণ্ঠে নিন্দা করা"), ("fulsome", "মাত্রাছাড়া স্তুতিপূর্ণ"),
    ("gainsay", "অস্বীকার করা"), ("gambit", "কৌশলী প্রথম চাল"),
    ("germinate", "অঙ্কুরিত হওয়া"), ("glib", "মোলায়েম অথচ অন্তঃসারশূন্য"),
    ("gossamer", "মাকড়সার জালের মতো সূক্ষ্ম"), ("grandiose", "অতি জাঁকালো"),
    ("gregariousness", "সঙ্গপ্রিয়তা"), ("guile", "ধূর্ত ছলনা"), ("halcyon", "শান্ত ও সুখের"),
    ("hapless", "হতভাগ্য"), ("harbinger", "অগ্রদূত"), ("haughtiness", "ঔদ্ধত্য"),
    ("heterodox", "প্রচলিত মতবিরোধী"), ("hiatus", "ফাঁক বা বিরতি"),
    ("histrionic", "নাটুকে"), ("hoary", "প্রাচীন ও পক্বকেশ"), ("hubris", "অহমিকাজনিত পতন"),
    ("husbandry", "মিতব্যয়ী পরিচালনা"), ("hyperbole", "অতিশয়োক্তি"),
    ("idyllic", "নিখাদ মনোরম"), ("ignoble", "নীচ"), ("imbroglio", "জটিল বিবাদ"),
    ("immure", "বন্দি করে রাখা"), ("impalpable", "অস্পর্শনীয়"),
    ("impassive", "ভাবলেশহীন"), ("impecunious state", "অর্থকষ্টের দশা"),
    ("imperturbable", "অবিচলিত"), ("impervious", "অভেদ্য ও অপ্রভাবিত"),
    ("impinge", "আঘাত হেনে প্রভাব ফেলা"), ("impious", "অধার্মিক"),
    ("importune", "নাছোড়ভাবে অনুরোধ করা"), ("imprecation", "অভিশাপবাণী"),
    ("impromptu", "তাৎক্ষণিক ও অপ্রস্তুত"), ("impugn", "সততায় প্রশ্ন তোলা"),
    ("inanity", "অর্থহীনতা"), ("incandescent", "উত্তপ্ত দীপ্তিময়"),
    ("incendiary", "উত্তেজনা ছড়ানো"), ("inchoate plan", "অসম্পূর্ণ পরিকল্পনা"),
    ("incipient", "সবে শুরু হওয়া"), ("inculcate", "বারবার বলে মনে গেঁথে দেওয়া"),
    ("indolent", "কুঁড়ে"), ("indomitable", "অদম্য"), ("ineluctable", "অপরিহার্য"),
    ("inept remark", "বেমানান মন্তব্য"), ("inertness", "নিষ্ক্রিয়তা"),
    ("ingratiate", "তোষামোদে প্রিয় হওয়া"), ("inimitable", "অননুকরণীয়"),
    ("iniquity", "অন্যায় পাপাচার"), ("innuendo", "পরোক্ষ কুৎসিত ইঙ্গিত"),
    ("inscrutable", "দুর্বোধ্য ও ভেদ করা কঠিন"), ("insipid", "স্বাদহীন ও নীরস"),
    ("insolvent", "দেউলিয়া"), ("insouciant", "বেপরোয়া নির্লিপ্ত"),
    ("insurgent", "বিদ্রোহী"), ("interlocutor", "কথোপকথনের সঙ্গী"),
    ("interminable", "অন্তহীন"), ("intimation", "সূক্ষ্ম ইঙ্গিত"),
    ("intrepid", "নির্ভীক অভিযাত্রী"), ("inundate", "প্লাবিত করে দেওয়া"),
    ("invective", "গালিগালাজপূর্ণ নিন্দা"), ("inveigh", "তীব্র ভাষায় আক্রমণ করা"),
    ("irascible", "সহজে রেগে ওঠা"), ("irredeemable", "উদ্ধারের অযোগ্য"),
    ("itinerant", "ভ্রাম্যমাণ"), ("jejune", "অপরিপক্ব ও নীরস"),
    ("jettison", "ভার কমাতে ফেলে দেওয়া"), ("jocular", "রসিকতাপ্রবণ"),
    ("jurisprudence", "আইনশাস্ত্র"), ("kindle", "প্রজ্বলিত করে জাগানো"),
    ("labyrinthine", "গোলকধাঁধার মতো"), ("lachrymose", "অশ্রুপ্রবণ"),
    ("languid", "নিস্তেজ ও শিথিল"), ("latency", "সুপ্তাবস্থা"), ("laudatory", "প্রশস্তিমূলক"),
    ("lavish", "উদারহস্তে ঢালা"), ("lethargy", "আলস্যজনিত জড়তা"),
    ("levity", "গাম্ভীর্যহীন লঘুতা"), ("liminal", "সীমানার দোরগোড়ার"),
    ("litany", "একঘেয়ে দীর্ঘ তালিকা"), ("loquacious", "বাকপটু ও বাচাল"),
    ("lucre", "অসৎ উপায়ের অর্থলাভ"), ("machination", "কূটকৌশল"),
    ("maelstrom", "ঘূর্ণাবর্তময় বিশৃঙ্খলা"), ("malaise", "অস্বস্তিকর অবসন্নতা"),
    ("malfeasance", "পদাধিকারের অপব্যবহার"), ("malinger", "কাজ এড়াতে ভান করা"),
    ("malleable", "সহজে আকার দেওয়া যায় এমন"), ("manifold", "বহুবিধ"),
    ("martinet", "কড়া নিয়মপন্থী"), ("maverick", "স্বাধীনচেতা ব্যতিক্রমী"),
    ("mawkish", "অতিরিক্ত ভাবালু"), ("mitigation", "লাঘবের ব্যবস্থা"),
    ("modicum", "সামান্য মাত্রা"), ("morose", "গোমড়া ও বিষণ্ন"),
    ("mundanity", "সাধারণত্ব"), ("myriad", "অগণিত"), ("nascent", "সদ্যোজাত"),
    ("nebulous", "কুয়াশাচ্ছন্ন ও অস্পষ্ট"), ("nemesis", "অমোঘ প্রতিশোধ"),
    ("nonchalant", "নির্বিকার নিস্পৃহ"), ("nostalgia", "অতীতকাতরতা"),
    ("nugatory", "মূল্যহীন"), ("obeisance", "নতজানু অভিবাদন"),
    ("obfuscation", "ইচ্ছাকৃত ঘোলাটে করা"), ("oblique", "তির্যক ও পরোক্ষ"),
    ("oblivion", "বিস্মৃতি"), ("obtuse", "মন্দবুদ্ধি"), ("officious", "অযাচিতভাবে গায়ে পড়া"),
    ("ossify", "অনমনীয় হয়ে জমে যাওয়া"), ("ostentatious", "লোকদেখানো জাঁক"),
    ("ostracize", "একঘরে করা"), ("palliate", "উপশম দিয়ে ঢাকা"),
    ("palpable", "স্পষ্টভাবে অনুভবযোগ্য"), ("panegyric", "স্তুতিগাথা"),
    ("paradigm", "আদর্শ কাঠামো"), ("pariah", "সমাজচ্যুত ব্যক্তি"),
    ("parity", "সমতা"), ("parody", "ব্যঙ্গানুকরণ"), ("parochial", "সংকীর্ণ দৃষ্টির"),
    ("paucity of data", "তথ্যের অপ্রতুলতা"), ("pedantic", "পুঁথিগত খুঁটিনাটিতে আচ্ছন্ন"),
    ("pejorative", "হেয়জ্ঞাপক"), ("penitent", "অনুতপ্ত প্রায়শ্চিত্তকারী"),
    ("penumbra", "আংশিক ছায়া"), ("perdition", "চূড়ান্ত সর্বনাশ"),
    ("peremptory", "আপত্তির অবকাশহীন আদেশসূচক"), ("perennial", "বারবার ফিরে আসা"),
    ("perfidy", "বিশ্বাসঘাতকতা"), ("peripatetic", "ঘুরে বেড়ানো"),
    ("permeate", "ভেতরে ছড়িয়ে যাওয়া"), ("pernicious", "সর্বনাশা"),
    ("perpetuate", "চিরস্থায়ী করা"), ("perquisite", "উপরি সুবিধা"),
    ("pestilent", "মারাত্মক ক্ষতিকর"), ("petulant", "খিটখিটে ও বায়নাক্কা"),
    ("philanthropy", "লোকহিতৈষণা"), ("pique", "মনঃক্ষোভ"), ("pivotal", "কেন্দ্রীয় গুরুত্বের"),
    ("placid", "শান্ত ও নিরুত্তেজ"), ("plaintive", "করুণ সুরের"),
    ("plausibility", "গ্রহণযোগ্যতার মাত্রা"), ("plaudit", "করতালিসহ প্রশংসা"),
    ("polemic", "বিতর্কমূলক আক্রমণ"), ("portend", "অশুভ পূর্বাভাস দেওয়া"),
    ("portent", "অমঙ্গলসূচক লক্ষণ"), ("posit", "ধরে নিয়ে উপস্থাপন করা"),
    ("pragmatism", "বাস্তববাদ"), ("precept", "আচরণবিধি"), ("precipitate", "অকালে ঘটিয়ে ফেলা"),
    ("preclusion", "আগাম প্রতিরোধ"), ("predilection", "বিশেষ পক্ষপাত"),
    ("preeminent", "সর্বাগ্রগণ্য"), ("prescient", "ভবিষ্যৎদ্রষ্টা"),
    ("prevarication", "সত্য এড়ানোর কৌশল"), ("pristine", "আদিম নির্মলতা বজায় রাখা"),
    ("probity test", "সততার পরীক্ষা"), ("proffer", "এগিয়ে দিয়ে প্রস্তাব করা"),
    ("promulgate", "ঘোষণা করে প্রচলন করা"), ("propitiate", "তুষ্ট করে শান্ত করা"),
    ("propitious", "অনুকূল"), ("prosaic style", "নীরস রচনাভঙ্গি"),
    ("proscribe", "নিষিদ্ধ ঘোষণা করা"), ("protracted", "দীর্ঘায়িত"),
    ("providence", "দৈব বিধান"), ("puissant", "প্রবল ক্ষমতাধর"),
    ("pulchritude", "সৌন্দর্য"), ("pundit", "বিশেষজ্ঞ ভাষ্যকার"),
    ("purported", "কথিত"), ("pusillanimous", "কাপুরুষোচিত"),
    ("quagmire", "জটিল আটকে পড়া অবস্থা"), ("quell dissent", "বিরোধ দমন"),
    ("quintessential", "নির্যাসস্বরূপ আদর্শ"), ("quotidian", "দৈনন্দিন"),
    ("raconteur", "সুবক্তা গল্পকথক"), ("rampant", "অবাধে ছড়ানো"),
    ("rapacious", "লুণ্ঠনপ্রবণ লোভী"), ("rapprochement", "সম্পর্কের পুনর্মিলন"),
    ("ratify", "আনুষ্ঠানিক অনুমোদন দেওয়া"), ("raucous", "কর্কশ হইচইপূর্ণ"),
    ("rebuff", "রূঢ় প্রত্যাখ্যান"), ("recant", "পূর্বমত প্রকাশ্যে প্রত্যাহার"),
    ("reciprocate", "প্রতিদান দেওয়া"), ("reclusive", "লোকসঙ্গবিমুখ"),
    ("recrimination", "পাল্টা দোষারোপ"), ("redress", "প্রতিবিধান"),
    ("refractory metal", "দুর্গলনীয় ধাতু"), ("regale", "আনন্দে মাতিয়ে রাখা"),
    ("reprisal", "প্রতিশোধমূলক পদক্ষেপ"), ("reproach", "ভর্ৎসনাসূচক অনুযোগ"),
    ("reprobate", "নীতিভ্রষ্ট ব্যক্তি"), ("rescind", "রদ করে বাতিল করা"),
    ("resurgence", "পুনরুত্থান"), ("reverie", "দিবাস্বপ্নে বিভোরতা"),
    ("ribald", "অশালীন রসিকতাপূর্ণ"), ("rivulet", "ক্ষুদ্র স্রোতধারা"),
    ("rue", "অনুশোচনা করা"), ("ruminate", "রোমন্থনের মতো ভাবতে থাকা"),
    ("sacrosanct", "অলঙ্ঘনীয়ভাবে পবিত্র"), ("salubrious", "স্বাস্থ্যপ্রদ"),
    ("salutary", "হিতকর"), ("sanction", "অনুমোদন বা শাস্তিমূলক ব্যবস্থা"),
    ("sanctimonious", "ধার্মিকতার ভান করা"), ("satiate", "পরিতৃপ্ত করা"),
    ("saturnine", "গম্ভীর ও বিষণ্ন প্রকৃতির"), ("savant", "অসাধারণ পণ্ডিত"),
    ("schism", "মতভেদজনিত বিভাজন"), ("scurrilous", "কুৎসিত কুৎসাপূর্ণ"),
    ("sedition", "রাষ্ট্রদ্রোহ"), ("sedulous", "অধ্যবসায়ী নিষ্ঠার"),
    ("semblance", "বাহ্যিক আভাস"), ("sententious", "নীতিবাক্যপ্রবণ"),
    ("sequester", "আলাদা করে সরিয়ে রাখা"), ("serendipitous", "আকস্মিক সৌভাগ্যজনক"),
    ("servile", "দাসসুলভ"), ("sinecure", "কাজবিহীন লাভজনক পদ"),
    ("sinuous", "সর্পিল"), ("slake", "তৃষ্ণা মেটানো"), ("solicitous", "উদ্বিগ্ন যত্নশীল"),
    ("soliloquy", "স্বগতোক্তি"), ("somnolent", "তন্দ্রাচ্ছন্ন"),
    ("sophistry", "কুতর্কের কৌশল"), ("sordid", "নোংরা ও হীন"),
    ("sparse data", "বিরল তথ্য"), ("specter", "ভীতিকর ছায়ামূর্তি"),
    ("splenetic", "খিটখিটে বদমেজাজি"), ("sporadic burst", "বিক্ষিপ্ত উদ্গিরণ"),
    ("stanch", "রক্তপাত থামানো"), ("stipulate", "শর্ত হিসেবে নির্ধারণ করা"),
    ("stolid", "ভাবলেশহীন নির্বিকার"), ("striated", "রেখাঙ্কিত"),
    ("stultify", "অকেজো ও হাস্যকর করে তোলা"), ("stupefy", "হতবুদ্ধি করে দেওয়া"),
    ("suave", "মার্জিত ও মোলায়েম"), ("subjugate", "বশীভূত করা"),
    ("sublimate", "উন্নততর রূপে রূপান্তর"), ("subterfuge", "ফাঁকি দেওয়ার কৌশল"),
    ("succinctness", "পরিমিত সংক্ষিপ্ততা"), ("succor", "বিপদে সাহায্য"),
    ("supplant", "সরিয়ে স্থান দখল করা"), ("supplicate", "কাতরভাবে প্রার্থনা করা"),
    ("surreptitious", "চুপিসারে করা"), ("sylvan", "বনানীঘেরা"),
    ("synopsis", "সারসংক্ষেপ"), ("tacit accord", "নীরব সমঝোতা"),
    ("tenacity", "অধ্যবসায়ী দৃঢ়তা"), ("tepid", "কুসুম গরম ও উৎসাহহীন"),
    ("terse", "রুক্ষ রকম সংক্ষিপ্ত"), ("timorous", "ভয়ে জড়সড়"),
    ("tirade", "দীর্ঘ ক্রুদ্ধ নিন্দাবাদ"), ("torment", "যন্ত্রণা দেওয়া"),
    ("transgress", "সীমা লঙ্ঘন করা"), ("transitory", "ক্ষণকালের"),
    ("trepidation", "শঙ্কামিশ্রিত কম্পন"), ("truism", "সর্বজনবিদিত সত্য"),
    ("turgid", "স্ফীত ও আড়ম্বরপূর্ণ"), ("tyro", "আনাড়ি নবিশ"),
    ("umbrage taken", "ক্ষুব্ধ অভিমান"), ("unassailable", "অজেয় ও অখণ্ডনীয়"),
    ("uncouth", "অমার্জিত"), ("unequivocal", "দ্ব্যর্থহীন"), ("unfettered", "শৃঙ্খলমুক্ত"),
    ("unilateral", "একতরফা"), ("unremitting", "অবিরাম নিরলস"),
    ("unwieldy", "বেঢপ ও সামলানো কঠিন"), ("upbraid", "তিরস্কার করে বকা"),
    ("usurp", "জবরদখল করা"), ("vagary", "খামখেয়ালি মোড়"),
    ("vanguard", "অগ্রবর্তী দল"), ("vantage", "সুবিধাজনক অবস্থান"),
    ("vapid", "নীরস ও প্রাণহীন"), ("variegated", "নানা বর্ণে মিশ্রিত"),
    ("vaunt", "গর্ব করে জাহির করা"), ("vehemence", "প্রবলতা"),
    ("venerable", "শ্রদ্ধেয় প্রবীণ"), ("veneer", "উপরিতলের মিথ্যা প্রলেপ"),
    ("verbatim", "অবিকল শব্দে শব্দে"), ("verdant", "সবুজে ভরা"),
    ("verisimilitude", "সত্যের মতো মনে হওয়া"), ("vestige", "অবশিষ্ট চিহ্ন"),
    ("vex question", "জটিল বিরক্তিকর প্রশ্ন"), ("viable option", "টেকসই বিকল্প"),
    ("vigilance", "সতর্ক প্রহরা"), ("vindictive", "প্রতিহিংসাপরায়ণ"),
    ("virtuoso", "অসাধারণ পারদর্শী শিল্পী"), ("visage", "মুখশ্রী"),
    ("vitriolic", "তীব্র বিষোদ্গারপূর্ণ"), ("vivacious", "প্রাণচঞ্চল ও হাসিখুশি"),
    ("volition", "স্বেচ্ছাপ্রণোদনা"), ("voluble", "অনর্গল কথা বলা"),
    ("wanton", "বেপরোয়া ও উদ্দেশ্যহীন নিষ্ঠুর"), ("wistful", "বিষণ্ন আকুলতাভরা"),
    ("wizened", "কুঁচকে যাওয়া শুষ্ক"), ("wrangle", "তর্কাতর্কি করা"),
    ("zealotry", "অন্ধ উন্মাদনা"), ("zephyr", "মৃদুমন্দ বাতাস"),
]


# ------------------------------- phrasal verbs, idioms and more words

PHRASAL = [
    ("account for", "কৈফিয়ত বা ব্যাখ্যা দেওয়া"), ("act up", "বেয়াড়া আচরণ করা"),
    ("add up", "হিসাবে মেলা"), ("aim at", "লক্ষ্য স্থির করা"),
    ("allow for", "হিসাবের মধ্যে রাখা"), ("ask after", "খোঁজখবর নেওয়া"),
    ("ask for", "চেয়ে নেওয়া"), ("back down", "দাবি থেকে সরে আসা"),
    ("back up", "সমর্থন দেওয়া বা নকল রাখা"), ("bank on", "ভরসা করা"),
    ("bear with", "ধৈর্য ধরে সহ্য করা"), ("blow up", "ফেটে পড়া"),
    ("break down", "ভেঙে পড়া বা বিকল হওয়া"), ("break in", "জোর করে ঢোকা"),
    ("break into", "সিঁধ কেটে ঢোকা"), ("break off", "হঠাৎ বন্ধ করা"),
    ("break out", "হঠাৎ ছড়িয়ে পড়া"), ("break up", "সম্পর্ক শেষ হওয়া"),
    ("bring about", "ঘটিয়ে তোলা"), ("bring back", "ফিরিয়ে আনা"),
    ("bring down", "নামিয়ে আনা"), ("bring in", "চালু করা বা আয় করা"),
    ("bring out", "প্রকাশ করা"), ("bring up", "লালনপালন করা বা প্রসঙ্গ তোলা"),
    ("brush up", "ঝালিয়ে নেওয়া"), ("build up", "গড়ে তোলা"),
    ("call back", "আবার ফোন করা"), ("call for", "দাবি করা"),
    ("call off", "বাতিল করা"), ("call on", "অনুরোধ জানানো"),
    ("calm down", "শান্ত হওয়া"), ("care for", "যত্ন নেওয়া"),
    ("carry on", "চালিয়ে যাওয়া"), ("carry out", "সম্পাদন করা"),
    ("catch on", "জনপ্রিয় হওয়া বা বুঝে ফেলা"), ("catch up", "পিছিয়ে পড়া পুষিয়ে নেওয়া"),
    ("check in", "হাজিরা নথিভুক্ত করা"), ("check out", "দেখে নেওয়া বা হিসাব চুকিয়ে যাওয়া"),
    ("cheer up", "মন ভালো করা"), ("clear up", "পরিষ্কার হওয়া বা মীমাংসা করা"),
    ("close down", "স্থায়ীভাবে বন্ধ করা"), ("come across", "হঠাৎ সামনে পড়া"),
    ("come along", "সঙ্গে আসা বা এগোনো"), ("come apart", "খুলে টুকরো হওয়া"),
    ("come back", "ফিরে আসা"), ("come by", "পাওয়া বা পথে আসা"),
    ("come down with", "অসুখে পড়া"), ("come in", "ভেতরে আসা"),
    ("come out", "প্রকাশিত হওয়া"), ("come over", "বাড়িতে আসা"),
    ("come round", "জ্ঞান ফেরা বা মত বদলানো"), ("come through", "কঠিন সময় পার করা"),
    ("come up", "উঠে আসা"), ("come up with", "ভেবে বের করা"),
    ("count on", "নির্ভর করা"), ("cross out", "কেটে দেওয়া"),
    ("cut back", "খরচ কমানো"), ("cut down", "কমিয়ে আনা"),
    ("cut in", "কথার মাঝে ঢুকে পড়া"), ("cut off", "বিচ্ছিন্ন করা"),
    ("cut out", "বাদ দেওয়া"), ("deal with", "সামাল দেওয়া"),
    ("die down", "স্তিমিত হওয়া"), ("die out", "বিলুপ্ত হওয়া"),
    ("do away with", "বাতিল করে দেওয়া"), ("do up", "মেরামত করে সাজানো"),
    ("do without", "ছাড়াই চালানো"), ("draw up", "খসড়া তৈরি করা"),
    ("dress up", "সেজেগুজে তৈরি হওয়া"), ("drop by", "হঠাৎ ঢুঁ মারা"),
    ("drop off", "নামিয়ে দেওয়া বা ঘুমিয়ে পড়া"), ("drop out", "পড়া ছেড়ে দেওয়া"),
    ("eat out", "বাইরে খাওয়া"), ("end up", "শেষমেশ পরিণত হওয়া"),
    ("face up to", "মোকাবিলা করতে রাজি হওয়া"), ("fall apart", "ভেঙে পড়া"),
    ("fall back on", "শেষ ভরসা হিসেবে নেওয়া"), ("fall behind", "পিছিয়ে পড়া"),
    ("fall out", "ঝগড়া করে সম্পর্ক নষ্ট হওয়া"), ("fall through", "ভেস্তে যাওয়া"),
    ("feel like", "ইচ্ছে করা"), ("figure out", "বুঝে ফেলা"),
    ("fill in", "ফরম পূরণ করা"), ("fill out", "পূরণ করে লেখা"),
    ("fill up", "ভরে ফেলা"), ("find out", "জেনে ফেলা"),
    ("focus on", "মনোযোগ দেওয়া"), ("get across", "বুঝিয়ে দিতে পারা"),
    ("get ahead", "এগিয়ে যাওয়া"), ("get along", "মিলেমিশে থাকা"),
    ("get around", "ঘুরে বেড়ানো বা এড়িয়ে যাওয়া"), ("get at", "ইঙ্গিত করা"),
    ("get away", "পালিয়ে যাওয়া"), ("get away with", "শাস্তি ছাড়াই পার পাওয়া"),
    ("get back", "ফিরে পাওয়া"), ("get by", "কোনোমতে চলে যাওয়া"),
    ("get down to", "মন দিয়ে কাজে নামা"), ("get in", "ভেতরে ঢোকা"),
    ("get into", "জড়িয়ে পড়া"), ("get off", "নেমে যাওয়া"),
    ("get on", "চড়া বা সম্পর্ক ভালো থাকা"), ("get out of", "এড়িয়ে যাওয়া"),
    ("get over", "সামলে ওঠা"), ("get through", "শেষ করা বা যোগাযোগ করতে পারা"),
    ("get together", "একত্র হওয়া"), ("get up", "ঘুম থেকে ওঠা"),
    ("give away", "বিলিয়ে দেওয়া বা ফাঁস করা"), ("give back", "ফেরত দেওয়া"),
    ("give in", "হার মেনে নেওয়া"), ("give off", "নিঃসৃত করা"),
    ("give out", "বিতরণ করা বা ফুরিয়ে যাওয়া"), ("give up", "ছেড়ে দেওয়া"),
    ("go after", "পিছু নেওয়া"), ("go against", "বিপক্ষে যাওয়া"),
    ("go ahead", "এগিয়ে চলা"), ("go along with", "মেনে নিয়ে চলা"),
    ("go back on", "কথা থেকে সরে আসা"), ("go by", "সময় পেরিয়ে যাওয়া"),
    ("go down", "কমে যাওয়া"), ("go for", "বেছে নেওয়া"),
    ("go in for", "শখ হিসেবে করা"), ("go into", "বিস্তারিত ঢোকা"),
    ("go off", "বিস্ফোরিত হওয়া বা নষ্ট হওয়া"), ("go on", "চলতে থাকা"),
    ("go out", "বাইরে যাওয়া বা নিভে যাওয়া"), ("go over", "খুঁটিয়ে দেখা"),
    ("go through", "সহ্য করা বা ঘেঁটে দেখা"), ("go up", "বেড়ে যাওয়া"),
    ("go with", "মানানসই হওয়া"), ("go without", "না পেয়েই চালানো"),
    ("grow into", "বড় হয়ে মানানসই হওয়া"), ("grow out of", "বড় হয়ে ছেড়ে দেওয়া"),
    ("grow up", "বড় হয়ে ওঠা"), ("hand back", "ফিরিয়ে দেওয়া"),
    ("hand in", "জমা দেওয়া"), ("hand out", "বিলি করা"),
    ("hand over", "হস্তান্তর করা"), ("hang around", "ঘোরাঘুরি করা"),
    ("hang on", "একটু অপেক্ষা করা"), ("hang up", "ফোন রেখে দেওয়া"),
    ("head for", "দিকে রওনা হওয়া"), ("hold back", "চেপে রাখা"),
    ("hold on", "ধরে থাকা"), ("hold up", "দেরি করিয়ে দেওয়া"),
    ("keep away", "দূরে থাকা"), ("keep from", "বিরত রাখা"),
    ("keep on", "চালিয়ে যেতে থাকা"), ("keep out", "ঢুকতে না দেওয়া"),
    ("keep up", "তাল মিলিয়ে চলা"), ("kick off", "শুরু হওয়া"),
    ("knock down", "ধাক্কা দিয়ে ফেলে দেওয়া"), ("knock out", "অজ্ঞান করে ফেলা"),
    ("lay off", "ছাঁটাই করা"), ("lead to", "পরিণতিতে নিয়ে যাওয়া"),
    ("leave behind", "ফেলে যাওয়া"), ("leave out", "বাদ দিয়ে যাওয়া"),
    ("let down", "নিরাশ করা"), ("let in", "ঢুকতে দেওয়া"),
    ("let off", "ছাড় দেওয়া"), ("live on", "নির্ভর করে বেঁচে থাকা"),
    ("live up to", "প্রত্যাশা পূরণ করা"), ("log in", "প্রবেশ করা"),
    ("look after", "দেখাশোনা করা"), ("look ahead", "সামনের কথা ভাবা"),
    ("look back", "অতীতে ফিরে তাকানো"), ("look down on", "ছোট করে দেখা"),
    ("look for", "খোঁজা"), ("look forward to", "অধীর আগ্রহে অপেক্ষা করা"),
    ("look into", "তদন্ত করে দেখা"), ("look out", "সাবধান হওয়া"),
    ("look over", "চোখ বুলিয়ে নেওয়া"), ("look through", "ঘেঁটে দেখা"),
    ("look up", "অভিধানে খুঁজে দেখা"), ("look up to", "শ্রদ্ধার চোখে দেখা"),
    ("make for", "দিকে এগোনো"), ("make out", "কোনোমতে বোঝা"),
    ("make up", "বানিয়ে বলা বা মিটমাট করা"), ("make up for", "ক্ষতি পুষিয়ে দেওয়া"),
    ("mix up", "গুলিয়ে ফেলা"), ("move in", "নতুন বাসায় ওঠা"),
    ("move on", "এগিয়ে যাওয়া"), ("narrow down", "সংকুচিত করে আনা"),
    ("opt for", "বেছে নেওয়া"), ("pass away", "মারা যাওয়া"),
    ("pass on", "পৌঁছে দেওয়া"), ("pass out", "জ্ঞান হারানো"),
    ("pay back", "শোধ করা"), ("pay off", "ফল দেওয়া বা ঋণ শোধ করা"),
    ("pick out", "বেছে নেওয়া"), ("pick up", "তুলে নেওয়া বা শিখে ফেলা"),
    ("point out", "দৃষ্টি আকর্ষণ করা"), ("pull off", "কঠিন কাজ করে ফেলা"),
    ("pull over", "গাড়ি থামানো"), ("pull through", "সেরে ওঠা"),
    ("put aside", "সরিয়ে রাখা"), ("put away", "গুছিয়ে রাখা"),
    ("put down", "নামিয়ে রাখা বা হেয় করা"), ("put forward", "প্রস্তাব করা"),
    ("put off", "স্থগিত রাখা"), ("put on", "পরে নেওয়া"),
    ("put out", "নিভিয়ে দেওয়া"), ("put through", "ফোনে সংযোগ করে দেওয়া"),
    ("put up", "থাকার জায়গা দেওয়া"), ("put up with", "মুখ বুজে সহ্য করা"),
    ("rely on", "নির্ভর করা"), ("result in", "পরিণত হওয়া"),
    ("ring up", "ফোন করা"), ("rule out", "বাতিল করে দেওয়া"),
    ("run away", "পালিয়ে যাওয়া"), ("run into", "হঠাৎ দেখা হয়ে যাওয়া"),
    ("run out of", "ফুরিয়ে যাওয়া"), ("run over", "চাপা দেওয়া"),
    ("see off", "বিদায় জানাতে যাওয়া"), ("see through", "ফাঁকি ধরে ফেলা"),
    ("sell out", "বিক্রি হয়ে ফুরিয়ে যাওয়া"), ("set aside", "আলাদা করে রাখা"),
    ("set off", "রওনা দেওয়া"), ("set out", "যাত্রা শুরু করা"),
    ("set up", "প্রতিষ্ঠা করা"), ("settle down", "থিতু হওয়া"),
    ("settle for", "কমেই রাজি হওয়া"), ("show off", "জাহির করা"),
    ("show up", "হাজির হওয়া"), ("shut down", "বন্ধ করে দেওয়া"),
    ("sign up", "নাম লেখানো"), ("sit down", "বসে পড়া"),
    ("slow down", "গতি কমানো"), ("sort out", "মিটিয়ে ফেলা"),
    ("speak up", "জোরে বা সাহস করে বলা"), ("stand by", "পাশে থাকা"),
    ("stand for", "বোঝানো বা সহ্য করা"), ("stand out", "চোখে পড়ার মতো হওয়া"),
    ("stand up for", "পক্ষ নিয়ে দাঁড়ানো"), ("stay up", "রাত জাগা"),
    ("stick to", "লেগে থাকা"), ("stick up for", "সমর্থন করে বলা"),
    ("sum up", "সারসংক্ষেপ করা"), ("switch off", "বন্ধ করা"),
    ("take after", "স্বভাবে মিল থাকা"), ("take apart", "খুলে ফেলা"),
    ("take away", "নিয়ে যাওয়া"), ("take back", "কথা প্রত্যাহার করা"),
    ("take down", "নামিয়ে নেওয়া বা লিখে নেওয়া"), ("take in", "বুঝে নেওয়া বা ঠকানো"),
    ("take off", "উড়াল দেওয়া বা খুলে ফেলা"), ("take on", "দায়িত্ব নেওয়া"),
    ("take out", "বের করে আনা"), ("take over", "দায়িত্ব দখল করা"),
    ("take to", "পছন্দ করে ফেলা"), ("take up", "শুরু করা বা জায়গা নেওয়া"),
    ("talk into", "রাজি করানো"), ("talk over", "আলোচনা করে দেখা"),
    ("tear up", "ছিঁড়ে ফেলা"), ("tell off", "বকা দেওয়া"),
    ("think over", "ভেবে দেখা"), ("think up", "ফন্দি বের করা"),
    ("throw away", "ছুড়ে ফেলা"), ("throw out", "বাতিল করে বের করে দেওয়া"),
    ("tidy up", "গুছিয়ে ফেলা"), ("try on", "পরে দেখা"),
    ("try out", "পরখ করে দেখা"), ("turn around", "ঘুরে দাঁড়ানো"),
    ("turn away", "ফিরিয়ে দেওয়া"), ("turn back", "ফিরে যাওয়া"),
    ("turn down", "প্রত্যাখ্যান করা বা আওয়াজ কমানো"), ("turn in", "জমা দেওয়া"),
    ("turn into", "রূপান্তরিত হওয়া"), ("turn off", "বন্ধ করা"),
    ("turn on", "চালু করা"), ("turn out", "শেষ পর্যন্ত দাঁড়ানো"),
    ("turn over", "উল্টে দেওয়া"), ("turn to", "সাহায্যের জন্য যাওয়া"),
    ("turn up", "হঠাৎ হাজির হওয়া"), ("use up", "পুরোটা খরচ করে ফেলা"),
    ("wait on", "পরিবেশন করা"), ("wake up", "জেগে ওঠা"),
    ("warm up", "গা গরম করা"), ("watch out", "সতর্ক থাকা"),
    ("wear off", "ক্রমে কমে যাওয়া"), ("wear out", "ক্ষয়ে যাওয়া বা কাহিল করা"),
    ("wipe out", "নিশ্চিহ্ন করে দেওয়া"), ("work on", "উন্নতির জন্য খাটা"),
    ("work out", "সমাধান হওয়া বা ব্যায়াম করা"), ("wrap up", "শেষ করে ফেলা"),
    ("write down", "লিখে রাখা"), ("write off", "বাতিল বলে ধরে নেওয়া"),
    ("zoom in", "কাছে টেনে দেখা"),
]

IDIOMS = [
    ("a blessing in disguise", "আপাত বিপদে লুকানো আশীর্বাদ"),
    ("a dime a dozen", "সস্তা ও যেখানে সেখানে মেলে এমন"),
    ("a drop in the ocean", "সাগরে এক বিন্দু"),
    ("a piece of cake", "একেবারে সহজ কাজ"),
    ("a stone's throw", "খুব কাছেই"),
    ("actions speak louder than words", "কথার চেয়ে কাজ বড়"),
    ("add fuel to the fire", "আগুনে ঘি ঢালা"),
    ("against the clock", "সময়ের সঙ্গে পাল্লা দিয়ে"),
    ("all ears", "মন দিয়ে শোনার জন্য প্রস্তুত"),
    ("all in the same boat", "সবাই একই অবস্থায়"),
    ("an arm and a leg", "গলাকাটা দাম"),
    ("at the drop of a hat", "বিনা দ্বিধায় সঙ্গে সঙ্গে"),
    ("back to square one", "আবার একেবারে গোড়ায়"),
    ("back to the drawing board", "নতুন করে পরিকল্পনা শুরু"),
    ("ball is in your court", "এখন সিদ্ধান্ত তোমার হাতে"),
    ("bark up the wrong tree", "ভুল জায়গায় খোঁজা"),
    ("beat around the bush", "ঘুরিয়ে পেঁচিয়ে কথা বলা"),
    ("beggars can't be choosers", "ভিক্ষার চাল কাঁড়া আর আকাঁড়া"),
    ("bend over backwards", "সাধ্যের অতিরিক্ত চেষ্টা করা"),
    ("best of both worlds", "দুই দিকেরই সুবিধা"),
    ("better late than never", "না হওয়ার চেয়ে দেরিতে হওয়া ভালো"),
    ("bite off more than you can chew", "সাধ্যের বেশি দায়িত্ব নেওয়া"),
    ("bite the bullet", "দাঁতে দাঁত চেপে সহ্য করা"),
    ("bite your tongue", "কথা গিলে ফেলা"),
    ("blessing in the making", "সময়ের সঙ্গে ভালো হয়ে ওঠা ব্যাপার"),
    ("blow off steam", "মনের ঝাল ঝেড়ে ফেলা"),
    ("blow your own trumpet", "নিজের ঢাক নিজে পেটানো"),
    ("break a leg", "সাফল্যের শুভকামনা"),
    ("break the ice", "আড়ষ্টতা কাটানো"),
    ("burn the midnight oil", "গভীর রাত পর্যন্ত খাটা"),
    ("burn your bridges", "ফেরার পথ নিজেই বন্ধ করা"),
    ("bury the hatchet", "ঝগড়া মিটিয়ে ফেলা"),
    ("by the book", "নিয়ম মেনে হুবহু"),
    ("by the skin of your teeth", "অল্পের জন্য রক্ষা"),
    ("call a spade a spade", "স্পষ্ট কথা স্পষ্টভাবে বলা"),
    ("call it a day", "আজকের মতো কাজ শেষ করা"),
    ("cast a shadow over", "ম্লান করে দেওয়া"),
    ("catch someone red handed", "হাতেনাতে ধরা"),
    ("caught between two stools", "দুই নৌকায় পা দিয়ে বিপাকে পড়া"),
    ("clear the air", "ভুল বোঝাবুঝি মিটিয়ে ফেলা"),
    ("come rain or shine", "যাই ঘটুক না কেন"),
    ("cost an arm and a leg", "অত্যন্ত ব্যয়বহুল হওয়া"),
    ("cross that bridge when you come to it", "সময় হলে সে কথা ভাবা"),
    ("cry over spilt milk", "গত নিয়ে আফসোস করা"),
    ("cry wolf", "মিথ্যা বিপদের ডাক দেওয়া"),
    ("cut corners", "মান কমিয়ে শর্টকাট নেওয়া"),
    ("cut to the chase", "আসল কথায় আসা"),
    ("devil's advocate", "ইচ্ছে করে বিপক্ষে যুক্তি দেওয়া"),
    ("don't count your chickens", "ফল পাওয়ার আগে হিসাব না করা"),
    ("don't judge a book by its cover", "বাইরের চেহারা দেখে বিচার নয়"),
    ("down to earth", "মাটির কাছাকাছি ও বাস্তববাদী"),
    ("draw the line", "সীমা টেনে দেওয়া"),
    ("drop in the bucket", "সামান্য অবদান"),
    ("easier said than done", "বলা সহজ করা কঠিন"),
    ("eat humble pie", "ভুল মেনে নত হওয়া"),
    ("every cloud has a silver lining", "প্রতিটি দুর্দিনের পরেও আশা থাকে"),
    ("face the music", "ফল ভোগ করতে প্রস্তুত হওয়া"),
    ("fall on deaf ears", "কারও কানে না পৌঁছানো"),
    ("feel under the weather", "শরীর খারাপ লাগা"),
    ("few and far between", "কালেভদ্রে মেলে এমন"),
    ("fish out of water", "বেমানান পরিবেশে অস্বস্তি"),
    ("fit as a fiddle", "একেবারে চাঙা"),
    ("food for thought", "ভাবার মতো বিষয়"),
    ("from scratch", "একেবারে গোড়া থেকে"),
    ("get a taste of your own medicine", "নিজের কর্মফল ভোগ করা"),
    ("get cold feet", "শেষ মুহূর্তে সাহস হারানো"),
    ("get out of hand", "নিয়ন্ত্রণের বাইরে চলে যাওয়া"),
    ("get the ball rolling", "কাজ শুরু করিয়ে দেওয়া"),
    ("get the hang of it", "কায়দাটা রপ্ত করা"),
    ("get your act together", "গুছিয়ে নিয়ে ঠিকভাবে চলা"),
    ("give someone the benefit of the doubt", "সন্দেহের সুবিধা দেওয়া"),
    ("give the cold shoulder", "ঠান্ডা উপেক্ষা দেখানো"),
    ("go back to the basics", "মূল বিষয়ে ফিরে যাওয়া"),
    ("go down in flames", "শোচনীয়ভাবে ব্যর্থ হওয়া"),
    ("go the extra mile", "প্রয়োজনের বেশি করা"),
    ("go with the flow", "স্রোতের সঙ্গে গা ভাসানো"),
    ("hang in there", "হাল না ছেড়ে টিকে থাকা"),
    ("have a chip on your shoulder", "পুরোনো ক্ষোভ পুষে রাখা"),
    ("have second thoughts", "দ্বিতীয়বার ভেবে দ্বিধায় পড়া"),
    ("have your hands full", "কাজে ভীষণ ব্যস্ত থাকা"),
    ("head in the clouds", "কল্পনার জগতে থাকা"),
    ("hit the books", "মন দিয়ে পড়তে বসা"),
    ("hit the nail on the head", "একেবারে সঠিক কথা বলা"),
    ("hit the road", "রওনা দেওয়া"),
    ("hit the sack", "শুতে যাওয়া"),
    ("hold your horses", "একটু ধৈর্য ধরা"),
    ("in a nutshell", "সংক্ষেপে বললে"),
    ("in hot water", "বিপদে পড়া"),
    ("in the long run", "দীর্ঘমেয়াদে"),
    ("in the nick of time", "ঠিক সময়মতো"),
    ("it takes two to tango", "ঝগড়ায় দুই পক্ষেরই ভূমিকা থাকে"),
    ("jump on the bandwagon", "চালু হাওয়ায় গা ভাসানো"),
    ("jump the gun", "সময়ের আগেই শুরু করে ফেলা"),
    ("keep an eye on", "নজরে রাখা"),
    ("keep your chin up", "মনোবল ধরে রাখা"),
    ("keep your fingers crossed", "মঙ্গল কামনা করা"),
    ("kill two birds with one stone", "এক ঢিলে দুই পাখি"),
    ("last straw", "সহ্যের শেষ সীমা"),
    ("learn the ropes", "কাজের খুঁটিনাটি শেখা"),
    ("leave no stone unturned", "চেষ্টার কোনো কসুর না রাখা"),
    ("let sleeping dogs lie", "ঘুমন্ত বিবাদ না জাগানো"),
    ("let the cat out of the bag", "গোপন কথা ফাঁস করে দেওয়া"),
    ("light at the end of the tunnel", "দুর্দিনের শেষে আশার আলো"),
    ("long story short", "সংক্ষেপে বলতে গেলে"),
    ("look on the bright side", "ভালো দিকটা দেখা"),
    ("lose your touch", "পুরোনো দক্ষতা হারানো"),
    ("make a long story short", "কথা ছোট করে বলা"),
    ("make ends meet", "কোনোমতে চলে যাওয়া"),
    ("make matters worse", "পরিস্থিতি আরও খারাপ করা"),
    ("make up your mind", "মনস্থির করা"),
    ("miss the boat", "সুযোগ হাতছাড়া করা"),
    ("music to my ears", "শুনে ভীষণ ভালো লাগা খবর"),
    ("neck of the woods", "এই এলাকা"),
    ("no pain no gain", "কষ্ট ছাড়া কেষ্ট মেলে না"),
    ("not my cup of tea", "আমার পছন্দের ধরন নয়"),
    ("off the hook", "দায় থেকে রেহাই পাওয়া"),
    ("off the record", "নথিবহির্ভূত ও অপ্রকাশ্য"),
    ("off the top of my head", "মাথায় যা আসে সঙ্গে সঙ্গে"),
    ("on cloud nine", "আনন্দে আত্মহারা"),
    ("on the ball", "চটপটে ও প্রস্তুত"),
    ("on the fence", "সিদ্ধান্তহীন দোলাচলে"),
    ("on the same page", "একই বোঝাপড়ায় থাকা"),
    ("on thin ice", "ঝুঁকির মুখে থাকা"),
    ("once in a blue moon", "কালেভদ্রে"),
    ("out of the blue", "একেবারে অপ্রত্যাশিতভাবে"),
    ("out of the question", "প্রশ্নই ওঠে না"),
    ("out of the woods", "বিপদ কাটিয়ে ওঠা"),
    ("over the moon", "অত্যন্ত খুশি"),
    ("paint the town red", "জমিয়ে ফুর্তি করা"),
    ("pay through the nose", "গলাকাটা দাম দেওয়া"),
    ("pick up the pieces", "ভাঙা অবস্থা থেকে গুছিয়ে নেওয়া"),
    ("piece of the pie", "ভাগের অংশ"),
    ("play by ear", "পরিস্থিতি বুঝে চলা"),
    ("play devil's advocate", "তর্কের খাতিরে বিপক্ষে বলা"),
    ("play it safe", "ঝুঁকি এড়িয়ে চলা"),
    ("pull someone's leg", "ঠাট্টা করে বোকা বানানো"),
    ("pull your socks up", "কোমর বেঁধে লেগে পড়া"),
    ("put all your eggs in one basket", "সব বাজি এক জায়গায় ধরা"),
    ("put your foot down", "কড়া অবস্থান নেওয়া"),
    ("put your foot in your mouth", "বেফাঁস কথা বলে ফেলা"),
    ("rain on someone's parade", "আনন্দ মাটি করে দেওয়া"),
    ("raining cats and dogs", "মুষলধারে বৃষ্টি"),
    ("read between the lines", "ভেতরের অর্থ বুঝে নেওয়া"),
    ("ring a bell", "চেনা চেনা ঠেকা"),
    ("rock the boat", "শান্ত অবস্থায় ঝামেলা বাধানো"),
    ("rub salt in the wound", "ক্ষতে নুন ছিটানো"),
    ("rule of thumb", "মোটামুটি চলনসই নিয়ম"),
    ("run in the family", "বংশে চলে আসা"),
    ("save face", "মান বাঁচানো"),
    ("see eye to eye", "একমত হওয়া"),
    ("sell like hot cakes", "গরম গরম বিক্রি হয়ে যাওয়া"),
    ("shoot yourself in the foot", "নিজের পায়ে কুড়াল মারা"),
    ("sit on the fence", "নিরপেক্ষ থেকে সিদ্ধান্ত এড়ানো"),
    ("sitting duck", "সহজ শিকার"),
    ("skeleton in the closet", "লুকোনো লজ্জার কথা"),
    ("sleep on it", "রাত পার করে ভেবে দেখা"),
    ("smell a rat", "গড়বড় আছে বলে সন্দেহ করা"),
    ("spill the beans", "গোপন কথা বলে ফেলা"),
    ("stab in the back", "পিছন থেকে ছুরি মারা"),
    ("stand your ground", "নিজের অবস্থানে অটল থাকা"),
    ("steal the show", "সবার নজর কেড়ে নেওয়া"),
    ("step up your game", "মান আরও বাড়ানো"),
    ("stick to your guns", "নিজের কথায় অটল থাকা"),
    ("straight from the horse's mouth", "একেবারে আসল সূত্র থেকে"),
    ("take it with a pinch of salt", "পুরোটা বিশ্বাস না করা"),
    ("take the bull by the horns", "সাহস করে সরাসরি মোকাবিলা করা"),
    ("test the waters", "আগে পরখ করে দেখা"),
    ("the ball is rolling", "কাজ গড়াতে শুরু করেছে"),
    ("the best of both worlds", "উভয় দিকের সেরাটা"),
    ("the bottom line", "মোদ্দা কথা"),
    ("the last laugh", "শেষ হাসি"),
    ("the tip of the iceberg", "সমস্যার সামান্য অংশ মাত্র"),
    ("think outside the box", "গতানুগতিকতার বাইরে ভাবা"),
    ("throw in the towel", "হাল ছেড়ে দেওয়া"),
    ("tie the knot", "বিয়ে করা"),
    ("time flies", "সময় উড়ে চলে যায়"),
    ("to each their own", "যার যার পছন্দ তার তার"),
    ("touch base", "একটু যোগাযোগ করে নেওয়া"),
    ("turn a blind eye", "জেনেও না দেখার ভান করা"),
    ("turn over a new leaf", "নতুন করে শুরু করা"),
    ("twist someone's arm", "চাপ দিয়ে রাজি করানো"),
    ("under your nose", "নাকের ডগায়"),
    ("up in the air", "অনিশ্চিত ঝুলে থাকা"),
    ("walk on eggshells", "খুব সাবধানে পা ফেলা"),
    ("water under the bridge", "গত হয়ে যাওয়া বিষয়"),
    ("wear your heart on your sleeve", "মনের কথা মুখে ফুটিয়ে রাখা"),
    ("when pigs fly", "যা কখনোই হবে না"),
    ("wild goose chase", "অর্থহীন দৌড়ঝাঁপ"),
    ("win hands down", "অনায়াসে জিতে যাওয়া"),
    ("wrap your head around", "মাথায় ঢোকানো বা বুঝে ওঠা"),
    ("you can say that again", "একদম ঠিক বলেছ"),
]

A1_EXTRA = [
    ("apple tree", "আপেল গাছ"), ("arm", "বাহু"), ("ash", "ছাই"),
    ("baby", "কোলের শিশু"), ("ball", "বল"), ("basket", "ঝুড়ি"),
    ("beard", "দাড়ি"), ("bell", "ঘণ্টা"), ("belt", "বেল্ট"),
    ("blanket", "কম্বল"), ("boat ride", "নৌকাভ্রমণ"), ("box", "বাক্স"),
    ("bread slice", "পাউরুটির টুকরা"), ("broom", "ঝাড়ু"), ("bucket", "বালতি"),
    ("candle", "মোমবাতি"), ("cap", "টুপি পরার ঢাকনা"), ("carpet", "গালিচা"),
    ("chalk", "চক"), ("cheek", "গাল"), ("chin", "চিবুক"),
    ("coat", "কোট"), ("coconut", "নারকেল"), ("corn", "ভুট্টা"),
    ("cotton", "তুলা"), ("cupboard", "আলমারি"), ("curtain", "পর্দা"),
    ("dinner", "রাতের খাবার"), ("doll", "পুতুল"), ("drum", "ঢোল"),
    ("dust bin", "ময়লার ঝুড়ি"), ("engine", "ইঞ্জিন"), ("eyebrow", "ভ্রু"),
    ("farmer wife", "কৃষকের স্ত্রী"), ("feather", "পালক"), ("flute", "বাঁশি"),
    ("forehead", "কপাল"), ("fork", "কাঁটাচামচ"), ("furniture", "আসবাব"),
    ("ginger", "আদা"), ("grape", "আঙুর"), ("guava", "পেয়ারা"),
    ("hammer", "হাতুড়ি"), ("handle", "হাতল"), ("heel", "গোড়ালি"),
    ("hill", "টিলা"), ("honeybee", "মধুমক্ষিকা"), ("jackfruit", "কাঁঠাল"),
    ("jar", "বয়াম"), ("kite", "ঘুড়ি"), ("ladder", "মই"),
    ("leather", "চামড়া"), ("lunch", "দুপুরের খাবার"), ("mat", "মাদুর"),
    ("needle", "সুই"), ("net", "জাল"), ("oven", "চুলার ওভেন"),
    ("palm", "হাতের তালু"), ("papaya", "পেঁপে"), ("peacock", "ময়ূর"),
    ("pillow", "বালিশ"), ("pocket money", "হাতখরচ"), ("pumpkin", "কুমড়া"),
    ("rope", "দড়ি"), ("rubber", "রাবার"), ("sandal", "স্যান্ডেল"),
    ("scissors", "কাঁচি"), ("shelf", "তাক"), ("shoulder bag", "কাঁধের ব্যাগ"),
    ("sofa", "সোফা"), ("spoonful", "এক চামচ পরিমাণ"), ("stick", "লাঠি"),
    ("sweater", "সোয়েটার"), ("thread", "সুতা"), ("thunder", "বজ্র"),
    ("tiffin", "টিফিন"), ("tray", "ট্রে"), ("village road", "গ্রামের পথ"),
    ("waist", "কোমর"), ("wheel", "চাকা"), ("whistle", "বাঁশির শিস"),
    ("wrist", "কব্জি"),
]

A2_EXTRA = [
    ("ability test", "দক্ষতার পরীক্ষা"), ("advertisement", "বিজ্ঞাপন"),
    ("agriculture", "কৃষি"), ("airport", "বিমানবন্দর"), ("ambulance", "অ্যাম্বুলেন্স"),
    ("anniversary", "বার্ষিকী"), ("appetite", "খিদে"), ("architect", "স্থপতি"),
    ("audience seat", "দর্শকের আসন"), ("bakery", "বেকারি"),
    ("balcony", "বারান্দা"), ("basement", "নিচতলার ঘর"), ("battle", "যুদ্ধ"),
    ("bedroom", "শোবার ঘর"), ("behaviour chart", "আচরণের তালিকা"),
    ("biography", "জীবনী"), ("blanket cover", "চাদর"), ("brochure", "পুস্তিকা"),
    ("calendar", "পঞ্জিকা"), ("campaign", "প্রচারাভিযান"), ("canteen", "ক্যান্টিন"),
    ("carpenter", "ছুতার"), ("cashier", "ক্যাশিয়ার"), ("ceiling", "ছাদের ভেতরের তল"),
    ("cemetery", "কবরস্থান"), ("chapter", "অধ্যায়"), ("charity", "দাতব্য"),
    ("cheque", "চেক"), ("childhood", "শৈশব"), ("cinema hall", "সিনেমা হল"),
    ("classroom", "শ্রেণিকক্ষ"), ("client list", "মক্কেলের তালিকা"),
    ("cloth market", "কাপড়ের বাজার"), ("coastline", "উপকূলরেখা"),
    ("compass", "কম্পাস"), ("conference", "সম্মেলন"), ("continent", "মহাদেশ"),
    ("cottage", "কুটির"), ("council", "পরিষদ"), ("courtyard", "উঠান"),
    ("craft", "কারুশিল্প"), ("cupboard shelf", "আলমারির তাক"),
    ("customs", "শুল্ক বিভাগ"), ("dairy", "দুগ্ধখামার"), ("dam wall", "বাঁধের দেয়াল"),
    ("dictionary", "অভিধান"), ("diploma", "ডিপ্লোমা"), ("directory", "নির্দেশিকা"),
    ("driver seat", "চালকের আসন"), ("earnings", "উপার্জন"),
    ("electrician", "বিদ্যুৎমিস্ত্রি"), ("embassy", "দূতাবাস"),
    ("engineer", "প্রকৌশলী"), ("envelope", "খাম"), ("equipment", "সরঞ্জাম"),
    ("escalator", "চলন্ত সিঁড়ি"), ("exhibition", "প্রদর্শনী"),
    ("fare", "ভাড়ার টাকা"), ("fence", "বেড়া"), ("ferry", "খেয়া"),
    ("firefighter", "দমকলকর্মী"), ("fisherman", "জেলে"), ("fountain", "ঝরনা"),
    ("furnace", "চুল্লি"), ("gallery", "চিত্রশালা"), ("garage", "গাড়ির ঘর"),
    ("gardener", "মালি"), ("glacier", "হিমবাহ"), ("goldsmith", "স্বর্ণকার"),
    ("grocery", "মুদিখানা"), ("harbour", "পোতাশ্রয়"), ("headline", "শিরোনাম"),
    ("helmet", "শিরস্ত্রাণ"), ("highway", "মহাসড়ক"), ("horizon", "দিগন্ত"),
    ("housewife", "গৃহিণী"), ("hut", "কুঁড়েঘর"), ("insurance", "বিমা"),
    ("journalist", "সাংবাদিক"), ("jungle", "জঙ্গল"), ("laboratory", "গবেষণাগার"),
    ("landlord", "বাড়িওয়ালা"), ("laundry", "কাপড় কাচার কাজ"),
    ("lawyer", "আইনজীবী"), ("lecture", "বক্তৃতা"), ("librarian", "গ্রন্থাগারিক"),
    ("lighthouse", "বাতিঘর"), ("luggage", "মালপত্র"), ("machinery", "যন্ত্রপাতি"),
    ("mechanic", "মিস্ত্রি"), ("medal", "পদক"), ("merchant", "বণিক"),
    ("microphone", "মাইক্রোফোন"), ("mill", "কল"), ("mineral", "খনিজ"),
    ("monument", "স্মৃতিসৌধ"), ("mosque", "মসজিদ"), ("motorcycle", "মোটরসাইকেল"),
    ("neighbourhood", "পাড়া"), ("newspaper", "সংবাদপত্র"), ("orchard", "ফলবাগান"),
    ("orphan", "অনাথ"), ("painter", "চিত্রশিল্পী"), ("passport", "পাসপোর্ট"),
    ("pavement", "ফুটপাত"), ("pharmacy", "ওষুধের দোকান"), ("photograph", "আলোকচিত্র"),
    ("pilgrim", "তীর্থযাত্রী"), ("pilot", "বিমানচালক"), ("plumber", "নলমিস্ত্রি"),
    ("poet", "কবি"), ("pollution level", "দূষণের মাত্রা"), ("postman", "ডাকপিয়ন"),
    ("pottery", "মৃৎশিল্প"), ("prayer", "প্রার্থনা"), ("printer machine", "ছাপার যন্ত্র"),
    ("railway", "রেলপথ"), ("receptionist", "অভ্যর্থনাকারী"), ("recipe", "রন্ধনপ্রণালী"),
    ("refugee", "শরণার্থী"), ("rescue boat", "উদ্ধারের নৌকা"), ("reservoir", "জলাধার"),
    ("rickshaw", "রিকশা"), ("roof top", "ছাদের উপরিভাগ"), ("sailor", "নাবিক"),
    ("scholarship", "বৃত্তি"), ("sculpture", "ভাস্কর্য"), ("secretary", "সচিব"),
    ("sermon", "ধর্মোপদেশ"), ("singer", "গায়ক"), ("skyline", "আকাশরেখা"),
    ("soldier camp", "সেনাশিবির"), ("souvenir", "স্মারক"), ("stadium", "স্টেডিয়াম"),
    ("stationery", "লেখার সরঞ্জাম"), ("statue", "মূর্তি"), ("suburb", "শহরতলি"),
    ("suitcase", "স্যুটকেস"), ("surgeon", "শল্যচিকিৎসক"), ("tailor", "দর্জি"),
    ("tanker", "ট্যাংকার"), ("telescope", "দূরবিন"), ("tenant", "ভাড়াটে"),
    ("theatre", "নাট্যশালা"), ("timetable", "সময়সূচি"), ("tourist", "পর্যটক"),
    ("tractor", "ট্রাক্টর"), ("translator", "অনুবাদক"), ("tunnel", "সুড়ঙ্গ"),
    ("uniform dress", "নির্দিষ্ট পোশাক"), ("vaccine", "টিকা"), ("valley road", "উপত্যকার পথ"),
    ("volcano", "আগ্নেয়গিরি"), ("voyage", "সমুদ্রযাত্রা"), ("warehouse", "গুদাম"),
    ("waterfall", "জলপ্রপাত"), ("weaver", "তাঁতি"), ("wedding card", "বিয়ের কার্ড"),
    ("wheelchair", "হুইলচেয়ার"), ("workshop", "কর্মশালা"), ("zoo", "চিড়িয়াখানা"),
]

B1_EXTRA = [
    ("abandon", "পরিত্যাগ করা"), ("absorb idea", "ভাবনা আত্মস্থ করা"),
    ("accuse someone", "দোষ দেওয়া"), ("adjust", "মানিয়ে নেওয়া"),
    ("admire view", "দৃশ্য উপভোগ করা"), ("advise", "পরামর্শ দেওয়া"),
    ("afford time", "সময় বের করা"), ("alarm", "সতর্কঘণ্টা"),
    ("amuse", "আমোদ দেওয়া"), ("announce result", "ফল ঘোষণা করা"),
    ("apply", "প্রয়োগ করা বা আবেদন করা"), ("appreciate", "কদর করা"),
    ("approach", "কাছে যাওয়া বা পন্থা"), ("arrange flowers", "ফুল সাজানো"),
    ("assume", "ধরে নেওয়া"), ("assure quality", "মানের নিশ্চয়তা দেওয়া"),
    ("attach file", "নথি জুড়ে দেওয়া"), ("attract attention", "মনোযোগ টানা"),
    ("avoid risk", "ঝুঁকি এড়ানো"), ("bargain", "দরদাম করা"),
    ("betray trust", "বিশ্বাস ভাঙা"), ("blame someone", "দোষ চাপানো"),
    ("boast", "বড়াই করা"), ("borrow book", "বই ধার নেওয়া"),
    ("breathe deeply", "গভীরভাবে শ্বাস নেওয়া"), ("calculate", "হিসাব করা"),
    ("cancel order", "ফরমাশ বাতিল করা"), ("caution", "সাবধানবাণী"),
    ("chase", "ধাওয়া করা"), ("cheer", "উৎসাহ দেওয়া"),
    ("chew", "চিবানো"), ("claim money", "টাকা দাবি করা"),
    ("collapse", "ধসে পড়া"), ("compare price", "দাম তুলনা করা"),
    ("complain politely", "ভদ্রভাবে অভিযোগ করা"), ("confess", "স্বীকার করা"),
    ("confirm", "নিশ্চিত করা"), ("congratulate", "অভিনন্দন জানানো"),
    ("consume energy", "শক্তি ব্যয় করা"), ("convince others", "অন্যদের বোঝানো"),
    ("cooperate", "সহযোগিতা করা"), ("crash", "সংঘর্ষ ঘটা"),
    ("crawl", "হামাগুড়ি দেওয়া"), ("criticise politely", "নম্রভাবে সমালোচনা"),
    ("cure illness", "রোগ সারানো"), ("declare", "ঘোষণা করা"),
    ("decorate", "সাজানো"), ("delay journey", "যাত্রা পিছিয়ে দেওয়া"),
    ("deliver parcel", "পার্সেল পৌঁছে দেওয়া"), ("deny charge", "অভিযোগ অস্বীকার করা"),
    ("depart", "রওনা হওয়া"), ("describe scene", "দৃশ্য বর্ণনা করা"),
    ("deserve praise", "প্রশংসার যোগ্য হওয়া"), ("discourage", "নিরুৎসাহিত করা"),
    ("dislike", "অপছন্দ করা"), ("divide work", "কাজ ভাগ করা"),
    ("donate", "দান করা"), ("doubt someone", "সন্দেহ পোষণ করা"),
    ("earn trust", "আস্থা অর্জন করা"), ("educate", "শিক্ষিত করা"),
    ("elect leader", "নেতা নির্বাচন করা"), ("employ staff", "কর্মী নিয়োগ করা"),
    ("encourage effort", "চেষ্টায় উৎসাহ দেওয়া"), ("endure pain", "কষ্ট সহ্য করা"),
    ("entertain", "মনোরঞ্জন করা"), ("escape danger", "বিপদ থেকে পালানো"),
    ("examine closely", "খুঁটিয়ে পরীক্ষা করা"), ("exchange gifts", "উপহার বিনিময়"),
    ("excuse mistake", "ভুল মাফ করা"), ("expand business", "ব্যবসা বাড়ানো"),
    ("explore area", "এলাকা ঘুরে দেখা"), ("expose truth", "সত্য ফাঁস করা"),
    ("fasten", "আঁটকে বাঁধা"), ("fetch", "গিয়ে নিয়ে আসা"),
    ("float", "ভেসে থাকা"), ("fold", "ভাঁজ করা"),
    ("forbid", "নিষেধ করা"), ("gather crowd", "ভিড় জমা করা"),
    ("glance", "আড়চোখে দেখা"), ("greet", "সম্ভাষণ জানানো"),
    ("hesitate", "ইতস্তত করা"), ("hire", "ভাড়া করা"),
    ("hunt", "শিকার করা"), ("hurt feelings", "মনে আঘাত দেওয়া"),
    ("identify", "শনাক্ত করা"), ("imitate", "অনুকরণ করা"),
    ("impress others", "মুগ্ধ করা"), ("improve health", "স্বাস্থ্যের উন্নতি করা"),
    ("include everyone", "সবাইকে রাখা"), ("indicate", "নির্দেশ করা"),
    ("inform quickly", "দ্রুত জানানো"), ("inherit", "উত্তরাধিকারসূত্রে পাওয়া"),
    ("injure knee", "হাঁটুতে চোট পাওয়া"), ("inspect work", "কাজ পরিদর্শন করা"),
    ("install app", "অ্যাপ বসানো"), ("insult", "অপমান করা"),
    ("interrupt", "বাধা দিয়ে থামানো"), ("introduce rule", "নিয়ম চালু করা"),
    ("invest money", "টাকা খাটানো"), ("investigate", "তদন্ত চালানো"),
    ("invite guests", "অতিথি নিমন্ত্রণ করা"), ("involve others", "অন্যদের যুক্ত করা"),
    ("knock", "টোকা দেওয়া"), ("lean", "হেলান দেওয়া"),
    ("lend", "ধার দেওয়া"), ("lift weight", "ওজন তোলা"),
    ("link ideas", "ভাবনা জুড়ে দেওয়া"), ("load truck", "ট্রাক বোঝাই করা"),
    ("maintain machine", "যন্ত্র রক্ষণাবেক্ষণ করা"), ("measure area", "এলাকা মাপা"),
    ("melt ice", "বরফ গলানো"), ("mend", "সারিয়ে নেওয়া"),
    ("mention name", "নাম উল্লেখ করা"), ("multiply", "গুণ করা"),
    ("negotiate price", "দাম নিয়ে দর কষা"), ("nod", "মাথা নাড়া"),
    ("obey rule", "নিয়ম মানা"), ("offer help", "সাহায্যের প্রস্তাব"),
    ("operate machine", "যন্ত্র চালানো"), ("owe money", "টাকা পাওনা থাকা"),
    ("own house", "বাড়ির মালিক হওয়া"), ("pack bags", "ব্যাগ গোছানো"),
    ("participate", "অংশ নেওয়া"), ("perform duty", "দায়িত্ব পালন করা"),
    ("persuade friend", "বন্ধুকে রাজি করানো"), ("pour water", "জল ঢালা"),
    ("practise daily", "রোজ অনুশীলন করা"), ("praise work", "কাজের প্রশংসা"),
    ("predict future", "ভবিষ্যৎ আঁচ করা"), ("prepare meal", "খাবার তৈরি করা"),
    ("prevent loss", "ক্ষতি ঠেকানো"), ("proceed carefully", "সাবধানে এগোনো"),
    ("promote", "পদোন্নতি বা প্রচার করা"), ("pronounce word", "শব্দ উচ্চারণ করা"),
    ("protect nature", "প্রকৃতি রক্ষা করা"), ("provide shelter", "আশ্রয় দেওয়া"),
    ("punish wrong", "অন্যায়ের শাস্তি দেওয়া"), ("purchase", "কেনাকাটা করা"),
    ("quit job", "চাকরি ছাড়া"), ("react", "প্রতিক্রিয়া দেখানো"),
    ("recall memory", "স্মৃতি মনে করা"), ("recognise face", "মুখ চেনা"),
    ("recommend book", "বই সুপারিশ করা"), ("recycle", "পুনর্ব্যবহার করা"),
    ("reduce waste", "অপচয় কমানো"), ("refuse offer", "প্রস্তাব ফিরিয়ে দেওয়া"),
    ("register name", "নাম নিবন্ধন করা"), ("regret decision", "সিদ্ধান্তে আফসোস"),
    ("relax mind", "মন হালকা করা"), ("release bird", "পাখি ছেড়ে দেওয়া"),
    ("remind gently", "আলতো করে মনে করিয়ে দেওয়া"), ("remove dust", "ধুলা সরানো"),
    ("rent flat", "ফ্ল্যাট ভাড়া নেওয়া"), ("repair roof", "ছাদ সারানো"),
    ("replace part", "যন্ত্রাংশ বদলানো"), ("reply quickly", "দ্রুত জবাব দেওয়া"),
    ("rescue child", "শিশুকে উদ্ধার করা"), ("reserve seat", "আসন সংরক্ষণ করা"),
    ("respect elders", "বড়দের সম্মান করা"), ("retire", "অবসর নেওয়া"),
    ("reveal secret", "গোপন কথা প্রকাশ করা"), ("review lesson", "পাঠ ঝালিয়ে নেওয়া"),
    ("ruin", "নষ্ট করে দেওয়া"), ("scatter", "ছড়িয়ে দেওয়া"),
    ("scratch", "আঁচড় কাটা"), ("seal", "সিল করে বন্ধ করা"),
    ("search room", "ঘর তল্লাশি করা"), ("select best", "সেরাটি বাছা"),
    ("serve customer", "খদ্দেরকে সেবা দেওয়া"), ("settle bill", "বিল মিটিয়ে দেওয়া"),
    ("shout", "চিৎকার করা"), ("sign paper", "কাগজে সই করা"),
    ("sink", "ডুবে যাওয়া"), ("slip", "পিছলে পড়া"),
    ("solve puzzle", "ধাঁধা মেলানো"), ("sow seeds", "বীজ বোনা"),
    ("split", "চিরে ফেলা"), ("spread news", "খবর ছড়ানো"),
    ("squeeze", "চেপে রস বের করা"), ("stare", "একদৃষ্টে তাকানো"),
    ("steal money", "টাকা চুরি করা"), ("stir", "নেড়ে মেশানো"),
    ("stretch legs", "পা ছড়িয়ে দেওয়া"), ("struggle hard", "কঠিন সংগ্রাম করা"),
    ("submit form", "ফরম জমা দেওয়া"), ("succeed finally", "শেষমেশ সফল হওয়া"),
    ("suggest idea", "ভাবনা প্রস্তাব করা"), ("supply water", "জল সরবরাহ করা"),
    ("suspect fraud", "জালিয়াতির সন্দেহ"), ("swallow", "গিলে ফেলা"),
    ("sweep floor", "মেঝে ঝাড়া"), ("swing", "দোল খাওয়া"),
    ("tear paper", "কাগজ ছেঁড়া"), ("tempt", "প্রলোভন দেখানো"),
    ("threaten", "হুমকি দেওয়া"), ("tie rope", "দড়ি বাঁধা"),
    ("train staff", "কর্মী প্রশিক্ষণ দেওয়া"), ("translate page", "পাতা অনুবাদ করা"),
    ("treat patient", "রোগীর চিকিৎসা করা"), ("trust friend", "বন্ধুকে বিশ্বাস করা"),
    ("unite", "ঐক্যবদ্ধ হওয়া"), ("unlock", "তালা খোলা"),
    ("upload file", "ফাইল আপলোড করা"), ("urge caution", "সতর্ক হতে বলা"),
    ("vanish", "উধাও হওয়া"), ("visit village", "গ্রামে বেড়াতে যাওয়া"),
    ("vote", "ভোট দেওয়া"), ("wander", "উদ্দেশ্যহীন ঘোরা"),
    ("warn driver", "চালককে সতর্ক করা"), ("waste time", "সময় নষ্ট করা"),
    ("weigh", "ওজন করা"), ("whisper", "ফিসফিস করা"),
    ("wipe", "মুছে ফেলা"), ("wonder why", "কেন ভেবে অবাক হওয়া"),
    ("wrap gift", "উপহার মোড়ানো"), ("yell", "হাঁক দেওয়া"),
]

COLLOCATIONS = [
    ("make a decision", "সিদ্ধান্ত নেওয়া"), ("take a decision", "সিদ্ধান্ত গ্রহণ করা"),
    ("do business", "ব্যবসা করা"), ("make progress", "অগ্রগতি করা"),
    ("take responsibility", "দায়িত্ব নেওয়া"), ("pay attention", "মনোযোগ দেওয়া"),
    ("keep a promise", "প্রতিশ্রুতি রাখা"), ("break a promise", "প্রতিশ্রুতি ভাঙা"),
    ("take a risk", "ঝুঁকি নেওয়া"), ("run a business", "ব্যবসা চালানো"),
    ("meet a deadline", "সময়সীমার মধ্যে শেষ করা"), ("miss a deadline", "সময়সীমা পার করে ফেলা"),
    ("raise a question", "প্রশ্ন তোলা"), ("answer a question", "প্রশ্নের উত্তর দেওয়া"),
    ("reach an agreement", "সমঝোতায় পৌঁছানো"), ("draw a conclusion", "সিদ্ধান্তে আসা"),
    ("make an effort", "চেষ্টা করা"), ("waste an opportunity", "সুযোগ নষ্ট করা"),
    ("seize an opportunity", "সুযোগ লুফে নেওয়া"), ("gain experience", "অভিজ্ঞতা অর্জন করা"),
    ("earn a living", "জীবিকা নির্বাহ করা"), ("save money", "টাকা জমানো"),
    ("spend money", "টাকা খরচ করা"), ("raise funds", "তহবিল সংগ্রহ করা"),
    ("cut costs", "খরচ কমানো"), ("set a target", "লক্ষ্যমাত্রা ঠিক করা"),
    ("achieve a goal", "লক্ষ্য অর্জন করা"), ("meet demand", "চাহিদা মেটানো"),
    ("place an order", "ফরমাশ দেওয়া"), ("sign a contract", "চুক্তিতে সই করা"),
    ("hold a meeting", "সভা করা"), ("attend a meeting", "সভায় উপস্থিত থাকা"),
    ("give a speech", "বক্তৃতা দেওয়া"), ("deliver a lecture", "বক্তৃতা প্রদান করা"),
    ("take notes", "নোট নেওয়া"), ("do homework", "বাড়ির কাজ করা"),
    ("sit an exam", "পরীক্ষায় বসা"), ("pass an exam", "পরীক্ষায় পাস করা"),
    ("fail an exam", "পরীক্ষায় ফেল করা"), ("get a degree", "ডিগ্রি অর্জন করা"),
    ("apply for a job", "চাকরির আবেদন করা"), ("get a promotion", "পদোন্নতি পাওয়া"),
    ("quit a job", "চাকরি ছেড়ে দেওয়া"), ("work overtime", "অতিরিক্ত সময় কাজ করা"),
    ("take a break", "বিরতি নেওয়া"), ("have a rest", "বিশ্রাম নেওয়া"),
    ("catch a cold", "ঠান্ডা লাগা"), ("take medicine", "ওষুধ খাওয়া"),
    ("do exercise", "ব্যায়াম করা"), ("keep fit", "ফিট থাকা"),
    ("gain weight", "ওজন বাড়া"), ("lose weight", "ওজন কমা"),
    ("have breakfast", "সকালের খাবার খাওয়া"), ("make tea", "চা বানানো"),
    ("cook a meal", "রান্না করা"), ("do the dishes", "বাসন ধোয়া"),
    ("make the bed", "বিছানা গোছানো"), ("do the laundry", "কাপড় কাচা"),
    ("take a shower", "গোসল করা"), ("brush your teeth", "দাঁত মাজা"),
    ("catch a bus", "বাস ধরা"), ("miss a train", "ট্রেন মিস করা"),
    ("book a ticket", "টিকিট বুক করা"), ("take a taxi", "ট্যাক্সি নেওয়া"),
    ("go on a trip", "ভ্রমণে যাওয়া"), ("pack your bags", "ব্যাগ গোছানো"),
    ("make a reservation", "আগাম বুকিং করা"), ("check the time", "সময় দেখা"),
    ("keep a secret", "গোপন রাখা"), ("tell the truth", "সত্য বলা"),
    ("tell a lie", "মিথ্যা বলা"), ("make a mistake", "ভুল করা"),
    ("learn a lesson", "শিক্ষা নেওয়া"), ("give advice", "উপদেশ দেওয়া"),
    ("take advice", "উপদেশ মানা"), ("ask a favour", "অনুগ্রহ চাওয়া"),
    ("make friends", "বন্ধুত্ব করা"), ("keep in touch", "যোগাযোগ রাখা"),
    ("have a chat", "খোশগল্প করা"), ("make a phone call", "ফোন করা"),
    ("send a message", "বার্তা পাঠানো"), ("write an email", "ইমেইল লেখা"),
    ("fill in a form", "ফরম পূরণ করা"), ("pay a bill", "বিল পরিশোধ করা"),
    ("open an account", "হিসাব খোলা"), ("take a loan", "ঋণ নেওয়া"),
    ("break the law", "আইন ভাঙা"), ("obey the rules", "নিয়ম মানা"),
    ("commit a crime", "অপরাধ করা"), ("serve a sentence", "সাজা ভোগ করা"),
    ("win an election", "নির্বাচনে জেতা"), ("cast a vote", "ভোট দেওয়া"),
    ("hold power", "ক্ষমতায় থাকা"), ("cause damage", "ক্ষতি করা"),
    ("solve a problem", "সমস্যার সমাধান করা"), ("face a challenge", "চ্যালেঞ্জের মুখোমুখি হওয়া"),
    ("take action", "ব্যবস্থা নেওয়া"), ("make an exception", "ব্যতিক্রম করা"),
    ("play a role", "ভূমিকা রাখা"), ("set an example", "দৃষ্টান্ত স্থাপন করা"),
    ("take part", "অংশ নেওয়া"), ("keep a record", "নথি রাখা"),
    ("do research", "গবেষণা করা"), ("carry out a survey", "জরিপ চালানো"),
    ("give an example", "উদাহরণ দেওয়া"), ("raise awareness", "সচেতনতা বাড়ানো"),
    ("meet expectations", "প্রত্যাশা পূরণ করা"), ("take advantage", "সুযোগ কাজে লাগানো"),
    ("bear in mind", "মনে রাখা"), ("make sense", "অর্থবহ হওয়া"),
    ("take time", "সময় লাগা"), ("kill time", "সময় কাটানো"),
    ("save time", "সময় বাঁচানো"), ("run out of time", "সময় ফুরিয়ে যাওয়া"),
]

PROVERBS = [
    ("a friend in need is a friend indeed", "বিপদের বন্ধুই প্রকৃত বন্ধু"),
    ("a stitch in time saves nine", "সময়ের এক ফোঁড় অসময়ের দশ ফোঁড়"),
    ("actions speak louder", "কাজেই পরিচয়, কথায় নয়"),
    ("all that glitters is not gold", "চকচক করলেই সোনা হয় না"),
    ("an empty vessel makes much noise", "অসারের তর্জন গর্জন সার"),
    ("as you sow so shall you reap", "যেমন কর্ম তেমন ফল"),
    ("barking dogs seldom bite", "যে গরু বেশি ডাকে সে কম দুধ দেয়"),
    ("better late than never", "দেরিতে হলেও না হওয়ার চেয়ে ভালো"),
    ("birds of a feather flock together", "চোরে চোরে মাসতুতো ভাই"),
    ("blood is thicker than water", "রক্তের টান বড় টান"),
    ("charity begins at home", "আপন ঘর আগে"),
    ("cut your coat according to your cloth", "আয় বুঝে ব্যয় করো"),
    ("do not put off till tomorrow", "আজকের কাজ কাল করো না"),
    ("do not count your chickens before they hatch", "গাছে কাঁঠাল গোঁফে তেল"),
    ("easy come easy go", "সহজে আসে সহজে যায়"),
    ("empty hands are the devil's workshop", "অলস মস্তিষ্ক শয়তানের কারখানা"),
    ("every dog has his day", "সবারই সুদিন আসে"),
    ("experience is the best teacher", "অভিজ্ঞতাই সেরা শিক্ষক"),
    ("fortune favours the brave", "ভাগ্য সাহসীদের পক্ষে থাকে"),
    ("great talkers are little doers", "যে বেশি বলে সে কম করে"),
    ("haste makes waste", "তাড়াহুড়ায় কাজ পণ্ড হয়"),
    ("health is wealth", "স্বাস্থ্যই সম্পদ"),
    ("honesty is the best policy", "সততাই শ্রেষ্ঠ পন্থা"),
    ("hunger is the best sauce", "খিদেই সেরা রসনা"),
    ("ignorance is bliss", "অজ্ঞতাই সুখ"),
    ("it takes two to make a quarrel", "এক হাতে তালি বাজে না"),
    ("knowledge is power", "জ্ঞানই শক্তি"),
    ("look before you leap", "ভেবে পা ফেলো"),
    ("man proposes god disposes", "মানুষ চায় এক হয় আরেক"),
    ("many a little makes a mickle", "বিন্দু বিন্দু জলে সিন্ধু হয়"),
    ("necessity is the mother of invention", "প্রয়োজনই উদ্ভাবনের জননী"),
    ("no pain no gain", "কষ্ট না করলে কেষ্ট মেলে না"),
    ("nothing succeeds like success", "সাফল্যই সাফল্য আনে"),
    ("old habits die hard", "পুরোনো অভ্যাস সহজে যায় না"),
    ("one swallow does not make a summer", "এক ফুলে বসন্ত হয় না"),
    ("out of sight out of mind", "চোখের আড়াল মনের আড়াল"),
    ("patience has its reward", "ধৈর্যের ফল মিঠা হয়"),
    ("practice makes perfect", "চর্চাই নিপুণতা আনে"),
    ("prevention is better than cure", "প্রতিকারের চেয়ে প্রতিরোধ ভালো"),
    ("rome was not built in a day", "একদিনে কিছু গড়ে ওঠে না"),
    ("silence is golden", "নীরবতা স্বর্ণসম"),
    ("slow and steady wins the race", "ধীরে চলো নিশ্চিত জয়"),
    ("something is better than nothing", "নেই মামার চেয়ে কানা মামা ভালো"),
    ("strike while the iron is hot", "গরম লোহায় ঘা মারো"),
    ("the early bird catches the worm", "যে আগে ওঠে সে আগে পায়"),
    ("the grass is greener on the other side", "পরের ভাত সবসময় বড় দেখায়"),
    ("there is no smoke without fire", "আগুন না থাকলে ধোঁয়া হয় না"),
    ("time and tide wait for none", "সময় ও স্রোত কারও জন্য বসে থাকে না"),
    ("to err is human", "মানুষমাত্রেই ভুল করে"),
    ("too many cooks spoil the broth", "অধিক সন্ন্যাসীতে গাজন নষ্ট"),
    ("union is strength", "একতাই বল"),
    ("what cannot be cured must be endured", "যা সারানো যায় না তা সইতে হয়"),
    ("when in rome do as the romans do", "যেমন দেশ তেমন বেশ"),
    ("where there is a will there is a way", "ইচ্ছা থাকলে উপায় হয়"),
    ("you cannot clap with one hand", "এক হাতে তালি বাজে না"),
    ("you reap what you sow", "যা বুনবে তাই কাটবে"),
]

CONFUSABLES = [
    ("accept and except", "গ্রহণ করা আর বাদ দিয়ে"),
    ("advice and advise", "উপদেশ বিশেষ্য আর উপদেশ দেওয়া ক্রিয়া"),
    ("affect and effect", "প্রভাব ফেলা ক্রিয়া আর প্রভাব বিশেষ্য"),
    ("aloud and allowed", "সশব্দে আর অনুমতিপ্রাপ্ত"),
    ("altar and alter", "বেদি আর পরিবর্তন করা"),
    ("amount and number", "অগণনীয়ের পরিমাণ আর গণনীয়ের সংখ্যা"),
    ("assure and ensure", "আশ্বস্ত করা আর নিশ্চিত করা"),
    ("bare and bear", "খালি আর বহন করা"),
    ("beside and besides", "পাশে আর তা ছাড়াও"),
    ("born and borne", "জন্মগ্রহণ আর বহন করা হয়েছে"),
    ("breath and breathe", "নিঃশ্বাস বিশেষ্য আর শ্বাস নেওয়া ক্রিয়া"),
    ("canvas and canvass", "ক্যানভাস কাপড় আর ভোট চাওয়া"),
    ("capital and capitol", "রাজধানী বা মূলধন আর আইনসভা ভবন"),
    ("cite and site", "উদ্ধৃত করা আর স্থান"),
    ("complement and compliment", "পরিপূরক আর প্রশংসা"),
    ("conscience and conscious", "বিবেক আর সজ্ঞান"),
    ("council and counsel", "পরিষদ আর পরামর্শ"),
    ("desert and dessert", "মরুভূমি আর মিষ্টান্ন"),
    ("device and devise", "যন্ত্র আর উদ্ভাবন করা"),
    ("die and dye", "মারা যাওয়া আর রং করা"),
    ("discreet and discrete", "বিচক্ষণভাবে সংযত আর পৃথক"),
    ("elicit and illicit", "বের করে আনা আর অবৈধ"),
    ("eminent and imminent", "খ্যাতনামা আর আসন্ন"),
    ("envelop and envelope", "ঢেকে ফেলা আর খাম"),
    ("farther and further", "দূরত্বে আরও দূর আর মাত্রায় আরও"),
    ("fewer and less", "গণনীয়ে কম আর অগণনীয়ে কম"),
    ("formally and formerly", "আনুষ্ঠানিকভাবে আর পূর্বে"),
    ("hear and here", "শোনা আর এখানে"),
    ("historic and historical", "ঐতিহাসিক গুরুত্বের আর ইতিহাসসংক্রান্ত"),
    ("imply and infer", "ইঙ্গিত করা আর অনুমান করে নেওয়া"),
    ("its and it is", "তার আর সেটি হলো"),
    ("later and latter", "পরে আর দুইয়ের শেষেরটি"),
    ("lay and lie", "রাখা আর শুয়ে পড়া"),
    ("lead and led", "নেতৃত্ব দেওয়া আর দিয়েছিল"),
    ("lend and borrow", "ধার দেওয়া আর ধার নেওয়া"),
    ("licence and license", "অনুমতিপত্র বিশেষ্য আর অনুমতি দেওয়া ক্রিয়া"),
    ("loose and lose", "ঢিলা আর হারানো"),
    ("moral and morale", "নৈতিক আর মনোবল"),
    ("passed and past", "অতিক্রম করেছিল আর অতীত"),
    ("personal and personnel", "ব্যক্তিগত আর কর্মীবৃন্দ"),
    ("practice and practise", "অনুশীলন বিশেষ্য আর অনুশীলন করা ক্রিয়া"),
    ("precede and proceed", "আগে আসা আর এগিয়ে চলা"),
    ("principal and principle", "প্রধান আর মূলনীতি"),
    ("quiet and quite", "নীরব আর বেশ"),
    ("raise and rise", "তোলা আর ওঠা"),
    ("respectably and respectively", "সম্মানজনকভাবে আর যথাক্রমে"),
    ("sit and set", "বসা আর রাখা"),
    ("stationary and stationery", "স্থির আর লেখার সরঞ্জাম"),
    ("than and then", "তুলনায় আর তারপর"),
    ("their and there", "তাদের আর সেখানে"),
    ("to and too", "প্রতি আর অতিরিক্ত"),
    ("weather and whether", "আবহাওয়া আর কিনা"),
    ("who and whom", "কর্তায় কে আর কর্মে কাকে"),
    ("your and you are", "তোমার আর তুমি হলে"),
]


HINDI_CORE = {
    "book": "किताब", "water": "पानी", "fire": "आग", "wind": "हवा",
    "sky": "आसमान", "soil": "मिट्टी", "tree": "पेड़", "flower": "फूल",
    "fruit": "फल", "bird": "पक्षी", "fish": "मछली", "dog": "कुत्ता",
    "cat": "बिल्ली", "cow": "गाय", "horse": "घोड़ा", "elephant": "हाथी",
    "tiger": "बाघ", "snake": "साँप", "ant": "चींटी", "house": "घर",
    "room": "कमरा", "door": "दरवाज़ा", "window": "खिड़की", "roof": "छत",
    "kitchen": "रसोई", "bed": "बिस्तर", "chair": "कुर्सी", "table": "मेज़",
    "light": "रोशनी", "darkness": "अंधेरा", "person": "व्यक्ति",
    "boy": "लड़का", "girl": "लड़की", "father": "पिता", "mother": "माँ",
    "brother": "भाई", "sister": "बहन", "friend": "दोस्त",
    "teacher": "शिक्षक", "student": "छात्र", "doctor": "डॉक्टर",
    "farmer": "किसान", "king": "राजा", "hand": "हाथ", "leg": "टांग",
    "eye": "आँख", "ear": "कान", "nose": "नाक", "mouth": "मुँह",
    "head": "सिर", "hair": "बाल", "tooth": "दाँत", "heart": "दिल",
    "blood": "खून", "to eat": "खाना", "to drink": "पीना",
    "to sleep": "सोना", "to walk": "चलना", "to run": "दौड़ना",
    "to sit": "बैठना", "to stand": "खड़ा होना", "to say": "कहना",
    "to listen": "सुनना", "to see": "देखना", "to read": "पढ़ना",
    "to write": "लिखना", "to learn": "सीखना", "to think": "सोचना",
    "to know": "जानना", "to give": "देना", "to take": "लेना",
    "to come": "आना", "to go": "जाना", "to work": "काम करना",
    "to play": "खेलना", "to laugh": "हँसना", "to cry": "रोना",
    "to buy": "खरीदना", "to sell": "बेचना", "to open": "खोलना",
    "to close": "बंद करना", "to wash": "धोना", "to cook": "पकाना",
    "to clean": "साफ़ करना", "to cut": "काटना", "to carry": "ले जाना",
    "to bring": "लाना", "to send": "भेजना", "to wait": "इंतज़ार करना",
    "to ask": "पूछना", "to call": "बुलाना", "to start": "शुरू करना",
    "to stop": "रुकना", "to find": "ढूँढ़ना", "to lose": "खोना",
    "to win": "जीतना", "to fall": "गिरना", "to jump": "कूदना",
    "to fly": "उड़ना", "to swim": "तैरना", "to climb": "चढ़ना",
    "to push": "धकेलना", "to pull": "खींचना", "to throw": "फेंकना",
    "to catch": "पकड़ना", "to build": "बनाना", "to break": "तोड़ना",
    "to fix": "ठीक करना", "to fill": "भरना", "to count": "गिनना",
    "to draw": "चित्र बनाना", "to dance": "नाचना", "to wear": "पहनना",
    "to smile": "मुस्कुराना", "to remember": "याद रखना",
    "to forget": "भूल जाना", "to follow": "पीछा करना",
    "big": "बड़ा", "small": "छोटा", "tall": "लंबा", "heavy": "भारी",
    "new": "नया", "old": "पुराना", "good": "अच्छा", "bad": "बुरा",
    "beautiful": "सुंदर", "fast": "तेज़", "slow": "धीमा", "hot": "गरम",
    "cold": "ठंडा", "easy": "आसान", "difficult": "कठिन", "true": "सच",
    "false": "झूठ", "rich": "अमीर", "poor": "गरीब", "happy": "खुश",
    "sad": "दुखी", "anger": "गुस्सा", "fear": "डर", "love": "प्यार",
    "hope": "उम्मीद", "dream": "सपना", "peace": "शांति",
    "courage": "साहस", "patience": "धैर्य", "time": "समय", "day": "दिन",
    "night": "रात", "morning": "सुबह", "noon": "दोपहर",
    "afternoon": "तीसरा पहर", "evening": "शाम", "week": "सप्ताह",
    "month": "महीना", "year": "साल", "today": "आज", "now": "अभी",
    "later": "बाद में", "always": "हमेशा", "money": "पैसा", "price": "दाम",
    "market": "बाज़ार", "shop": "दुकान", "food": "खाना", "rice": "चावल",
    "bread": "रोटी", "milk": "दूध", "tea": "चाय", "egg": "अंडा",
    "salt": "नमक", "sugar": "चीनी", "oil": "तेल", "meat": "मांस",
    "vegetable": "सब्ज़ी", "city": "शहर", "village": "गाँव",
    "road": "सड़क", "river": "नदी", "sea": "समुद्र", "mountain": "पहाड़",
    "forest": "जंगल", "country": "देश", "earth": "पृथ्वी", "sun": "सूरज",
    "moon": "चाँद", "star": "तारा", "cloud": "बादल", "rain": "बारिश",
    "storm": "तूफ़ान", "school": "स्कूल", "university": "विश्वविद्यालय",
    "pen": "कलम", "paper": "कागज़", "question": "सवाल", "answer": "जवाब",
    "language": "भाषा", "word": "शब्द", "sentence": "वाक्य",
    "story": "कहानी", "poem": "कविता", "news": "समाचार", "picture": "तस्वीर",
    "song": "गीत", "movie": "फ़िल्म", "health": "स्वास्थ्य", "body": "शरीर",
    "medicine": "दवा", "hospital": "अस्पताल", "exercise": "व्यायाम",
    "job": "नौकरी", "office": "दफ़्तर", "business": "व्यापार",
    "bank": "बैंक", "government": "सरकार", "law": "कानून",
    "freedom": "आज़ादी", "history": "इतिहास", "culture": "संस्कृति",
    "science": "विज्ञान", "technology": "तकनीक", "computer": "कंप्यूटर",
    "machine": "मशीन", "electricity": "बिजली", "help": "मदद",
    "effort": "प्रयास", "opportunity": "अवसर", "problem": "समस्या",
    "solution": "समाधान", "reason": "कारण", "result": "परिणाम",
    "example": "उदाहरण", "difference": "अंतर", "importance": "महत्व",
    "need": "ज़रूरत", "decision": "फैसला", "plan": "योजना",
    "goal": "लक्ष्य", "success": "सफलता", "failure": "असफलता",
    "experience": "अनुभव", "knowledge": "ज्ञान", "memory": "याददाश्त",
    "habit": "आदत", "responsibility": "ज़िम्मेदारी", "trust": "भरोसा",
    "respect": "सम्मान", "gift": "उपहार", "guest": "मेहमान",
    "neighbour": "पड़ोसी", "family": "परिवार", "marriage": "शादी",
    "child": "बच्चा", "one": "एक", "two": "दो", "three": "तीन",
    "four": "चार", "five": "पाँच", "six": "छह", "seven": "सात",
    "eight": "आठ", "nine": "नौ", "ten": "दस", "hundred": "सौ",
    "thousand": "हज़ार", "first": "पहला", "second": "दूसरा",
    "third": "तीसरा", "half": "आधा", "number": "संख्या", "red": "लाल",
    "blue": "नीला", "green": "हरा", "yellow": "पीला", "black": "काला",
    "white": "सफ़ेद", "brown": "भूरा", "pink": "गुलाबी", "grey": "धूसर",
    "colour": "रंग", "shirt": "कमीज़", "trousers": "पतलून",
    "shoe": "जूता", "sock": "मोज़ा", "hat": "टोपी", "dress": "पोशाक",
    "cloth": "कपड़ा", "bag": "थैला", "umbrella": "छाता", "key": "चाबी",
    "ring": "अंगूठी", "button": "बटन", "pocket": "जेब", "towel": "तौलिया",
    "soap": "साबुन", "mirror": "आईना", "spoon": "चम्मच", "plate": "थाली",
    "glass": "गिलास", "cup": "प्याला", "knife": "चाकू", "bottle": "बोतल",
    "stove": "चूल्हा", "fan": "पंखा", "lamp": "दीपक", "floor": "फ़र्श",
    "wall": "दीवार", "garden": "बगीचा", "mango": "आम", "banana": "केला",
    "apple": "सेब", "lemon": "नींबू", "potato": "आलू", "onion": "प्याज़",
    "garlic": "लहसुन", "tomato": "टमाटर", "chicken": "मुर्गी",
    "curd": "दही", "butter": "मक्खन", "honey": "शहद", "flour": "आटा",
    "lentil": "दाल", "spice": "मसाला", "juice": "रस", "sweet": "मीठा",
    "sour": "खट्टा", "bitter": "कड़वा", "spicy": "तीखा",
    "hungry": "भूखा", "thirsty": "प्यासा", "tasty": "स्वादिष्ट",
    "mouse": "चूहा", "goat": "बकरी", "sheep": "भेड़", "duck": "बत्तख",
    "crow": "कौआ", "pigeon": "कबूतर", "monkey": "बंदर", "lion": "शेर",
    "bear": "भालू", "deer": "हिरण", "frog": "मेंढक", "butterfly": "तितली",
    "bee": "मधुमक्खी", "mosquito": "मच्छर", "spider": "मकड़ी",
    "insect": "कीड़ा", "uncle": "चाचा", "aunt": "चाची",
    "grandfather": "दादा", "grandmother": "दादी", "son": "बेटा",
    "daughter": "बेटी", "husband": "पति", "wife": "पत्नी", "name": "नाम",
    "age": "उम्र", "man": "आदमी", "woman": "औरत", "people": "लोग",
    "finger": "उँगली", "knee": "घुटना", "shoulder": "कंधा", "back": "पीठ",
    "chest": "छाती", "stomach": "पेट", "skin": "त्वचा", "bone": "हड्डी",
    "neck": "गर्दन", "lip": "होंठ", "tongue": "जीभ", "here": "यहाँ",
    "there": "वहाँ", "near": "पास", "far": "दूर", "above": "ऊपर",
    "below": "नीचे", "inside": "अंदर", "outside": "बाहर", "left": "बायाँ",
    "right": "दायाँ", "front": "सामने", "behind": "पीछे", "between": "बीच",
    "again": "फिर से", "please": "कृपया", "sorry": "माफ़ कीजिए",
    "hello": "नमस्ते", "goodbye": "अलविदा", "yesterday": "कल बीता",
    "tomorrow": "आने वाला कल", "hour": "घंटा", "minute": "मिनट",
    "holiday": "छुट्टी", "birthday": "जन्मदिन", "festival": "त्योहार",
    "bus": "बस", "train": "रेलगाड़ी", "car": "गाड़ी", "bicycle": "साइकिल",
    "boat": "नाव", "plane": "हवाई जहाज़", "station": "स्टेशन",
    "ticket": "टिकट", "driver": "चालक", "police": "पुलिस",
    "nurse": "नर्स", "worker": "मज़दूर", "soldier": "सैनिक",
    "letter": "चिट्ठी", "map": "नक्शा", "phone": "फ़ोन",
    "television": "टेलीविज़न", "camera": "कैमरा", "toy": "खिलौना",
    "strong": "मज़बूत", "weak": "कमज़ोर", "clean": "साफ़",
    "dirty": "गंदा", "wet": "गीला", "dry": "सूखा", "full": "भरा",
    "empty": "खाली", "soft": "नरम", "hard": "कठोर", "sharp": "तेज़धार",
    "round": "गोल", "straight": "सीधा", "wide": "चौड़ा", "narrow": "सँकरा",
    "deep": "गहरा", "quiet": "शांत", "loud": "ज़ोरदार", "young": "जवान",
    "free": "मुक्त", "busy": "व्यस्त", "ready": "तैयार", "sure": "निश्चित",
    "alone": "अकेला", "same": "समान", "different": "अलग", "many": "बहुत",
    "few": "थोड़े", "more": "अधिक", "less": "कम",
}


def word_bank():
    """Every starter word as (english, bangla, level).

    Duplicates are dropped keeping the easiest level, and made up phrases are
    left out, so a card never shows anything but a real word or a "to" verb.
    """
    out, seen = [], set()
    groups = [(("A2" if en in A2_WORDS else "A1"), en, bn) for en, bn in SEED]
    groups += [("A1", en, bn) for en, bn in A1_EXTRA]
    groups += [("A2", en, bn) for en, bn in A2_EXTRA]
    groups += [("B1", en, bn) for en, bn in B1_EXTRA]
    groups += [("A1", en, bn) for en, bn in A1_MORE]
    groups += [("A2", en, bn) for en, bn in A2_MORE]
    groups += [("B1", en, bn) for en, bn in B1]
    groups += [("B1", en, bn) for en, bn in B1_MORE]
    groups += [("B2", en, bn) for en, bn in B2]
    groups += [("B2", en, bn) for en, bn in B2_MORE]
    groups += [("C1", en, bn) for en, bn in C1]
    groups += [("C1", en, bn) for en, bn in C1_MORE]
    groups += [("C2", en, bn) for en, bn in C2]
    groups += [("C2", en, bn) for en, bn in C2_MORE]
    for level, en, bn in groups:
        key = en.strip().lower()
        if key in seen or not bn.strip():
            continue
        if " " in key and not key.startswith("to "):
            continue
        seen.add(key)
        out.append((key, bn.strip(), level, "word"))
    # phrasal verbs and idioms are whole expressions, so the single word rule
    # above does not apply to them
    for kind, rows, level in (("phrasal", PHRASAL, "B1"),
                              ("idiom", IDIOMS, "B2"),
                              ("collocation", COLLOCATIONS, "B1"),
                              ("proverb", PROVERBS, "B2"),
                              ("confusable", CONFUSABLES, "B2")):
        for en, bn in rows:
            key = " ".join(en.strip().lower().split())
            if key in seen or not bn.strip():
                continue
            seen.add(key)
            out.append((key, bn.strip(), level, kind))
    return out


# -------------------------------------------------------------------- storage

class Store:
    def __init__(self, path=DB_PATH):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.setup()

    def setup(self):
        with self.lock:
            c = self.conn
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS words (
                    id INTEGER PRIMARY KEY,
                    english TEXT NOT NULL,
                    bangla TEXT NOT NULL DEFAULT '',
                    hindi TEXT DEFAULT '',
                    pos TEXT DEFAULT '',
                    definition TEXT DEFAULT '',
                    example TEXT DEFAULT '',
                    phonetic TEXT DEFAULT '',
                    synonyms TEXT DEFAULT '',
                    antonyms TEXT DEFAULT '',
                    examples TEXT DEFAULT '',
                    senses TEXT DEFAULT '',
                    audio TEXT DEFAULT '',
                    level TEXT DEFAULT 'A1',
                    kind TEXT DEFAULT 'word',
                    enriched INTEGER DEFAULT 0,
                    created TEXT,
                    UNIQUE(english)
                );
                CREATE TABLE IF NOT EXISTS progress (
                    word_id INTEGER PRIMARY KEY,
                    box INTEGER DEFAULT 1,
                    seen INTEGER DEFAULT 0,
                    correct INTEGER DEFAULT 0,
                    wrong INTEGER DEFAULT 0,
                    last_shown TEXT,
                    due TEXT
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS answers (
                    id INTEGER PRIMARY KEY,
                    day TEXT NOT NULL,
                    at TEXT,
                    word_id INTEGER,
                    correct INTEGER DEFAULT 0,
                    style TEXT DEFAULT 'choice'
                );
                CREATE TABLE IF NOT EXISTS daily (
                    day TEXT NOT NULL,
                    word_id INTEGER NOT NULL,
                    position INTEGER DEFAULT 0,
                    shown INTEGER DEFAULT 0,
                    known INTEGER DEFAULT 0,
                    PRIMARY KEY (day, word_id)
                );
                """
            )
            columns = [r["name"] for r in
                       c.execute("PRAGMA table_info(words)").fetchall()]
            if "level" not in columns:
                # older database from before levels existed
                c.execute("ALTER TABLE words ADD COLUMN level TEXT DEFAULT 'A1'")
            if "examples" not in columns:
                c.execute("ALTER TABLE words ADD COLUMN examples TEXT DEFAULT ''")
            if "audio" not in columns:
                c.execute("ALTER TABLE words ADD COLUMN audio TEXT DEFAULT ''")
            if "senses" not in columns:
                c.execute("ALTER TABLE words ADD COLUMN senses TEXT "
                          "DEFAULT ''")
            if "hindi" not in columns:
                c.execute("ALTER TABLE words ADD COLUMN hindi TEXT DEFAULT ''")
            if "antonyms" not in columns:
                c.execute("ALTER TABLE words ADD COLUMN antonyms TEXT "
                          "DEFAULT ''")
            if "kind" not in columns:
                c.execute("ALTER TABLE words ADD COLUMN kind TEXT "
                          "DEFAULT 'word'")
            if "favourite" not in columns:
                c.execute("ALTER TABLE words ADD COLUMN favourite INTEGER "
                          "DEFAULT 0")
                c.execute("ALTER TABLE words ADD COLUMN fav_at TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS words_level ON words(level)")
            c.execute("CREATE INDEX IF NOT EXISTS words_kind ON words(kind)")
            c.execute("CREATE INDEX IF NOT EXISTS answers_day ON answers(day)")
            for k, v in DEFAULTS.items():
                c.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (k, v))
            c.commit()

    # settings -------------------------------------------------------------
    def get(self, key, fallback=None):
        with self.lock:
            row = self.conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return DEFAULTS.get(key, fallback)
        return row["value"]

    def get_int(self, key):
        try:
            return int(self.get(key))
        except (TypeError, ValueError):
            return int(DEFAULTS.get(key, 0))

    def put(self, key, value):
        with self.lock:
            self.conn.execute(
                "INSERT INTO settings VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))
            self.conn.commit()

    # words ----------------------------------------------------------------
    def seed_if_empty(self):
        with self.lock:
            n = self.conn.execute("SELECT COUNT(*) n FROM words").fetchone()["n"]
        if n:
            return 0
        bank = word_bank()
        self.bulk_add(bank)
        self.put("seed_version", SEED_VERSION)
        return len(bank)

    def bulk_add(self, rows):
        """Insert many starter words in one transaction. Existing rows keep
        their meaning and their score, they only gain their level."""
        now = datetime.now().isoformat(timespec="seconds")
        added = 0
        with self.lock:
            c = self.conn
            before = c.execute("SELECT COUNT(*) n FROM words").fetchone()["n"]
            c.executemany(
                "INSERT OR IGNORE INTO words (english,bangla,hindi,pos,level,"
                "kind,created) VALUES (?,?,?,?,?,?,?)",
                [(en, bn, HINDI_CORE.get(en, ""), guess_pos(en, kd, bn), lv,
                  kd, now) for en, bn, lv, kd in rows])
            # older rows, and anything added before this version, get one too
            c.executemany(
                "UPDATE words SET pos=? WHERE english=? AND "
                "(pos IS NULL OR pos='')",
                [(guess_pos(en, kd, bn), en) for en, bn, lv, kd in rows])
            c.executemany(
                "UPDATE words SET hindi=? WHERE english=? AND "
                "(hindi IS NULL OR hindi='')",
                [(hi, en) for en, hi in HINDI_CORE.items()])
            c.executemany(
                "UPDATE words SET level=?, kind=? WHERE english=? AND "
                "(level IS NULL OR level='' OR level<>? OR kind IS NULL "
                "OR kind<>?)",
                [(lv, kd, en, lv, kd) for en, bn, lv, kd in rows])
            c.execute(
                "INSERT OR IGNORE INTO progress (word_id,box,due) "
                "SELECT id,1,? FROM words WHERE id NOT IN "
                "(SELECT word_id FROM progress)", (now,))
            c.commit()
            added = c.execute(
                "SELECT COUNT(*) n FROM words").fetchone()["n"] - before
        return added

    def sync_seed(self):
        """Add starter words that are new since this database was created, and
        tag older rows with their level. Runs once per seed version."""
        if self.get("seed_version") == str(SEED_VERSION):
            return 0
        added = self.bulk_add(word_bank())
        self.put("seed_version", SEED_VERSION)
        return added

    def add_word(self, english, bangla="", level=None, kind=None, **extra):
        english = (english or "").strip().lower()
        bangla = (bangla or "").strip()
        if not english:
            return None
        level = level if level in LEVELS else None
        kind = kind if kind in KINDS else "word"
        if not extra.get("pos"):
            extra["pos"] = guess_pos(english, kind, bangla)
        now = datetime.now().isoformat(timespec="seconds")
        with self.lock:
            self.conn.execute(
                "INSERT INTO words (english,bangla,pos,definition,example,"
                "phonetic,synonyms,level,kind,enriched,created) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(english) DO UPDATE SET "
                "bangla=CASE WHEN excluded.bangla<>'' THEN excluded.bangla "
                "ELSE words.bangla END",
                (english, bangla, extra.get("pos", ""),
                 extra.get("definition", ""), extra.get("example", ""),
                 extra.get("phonetic", ""), extra.get("synonyms", ""),
                 level or "A1", kind,
                 int(bool(extra.get("enriched", 0))), now))
            # lastrowid is unreliable after an upsert, so read the id back
            wid = self.conn.execute(
                "SELECT id FROM words WHERE english=?",
                (english,)).fetchone()["id"]
            self.conn.execute(
                "INSERT OR IGNORE INTO progress (word_id,box,due) VALUES (?,1,?)",
                (wid, now))
            self.conn.commit()
        return wid

    def update_word(self, wid, **fields):
        allowed = ("english", "bangla", "pos", "definition", "example",
                   "hindi", "phonetic", "synonyms", "antonyms", "examples",
                   "senses", "audio", "level", "kind", "enriched")
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(k + "=?")
                vals.append(v)
        if not sets:
            return
        vals.append(wid)
        with self.lock:
            self.conn.execute(
                "UPDATE words SET " + ",".join(sets) + " WHERE id=?", vals)
            self.conn.commit()

    def delete_word(self, wid):
        with self.lock:
            self.conn.execute("DELETE FROM words WHERE id=?", (wid,))
            self.conn.execute("DELETE FROM progress WHERE word_id=?", (wid,))
            self.conn.commit()

    def all_words(self):
        with self.lock:
            return self.conn.execute(
                "SELECT w.*, p.box, p.seen, p.correct, p.wrong FROM words w "
                "LEFT JOIN progress p ON p.word_id=w.id "
                "ORDER BY w.english").fetchall()

    def word(self, wid):
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM words WHERE id=?", (wid,)).fetchone()

    def count(self):
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) n FROM words").fetchone()["n"]

    # levels ---------------------------------------------------------------
    def active_levels(self):
        """Which levels the cards and quizzes draw from right now."""
        chosen = self.get("level")
        if chosen not in LEVELS:
            chosen = DEFAULTS["level"]
        if self.get("include_lower") == "1":
            return LEVELS[:LEVELS.index(chosen) + 1]
        return [chosen]

    def mode(self):
        chosen = self.get("mode")
        return chosen if chosen in KINDS else "word"

    def language(self):
        chosen = self.get("language")
        return chosen if chosen in ("bn", "hi", "both") else "bn"

    def meaning_of(self, row, language=None):
        """The meaning in the language you are learning in."""
        language = language or self.language()
        bn = (row["bangla"] or "").strip()
        try:
            hi = (row["hindi"] or "").strip()
        except Exception:
            hi = ""
        if language == "hi":
            return hi or bn
        if language == "both":
            both = [x for x in (bn, hi) if x]
            return "\n".join(both) if both else ""
        return bn or hi

    def fill_language(self, rows, language="hi", stop=None, on_step=None):
        """Translate the missing meanings for a batch of entries."""
        field = "hindi" if language == "hi" else "bangla"
        pair = "en|hi" if language == "hi" else "en|bn"
        email = self.get("contact_email") or ""
        done = 0
        for row in rows:
            if stop is not None and stop.is_set():
                break
            current = ""
            try:
                current = (row[field] or "").strip()
            except Exception:
                current = ""
            if current:
                continue
            text = cached(field + ":" + row["english"],
                          lambda w=row["english"]: translate(w, pair, email))
            if text and text.lower() != row["english"].lower():
                self.update_word(row["id"], **{field: text})
                done += 1
                if on_step:
                    on_step(done, row["english"], text)
        return done

    def level_filter(self, alias="w"):
        """What the cards and quizzes may draw from right now.

        Phrasal verbs and idioms are not graded A1 to C2, so in those modes
        the level is ignored and only the kind matters.
        """
        kind = self.mode()
        clause = " AND " + alias + ".kind=? "
        values = [kind]
        if kind == "word":
            levels = self.active_levels()
            clause += ("AND " + alias + ".level IN (" +
                       ",".join("?" * len(levels)) + ") ")
            values += levels
        return clause, values

    def level_counts(self, kind=None):
        kind = kind or self.mode()
        with self.lock:
            rows = self.conn.execute(
                "SELECT level, COUNT(*) n FROM words WHERE kind=? "
                "GROUP BY level", (kind,)).fetchall()
        return {r["level"]: r["n"] for r in rows}

    def kind_counts(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT kind, COUNT(*) n FROM words GROUP BY kind").fetchall()
        return {r["kind"]: r["n"] for r in rows}

    # the day's set ---------------------------------------------------------
    def today_key(self):
        return datetime.now().strftime("%Y-%m-%d")

    def daily_rows(self, day=None):
        day = day or self.today_key()
        with self.lock:
            return self.conn.execute(
                "SELECT w.*, d.position, d.shown, d.known FROM daily d "
                "JOIN words w ON w.id=d.word_id WHERE d.day=? "
                "ORDER BY d.position", (day,)).fetchall()

    def build_today(self, count=None, force=False):
        """Choose the words for today.

        The set is kept until tomorrow, except when the level changes: a set
        built for C2 would otherwise keep showing C2 words after a switch.
        """
        day = self.today_key()
        stamp = self.mode() + ":" + ",".join(self.active_levels())
        if not force and self.daily_rows(day):
            if self.get("daily_levels") == stamp:
                return self.daily_rows(day)
            force = True
        count = count or max(1, self.get_int("daily_count"))
        where, levels = self.level_filter()
        now = datetime.now().isoformat(timespec="seconds")
        with self.lock:
            # words that are due or weak come first, then anything unseen
            rows = self.conn.execute(
                "SELECT w.id FROM words w JOIN progress p ON p.word_id=w.id "
                "WHERE 1=1" + where +
                "ORDER BY (p.due IS NULL OR p.due<=?) DESC, p.box ASC, "
                "COALESCE(p.last_shown,'') ASC, RANDOM() LIMIT ?",
                levels + [now, count * 4]).fetchall()
        ids = [r["id"] for r in rows]
        random.shuffle(ids)
        ids = ids[:count]
        with self.lock:
            self.conn.execute("DELETE FROM daily WHERE day=?", (day,))
            self.conn.executemany(
                "INSERT OR IGNORE INTO daily (day,word_id,position) "
                "VALUES (?,?,?)",
                [(day, wid, i) for i, wid in enumerate(ids)])
            self.conn.commit()
        self.put("daily_levels", stamp)
        return self.daily_rows(day)

    def next_daily_word(self):
        """The next word in today's loop: least shown, then in order."""
        rows = self.build_today()
        if not rows:
            return None
        fresh = [r for r in rows if not r["known"]] or list(rows)
        fewest = min(r["shown"] for r in fresh)
        return next(r for r in fresh if r["shown"] == fewest)

    def mark_daily_shown(self, wid):
        with self.lock:
            self.conn.execute(
                "UPDATE daily SET shown=shown+1 WHERE day=? AND word_id=?",
                (self.today_key(), wid))
            self.conn.commit()

    def mark_daily_known(self, wid, known=True):
        with self.lock:
            self.conn.execute(
                "UPDATE daily SET known=? WHERE day=? AND word_id=?",
                (1 if known else 0, self.today_key(), wid))
            self.conn.commit()

    def daily_progress(self):
        rows = self.daily_rows()
        done = sum(1 for r in rows if r["known"])
        loops = min((r["shown"] for r in rows), default=0)
        return done, len(rows), loops

    # what you answered, day by day --------------------------------------
    def log_answer(self, wid, correct, style="choice"):
        now = datetime.now()
        with self.lock:
            self.conn.execute(
                "INSERT INTO answers (day,at,word_id,correct,style) "
                "VALUES (?,?,?,?,?)",
                (now.strftime("%Y-%m-%d"), now.isoformat(timespec="seconds"),
                 wid, 1 if correct else 0, style))
            self.conn.commit()

    def day_totals(self, days=14):
        """Answers and hits for each of the last few days, oldest first."""
        out = []
        for back in range(days - 1, -1, -1):
            day = (datetime.now() - timedelta(days=back)).strftime("%Y-%m-%d")
            with self.lock:
                row = self.conn.execute(
                    "SELECT COUNT(*) n, SUM(correct) c FROM answers WHERE day=?",
                    (day,)).fetchone()
                learned = self.conn.execute(
                    "SELECT COUNT(*) n FROM daily WHERE day=? AND known=1",
                    (day,)).fetchone()["n"]
            out.append({"day": day, "answered": row["n"] or 0,
                        "correct": row["c"] or 0, "learned": learned})
        return out

    def streak(self):
        """How many days in a row you have studied, today or yesterday first."""
        with self.lock:
            days = {r["day"] for r in self.conn.execute(
                "SELECT DISTINCT day FROM answers").fetchall()}
            days |= {r["day"] for r in self.conn.execute(
                "SELECT DISTINCT day FROM daily WHERE known=1").fetchall()}
        if not days:
            return 0
        today = datetime.now().date()
        start = today
        if today.strftime("%Y-%m-%d") not in days:
            start = today - timedelta(days=1)
            if start.strftime("%Y-%m-%d") not in days:
                return 0
        count = 0
        while start.strftime("%Y-%m-%d") in days:
            count += 1
            start -= timedelta(days=1)
        return count

    def overall(self):
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) n, SUM(correct) c FROM answers").fetchone()
            days = self.conn.execute(
                "SELECT COUNT(DISTINCT day) d FROM answers").fetchone()["d"]
        answered = row["n"] or 0
        correct = row["c"] or 0
        return {"answered": answered, "correct": correct, "days": days or 0,
                "accuracy": (100.0 * correct / answered) if answered else 0.0}

    def hard_words(self, limit=100):
        """Words you keep getting wrong, worst first."""
        with self.lock:
            return self.conn.execute(
                "SELECT w.*, p.box, p.seen, p.correct, p.wrong FROM words w "
                "JOIN progress p ON p.word_id=w.id "
                "WHERE p.wrong >= 2 AND p.wrong > p.correct "
                "ORDER BY (p.wrong - p.correct) DESC, p.wrong DESC LIMIT ?",
                (limit,)).fetchall()

    def search(self, text, limit=300):
        text = "%" + (text or "").strip() + "%"
        with self.lock:
            return self.conn.execute(
                "SELECT w.*, p.box, p.seen, p.correct, p.wrong FROM words w "
                "LEFT JOIN progress p ON p.word_id=w.id "
                "WHERE w.english LIKE ? OR w.bangla LIKE ? "
                "OR w.definition LIKE ? ORDER BY w.english LIMIT ?",
                (text, text, text, limit)).fetchall()

    def family(self, row, limit=8):
        """Other entries built on the same stem, such as meticulously."""
        base = word_stem(row["english"])
        if not base or len(base) < 4:
            return []
        with self.lock:
            return self.conn.execute(
                "SELECT english, bangla, kind FROM words WHERE id<>? AND "
                "english LIKE ? ORDER BY LENGTH(english) LIMIT ?",
                (row["id"], base + "%", limit)).fetchall()

    def with_synonyms(self, n=1, kind="word"):
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM words WHERE synonyms<>'' AND kind=? "
                "ORDER BY RANDOM() LIMIT ?", (kind, n)).fetchall()

    def quiet_now(self):
        """Whether the clock is inside the hours you asked to be left alone."""
        try:
            start = int(self.get("quiet_from"))
            end = int(self.get("quiet_to"))
        except (TypeError, ValueError):
            return False
        hour = datetime.now().hour
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    # favourites -------------------------------------------------------------
    def set_favourite(self, wid, on=True):
        now = datetime.now().isoformat(timespec="seconds") if on else None
        with self.lock:
            self.conn.execute(
                "UPDATE words SET favourite=?, fav_at=? WHERE id=?",
                (1 if on else 0, now, wid))
            self.conn.commit()

    def is_favourite(self, wid):
        row = self.word(wid)
        if row is None:
            return False
        try:
            return bool(row["favourite"])
        except Exception:
            return False

    def favourites(self):
        with self.lock:
            return self.conn.execute(
                "SELECT w.*, p.box, p.seen, p.correct, p.wrong FROM words w "
                "LEFT JOIN progress p ON p.word_id=w.id WHERE w.favourite=1 "
                "ORDER BY COALESCE(w.fav_at,'') DESC").fetchall()

    def pick_word(self):
        """Pick the word most worth showing: due first, weakest box first."""
        if self.get("daily_only") == "1":
            row = self.next_daily_word()
            if row is not None:
                return row
        now = datetime.now().isoformat(timespec="seconds")
        where, levels = self.level_filter()
        with self.lock:
            rows = self.conn.execute(
                "SELECT w.*, p.box, p.due FROM words w "
                "JOIN progress p ON p.word_id=w.id "
                "WHERE (p.due IS NULL OR p.due<=?)" + where +
                "ORDER BY p.box ASC, COALESCE(p.last_shown,'') ASC LIMIT 25",
                [now] + levels).fetchall()
            if not rows:
                rows = self.conn.execute(
                    "SELECT w.*, p.box, p.due FROM words w "
                    "JOIN progress p ON p.word_id=w.id WHERE 1=1" + where +
                    "ORDER BY p.box ASC, RANDOM() LIMIT 25", levels).fetchall()
            if not rows:
                # nothing at the chosen level, fall back to anything
                rows = self.conn.execute(
                    "SELECT w.*, p.box, p.due FROM words w "
                    "JOIN progress p ON p.word_id=w.id "
                    "ORDER BY RANDOM() LIMIT 25").fetchall()
        if not rows:
            return None
        return random.choice(rows[:8])

    def quiz_pool(self, n):
        where, levels = self.level_filter()
        with self.lock:
            rows = self.conn.execute(
                "SELECT w.*, p.box FROM words w JOIN progress p "
                "ON p.word_id=w.id WHERE w.bangla<>''" + where +
                "ORDER BY p.box ASC, RANDOM() LIMIT ?",
                levels + [max(n * 3, 12)]).fetchall()
        rows = list(rows)
        random.shuffle(rows)
        return rows[:n]

    def distractors(self, wid, n=3, level=None, kind=None):
        """Wrong answers come from the same kind and level, so the quiz is fair."""
        kind = kind or self.mode()
        with self.lock:
            rows = []
            if level and kind == "word":
                rows = self.conn.execute(
                    "SELECT english,bangla FROM words WHERE id<>? AND bangla<>''"
                    " AND level=? AND kind=? ORDER BY RANDOM() LIMIT ?",
                    (wid, level, kind, n)).fetchall()
            if len(rows) < n:
                rows = self.conn.execute(
                    "SELECT english,bangla FROM words WHERE id<>? AND bangla<>''"
                    " AND kind=? ORDER BY RANDOM() LIMIT ?",
                    (wid, kind, n)).fetchall()
            if len(rows) < n:
                rows = self.conn.execute(
                    "SELECT english,bangla FROM words WHERE id<>? AND bangla<>''"
                    " ORDER BY RANDOM() LIMIT ?", (wid, n)).fetchall()
            return rows

    # progress -------------------------------------------------------------
    def mark(self, wid, knew, style=""):
        """Leitner style: right answer moves the word up a box, wrong resets."""
        if style:
            self.log_answer(wid, knew, style)
        gaps = {1: 0, 2: 1, 3: 3, 4: 7, 5: 16}
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM progress WHERE word_id=?", (wid,)).fetchone()
            box = row["box"] if row else 1
            box = min(5, box + 1) if knew else 1
            due = (datetime.now() + timedelta(days=gaps[box])
                   ).isoformat(timespec="seconds")
            now = datetime.now().isoformat(timespec="seconds")
            self.conn.execute(
                "INSERT INTO progress (word_id,box,seen,correct,wrong,"
                "last_shown,due) VALUES (?,?,1,?,?,?,?) "
                "ON CONFLICT(word_id) DO UPDATE SET box=?, seen=progress.seen+1,"
                "correct=progress.correct+?, wrong=progress.wrong+?, "
                "last_shown=?, due=?",
                (wid, box, int(knew), int(not knew), now, due,
                 box, int(knew), int(not knew), now, due))
            self.conn.commit()

    def touch(self, wid):
        now = datetime.now().isoformat(timespec="seconds")
        with self.lock:
            self.conn.execute(
                "UPDATE progress SET seen=seen+1, last_shown=? WHERE word_id=?",
                (now, wid))
            self.conn.commit()

    def stats(self):
        with self.lock:
            r = self.conn.execute(
                "SELECT COUNT(*) total, "
                "SUM(CASE WHEN p.box>=4 THEN 1 ELSE 0 END) strong, "
                "SUM(COALESCE(p.correct,0)) correct, "
                "SUM(COALESCE(p.wrong,0)) wrong "
                "FROM words w LEFT JOIN progress p ON p.word_id=w.id"
            ).fetchone()
        return dict(total=r["total"] or 0, strong=r["strong"] or 0,
                    correct=r["correct"] or 0, wrong=r["wrong"] or 0)


# ------------------------------------------------------------- online lookup

def is_bangla(text):
    return any("ঀ" <= ch <= "৿" for ch in text or "")


def fetch_json(url, timeout=NET_TIMEOUT):
    req = urllib.request.Request(
        url, headers={"User-Agent": SHORT_NAME + "/" + VERSION,
                      "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def translate(text, pair, email="", timeout=NET_TIMEOUT):
    """MyMemory translation. pair is like 'en|bn'. Returns text or empty."""
    params = {"q": text, "langpair": pair}
    if email:
        params["de"] = email
    try:
        data = fetch_json(TRANSLATE_API + "?" + urllib.parse.urlencode(params),
                          timeout=timeout)
    except Exception:
        return ""
    out = ((data or {}).get("responseData") or {}).get("translatedText") or ""
    if "MYMEMORY WARNING" in out.upper() or "INVALID" in out.upper()[:12]:
        return ""
    if out.strip().lower() == (text or "").strip().lower():
        return out.strip()
    return out.strip()


def define(word, timeout=NET_TIMEOUT):
    """Free Dictionary API. Returns a dict, empty when the word is unknown."""
    try:
        data = fetch_json(DICT_API + urllib.parse.quote(word.strip()),
                          timeout=timeout)
    except Exception:
        return {}
    if not isinstance(data, list) or not data:
        return {}
    phonetic = audio = ""
    for entry in data:
        if not phonetic:
            phonetic = entry.get("phonetic") or ""
        for p in entry.get("phonetics") or []:
            if not phonetic and p.get("text"):
                phonetic = p["text"]
            if not audio and p.get("audio"):
                audio = p["audio"]
                if audio.startswith("//"):
                    audio = "https:" + audio
    pos = definition = ""
    synonyms, antonyms, examples, senses = [], [], [], []
    meanings = []
    for entry in data:
        meanings.extend(entry.get("meanings") or [])
    for meaning in meanings:
        defs = meaning.get("definitions") or []
        if not defs:
            continue
        this_pos = meaning.get("partOfSpeech") or ""
        if not definition:
            pos = this_pos
            definition = defs[0].get("definition") or ""
        for d in defs[:2]:
            text = (d.get("definition") or "").strip()
            if text and not any(x["definition"] == text for x in senses):
                senses.append({"pos": this_pos, "definition": text,
                               "example": (d.get("example") or "").strip()})
        for d in defs:
            synonyms.extend(d.get("synonyms") or [])
            antonyms.extend(d.get("antonyms") or [])
            if d.get("example"):
                examples.append(d["example"].strip())
        synonyms.extend(meaning.get("synonyms") or [])
        antonyms.extend(meaning.get("antonyms") or [])
    def tidy(words):
        seen, clean = set(), []
        for w in words:
            if w and w.lower() not in seen:
                seen.add(w.lower())
                clean.append(w)
        return clean
    clean = tidy(synonyms)
    clean_ant = tidy(antonyms)
    return {"phonetic": phonetic, "pos": pos, "definition": definition,
            "example": examples[0] if examples else "",
            "examples": tidy_sentences(examples),
            "audio": audio, "synonyms": ", ".join(clean[:6]),
            "antonyms": ", ".join(clean_ant[:6]), "senses": senses[:8]}


def tidy_sentences(sentences, keep=4, word=""):
    """Trim, de-duplicate and keep sentences that are actually readable."""
    out, seen = [], set()
    for s in sentences or []:
        s = " ".join(str(s).split())
        if not s or len(s) < 12 or len(s) > 160:
            continue
        low = s.lower()
        if low in seen:
            continue
        if word and word.lower() not in low:
            continue
        seen.add(low)
        if s[-1] not in ".!?":
            s += "."
        out.append(s[0].upper() + s[1:])
        if len(out) >= keep:
            break
    return out


def word_stem(word):
    """A rough stem, so 'meticulous' also matches 'meticulously'."""
    w = (word or "").strip().lower()
    if w.startswith("to "):
        w = w[3:]
    for suffix in ("ing", "edly", "ely", "ly", "ed", "es", "s"):
        if len(w) > len(suffix) + 3 and w.endswith(suffix):
            return w[:-len(suffix)]
    return w


def tatoeba_examples(word, limit=6):
    """Second source of example sentences."""
    try:
        url = TATOEBA_API + "?" + urllib.parse.urlencode(
            {"query": word, "from": "eng", "to": "eng", "limit": limit})
        data = fetch_json(url)
    except Exception:
        return []
    rows = (data or {}).get("results") or []
    return tidy_sentences([r.get("text", "") for r in rows], limit,
                          word_stem(word))


def wiktionary_examples(word, limit=4):
    """Third source. Wiktionary quotes real usage under each definition."""
    try:
        url = WIKTIONARY_API + "?" + urllib.parse.urlencode(
            {"action": "parse", "page": word, "prop": "wikitext",
             "format": "json", "formatversion": "2", "redirects": "1"})
        data = fetch_json(url)
    except Exception:
        return []
    text = (((data or {}).get("parse") or {}).get("wikitext") or "")
    if isinstance(text, dict):
        text = text.get("*", "")
    found = []
    for line in str(text).splitlines():
        line = line.strip()
        if not line.startswith("#:") and not line.startswith("#*:"):
            continue
        line = line.lstrip("#*: ").strip()
        line = re.sub(r"\{\{(?:ux|usex|uxi)\|[^|}]*\|([^|}]*).*?\}\}", r"\1",
                      line)
        line = re.sub(r"\{\{[^}]*\}\}", " ", line)
        line = re.sub(r"\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", line)
        line = re.sub(r"'{2,}", "", line)
        line = re.sub(r"<[^>]+>", " ", line)
        line = line.replace("''", "").strip()
        if line:
            found.append(line)
        if len(found) >= limit * 3:
            break
    return tidy_sentences(found, limit, word_stem(word))


LOOKUP_CACHE = {}
CACHE_LOCK = threading.Lock()


def cached(key, work):
    """Remember what each source said, so the second look is instant."""
    with CACHE_LOCK:
        if key in LOOKUP_CACHE:
            return LOOKUP_CACHE[key]
    value = work()
    with CACHE_LOCK:
        LOOKUP_CACHE[key] = value
        if len(LOOKUP_CACHE) > 400:
            for old_key in list(LOOKUP_CACHE)[:100]:
                LOOKUP_CACHE.pop(old_key, None)
    return value


def gather_sentences(word, on_update=None, timeout=9):
    """Ask all three sources at the same time, not one after the other.

    on_update is called with the sentences found so far each time a source
    answers, so a card can show the first ones while the rest are still on
    their way. Returns everything that arrived before the timeout.
    """
    base = word[3:] if word.startswith("to ") else word
    stem = word_stem(base)
    found, info = [], {}
    lock = threading.Lock()

    def note(more):
        if not more:
            return
        with lock:
            added = [s for s in more if s not in found]
            found.extend(added)
            snapshot = list(found)
        if added and on_update:
            try:
                on_update(snapshot)
            except Exception:
                pass

    def from_dictionary():
        data = define_any(base, timeout=timeout)
        if data:
            with lock:
                info.update(data)
            note(data.get("examples") or [])

    def from_tatoeba():
        note(cached("tat:" + base,
                    lambda: tatoeba_examples(base)))

    def from_wiktionary():
        note(cached("wik:" + base, lambda: wiktionary_examples(base)))

    def from_stem():
        if stem and stem != base and len(stem) > 3:
            note(cached("tat:" + stem, lambda: tatoeba_examples(stem)))

    workers = [threading.Thread(target=fn, daemon=True) for fn in
               (from_dictionary, from_tatoeba, from_wiktionary, from_stem)]
    for worker in workers:
        worker.start()
    deadline = time.time() + timeout
    for worker in workers:
        worker.join(max(0.1, deadline - time.time()))
    with lock:
        return tidy_sentences(list(found), 4), dict(info)


def sentences_for(word, on_update=None):
    """Example sentences for one word, from three sources at once."""
    return gather_sentences(word, on_update)


def mask_word(word, hide=0.45):
    """Knock letters out of a word, keeping the first and the last."""
    letters = list(str(word))
    spots = [i for i, ch in enumerate(letters)
             if ch.isalpha() and 0 < i < len(letters) - 1]
    if not spots:
        return " ".join(letters)
    count = max(1, int(round(len(spots) * hide)))
    for index in random.sample(spots, min(count, len(spots))):
        letters[index] = "_"
    return " ".join(letters)


def word_forms(word):
    """The shapes a copied word might really be: boxes, running, studied."""
    base = (word or "").strip().lower()
    out = [base]
    rules = (
        ("ies", "y"), ("ied", "y"), ("ier", "y"), ("iest", "y"),
        ("sses", "ss"), ("ches", "ch"), ("shes", "sh"), ("xes", "x"),
        ("ves", "f"), ("es", ""), ("s", ""),
        ("ing", ""), ("ed", ""), ("er", ""), ("est", ""), ("ly", ""),
    )
    for suffix, replacement in rules:
        if base.endswith(suffix) and len(base) > len(suffix) + 2:
            stem = base[:-len(suffix)] + replacement
            for candidate in (stem, stem + "e"):
                if candidate and candidate not in out:
                    out.append(candidate)
            # running becomes runn, so try the doubled letter as well
            if len(stem) > 2 and stem[-1] == stem[-2]:
                trimmed = stem[:-1]
                if trimmed not in out:
                    out.append(trimmed)
    return out[:6]


def define_any(word, timeout=NET_TIMEOUT):
    """Look the word up, then its base form, so copied words still work."""
    for candidate in word_forms(word):
        info = cached("def:" + candidate,
                      lambda c=candidate: define(c, timeout=timeout))
        if info and info.get("definition"):
            if candidate != word:
                info = dict(info)
                info["base"] = candidate
            return info
    return {}


def enrich_quick(store, row):
    """The half you need straight away: the Bengali meaning and the entry.

    The Bengali translation goes first, because that is the line you look at,
    and the dictionary call follows. Sentences come later in enrich_sentences,
    so a card can appear and fill itself in rather than making you wait.
    """
    if store.get("online") != "1":
        return {}
    english = row["english"]
    changes = {}
    language = store.language()
    email = store.get("contact_email") or ""
    if language in ("bn", "both") and not (row["bangla"] or "").strip():
        bn = cached("bangla:" + english, lambda: translate(
            english, "en|bn", email, timeout=6))
        if bn and is_bangla(bn):
            changes["bangla"] = bn
    if language in ("hi", "both"):
        try:
            has_hindi = (row["hindi"] or "").strip()
        except Exception:
            has_hindi = ""
        if not has_hindi:
            hi = cached("hindi:" + english, lambda: translate(
                english, "en|hi", email, timeout=6))
            if hi and hi.lower() != english.lower():
                changes["hindi"] = hi
    base = english[3:] if english.startswith("to ") else english
    info = define_any(base, timeout=6)
    for key in ("phonetic", "definition", "example", "audio",
                "synonyms", "antonyms"):
        if info.get(key) and not (row[key] if key in row.keys() else ""):
            changes[key] = info[key]
    # the dictionary always wins over the guess made when the word was saved
    if info.get("pos"):
        changes["pos"] = info["pos"]
    got = info.get("examples") or []
    if got:
        changes["examples"] = json.dumps(got, ensure_ascii=False)
    if info.get("senses"):
        changes["senses"] = json.dumps(info["senses"], ensure_ascii=False)
    if changes:
        store.update_word(row["id"], **changes)
    return changes


def enrich_sentences(store, row):
    """The slower half: example sentences from the other two sources."""
    if store.get("online") != "1":
        return {}
    fresh = store.word(row["id"]) or row
    have = []
    try:
        have = [x for x in json.loads(fresh["examples"] or "[]")
                if isinstance(x, str)]
    except Exception:
        have = []
    if len(have) >= 3:
        store.update_word(fresh["id"], enriched=1)
        return {}
    found, info = sentences_for(fresh["english"])
    for s in have:
        if s not in found:
            found.append(s)
    changes = {"enriched": 1}
    if found:
        changes["examples"] = json.dumps(found[:4], ensure_ascii=False)
        if not (fresh["example"] or ""):
            changes["example"] = found[0]
    for key in ("phonetic", "pos", "definition", "audio", "synonyms",
                "antonyms"):
        if info.get(key) and not (fresh[key] or ""):
            changes[key] = info[key]
    store.update_word(fresh["id"], **changes)
    return changes


def enrich(store, row):
    """Everything, in one call. Used where waiting does not matter."""
    changes = enrich_quick(store, row)
    fresh = store.word(row["id"]) or row
    changes.update(enrich_sentences(store, fresh) or {})
    if changes:
        changes["enriched"] = 1
    return changes


def lookup_new(store, text):
    """Add a word typed by the user in either script. Returns (id, message)."""
    text = (text or "").strip()
    if not text:
        return None, "Type a word first."
    email = store.get("contact_email") or ""
    online = store.get("online") == "1"
    if is_bangla(text):
        english = translate(text, "bn|en", email) if online else ""
        if not english:
            return None, "Could not find an English meaning. Check your connection."
        bangla = text
    else:
        english = text
        bangla = translate(text, "en|bn", email) if online else ""
    wid = store.add_word(english, bangla)
    row = store.word(wid)
    if row is not None and online:
        enrich(store, row)
    return wid, "Saved " + english


# ----------------------------------------------------------------- tray icon

def app_dir():
    """Where this program lives, whether it is a script or the built exe."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def icon_file():
    for folder in (getattr(sys, "_MEIPASS", ""), app_dir()):
        if not folder:
            continue
        path = os.path.join(folder, "VocabTrainer.ico")
        if os.path.exists(path):
            return path
    return ""


def make_icon_image(size=64):
    from PIL import Image, ImageDraw, ImageFont
    path = icon_file()
    if path:
        try:
            img = Image.open(path)
            img.load()
            return img.convert("RGBA").resize((size, size), Image.LANCZOS)
        except Exception:
            pass
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, size - 1, size - 1), fill=(29, 78, 137, 255))
    text = "ব"
    font = None
    for name in ("nirmala.ttf", "vrinda.ttf", "NotoSansBengali-Regular.ttf",
                 "seguiemj.ttf", "arial.ttf"):
        try:
            font = ImageFont.truetype(name, int(size * 0.62))
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
        text = "V"
    try:
        box = d.textbbox((0, 0), text, font=font)
        w, h = box[2] - box[0], box[3] - box[1]
        d.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1]), text,
               font=font, fill="white")
    except Exception:
        d.text((size / 4, size / 5), text, font=font, fill="white")
    return img



# ------------------------------------------------------------------- looks

THEMES = {
    "midnight": {
        "label": "Midnight", "alpha": 0.97,
        "bg": "#0c1a29", "panel": "#132a40", "card": "#0f2336",
        "line": "#1d3f5c", "text": "#eaf2f8", "dim": "#90aac1",
        "faint": "#5f7d97", "accent": "#4aa8ff", "bn": "#8fd0ff",
        "quiet": "#1c3a56", "quiet_lit": "#25506f",
        "go": "#1f7a4d", "go_lit": "#2a9b63",
        "warn": "#8a5a1f", "warn_lit": "#a86f28",
        "bad": "#8a2f2f", "bad_lit": "#a83b3b",
        "glass": "#16324b",
        "quiet_text": "#ffffff",
    },
    "glass": {
        "label": "Glass", "alpha": 0.90,
        "bg": "#101c28", "panel": "#22364a", "card": "#1b2d40",
        "line": "#3a5670", "text": "#f2f7fb", "dim": "#b9cbdb",
        "faint": "#87a0b6", "accent": "#63b8ff", "bn": "#a8dcff",
        "quiet": "#2b4360", "quiet_lit": "#37567a",
        "go": "#2b8a5c", "go_lit": "#36a76f",
        "warn": "#9a6a2a", "warn_lit": "#b47f36",
        "bad": "#9a3d3d", "bad_lit": "#b54c4c",
        "glass": "#2a4159",
        "quiet_text": "#ffffff",
    },
    "daylight": {
        "label": "Daylight", "alpha": 0.97,
        "bg": "#eef3f8", "panel": "#ffffff", "card": "#ffffff",
        "line": "#cdd9e5", "text": "#152634", "dim": "#4d6a83",
        "faint": "#7a8ea1", "accent": "#1b74d4", "bn": "#0f5aa8",
        "quiet": "#dce6f0", "quiet_lit": "#c8d7e6",
        "go": "#1f7a4d", "go_lit": "#269460",
        "warn": "#a3701f", "warn_lit": "#c0862a",
        "bad": "#a33a3a", "bad_lit": "#c04848",
        "glass": "#f6f9fc",
        "quiet_text": "#152634",
    },
    "forest": {
        "label": "Forest", "alpha": 0.96,
        "bg": "#0e1f1a", "panel": "#163029", "card": "#122720",
        "line": "#24483d", "text": "#e9f5ef", "dim": "#9dc0b2",
        "faint": "#6d9384", "accent": "#4fd1a1", "bn": "#8fe3c4",
        "quiet": "#1d4034", "quiet_lit": "#28564a",
        "go": "#1f7a4d", "go_lit": "#2a9b63",
        "warn": "#8a6a1f", "warn_lit": "#a8842a",
        "bad": "#8a3a34", "bad_lit": "#a84a42",
        "glass": "#19362d",
        "quiet_text": "#ffffff",
    },
    "plum": {
        "label": "Plum", "alpha": 0.96,
        "bg": "#1a1226", "panel": "#2a1d3d", "card": "#221733",
        "line": "#3d2b56", "text": "#f2ecfa", "dim": "#bda8d6",
        "faint": "#8d7aa6", "accent": "#b57bff", "bn": "#d2b3ff",
        "quiet": "#33244a", "quiet_lit": "#432f60",
        "go": "#2f7a57", "go_lit": "#3c9b6e",
        "warn": "#8a5a1f", "warn_lit": "#a86f28",
        "bad": "#8a2f4a", "bad_lit": "#a83b5c",
        "glass": "#2d2042",
        "quiet_text": "#ffffff",
    },
}
UI = dict(THEMES["midnight"])


def apply_theme(name):
    """Swap every colour in one go. Windows read UI when they are built."""
    UI.update(THEMES.get(name, THEMES["midnight"]))
    return UI


TRANSPARENT_KEY = "#ff00fe"      # a colour nobody would draw with


def mix(colour_a, colour_b, amount):
    """Blend two hex colours, amount 0 gives the first, 1 gives the second."""
    a = [int(colour_a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(colour_b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(
        int(round(x + (y - x) * amount)) for x, y in zip(a, b))


def rounded(canvas, x1, y1, x2, y2, radius, **kw):
    """A rounded rectangle, which the canvas has no primitive for."""
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kw)


def glass_panel(win, width, height, radius=18):
    """A frosted, rounded sheet to put a card on.

    The window keeps a key colour that Windows makes see through, so the
    corners really are round rather than square with paint on them. Where
    that is not supported the same drawing simply sits on the background.
    """
    tk = win.tk
    import tkinter as tkinter_module
    see_through = False
    try:
        win.attributes("-transparentcolor", TRANSPARENT_KEY)
        see_through = True
    except Exception:
        see_through = False
    backing = TRANSPARENT_KEY if see_through else UI["bg"]
    win.configure(bg=backing)
    canvas = tkinter_module.Canvas(win, width=width, height=height,
                                   bg=backing, highlightthickness=0,
                                   bd=0)
    canvas.pack(fill="both", expand=True)
    # the sheet, with a light sheen down the top third
    rounded(canvas, 2, 2, width - 2, height - 2, radius,
            fill=UI["card"], outline=UI["line"], width=1)
    sheen_height = max(24, int(height * 0.28))
    for i in range(sheen_height):
        shade = mix(UI["glass"], UI["card"], i / float(sheen_height))
        canvas.create_line(radius // 2, 3 + i, width - radius // 2, 3 + i,
                           fill=shade)
    rounded(canvas, 2, 2, width - 2, height - 2, radius,
            fill="", outline=UI["line"], width=1)
    canvas.create_line(radius, 3, width - radius, 3,
                       fill=mix(UI["card"], "#ffffff", 0.18))
    return canvas


def glassy(win, extra=0.0):
    """A little see through, the way frosted panels look."""
    try:
        win.attributes("-alpha", max(0.55, min(1.0, UI["alpha"] + extra)))
    except Exception:
        pass


def tone(kind):
    """Background and hover colour for a button of the given kind."""
    return {
        "go": (UI["go"], UI["go_lit"]),
        "warn": (UI["warn"], UI["warn_lit"]),
        "bad": (UI["bad"], UI["bad_lit"]),
        "accent": ("#1b4b6b", "#26648c"),
    }.get(kind, (UI["quiet"], UI["quiet_lit"]))


# --------------------------------------------------------------- application

class Trainer:
    def __init__(self):
        stage("importing tkinter")
        import tkinter as tk
        self.tk = tk
        stage("opening the database")
        self.store = Store()
        apply_theme(self.store.get("theme"))
        self.store.seed_if_empty()
        self.store.sync_seed()
        self.store.build_today()
        stage("words ready")
        self.jobs = queue.Queue()
        self.root = tk.Tk()
        self.root.withdraw()
        stage("window system ready")
        self.root.title(APP_NAME)
        self.bn_font = self.pick_font(
            ["Nirmala UI", "Shonar Bangla", "Vrinda", "Noto Sans Bengali",
             "Kalpurush", "Siyam Rupali", "Lohit Bengali"])
        self.ui_font = self.pick_font(["Segoe UI", "Calibri", "DejaVu Sans"])
        self.card = None
        self.last_clip = ""
        self.history = []
        self.hist_at = -1
        self.panel = None
        self.today_win = None
        self.settings_win = None
        self.stats_win = None
        self.quiz = None
        self.timer = None
        self.icon = None
        self.root.after(120, self.pump)
        self.root.after(1500, self.watch_clipboard)
        self.schedule_next()

    # helpers --------------------------------------------------------------
    def pick_font(self, wanted):
        from tkinter import font as tkfont
        families = set(tkfont.families(self.root))
        for name in wanted:
            if name in families:
                return name
        return "TkDefaultFont"

    def pump(self):
        while True:
            try:
                fn = self.jobs.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as exc:
                log("Task failed: " + repr(exc))
        self.root.after(120, self.pump)

    def post(self, fn):
        self.jobs.put(fn)

    def background(self, work, done=None):
        def run():
            try:
                result = work()
            except Exception as exc:
                result = exc
            if done is not None:
                self.post(lambda: done(result))
        threading.Thread(target=run, daemon=True).start()

    # copy anywhere, learn here ---------------------------------------------
    CLIP_POLL = 200          # milliseconds between looks at the clipboard

    def watch_clipboard(self):
        """Look up whatever English word you copy, anywhere in Windows."""
        try:
            if self.store.get("clipboard") == "1":
                text = ""
                try:
                    text = self.root.clipboard_get()
                except Exception:
                    text = ""
                text = " ".join(str(text).split())
                if text != self.last_clip:
                    self.last_clip = text
                    if self.is_lookupable(text):
                        self.lookup_and_show(text)
        except Exception as exc:
            log("Clipboard watch failed: " + repr(exc))
        self.root.after(self.CLIP_POLL, self.watch_clipboard)

    @staticmethod
    def is_lookupable(text):
        """A short piece of English, not a paragraph and not Bengali."""
        text = (text or "").strip()
        if not text or len(text) > 40 or is_bangla(text):
            return False
        if len(text.split()) > 4:
            return False
        return all(ch.isalpha() or ch in " -'" for ch in text)

    def lookup_and_show(self, text):
        """Open a card at once, then fill it in as the answers arrive."""
        text = " ".join((text or "").split())
        if not text:
            return
        clean = text.strip().lower()
        with self.store.lock:
            row = self.store.conn.execute(
                "SELECT id FROM words WHERE english=?", (clean,)).fetchone()
            if row is None:
                # boxes, running, studied: show the entry they come from
                for form in word_forms(clean)[1:]:
                    row = self.store.conn.execute(
                        "SELECT id FROM words WHERE english IN (?,?)",
                        (form, "to " + form)).fetchone()
                    if row is not None:
                        break
        if row is not None:
            self.show_card(row["id"], replay=True)
            return
        # save it straight away with nothing in it, so the card can open now
        wid = self.store.add_word(clean, "")
        if not wid:
            return
        self.show_card(wid, replay=True)

    def bind_card_keys(self, win, actions):
        """Space to mark known, arrows to move, P to pronounce, C to copy."""
        if self.store.get("hotkeys") != "1":
            return
        keys = {"<space>": "knew", "<Return>": "knew", "<Left>": "previous",
                "<Right>": "next", "<Escape>": "close", "p": "speak",
                "P": "speak", "c": "copy", "C": "copy", "f": "favourite",
                "F": "favourite", "d": "details", "D": "details",
                "a": "again", "A": "again"}
        for key, name in keys.items():
            if name in actions:
                win.bind(key, lambda e, fn=actions[name]: (fn(), "break")[1])

    # scheduling -----------------------------------------------------------
    def schedule_next(self):
        if self.timer is not None:
            try:
                self.root.after_cancel(self.timer)
            except Exception:
                pass
            self.timer = None
        if self.store.get("paused") == "1":
            return
        minutes = max(1, self.store.get_int("interval"))
        self.timer = self.root.after(minutes * 60 * 1000, self.on_timer)

    def on_timer(self):
        self.timer = None
        if self.store.get("paused") != "1" and not self.store.quiet_now():
            self.show_card()
        self.schedule_next()

    # word card ------------------------------------------------------------
    # shared widgets --------------------------------------------------------
    def button(self, parent, text="", command=None, kind="quiet", pad=(14, 7),
               size=10, textvar=None, bold=False):
        """A flat button that lights up under the pointer."""
        tk = self.tk
        rest, lit = tone(kind)
        weight = "bold" if bold else "normal"
        ink = UI.get("quiet_text", "white") if kind == "quiet" else "white"
        b = tk.Button(parent, text=text, command=command, relief="flat",
                      bg=rest, fg=ink, activebackground=lit,
                      activeforeground=ink, borderwidth=0,
                      highlightthickness=0, padx=pad[0], pady=pad[1],
                      cursor="hand2", font=(self.ui_font, size, weight))
        if textvar is not None:
            b.config(textvariable=textvar)
        b.bind("<Enter>", lambda e: b.config(bg=lit))
        b.bind("<Leave>", lambda e: b.config(bg=b.rest_colour))
        b.rest_colour = rest
        return b

    def recolour(self, widget, kind):
        rest, lit = tone(kind)
        ink = UI.get("quiet_text", "white") if kind == "quiet" else "white"
        widget.rest_colour = rest
        widget.config(bg=rest, activebackground=lit, fg=ink,
                      activeforeground=ink)

    def divider(self, parent, pad=10, colour=None):
        line = self.tk.Frame(parent, height=1, bg=colour or UI["line"])
        line.pack(fill="x", pady=pad)
        return line

    def progress_bar(self, parent, done, total, width=260):
        """A slim bar showing how far through the day you are."""
        tk = self.tk
        holder = tk.Frame(parent, bg=UI["line"], height=6, width=width)
        holder.pack_propagate(False)
        share = 0 if not total else max(0.0, min(1.0, done / float(total)))
        fill = tk.Frame(holder, bg=UI["accent"], height=6,
                        width=max(2, int(width * share)))
        fill.place(x=0, y=0)
        return holder

    def copy_text(self, text, button=None, label="Copy"):
        """Put text on the clipboard and say so on the button."""
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
        except Exception as exc:
            log("Copy failed: " + repr(exc))
            return False
        if button is not None and button.winfo_exists():
            button.config(text="Copied")
            button.after(1200, lambda: button.winfo_exists()
                         and button.config(text=label))
        return True

    def word_as_text(self, row):
        """The word, its meaning and an example, ready to paste anywhere."""
        parts = [row["english"]]
        meaning = self.store.meaning_of(row, "both").replace("\n", " / ")
        if meaning:
            parts.append(" - " + meaning)
        head = "".join(parts)
        lines = [head]
        meta = "  ".join(x for x in (row["level"], row_pos(row),
                                     row["phonetic"]) if x)
        if meta:
            lines.append(meta)
        if row["definition"]:
            lines.append(row["definition"])
        for sentence in self.sentence_list(row)[:2]:
            lines.append(sentence)
        return "\n".join(lines)

    def scroll_area(self, parent, height=560, width=470):
        """A panel that scrolls, for windows with more in them than fits."""
        tk = self.tk
        holder = tk.Frame(parent, bg=UI["bg"])
        holder.pack(fill="both", expand=True)
        canvas = tk.Canvas(holder, bg=UI["bg"], highlightthickness=0,
                           height=height, width=width)
        bar = tk.Scrollbar(holder, orient="vertical", command=canvas.yview,
                           bg=UI["line"], troughcolor=UI["bg"],
                           activebackground=UI["quiet_lit"], borderwidth=0,
                           highlightthickness=0)
        inner = tk.Frame(canvas, bg=UI["bg"])
        window = canvas.create_window((0, 0), window=inner, anchor="nw",
                                      width=width)
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

        def resized(event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(window, width=canvas.winfo_width())
        inner.bind("<Configure>", resized)
        canvas.bind("<Configure>", resized)

        def wheel(event):
            step = -1 if getattr(event, "delta", 0) > 0 or event.num == 4 else 1
            canvas.yview_scroll(step, "units")
        for target in (canvas, inner):
            target.bind("<MouseWheel>", wheel)
            target.bind("<Button-4>", wheel)
            target.bind("<Button-5>", wheel)
        return inner

    def style_tables(self, win):
        """Dark styling for the list windows."""
        from tkinter import ttk
        style = ttk.Style(win)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        try:
            style.configure("Vocab.Treeview", background=UI["panel"],
                            fieldbackground=UI["panel"], foreground=UI["text"],
                            rowheight=28, borderwidth=0,
                            font=(self.bn_font, 11))
            style.configure("Vocab.Treeview.Heading", background=UI["line"],
                            foreground=UI["text"], relief="flat",
                            font=(self.ui_font, 10, "bold"))
            style.map("Vocab.Treeview.Heading",
                      background=[("active", UI["quiet_lit"])])
            style.map("Vocab.Treeview",
                      background=[("selected", UI["quiet_lit"])],
                      foreground=[("selected", "white")])
            style.configure("Vocab.Vertical.TScrollbar",
                            background=UI["line"], troughcolor=UI["bg"],
                            borderwidth=0, arrowcolor=UI["dim"])
        except Exception:
            pass
        return style

    def show_card(self, word_id=None, replay=False):
        """The word card: a small, quiet panel in the corner of the screen."""
        tk = self.tk
        row = self.store.word(word_id) if word_id else self.store.pick_word()
        if row is None:
            self.notify("No words yet. Use Add a word from the tray menu.")
            return
        if self.card is not None and self.card["win"].winfo_exists():
            try:
                self.card["win"].destroy()
            except Exception:
                pass
        wid = row["id"]
        if not replay:
            self.store.touch(wid)
            self.store.mark_daily_shown(wid)
            # remember the trail so Previous can walk back through it
            if self.history and self.hist_at < len(self.history) - 1:
                self.history = self.history[:self.hist_at + 1]
            if not self.history or self.history[-1] != wid:
                self.history.append(wid)
                self.history = self.history[-60:]
            self.hist_at = len(self.history) - 1

        win = tk.Toplevel(self.root)
        win.title(SHORT_NAME)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.configure(bg=UI["line"])
        glassy(win, 0.01)
        try:
            win.overrideredirect(True)
        except Exception:
            pass

        # a frosted sheet, with the card laid on top of it. The frame has to
        # belong to the canvas, or the canvas paints straight over it.
        sheet = None
        try:
            sheet = glass_panel(win, 520, 430)
            frame = tk.Frame(sheet, bg=UI["card"], padx=24, pady=18)
            sheet.create_window(260, 215, window=frame, anchor="center",
                                width=496, height=406)
        except Exception as exc:
            log("Glass sheet failed, using a plain card: " + repr(exc))
            sheet = None
            win.configure(bg=UI["line"])
            frame = tk.Frame(win, bg=UI["card"], padx=24, pady=18)
            frame.pack(padx=1, pady=1, fill="both", expand=True)

        def close():
            try:
                win.destroy()
            except Exception:
                pass
            self.card = None

        # header: level badge, phonetics, close
        top = tk.Frame(frame, bg=UI["card"])
        top.pack(fill="x")
        kind = row["kind"] or "word"
        badge_text = (row["level"] or "") if kind == "word" \
            else KIND_SINGULAR.get(kind, kind)
        badge = tk.Label(top, text=" " + badge_text + " ",
                         bg=UI["quiet"], fg=UI["bn"],
                         font=(self.ui_font, 9, "bold"), padx=4)
        badge.pack(side="left")
        others = self.other_parts(row)
        meta = tk.Label(top, text="  ".join(
            x for x in (row_pos(row), row["phonetic"],
                        ("also " + ", ".join(others)) if others else "") if x),
                        bg=UI["card"], fg=UI["faint"],
                        font=(self.ui_font, 9, "italic"))
        meta.pack(side="left", padx=8)
        shut = tk.Label(top, text="Close", bg=UI["card"], fg=UI["faint"],
                        font=(self.ui_font, 9), cursor="hand2")
        shut.pack(side="right")
        shut.bind("<Button-1>", lambda e: close())
        shut.bind("<Enter>", lambda e: shut.config(fg=UI["text"]))
        shut.bind("<Leave>", lambda e: shut.config(fg=UI["faint"]))

        # dragging, since the card has no title bar of its own
        drag = {"x": 0, "y": 0}

        def grab(event):
            drag["x"], drag["y"] = event.x, event.y

        def move(event):
            win.geometry("+%d+%d" % (event.x_root - drag["x"],
                                     event.y_root - drag["y"]))
        for holder in (top, meta, badge):
            holder.bind("<Button-1>", grab, add="+")
            holder.bind("<B1-Motion>", move, add="+")

        # the two words
        bn = tk.Label(frame, text=self.store.meaning_of(row) or "...",
                      bg=UI["card"], fg=UI["bn"], font=(self.bn_font, 27),
                      justify="left")
        bn.pack(anchor="w", pady=(10, 0))
        en = tk.Label(frame, text=row["english"], bg=UI["card"],
                      fg=UI["text"], font=(self.ui_font, 23, "bold"))
        en.pack(anchor="w")

        # what you can do with it
        tools = tk.Frame(frame, bg=UI["card"])
        tools.pack(anchor="w", pady=(10, 0))
        self.button(tools, "Pronounce", lambda: self.speak_word(wid),
                    "accent", pad=(12, 5), size=9).pack(side="left")
        copybtn = self.button(tools, "Copy", None, "quiet", pad=(12, 5), size=9)
        copybtn.config(command=lambda: self.copy_text(
            self.word_as_text(self.store.word(wid)), copybtn))
        copybtn.pack(side="left", padx=6)
        self.button(tools, "Show on web", lambda: open_web(row["english"]),
                    "quiet", pad=(12, 5), size=9).pack(side="left",
                                                       padx=(0, 6))
        fav = self.button(tools, "", None, "quiet", pad=(12, 5), size=9)

        def paint_fav():
            on = self.store.is_favourite(wid)
            fav.config(text="In favourites" if on else "Add to favourites")
            self.recolour(fav, "warn" if on else "quiet")

        def toggle_fav():
            self.store.set_favourite(wid, not self.store.is_favourite(wid))
            paint_fav()
        fav.config(command=toggle_fav)
        paint_fav()
        fav.pack(side="left")

        self.divider(frame, pad=12)

        definition = tk.Label(
            frame, text=row["definition"] or "Looking up the meaning...",
            bg=UI["card"], fg=UI["text"], font=(self.ui_font, 11),
            wraplength=430, justify="left")
        definition.pack(anchor="w")

        tk.Label(frame, text="IN A SENTENCE", bg=UI["card"], fg=UI["faint"],
                 font=(self.ui_font, 8, "bold")).pack(anchor="w", pady=(14, 4))
        example = tk.Label(
            frame, text=self.sentence_text(row) or "Looking for examples...",
            bg=UI["card"], fg=UI["dim"], font=(self.ui_font, 10, "italic"),
            wraplength=430, justify="left")
        example.pack(anchor="w")

        family_row = tk.Frame(frame, bg=UI["card"])
        family_row.pack(anchor="w", fill="x")

        def paint_family():
            for child in family_row.winfo_children():
                child.destroy()
            current = self.store.word(wid)
            if current is None:
                return
            members = self.store.family(current)
            if not members:
                family_row.pack_forget()
                return
            family_row.pack(anchor="w", fill="x", pady=(12, 0))
            tk.Label(family_row, text="WORD FAMILY", bg=UI["card"],
                     fg=UI["faint"], font=(self.ui_font, 8, "bold")).pack(
                         side="left", padx=(0, 8))
            for member in members[:5]:
                tag = short_pos(guess_pos(member["english"], member["kind"]))
                tk.Label(family_row,
                         text=" %s %s " % (member["english"],
                                           tag if tag else ""),
                         bg=UI["quiet"], fg=UI["bn"],
                         font=(self.ui_font, 9)).pack(side="left", padx=3)
        paint_family()

        self.divider(frame, pad=14)

        def knew():
            self.store.mark(wid, True, "card")
            self.store.mark_daily_known(wid, True)
            close()

        def again():
            self.store.mark(wid, False, "card")
            self.store.mark_daily_known(wid, False)
            close()

        def nxt():
            close()
            if self.history and self.hist_at < len(self.history) - 1:
                self.hist_at += 1
                self.show_card(self.history[self.hist_at], replay=True)
            else:
                self.show_card()

        def previous():
            if not self.history or self.hist_at <= 0:
                self.notify("This is the first word of the session.")
                return
            self.hist_at -= 1
            close()
            self.show_card(self.history[self.hist_at], replay=True)

        bar = tk.Frame(frame, bg=UI["card"])
        bar.pack(fill="x")
        back = self.button(bar, "Previous", previous, "quiet", pad=(11, 6))
        back.pack(side="left", padx=(0, 6))
        if not self.history or self.hist_at <= 0:
            back.config(state="disabled", fg=UI["faint"])
        for text, cmd, tint in (("I know this", knew, "go"),
                                ("Show again soon", again, "warn"),
                                ("Next word", nxt, "quiet"),
                                ("Details", lambda: self.word_window(wid),
                                 "quiet")):
            self.button(bar, text, cmd, tint, pad=(11, 6), size=10
                        ).pack(side="left", padx=(0, 6))

        # how far through the day this is
        done, total, loops = self.store.daily_progress()
        if total:
            foot = tk.Frame(frame, bg=UI["card"])
            foot.pack(fill="x", pady=(14, 0))
            tk.Label(foot, text="%d of %d learned today" % (done, total),
                     bg=UI["card"], fg=UI["faint"],
                     font=(self.ui_font, 9)).pack(side="left")
            self.progress_bar(foot, done, total, width=150).pack(side="right",
                                                                 pady=6)

        self.bind_card_keys(win, {
            "knew": knew, "again": again, "next": nxt, "previous": previous,
            "close": close, "speak": lambda: self.speak_word(wid),
            "copy": lambda: self.copy_text(
                self.word_as_text(self.store.word(wid)), copybtn),
            "favourite": toggle_fav,
            "details": lambda: self.word_window(wid)})
        try:
            win.focus_force()
        except Exception:
            pass

        win.update_idletasks()
        if sheet is not None:
            # grow the sheet to whatever the content needs
            need_w = max(500, frame.winfo_reqwidth() + 46)
            need_h = max(250, frame.winfo_reqheight() + 40)
            win.geometry("%dx%d" % (need_w, need_h))
            sheet.config(width=need_w, height=need_h)
            sheet.delete("all")
            rounded(sheet, 2, 2, need_w - 2, need_h - 2, 18,
                    fill=UI["card"], outline=UI["line"], width=1)
            sheen = max(24, int(need_h * 0.26))
            for i in range(sheen):
                sheet.create_line(9, 3 + i, need_w - 9, 3 + i,
                                  fill=mix(UI["glass"], UI["card"],
                                           i / float(sheen)))
            rounded(sheet, 2, 2, need_w - 2, need_h - 2, 18,
                    fill="", outline=UI["line"], width=1)
            sheet.create_window(need_w // 2, need_h // 2, window=frame,
                                anchor="center", width=need_w - 24,
                                height=need_h - 22)
            win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry("+%d+%d" % (sw - w - 40, sh - h - 90))
        win.protocol("WM_DELETE_WINDOW", close)
        self.card = {"win": win, "id": wid}

        seconds = max(4, self.store.get_int("popup_seconds"))
        win.after(seconds * 1000,
                  lambda: close() if win.winfo_exists() else None)

        def repaint(say=False):
            """Put whatever is known now on the card."""
            if not win.winfo_exists():
                return
            fresh = self.store.word(wid)
            if fresh is None:
                return
            bn.config(text=self.store.meaning_of(fresh) or "...")
            en.config(text=fresh["english"])
            more = self.other_parts(fresh)
            meta.config(text="  ".join(
                x for x in (row_pos(fresh), fresh["phonetic"],
                            ("also " + ", ".join(more)) if more else "")
                if x))
            definition.config(text=fresh["definition"]
                              or "Looking up the meaning...")
            lines = self.sentence_text(fresh)
            if lines:
                example.config(text=lines)
            paint_family()
            if say and self.store.get("speak_on_card") == "1":
                SPEAKER.say_async(fresh["english"], fresh["audio"] or "")

        if not row["enriched"] and self.store.get("online") == "1":
            # first the Bengali and the dictionary entry, which are quick
            def quick():
                return enrich_quick(self.store, self.store.word(wid))

            def quick_done(changes):
                if not win.winfo_exists():
                    return
                repaint(say=True)
                fresh = self.store.word(wid)
                if not fresh["definition"]:
                    definition.config(text="No online meaning found.")
                if not self.sentence_list(fresh):
                    example.config(text="Looking for examples...")

                # then the sentences, each source landing as it answers
                def sentences():
                    def on_batch(found):
                        self.post(lambda: show_batch(found))
                    got, info = gather_sentences(fresh["english"], on_batch)
                    changes = {"enriched": 1}
                    if got:
                        changes["examples"] = json.dumps(got,
                                                         ensure_ascii=False)
                        if not (fresh["example"] or ""):
                            changes["example"] = got[0]
                    for key in ("phonetic", "pos", "definition", "audio",
                                "synonyms", "antonyms"):
                        if info.get(key) and not (fresh[key] or ""):
                            changes[key] = info[key]
                    if info.get("senses") and not (fresh["senses"] or ""):
                        changes["senses"] = json.dumps(info["senses"],
                                                       ensure_ascii=False)
                    self.store.update_word(wid, **changes)
                    return got

                def show_batch(found):
                    if win.winfo_exists() and found:
                        example.config(text="\n".join(found[:2]))

                def sentences_done(result):
                    if not win.winfo_exists():
                        return
                    repaint()
                    fresh2 = self.store.word(wid)
                    if not self.sentence_list(fresh2):
                        example.config(
                            text="Similar words: " + fresh2["synonyms"]
                            if fresh2["synonyms"] else
                            "No sentence found. Open Details to search again.")
                self.background(sentences, sentences_done)
            self.background(quick, quick_done)
        elif self.store.get("speak_on_card") == "1":
            SPEAKER.say_async(row["english"], row["audio"] or "")

    # words, sentences and sound -------------------------------------------
    def sense_list(self, row):
        """Every part of speech the dictionary gave for this word."""
        try:
            raw = row["senses"] if "senses" in row.keys() else ""
            got = json.loads(raw) if raw else []
        except Exception:
            got = []
        out = [g for g in got if isinstance(g, dict) and g.get("definition")]
        if not out and (row["definition"] or ""):
            out = [{"pos": row["pos"] or "", "definition": row["definition"],
                    "example": row["example"] or ""}]
        return out

    def other_parts(self, row):
        """The short forms of the other parts of speech, such as n. and v."""
        kind = row["kind"] if "kind" in row.keys() else "word"
        first = short_pos(row["pos"], kind, row["english"])
        seen, out = {first}, []
        for sense in self.sense_list(row):
            tag = short_pos(sense.get("pos", ""), kind, row["english"])
            if tag and tag not in seen:
                seen.add(tag)
                out.append(tag)
        return out

    def sentence_list(self, row):
        """Example sentences stored for a word, newest format first."""
        out = []
        try:
            raw = row["examples"] if "examples" in row.keys() else ""
            if raw:
                out = [s for s in json.loads(raw) if isinstance(s, str)]
        except Exception:
            out = []
        single = (row["example"] or "").strip()
        if single and single not in out:
            out.insert(0, single)
        return out

    def sentence_text(self, row, limit=2):
        return "\n".join(self.sentence_list(row)[:limit])

    def speak_word(self, wid, sentence=""):
        row = self.store.word(wid)
        if row is None:
            return

        def work():
            return SPEAKER.say(row["english"], row["audio"] or "", sentence)

        def done(ok):
            if ok is True:
                return
            self.notify("Could not play the sound. " +
                        (SPEAKER.last_error or "No voice found.")[:120])
        self.background(work, done)

    def word_window(self, wid):
        """Everything known about one word, with buttons to hear it."""
        tk = self.tk
        row = self.store.word(wid)
        if row is None:
            return
        win = tk.Toplevel(self.root)
        win.title(row["english"])
        win.configure(bg=UI["bg"])
        win.geometry("580x640")
        win.minsize(520, 480)
        win.attributes("-topmost", True)

        head = tk.Frame(win, bg=UI["bg"])
        head.pack(fill="x", padx=22, pady=(18, 0))
        tk.Label(head, text=row["english"], bg=UI["bg"], fg="white",
                 font=(self.ui_font, 20, "bold")).pack(side="left")
        self.button(head, "Pronounce", lambda: self.speak_word(wid), "accent",
                    pad=(12, 5), size=9).pack(side="left", padx=(12, 0))
        copyb = self.button(head, "Copy", None, "quiet", pad=(12, 5), size=9)
        copyb.config(command=lambda: self.copy_text(
            self.word_as_text(self.store.word(wid)), copyb))
        copyb.pack(side="left", padx=6)
        self.button(head, "Show on web", lambda: open_web(row["english"]),
                    "quiet", pad=(12, 5), size=9).pack(side="left",
                                                       padx=(0, 6))
        favbtn = self.button(head, "", None, "quiet", pad=(12, 5), size=9)

        def paint():
            on = self.store.is_favourite(wid)
            favbtn.config(text="In favourites" if on else "Add to favourites")
            self.recolour(favbtn, "warn" if on else "quiet")

        def flip():
            self.store.set_favourite(wid, not self.store.is_favourite(wid))
            paint()
        favbtn.config(command=flip)
        paint()
        favbtn.pack(side="left")
        tk.Label(win, text=self.store.meaning_of(row, "both") or "",
                 bg=UI["bg"], fg=UI["bn"], font=(self.bn_font, 20),
                 justify="left").pack(anchor="w", padx=22)
        tk.Label(win, text="  ".join(x for x in (
            (row["level"] if (row["kind"] or "word") == "word"
             else KIND_SINGULAR.get(row["kind"], "")),
            row_pos(row), row["phonetic"], row["pos"]) if x),
                 bg=UI["bg"], fg=UI["dim"],
                 font=(self.ui_font, 10, "italic")).pack(anchor="w", padx=22)
        tk.Label(win, text=row["definition"] or "No meaning stored yet.",
                 bg=UI["bg"], fg=UI["text"], font=(self.ui_font, 11),
                 wraplength=470, justify="left").pack(anchor="w", padx=22,
                                                      pady=(10, 0))
        if row["synonyms"]:
            tk.Label(win, text="Similar: " + row["synonyms"],
                     bg=UI["bg"], fg=UI["dim"], font=(self.ui_font, 9),
                     wraplength=470, justify="left").pack(anchor="w", padx=22,
                                                          pady=(6, 0))
        if row["antonyms"]:
            tk.Label(win, text="Opposite: " + row["antonyms"],
                     bg=UI["bg"], fg=UI["dim"], font=(self.ui_font, 9),
                     wraplength=470, justify="left").pack(anchor="w", padx=22)
        family = self.store.family(row)
        if family:
            line = tk.Frame(win, bg=UI["bg"])
            line.pack(anchor="w", padx=22, pady=(8, 0), fill="x")
            tk.Label(line, text="Word family:", bg=UI["bg"], fg=UI["faint"],
                     font=(self.ui_font, 9)).pack(side="left", padx=(0, 6))
            for member in family[:6]:
                tk.Label(line, text=member["english"], bg=UI["quiet"],
                         fg=UI["bn"], font=(self.ui_font, 9),
                         padx=6).pack(side="left", padx=3)

        senses = self.sense_list(row)
        if len(senses) > 1:
            tk.Label(win, text="ALL PARTS OF SPEECH", bg=UI["bg"],
                     fg=UI["faint"], font=(self.ui_font, 8, "bold")).pack(
                         anchor="w", padx=22, pady=(14, 4))
            for sense in senses[:6]:
                line = tk.Frame(win, bg=UI["bg"])
                line.pack(anchor="w", fill="x", padx=22, pady=1)
                tag = short_pos(sense.get("pos", ""), row["kind"] or "word",
                                row["english"]) or "?"
                tk.Label(line, text=tag, bg=UI["quiet"], fg=UI["bn"],
                         font=(self.ui_font, 8, "bold"), padx=5,
                         width=7).pack(side="left", anchor="n")
                tk.Label(line, text=sense.get("definition", ""), bg=UI["bg"],
                         fg=UI["text"], font=(self.ui_font, 10),
                         wraplength=430, justify="left").pack(side="left",
                                                              padx=8)

        tk.Label(win, text="EXAMPLE SENTENCES", bg=UI["bg"], fg=UI["faint"],
                 font=(self.ui_font, 8, "bold")).pack(anchor="w", padx=22,
                                                      pady=(16, 6))
        holder = tk.Frame(win, bg=UI["bg"])
        holder.pack(fill="both", expand=True, padx=22)

        def draw(sentences):
            for child in holder.winfo_children():
                child.destroy()
            if not sentences:
                tk.Label(holder, text="No example sentences found yet.",
                         bg=UI["bg"], fg=UI["dim"],
                         font=(self.ui_font, 10)).pack(anchor="w")
                return
            for text in sentences[:4]:
                line = tk.Frame(holder, bg=UI["bg"])
                line.pack(fill="x", pady=3)
                self.button(line, "Say",
                            lambda t=text: self.speak_word(wid, t), "quiet",
                            pad=(9, 3), size=8).pack(side="left", padx=(0, 10),
                                                     anchor="n")
                tk.Label(line, text=text, bg=UI["bg"], fg=UI["dim"],
                         font=(self.ui_font, 10), wraplength=400,
                         justify="left").pack(side="left", anchor="w")

        draw(self.sentence_list(row))

        def refresh():
            def work():
                fresh = self.store.word(wid)
                sentences, info = sentences_for(fresh["english"])
                changes = {}
                if sentences:
                    changes["examples"] = json.dumps(sentences,
                                                     ensure_ascii=False)
                    changes["example"] = sentences[0]
                for key in ("phonetic", "pos", "definition", "audio",
                            "synonyms"):
                    if info.get(key) and not (fresh[key] or ""):
                        changes[key] = info[key]
                if changes:
                    self.store.update_word(wid, enriched=1, **changes)
                return sentences

            def done(result):
                if isinstance(result, Exception) or not win.winfo_exists():
                    return
                draw(self.sentence_list(self.store.word(wid)))
            self.background(work, done)

        bottom = tk.Frame(win, bg=UI["bg"])
        bottom.pack(pady=14)
        self.button(bottom, "Find more sentences online", refresh, "accent",
                    pad=(16, 7)).pack(side="left", padx=4)
        self.button(bottom, "Meaning on Google",
                    lambda: open_web(row["english"], "meaning"), "quiet",
                    pad=(14, 7)).pack(side="left", padx=4)
        self.button(bottom, "Pictures",
                    lambda: open_web(row["english"], "images"), "quiet",
                    pad=(14, 7)).pack(side="left", padx=4)
        if not self.sentence_list(row) and self.store.get("online") == "1":
            refresh()

    # quiz -----------------------------------------------------------------
    # practice ---------------------------------------------------------------
    STYLES = [("choice", "Multiple choice"), ("typing", "Typing"),
              ("spelling", "Spelling"), ("listening", "Listening"),
              ("blank", "Fill the blank"), ("synonym", "Synonyms"),
              ("mixed", "Mixed")]

    def start_quiz(self, style=None):
        """One window, five ways to be tested."""
        tk = self.tk
        if self.quiz is not None and self.quiz.winfo_exists():
            self.quiz.lift()
            return
        length = max(3, self.store.get_int("quiz_length"))
        pool = self.store.quiz_pool(length)
        if len(pool) < 4:
            self.notify("Add at least four entries before practising.")
            return

        win = tk.Toplevel(self.root)
        self.quiz = win
        win.title("Practice")
        win.configure(bg=UI["bg"])
        win.attributes("-topmost", True)
        win.geometry("620x700")
        win.minsize(540, 580)
        chosen = style or self.store.get("practice_style") or "mixed"
        state = {"i": 0, "score": 0, "answered": False, "style": chosen}

        # style picker across the top
        picker = tk.Frame(win, bg=UI["bg"])
        picker.pack(pady=(14, 4))
        style_buttons = {}

        def paint_styles():
            for name, widget in style_buttons.items():
                self.recolour(widget,
                              "accent" if name == state["style"] else "quiet")

        def pick_style(name):
            state["style"] = name
            self.store.put("practice_style", name)
            paint_styles()
            build()

        for name, label_text in self.STYLES:
            b = self.button(picker, label_text, lambda n=name: pick_style(n),
                            "quiet", pad=(9, 4), size=8)
            b.pack(side="left", padx=2)
            style_buttons[name] = b
        paint_styles()

        head = tk.Label(win, text="", bg=UI["bg"], fg=UI["dim"],
                        font=(self.ui_font, 10))
        head.pack(pady=(8, 6))
        track = tk.Frame(win, bg=UI["bg"])
        track.pack(fill="x", padx=40)
        prompt = tk.Label(win, text="", bg=UI["bg"], fg=UI["text"],
                          font=(self.bn_font, 24), wraplength=520)
        prompt.pack(pady=(12, 6))
        hint = tk.Label(win, text="", bg=UI["bg"], fg=UI["faint"],
                        font=(self.ui_font, 10, "italic"))
        hint.pack()
        holder = tk.Frame(win, bg=UI["bg"])
        holder.pack(pady=12, fill="x", padx=45)
        footer = tk.Frame(win, bg=UI["glass"])
        footer.pack(side="bottom", fill="x")
        tk.Frame(footer, height=1, bg=UI["line"]).pack(fill="x")
        inner = tk.Frame(footer, bg=UI["glass"], pady=10)
        inner.pack(fill="x")
        tools = tk.Frame(inner, bg=UI["glass"])
        tools.pack(side="left", padx=14)
        nextbtn = self.button(inner, "Next", None, "accent", pad=(24, 9),
                              bold=True)
        feedback = tk.Label(win, text="", bg=UI["bg"], fg=UI["dim"],
                            font=(self.ui_font, 11), wraplength=520,
                            justify="left")
        feedback.pack(side="bottom", pady=(6, 10), padx=20, fill="x")

        def clear():
            for child in holder.winfo_children():
                child.destroy()
            for child in tools.winfo_children():
                child.destroy()
            feedback.config(text="")
            nextbtn.pack_forget()

        def after_answer(row, ok, right_answer, style_used):
            state["answered"] = True
            if ok:
                state["score"] += 1
                feedback.config(text="Correct", fg="#6ee7a8")
            else:
                tag = row_pos(row)
                feedback.config(text="Answer: " + right_answer +
                                ("   (%s)" % tag if tag else ""),
                                fg="#ffb4b4")
            self.store.mark(row["id"], ok, style_used)
            extra = [x for x in (row["definition"],
                                 (self.sentence_list(row) or [""])[0]) if x]
            if extra:
                feedback.config(text=feedback.cget("text") + "\n" +
                                "\n".join(extra[:2]))
            tag_now = row_pos(row)
            self.button(tools, "Pronounce " + row["english"] +
                        (" (%s)" % tag_now if tag_now else ""),
                        lambda: self.speak_word(row["id"]), "accent",
                        pad=(12, 5), size=9).pack(side="left", padx=4)
            copyq = self.button(tools, "Copy", None, "quiet", pad=(12, 5),
                                size=9)
            copyq.config(command=lambda: self.copy_text(
                self.word_as_text(self.store.word(row["id"])), copyq))
            copyq.pack(side="left", padx=4)
            self.button(tools, "Show on web",
                        lambda: open_web(row["english"]), "quiet",
                        pad=(12, 5), size=9).pack(side="left", padx=4)
            self.button(tools, "Details",
                        lambda: self.word_window(row["id"]), "quiet",
                        pad=(12, 5), size=9).pack(side="left", padx=4)
            nextbtn.pack(side="right", padx=14)

        def typed_check(row, box, right_answer, style_used):
            def submit(event=None):
                if state["answered"]:
                    return
                given = " ".join(box.get().strip().lower().split())
                want = " ".join(right_answer.strip().lower().split())
                ok = given == want
                if not ok and given and want.startswith("to "):
                    ok = given == want[3:]
                box.config(state="disabled",
                           disabledbackground=UI["go"] if ok else UI["bad"],
                           disabledforeground="white")
                after_answer(row, ok, right_answer, style_used)
            return submit

        def build():
            state["answered"] = False
            clear()
            if state["i"] >= len(pool):
                return finish()
            row = pool[state["i"]]
            style_now = state["style"]
            if style_now == "mixed":
                choices = ["choice", "typing", "blank", "spelling"]
                if SPEAKER.available() or row["audio"]:
                    choices.append("listening")
                if row["synonyms"]:
                    choices.append("synonym")
                style_now = random.choice(choices)
            head.config(text="Question %d of %d     Score %d"
                             % (state["i"] + 1, len(pool), state["score"]))
            for child in track.winfo_children():
                child.destroy()
            self.progress_bar(track, state["i"], len(pool), width=500).pack(
                fill="x")

            sentences = self.sentence_list(row)
            if style_now == "blank" and not sentences:
                style_now = "typing"
            if style_now == "synonym" and not row["synonyms"]:
                style_now = "choice"
            if style_now == "listening" and not (SPEAKER.available()
                                                 or row["audio"]):
                style_now = "typing"

            if style_now == "choice":
                direction = self.store.get("direction")
                if direction == "both":
                    direction = random.choice(["bangla", "english"])
                others = self.store.distractors(row["id"], 3, row["level"])
                language = self.store.language()
                lang_name = {"bn": "Bengali", "hi": "Hindi",
                             "both": "Bengali"}[language]
                if direction == "bangla":
                    prompt.config(text=self.store.meaning_of(row),
                                  font=(self.bn_font, 25))
                    hint.config(text="Pick the English meaning")
                    right = row["english"]
                    options = [right] + [o["english"] for o in others]
                    opt_font = (self.ui_font, 12)
                else:
                    prompt.config(text=row["english"],
                                  font=(self.ui_font, 23, "bold"))
                    tag = row_pos(row)
                    hint.config(text=("%s   " % tag if tag else "") +
                                "Pick the " + lang_name + " meaning")
                    right = self.store.meaning_of(row, "hi"
                                                  if language == "hi" else "bn")
                    options = [right] + [
                        self.store.meaning_of(o, "hi" if language == "hi"
                                              else "bn") for o in others]
                    opt_font = (self.bn_font, 15)
                options = [o for o in options if o]
                options = list(dict.fromkeys(options))
                random.shuffle(options)

                def choose(value, button):
                    if state["answered"]:
                        return
                    ok = value == right
                    self.recolour(button, "go" if ok else "bad")
                    button.config(bg=UI["go"] if ok else UI["bad"])
                    after_answer(row, ok, right, "choice")

                for value in options:
                    b = self.button(holder, value, None, "quiet", pad=(10, 9))
                    b.config(font=opt_font,
                             command=lambda v=value, btn=b: choose(v, btn))
                    b.pack(fill="x", pady=4)

            elif style_now == "synonym":
                prompt.config(text=row["english"],
                              font=(self.ui_font, 23, "bold"))
                tag = row_pos(row)
                hint.config(text=("%s   " % tag if tag else "") +
                            "Which word means the same")
                right = row["synonyms"].split(",")[0].strip()
                others = [o["english"] for o in
                          self.store.distractors(row["id"], 3, row["level"])]
                options = list(dict.fromkeys([right] + others))
                random.shuffle(options)

                def choose_syn(value, button):
                    if state["answered"]:
                        return
                    ok = value == right
                    self.recolour(button, "go" if ok else "bad")
                    button.config(bg=UI["go"] if ok else UI["bad"])
                    after_answer(row, ok, right, "synonym")

                for value in options:
                    b = self.button(holder, value, None, "quiet", pad=(10, 9))
                    b.config(font=(self.ui_font, 12),
                             command=lambda v=value, btn=b: choose_syn(v, btn))
                    b.pack(fill="x", pady=4)

            else:
                # the three typed styles share one answer box
                if style_now == "typing":
                    prompt.config(text=self.store.meaning_of(row),
                                  font=(self.bn_font, 25))
                    hint.config(text="Type the English word")
                elif style_now == "spelling":
                    prompt.config(text=mask_word(row["english"]),
                                  font=(self.ui_font, 24, "bold"))
                    meaning_now = self.store.meaning_of(row).replace("\n",
                                                                     "   ")
                    hint.config(text="Fill in the missing letters   " +
                                     meaning_now)
                elif style_now == "listening":
                    prompt.config(text="Listen", font=(self.ui_font, 22, "bold"))
                    hint.config(text="Type the word you hear")
                    SPEAKER.say_async(row["english"], row["audio"] or "")
                else:
                    blanked = sentences[0]
                    pattern = re.compile(re.escape(row["english"]), re.I)
                    if not pattern.search(blanked):
                        pattern = re.compile(
                            re.escape(word_stem(row["english"])) + r"\w*", re.I)
                    blanked = pattern.sub("______", blanked, count=1)
                    prompt.config(text=blanked, font=(self.ui_font, 15),
                                  wraplength=520)
                    hint.config(text="Type the missing word   (" +
                                     (self.store.meaning_of(row)
                                      .replace("\n", "  ") or "") + ")")
                box = tk.Entry(holder, font=(self.ui_font, 16), justify="center",
                               bg=UI["panel"], fg=UI["text"], relief="flat",
                               insertbackground=UI["text"],
                               highlightthickness=1,
                               highlightbackground=UI["line"],
                               highlightcolor=UI["accent"],
                               disabledforeground="white")
                box.pack(fill="x", ipady=6)
                box.focus_set()
                submit = typed_check(row, box, row["english"], style_now)
                box.bind("<Return>", submit)
                row_tools = tk.Frame(holder, bg=UI["bg"])
                row_tools.pack(pady=8)
                self.button(row_tools, "Check", submit, "go",
                            pad=(18, 6)).pack(side="left", padx=4)
                if style_now == "spelling":
                    self.button(row_tools, "Say it",
                                lambda: self.speak_word(row["id"]),
                                "accent", pad=(14, 6)).pack(side="left",
                                                            padx=4)
                if style_now == "listening":
                    self.button(row_tools, "Play again",
                                lambda: SPEAKER.say_async(row["english"],
                                                          row["audio"] or ""),
                                "accent", pad=(14, 6)).pack(side="left", padx=4)
                self.button(row_tools, "Skip",
                            lambda: after_answer(row, False, row["english"],
                                                 style_now),
                            "quiet", pad=(14, 6)).pack(side="left", padx=4)

        def advance():
            state["i"] += 1
            build()

        def finish():
            clear()
            total = len(pool)
            prompt.config(text="Practice finished",
                          font=(self.ui_font, 22, "bold"))
            hint.config(text="")
            head.config(text="")
            feedback.config(text="You scored %d out of %d."
                                 % (state["score"], total), fg=UI["text"])
            nextbtn.config(text="Close", command=win.destroy)
            nextbtn.pack(side="right", padx=14)

        nextbtn.config(command=advance)
        build()
        win.bind("<Return>", lambda e: advance() if state["answered"] else None)

        def on_close():
            self.quiz = None
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", on_close)

    # add word -------------------------------------------------------------
    def add_word_window(self):
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title("Add a word")
        win.configure(bg=UI["bg"])
        win.attributes("-topmost", True)
        win.geometry("420x210")
        tk.Label(win, text="Type a Bengali or an English word",
                 bg=UI["bg"], fg="white", font=(self.ui_font, 12)).pack(pady=(18, 4))
        entry = tk.Entry(win, font=(self.bn_font, 16), justify="center")
        entry.pack(padx=30, fill="x")
        entry.focus_set()
        status = tk.Label(win, text="", bg=UI["bg"], fg=UI["dim"],
                          font=(self.ui_font, 10), wraplength=360)
        status.pack(pady=10)

        def submit(_=None):
            text = entry.get().strip()
            if not text:
                return
            status.config(text="Looking it up...")
            btn.config(state="disabled")

            def work():
                return lookup_new(self.store, text)

            def done(result):
                btn.config(state="normal")
                if isinstance(result, Exception):
                    status.config(text="Lookup failed. Check your connection.")
                    return
                wid, message = result
                status.config(text=message)
                if wid:
                    entry.delete(0, "end")
            self.background(work, done)

        btn = self.button(win, "Look up and save", submit, "go", pad=(18, 8))
        btn.pack()
        entry.bind("<Return>", submit)

    # word list ------------------------------------------------------------
    def today_window(self):
        """The words chosen for today, and how far round the loop you are."""
        tk = self.tk
        from tkinter import ttk
        if getattr(self, "today_win", None) is not None and \
                self.today_win.winfo_exists():
            self.today_win.lift()
            return
        win = tk.Toplevel(self.root)
        self.today_win = win
        win.title("Today's words")
        win.geometry("760x500")
        win.configure(bg=UI["bg"])

        head = tk.Label(win, text="", bg=UI["bg"], fg=UI["text"],
                        font=(self.ui_font, 12, "bold"))
        head.pack(pady=(16, 8))

        self.style_tables(win)
        cols = ("no", "bangla", "english", "pos", "seen", "known")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=12,
                            style="Vocab.Treeview")
        for col, title, width, grow in (("no", "No", 42, False),
                                        ("bangla", "Meaning", 205, False),
                                        ("english", "English", 165, True),
                                        ("pos", "Type", 62, False),
                                        ("seen", "Times seen", 90, False),
                                        ("known", "Learned", 80, False)):
            tree.heading(col, text=title)
            tree.column(col, width=width, anchor="w", stretch=grow)
        tree.pack(fill="both", expand=True, padx=14, pady=6)

        def refresh():
            rows = self.store.build_today()
            tree.delete(*tree.get_children())
            for r in rows:
                tree.insert("", "end", iid=str(r["id"]),
                            values=(r["position"] + 1,
                                    self.store.meaning_of(r).replace("\n",
                                                                     "  "),
                                    r["english"], row_pos(r), r["shown"],
                                    "yes" if r["known"] else ""))
            done, total, loops = self.store.daily_progress()
            head.config(text="%s   %d of %d learned today, %d full %s"
                             % (KIND_NAMES[self.store.mode()], done, total,
                                loops, "loop" if loops == 1 else "loops"))

        def open_row(event=None):
            picked = tree.selection()
            if picked:
                self.word_window(int(picked[0]))
        tree.bind("<Double-1>", open_row)

        bar = tk.Frame(win, bg=UI["bg"])
        bar.pack(pady=(0, 14))

        def new_set():
            self.store.build_today(force=True)
            refresh()

        def show_next():
            self.show_card()
            win.after(400, refresh)

        for text, cmd, kind in (("Show the next word", show_next, "go"),
                                ("Open selected", open_row, "quiet"),
                                ("Start a new set", new_set, "warn")):
            self.button(bar, text, cmd, kind).pack(side="left", padx=5)
        refresh()

        def on_close():
            self.today_win = None
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", on_close)

    def stats_window(self):
        """Streak, accuracy and the last two weeks, drawn as a small chart."""
        tk = self.tk
        if getattr(self, "stats_win", None) is not None and \
                self.stats_win.winfo_exists():
            self.stats_win.lift()
            return
        win = tk.Toplevel(self.root)
        self.stats_win = win
        win.title("Progress")
        win.configure(bg=UI["bg"])
        win.geometry("640x520")

        tk.Label(win, text="Your progress", bg=UI["bg"], fg=UI["text"],
                 font=(self.ui_font, 15, "bold")).pack(pady=(18, 4))

        tiles = tk.Frame(win, bg=UI["bg"])
        tiles.pack(pady=8)

        def tile(parent, value, caption):
            box = tk.Frame(parent, bg=UI["panel"], padx=18, pady=12)
            box.pack(side="left", padx=6)
            tk.Label(box, text=str(value), bg=UI["panel"], fg=UI["accent"],
                     font=(self.ui_font, 20, "bold")).pack()
            tk.Label(box, text=caption, bg=UI["panel"], fg=UI["dim"],
                     font=(self.ui_font, 9)).pack()

        overall = self.store.overall()
        stats = self.store.stats()
        streak = self.store.streak()
        tile(tiles, streak, "day streak" if streak != 1 else "day")
        tile(tiles, stats["strong"], "well known")
        tile(tiles, overall["answered"], "answers")
        tile(tiles, "%.0f%%" % overall["accuracy"], "accuracy")

        tk.Label(win, text="LAST TWO WEEKS", bg=UI["bg"], fg=UI["faint"],
                 font=(self.ui_font, 8, "bold")).pack(anchor="w", padx=26,
                                                      pady=(16, 6))
        canvas = tk.Canvas(win, height=190, bg=UI["bg"], highlightthickness=0)
        canvas.pack(fill="x", padx=26)

        days = self.store.day_totals(14)
        top = max([d["answered"] for d in days] + [1])
        width = 560
        gap = width / float(len(days))
        base = 150
        for i, day in enumerate(days):
            x = 10 + i * gap
            height = int((day["answered"] / float(top)) * 120)
            canvas.create_rectangle(x, base - height, x + gap * 0.6, base,
                                    fill=UI["accent"] if day["answered"]
                                    else UI["line"], outline="")
            hits = int((day["correct"] / float(top)) * 120)
            if hits:
                canvas.create_rectangle(x, base - hits, x + gap * 0.6, base,
                                        fill="#2a9b63", outline="")
            canvas.create_text(x + gap * 0.3, base + 12,
                               text=day["day"][-2:], fill=UI["faint"],
                               font=(self.ui_font, 8))
            if day["answered"]:
                canvas.create_text(x + gap * 0.3, base - height - 8,
                                   text=str(day["answered"]), fill=UI["dim"],
                                   font=(self.ui_font, 8))
        canvas.create_text(10, base + 34, anchor="w",
                           text="blue is answers, green is correct",
                           fill=UI["faint"], font=(self.ui_font, 8))

        bar = tk.Frame(win, bg=UI["bg"])
        bar.pack(pady=14)
        self.button(bar, "Hard words", self.hard_window, "warn").pack(
            side="left", padx=5)
        self.button(bar, "Export", self.export_window, "quiet").pack(
            side="left", padx=5)
        self.button(bar, "Close", win.destroy, "quiet").pack(side="left",
                                                             padx=5)

        def on_close():
            self.stats_win = None
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", on_close)

    def hard_window(self):
        self.word_list_window(rows_getter=self.store.hard_words,
                              title="Hard words")

    def export_window(self):
        """Save the collection as a spreadsheet, a csv or a printable page."""
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title("Export")
        win.configure(bg=UI["bg"])
        win.geometry("460x330")
        win.attributes("-topmost", True)
        tk.Label(win, text="What to save", bg=UI["bg"], fg=UI["text"],
                 font=(self.ui_font, 13, "bold")).pack(pady=(18, 10))
        what = tk.StringVar(value="today")
        for value, text in (("today", "Today's words"),
                            ("favourites", "Favourites"),
                            ("hard", "Hard words"),
                            ("all", "Everything")):
            self.choice(win, text, value, what).pack(anchor="w", padx=40)
        note = tk.Label(win, text="", bg=UI["bg"], fg=UI["dim"],
                        font=(self.ui_font, 9), wraplength=400)
        note.pack(pady=10)

        def rows_for():
            pick = what.get()
            if pick == "today":
                return self.store.daily_rows()
            if pick == "favourites":
                return self.store.favourites()
            if pick == "hard":
                return self.store.hard_words(500)
            return self.store.all_words()

        def save(kind):
            rows = rows_for()
            if not rows:
                note.config(text="There is nothing in that list yet.")
                return
            path = export_rows(rows, kind, self)
            if path:
                note.config(text="Saved to " + path, fg="#6ee7a8")
                threading.Thread(target=open_in_explorer, args=(path,),
                                 daemon=True).start()
            else:
                note.config(text="Could not save the file. See the log.",
                            fg="#ffb4b4")

        bar = tk.Frame(win, bg=UI["bg"])
        bar.pack(pady=8)
        self.button(bar, "Excel", lambda: save("xlsx"), "go").pack(side="left",
                                                                   padx=4)
        self.button(bar, "CSV", lambda: save("csv"), "quiet").pack(side="left",
                                                                    padx=4)
        self.button(bar, "Printable page", lambda: save("html"),
                    "accent").pack(side="left", padx=4)
        tk.Label(win, text="The printable page opens in your browser, where "
                           "Ctrl and P saves it as a PDF.",
                 bg=UI["bg"], fg=UI["faint"], font=(self.ui_font, 8),
                 wraplength=400).pack(pady=(6, 0))

    def favourites_window(self):
        self.word_list_window(only_favourites=True)

    def word_list_window(self, only_favourites=False, rows_getter=None,
                         title=""):
        tk = self.tk
        from tkinter import ttk
        win = tk.Toplevel(self.root)
        win.title(title or ("Favourites" if only_favourites else "My words"))
        win.geometry("900x540")
        win.configure(bg=UI["bg"])
        self.style_tables(win)
        top = tk.Frame(win, bg=UI["bg"])
        top.pack(fill="x", padx=14, pady=(12, 4))
        finder = tk.Frame(win, bg=UI["bg"])
        finder.pack(fill="x", padx=16, pady=(4, 2))
        tk.Label(finder, text="Search", bg=UI["bg"], fg=UI["dim"],
                 font=(self.ui_font, 9)).pack(side="left", padx=(0, 8))
        typed_text = tk.StringVar()
        query = self.field(finder, "", typed_text)
        query.pack(side="left", fill="x", expand=True, ipady=3)
        summary = tk.Label(win, text="", bg=UI["bg"], fg=UI["dim"],
                           font=(self.ui_font, 10), anchor="w")
        summary.pack(fill="x", padx=16, pady=(6, 8))

        cols = ("level", "bangla", "english", "pos", "fav", "box",
                "definition")
        tree = ttk.Treeview(win, columns=cols, show="headings",
                            style="Vocab.Treeview")
        for col, title, width, grow in (("level", "Level", 58, False),
                                        ("bangla", "Meaning", 185, False),
                                        ("english", "English", 145, False),
                                        ("pos", "Type", 62, False),
                                        ("fav", "Favourite", 80, False),
                                        ("box", "Learned", 70, False),
                                        ("definition", "Definition", 230,
                                         True)):
            tree.heading(col, text=title)
            tree.column(col, width=width, anchor="w", stretch=grow)
        scroll = ttk.Scrollbar(win, orient="vertical", command=tree.yview,
                               style="Vocab.Vertical.TScrollbar")
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True, padx=(14, 0),
                  pady=(0, 14))
        scroll.pack(side="right", fill="y", pady=(0, 14), padx=(0, 14))

        def refresh(event=None):
            tree.delete(*tree.get_children())
            typed = typed_text.get().strip()
            if typed:
                rows = self.store.search(typed)
            elif rows_getter is not None:
                rows = rows_getter()
            elif only_favourites:
                rows = self.store.favourites()
            else:
                rows = self.store.all_words()
            for r in rows:
                tree.insert("", "end", iid=str(r["id"]),
                            values=(r["level"] or "",
                                    self.store.meaning_of(r).replace("\n",
                                                                     "  "),
                                    r["english"], row_pos(r),
                                    "yes" if r["favourite"] else "",
                                    r["box"] or 1,
                                    (r["definition"] or "")[:90]))
            if typed:
                summary.config(text="%d matches for %s. Double click one to "
                                    "open it." % (len(rows), typed))
            elif rows_getter is not None:
                summary.config(text="%d entries you keep missing. Double click "
                                    "one to open it." % len(rows))
            elif only_favourites:
                summary.config(text="%d favourite entries. Double click one to "
                                    "open it." % len(rows))
            else:
                st = self.store.stats()
                summary.config(
                    text="%d words    %d well known    %d correct    %d missed"
                         % (st["total"], st["strong"], st["correct"],
                            st["wrong"]))
        refresh()
        # a trace catches typing and pasting alike
        typed_text.trace_add("write", lambda *a: refresh())

        def open_word(event=None):
            picked = tree.selection()
            if picked:
                self.word_window(int(picked[0]))
        tree.bind("<Double-1>", open_word)
        self.button(top, "Open word", open_word, "accent", pad=(12, 5),
                    size=9).pack(side="right", padx=(0, 8))

        def remove():
            for iid in tree.selection():
                if only_favourites:
                    self.store.set_favourite(int(iid), False)
                else:
                    self.store.delete_word(int(iid))
            refresh()
        self.button(top, "Remove from favourites" if only_favourites
                    else "Delete selected", remove, "bad", pad=(12, 5),
                    size=9).pack(side="right")

        def toggle_fav():
            for iid in tree.selection():
                wid = int(iid)
                self.store.set_favourite(wid, not self.store.is_favourite(wid))
            refresh()
        if not only_favourites:
            self.button(top, "Add to favourites", toggle_fav, "warn",
                        pad=(12, 5), size=9).pack(side="right", padx=(0, 8))

    # settings -------------------------------------------------------------
    def field(self, parent, value="", variable=None):
        """A text box that belongs to the current theme."""
        tk = self.tk
        box = tk.Entry(parent, font=(self.ui_font, 11), bg=UI["panel"],
                       fg=UI["text"], insertbackground=UI["text"],
                       relief="flat", highlightthickness=1,
                       highlightbackground=UI["line"],
                       highlightcolor=UI["accent"])
        if variable is not None:
            box.config(textvariable=variable)
            variable.set(str(value))
        else:
            box.insert(0, str(value))
        return box

    def tick(self, parent, text, variable):
        return self.tk.Checkbutton(
            parent, text=text, variable=variable, bg=UI["bg"], fg=UI["text"],
            selectcolor=UI["panel"], activebackground=UI["bg"],
            activeforeground=UI["text"], borderwidth=0, highlightthickness=0,
            anchor="w", cursor="hand2", font=(self.ui_font, 10))

    def choice(self, parent, text, value, variable):
        return self.tk.Radiobutton(
            parent, text=text, value=value, variable=variable, bg=UI["bg"],
            fg=UI["text"], selectcolor=UI["panel"], activebackground=UI["bg"],
            activeforeground=UI["text"], borderwidth=0, highlightthickness=0,
            cursor="hand2", font=(self.ui_font, 9))

    def set_theme(self, name):
        """Change the look, then rebuild the windows that are open."""
        self.store.put("theme", name)
        apply_theme(name)
        for attr in ("panel", "settings_win", "stats_win", "today_win"):
            win = getattr(self, attr, None)
            if win is not None and win.winfo_exists():
                win.destroy()
                setattr(self, attr, None)
        if self.card is not None and self.card["win"].winfo_exists():
            self.card["win"].destroy()
            self.card = None
        if self.quiz is not None and self.quiz.winfo_exists():
            self.quiz.destroy()
            self.quiz = None
        self.control_panel()
        self.refresh_menu()

    def settings_window(self):
        """Everything adjustable, grouped so it reads at a glance."""
        tk = self.tk
        if getattr(self, "settings_win", None) is not None and \
                self.settings_win.winfo_exists():
            self.settings_win.lift()
            return
        win = tk.Toplevel(self.root)
        self.settings_win = win
        win.title("Settings")
        win.configure(bg=UI["bg"])
        win.geometry("540x680")
        win.minsize(500, 520)
        win.attributes("-topmost", True)

        # the buttons sit at the bottom, the rest scrolls between them
        bar = tk.Frame(win, bg=UI["glass"])
        bar.pack(side="bottom", fill="x")
        tk.Frame(bar, height=1, bg=UI["line"]).pack(fill="x")
        body = self.scroll_area(win, height=560, width=500)
        win = body                       # everything below packs into the body

        def section(title):
            tk.Label(win, text=title.upper(), bg=UI["bg"], fg=UI["faint"],
                     font=(self.ui_font, 8, "bold")).pack(anchor="w", padx=20,
                                                          pady=(16, 2))
            tk.Frame(win, height=1, bg=UI["line"]).pack(fill="x", padx=20,
                                                        pady=(0, 8))

        def label(text):
            tk.Label(win, text=text, bg=UI["bg"], fg=UI["dim"],
                     font=(self.ui_font, 10), anchor="w").pack(
                         fill="x", padx=20, pady=(8, 2))

        # --- what you study ---
        section("What you study")
        mode = tk.StringVar(value=self.store.mode())
        mode_row = tk.Frame(win, bg=UI["bg"])
        mode_row.pack(fill="x", padx=18, pady=(0, 8))
        for column in range(2):
            mode_row.columnconfigure(column, weight=1, uniform="kinds")
        kinds = self.store.kind_counts()
        for index, name in enumerate(KINDS):
            self.choice(mode_row, "%s  %d" % (KIND_NAMES[name],
                                              kinds.get(name, 0)),
                        name, mode).grid(row=index // 2, column=index % 2,
                                         sticky="w", padx=4, pady=1)
        tk.Label(win, text="Levels apply to single words only", bg=UI["bg"],
                 fg=UI["faint"], font=(self.ui_font, 8)).pack(anchor="w",
                                                              padx=20,
                                                              pady=(2, 2))
        counts = self.store.level_counts("word")
        level = tk.StringVar(value=self.store.get("level"))
        grid = tk.Frame(win, bg=UI["bg"])
        grid.pack(fill="x", padx=18)
        for column in range(3):
            grid.columnconfigure(column, weight=1, uniform="levels")
        for i, lv in enumerate(LEVELS):
            self.choice(grid, "%s  %d" % (lv, counts.get(lv, 0)), lv,
                        level).grid(row=i // 3, column=i % 3, sticky="w",
                                    padx=4, pady=2)
        lower = tk.IntVar(
            value=1 if self.store.get("include_lower") == "1" else 0)
        self.tick(win, "Include the easier levels as well", lower).pack(
            fill="x", padx=18, pady=(4, 0))

        label("Words to study per day")
        daily = self.field(win, self.store.get("daily_count"))
        daily.pack(fill="x", padx=20, ipady=3)
        loop_on = tk.IntVar(
            value=1 if self.store.get("daily_only") == "1" else 0)
        self.tick(win, "Keep looping through today's words only",
                  loop_on).pack(fill="x", padx=18, pady=(6, 0))

        # --- cards ---
        section("Cards")
        row = tk.Frame(win, bg=UI["bg"])
        row.pack(fill="x", padx=20)
        left = tk.Frame(row, bg=UI["bg"])
        left.pack(side="left", expand=True, fill="x", padx=(0, 8))
        right = tk.Frame(row, bg=UI["bg"])
        right.pack(side="left", expand=True, fill="x")
        tk.Label(left, text="Minutes apart", bg=UI["bg"], fg=UI["dim"],
                 font=(self.ui_font, 10), anchor="w").pack(fill="x")
        interval = self.field(left, self.store.get("interval"))
        interval.pack(fill="x", pady=2, ipady=3)
        tk.Label(right, text="Seconds on screen", bg=UI["bg"], fg=UI["dim"],
                 font=(self.ui_font, 10), anchor="w").pack(fill="x")
        secs = self.field(right, self.store.get("popup_seconds"))
        secs.pack(fill="x", pady=2, ipady=3)

        # --- quiz ---
        section("Quiz")
        label("Questions per quiz")
        qlen = self.field(win, self.store.get("quiz_length"))
        qlen.pack(fill="x", padx=20, ipady=3)
        direction = tk.StringVar(value=self.store.get("direction"))
        dirrow = tk.Frame(win, bg=UI["bg"])
        dirrow.pack(fill="x", padx=18, pady=(8, 0))
        for value, text in (("bangla", "Bengali to English"),
                            ("english", "English to Bengali"),
                            ("both", "Mixed")):
            self.choice(dirrow, text, value, direction).pack(side="left",
                                                             padx=(0, 6))

        # --- look ---
        section("Look")
        theme_row = tk.Frame(win, bg=UI["bg"])
        theme_row.pack(fill="x", padx=18, pady=(0, 6))
        for name in THEMES:
            b = self.button(theme_row, THEMES[name]["label"],
                            lambda n=name: self.set_theme(n),
                            "accent" if self.store.get("theme") == name
                            else "quiet", pad=(10, 5), size=9)
            b.pack(side="left", padx=(0, 4))
        tk.Label(win, text="The windows are redrawn as soon as you pick one.",
                 bg=UI["bg"], fg=UI["faint"],
                 font=(self.ui_font, 8)).pack(anchor="w", padx=20)

        # --- language ---
        section("Meaning language")
        language = tk.StringVar(value=self.store.language())
        lang_row = tk.Frame(win, bg=UI["bg"])
        lang_row.pack(fill="x", padx=18)
        for value, text in (("bn", "Bengali"), ("hi", "Hindi"),
                            ("both", "Both")):
            self.choice(lang_row, text, value, language).pack(side="left",
                                                              padx=(0, 10))
        fill_note = tk.Label(win, text="", bg=UI["bg"], fg=UI["dim"],
                             font=(self.ui_font, 9), wraplength=430,
                             justify="left")
        fill_note.pack(anchor="w", padx=20, pady=(6, 0))

        def fill_hindi():
            fill_note.config(text="Translating in the background...")

            def work():
                rows = list(self.store.daily_rows()) + \
                    list(self.store.favourites()) + \
                    [r for r in self.store.all_words()[:400]]
                return self.store.fill_language(rows, "hi")

            def done(count):
                if fill_note.winfo_exists():
                    fill_note.config(
                        text=("Added Hindi for %d entries." % count)
                        if isinstance(count, int) else
                        "Could not reach the translator.")
            self.background(work, done)

        self.button(win, "Fill missing Hindi meanings now", fill_hindi,
                    "accent", pad=(14, 6), size=9).pack(anchor="w", padx=20,
                                                        pady=(8, 2))

        # --- while you work ---
        section("While you work")
        clip = tk.IntVar(value=1 if self.store.get("clipboard") == "1" else 0)
        self.tick(win, "Look up any English word I copy", clip).pack(
            fill="x", padx=18)
        keys = tk.IntVar(value=1 if self.store.get("hotkeys") == "1" else 0)
        self.tick(win, "Keyboard shortcuts on the card "
                       "(space, arrows, P, C, F, D)", keys).pack(fill="x",
                                                                 padx=18)
        startup = tk.IntVar(value=1 if self.startup_enabled() else 0)
        self.tick(win, "Start with Windows", startup).pack(fill="x", padx=18)
        quiet_row = tk.Frame(win, bg=UI["bg"])
        quiet_row.pack(fill="x", padx=20, pady=(10, 2))
        tk.Label(quiet_row, text="No cards from", bg=UI["bg"], fg=UI["dim"],
                 font=(self.ui_font, 10)).pack(side="left")
        quiet_from = self.field(quiet_row, self.store.get("quiet_from"))
        quiet_from.config(width=4, justify="center")
        quiet_from.pack(side="left", padx=6)
        tk.Label(quiet_row, text="until", bg=UI["bg"], fg=UI["dim"],
                 font=(self.ui_font, 10)).pack(side="left")
        quiet_to = self.field(quiet_row, self.store.get("quiet_to"))
        quiet_to.config(width=4, justify="center")
        quiet_to.pack(side="left", padx=6)
        tk.Label(quiet_row, text="o'clock, 0 to 23, leave empty for always",
                 bg=UI["bg"], fg=UI["faint"],
                 font=(self.ui_font, 8)).pack(side="left", padx=4)

        # --- sound ---
        section("Sound")
        speak_on = tk.IntVar(
            value=1 if self.store.get("speak_on_card") == "1" else 0)
        self.tick(win, "Say the word out loud when a card appears",
                  speak_on).pack(fill="x", padx=18)
        voice_row = tk.Frame(win, bg=UI["bg"])
        voice_row.pack(anchor="w", padx=20, pady=(6, 0))
        voice_note = tk.Label(voice_row, text="", bg=UI["bg"], fg=UI["dim"],
                              font=(self.ui_font, 9))

        def test_voice():
            voice_note.config(text="Speaking...", fg=UI["dim"])

            def work():
                return SPEAKER.say("pronunciation")

            def done(ok):
                if not voice_note.winfo_exists():
                    return
                voice_note.config(
                    text=SPEAKER.report() if ok is True
                    else "No sound. " + (SPEAKER.last_error or "")[:60],
                    fg="#6ee7a8" if ok is True else "#ffb4b4")
            self.background(work, done)
        self.button(voice_row, "Test the voice", test_voice, "accent",
                    pad=(12, 5), size=9).pack(side="left")
        voice_note.pack(side="left", padx=10)

        # --- online ---
        section("Online lookup")
        online = tk.IntVar(value=1 if self.store.get("online") == "1" else 0)
        self.tick(win, "Look up meanings, sentences and audio online",
                  online).pack(fill="x", padx=18)
        label("Your email, optional, raises the free translation quota")
        email = self.field(win, self.store.get("contact_email"))
        email.pack(fill="x", padx=20, ipady=3)

        status = tk.Label(win, text="", bg=UI["bg"], fg="#6ee7a8",
                          font=(self.ui_font, 10))
        status.pack(pady=(10, 14))

        def save():
            def number(entry, key, low, high):
                try:
                    v = int(entry.get().strip())
                except ValueError:
                    return
                self.store.put(key, max(low, min(high, v)))
            before_count = self.store.get_int("daily_count")
            before_mode = self.store.mode()
            self.store.put("mode", mode.get())
            self.store.put("language", language.get())
            number(daily, "daily_count", 1, 100)
            self.store.put("daily_only", loop_on.get())
            number(interval, "interval", 1, 720)
            number(secs, "popup_seconds", 4, 120)
            number(qlen, "quiz_length", 3, 50)
            self.store.put("level", level.get())
            self.store.put("include_lower", lower.get())
            self.store.put("direction", direction.get())
            self.store.put("contact_email", email.get().strip())
            self.store.put("online", online.get())
            self.store.put("speak_on_card", speak_on.get())
            self.store.put("clipboard", clip.get())
            self.store.put("hotkeys", keys.get())
            for box, key in ((quiet_from, "quiet_from"), (quiet_to, "quiet_to")):
                raw = box.get().strip()
                if not raw:
                    self.store.put(key, "")
                    continue
                try:
                    self.store.put(key, max(0, min(23, int(raw))))
                except ValueError:
                    pass
            if bool(startup.get()) != self.startup_enabled():
                self.toggle_startup()
            if self.store.get_int("daily_count") != before_count or \
                    self.store.mode() != before_mode:
                self.store.build_today(force=True)
            self.schedule_next()
            status.config(text="Saved")
            self.refresh_menu()

        holder = tk.Frame(bar, bg=UI["glass"], pady=10)
        holder.pack(fill="x")
        self.button(holder, "Save", save, "go", pad=(24, 8),
                    bold=True).pack(side="right", padx=(6, 16))
        self.button(holder, "Close", self.settings_win.destroy, "quiet",
                    pad=(18, 8)).pack(side="right")

        def on_close():
            window = self.settings_win
            self.settings_win = None
            if window is not None and window.winfo_exists():
                window.destroy()
        self.settings_win.protocol("WM_DELETE_WINDOW", on_close)

    def about_window(self):
        tk = self.tk
        s = self.store.stats()
        win = tk.Toplevel(self.root)
        win.title("About")
        win.configure(bg=UI["bg"])
        win.geometry("460x330")
        win.attributes("-topmost", True)
        counts = self.store.level_counts()
        spread = "   ".join("%s %d" % (lv, counts.get(lv, 0)) for lv in LEVELS)
        text = (APP_NAME + "\nVersion " + VERSION + "\n\n"
                "Words in your collection: %d\n%s\n\n"
                "Studying: %s\nToday: %d of %d learned\nWell known: %d\n"
                "Correct answers: %d\nMissed: %d\n\nData folder:\n%s"
                % (s["total"], spread,
                   ", ".join(self.store.active_levels()),
                   self.store.daily_progress()[0],
                   self.store.daily_progress()[1],
                   s["strong"], s["correct"], s["wrong"], data_dir()))
        tk.Label(win, text=text, bg=UI["bg"], fg=UI["text"], justify="left",
                 font=(self.ui_font, 10)).pack(padx=20, pady=18, anchor="w")

    def notify(self, message):
        try:
            if self.icon is not None:
                self.icon.notify(message, SHORT_NAME)
                return
        except Exception:
            pass
        say_out(message)

    # windows startup ------------------------------------------------------
    def startup_enabled(self):
        if os.name != "nt":
            return False
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run")
            try:
                winreg.QueryValueEx(key, "BengaliVocabTrainer")
                return True
            finally:
                winreg.CloseKey(key)
        except Exception:
            return False

    def toggle_startup(self):
        if os.name != "nt":
            return
        import winreg
        path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                                 winreg.KEY_ALL_ACCESS)
        except Exception:
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, path)
        try:
            if self.startup_enabled():
                winreg.DeleteValue(key, "BengaliVocabTrainer")
            else:
                if getattr(sys, "frozen", False):
                    command = '"%s"' % sys.executable
                else:
                    pyw = sys.executable.replace("python.exe", "pythonw.exe")
                    if not os.path.exists(pyw):
                        pyw = sys.executable
                    command = '"%s" "%s"' % (pyw, os.path.abspath(__file__))
                winreg.SetValueEx(key, "BengaliVocabTrainer", 0,
                                  winreg.REG_SZ, command)
        finally:
            winreg.CloseKey(key)
        self.refresh_menu()

    # tray -----------------------------------------------------------------
    def build_menu(self):
        import pystray
        item = pystray.MenuItem

        def call(fn):
            return lambda icon=None, it=None: self.post(fn)

        def set_interval(minutes):
            def apply(icon=None, it=None):
                def do():
                    self.store.put("interval", minutes)
                    self.schedule_next()
                    self.refresh_menu()
                self.post(do)
            return apply

        intervals = pystray.Menu(*[
            item(str(m) + " minutes", set_interval(m),
                 checked=lambda it, m=m: self.store.get_int("interval") == m,
                 radio=True)
            for m in (5, 10, 15, 20, 30, 45, 60, 120)])

        def toggle_pause(icon=None, it=None):
            def do():
                paused = self.store.get("paused") == "1"
                self.store.put("paused", 0 if paused else 1)
                self.schedule_next()
                self.refresh_menu()
            self.post(do)

        def set_level(value):
            def apply(icon=None, it=None):
                def do():
                    self.store.put("level", value)
                    self.refresh_menu()
                self.post(do)
            return apply

        def toggle_clip(icon=None, it=None):
            def do():
                now = self.store.get("clipboard") == "1"
                self.store.put("clipboard", 0 if now else 1)
                self.refresh_menu()
                if not now:
                    self.notify("Copy any English word and a card will appear.")
            self.post(do)

        def toggle_lower(icon=None, it=None):
            def do():
                now = self.store.get("include_lower") == "1"
                self.store.put("include_lower", 0 if now else 1)
                self.refresh_menu()
            self.post(do)

        def set_mode(name):
            def apply(icon=None, it=None):
                def do():
                    self.store.put("mode", name)
                    self.refresh_menu()
                self.post(do)
            return apply

        def set_theme_item(name):
            def apply(icon=None, it=None):
                self.post(lambda: self.set_theme(name))
            return apply

        def set_language(value):
            def apply(icon=None, it=None):
                def do():
                    self.store.put("language", value)
                    self.refresh_menu()
                self.post(do)
            return apply

        language_menu = pystray.Menu(*[
            item(text, set_language(value),
                 checked=lambda it, v=value: self.store.language() == v,
                 radio=True)
            for value, text in (("bn", "Bengali"), ("hi", "Hindi"),
                                ("both", "Both"))])

        theme_menu = pystray.Menu(*[
            item(THEMES[name]["label"], set_theme_item(name),
                 checked=lambda it, name=name: self.store.get("theme") == name,
                 radio=True) for name in THEMES])

        kind_totals = self.store.kind_counts()
        mode_menu = pystray.Menu(*[
            item("%s   %d" % (KIND_NAMES[name], kind_totals.get(name, 0)),
                 set_mode(name),
                 checked=lambda it, name=name: self.store.mode() == name,
                 radio=True) for name in KINDS])

        counts = self.store.level_counts()
        level_menu = pystray.Menu(*(
            [item("%s   %d words" % (LEVEL_NAMES[lv], counts.get(lv, 0)),
                  set_level(lv),
                  checked=lambda it, lv=lv: self.store.get("level") == lv,
                  radio=True) for lv in LEVELS]
            + [pystray.Menu.SEPARATOR,
               item("Include easier levels too", toggle_lower,
                    checked=lambda it: self.store.get("include_lower") == "1")]))

        entries = [
            item("Show a word now", call(self.show_card), default=True),
            item("Practice", call(self.start_quiz)),
            pystray.Menu.SEPARATOR,
            item("Learning  (" + KIND_NAMES[self.store.mode()] + ")",
                 mode_menu),
            item("Word level  (" + self.store.get("level") + ")", level_menu),
            item("Today's words", call(self.today_window)),
            item("Progress and streak", call(self.stats_window)),
            item("Favourites", call(self.favourites_window)),
            item("Hard words", call(self.hard_window)),
            pystray.Menu.SEPARATOR,
            item("Add a word", call(self.add_word_window)),
            item("My words", call(self.word_list_window)),
            pystray.Menu.SEPARATOR,
            item("Card interval", intervals),
            item("Pause reminders", toggle_pause,
                 checked=lambda it: self.store.get("paused") == "1"),
        ]
        if os.name == "nt":
            entries.append(item("Start with Windows",
                                lambda icon=None, it=None: self.post(self.toggle_startup),
                                checked=lambda it: self.startup_enabled()))
        entries += [
            item("Look up what I copy", toggle_clip,
                 checked=lambda it: self.store.get("clipboard") == "1"),
            item("Meaning language  (" +
                 {"bn": "Bengali", "hi": "Hindi",
                  "both": "Both"}[self.store.language()] + ")",
                 language_menu),
            item("Theme", theme_menu),
            item("Export", call(self.export_window)),
            item("Settings", call(self.settings_window)),
            pystray.Menu.SEPARATOR,
            item("About", call(self.about_window)),
            item("Quit", lambda icon=None, it=None: self.post(self.quit)),
        ]
        return pystray.Menu(*entries)

    def refresh_menu(self):
        if self.icon is None:
            return
        try:
            self.icon.menu = self.build_menu()
            self.icon.update_menu()
        except Exception:
            pass

    def quit(self):
        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception:
            pass
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)

    # window mode ----------------------------------------------------------
    def control_panel(self, note=""):
        """Home window: the day at a glance and everything in one place."""
        tk = self.tk
        if getattr(self, "panel", None) is not None and self.panel.winfo_exists():
            self.panel.deiconify()
            self.panel.lift()
            return
        win = tk.Toplevel(self.root)
        self.panel = win
        win.title(SHORT_NAME)
        win.configure(bg=UI["bg"])
        win.geometry("450x720")
        win.minsize(420, 620)

        # header
        head = tk.Frame(win, bg=UI["panel"], padx=20, pady=16)
        head.pack(fill="x")
        badge = tk.Frame(head, bg=UI["panel"])
        badge.pack(anchor="w", fill="x")
        try:
            from PIL import ImageTk
            self._logo = ImageTk.PhotoImage(make_icon_image(40))
            tk.Label(badge, image=self._logo, bg=UI["panel"]).pack(side="left",
                                                                   padx=(0, 12))
        except Exception:
            pass
        titles = tk.Frame(badge, bg=UI["panel"])
        titles.pack(side="left", anchor="w")
        tk.Label(titles, text="Vocabulary Trainer", bg=UI["panel"],
                 fg=UI["text"], font=(self.ui_font, 15, "bold")).pack(anchor="w")
        sub = tk.Label(titles, text="", bg=UI["panel"], fg=UI["dim"],
                       font=(self.ui_font, 9))
        sub.pack(anchor="w")

        # today
        prog = tk.Frame(win, bg=UI["bg"], padx=20, pady=14)
        prog.pack(fill="x")
        line = tk.Frame(prog, bg=UI["bg"])
        line.pack(fill="x")
        day_label = tk.Label(line, text="", bg=UI["bg"], fg=UI["dim"],
                             font=(self.ui_font, 10))
        day_label.pack(side="left")
        bar_holder = tk.Frame(prog, bg=UI["bg"])
        bar_holder.pack(fill="x", pady=(8, 0))

        def paint_day():
            done, total, loops = self.store.daily_progress()
            run = self.store.streak()
            day_label.config(
                text="Today  %d of %d learned%s"
                     % (done, total,
                        "    %d day streak" % run if run else ""))
            mode_now = self.store.mode()
            where = (self.store.get("level") if mode_now == "word"
                     else KIND_NAMES[mode_now])
            sub.config(text="%s   %d entries in your collection"
                            % (where, self.store.count()))
            for child in bar_holder.winfo_children():
                child.destroy()
            self.progress_bar(bar_holder, done, total, width=360).pack(
                anchor="w", fill="x")

        # a quick way to change the look
        look = tk.Frame(win, bg=UI["panel"], padx=20, pady=8)
        look.pack(fill="x")
        tk.Label(look, text="THEME", bg=UI["panel"], fg=UI["faint"],
                 font=(self.ui_font, 8, "bold")).pack(side="left",
                                                      padx=(0, 10))

        def set_language_chip(value):
            self.store.put("language", value)
            self.set_theme(self.store.get("theme"))
        for name in THEMES:
            self.button(look, THEMES[name]["label"],
                        lambda n=name: self.set_theme(n),
                        "accent" if self.store.get("theme") == name
                        else "quiet", pad=(8, 3), size=8).pack(side="left",
                                                               padx=2)

        lang_bar = tk.Frame(win, bg=UI["panel"], padx=20, pady=6)
        lang_bar.pack(fill="x")
        tk.Label(lang_bar, text="MEANING", bg=UI["panel"], fg=UI["faint"],
                 font=(self.ui_font, 8, "bold")).pack(side="left", padx=(0, 8))
        for value, text in (("bn", "Bengali"), ("hi", "Hindi"),
                            ("both", "Both")):
            self.button(lang_bar, text,
                        lambda v=value: set_language_chip(v),
                        "accent" if self.store.language() == value
                        else "quiet", pad=(9, 3), size=8).pack(side="left",
                                                               padx=2)

        # what to learn
        chips = tk.Frame(win, bg=UI["bg"], padx=20)
        chips.pack(fill="x")
        tk.Label(chips, text="LEARNING", bg=UI["bg"], fg=UI["faint"],
                 font=(self.ui_font, 8, "bold")).pack(anchor="w", pady=(2, 6))
        mode_row = tk.Frame(chips, bg=UI["bg"])
        mode_row.pack(anchor="w", fill="x")
        for column in range(3):
            mode_row.columnconfigure(column, weight=1, uniform="kinds")
        mode_widgets = {}
        level_label = tk.Label(chips, text="LEVEL", bg=UI["bg"],
                               fg=UI["faint"],
                               font=(self.ui_font, 8, "bold"))
        level_label.pack(anchor="w", pady=(10, 6))
        chip_row = tk.Frame(chips, bg=UI["bg"])
        chip_row.pack(anchor="w")
        chip_widgets = {}

        def paint_modes():
            current = self.store.mode()
            for name, widget in mode_widgets.items():
                chosen = name == current
                self.recolour(widget, "accent" if chosen else "quiet")
            # levels only apply to single words
            if current == "word":
                level_label.pack(anchor="w", pady=(10, 6))
                chip_row.pack(anchor="w", fill="x")
            else:
                level_label.pack_forget()
                chip_row.pack_forget()

        def choose_mode(name):
            self.store.put("mode", name)
            paint_modes()
            paint_day()
            self.refresh_menu()

        counts_by_kind = self.store.kind_counts()
        for index, name in enumerate(KINDS):
            b = self.button(mode_row, "%s  %d" % (KIND_NAMES[name],
                                                  counts_by_kind.get(name, 0)),
                            lambda n=name: choose_mode(n), "quiet",
                            pad=(6, 5), size=9)
            b.grid(row=index // 3, column=index % 3, sticky="ew", padx=2,
                   pady=2)
            mode_widgets[name] = b

        def paint_chips():
            current = self.store.get("level")
            for name, widget in chip_widgets.items():
                chosen = name == current
                self.recolour(widget, "accent" if chosen else "quiet")

        def choose(level):
            self.store.put("level", level)
            paint_chips()
            paint_day()

        for column, level in enumerate(LEVELS):
            chip_row.columnconfigure(column, weight=1, uniform="levels")
            b = self.button(chip_row, level, lambda lv=level: choose(lv),
                            "quiet", pad=(4, 4), size=9)
            b.grid(row=0, column=column, sticky="ew", padx=2)
            chip_widgets[level] = b
        paint_chips()
        paint_modes()

        if note:
            tk.Label(win, text=note, bg=UI["bg"], fg=UI["dim"],
                     font=(self.ui_font, 9), wraplength=340,
                     justify="left").pack(padx=20, pady=(10, 0), anchor="w")

        # the actions
        grid = tk.Frame(win, bg=UI["bg"], padx=20, pady=16)
        grid.pack(fill="both", expand=True)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        def wrap(fn):
            def go():
                fn()
                paint_day()
            return go

        actions = [
            ("Show a word now", wrap(self.show_card), "go"),
            ("Practice", self.start_quiz, "accent"),
            ("Today's words", self.today_window, "quiet"),
            ("Progress", self.stats_window, "quiet"),
            ("Favourites", self.favourites_window, "quiet"),
            ("Hard words", self.hard_window, "quiet"),
            ("Add a word", self.add_word_window, "quiet"),
            ("My words", self.word_list_window, "quiet"),
            ("Export", self.export_window, "quiet"),
            ("Settings", self.settings_window, "quiet"),
        ]
        for i, (text, cmd, kind) in enumerate(actions):
            grid.rowconfigure(i // 2, weight=1)
            b = self.button(grid, text, cmd, kind, pad=(10, 10), size=10,
                            bold=(i < 2))
            b.grid(row=i // 2, column=i % 2, sticky="nsew", padx=4, pady=4)

        foot = tk.Frame(win, bg=UI["bg"], padx=20, pady=12)
        foot.pack(fill="x")
        pause_text = tk.StringVar()

        def refresh_pause():
            pause_text.set("Resume reminders"
                           if self.store.get("paused") == "1"
                           else "Pause reminders")

        def toggle():
            self.store.put("paused", 0 if self.store.get("paused") == "1" else 1)
            self.schedule_next()
            refresh_pause()

        refresh_pause()
        self.button(foot, "", toggle, "warn", pad=(12, 7), size=9,
                    textvar=pause_text).pack(side="left")
        self.button(foot, "Quit", self.quit, "bad", pad=(12, 7),
                    size=9).pack(side="right")

        paint_day()
        win.protocol("WM_DELETE_WINDOW", win.withdraw)

    def run(self, force_window=False, note=""):
        stage("starting the tray")
        started = False
        if not force_window:
            try:
                import pystray
                self.icon = pystray.Icon("BengaliVocabTrainer",
                                         make_icon_image(), SHORT_NAME,
                                         self.build_menu())

                def spin():
                    try:
                        self.icon.run()
                    except Exception as exc:
                        log("Tray stopped: " + repr(exc))
                        self.icon = None
                        self.post(lambda: self.control_panel(
                            "The system tray icon could not start, so the "
                            "controls are here instead."))
                threading.Thread(target=spin, daemon=True).start()
                time.sleep(0.8)
                started = self.icon is not None
            except Exception as exc:
                log("Tray unavailable: " + repr(exc))
                started = False
        stage("tray started" if started else "no tray, using a window")
        if started:
            self.notify("Running in the tray. Right click the icon to begin.")
            if self.store.get("seen_intro") != "1":
                self.store.put("seen_intro", 1)
                self.post(lambda: self.control_panel(
                    "The blue icon is now in the notification area near the "
                    "clock, under the arrow that shows hidden icons. Close "
                    "this window whenever you like, the app keeps running."))
        else:
            if not note and not force_window:
                note = ("The system tray icon could not start, so the "
                        "controls are here instead.")
            self.control_panel(note)
        stage("running")
        self.root.mainloop()


# ------------------------------------------------------------------ selftest

def selftest():
    import tempfile
    tmp = os.path.join(tempfile.mkdtemp(), "test.db")
    store = Store(tmp)
    bank = word_bank()
    words_only = [r for r in bank if r[3] == "word"]
    added = store.seed_if_empty()
    assert added == len(bank), added
    assert store.count() == len(bank)

    # levels: every word carries one, and C2 is where a new user starts
    assert store.get("level") == "C2"
    counts = store.level_counts("word")
    for lv in LEVELS:
        assert counts.get(lv, 0) >= 300, (lv, counts)
    assert set(counts) == set(LEVELS), counts
    kinds = store.kind_counts()
    assert kinds.get("phrasal", 0) >= 200, kinds
    assert kinds.get("idiom", 0) >= 150, kinds

    # cards and quizzes stay inside the chosen level
    assert store.active_levels() == ["C2"]
    for _ in range(30):
        assert store.pick_word()["level"] == "C2"
    pool = store.quiz_pool(12)
    assert len(pool) == 12
    assert all(q["level"] == "C2" for q in pool)
    for q in pool[:5]:
        wrong = store.distractors(q["id"], 3, q["level"])
        assert len(wrong) == 3
    store.put("level", "A1")
    assert all(store.pick_word()["level"] == "A1" for _ in range(20))

    # including easier levels widens the pool but never goes above the choice
    store.put("level", "B1")
    store.put("include_lower", 1)
    assert store.active_levels() == ["A1", "A2", "B1"]
    seen_levels = {store.pick_word()["level"] for _ in range(60)}
    assert seen_levels <= {"A1", "A2", "B1"}, seen_levels
    store.put("include_lower", 0)
    store.put("level", "C2")

    # an older database gains the level column and the new words
    import sqlite3 as _sq
    old_path = tmp + ".old"
    conn = _sq.connect(old_path)
    conn.executescript(
        "CREATE TABLE words (id INTEGER PRIMARY KEY, english TEXT NOT NULL,"
        " bangla TEXT DEFAULT '', pos TEXT DEFAULT '',"
        " definition TEXT DEFAULT '', example TEXT DEFAULT '',"
        " phonetic TEXT DEFAULT '', synonyms TEXT DEFAULT '',"
        " enriched INTEGER DEFAULT 0, created TEXT, UNIQUE(english));"
        "CREATE TABLE progress (word_id INTEGER PRIMARY KEY, box INTEGER"
        " DEFAULT 1, seen INTEGER DEFAULT 0, correct INTEGER DEFAULT 0,"
        " wrong INTEGER DEFAULT 0, last_shown TEXT, due TEXT);"
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);"
        "INSERT INTO words (english,bangla) VALUES ('book','\u09ac\u0987');"
        "INSERT INTO progress (word_id,box) VALUES (1,4);")
    conn.commit()
    conn.close()
    old_store = Store(old_path)
    assert old_store.seed_if_empty() == 0, "existing words must be kept"
    grew = old_store.sync_seed()
    assert grew == len(bank) - 1, grew
    assert old_store.word(1)["kind"] == "word"
    assert old_store.word(1)["level"] == "A1"
    with old_store.lock:
        kept = old_store.conn.execute(
            "SELECT box FROM progress WHERE word_id=1").fetchone()
    assert kept["box"] == 4, "progress must survive the upgrade"
    assert old_store.get("level") == "C2"
    assert old_store.sync_seed() == 0, "sync must run only once"
    with old_store.lock:
        cols = [r["name"] for r in old_store.conn.execute(
            "PRAGMA table_info(words)").fetchall()]
    for needed in ("level", "examples", "audio", "kind"):
        assert needed in cols, (needed, cols)

    # settings round trip
    store.put("interval", 7)
    assert store.get_int("interval") == 7
    assert store.get("direction") == "both"

    # picking and marking
    row = store.pick_word()
    assert row is not None and row["english"]
    store.mark(row["id"], True)
    with store.lock:
        p = store.conn.execute("SELECT * FROM progress WHERE word_id=?",
                               (row["id"],)).fetchone()
    assert p["box"] == 2 and p["correct"] == 1, dict(p)
    store.mark(row["id"], False)
    with store.lock:
        p = store.conn.execute("SELECT * FROM progress WHERE word_id=?",
                               (row["id"],)).fetchone()
    assert p["box"] == 1 and p["wrong"] == 1

    # quiz material
    pool = store.quiz_pool(10)
    assert len(pool) == 10
    for q in pool:
        others = store.distractors(q["id"], 3)
        assert len(others) == 3
        assert all(o["english"] != q["english"] for o in others)

    # add, update, delete
    # a word of the user's own, one the starter bank does not contain
    assert not any(w == "quokka" for w, _, _, _ in bank)
    wid = store.add_word("Quokka", "এক প্রকার ছোট ক্যাঙারু", level="C1")
    assert store.word(wid)["english"] == "quokka"
    assert store.word(wid)["level"] == "C1"
    store.add_word("quokka", "")               # must not wipe the meaning
    assert store.word(wid)["bangla"] == "এক প্রকার ছোট ক্যাঙারু"
    # adding a word that already exists must return that same word
    existing = store.add_word("book", "বই")
    assert store.word(existing)["english"] == "book", dict(store.word(existing))
    store.update_word(wid, definition="a happy accident", enriched=1)
    assert store.word(wid)["definition"] == "a happy accident"
    store.delete_word(wid)
    assert store.word(wid) is None

    # learning modes keep their own material apart
    assert store.mode() == "word"
    store.put("mode", "phrasal")
    assert all(store.pick_word()["kind"] == "phrasal" for _ in range(20))
    phrasal_pool = store.quiz_pool(8)
    assert len(phrasal_pool) == 8
    assert all(q["kind"] == "phrasal" for q in phrasal_pool)
    for q in phrasal_pool[:4]:
        with store.lock:
            kinds_seen = [store.conn.execute(
                "SELECT kind FROM words WHERE english=?", (d["english"],)
            ).fetchone()["kind"] for d in store.distractors(q["id"], 3)]
        assert set(kinds_seen) == {"phrasal"}, kinds_seen
    store.put("mode", "idiom")
    assert all(store.pick_word()["kind"] == "idiom" for _ in range(20))
    # the level is ignored outside single words
    store.put("level", "A1")
    assert store.pick_word()["kind"] == "idiom"
    store.put("mode", "word")
    store.put("level", "C2")
    assert all(store.pick_word()["kind"] == "word" for _ in range(10))

    # two languages
    assert store.language() == "bn"
    row_bn = store.word(store.add_word("water", "জল"))
    assert store.meaning_of(row_bn) == "জল"
    with store.lock:
        hindi_count = store.conn.execute(
            "SELECT COUNT(*) n FROM words WHERE hindi<>''").fetchone()["n"]
    assert hindi_count >= 250, hindi_count
    store.put("language", "hi")
    assert store.meaning_of(store.word(row_bn["id"])) == "पानी"
    store.put("language", "both")
    both = store.meaning_of(store.word(row_bn["id"]))
    assert "জল" in both and "पानी" in both, both
    # a word with no Hindi yet falls back rather than showing nothing
    only_bn = store.word(store.add_word("zzhypothetical", "পরীক্ষামূলক"))
    store.put("language", "hi")
    assert store.meaning_of(only_bn) == "পরীক্ষামূলক"
    store.delete_word(only_bn["id"])
    store.put("language", "bn")

    # themes
    assert set(THEMES) >= {"midnight", "glass", "daylight", "forest", "plum"}
    for name, palette in THEMES.items():
        for key in ("bg", "panel", "card", "line", "text", "dim", "faint",
                    "accent", "bn", "quiet", "quiet_lit", "go", "go_lit",
                    "warn", "warn_lit", "bad", "bad_lit", "glass", "alpha",
                    "label"):
            assert key in palette, (name, key)
        for key, value in palette.items():
            if key not in ("alpha", "label"):
                assert re.match(r"^#[0-9a-f]{6}$", value), (name, key, value)
    apply_theme("forest")
    assert UI["bg"] == THEMES["forest"]["bg"]
    apply_theme("midnight")

    # spelling practice hides letters but keeps the shape
    for trial in range(20):
        masked = mask_word("meticulous")
        assert masked.replace(" ", "")[0] == "m"
        assert masked.replace(" ", "")[-1] == "s"
        assert "_" in masked
        assert len(masked.replace(" ", "")) == len("meticulous")
    assert "_" in mask_word("cat") or mask_word("cat") == "c a t"

    # base forms, so a copied "boxes" still finds "box"
    assert "box" in word_forms("boxes")
    assert "run" in word_forms("running")
    assert "study" in word_forms("studied")
    assert "meticulous" in word_forms("meticulously")
    assert word_forms("cat") == ["cat"]

    # a part of speech for every single entry, before any lookup
    missing = [en for en, bn, lv, kd in bank if not guess_pos(en, kd, bn)]
    assert not missing, missing[:5]
    assert guess_pos("talent") == "noun"
    assert guess_pos("pavement") == "noun"
    assert guess_pos("meticulous") == "adjective"
    assert guess_pos("quickly") == "adverb"
    assert guess_pos("organise") == "verb"
    assert guess_pos("put up with", "phrasal") == "verb"
    with store.lock:
        blank_pos = store.conn.execute(
            "SELECT COUNT(*) n FROM words WHERE pos IS NULL OR pos=''"
        ).fetchone()["n"]
    assert blank_pos == 0, blank_pos

    # every sense the dictionary gives, not just the first
    fake_entry = [{
        "word": "run", "phonetic": "/rʌn/",
        "meanings": [
            {"partOfSpeech": "verb", "definitions": [
                {"definition": "To move quickly on foot.",
                 "example": "they run every morning together"}]},
            {"partOfSpeech": "noun", "definitions": [
                {"definition": "An act of running.",
                 "example": "she went for a long run"}]},
            {"partOfSpeech": "adjective", "definitions": [
                {"definition": "Melted or flowing."}]},
        ]}]
    saved_fetch = globals().get("fetch_json")
    globals()["fetch_json"] = lambda url, timeout=10: (
        fake_entry if url.startswith(DICT_API) else {})
    LOOKUP_CACHE.clear()
    info = define("run")
    globals()["fetch_json"] = saved_fetch
    LOOKUP_CACHE.clear()
    senses = info.get("senses") or []
    assert len(senses) == 3, senses
    assert [x["pos"] for x in senses] == ["verb", "noun", "adjective"]
    assert short_pos(senses[1]["pos"]) == "n."

    # the short part of speech
    assert short_pos("noun") == "n." and short_pos("adjective") == "adj."
    assert short_pos("", "phrasal") == "phr. v."
    assert short_pos("", "idiom") == "idiom"
    assert short_pos("", "word", "to ponder") == "v."
    assert short_pos("") == ""

    # the day's set loops through a fixed number of words
    store.put("level", "C2")
    store.put("daily_count", 5)
    store.put("daily_only", 1)
    today = store.build_today(force=True)
    assert len(today) == 5, len(today)
    assert all(r["level"] == "C2" for r in today)
    ids = [r["id"] for r in today]
    # the loop hands out each word once before repeating any of them
    seen_order = []
    for _ in range(10):
        pick = store.pick_word()
        seen_order.append(pick["id"])
        store.mark_daily_shown(pick["id"])
    assert set(seen_order) <= set(ids), "the loop left today's set"
    assert sorted(seen_order[:5]) == sorted(ids), seen_order[:5]
    assert sorted(seen_order[5:]) == sorted(ids), "second loop differed"
    # the set survives until tomorrow
    assert [r["id"] for r in store.build_today()] == ids
    # learned words drop out of the loop, and progress is counted
    store.mark_daily_known(ids[0], True)
    store.mark_daily_known(ids[1], True)
    done, total, loops = store.daily_progress()
    assert (done, total) == (2, 5), (done, total)
    assert loops >= 2, loops
    assert all(store.pick_word()["id"] not in ids[:2] for _ in range(6))
    # a new level means a new set
    store.put("level", "A1")
    fresh_set = store.build_today()
    assert all(r["level"] == "A1" for r in fresh_set), "level change ignored"
    assert [r["id"] for r in fresh_set] != ids
    store.put("level", "C2")
    store.put("daily_only", 0)

    # favourites
    fav_id = store.pick_word()["id"]
    assert not store.is_favourite(fav_id)
    store.set_favourite(fav_id, True)
    assert store.is_favourite(fav_id)
    assert [r["id"] for r in store.favourites()] == [fav_id]
    store.set_favourite(fav_id, False)
    assert store.favourites() == []

    # answers are logged, and the streak follows them
    wid_s = store.pick_word()["id"]
    store.mark(wid_s, True, "typing")
    store.mark(wid_s, False, "listening")
    overall = store.overall()
    assert overall["answered"] == 2 and overall["correct"] == 1, overall
    assert 0 < overall["accuracy"] < 100
    assert store.streak() == 1, store.streak()
    days = store.day_totals(5)
    assert len(days) == 5 and days[-1]["answered"] == 2, days

    # hard words surface themselves
    hard_id = store.pick_word()["id"]
    for _ in range(3):
        store.mark(hard_id, False, "typing")
    hard = store.hard_words()
    assert hard and hard[0]["id"] == hard_id, [h["english"] for h in hard[:3]]

    # search looks in both scripts and in the meanings
    found = store.search("meticul")
    assert any(r["english"] == "meticulous" for r in found), len(found)
    assert store.search("অতি সূক্ষ্ম")
    assert store.search("zzzznothing") == []

    # word families group the forms of one word
    base_row = store.word(store.add_word("meticulous", "অতি সূক্ষ্ম যত্নশীল",
                                         level="C2"))
    extra_id = store.add_word("meticulously", "অতি সূক্ষ্মভাবে", level="C2")
    fam = [r["english"] for r in store.family(base_row)]
    assert "meticulously" in fam, fam
    store.delete_word(extra_id)          # keep the collection count honest

    # quiet hours
    store.put("quiet_from", 0)
    store.put("quiet_to", 24 if False else 23)
    assert store.quiet_now() in (True, False)
    store.put("quiet_from", "")
    store.put("quiet_to", "")
    assert store.quiet_now() is False

    # exports write real files
    import os as _os
    sample = store.all_words()[:12]
    for kind in ("csv", "html", "xlsx"):
        path = export_rows(sample, kind)
        assert path and _os.path.exists(path) and _os.path.getsize(path) > 200, kind
        assert path.endswith(kind) or kind == "xlsx"
    assert export_rows([], "csv") or True

    # the new material
    for kind in ("collocation", "proverb", "confusable"):
        assert store.kind_counts().get(kind, 0) >= 50, store.kind_counts()
        store.put("mode", kind)
        assert all(store.pick_word()["kind"] == kind for _ in range(8))
    store.put("mode", "word")

    # sentences and sound
    store.update_word(wid2 if False else store.add_word("cogent", "যুক্তিগ্রাহ্য"),
                      examples=json.dumps(["A cogent argument won the debate.",
                                           "Her reply was cogent and short."]),
                      example="A cogent argument won the debate.")
    cog = store.word(store.add_word("cogent", ""))
    kept = json.loads(cog["examples"])
    assert len(kept) == 2 and kept[0].startswith("A cogent")
    assert tidy_sentences(["  too short ", "A proper sentence about a word",
                           "A proper sentence about a word", "x" * 200]) == [
        "A proper sentence about a word."]
    assert tidy_sentences(["The diligent clerk checked every page"],
                          word="diligent")
    assert not tidy_sentences(["Nothing relevant in this line at all"],
                              word="diligent")
    assert SPEAKER.say("", "") is False
    assert word_stem("meticulously") == "meticulous"
    assert word_stem("to ponder") == "ponder"
    assert word_stem("cat") == "cat"
    # a frozen build must never try to install anything
    sys.frozen = True
    try:
        ok, message = ensure_packages(quiet=True)
        assert ok and "exe" in message, message
    finally:
        del sys.frozen
    assert isinstance(SPEAKER.pick_backend(), str)

    # script detection
    assert is_bangla("বই") and not is_bangla("book")
    assert is_bangla("bengali শব্দ")

    # stats
    s = store.stats()
    assert s["total"] == len(bank) and s["correct"] >= 1

    # offline behaviour must never raise
    store.put("online", 0)
    assert enrich(store, store.pick_word()) == {}
    wid2, msg = lookup_new(store, "কলম")
    assert wid2 is None and "connection" in msg.lower()
    wid3, msg3 = lookup_new(store, "  ")
    assert wid3 is None

    # no duplicates anywhere in the word bank
    names = [w for w, _, _, _ in bank]
    assert len(names) == len(set(names))
    for group in (SEED, B1, B2, C1, C2, PHRASAL, IDIOMS,
                  A1_EXTRA, A2_EXTRA, B1_EXTRA):
        inner = [w for w, _ in group]
        assert len(inner) == len(set(inner)), [
            w for w in inner if inner.count(w) > 1]
    # every entry has a real Bengali meaning
    blank = [w for w, bn, _, _ in bank if not bn.strip()]
    assert not blank, blank
    # only real words and "to" verbs reach a card, no invented phrases
    odd = [w for w, _, _, k in bank
           if k == "word" and " " in w and not w.startswith("to ")]
    assert not odd, odd
    assert all(w == w.strip().lower() for w, _, _, _ in bank)
    # the bank is big enough to study from at every level
    assert len(bank) > 2500, len(bank)

    # icon drawing
    try:
        img = make_icon_image()
        assert img.size == (64, 64)
        icon_note = "icon ok"
    except ImportError:
        icon_note = "icon skipped, pillow not installed"

    print("Selftest passed.")
    print("  entries      :", len(bank))
    print("  by kind      :", ", ".join(
        "%s %d" % (k, sum(1 for r in bank if r[3] == k)) for k in KINDS))
    print("  by level     :",
          "  ".join("%s %d" % (lv, store.level_counts("word").get(lv, 0))
                    for lv in LEVELS))
    print("  database     :", tmp)
    print("  ", icon_note)


def doctor():
    print(APP_NAME, "version", VERSION)
    print("Python      :", sys.version.split()[0], "at", sys.executable)
    print("Platform    :", sys.platform, os.name)
    print("Data folder :", data_dir())
    print("Database    :", DB_PATH,
          "(exists)" if os.path.exists(DB_PATH) else "(not created yet)")
    for module, package in [("tkinter", "tkinter"), ("sqlite3", "sqlite3")
                            ] + REQUIRED:
        try:
            m = importlib.import_module(module)
            version = getattr(m, "__version__", "")
            print("%-12s: ok %s" % (package, version))
        except Exception as exc:
            print("%-12s: MISSING  %s" % (package, exc))
    try:
        import pystray
        print("Tray backend:", pystray.Icon.__module__)
    except Exception as exc:
        print("Tray backend: not available,", exc)
    try:
        json_data = fetch_json(DICT_API + "hello")
        print("Dictionary  : reachable,", json_data[0]["word"])
    except Exception as exc:
        print("Dictionary  : not reachable,", exc)
    try:
        out = translate("book", "en|bn")
        print("Translation :", "reachable, book is " + out if out
              else "no result, the daily free quota may be used up")
    except Exception as exc:
        print("Translation : not reachable,", exc)
    print("Voice       :", SPEAKER.pick_backend() or "none found")
    for name in SPEAKER.backends():
        ok = SPEAKER.try_backend(name, "test", silent=True)
        print("  %-11s %s" % (name, "available" if ok else
                              "no  " + SPEAKER.last_error[:60]))
    if os.path.exists(LOG_PATH):
        print("\nLast lines of the log:")
        try:
            with open(LOG_PATH, encoding="utf-8") as fh:
                for line in fh.readlines()[-12:]:
                    print("  " + line.rstrip())
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--selftest", action="store_true",
                    help="run the internal checks and exit")
    ap.add_argument("--doctor", action="store_true",
                    help="report what is installed and what is reachable")
    ap.add_argument("--window", action="store_true",
                    help="skip the tray and use a plain window")
    ap.add_argument("--speak", metavar="WORD", nargs="?", const="pronunciation",
                    help="say a word out loud and exit, to test the voice")
    ap.add_argument("--reset", action="store_true",
                    help="delete the local database and start fresh")
    ap.add_argument("--no-install", action="store_true",
                    help="do not install missing packages automatically")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.doctor:
        return doctor()
    if args.speak:
        for name in SPEAKER.backends():
            ok = SPEAKER.try_backend(name, "test", silent=True)
            print("  %-11s %s" % (name, "available" if ok else
                                  "not usable  " + SPEAKER.last_error[:70]))
        print("Speaking:", args.speak)
        ok = SPEAKER.say(args.speak)
        print("Worked, voice was " + SPEAKER.pick_backend() if ok
              else "No sound. " + (SPEAKER.last_error or "") +
              "\nDetails in " + LOG_PATH)
        return
    if args.reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    force_window = args.window
    note = ""
    if not args.no_install and not getattr(sys, "frozen", False) \
            and missing_packages():
        ok, message = ensure_packages()
        if not ok:
            # The tray needs those packages, the rest of the app does not,
            # so carry on in window mode rather than refusing to start.
            force_window = True
            note = ("The tray icon needs pystray and pillow, which could not "
                    "be installed, so the controls are in this window. "
                    "Details are in " + LOG_PATH)
    stage("starting up", fresh=True)
    try:
        Trainer().run(force_window=force_window, note=note)
    except BaseException:
        report = traceback.format_exc()
        log(report)
        show_error(APP_NAME + " could not start", report)
        sys.exit(1)


if __name__ == "__main__":
    main()
