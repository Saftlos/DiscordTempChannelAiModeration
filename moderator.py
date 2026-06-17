import os
import json
import urllib.request
import unicodedata
from config import GEMINI_API_KEY

WHITELIST_FILE = "whitelist.txt"
BLOCKLIST_FILE = "blocklist.txt"
CACHE_FILE = "moderation_cache.json"

# Local cache for AI moderation results
_moderation_cache = {}

def load_cache():
    global _moderation_cache
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                _moderation_cache = json.load(f)
            print(f"Loaded {len(_moderation_cache)} cached moderation results.")
        except Exception as e:
            print(f"Error loading moderation cache: {e}")
            _moderation_cache = {}

def save_cache():
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_moderation_cache, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Error saving moderation cache: {e}")

# Load cache on startup
load_cache()

def normalize_text(text: str) -> tuple[str, str]:
    """
    Normalizes text to defeat Unicode exploits.
    Returns a tuple of:
    - (normalized_nfkc, stripped_combining)
    """
    # NFKC normalizes fullwidth characters, mathematical symbols, circled letters, etc.
    nfkc = unicodedata.normalize('NFKC', text)
    # Strip combining marks (like strikethrough, slash accents, zalgo)
    stripped = "".join(c for c in nfkc if not unicodedata.combining(c))
    return nfkc, stripped

def load_words_file(filepath: str) -> list[str]:
    """Reads lines from a text file, strips spaces and filters empty lines."""
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return [line.strip().lower() for line in f if line.strip()]
    except Exception as e:
        print(f"Error loading {filepath}: {e}")
        return []

def check_whitelist(text: str) -> bool:
    """
    Checks if the text consists entirely of whitelisted terms.
    Every word in the text must be present in the whitelist to skip AI moderation.
    """
    whitelist = load_words_file(WHITELIST_FILE)
    if not whitelist:
        return False
    
    nfkc, stripped = normalize_text(text)
    
    import re
    # Extract all alphanumeric words (handles unicode/umlauts)
    words = re.findall(r'\w+', stripped.lower())
    if not words:
        return False
        
    for word in words:
        if word not in whitelist:
            return False
            
    return True

def check_blocklist(text: str) -> bool:
    """Checks if the text contains any blacklisted term (pre-filter to save API calls)."""
    blocklist = load_words_file(BLOCKLIST_FILE)
    if not blocklist:
        return False
    
    nfkc, stripped = normalize_text(text)
    nfkc_lower = nfkc.lower()
    stripped_lower = stripped.lower()
    
    for term in blocklist:
        if term in nfkc_lower or term in stripped_lower:
            return True
    return False

def check_channel_name_with_ai(original_name: str) -> tuple[bool, str, str]:
    """
    Checks the channel name using Gemma 4 26B model.
    Optimized for API conservation:
    1. Whitelist Check (Instant Approve)
    2. Local Blocklist Check (Instant Deny)
    3. JSON Caching Check (Instant Lookup)
    4. AI Call (Fallback)
    Returns (approved: bool, reason: str, source: str).
    """
    nfkc_name, stripped_name = normalize_text(original_name)
    cache_key = stripped_name.lower().strip()

    # 1. Whitelist Check
    if check_whitelist(original_name):
        return True, "Wort aus der Whitelist erkannt (AI-Moderation übersprungen).", "Local_Whitelist"

    # 2. Local Blocklist Check (saves API requests from obvious trolls)
    if check_blocklist(original_name):
        return False, "Kanalname enthält blockierte Begriffe aus der lokalen Sperrliste.", "Local_Blocklist"

    # 3. Cache Check (saves API requests for recurring names)
    if cache_key in _moderation_cache:
        cached = _moderation_cache[cache_key]
        return cached["approved"], cached["reason"], "Local_Cache"

    # 4. Gemma 4 API Request Setup
    if not GEMINI_API_KEY:
        return False, "Fehler: Google API Key fehlt in der Konfiguration.", "Configuration_Error"

    # Using the Gemini 2.5 Flash model for faster response times
    model_name = "models/gemini-2.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/{model_name}:generateContent?key={GEMINI_API_KEY}"

    system_instruction = (
        "Du bist ein präziser, strenger und fairer Discord-Moderator für einen APEX LEGENDS Gaming-Server. "
        "Deine Aufgabe ist es, vorgeschlagene Voice-Channel-Namen zu prüfen. "
        "Filtere Namen heraus, die beleidigend, rassistisch, extremistisch, sexuell explizit (NSFW), "
        "Spam oder anderweitig gegen die Discord-Nutzungsbedingungen verstoßen. "
        "WICHTIG: Erlaube Apex-Legends-spezifische Begriffe wie 'Predator'/'Pred' (der höchste Rang im Spiel), "
        "'Kill Leader', 'Deathbox', 'Scrims', 'Champion', 'Ranked', 'Lobby' und Legendennamen. Diese sind im Spielkontext völlig harmlos und dürfen NICHT blockiert werden. "
        "Beachte, dass Benutzer versuchen könnten, Filter mittels Unicode-Zeichen (z.B. Zalgo, "
        "durchgestrichene Schrift, mathematische Symbole, Symbole im Kreis) zu umgehen. "
        "Wir stellen dir den Originalnamen, die normalisierte Version und die von Strichen/Akzenten bereinigte Version zur Verfügung. "
        "Gib IMMER ausschließlich ein valides JSON-Objekt im folgenden Format zurück:\n"
        "{\n"
        "  \"approved\": true oder false,\n"
        "  \"reason\": \"Erklärung auf Deutsch warum der Name freigegeben oder abgelehnt wurde\"\n"
        "}"
    )

    user_prompt = (
        f"Bitte überprüfe den folgenden Channel-Namen:\n\n"
        f"Original-Eingabe: \"{original_name}\"\n"
        f"Normalisiert (NFKC): \"{nfkc_name}\"\n"
        f"Bereinigt (ohne Combining Marks): \"{stripped_name}\"\n\n"
        f"Antworte nur mit dem JSON-Objekt."
    )

    payload = {
        "systemInstruction": {
            "parts": [{"text": system_instruction}]
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_prompt}]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }

    import time
    max_retries = 3
    retry_delay = 1.5

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            
            with urllib.request.urlopen(req, timeout=15) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                
                # Find the non-thought text part
                text_content = None
                try:
                    parts = res_data["candidates"][0]["content"]["parts"]
                    for part in parts:
                        if not part.get("thought"):
                            text_content = part["text"]
                            break
                except (KeyError, IndexError):
                    pass
                
                if not text_content:
                    raise ValueError("Ungültige Antwortstruktur der KI erhalten.")

                # Parse the inner JSON returned by the model
                try:
                    cleaned_text = text_content.strip()
                    if cleaned_text.startswith("```json"):
                        cleaned_text = cleaned_text[7:]
                    if cleaned_text.startswith("```"):
                        cleaned_text = cleaned_text[3:]
                    if cleaned_text.endswith("```"):
                        cleaned_text = cleaned_text[:-3]
                    cleaned_text = cleaned_text.strip()
                    
                    result = json.loads(cleaned_text)
                    approved = bool(result.get("approved", False))
                    reason = str(result.get("reason", "Keine Begründung angegeben."))

                    # Save response to cache
                    _moderation_cache[cache_key] = {"approved": approved, "reason": reason}
                    save_cache()

                    return approved, reason, "AI_Gemini_2.5_Flash"
                except json.JSONDecodeError as je:
                    print(f"JSON decode error of model output: {je}. Raw output was: {text_content}")
                    raise ValueError(f"KI-Antwort konnte nicht als JSON interpretiert werden. (Raw: {text_content[:100]})")
                    
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as e:
            status_code = getattr(e, 'code', None)
            print(f"[Attempt {attempt + 1}/{max_retries}] Error querying Gemma API (Status {status_code}): {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay * (attempt + 1))
                continue
            else:
                return False, f"Fehler bei der Kommunikation mit der Moderations-API nach {max_retries} Versuchen: {str(e)}", "API_Error"
        except Exception as e:
            print(f"[Attempt {attempt + 1}/{max_retries}] Unexpected error querying Gemma API: {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue
            else:
                return False, f"Fehler bei der Kommunikation mit der Moderations-API: {str(e)}", "API_Error"
