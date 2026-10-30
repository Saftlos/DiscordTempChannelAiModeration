"""
Channel name moderation pipeline.

A requested voice channel name passes through four stages, cheapest first,
so the AI model is only queried when nothing local can decide:

1. Whitelist  - every word is a known harmless term   -> approve instantly
2. Blocklist  - contains a known bad term             -> deny instantly
3. Cache      - the same name was judged before        -> reuse that verdict
4. Gemma AI   - ask the model for a verdict + reason   -> cache the result

Every stage works on Unicode-normalized text, so tricks like fullwidth letters,
circled letters or zalgo/strikethrough marks cannot sneak past the filters.
"""

import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request

from config import GEMINI_API_KEY, GEMMA_MODEL

WHITELIST_FILE = "whitelist.txt"
BLOCKLIST_FILE = "blocklist.txt"
CACHE_FILE = "moderation_cache.json"

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
API_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 1.5

SYSTEM_INSTRUCTION = (
    "Du bist ein präziser, strenger und fairer Discord-Moderator für einen APEX LEGENDS Gaming-Server. "
    "Deine Aufgabe ist es, vorgeschlagene Voice-Channel-Namen zu prüfen. "
    "Filtere Namen heraus, die beleidigend, rassistisch, extremistisch, sexuell explizit (NSFW), "
    "Spam oder anderweitig gegen die Discord-Nutzungsbedingungen verstoßen. "
    "WICHTIG: Erlaube Apex-Legends-spezifische Begriffe wie 'Predator'/'Pred' (der höchste Rang im Spiel), "
    "'Kill Leader', 'Deathbox', 'Scrims', 'Champion', 'Ranked', 'Lobby' und Legendennamen. "
    "Diese sind im Spielkontext völlig harmlos und dürfen NICHT blockiert werden. "
    "Beachte, dass Benutzer versuchen könnten, Filter mittels Unicode-Zeichen (z.B. Zalgo, "
    "durchgestrichene Schrift, mathematische Symbole, Symbole im Kreis) zu umgehen. "
    "Wir stellen dir den Originalnamen, die normalisierte Version und die von Strichen/Akzenten "
    "bereinigte Version zur Verfügung. "
    "Gib IMMER ausschließlich ein valides JSON-Objekt im folgenden Format zurück:\n"
    "{\n"
    "  \"approved\": true oder false,\n"
    "  \"reason\": \"Erklärung auf Deutsch warum der Name freigegeben oder abgelehnt wurde\"\n"
    "}"
)


# --- Cache ---------------------------------------------------------------

def _load_cache() -> dict:
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        print(f"Loaded {len(cache)} cached moderation results.")
        return cache
    except Exception as e:
        print(f"Error loading moderation cache: {e}")
        return {}


def _save_cache() -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_moderation_cache, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Error saving moderation cache: {e}")


# Verdicts of earlier AI calls, keyed by the normalized lowercase name
_moderation_cache: dict = _load_cache()


# --- Normalization & word lists -----------------------------------------

def normalize_text(text: str) -> tuple[str, str]:
    """
    Normalizes text to defeat Unicode exploits.

    Returns (nfkc, stripped):
    - nfkc:     NFKC form, turns fullwidth/math/circled letters into plain ones
    - stripped: nfkc without combining marks (strikethrough, accents, zalgo)
    """
    nfkc = unicodedata.normalize("NFKC", text)
    stripped = "".join(c for c in nfkc if not unicodedata.combining(c))
    return nfkc, stripped


def load_words_file(filepath: str) -> list[str]:
    """
    Reads one lowercase term per line, skipping empty lines.
    The file is read on every call, so list edits apply without a restart.
    """
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return [line.strip().lower() for line in f if line.strip()]
    except Exception as e:
        print(f"Error loading {filepath}: {e}")
        return []


def check_whitelist(text: str) -> bool:
    """True if every word of the text is whitelisted (skips the AI call)."""
    whitelist = set(load_words_file(WHITELIST_FILE))
    if not whitelist:
        return False

    _, stripped = normalize_text(text)
    words = re.findall(r"\w+", stripped.lower())
    return bool(words) and all(word in whitelist for word in words)


def check_blocklist(text: str) -> bool:
    """
    True if the text contains a blocklisted term anywhere.
    Substring matching is intentional: it also catches glued spellings
    like "xXbadwordXx" that a word-based match would miss.
    """
    blocklist = load_words_file(BLOCKLIST_FILE)
    if not blocklist:
        return False

    nfkc, stripped = normalize_text(text)
    candidates = (nfkc.lower(), stripped.lower())
    return any(term in candidate for term in blocklist for candidate in candidates)


# --- Gemma API -----------------------------------------------------------

def _build_prompt(original_name: str, nfkc_name: str, stripped_name: str) -> str:
    # Gemma has no separate system role in this API, so the instruction
    # is placed at the top of the user message.
    return (
        f"{SYSTEM_INSTRUCTION}\n\n"
        f"Bitte überprüfe den folgenden Channel-Namen:\n\n"
        f"Original-Eingabe: \"{original_name}\"\n"
        f"Normalisiert (NFKC): \"{nfkc_name}\"\n"
        f"Bereinigt (ohne Combining Marks): \"{stripped_name}\"\n\n"
        f"Antworte nur mit dem JSON-Objekt."
    )


def _request_model(prompt: str) -> str:
    """Sends one prompt to the model and returns its answer text."""
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    req = urllib.request.Request(
        API_URL.format(model=GEMMA_MODEL),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            # Header instead of ?key= so the key never shows up in URL logs
            "x-goog-api-key": GEMINI_API_KEY,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as response:
        res_data = json.loads(response.read().decode("utf-8"))

    # Skip "thought" parts of thinking models, only the final answer matters
    try:
        for part in res_data["candidates"][0]["content"]["parts"]:
            if not part.get("thought") and part.get("text"):
                return part["text"]
    except (KeyError, IndexError):
        pass
    raise ValueError("Ungültige Antwortstruktur der KI erhalten.")


def _parse_verdict(text: str) -> tuple[bool, str]:
    """Extracts (approved, reason) from the model's JSON answer, with or without ``` fences."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"JSON decode error of model output: {e}. Raw output was: {text}")
        raise ValueError(f"KI-Antwort konnte nicht als JSON interpretiert werden. (Raw: {text[:100]})")

    approved = bool(result.get("approved", False))
    reason = str(result.get("reason", "Keine Begründung angegeben."))
    return approved, reason


# --- Public entry point --------------------------------------------------

def check_channel_name_with_ai(original_name: str) -> tuple[bool, str, str]:
    """
    Runs the full moderation pipeline for a channel name.
    Blocking (network I/O), so call it via asyncio.to_thread from the bot.

    Returns (approved, reason, source) where source names the deciding stage:
    Local_Whitelist, Local_Blocklist, Local_Cache, AI_Gemma_4,
    Configuration_Error or API_Error. Errors always deny, never approve.
    """
    nfkc_name, stripped_name = normalize_text(original_name)
    cache_key = stripped_name.lower().strip()

    if check_whitelist(original_name):
        return True, "Wort aus der Whitelist erkannt (AI-Moderation übersprungen).", "Local_Whitelist"

    if check_blocklist(original_name):
        return False, "Kanalname enthält blockierte Begriffe aus der lokalen Sperrliste.", "Local_Blocklist"

    if cache_key in _moderation_cache:
        cached = _moderation_cache[cache_key]
        return cached["approved"], cached["reason"], "Local_Cache"

    if not GEMINI_API_KEY:
        return False, "Fehler: Google API Key fehlt in der Konfiguration.", "Configuration_Error"

    prompt = _build_prompt(original_name, nfkc_name, stripped_name)
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            approved, reason = _parse_verdict(_request_model(prompt))
            _moderation_cache[cache_key] = {"approved": approved, "reason": reason}
            _save_cache()
            return approved, reason, "AI_Gemma_4"
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            # HTTPError is a subclass of URLError and carries the status code
            status_code = getattr(e, "code", None)
            print(f"[Attempt {attempt}/{MAX_RETRIES}] Error querying Gemma API (Status {status_code}): {e}")
            last_error = e
        except Exception as e:
            print(f"[Attempt {attempt}/{MAX_RETRIES}] Unexpected error querying Gemma API: {e}")
            last_error = e

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY_SECONDS * attempt)

    return (
        False,
        f"Fehler bei der Kommunikation mit der Moderations-API nach {MAX_RETRIES} Versuchen: {last_error}",
        "API_Error",
    )
