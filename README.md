# Bengali to English Vocabulary Trainer

A Windows tray app that teaches English vocabulary starting from Bengali. It shows a word card every so often, quizzes you in five practice styles and tracks your progress with light spaced repetition. Built in Python.

| | |
|---|---|
| Word bank | 3,805 entries: words, phrasal verbs, idioms, collocations, proverbs and confusing pairs, each with a Bengali meaning |
| Levels | CEFR A1 to C2 |
| Practice | Multiple choice, typing, listening, fill the gap, synonyms |
| Offline | Lookups are cached in a local SQLite database |
| Tech | Python, Tkinter, pystray, Pillow, SQLite, Free Dictionary API, MyMemory API |

## What it does

- **Word cards on a timer.** Every 20 minutes by default, a card appears in the bottom right corner with the Bengali word, the English word, its phonetics, the meaning, example sentences and how far through the day you are. Buttons: Pronounce, Copy, Add to favourites, Previous, I know this, Show again soon, Next word, Details. Drag the card by its top edge to move it.
- **Copy button.** Copies the word, its Bengali meaning, the definition and two example sentences to the clipboard, ready to paste into notes or a message. The button says Copied for a moment so you know it worked. It is on the card, in Details, and in the quiz after you answer.
- **Pronounce button.** Plays the recorded human voice from the dictionary when there is one, and otherwise uses the voice built into Windows, so it still works with no internet. There is a Say button for each example sentence too.
- **Example sentences.** Each word shows real sentences so you see how it is used, drawn from the dictionary, then Tatoeba, then Wiktionary, until enough are found.
- **Practice, five ways.** Multiple choice, typing the English from the Bengali, listening and typing what you hear, filling the missing word in a real sentence, and picking the synonym. Mixed rotates them. Every answer is recorded with the style used.
- **Online lookup.** Meanings, phonetics, part of speech and example sentences come from the Free Dictionary API, and Bengali meanings come from the MyMemory translation API. Neither needs an API key.
- **Add any word.** Type a word in Bengali or in English. The app detects the script, translates in the right direction and saves the full entry.
- **Everything is cached.** Once a word has been looked up, it is stored locally, so the app keeps working with no internet.
- **Light spaced repetition.** A word you answer correctly moves up a level and comes back after 1, 3, 7 then 16 days. A word you miss drops back to level 1 and returns quickly.
- **CEFR levels from A1 to C2.** The default is **C2**, so you get words like taciturn, obfuscate, perspicacious and vicissitude rather than book and water. Change level from the tray menu, the Settings window or the buttons in the control panel.
- **3805 entries** in six kinds: 3105 words, 287 phrasal verbs, 191 idioms, 114 collocations, 54 proverbs and 54 confusing pairs, every one with a Bengali meaning written for it.
- **Copy anywhere, learn here.** Turn on clipboard lookup and any English word you copy, in a browser, a PDF or an email, opens a card with the meaning, Bengali, sentences and pronunciation. Unknown words are looked up and saved.
- **Progress and streak.** Day streak, accuracy, total answers and a two week chart of answers against correct ones.
- **Hard words.** Anything you miss twice collects itself into its own list.
- **Search** across English, Bengali and meanings, in any list window.
- **Export** today's words, favourites, hard words or everything as Excel, CSV or a printable page that saves as PDF from the browser.
- **Word families and opposites.** Details shows related forms such as meticulous and meticulously, plus synonyms and antonyms.
- **Keyboard shortcuts** on the card: space marks it known, arrows move back and forward, P pronounces, C copies, F favourites, D opens details, Escape closes.
- **Quiet hours.** Set the hours when no cards should appear.
- **Bengali and Hindi.** Show meanings in Bengali, in Hindi, or both at once. 445 entries ship with Hindi already written, and Settings has a button that fills the rest from the translator in the background.
- **Five themes.** Midnight, Glass, Daylight, Forest and Plum, changed from the tray, the home window or Settings, applied immediately.
- **Glass cards.** The word card is a rounded, frosted sheet with a soft sheen, slightly see through, and it can be dragged anywhere.
- **Every entry has its type from the start**, with no lookup needed: n., v., adj., adv., phr. v., idiom, proverb, phrase or pair. The dictionary replaces the built in reading as soon as it answers.
- **Spelling practice.** The word appears with letters knocked out, m _ t i c _ l o u s, with its meaning underneath and a button to hear it.
- **Show on web.** A button on the card, in Details and after each practice answer opens the word in your browser. Details also has Meaning on Google and Pictures.
- **Word family on the card.** Related forms appear as chips with their own types, so meticulous shows meticulously adv. and meticulousness n.
- **Copied words that are not in your collection** are looked up live, and an inflected word finds its base: copying "meticulously", "boxes" or "running" finds the right entry.
- **All parts of speech.** Each card shows n., v., adj. beside the word and lists the others as "also n., v.". Details spells out every sense the dictionary has, and the type appears in My words, Today's words, the practice prompts and the exports.
- **Previous button.** Walk back through the words you have already seen this session, and forward again, without losing your place in the day's set.

## Installing, in one click

Double click **VocabTrainerSetup.exe**. That is the whole process. There are no questions to answer and no administrator prompt: it installs for you alone, puts shortcuts on the desktop and in the Start Menu, sets itself to start with Windows, and opens the app when it finishes.

Windows may show a "Windows protected your PC" notice the first time, because the file is new and unsigned. Click **More info** then **Run anyway**.

To remove it, use Add or remove programs. It asks whether to keep your words, favourites and progress, so you can reinstall later without losing anything.

If you would rather not install at all, **VocabTrainer.exe** runs on its own from any folder.

## The batch files are optional

**You do not need any of them.** VocabTrainerSetup.exe, and the VocabTrainer.exe it installs, contain Python inside themselves. The batch files exist only for running the app from its source code.

If you run one anyway, it looks for VocabTrainer.exe first and starts that, then for VocabTrainerSetup.exe, and only asks for Python when neither is beside it.

To run from the source: put the folder anywhere, for example `D:\English Python tool`, then double click **InstallAndRun.bat**. It finds Python, installs pystray and pillow, starts the app, and leaves the window open showing any error instead of vanishing. If it says Python is missing, either use the installer or get Python from [python.org/downloads](https://www.python.org/downloads/) with **Add python.exe to PATH** ticked.

Afterwards, **StartVocabTrainer.bat** launches it silently for daily use. You can also run it by hand:

```
python BengaliEnglishTrainer.py
```

The app installs its own packages on startup, so `pip install` is never something you have to remember.

On the first run a small window appears telling you where the icon went. Close it whenever you like, the app keeps running. The icon is the blue circle in the notification area near the clock, usually hidden behind the arrow. Drag it down onto the taskbar to keep it visible. Right click it for the menu, left click it to see a word immediately.

## If nothing seems to happen

Double click **CheckSetup.bat**. With the exe beside it, it asks the program itself for a report. Otherwise it prints your Python version, whether each package is installed, whether the dictionary and translation services are reachable, and the last lines of the error log. That output tells you exactly what is wrong.

Common causes:

- **The app is running, you just cannot see it.** The tray icon hides behind the arrow at the left of the clock. Click the arrow, then drag the blue circle onto the taskbar.
- **Python is not on PATH.** Reinstall Python with "Add python.exe to PATH" ticked, or use the py launcher: `py -3 BengaliEnglishTrainer.py`.
- **Double clicking the .py file flashes a window and closes.** That is Windows running it and showing the error for a split second. Use InstallAndRun.bat, which keeps the window open.
- **The tray icon will not start on your machine.** The app notices and opens a plain window with the same buttons instead. You can force that mode any time with `python BengaliEnglishTrainer.py --window`.

Every error is written to `%APPDATA%\BengaliVocabTrainer\error.log`, and `startup.log` in the same folder records how far the last start got, line by line. Between them they say exactly where a problem is.

## Your day

Instead of drifting through 2808 words at random, the app picks a set for the day and loops through it until you know them.

- **Words per day.** Set it in Settings, ten by default. The set is chosen once a day from your level, favouring words that are due or weak.
- **The loop.** Cards go round the set in turn, each word coming up once before any repeats. A word you mark as known drops out of the loop for the rest of the day. Tomorrow you get a fresh set.
- **Today's words.** Open it from the tray or the control panel to see the set, how many times you have seen each word, which are learned, and how many full loops you have done. Buttons there show the next word, open a word, or start a new set early.
- Changing level rebuilds the set straight away, so a switch to B1 does not keep feeding you C2 words.
- Turn the loop off in Settings with **Keep looping through today's words only**, and cards go back to drawing from the whole level.

## Favourites

Every word card and every Details window has **Add to favourites**. The button changes to In favourites once saved, and pressing it again removes the word.

**Favourites** in the tray menu and on the control panel lists everything you have saved, newest first. Double click a row to open the word with its meaning, sentences and pronunciation, or select rows and press Remove from favourites. My words has a Favourite column and an Add to favourites button too, so you can mark words while browsing the full list.

## Three things to learn

Pick what you want to study with **Learning** in the tray menu, the chips at the top of the home window, or Settings.

| Mode | Entries | Examples |
|---|---|---|
| Words | 3105 | taciturn, alleviate, receipt, umbrella |
| Phrasal verbs | 287 | put up with, get away with, look forward to, take after |
| Idioms | 191 | spill the beans, once in a blue moon, bite the bullet, on thin ice |
| Collocations | 114 | make a decision, meet a deadline, earn a living |
| Proverbs | 54 | haste makes waste, where there is a will there is a way |
| Confusing pairs | 54 | affect and effect, lay and lie, principal and principle |

The day's set, the cards and the quiz all follow the mode you pick, and quiz wrong answers come from the same mode, so an idiom question never offers a single word as a choice. CEFR levels apply to single words, so the level row hides itself in the other two modes.

## Levels

| Level | Words | Examples |
|---|---|---|
| A1 beginner | 390 | book, mother, umbrella, to wash |
| A2 elementary | 361 | monsoon, receipt, shadow, to apologize |
| B1 intermediate | 510 | attitude, persuade, reputation, tolerate |
| B2 upper intermediate | 437 | allocate, consensus, undermine, sustainable |
| C1 advanced | 485 | alleviate, discreet, meticulous, relentless |
| C2 proficient | 625 | taciturn, obfuscate, perfidy, quintessential |

Set your level three ways: the tray menu under **Word level**, the Settings window, or the A1 to C2 buttons on the control panel. Whatever you pick is remembered.

A new install starts at **C2**. Cards and quizzes stay inside the level you pick, and the wrong answers in a quiz are drawn from the same level, so a C2 question never gives itself away with three beginner options. Tick **Include easier levels too** if you would rather revise everything up to your level.

If you already had the app running, your words and your score are kept. The new levels are added the first time you start a new version, nothing is reset.

## The look

Five themes, changed in one click from the tray menu, the home window or Settings: **Midnight**, **Glass**, **Daylight**, **Forest** and **Plum**. Every window is redrawn immediately, and light and dark themes both keep their text readable.

The word card is a rounded frosted sheet with a sheen along the top and a little transparency, so it sits over your work without blocking it. Drag it by its top edge to move it.

## Tray menu

| Item | What it does |
|---|---|
| Show a word now | Pops a card straight away |
| Start quiz | Opens the quiz window |
| Learning | Words, phrasal verbs, idioms, collocations, proverbs or confusing pairs |
| Practice | The five test styles |
| Progress and streak | Your chart, streak and accuracy |
| Hard words | Everything you keep missing |
| Look up what I copy | Clipboard lookup on or off |
| Export | Excel, CSV or a printable page |
| Word level | A1 to C2, and whether to include the easier levels |
| Today's words | The day's set, your progress through it, and a new set button |
| Favourites | Everything you have saved, with open and remove |
| Add a word | Look up and save any Bengali or English word |
| My words | Table of your collection with levels and scores, and a delete button |
| Card interval | 5 to 120 minutes |
| Pause reminders | Stops the timer, the quiz still works |
| Start with Windows | Registers the app so it launches at login |
| Settings | Level, words per day, the loop, interval, card duration, quiz length, quiz direction, speech, contact email, online lookup |
| About | Your totals and the data folder location |
| Quit | Closes it |

## Rebuilding the exe

If you change the code and want a new exe, double click **BuildExe.bat**. It installs PyInstaller and rebuilds **VocabTrainer.exe** in the folder, with the icon and no console window.

## No console window

Use **StartVocabTrainer.vbs** for daily use. It starts the app hidden, so nothing flashes on screen at all. StartVocabTrainer.bat hands over to it, Start with Windows uses the windowless launcher, and the pip install and the speech engine run without opening a window.

Keep **InstallAndRun.bat** for the first run and for troubleshooting, since seeing the messages is the point there.

## Settings worth knowing

**If the pronounce button stays silent**, open Settings and press **Test the voice**. It names the voice it used, or the reason it failed. `CheckSetup.bat` lists every speech engine on the machine and whether each one is usable.

**Say the word out loud when a card appears.** Off by default. Turn it on in Settings if you want every card spoken. **Test the voice** in the same window tells you which voice was found, and `python BengaliEnglishTrainer.py --speak hello` does the same from a command line.


**Contact email.** The MyMemory translation service allows a limited number of free translations per day for anonymous users and a much larger allowance if you put an email address in the request. Adding your email in Settings simply raises that daily allowance. It is sent only to that translation service.

**Use online lookup.** Turn it off if you want a purely offline session. Cards then show whatever is already cached.

## Where your data lives

```
%APPDATA%\BengaliVocabTrainer\vocab.db
```

A single SQLite file with three tables: words, progress and settings. Copy it to move your collection to another machine. To start over:

```
python BengaliEnglishTrainer.py --reset
```

## Build a standalone exe

If you would rather not install Python on every machine:

```
pip install pyinstaller
pyinstaller --noconsole --onefile --name "VocabTrainer" BengaliEnglishTrainer.py
```

The result lands in `dist\VocabTrainer.exe`. It carries the starter words inside itself and creates the same database folder on first run. Note that a one file build unpacks itself on every launch, so `--onedir` starts faster if that bothers you.

## Command line options

| Option | What it does |
|---|---|
| `--doctor` | Reports Python, packages, network reachability and the recent log |
| `--window` | Skips the tray and uses a plain window |
| `--speak WORD` | Says a word out loud and exits, to test the voice |
| `--selftest` | Runs the internal checks and exits, should print "Selftest passed" |
| `--reset` | Deletes the local database and starts fresh |
| `--no-install` | Leaves package installation alone |

## Bengali text not showing

The app looks for Nirmala UI, which ships with Windows 10 and 11, then falls back to Shonar Bangla, Vrinda, Noto Sans Bengali, Kalpurush and Siyam Rupali. If you see boxes instead of letters, install Noto Sans Bengali from Google Fonts and restart the app.

## Notes and limits

- The starter Bengali meanings are common everyday translations. Machine translation for words added later can occasionally be loose, so treat an odd looking result as a prompt to correct it yourself in My words.
- The dictionary API covers English headwords. For a phrase such as "to ponder" the app strips the leading "to" before asking for the definition.
- If a lookup fails, the card still appears with whatever is stored and the app never blocks or crashes on a network error.

## Author

Built by Sourab Kumar Saha. Available for custom Python and Excel automation on [Fiverr](https://www.fiverr.com/spontaneoussrv).
