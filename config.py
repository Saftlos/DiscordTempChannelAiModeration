"""
Configuration: secrets come from .env, server-specific IDs from config.json.
config.json is written by the /setup command, so it never has to be edited by hand.
"""

import os
import json
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# Gemma 4 26B (MoE, ~4B active parameters): fast enough for live moderation
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "gemma-4-26b-a4b-it")

CONFIG_FILE = "config.json"

class Config:
    """Discord IDs the bot needs: lobby text channel, admin log channel and the voice category."""

    def __init__(self):
        self.lobby_channel_id = None
        self.log_channel_id = None
        self.category_id = None
        self.load()

    def load(self):
        """Loads configuration from config.json if it exists."""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.lobby_channel_id = data.get("lobby_channel_id")
                    self.log_channel_id = data.get("log_channel_id")
                    self.category_id = data.get("category_id")
            except Exception as e:
                print(f"Error loading config.json: {e}")

    def save(self):
        """Saves current configuration to config.json."""
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "lobby_channel_id": self.lobby_channel_id,
                    "log_channel_id": self.log_channel_id,
                    "category_id": self.category_id
                }, f, indent=4)
        except Exception as e:
            print(f"Error saving config.json: {e}")

    def update(self, lobby_channel_id=None, log_channel_id=None, category_id=None):
        """Updates configuration settings and saves them."""
        if lobby_channel_id is not None:
            self.lobby_channel_id = int(lobby_channel_id)
        if log_channel_id is not None:
            self.log_channel_id = int(log_channel_id)
        if category_id is not None:
            self.category_id = int(category_id)
        self.save()

    def is_fully_configured(self) -> bool:
        """Checks if all necessary Discord settings are configured."""
        return all(v is not None for v in [self.lobby_channel_id, self.log_channel_id, self.category_id])

# Global configuration instance
bot_config = Config()
