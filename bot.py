"""
Temp voice channel bot with AI name moderation.

Flow:
1. A user clicks the lobby button and enters a channel name in a modal.
2. The bot instantly creates a voice channel with a placeholder name,
   so the user never waits on the AI.
3. In the background the name runs through moderator.py.
   Approved: the channel gets the (normalized) name.
   Denied:   the channel is renamed to a neutral "rejected" label.
4. Every decision lands as a JSON dev log in the admin log channel, with
   buttons to delete the channel or approve the name manually.
5. Once the last member leaves, the channel is deleted automatically.
"""

import asyncio
import json
import os
import sys

import discord
from discord.ext import commands

# Set utf-8 stdout encoding for Windows compatibility (emoji in logs)
sys.stdout.reconfigure(encoding="utf-8")

from config import bot_config, DISCORD_TOKEN
import moderator

TEMP_CHANNELS_FILE = "temp_channels.json"
PENDING_NAME = "⏳ | Prüfung ausstehend..."
DENIED_NAME = "❌ | Name abgelehnt"
UNUSED_CHANNEL_TIMEOUT = 30  # seconds until a channel nobody joined is deleted
COLOR_APPROVED = 0x2ECC71
COLOR_DENIED = 0xE74C3C


# --- Temp channel registry -----------------------------------------------
# Only channels created by this bot get auto-deleted. Tracking them by ID
# (instead of "everything in the category") keeps manually created
# channels in the same category safe. Persisted so a restart forgets nothing.

def _load_temp_channels() -> set[int]:
    if not os.path.exists(TEMP_CHANNELS_FILE):
        return set()
    try:
        with open(TEMP_CHANNELS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception as e:
        print(f"Error loading {TEMP_CHANNELS_FILE}: {e}")
        return set()


def _save_temp_channels() -> None:
    try:
        with open(TEMP_CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(temp_channel_ids), f)
    except Exception as e:
        print(f"Error saving {TEMP_CHANNELS_FILE}: {e}")


temp_channel_ids: set[int] = _load_temp_channels()


# --- Bot -----------------------------------------------------------------

intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True


class CustomChannelBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Persistent views keep their buttons working after a restart
        self.add_view(CreateChannelLobby())
        self.add_view(AdminChannelControls(show_approve=True))
        self.add_view(AdminChannelControls(show_approve=False))
        print("Persistent views registered successfully.")

        # Synced once here rather than in on_ready, which fires again on every reconnect
        try:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} application commands.")
        except Exception as e:
            print(f"Failed to sync application commands: {e}")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        # Clean up temp channels that were left empty while the bot was offline
        for guild in self.guilds:
            for channel_id in list(temp_channel_ids):
                if guild.get_channel(channel_id):
                    await delete_if_empty(guild, channel_id, delay=0)


bot = CustomChannelBot()


# --- Log embed helpers ---------------------------------------------------
# The log embed is the only state an admin button gets, so the channel ID
# lives in the footer and the requested name in the JSON description.

def build_log_embed(
    interaction: discord.Interaction,
    voice_channel: discord.VoiceChannel,
    requested_name: str,
    approved: bool,
    reason: str,
    source: str,
) -> discord.Embed:
    nfkc_name, stripped_name = moderator.normalize_text(requested_name)
    log_data = {
        "event": "voice_channel_created",
        "status": "approved" if approved else "denied",
        "guild_id": interaction.guild.id,
        "channel_id": voice_channel.id,
        "creator": {"id": interaction.user.id, "name": interaction.user.name},
        "input": {"raw": requested_name, "nfkc": nfkc_name, "stripped": stripped_name},
        "moderation": {"approved": approved, "reason": reason, "source": source},
    }
    json_log = json.dumps(log_data, indent=2, ensure_ascii=False)

    status = "Approved" if approved else "Blocked"
    embed = discord.Embed(
        title=f"[DEV LOG] Channel {status}: {voice_channel.id}",
        description=f"```json\n{json_log}\n```",
        color=COLOR_APPROVED if approved else COLOR_DENIED,
    )
    embed.set_footer(text=f"Kanal-ID: {voice_channel.id} | User-ID: {interaction.user.id}")
    return embed


def channel_id_from_log(message: discord.Message) -> int:
    """Reads the channel ID back from a footer like 'Kanal-ID: 123 | User-ID: 456'."""
    footer = message.embeds[0].footer.text
    return int(footer.split(" | ")[0].split(": ")[1])


def name_from_log(message: discord.Message) -> str:
    """Reads the normalized requested name back from the JSON log."""
    description = message.embeds[0].description
    raw_json = description.replace("```json", "").replace("```", "").strip()
    return json.loads(raw_json)["input"]["nfkc"]


# --- Persistent views & modals -------------------------------------------

class CreateChannelLobby(discord.ui.View):
    """The public button in the lobby channel."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Voice-Channel erstellen",
        style=discord.ButtonStyle.success,
        emoji="🎙️",
        custom_id="persistent:create_channel_lobby",
    )
    async def create_channel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ChannelNameModal())


class ChannelNameModal(discord.ui.Modal, title="Eigenen Voice-Channel erstellen"):
    channel_name = discord.ui.TextInput(
        label="Name des Voice-Channels",
        placeholder="z.B. Zocker-Ecke",
        min_length=2,
        max_length=50,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        # Defer immediately: Discord drops interactions not answered within 3 seconds
        await interaction.response.defer(ephemeral=True)

        if not bot_config.is_fully_configured():
            await interaction.followup.send(
                "Der Bot ist noch nicht vollständig eingerichtet. Bitte wende dich an einen Administrator.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        category = guild.get_channel(bot_config.category_id)
        if not isinstance(category, discord.CategoryChannel):
            await interaction.followup.send(
                "Konfigurierte Kategorie existiert nicht oder ist ungültig. Bitte informiere einen Admin.",
                ephemeral=True,
            )
            return

        log_channel = guild.get_channel(bot_config.log_channel_id)
        if not isinstance(log_channel, discord.TextChannel):
            await interaction.followup.send(
                "Konfigurierter Log-Kanal existiert nicht oder ist ungültig. Bitte informiere einen Admin.",
                ephemeral=True,
            )
            return

        requested_name = self.channel_name.value.strip()

        # Create the channel with a placeholder first, the AI check runs afterwards
        try:
            voice_channel = await guild.create_voice_channel(
                name=PENDING_NAME,
                category=category,
                reason=f"Custom channel request by {interaction.user}",
            )
        except Exception as e:
            await interaction.followup.send(f"Kanal konnte nicht erstellt werden: {e}", ephemeral=True)
            return

        temp_channel_ids.add(voice_channel.id)
        _save_temp_channels()

        # Move the user over if they are already in a voice channel
        moved = False
        if interaction.user.voice and interaction.user.voice.channel:
            try:
                await interaction.user.move_to(voice_channel)
                moved = True
            except Exception:
                pass

        # Bots can only move members who are already in voice, so everyone
        # else gets a one-click link to the new channel instead
        join_view = discord.utils.MISSING
        if not moved:
            join_view = discord.ui.View()
            join_view.add_item(discord.ui.Button(label="Channel beitreten", emoji="🔊", url=voice_channel.jump_url))

        where = "du wurdest verschoben" if moved else "klick unten, um beizutreten"
        await interaction.followup.send(
            f"Dein Voice-Channel wurde erstellt und {where}. "
            f"Die Namensprüfung für **\"{requested_name}\"** läuft...",
            view=join_view,
            ephemeral=True,
        )

        asyncio.create_task(self.run_moderation(interaction, voice_channel, requested_name, log_channel))
        # on_voice_state_update only fires when someone leaves, so a channel
        # nobody ever joins needs its own timeout
        asyncio.create_task(delete_if_empty(voice_channel.guild, voice_channel.id, delay=UNUSED_CHANNEL_TIMEOUT))

    async def run_moderation(
        self,
        interaction: discord.Interaction,
        voice_channel: discord.VoiceChannel,
        requested_name: str,
        log_channel: discord.TextChannel,
    ):
        # Background task: exceptions here would otherwise vanish silently
        try:
            # The API call blocks, so it runs in a thread to keep the bot responsive
            approved, reason, source = await asyncio.to_thread(
                moderator.check_channel_name_with_ai, requested_name
            )

            if approved:
                nfkc_name, _ = moderator.normalize_text(requested_name)
                new_name, audit_reason = nfkc_name, "Kanalname durch AI freigegeben"
            else:
                new_name, audit_reason = DENIED_NAME, "Kanalname durch AI blockiert"

            try:
                await voice_channel.edit(name=new_name, reason=audit_reason)
            except discord.NotFound:
                pass  # User already left and the channel was auto-deleted
            except Exception as e:
                print(f"Error renaming channel {voice_channel.id}: {e}")

            embed = build_log_embed(interaction, voice_channel, requested_name, approved, reason, source)
            # Denied entries also get the manual "approve" button
            await log_channel.send(embed=embed, view=AdminChannelControls(show_approve=not approved))
        except Exception as e:
            print(f"Moderation task failed for channel {voice_channel.id}: {e}")


class AdminChannelControls(discord.ui.View):
    """Buttons under each log entry. Only members with 'Kanäle verwalten' may use them."""

    def __init__(self, show_approve: bool = True):
        super().__init__(timeout=None)
        if not show_approve:
            self.remove_item(self.force_approve_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.manage_channels:
            return True
        await interaction.response.send_message(
            "⛔ Dafür brauchst du die Berechtigung „Kanäle verwalten“.", ephemeral=True
        )
        return False

    @discord.ui.button(
        label="Kanal löschen",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
        custom_id="persistent:delete_channel",
    )
    async def delete_channel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        try:
            channel_id = channel_id_from_log(interaction.message)
        except Exception:
            await interaction.followup.send(
                "Fehler: Kanal-ID konnte nicht aus dem Log-Embed gelesen werden.", ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(channel_id)
        if not channel:
            await interaction.followup.send(
                "Der Voice-Channel existiert bereits nicht mehr oder wurde gelöscht.", ephemeral=True
            )
            # The entry is inactive now, so its buttons go too
            await interaction.message.edit(view=None)
            return

        try:
            await channel.delete(reason=f"Gelöscht durch Moderator {interaction.user}")
        except Exception as e:
            await interaction.followup.send(f"Fehler beim Löschen des Kanals: {e}", ephemeral=True)
            return
        await interaction.followup.send(f"Kanal #{channel.name} wurde gelöscht.", ephemeral=True)

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.dark_grey()
        embed.title = f"🗑️ {embed.title} (Gelöscht)"
        await interaction.message.edit(embed=embed, view=None)

    @discord.ui.button(
        label="Kanal freigeben (Force Approve)",
        style=discord.ButtonStyle.success,
        emoji="🔓",
        custom_id="persistent:force_approve",
    )
    async def force_approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        try:
            channel_id = channel_id_from_log(interaction.message)
            normalized_name = name_from_log(interaction.message)
        except Exception:
            await interaction.followup.send(
                "Fehler: Kanal-ID oder Name konnte nicht aus dem Log-Embed gelesen werden.", ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(channel_id)
        if not channel:
            await interaction.followup.send("Kanal existiert nicht mehr (wurde evtl. gelöscht).", ephemeral=True)
            await interaction.message.edit(view=None)
            return

        try:
            await channel.edit(name=normalized_name, reason=f"Manuell freigegeben durch {interaction.user}")
        except Exception as e:
            await interaction.followup.send(f"Fehler beim Umbenennen des Kanals: {e}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Kanal freigegeben und in **\"{normalized_name}\"** umbenannt.", ephemeral=True
        )

        embed = interaction.message.embeds[0]
        embed.color = COLOR_APPROVED
        embed.title = f"🟢 Manuell freigegeben von {interaction.user.display_name}: {channel_id}"
        # Approved now, so only the delete button stays
        await interaction.message.edit(embed=embed, view=AdminChannelControls(show_approve=False))


# --- Slash commands ------------------------------------------------------

@bot.tree.command(name="setup", description="Richtet den Custom-Channel-Bot für diesen Discord-Server ein.")
@discord.app_commands.default_permissions(administrator=True)
@discord.app_commands.checks.has_permissions(administrator=True)
async def setup_cmd(
    interaction: discord.Interaction,
    lobby_channel: discord.TextChannel,
    log_channel: discord.TextChannel,
    category: discord.CategoryChannel,
):
    bot_config.update(
        lobby_channel_id=lobby_channel.id,
        log_channel_id=log_channel.id,
        category_id=category.id,
    )
    await interaction.response.send_message(
        f"✅ Setup abgeschlossen!\n"
        f"- **Lobby-Kanal:** {lobby_channel.mention}\n"
        f"- **Moderations-Log-Kanal:** {log_channel.mention}\n"
        f"- **Kategorie für Voice-Channels:** {category.name}",
        ephemeral=True,
    )


@bot.tree.command(name="post_lobby", description="Sendet den Erstellungsknopf in den konfigurierten Lobby-Kanal.")
@discord.app_commands.default_permissions(administrator=True)
@discord.app_commands.checks.has_permissions(administrator=True)
async def post_lobby_cmd(interaction: discord.Interaction):
    if not bot_config.is_fully_configured():
        await interaction.response.send_message(
            "❌ Bot ist noch nicht eingerichtet. Bitte führe zuerst `/setup` aus.", ephemeral=True
        )
        return

    lobby_channel = interaction.guild.get_channel(bot_config.lobby_channel_id)
    if not isinstance(lobby_channel, discord.TextChannel):
        await interaction.response.send_message(
            "❌ Der konfigurierte Lobby-Kanal existiert nicht oder ist kein Textkanal.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title="🎙️ Eigenen Voice-Channel erstellen",
        description=(
            "Klicke auf den Button, gib deinem Channel einen Namen und leg los.\n"
            "Der Name wird automatisch von einer KI geprüft. "
            "Sobald alle den Channel verlassen haben, wird er wieder gelöscht."
        ),
        color=discord.Color.dark_grey(),
    )

    try:
        await lobby_channel.send(embed=embed, view=CreateChannelLobby())
        await interaction.response.send_message("✅ Erstellungs-Embed wurde erfolgreich gesendet.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Fehler beim Senden der Nachricht: {e}", ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
    if isinstance(error, discord.app_commands.MissingPermissions):
        message = "⛔ Dafür brauchst du Administrator-Rechte."
    else:
        print(f"Command error: {error}")
        message = f"❌ Ein Fehler ist aufgetreten: {error}"

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


# --- Events --------------------------------------------------------------

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Deletes a bot-created voice channel once the last member has left."""
    channel = before.channel
    if channel and channel.id in temp_channel_ids and not channel.members:
        # Short grace period in case someone rejoins right away
        await delete_if_empty(member.guild, channel.id, delay=2)


async def delete_if_empty(guild: discord.Guild, channel_id: int, delay: float) -> None:
    """Waits, then deletes the temp channel if it still exists and nobody is in it."""
    await asyncio.sleep(delay)
    channel = guild.get_channel(channel_id)
    if not channel or channel.id not in temp_channel_ids or channel.members:
        return

    # The registry entry is removed in on_guild_channel_delete
    try:
        await channel.delete(reason="Temporärer Voice-Channel ist leer.")
    except discord.NotFound:
        pass
    except Exception as e:
        print(f"Error auto-deleting channel {channel.id}: {e}")


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    """Keeps the registry clean when a temp channel is deleted by hand."""
    if channel.id in temp_channel_ids:
        temp_channel_ids.discard(channel.id)
        _save_temp_channels()


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("CRITICAL ERROR: DISCORD_TOKEN is missing in the .env file.")
        sys.exit(1)

    bot.run(DISCORD_TOKEN)
