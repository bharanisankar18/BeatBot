"""BeatBot - plays songs and audio/video files in Telegram group video chats.

How it works:
  * The BOT account (from @BotFather) receives your commands.
  * The ASSISTANT account (a normal Telegram user, logged in via SESSION_STRING)
    joins the group's video chat and streams the audio/video, because Telegram
    does not let bot accounts join voice/video chats themselves.
"""
import asyncio
import html
import logging
import os
import random
from dataclasses import dataclass, field

from pyrogram import Client, filters, idle
from pyrogram.enums import ChatMemberStatus, ParseMode
from pyrogram.errors import (
    ChatAdminRequired,
    InviteRequestSent,
    RPCError,
    UserAlreadyParticipant,
)
from pyrogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from pytgcalls import PyTgCalls
from pytgcalls import filters as call_filters
from pytgcalls.exceptions import NoActiveGroupCall, NotInCallError
from pytgcalls.types import (
    AudioQuality,
    ChatUpdate,
    MediaStream,
    StreamEnded,
    VideoQuality,
)
from yt_dlp import YoutubeDL

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
log = logging.getLogger("BeatBot")

bot = Client(
    "BeatBot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    in_memory=True,
    parse_mode=ParseMode.HTML,
)
assistant = Client(
    "BeatBotAssistant",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    session_string=config.SESSION_STRING,
    in_memory=True,
)
calls = PyTgCalls(assistant)


# --------------------------------------------------------------------------- #
#  State
# --------------------------------------------------------------------------- #
@dataclass
class Track:
    title: str
    path: str
    duration: int  # seconds, 0 = unknown
    requested_by: str
    video: bool
    source: str  # "YouTube" or "Telegram"
    url: str | None = None


@dataclass
class ChatState:
    queue: list[Track] = field(default_factory=list)
    current: Track | None = None
    loop: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


states: dict[int, ChatState] = {}


def get_state(chat_id: int) -> ChatState:
    if chat_id not in states:
        states[chat_id] = ChatState()
    return states[chat_id]


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def fmt_time(seconds: int | None) -> str:
    if not seconds:
        return "?"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def mention(message: Message) -> str:
    if message.from_user:
        return message.from_user.mention
    if message.sender_chat:
        return html.escape(message.sender_chat.title or "Anonymous")
    return "Someone"


def controls() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⏸ Pause", callback_data="ctl:pause"),
                InlineKeyboardButton("▶️ Resume", callback_data="ctl:resume"),
            ],
            [
                InlineKeyboardButton("⏭ Skip", callback_data="ctl:skip"),
                InlineKeyboardButton("⏹ Stop", callback_data="ctl:stop"),
            ],
        ]
    )


def now_playing_text(track: Track) -> str:
    kind = "🎬 Video" if track.video else "🎵 Audio"
    title = html.escape(track.title)
    if track.url:
        title = f'<a href="{html.escape(track.url)}">{title}</a>'
    return (
        f"<b>▶️ Now playing</b> ({kind})\n\n"
        f"<b>{title}</b>\n"
        f"⏱ {fmt_time(track.duration)} · 📀 {track.source}\n"
        f"🙋 Requested by {track.requested_by}"
    )


def remove_file(track: Track, state: ChatState | None = None) -> None:
    """Delete a downloaded file unless another queued track still uses it."""
    if not track.path.startswith(config.DOWNLOAD_DIR):
        return
    if state:
        in_use = [t.path for t in state.queue]
        if state.current:
            in_use.append(state.current.path)
        if track.path in in_use:
            return
    try:
        os.remove(track.path)
    except OSError:
        pass  # file busy (Windows) or already gone - cleaned on next start


async def can_control(message: Message) -> bool:
    """Admins, the group owner, sudo users (and anonymous admins) may control."""
    if not config.ADMIN_ONLY:
        return True
    chat_id = message.chat.id
    if message.sender_chat and message.sender_chat.id == chat_id:
        return True  # anonymous group admin
    if not message.from_user:
        return False
    return await is_admin(chat_id, message.from_user.id)


async def is_admin(chat_id: int, user_id: int) -> bool:
    if user_id in config.SUDO_USERS:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except RPCError:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


# --------------------------------------------------------------------------- #
#  Getting media
# --------------------------------------------------------------------------- #
class TrackError(Exception):
    """Error that should be shown to the user as-is."""


def _youtube_download(query: str, video: bool) -> dict:
    if not query.startswith(("http://", "https://")):
        query = f"ytsearch1:{query}"
    opts = {
        "format": (
            "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
            if video
            else "bestaudio/best"
        ),
        "outtmpl": os.path.join(config.DOWNLOAD_DIR, "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "geo_bypass": True,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:
            entries = list(info["entries"])
            if not entries:
                raise TrackError("No results found.")
            info = entries[0]
        if info.get("is_live"):
            raise TrackError("Live streams are not supported.")
        duration = int(info.get("duration") or 0)
        if duration > config.MAX_DURATION_MIN * 60:
            raise TrackError(
                f"That track is {fmt_time(duration)} long - "
                f"the limit is {config.MAX_DURATION_MIN} minutes."
            )
        info = ydl.extract_info(info["webpage_url"], download=True)
        downloads = info.get("requested_downloads") or []
        path = downloads[0]["filepath"] if downloads else ydl.prepare_filename(info)
        return {
            "title": info.get("title") or "Unknown",
            "path": path,
            "duration": duration,
            "url": info.get("webpage_url"),
        }


async def track_from_youtube(query: str, video: bool, requester: str) -> Track:
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(None, _youtube_download, query, video)
    except TrackError:
        raise
    except Exception as e:
        log.exception("yt-dlp failed for %r", query)
        raise TrackError(f"Could not download that: {html.escape(str(e))[:300]}")
    return Track(
        title=data["title"],
        path=data["path"],
        duration=data["duration"],
        requested_by=requester,
        video=video,
        source="YouTube",
        url=data["url"],
    )


def telegram_media(msg: Message):
    """Return (media_object, is_video, title) for a playable Telegram message."""
    if msg.audio:
        a = msg.audio
        name = " - ".join(x for x in (a.performer, a.title) if x)
        return a, False, name or a.file_name or "Audio file"
    if msg.voice:
        return msg.voice, False, "Voice message"
    if msg.video:
        return msg.video, True, msg.video.file_name or "Video"
    if msg.video_note:
        return msg.video_note, True, "Video message"
    if msg.document and msg.document.mime_type:
        mime = msg.document.mime_type
        if mime.startswith("audio/"):
            return msg.document, False, msg.document.file_name or "Audio file"
        if mime.startswith("video/"):
            return msg.document, True, msg.document.file_name or "Video file"
    return None


async def track_from_telegram(
    msg: Message, want_video: bool, requester: str
) -> Track:
    media, is_video, title = telegram_media(msg)
    duration = int(getattr(media, "duration", 0) or 0)
    if duration > config.MAX_DURATION_MIN * 60:
        raise TrackError(
            f"That file is {fmt_time(duration)} long - "
            f"the limit is {config.MAX_DURATION_MIN} minutes."
        )
    target = os.path.join(config.DOWNLOAD_DIR, f"tg_{media.file_unique_id}")
    path = await bot.download_media(msg, file_name=target + os.sep)
    if not path:
        raise TrackError("Could not download that file.")
    return Track(
        title=title,
        path=path,
        duration=duration,
        requested_by=requester,
        video=want_video and is_video,
        source="Telegram",
    )


# --------------------------------------------------------------------------- #
#  Assistant & streaming
# --------------------------------------------------------------------------- #
async def warm_dialogs() -> None:
    """Load the assistant's chats so it can resolve group IDs."""
    try:
        async for _ in assistant.get_dialogs():
            pass
    except RPCError as e:
        log.warning("Could not load assistant dialogs: %s", e)


async def ensure_assistant(chat_id: int) -> str | None:
    """Make sure the assistant is in the group. Returns an error text or None."""
    try:
        await assistant.get_chat_member(chat_id, "me")
        return None
    except Exception:
        pass  # not a member, or the assistant doesn't know this chat yet

    try:
        link = await bot.export_chat_invite_link(chat_id)
    except ChatAdminRequired:
        me = assistant.me
        name = f"@{me.username}" if me.username else me.first_name
        return (
            "❌ My assistant account is not in this group.\n"
            "Either make me an <b>admin</b> with the <b>Invite users</b> "
            f"permission, or add {html.escape(name)} to the group yourself."
        )
    try:
        await assistant.join_chat(link)
    except UserAlreadyParticipant:
        await warm_dialogs()
    except InviteRequestSent:
        try:
            await bot.approve_chat_join_request(chat_id, assistant.me.id)
        except RPCError:
            return "❌ The assistant's join request needs to be approved by an admin."
    except RPCError as e:
        return f"❌ The assistant could not join this group: {html.escape(str(e))}"
    await asyncio.sleep(1)
    return None


async def stream(chat_id: int, track: Track) -> None:
    await calls.play(
        chat_id,
        MediaStream(
            track.path,
            audio_parameters=AudioQuality.HIGH,
            video_parameters=VideoQuality.HD_720p,
            video_flags=(
                MediaStream.Flags.AUTO_DETECT if track.video else MediaStream.Flags.IGNORE
            ),
        ),
    )


def stream_error_text(e: Exception) -> str:
    if isinstance(e, NoActiveGroupCall):
        return (
            "❌ There is no active video chat, and my assistant isn't allowed to "
            "start one.\nStart a video chat in the group, or make the assistant an "
            "admin with the <b>Manage video chats</b> permission."
        )
    if isinstance(e, FileNotFoundError):
        return "❌ Could not read the media file. Is ffmpeg installed?"
    return f"❌ Could not start playback: {html.escape(str(e))[:300]}"


async def play_next(chat_id: int, skip: bool = False) -> None:
    """Move on to the next track (or repeat it when loop is on)."""
    state = get_state(chat_id)
    async with state.lock:
        while True:
            finished = state.current
            if state.loop and finished and not skip:
                nxt = finished
            else:
                nxt = state.queue.pop(0) if state.queue else None
            state.current = nxt
            if finished and finished is not nxt:
                remove_file(finished, state)

            if nxt is None:
                try:
                    await calls.leave_call(chat_id)
                except (NotInCallError, NoActiveGroupCall):
                    pass
                await bot.send_message(chat_id, "✅ Queue finished. Left the video chat.")
                return
            try:
                await stream(chat_id, nxt)
            except Exception as e:
                log.exception("Failed to play next track in %s", chat_id)
                await bot.send_message(
                    chat_id,
                    f"⚠️ Skipping <b>{html.escape(nxt.title)}</b>\n{stream_error_text(e)}",
                )
                state.loop = False
                skip = True
                continue
            await bot.send_message(
                chat_id,
                now_playing_text(nxt),
                reply_markup=controls(),
                disable_web_page_preview=True,
            )
            return


async def stop_chat(chat_id: int) -> None:
    state = get_state(chat_id)
    async with state.lock:
        tracks = state.queue + ([state.current] if state.current else [])
        state.queue.clear()
        state.current = None
        state.loop = False
        try:
            await calls.leave_call(chat_id)
        except (NotInCallError, NoActiveGroupCall):
            pass
        for t in tracks:
            remove_file(t)


@calls.on_update(call_filters.stream_end(StreamEnded.Type.AUDIO))
async def on_stream_end(_, update: StreamEnded):
    await play_next(update.chat_id)


@calls.on_update(call_filters.chat_update(ChatUpdate.Status.LEFT_CALL))
async def on_left_call(_, update: ChatUpdate):
    """The video chat was closed or the assistant was kicked - reset."""
    state = states.pop(update.chat_id, None)
    if state:
        for t in state.queue + ([state.current] if state.current else []):
            remove_file(t)


# --------------------------------------------------------------------------- #
#  Commands
# --------------------------------------------------------------------------- #
HELP_TEXT = """<b>🎧 BeatBot commands</b>

<b>Playing</b>
/play &lt;song name or link&gt; - play audio from YouTube
/play (reply to an audio/voice/video file) - play that file
/vplay &lt;song name or link&gt; - play as video
/vplay (reply to a video) - play that video

<b>Controls</b> (admins)
/pause - pause
/resume - resume
/skip - skip to the next track
/stop - stop, clear queue and leave
/volume &lt;1-200&gt; - set the volume
/loop - repeat the current track on/off
/shuffle - shuffle the queue
/mute, /unmute - mute or unmute the stream

<b>Info</b>
/queue - show the queue
/now - what's playing now
/ping - check that the bot is alive

<b>Setup:</b> add me to your group as an admin (with <i>Invite users</i>). \
My assistant joins automatically and starts the video chat if needed."""

BOT_COMMANDS = [
    BotCommand("play", "Play a song or reply to an audio file"),
    BotCommand("vplay", "Play a video or reply to a video file"),
    BotCommand("pause", "Pause playback"),
    BotCommand("resume", "Resume playback"),
    BotCommand("skip", "Skip to the next track"),
    BotCommand("stop", "Stop and leave the video chat"),
    BotCommand("queue", "Show the queue"),
    BotCommand("now", "Show the current track"),
    BotCommand("volume", "Set volume (1-200)"),
    BotCommand("loop", "Repeat the current track on/off"),
    BotCommand("shuffle", "Shuffle the queue"),
    BotCommand("mute", "Mute the stream"),
    BotCommand("unmute", "Unmute the stream"),
    BotCommand("ping", "Check if the bot is alive"),
    BotCommand("help", "Show all commands"),
]


@bot.on_message(filters.command(["start", "help"]))
async def cmd_start(_, message: Message):
    if message.chat.type.name == "PRIVATE":
        button = InlineKeyboardMarkup(
            [[InlineKeyboardButton(
                "➕ Add me to a group",
                url=f"https://t.me/{bot.me.username}?startgroup=true",
            )]]
        )
        await message.reply_text(
            "👋 Hi! I'm <b>BeatBot</b>. I play music and audio files in group "
            "video chats.\n\n" + HELP_TEXT,
            reply_markup=button,
        )
    else:
        await message.reply_text(HELP_TEXT)


@bot.on_message(filters.command("ping"))
async def cmd_ping(_, message: Message):
    await message.reply_text("🏓 Pong! BeatBot is online.")


@bot.on_message(filters.command(["play", "vplay"]) & filters.private)
async def cmd_play_private(_, message: Message):
    await message.reply_text("Add me to a group, start a video chat and use /play there.")


@bot.on_message(filters.command(["play", "vplay", "p"]) & filters.group)
async def cmd_play(_, message: Message):
    video = message.command[0].lower() == "vplay"
    chat_id = message.chat.id
    query = " ".join(message.command[1:]).strip()
    reply = message.reply_to_message
    has_media = bool(reply and telegram_media(reply))

    if not has_media and not query:
        cmd = message.command[0]
        await message.reply_text(
            f"Usage:\n<code>/{cmd} song name</code>\n<code>/{cmd} YouTube link</code>\n"
            f"or reply to an audio/video file with /{cmd}"
        )
        return

    status = await message.reply_text("🔎 Getting your track…")
    try:
        if has_media:
            track = await track_from_telegram(reply, video, mention(message))
        else:
            await status.edit_text(f"🔎 Searching <b>{html.escape(query)}</b>…")
            track = await track_from_youtube(query, video, mention(message))
    except TrackError as e:
        await status.edit_text(f"❌ {e}")
        return

    state = get_state(chat_id)
    async with state.lock:
        if state.current is not None:
            state.queue.append(track)
            await status.edit_text(
                f"➕ Added to queue at <b>#{len(state.queue)}</b>\n\n"
                f"<b>{html.escape(track.title)}</b> · ⏱ {fmt_time(track.duration)}"
            )
            return

        error = await ensure_assistant(chat_id)
        if error:
            remove_file(track)
            await status.edit_text(error)
            return
        try:
            await stream(chat_id, track)
        except Exception as e:
            log.exception("Failed to start playback in %s", chat_id)
            remove_file(track)
            await status.edit_text(stream_error_text(e))
            return
        state.current = track
        await status.edit_text(
            now_playing_text(track), reply_markup=controls(), disable_web_page_preview=True
        )


def admin_command(name: str | list[str]):
    """Decorator: group-only command that requires control permission."""
    def wrap(func):
        @bot.on_message(filters.command(name) & filters.group)
        async def handler(client, message: Message):
            if not await can_control(message):
                await message.reply_text("🔒 Only group admins can use this command.")
                return
            await func(client, message)
        return handler
    return wrap


async def do_pause(chat_id: int) -> str:
    if not get_state(chat_id).current:
        return "Nothing is playing."
    try:
        await calls.pause(chat_id)
    except NotInCallError:
        return "Nothing is playing."
    return "⏸ Paused."


async def do_resume(chat_id: int) -> str:
    if not get_state(chat_id).current:
        return "Nothing is playing."
    try:
        await calls.resume(chat_id)
    except NotInCallError:
        return "Nothing is playing."
    return "▶️ Resumed."


async def do_skip(chat_id: int) -> str | None:
    if not get_state(chat_id).current:
        return "Nothing is playing."
    await play_next(chat_id, skip=True)
    return None  # play_next posts its own message


async def do_stop(chat_id: int) -> str:
    await stop_chat(chat_id)
    return "⏹ Stopped, cleared the queue and left the video chat."


@admin_command("pause")
async def cmd_pause(_, message: Message):
    await message.reply_text(await do_pause(message.chat.id))


@admin_command("resume")
async def cmd_resume(_, message: Message):
    await message.reply_text(await do_resume(message.chat.id))


@admin_command(["skip", "next"])
async def cmd_skip(_, message: Message):
    text = await do_skip(message.chat.id)
    if text:
        await message.reply_text(text)


@admin_command(["stop", "end"])
async def cmd_stop(_, message: Message):
    await message.reply_text(await do_stop(message.chat.id))


@admin_command(["volume", "vol"])
async def cmd_volume(_, message: Message):
    chat_id = message.chat.id
    if len(message.command) < 2 or not message.command[1].isdigit():
        await message.reply_text("Usage: <code>/volume 1-200</code>")
        return
    vol = max(1, min(200, int(message.command[1])))
    if not get_state(chat_id).current:
        await message.reply_text("Nothing is playing.")
        return
    try:
        await calls.change_volume_call(chat_id, vol)
    except Exception as e:
        await message.reply_text(
            f"❌ Could not change volume ({html.escape(str(e))}).\n"
            "The assistant needs to be an admin with <b>Manage video chats</b>."
        )
        return
    await message.reply_text(f"🔊 Volume set to <b>{vol}%</b>.")


@admin_command("loop")
async def cmd_loop(_, message: Message):
    state = get_state(message.chat.id)
    state.loop = not state.loop
    await message.reply_text("🔁 Loop is <b>ON</b>." if state.loop else "➡️ Loop is <b>OFF</b>.")


@admin_command("shuffle")
async def cmd_shuffle(_, message: Message):
    state = get_state(message.chat.id)
    if len(state.queue) < 2:
        await message.reply_text("Need at least 2 tracks in the queue to shuffle.")
        return
    random.shuffle(state.queue)
    await message.reply_text("🔀 Queue shuffled.")


@admin_command("mute")
async def cmd_mute(_, message: Message):
    try:
        await calls.mute(message.chat.id)
        await message.reply_text("🔇 Muted.")
    except NotInCallError:
        await message.reply_text("Nothing is playing.")


@admin_command("unmute")
async def cmd_unmute(_, message: Message):
    try:
        await calls.unmute(message.chat.id)
        await message.reply_text("🔊 Unmuted.")
    except NotInCallError:
        await message.reply_text("Nothing is playing.")


@bot.on_message(filters.command(["queue", "q"]) & filters.group)
async def cmd_queue(_, message: Message):
    state = get_state(message.chat.id)
    if not state.current:
        await message.reply_text("📭 The queue is empty. Use /play to add something.")
        return
    lines = [f"<b>▶️ Now:</b> {html.escape(state.current.title)} · {fmt_time(state.current.duration)}"]
    if state.loop:
        lines.append("🔁 Loop is on")
    if state.queue:
        lines.append("\n<b>Up next:</b>")
        for i, t in enumerate(state.queue[:15], 1):
            lines.append(f"{i}. {html.escape(t.title)} · {fmt_time(t.duration)}")
        if len(state.queue) > 15:
            lines.append(f"…and {len(state.queue) - 15} more")
    await message.reply_text("\n".join(lines))


@bot.on_message(filters.command(["now", "np"]) & filters.group)
async def cmd_now(_, message: Message):
    state = get_state(message.chat.id)
    if not state.current:
        await message.reply_text("Nothing is playing.")
        return
    text = now_playing_text(state.current)
    try:
        elapsed = await calls.time(message.chat.id)
        text += f"\n⏳ {fmt_time(elapsed) if elapsed else '0:00'} / {fmt_time(state.current.duration)}"
    except Exception:
        pass
    await message.reply_text(text, reply_markup=controls(), disable_web_page_preview=True)


@bot.on_callback_query(filters.regex(r"^ctl:"))
async def on_control_button(_, query: CallbackQuery):
    chat_id = query.message.chat.id
    if config.ADMIN_ONLY and not await is_admin(chat_id, query.from_user.id):
        await query.answer("Only group admins can use these buttons.", show_alert=True)
        return
    action = query.data.split(":", 1)[1]
    if action == "pause":
        text = await do_pause(chat_id)
    elif action == "resume":
        text = await do_resume(chat_id)
    elif action == "skip":
        text = await do_skip(chat_id) or "⏭ Skipped."
    elif action == "stop":
        text = await do_stop(chat_id)
    else:
        text = "Unknown action."
    await query.answer(text)


# --------------------------------------------------------------------------- #
#  Startup
# --------------------------------------------------------------------------- #
def clean_downloads() -> None:
    for name in os.listdir(config.DOWNLOAD_DIR):
        path = os.path.join(config.DOWNLOAD_DIR, name)
        try:
            if os.path.isdir(path):
                for sub in os.listdir(path):
                    os.remove(os.path.join(path, sub))
                os.rmdir(path)
            else:
                os.remove(path)
        except OSError:
            pass


async def main():
    clean_downloads()
    await bot.start()
    await assistant.start()
    if assistant.me.is_bot:
        log.error(
            "SESSION_STRING belongs to a BOT (%s). The assistant must be a normal "
            "user account. Run generate_session.py again and log in with a PHONE "
            "NUMBER, not the bot token.",
            assistant.me.username,
        )
        await assistant.stop()
        await bot.stop()
        return
    await calls.start()
    asyncio.create_task(warm_dialogs())
    await bot.set_bot_commands(BOT_COMMANDS)
    a = assistant.me
    log.info("BeatBot @%s is online", bot.me.username)
    log.info("Assistant: %s (@%s, id %s)", a.first_name, a.username, a.id)
    await idle()
    await bot.stop()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
