# Pfade für spell.py/translate.py (Kopien aus notist): Wörterbuch des Übersetzers wird mit notist geteilt.
import os

HOME = os.path.expanduser("~/.local/share/notes") if os.path.isdir(os.path.expanduser("~/.local/share/notes/dict")) \
    else os.path.expanduser("~/.local/share/mailist")
WORDS = os.path.join(os.path.expanduser("~/.config/mailist"), "words.txt")
