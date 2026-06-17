import discord
from discord.ext import commands
import asyncio
import sys
import json

# Set utf-8 stdout encoding for Windows compatibility
sys.stdout.reconfigure(encoding='utf-8')

from config import bot_config, DISCORD_TOKEN
import moderator

# Initialize intents
intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True

class CustomChannelBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Register persistent views
        self.add_view(CreateChannelLobby())
        self.add_view(AdminChannelControls(show_approve=True))
        self.add_view(AdminChannelControls(show_approve=False))
        print("Persistent views registered successfully.")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print("Syncing slash commands...")
        try:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} application commands.")
        except Exception as e:
            print(f"Failed to sync application commands: {e}")

bot = CustomChannelBot()

# --- PERSISTENT VIEWS & MODALS ---

class CreateChannelLobby(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Voice-Channel erstellen",
        style=discord.ButtonStyle.success,
        emoji="🎙️",
        custom_id="persistent:create_channel_lobby"
    )
    async def create_channel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ChannelNameModal())


class ChannelNameModal(discord.ui.Modal, title="Eigenen Voice-Channel erstellen"):
    channel_name = discord.ui.TextInput(
        label="Name des Voice-Channels",
        placeholder="z.B. Zocker-Ecke",
        min_length=2,
        max_length=50,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        # Defer immediately to prevent interaction timeout (3-second limit)
        await interaction.response.defer(ephemeral=True)
        
        if not bot_config.is_fully_configured():
            await interaction.followup.send(
                "Der Bot ist noch nicht vollständig eingerichtet. Bitte wende dich an einen Administrator.",
                ephemeral=True
            )
            return

        guild = interaction.guild
        category = guild.get_channel(bot_config.category_id)
        if not category or not isinstance(category, discord.CategoryChannel):
            await interaction.followup.send(
                "Konfigurierte Kategorie existiert nicht oder ist ungültig. Bitte informiere einen Admin.",
                ephemeral=True
            )
            return

        log_channel = guild.get_channel(bot_config.log_channel_id)
        if not log_channel or not isinstance(log_channel, discord.TextChannel):
            await interaction.followup.send(
                "Konfigurierter Log-Kanal existiert nicht oder ist ungültig. Bitte informiere einen Admin.",
                ephemeral=True
            )
            return

        requested_name = self.channel_name.value.strip()

        # Create voice channel with placeholder name
        temp_name = "⏳ | Prüfung ausstehend..."
        try:
            voice_channel = await guild.create_voice_channel(
                name=temp_name,
                category=category,
                reason=f"Custom channel request by {interaction.user}"
            )
        except Exception as e:
            await interaction.followup.send(
                f"Kanal konnte nicht erstellt werden: {e}",
                ephemeral=True
            )
            return

        # Attempt to move the user if they are in a voice channel
        moved = False
        if interaction.user.voice and interaction.user.voice.channel:
            try:
                await interaction.user.move_to(voice_channel)
                moved = True
            except Exception:
                pass

        if moved:
            await interaction.followup.send(
                f"Dein Voice-Channel wurde erstellt und du wurdest verschoben. Die Namensprüfung für **\"{requested_name}\"** läuft...",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"Dein Voice-Channel wurde erstellt. Bitte trete ihm bei. Die Namensprüfung für **\"{requested_name}\"** läuft...",
                ephemeral=True
            )

        # Offload AI moderation checking to background task
        asyncio.create_task(self.run_moderation(interaction, voice_channel, requested_name, log_channel))

    async def run_moderation(self, interaction: discord.Interaction, voice_channel: discord.VoiceChannel, requested_name: str, log_channel: discord.TextChannel):
        # Query Gemma 4 AI in a thread to keep the event loop responsive
        approved, reason, source = await asyncio.to_thread(moderator.check_channel_name_with_ai, requested_name)
        nfkc_name, stripped_name = moderator.normalize_text(requested_name)

        if approved:
            # Apply approved name
            try:
                await voice_channel.edit(name=nfkc_name, reason="Kanalname durch AI freigegeben")
            except Exception as e:
                print(f"Error renaming approved channel {voice_channel.id}: {e}")

            # Create JSON log structure for devs
            log_data = {
                "event": "voice_channel_created",
                "status": "approved",
                "guild_id": interaction.guild.id,
                "channel_id": voice_channel.id,
                "creator": {
                    "id": interaction.user.id,
                    "name": interaction.user.name
                },
                "input": {
                    "raw": requested_name,
                    "nfkc": nfkc_name,
                    "stripped": stripped_name
                },
                "moderation": {
                    "approved": True,
                    "reason": reason,
                    "source": source
                }
            }
            json_log = json.dumps(log_data, indent=2, ensure_ascii=False)

            # Send log to admins
            embed = discord.Embed(
                title=f"[DEV LOG] Channel Approved: {voice_channel.id}",
                description=f"```json\n{json_log}\n```",
                color=0x2ecc71  # Emerald green
            )
            embed.set_footer(text=f"Kanal-ID: {voice_channel.id} | User-ID: {interaction.user.id}")

            # Approved view has "Kanal löschen"
            await log_channel.send(embed=embed, view=AdminChannelControls(show_approve=False))
        else:
            # Apply denied name
            try:
                await voice_channel.edit(name="❌ | Name abgelehnt", reason="Kanalname durch AI blockiert")
            except Exception as e:
                print(f"Error renaming denied channel {voice_channel.id}: {e}")

            # Create JSON log structure for devs
            log_data = {
                "event": "voice_channel_created",
                "status": "denied",
                "guild_id": interaction.guild.id,
                "channel_id": voice_channel.id,
                "creator": {
                    "id": interaction.user.id,
                    "name": interaction.user.name
                },
                "input": {
                    "raw": requested_name,
                    "nfkc": nfkc_name,
                    "stripped": stripped_name
                },
                "moderation": {
                    "approved": False,
                    "reason": reason,
                    "source": source
                }
            }
            json_log = json.dumps(log_data, indent=2, ensure_ascii=False)

            # Send log to admins
            embed = discord.Embed(
                title=f"[DEV LOG] Channel Blocked: {voice_channel.id}",
                description=f"```json\n{json_log}\n```",
                color=0xe74c3c  # Alizarin red
            )
            embed.set_footer(text=f"Kanal-ID: {voice_channel.id} | User-ID: {interaction.user.id}")

            # Denied view has both "Kanal freigeben" and "Kanal löschen"
            await log_channel.send(embed=embed, view=AdminChannelControls(show_approve=True))


class AdminChannelControls(discord.ui.View):
    def __init__(self, show_approve: bool = True):
        super().__init__(timeout=None)
        if not show_approve:
            self.remove_item(self.force_approve_button)

    @discord.ui.button(
        label="Kanal löschen",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
        custom_id="persistent:delete_channel"
    )
    async def delete_channel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        try:
            footer = interaction.message.embeds[0].footer.text
            parts = footer.split(" | ")
            channel_id = int(parts[0].split(": ")[1])
        except Exception:
            await interaction.followup.send(
                "Fehler: Kanal-ID konnte nicht aus dem Log-Embed gelesen werden.",
                ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(channel_id)
        if channel:
            try:
                await channel.delete(reason=f"Gelöscht durch Moderator {interaction.user}")
                await interaction.followup.send(f"Kanal #{channel.name} wurde gelöscht.", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"Fehler beim Löschen des Kanals: {e}", ephemeral=True)
                return
            
            # Disable control buttons in the log embed
            embed = interaction.message.embeds[0]
            embed.color = discord.Color.dark_grey()
            embed.title = f"🗑️ {embed.title} (Gelöscht)"
            await interaction.message.edit(embed=embed, view=None)
        else:
            await interaction.followup.send(
                "Der Voice-Channel existiert bereits nicht mehr oder wurde gelöscht.",
                ephemeral=True
            )
            # Remove buttons from log anyway since it's inactive
            await interaction.message.edit(view=None)

    @discord.ui.button(
        label="Kanal freigeben (Force Approve)",
        style=discord.ButtonStyle.success,
        emoji="🔓",
        custom_id="persistent:force_approve"
    )
    async def force_approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        try:
            footer = interaction.message.embeds[0].footer.text
            parts = footer.split(" | ")
            channel_id = int(parts[0].split(": ")[1])
            
            # Extract normalized name from JSON log in description
            description = interaction.message.embeds[0].description
            raw_json = description.replace("```json", "").replace("```", "").strip()
            log_data = json.loads(raw_json)
            normalized_name = log_data["input"]["nfkc"]
        except Exception:
            await interaction.followup.send(
                "Fehler: Kanal-ID oder Name konnte nicht aus dem Log-Embed gelesen werden.",
                ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(channel_id)
        if channel:
            try:
                await channel.edit(name=normalized_name, reason=f"Manuell freigegeben durch {interaction.user}")
                await interaction.followup.send(f"Kanal freigegeben und in **\"{normalized_name}\"** umbenannt.", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"Fehler beim Umbenennen des Kanals: {e}", ephemeral=True)
                return

            # Update log embed to approved state
            embed = interaction.message.embeds[0]
            embed.color = 0x2ecc71  # Emerald green
            embed.title = "🟢 Custom Channel freigegeben (Manuell Freigegeben)"
            
            # Keep only the delete button on the view
            await interaction.message.edit(embed=embed, view=AdminChannelControls(show_approve=False))
        else:
            await interaction.followup.send(
                "Kanal existiert nicht mehr (wurde evtl. gelöscht).",
                ephemeral=True
            )
            await interaction.message.edit(view=None)


# --- APPLICATION COMMANDS (SLASH COMMANDS) ---

@bot.tree.command(
    name="setup",
    description="Richtet den Custom-Channel-Bot für diesen Discord-Server ein."
)
@discord.app_commands.checks.has_permissions(administrator=True)
async def setup_cmd(
    interaction: discord.Interaction,
    lobby_channel: discord.TextChannel,
    log_channel: discord.TextChannel,
    category: discord.CategoryChannel
):
    bot_config.update(
        lobby_channel_id=lobby_channel.id,
        log_channel_id=log_channel.id,
        category_id=category.id
    )
    
    await interaction.response.send_message(
        f"✅ Setup abgeschlossen!\n"
        f"- **Lobby-Kanal:** {lobby_channel.mention}\n"
        f"- **Moderations-Log-Kanal:** {log_channel.mention}\n"
        f"- **Kategorie für Voice-Channels:** {category.name}",
        ephemeral=True
    )


@bot.tree.command(
    name="post_lobby",
    description="Sendet den Erstellungsknopf in den konfigurierten Lobby-Kanal."
)
@discord.app_commands.checks.has_permissions(administrator=True)
async def post_lobby_cmd(interaction: discord.Interaction):
    if not bot_config.is_fully_configured():
        await interaction.response.send_message(
            "❌ Bot ist noch nicht eingerichtet. Bitte führe zuerst `/setup` aus.",
            ephemeral=True
        )
        return

    lobby_channel = interaction.guild.get_channel(bot_config.lobby_channel_id)
    if not lobby_channel or not isinstance(lobby_channel, discord.TextChannel):
        await interaction.response.send_message(
            "❌ Der konfigurierte Lobby-Kanal existiert nicht oder ist kein Textkanal.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="[Test]",
        description=(
            "Klicke auf den Button unten, um einen temporären Voice-Channel zu erstellen.\n"
            "Dieses Modul dient zur Demonstration der AI-Moderation und Normalisierung."
        ),
        color=discord.Color.dark_grey()
    )

    view = CreateChannelLobby()
    try:
        await lobby_channel.send(embed=embed, view=view)
        await interaction.response.send_message(
            "✅ Erstellungs-Embed wurde erfolgreich gesendet.",
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(
            f"❌ Fehler beim Senden der Nachricht: {e}",
            ephemeral=True
        )


# --- EVENT HANDLERS ---

@bot.event
async def on_voice_state_update(member, before, after):
    """Auto-deletes empty custom voice channels inside the configured category."""
    if not bot_config.is_fully_configured():
        return

    # Check if user left a voice channel under the custom category
    if before.channel and before.channel.category_id == bot_config.category_id:
        channel = before.channel
        
        # Verify it's a VoiceChannel and is now empty
        if isinstance(channel, discord.VoiceChannel) and len(channel.members) == 0:
            try:
                # Add a brief 2-second sleep to prevent race conditions if someone joins back instantly
                await asyncio.sleep(2)
                # Fetch fresh status of the channel
                channel_fresh = member.guild.get_channel(channel.id)
                if channel_fresh and len(channel_fresh.members) == 0:
                    await channel_fresh.delete(reason="Temporärer Voice-Channel ist jetzt leer.")
            except Exception as e:
                print(f"Error auto-deleting channel {channel.id}: {e}")


# Run the bot
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("CRITICAL ERROR: DISCORD_TOKEN is missing in the .env file.")
        sys.exit(1)
    
    bot.run(DISCORD_TOKEN)
