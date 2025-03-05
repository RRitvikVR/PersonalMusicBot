import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
from yt_dlp import YoutubeDL
import subprocess
from collections import deque
import asyncio
import psutil
import time
import logging

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("MusicBot")

intents = discord.Intents.default()
intents.message_content = True

# Bot Setup
class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.queue = deque()
        self.current_song = None
        self.current_duration = 0
        self.current_timestamp = 0
        self.playing_message = None
        self.is_paused = False
        self.volume = 100  # Default volume (0-100)
        self.seeking = False  # Flag to prevent multiple seek operations at once
        self.current_process = None  # Track the current ffmpeg process
        self.process_start_time = 0  # Track when the process started
        self.reconnect_voice = False
        self.voice_reconnect_task = None
        self.heartbeat_task = None
        # Add a buffer to store audio data
        self.audio_buffer = bytearray(8192)  # 8KB buffer

    async def setup_hook(self):
        await self.tree.sync()
        # Start voice connection health check
        self.heartbeat_task = self.heartbeat.start()

    # Improved heartbeat task to check voice connection health
    @tasks.loop(seconds=10)  # Increased frequency from 30 to 10 seconds
    async def heartbeat(self):
        for guild in self.guilds:
            # Check if we have a voice client in this guild
            if guild.voice_client and guild.voice_client.is_connected():
                # If we're not playing and not paused, but have a current song, something might be wrong
                if not guild.voice_client.is_playing() and not self.is_paused and self.current_song:
                    # Check if ffmpeg process is still alive but audio stopped
                    if self.current_process and self.current_process.poll() is None:
                        # Process is still running but no audio - check how long it's been
                        current_time = time.time()
                        if current_time - self.process_start_time > 3:  # Reduced from 5 to 3 seconds
                            logger.warning(f"Heartbeat detected stalled playback at {self.current_timestamp}s, attempting recovery")
                            # Attempt recovery by restarting playback from current timestamp
                            self.reconnect_voice = True
                            current_position = self.current_timestamp
                            
                            # Clean up the existing process
                            cleanup_processes()
                            
                            # Schedule reconnection
                            if not self.voice_reconnect_task or self.voice_reconnect_task.done():
                                self.voice_reconnect_task = self.loop.create_task(
                                    reconnect_voice_client(guild, guild.voice_client.channel, current_position)
                                )
                else:
                    # Even when playing, periodically check if the process is healthy
                    if self.current_process and time.time() - self.process_start_time > 30:
                        # Check if process is consuming CPU
                        try:
                            if psutil.pid_exists(self.current_process.pid):
                                proc = psutil.Process(self.current_process.pid)
                                # If CPU usage is extremely low for an active process, it might be stuck
                                if proc.cpu_percent(interval=0.5) < 0.1 and guild.voice_client.is_playing():
                                    logger.warning("Process appears to be stalled despite playback status")
                                    # Force reconnection
                                    self.reconnect_voice = True
                                    current_position = self.current_timestamp
                                    cleanup_processes()
                                    
                                    if not self.voice_reconnect_task or self.voice_reconnect_task.done():
                                        self.voice_reconnect_task = self.loop.create_task(
                                            reconnect_voice_client(guild, guild.voice_client.channel, current_position)
                                        )
                        except Exception as e:
                            logger.error(f"Error checking process health: {e}")

    @heartbeat.before_loop
    async def before_heartbeat(self):
        await self.wait_until_ready()

bot = MusicBot()

# Helper function to get ffmpeg path
def get_ffmpeg_path():
    if os.name == 'nt':  # Windows
        ffmpeg_path = os.path.join(os.path.dirname(__file__), "ffmpeg", "ffmpeg.exe")
        if not os.path.exists(ffmpeg_path):
            # Try system path
            ffmpeg_path = "ffmpeg"
    else:  # Linux/Mac
        # Try using system ffmpeg first
        if subprocess.run(['which', 'ffmpeg'], capture_output=True).returncode == 0:
            ffmpeg_path = 'ffmpeg'
        else:
            ffmpeg_path = os.path.join(os.path.dirname(__file__), "ffmpeg", "ffmpeg")
    
    logger.info(f"Using FFmpeg path: {ffmpeg_path}")
    return ffmpeg_path

# Helper function to clean up processes - modified to use process ID
def cleanup_processes(specific_pid=None):
    try:
        if specific_pid:
            # Only terminate a specific process
            try:
                proc = psutil.Process(specific_pid)
                if proc.is_running():
                    logger.info(f"Terminating specific process with PID {specific_pid}")
                    proc.terminate()
                    proc.wait(timeout=3)  # Wait for termination
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired) as e:
                logger.warning(f"Exception during specific process cleanup: {e}")
        elif bot.current_process:
            # Only terminate the bot's current process
            try:
                if bot.current_process.poll() is None:
                    logger.info("Terminating current bot process")
                    bot.current_process.terminate()
                    # Wait a moment for process to terminate
                    try:
                        bot.current_process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        logger.warning("Process termination timed out, forcing kill")
                        if os.name == 'nt':  # Windows
                            subprocess.run(['taskkill', '/F', '/T', '/PID', str(bot.current_process.pid)])
                        else:  # Linux/Mac
                            bot.current_process.kill()
            except Exception as e:
                logger.error(f"Error terminating current process: {e}")
    except Exception as e:
        logger.error(f"Error in cleanup_processes: {e}")

@bot.event
async def on_ready():
    logger.info(f"Bot logged in as {bot.user}")

# Custom PCM audio source with larger buffer
class BufferedPCMAudio(discord.AudioSource):
    def __init__(self, source, buffer_size=4096):
        self.source = source
        self.buffer = bytearray(buffer_size)
        self.buffer_size = buffer_size
        self.read_size = 3840  # Discord's read frame size (typically 20ms of 48kHz audio)
        self._is_opus = False
        self.last_read_time = time.time()

    def read(self):
        # Read data from source into our buffer
        try:
            bytes_read = self.source.read(self.read_size)
            current_time = time.time()
            time_diff = current_time - self.last_read_time
            
            # Log if read took too long
            if time_diff > 0.1:  # More than 100ms between reads
                logger.warning(f"Audio read delay: {time_diff:.3f}s")
            
            self.last_read_time = current_time
            
            if not bytes_read:
                return b''
                
            return bytes_read
        except Exception as e:
            logger.error(f"Error reading audio data: {e}")
            return b''

    def cleanup(self):
        try:
            self.source.close()
        except:
            pass

# Voice reconnection helper with improved stability
async def reconnect_voice_client(guild, channel, timestamp):
    logger.info(f"Attempting to reconnect voice in guild {guild.id}, resuming at {timestamp}s")
    try:
        # Disconnect if connected
        if guild.voice_client:
            try:
                await guild.voice_client.disconnect(force=True)
            except Exception as e:
                logger.error(f"Error disconnecting: {e}")
        
        # Wait a moment before reconnecting
        await asyncio.sleep(2)
        
        # Reconnect
        vc = await channel.connect()
        
        # Resume playback if we have a current song
        if bot.current_song:
            bot.current_timestamp = timestamp
            bot.seeking = False
            
            # Find an interaction to use for UI updates - this is a workaround
            for channel in guild.text_channels:
                if channel.permissions_for(guild.me).send_messages:
                    class DummyInteraction:
                        async def followup(self):
                            return channel
                        
                        response = property(lambda self: self)
                        
                        @staticmethod
                        async def send_message(*args, **kwargs):
                            return await channel.send(*args, **kwargs)
                        
                        @staticmethod
                        async def is_done():
                            return True
                    
                    dummy_interaction = DummyInteraction()
                    dummy_interaction.guild = guild
                    
                    # Resume playback
                    await play_audio_at_position(vc, dummy_interaction, bot.current_song, 
                                               bot.current_timestamp, bot.current_duration)
                    break
    except Exception as e:
        logger.error(f"Failed to reconnect voice: {e}")
    finally:
        bot.reconnect_voice = False

# Play audio at specific position - improved for stability
async def play_audio_at_position(vc, interaction, audio_url, position, duration, title=None):
    # Clean up any existing processes
    cleanup_processes()
    
    ffmpeg_path = get_ffmpeg_path()
    volume_multiplier = bot.volume / 100.0
    was_paused = bot.is_paused
    
    # Update UI if we have a title
    if title:
        await send_playing_ui(interaction, title, duration)
    
    try:
        # Create ffmpeg process with improved buffer settings and higher priority
        process = subprocess.Popen(
            [
                ffmpeg_path, 
                '-reconnect', '1',
                '-reconnect_streamed', '1',
                '-reconnect_delay_max', '5',
                '-ss', str(position), 
                '-i', audio_url,
                '-filter:a', f'volume={volume_multiplier}',
                '-f', 's16le', 
                '-ar', '48000', 
                '-ac', '2',
                '-bufsize', '8M',  # 8MB buffer
                'pipe:1',
                '-loglevel', 'warning'
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=8192,  # Increased buffer size
            # Set higher process priority
            creationflags=subprocess.HIGH_PRIORITY_CLASS if os.name == 'nt' else 0
        )
        
        # Store the process and its start time
        bot.current_process = process
        bot.process_start_time = time.time()
        
        # Play the audio with our custom buffer
        buffered_source = BufferedPCMAudio(process.stdout, buffer_size=8192)
        vc.play(
            buffered_source,
            after=lambda e: bot.loop.create_task(handle_playback_finished(vc, interaction, e))
        )
        
        # Restore pause state if needed
        if was_paused:
            vc.pause()
        
        # Start UI update task if not running
        if not update_ui.is_running():
            update_ui.start(vc, interaction)
        
        # Update UI with current position
        if bot.playing_message:
            try:
                embed = bot.playing_message.embeds[0]
                embed.set_field_at(
                    0,
                    name="Duration",
                    value=f"{format_timestamp(position)} / {format_timestamp(duration)}"
                )
                await bot.playing_message.edit(embed=embed)
            except Exception as e:
                logger.error(f"Error updating UI after playback: {e}")
    except Exception as e:
        logger.error(f"Error playing audio at position {position}: {e}")
        # Reset seeking flag
        bot.seeking = False

# Play Command - improved
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
        except discord.ClientException as e:
            logger.error(f"Failed to connect to voice channel: {e}")
            await interaction.response.send_message("Failed to connect to the voice channel.", ephemeral=True)
            return
    else:
        vc = interaction.guild.voice_client
        # Check if the bot is in a different voice channel
        if vc.channel.id != voice_channel.id:
            await interaction.response.send_message(
                "I'm already in a different voice channel. Use `/stop` first.", ephemeral=True
            )
            return

    await interaction.response.defer()

    # Improved YoutubeDL options for better stability
    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "extract_flat": True,
        "skip_download": True,
        "force_generic_extractor": False,
        # Add timeout options
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            logger.info(f"Extracting info for URL: {url}")
            info = ydl.extract_info(url, download=False)
            
        if "entries" in info:  # It's a playlist
            await interaction.followup.send("Playlists are not supported. Please provide a single video URL.")
            return
            
        audio_url = info["url"]
        title = info.get("title", "Unknown Title")
        duration = info.get("duration", 0)

        bot.queue.append((audio_url, title, duration))
        await interaction.followup.send(f"Added to queue: **{title}**")

        if not vc.is_playing() and not bot.is_paused:
            await play_next_in_queue(vc, interaction)

    except Exception as e:
        logger.error(f"Error playing audio: {e}")
        await interaction.followup.send(f"An error occurred: {str(e)[:1900]}")  # Truncate long error messages

# Play Next Song in Queue - Modified to use our new play_audio function
async def play_next_in_queue(vc, interaction):
    # Don't play next if we're in the middle of reconnecting
    if bot.reconnect_voice:
        return
        
    # Clean up any existing processes first
    cleanup_processes()
    
    if bot.queue:
        audio_url, title, duration = bot.queue.popleft()
        bot.current_song = audio_url
        bot.current_duration = duration
        bot.current_timestamp = 0
        bot.seeking = False  # Reset seeking flag for new song

        # Play audio from beginning
        await play_audio_at_position(vc, interaction, audio_url, 0, duration, title)
    else:
        bot.current_song = None
        bot.current_process = None
        if not interaction.response.is_done():
            await interaction.followup.send("Queue is empty.")

# Handler for when playback finishes - modified to log errors
async def handle_playback_finished(vc, interaction, error=None):
    if error:
        logger.error(f"Error during playback: {error}")
    
    # Add a small delay to ensure seeking flag is properly set
    await asyncio.sleep(0.1)
    
    # Only auto-play next if not caused by seeking or manual stop
    if not bot.seeking and bot.current_song and not bot.reconnect_voice:
        await play_next_in_queue(vc, interaction)

# Send Playing UI with Buttons
async def send_playing_ui(interaction, title, duration):
    embed = discord.Embed(
        title="🎶 Now Playing",
        description=f"**{title}**",
        color=discord.Color.blue()
    )
    embed.add_field(name="Duration", value=f"{format_timestamp(bot.current_timestamp)} / {format_timestamp(duration)}")
    embed.set_footer(text="Use the buttons below to control playback.")

    view = PlaybackControls()

    if bot.playing_message:
        try:
            # Attempt to delete the previous playing message if it exists
            await bot.playing_message.delete()
        except discord.NotFound:
            pass  # If the message is already deleted, just ignore
        except Exception as e:
            logger.error(f"Error deleting previous message: {e}")

    # Send a new message with the updated UI
    try:
        bot.playing_message = await interaction.followup.send(embed=embed, view=view)
    except Exception as e:
        logger.error(f"Error sending playing UI: {e}")

# Update UI Task - with improved error handling
@tasks.loop(seconds=1)
async def update_ui(vc, interaction):
    try:
        if vc.is_playing() and not bot.is_paused and not bot.seeking:
            bot.current_timestamp += 1
            # Ensure timestamp doesn't exceed duration
            if bot.current_timestamp > bot.current_duration:
                bot.current_timestamp = bot.current_duration
                
            # Check if playing_message still exists
            if bot.playing_message:
                try:
                    embed = bot.playing_message.embeds[0]
                    embed.set_field_at(
                        0,
                        name="Duration",
                        value=f"{format_timestamp(bot.current_timestamp)} / {format_timestamp(bot.current_duration)}"
                    )
                    await bot.playing_message.edit(embed=embed)
                except discord.NotFound:
                    logger.warning("Playing message not found, stopping UI updates")
                    update_ui.stop()  # Stop the task if the message is gone
                except Exception as e:
                    logger.error(f"Error updating UI: {e}")
                    # Don't stop the task for other errors
        elif bot.is_paused or bot.seeking:
            # Don't increment time when paused or seeking
            pass
        else:
            if not vc.is_connected():
                logger.warning("Voice client disconnected, stopping UI updates")
                update_ui.stop()
    except Exception as e:
        logger.error(f"Exception in update_ui task: {e}")
        # Don't stop the task for general errors

@update_ui.before_loop
async def before_update_ui():
    await bot.wait_until_ready()

# Format Timestamp
def format_timestamp(seconds):
    if seconds is None:
        seconds = 0
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours:02}:{minutes:02}:{seconds:02}"
    return f"{minutes:02}:{seconds:02}"

# Queue Command
@bot.tree.command(name="queue", description="Display the current song queue")
async def queue_command(interaction: discord.Interaction):
    await interaction.response.defer()
    
    if not bot.queue:
        if bot.current_song and bot.playing_message:
            try:
                embed = discord.Embed(
                    title="🎶 Music Queue",
                    description="**Currently Playing:**\n" + bot.playing_message.embeds[0].description,
                    color=discord.Color.blue()
                )
                embed.add_field(name="Queue", value="No songs in queue")
            except Exception:
                embed = discord.Embed(
                    title="🎶 Music Queue",
                    description="Currently playing a song, but there's no queue.",
                    color=discord.Color.blue()
                )
        else:
            embed = discord.Embed(
                title="🎶 Music Queue",
                description="Nothing is playing and the queue is empty.",
                color=discord.Color.blue()
            )
    else:
        embed = discord.Embed(
            title="🎶 Music Queue",
            color=discord.Color.blue()
        )
        
        if bot.current_song and bot.playing_message:
            try:
                embed.description = "**Currently Playing:**\n" + bot.playing_message.embeds[0].description
            except Exception:
                embed.description = "**Currently Playing a song**"
        
        queue_text = ""
        for i, (_, title, duration) in enumerate(bot.queue, 1):
            queue_text += f"{i}. **{title}** ({format_timestamp(duration)})\n"
            
            # Split into multiple fields if queue is too long
            if i % 10 == 0:
                embed.add_field(name=f"Queue (Songs {i-9}-{i})", value=queue_text, inline=False)
                queue_text = ""
        
        if queue_text:
            embed.add_field(name="Queue", value=queue_text, inline=False)
    
    await interaction.followup.send(embed=embed)

# Remove Command
@bot.tree.command(name="remove", description="Remove a song from the queue")
@app_commands.describe(position="Position of the song in the queue (use /queue to see positions)")
async def remove_command(interaction: discord.Interaction, position: int):
    if not bot.queue:
        await interaction.response.send_message("The queue is empty!", ephemeral=True)
        return
    
    if position < 1 or position > len(bot.queue):
        await interaction.response.send_message(f"Invalid position. Please enter a number between 1 and {len(bot.queue)}.", ephemeral=True)
        return
    
    # Convert position to 0-based index
    index = position - 1
    _, title, _ = bot.queue[index]
    bot.queue.remove(bot.queue[index])
    
    await interaction.response.send_message(f"Removed **{title}** from the queue.")

# Volume Command - modified to use play_audio_at_position
@bot.tree.command(name="volume", description="Set the volume of the player (0-100)")
@app_commands.describe(level="Volume level from 0 to 100")
async def volume_command(interaction: discord.Interaction, level: int):
    if level < 0 or level > 100:
        await interaction.response.send_message("Volume must be between 0 and 100", ephemeral=True)
        return
    
    # Store the volume level
    bot.volume = level
    
    # Apply volume immediately if playing
    if interaction.guild.voice_client and (interaction.guild.voice_client.is_playing() or interaction.guild.voice_client.is_paused()):
        await interaction.response.defer()
        
        vc = interaction.guild.voice_client
        if vc.is_playing() or vc.is_paused():
            # Set the seeking flag to prevent multiple operations
            bot.seeking = True
            was_paused = vc.is_paused()
            current_position = bot.current_timestamp
            
            # Play audio with new volume at current position
            await play_audio_at_position(vc, interaction, bot.current_song, current_position, bot.current_duration)
            
            # Restore pause state
            if was_paused:
                vc.pause()
                bot.is_paused = True
            
            bot.seeking = False
        
        await interaction.followup.send(f"Volume set to {level}%")
    else:
        await interaction.response.send_message(f"Volume set to {level}% (will apply to next song)", ephemeral=True)

# Playback Controls
class PlaybackControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.primary)
    async def pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Just acknowledge the interaction to keep the button responsive
        await interaction.response.defer(ephemeral=True)
        
        vc = interaction.guild.voice_client
        if vc and vc.is_playing() and not bot.is_paused:
            vc.pause()
            button.label = "Resume"
            bot.is_paused = True
            await interaction.message.edit(view=self)
        elif vc and vc.is_paused() and bot.is_paused:
            vc.resume()
            button.label = "Pause"
            bot.is_paused = False
            await interaction.message.edit(view=self)

    @discord.ui.button(label="Forward", style=discord.ButtonStyle.success)
    async def forward(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        # Check if we can perform seeking
        if bot.seeking or not interaction.guild.voice_client or not bot.current_song:
            return
            
        # Set seeking flag and calculate new position
        bot.seeking = True
        try:
            old_timestamp = bot.current_timestamp
            bot.current_timestamp = min(bot.current_duration, bot.current_timestamp + 10)
            
            # Only seek if position actually changed
            if old_timestamp != bot.current_timestamp:
                vc = interaction.guild.voice_client
                await play_audio_at_position(vc, interaction, bot.current_song, bot.current_timestamp, bot.current_duration)
            else:
                # If at the end already, no need to seek
                bot.seeking = False
        except Exception as e:
            logger.error(f"Error during forward: {e}")
            bot.seeking = False

    @discord.ui.button(label="Backward", style=discord.ButtonStyle.success)
    async def backward(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        # Check if we can perform seeking
        if bot.seeking or not interaction.guild.voice_client or not bot.current_song:
            return
            
        # Set seeking flag and calculate new position
        bot.seeking = True
        try:
            old_timestamp = bot.current_timestamp
            bot.current_timestamp = max(0, bot.current_timestamp - 10)
            
            # Only seek if position actually changed
            if old_timestamp != bot.current_timestamp:
                vc = interaction.guild.voice_client
                await play_audio_at_position(vc, interaction, bot.current_song, bot.current_timestamp, bot.current_duration)
            else:
                # If at the start already, no need to seek
                bot.seeking = False
        except Exception as e:
            logger.error(f"Error during backward: {e}")
            bot.seeking = False

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.red)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Acknowledge the interaction to keep the button responsive
        await interaction.response.defer(ephemeral=True)
        
        if interaction.guild.voice_client:
            # Stop the UI update task
            update_ui.stop()
            
            # Clean up processes
            cleanup_processes()
            
            # Disconnect and clear queue
            await interaction.guild.voice_client.disconnect()
            bot.queue.clear()
            bot.current_song = None
            bot.current_timestamp = 0
            bot.is_paused = False
            bot.seeking = False
            bot.current_process = None

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.danger)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Acknowledge the interaction to keep the button responsive
        await interaction.response.defer(ephemeral=True)
        
        # Prevent multiple clicks from causing issues
        if bot.seeking:
            return
            
        vc = interaction.guild.voice_client
        if not vc:
            return

        if vc.is_playing() or vc.is_paused():
            bot.seeking = True  # Set flag to prevent auto-play
            
            # Stop current playback
            vc.stop()
            
            # Clean up processes
            cleanup_processes()
            
            # Make sure we are not trying to send a new UI if the previous message was deleted
            if bot.playing_message:
                try:
                    await bot.playing_message.delete()  # Delete old message if it exists
                except discord.NotFound:
                    pass  # If the message was already deleted, ignore
                except Exception as e:
                    logger.error(f"Error deleting message during skip: {e}")
            
            bot.seeking = False
            await play_next_in_queue(vc, interaction)

# Ping command to check bot latency
@bot.tree.command(name="ping", description="Check the bot's latency")
async def ping_command(interaction: discord.Interaction):
    start_time = time.time()
    await interaction.response.send_message("Pinging...")
    
    end_time = time.time()
    latency = (end_time - start_time) * 1000
    
    await interaction.edit_original_response(content=f"Pong! Latency: {latency:.2f}ms | Discord API: {bot.latency * 1000:.2f}ms")


bot.run("Post ur token here")
