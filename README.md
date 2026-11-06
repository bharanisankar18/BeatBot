<div align="center">

# 🎧 BeatBot

**Play music in your Telegram group's video chat.**

Songs from YouTube · Audio & video files from the chat · Simple commands

</div>

---

## ✨ Features

- 🎵 Play any song by **name or YouTube link**
- 📁 Play **audio / video files** sent in the chat
- 🎬 Stream **video** into the video chat
- ⏯️ Pause, resume, skip, loop, shuffle and volume control
- 🔘 Handy **buttons** on every "Now playing" message

---

## 🚀 Setup

**1. Install**
```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```
You also need **ffmpeg** installed.

**2. Get your keys**

| What | Where |
|---|---|
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `API_ID` & `API_HASH` | [my.telegram.org](https://my.telegram.org) → API development tools |
| `SESSION_STRING` | Run `python generate_session.py` |

**3. Configure**

Copy `.env.example` to `.env` and fill in the values.

**4. Run**
```bash
python bot.py
```

---

## 👥 In your group

1. Add the bot as **admin** (with *Invite users*)
2. Make the assistant account **admin** (with *Manage video chats*)
3. Send `/play song name` 🎶

---

## 🎛️ Commands

| Command | Action |
|---|---|
| `/play` | Play a song or replied file |
| `/vplay` | Play as video |
| `/pause` · `/resume` | Pause / resume |
| `/skip` | Next track |
| `/stop` | Stop and leave |
| `/queue` · `/now` | Show queue / current track |
| `/volume 1-200` | Set volume |
| `/loop` · `/shuffle` | Repeat / shuffle |
| `/mute` · `/unmute` | Mute / unmute |
| `/ping` · `/help` | Status / help |

---

<div align="center">

Made by **Bharani Sankar** with ❤️

</div>
