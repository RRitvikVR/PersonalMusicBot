#### **Overview**  
MusicBot is a for music on discord


### **Features**  
**Music Playback** – Play songs from YouTube with seeking, skipping, pausing, and stopping support.  
**Queue Management** – Add, remove, and view songs in the queue.  
 **Playback Controls** – Pause, resume, skip, stop, and seek forward/backward.  
 **Volume Control** – Adjust volume from 0 to 100%.  
 **Auto-Reconnect** – Recovers from unexpected disconnects and resumes playback.  
 **Performance Optimizations** – Uses buffered audio and ffmpeg process monitoring for smooth playback.  
**Logging & Debugging** – Provides real-time logs for easier troubleshooting.  

### **Commands**  
 `/play <url>` - Plays a song from YouTube. 
 `/queue` - Displays the current queue. 
 `/remove <position>` -Removes a song from the queue. 
 `/volume <level>` - Sets the volume (0-100). 
 `/ping` - Checks bot latency. 

### **Installation & Setup**  
#### **Requirements**  
- Python 3.8 or later  
- `ffmpeg` installed and accessible from the system path  
- The following Python libraries:  

#### **Install Required Packages**  
Run the following command to install dependencies:  
```bash
pip install discord.py yt-dlp psutil asyncio
```

#### **Download & Install FFmpeg**  
- **Windows**: Download from [FFmpeg.org](https://ffmpeg.org/download.html) and add it to your system path.  
- **Linux (Debian/Ubuntu)**:  
  ```bash
  sudo apt install ffmpeg
  ```
- **Mac**:  
  ```bash
  brew install ffmpeg
  ```

#### **Clone the Repository**  
```bash
git clone https://github.com/your-repo/musicbot.git
cd musicbot
```

#### **Run the Bot**  
1. Open `musicbot.py` and replace `"Post ur token here"` with your **Discord bot token**.  
2. Start the bot:  
   ```bash
   python musicbot.py
   ```
   
### **Bot Token Setup**  
- Go to the [Discord Developer Portal](https://discord.com/developers/applications).  
- Create an application and add a bot.  
- Copy the bot token and paste it in `bot.run("YOUR_BOT_TOKEN")`.  
