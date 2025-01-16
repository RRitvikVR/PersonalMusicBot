import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
from yt_dlp import YoutubeDL
import subprocess
from collections import deque
import asyncio

intents = discord.Intents.default()
intents.message_content = True

class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.queue = deque()
        self.current_song = None
        self.current_duration = 0
        self.current_timestamp = 0
        self.playing_message = None
        self.is_paused = False  

    async def setup_hook(self):
        await self.tree.sync()

bot = MusicBot()

@bot.event
async def on_ready():
    print(f"Bot logged in as {bot.user}")

# Play Command
@bot.tree.command(name="play", description="Play a song in a voice channel.")
@app_commands.describe(url="The YouTube URL of the song to play.")
async def play(interaction: discord.Interaction, url: str):
    if not interaction.user.voice:
        await interaction.response.send_message("You must be in a voice channel to use this command.", ephemeral=True)
        return

    voice_channel = interaction.user.voice.channel
    if interaction.guild.voice_client is None:
        try:
            vc = await voice_channel.connect()
        except discord.ClientException:
            await interaction.response.send_message("Failed to connect to the voice channel.", ephemeral=True)
            return
    else:
        vc = interaction.guild.voice_client

    await interaction.response.defer()

    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        audio_url = info["url"]
        title = info.get("title", "Unknown Title")
        duration = info.get("duration", 0)

        bot.queue.append((audio_url, title, duration))
        await interaction.followup.send(f"Added to queue: **{title}**")

        if not vc.is_playing():
            await play_next_in_queue(vc, interaction)

    except Exception as e:
        print(f"Error playing audio: {e}")
        await interaction.followup.send(f"An error occurred: {e}")

# Play Next Song in Queue
async def play_next_in_queue(vc, interaction):
    if bot.queue:
        audio_url, title, duration = bot.queue.popleft()
        bot.current_song = title
        bot.current_duration = duration
        bot.current_timestamp = 0

        ffmpeg_path = os.path.join(os.path.dirname(__file__), "ffmpeg", "ffmpeg.exe")
        if not os.path.exists(ffmpeg_path):
            await interaction.followup.send(
                "FFmpeg not found. Ensure ffmpeg.exe is in a folder named 'ffmpeg' in the same directory as this script."
            )
            return

        process = subprocess.Popen(
            [ffmpeg_path, '-i', audio_url, '-f', 's16le', '-ar', '48000', '-ac', '2', 'pipe:1'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        vc.play(discord.PCMAudio(process.stdout), after=lambda e: bot.loop.create_task(play_next_in_queue(vc, interaction)))

        # Create and send the UI
        await send_playing_ui(interaction, title, duration)
        update_ui.start(vc, interaction)
    else:
        bot.current_song = None
        await interaction.followup.send("Queue is empty.")

# Send Playing UI with Buttons
async def send_playing_ui(interaction, title, duration):
    embed = discord.Embed(
        title="🎶 Now Playing",
        description=f"**{title}**",
        color=discord.Color.blue()
    )
    embed.add_field(name="Duration", value=f"{format_timestamp(0)} / {format_timestamp(duration)}")
    embed.set_footer(text="Use the buttons below to control playback.")

    view = PlaybackControls()

    if bot.playing_message:
        await bot.playing_message.delete()

    bot.playing_message = await interaction.followup.send(embed=embed, view=view)

# Update UI Task
@tasks.loop(seconds=1)
async def update_ui(vc, interaction):
    if vc.is_playing() and not bot.is_paused:
        bot.current_timestamp += 1
        embed = bot.playing_message.embeds[0]
        embed.set_field_at(
            0,
            name="Duration",
            value=f"{format_timestamp(bot.current_timestamp)} / {format_timestamp(bot.current_duration)}"
        )
        await bot.playing_message.edit(embed=embed)
    elif bot.is_paused:
        pass
    else:
        update_ui.stop()


def format_timestamp(seconds):
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes:02}:{seconds:02}"

# Playback Controls
class PlaybackControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.primary)
    async def pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
            interaction.guild.voice_client.pause()
            button.label = "Resume"
            bot.is_paused = True  # Set paused state
            await interaction.message.edit(view=self)
            await interaction.response.defer()  
        elif interaction.guild.voice_client and interaction.guild.voice_client.is_paused():
            interaction.guild.voice_client.resume()
            button.label = "Pause"
            bot.is_paused = False  # Reset paused state
            await interaction.message.edit(view=self)
            await interaction.response.defer()  

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.danger)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
            interaction.guild.voice_client.stop()
            await interaction.response.send_message("Skipped to the next track.", ephemeral=True)
            await interaction.response.defer() 

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.red)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild.voice_client:
            await interaction.guild.voice_client.disconnect()
            bot.queue.clear()
            update_ui.stop()
            await interaction.response.send_message("Stopped playback and cleared the queue.", ephemeral=True)
            await interaction.response.defer() 

# Run the Bot
bot.run("INSERT TOKEN HERE")
