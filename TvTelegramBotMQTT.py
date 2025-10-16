#!/usr/bin/env python3
# TvTelegramBot.py
# Multi-plug / multi-user Telegram bot with per-user booking calendar (admin-only)
#
# Requirements:
#   pip install python-telegram-bot aiomqtt python-dotenv psutil requests
#
# Set environment variables (or .env):
#   TELEGRAM_BOT_TOKEN
#   AUTHORIZED_USER_ID  (admin numeric Telegram user id)
#   chatID (optional broadcast chat id)
#
# config.json (optional) may hold:
# {
#   "broker": "192.168.1.27",
#   "port": 1883,
#   "powered_on_min_watts": 30,
#   "interval_minutes": 2,
#   "default_daily_minutes": 100,
#   "plugs": [
#     {"name": "plug1", "topic_prefix": "tasmota_512W10"},
#     {"name": "plug2", "topic_prefix": "tasmota_QBCD19"}
#   ]
# }

import asyncio
import json
import os
import time
from datetime import datetime, date, timedelta, time as dtime
from typing import Dict, Optional
import functools

time.sleep(5)  #waits for Mosquitto to be ready
def with_timeout(seconds: float):
    """Decorator to run an async function with a timeout."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await asyncio.wait_for(func(*args, **kwargs), timeout=seconds)
            except asyncio.TimeoutError:
                print(f"⏱️ {func.__name__} timed out after {seconds}s")
                return None   # or raise, or return a default value
        return wrapper
    return decorator

try:
    import aiomqtt
except Exception:
    raise RuntimeError("aiomqtt is required. Install: pip install aiomqtt")

from dotenv import load_dotenv
load_dotenv()

# Telegram imports
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

import logging
import requests
import signal
import psutil
import platform

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

half_hours= None
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# environment
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN_MQTT")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Please set TELEGRAM_BOT_TOKEN in environment or .env")

CHAT_ID = os.getenv("chatID_MQTT")  # optional broadcast target
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID_MQTT")) if os.getenv("AUTHORIZED_USER_ID_MQTT") else None
user1_id = int(os.getenv("user1_id")) if os.getenv("user1_id") else None
plug1_id = int(os.getenv("plug1_id")) if os.getenv("plug1_id") else None

# files
CONFIG_FILE = os.path.join(BASE_DIR, "configMQTT.json")
USERS_FILE = os.path.join(BASE_DIR, "usersMQTT.json")
CALENDAR_FILE = os.path.join(BASE_DIR, "calendarMQTT.json")

# default config
DEFAULT_CONFIG = {
    "broker": "192.168.1.27",
    "port": 1883,
    "powered_on_min_watts": 30,
    "interval_minutes": 2,
    "plugs": [],
    "default_daily_minutes": 125,
    "add_errors_to_user1": False,
}

commands_help_text = [
    "📋 **Available Commands:**",
    "/start - Register as user",
    "/listplugs - List available plugs",
    "/startplug <plugname> - start using a plug",
    "/stopplug [plugname] - stop using current or given plug",
    "/status - Show system status",
    "/help - Show all commands",
    "/my_bookings - Show your bookings",
    "",
    "**Admin Commands:**",
    "/addminutes <user_id|@username> <minutes>",
    "/setDailyMinutes <user_id|@username> <minutes>",
    "/timerMinutesHoliday <plugname> <minutes> - set minutes of holidays (admin only)",
    "/book <user> - Manage bookings",
    "/my_bookings [user_id|@username] - admin may check others",
    "/calendar - View weekly calendar",
    "/activate <action> <plug> - Enable/disable plugs",
    "/plug <on|off> <plug> - Control plugs"
]

async def sleep_async(seconds) -> None:
    """Async sleep that doesn't block other threads"""
    for i in range(int(seconds)):
        time.sleep(0.7)    # This is blocking, stops threads, it could save energy
        await asyncio.sleep(0.3) # This is non-blocking, doesn't stop threads

def is_yesterday(dt: datetime) -> bool:
    if dt is None:
        return False
    today = datetime.today().date()
    yesterday = today - timedelta(days=1)
    return dt.date() <= yesterday

def load_json_if_exists(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.error("Error loading %s: %s", path, e)
    return default

def save_json(path: str, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        logger.error("Error saving %s: %s", path, e)

# -------------------
# WeeklyCalendar class
# -------------------
class WeeklyCalendar:
    """
    Simple weekly calendar storing per-day 30-minute slots.
    bookings: { "Mon": { "07:30": {"user_id": id, "username": name, "booked_at": iso}, ... }, ... }
    """
    DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    def __init__(self):
        self.bookings = load_json_if_exists(CALENDAR_FILE, {})
        self.user_selections = {}

    def save(self):
        save_json(CALENDAR_FILE, self.bookings)

    def day_name_from_date(self, dt: date) -> str:
        # map datetime.date -> Mon/Tue...
        return dt.strftime("%a")

    def slot_times(self):
        # returns list of HH:MM strings from 00:00 to 23:30 inclusive step 30m
        # Use a simple approach to avoid DST issues
        slots = []
        for hour in range(24):
            for minute in [0, 30]:
                slots.append(f"{hour:02d}:{minute:02d}")
        return slots

    def is_slot_free(self, user_id: int, day: str, slot: str) -> bool:
        return slot not in self.bookings.get(str(user_id), {}).get(day, {})

    def book_slot(self, user_id: int, day: str, slot: str, username: str):
        uid = str(user_id)
        self.bookings.setdefault(uid, {})
        self.bookings[uid].setdefault(day, {})
        self.bookings[uid][day][slot] = {
            "user_id": int(user_id),
            "username": username,
            "booked_at": datetime.utcnow().isoformat()
        }
        self.save()

    def cancel_slot(self, user_id: int, day: str, slot: str):
        uid = str(user_id)
        if uid in self.bookings and day in self.bookings[uid]:
            self.bookings[uid][day].pop(slot, None)
            self.save()

    def get_user_slots(self, user_id: int):
        uid = str(user_id)
        out = []
        for d in WeeklyCalendar.DAYS:
            for slot, info in self.bookings.get(uid, {}).get(d, {}).items():
                out.append((d, slot, info))
        return out

    def get_week_dates(self):
        # return list of date objects for current week (Mon-Sun)
        today = date.today()
        # find Monday
        monday = today - timedelta(days=(today.weekday()))
        return [monday + timedelta(days=i) for i in range(7)]

    def get_user_selection_key(self, user_id: int, day_name: str) -> str:
        """Generate a key for storing user selections"""
        return f"{user_id}_{day_name}"

    def get_user_selected_slots(self, user_id: int, day_name: str) -> list:
        return self.user_selections.get(self.get_user_selection_key(user_id, day_name), [])

    def toggle_user_slot_selection(self, user_id: int, day_name: str, time_slot: str):
        """Toggle a time slot selection for a user"""
        key = self.get_user_selection_key(user_id, day_name)
        if key not in self.user_selections:
            self.user_selections[key] = []
        if time_slot in self.user_selections[key]:
            self.user_selections[key].remove(time_slot)
        else:
            self.user_selections[key].append(time_slot)

    def clear_user_selection(self, user_id: int, day_name: str):
        self.user_selections[self.get_user_selection_key(user_id, day_name)] = []

    def get_week_text(self) -> str:
        lines = []
        for uid, udata in self.bookings.items():
            lines.append(f"👤 User {uid}:")
            for d in WeeklyCalendar.DAYS:
                day_bookings = udata.get(d, {})
                if not day_bookings:
                    continue
                lines.append(f"  *{d}:*")
                for slot, info in sorted(day_bookings.items()):
                    lines.append(f"    • {slot} → @{info.get('username')}")
        if not lines:
            return "(no bookings)"
        return "\n".join(lines)


# -------------------
# User and Plug objects
# -------------------
class User:
    def __init__(self, user_id: int, username: str, default_minutes: int, initial_minutes: int):
        self.user_id = int(user_id)
        self.username = username or ""
        self.default_minutes = int(default_minutes)
        self.initial_minutes = int(initial_minutes)
        self.remaining_minutes = int(initial_minutes)
        self.used_minutes = 0  # <-- Added variable
        self.active_plug: Optional["Plug"] = None
        self.error_minutes: float = 0

    def to_dict(self):
        return {
            "user_id": self.user_id,
            "username": self.username,
            "default_minutes": self.default_minutes,
            "initial_minutes": self.initial_minutes,
            "remaining_minutes": self.remaining_minutes,
            "used_minutes": self.used_minutes,  # <-- Added to dict
            "error_minutes": self.error_minutes
        }

    @classmethod
    def from_dict(cls, d):
        u = cls(d["user_id"], d.get("username", ""),  d.get("default_minutes", 0), d.get("initial_minutes", 0))
        u.default_minutes = int(d.get("default_minutes", u.default_minutes))
        u.remaining_minutes = int(d.get("remaining_minutes", u.initial_minutes))
        u.used_minutes = int(d.get("used_minutes", 0))  # <-- Load from dict
        return u

    def attach_plug(self, plug: "Plug"):
        if self.active_plug is plug:
            return
        if self.active_plug:
            self.active_plug.user = None
        self.active_plug = plug
        plug.user = self

    def detach_plug(self):
        if self.active_plug:
            self.active_plug.user = None
        self.active_plug = None

    def consume_minutes(self, minutes: int):
        before = self.remaining_minutes
        self.remaining_minutes = max(0, int(self.remaining_minutes - minutes))
        self.used_minutes += before - self.remaining_minutes
        logger.debug("User %s consumed %d -> remaining %d", self.username, minutes, self.remaining_minutes)
        return before - self.remaining_minutes

    def reset_daily(self, new_default: Optional[int] = None, new_initial: Optional[int] = None, reset_remaining: bool = True, reset_used: bool = True, reset_error: bool = True):
        if new_default is not None:
            self.default_minutes = int(new_default)
        if new_initial is not None:
            self.initial_minutes = int(new_initial)
        if reset_remaining:
            self.remaining_minutes = int(self.initial_minutes)
        if reset_error:
            self.error_minutes = 0
        if reset_used:
            self.used_minutes = 0  # <-- Reset used_minutes

class Plug:
    def __init__(self, name: str, topic_prefix: str, broker: str, port: int = 1883, active: bool = True):
        self.name = name
        self.topic_prefix = topic_prefix
        self.broker = broker
        self.port = port
        self.topic_cmd = f"cmnd/{topic_prefix}/POWER"
        self.topic_tele = f"cmnd/{topic_prefix}/TelePeriod"
        self.topic_sensor = f"tele/{topic_prefix}/SENSOR"
        self.last_power = 0.0
        self.user: Optional[User] = None
        self._listening_task: Optional[asyncio.Task] = None
        self.last_seen: Optional[datetime] = None
        self.error_minutes: float = 0
        self.in_error: bool = False
        self.active: bool = active
        self.state: bool = None
        self.timerMinutesHoliday: int = 0

    @with_timeout(5.0)
    async def send_command(self, command: str):
        try:
            async with aiomqtt.Client(self.broker, self.port) as client:
                await client.publish(self.topic_cmd, command)
                logger.info("MQTT published %s -> %s", self.topic_cmd, command)
        except Exception as e:
            logger.error("Failed to publish command for %s: %s", self.name, e)

    async def set_teleperiod(self, seconds: int):
        try:
            async with aiomqtt.Client(self.broker, self.port) as client:
                await client.publish(self.topic_tele, str(seconds))
                logger.info("MQTT published teleperiod %s -> %s", self.topic_tele, seconds)
        except Exception as e:
            logger.error("Failed to set TelePeriod for %s: %s", self.name, e)

    async def _sensor_loop(self):
        retry_delay = 10
        max_delay = 300  # 5 minutes
        retry_count = 0
        max_retries = 10  # After this, notify admin and pause longer
        while True:
            try:
                async with aiomqtt.Client(self.broker, self.port) as client:
                    await client.subscribe(self.topic_sensor)
                    logger.info("Plug %s listening to %s", self.name, self.topic_sensor)
                    retry_delay = 10  # Reset delay after successful connection
                    retry_count = 0
                    async for message in client.messages:
                        self.last_seen = datetime.now()   #update last seen on any message
                        self.in_error = False
                        try:
                            # message.topic may be bytes-like or str; check contains
                            payload = message.payload.decode()
                            data = json.loads(payload)
                            power = None
                            if isinstance(data, dict):
                                if "ENERGY" in data and "Power" in data["ENERGY"]:
                                    power = float(data["ENERGY"]["Power"])
                                elif "POWER" in data:
                                    try:
                                        power = float(data["POWER"])
                                    except Exception:
                                        power = None
                            if power is None:
                                power = 0.0
                            self.last_power = float(power)
                            logger.debug("Plug %s power=%s", self.name, self.last_power)
                        except Exception as e:
                            logger.exception("Error parsing MQTT message for plug %s: %s", self.name, e)
            except asyncio.CancelledError:
                logger.info("Sensor loop for %s cancelled", self.name)
                raise
            except Exception as e:
                retry_count += 1
                logger.warning("MQTT connection failed for %s: %s. Retrying in %ds...", self.name, e, retry_delay)
                if retry_count >= max_retries:
                    logger.error("Plug %s: MQTT broker unreachable after %d retries. Pausing for %ds.", self.name, retry_count, max_delay)
                    # Optionally notify admin here
                    if AUTHORIZED_USER_ID:
                        try:
                            await send_to_telegram_simple(TELEGRAM_BOT_TOKEN, AUTHORIZED_USER_ID, f"Plug {self.name}: MQTT broker unreachable after {retry_count} retries.")
                        except Exception:
                            pass
                    await sleep_async(max_delay)
                    retry_count = 0
                    retry_delay = 10
                else:
                    await sleep_async(retry_delay)
                    retry_delay = min(retry_delay * 2, max_delay)

    def start_listening(self):
        # Clean up finished or cancelled tasks
        if self._listening_task and (self._listening_task.done() or self._listening_task.cancelled()):
            self._listening_task = None
        if not self._listening_task:
            self._listening_task = asyncio.create_task(self._sensor_loop())

    def stop_listening(self):
        if self._listening_task and not self._listening_task.done():
            self._listening_task.cancel()
            self._listening_task = None


# -------------------
# SystemManager
# -------------------
class SystemManager:
    def __init__(self, config: dict, calendar: WeeklyCalendar):
        self.config = config
        self.broker = config.get("broker", DEFAULT_CONFIG["broker"])
        self.port = int(config.get("port", DEFAULT_CONFIG["port"]))
        self.power_threshold = config.get("powered_on_min_watts", DEFAULT_CONFIG["powered_on_min_watts"])
        self.interval_minutes = int(config.get("interval_minutes", DEFAULT_CONFIG["interval_minutes"]))
        self.add_errors_to_user1 = config.get("add_errors_to_user1", DEFAULT_CONFIG["add_errors_to_user1"])
        self.plugs: Dict[str, Plug] = {}
        self.users: Dict[int, User] = {}
        self._bg_task: Optional[asyncio.Task] = None
        self.calendar = calendar

        # load plugs
        for p in config.get("plugs", []):
            name = p["name"]
            topic_prefix = p["topic_prefix"]
            active = p.get("active", True)
            self.plugs[name] = Plug(name, topic_prefix, self.broker, self.port, active)

        if not self.plugs:
            # fallback default plug
            self.plugs["plug1"] = Plug("plug1", "tasmota_502E10", self.broker, self.port)
            logger.warning("No plugs in config.json: created default plug 'plug1'")

        # load users
        saved_users = load_json_if_exists(USERS_FILE, {})
        for uid_str, udata in saved_users.items():
            try:
                u = User.from_dict(udata)
                self.users[u.user_id] = u
            except Exception:
                logger.exception("Failed to load user %s", uid_str)

        # create placeholders if no users
        if not self.users:
            default_minutes = int(self.config.get("default_daily_minutes", DEFAULT_CONFIG["default_daily_minutes"]))
            placeholders = [("user1", default_minutes), ("user2", default_minutes), ("user3", default_minutes), ("user4", default_minutes), ("user5", default_minutes)]
            for i, (uname, mins) in enumerate(placeholders, start=1):
                uid = 100000 + i
                self.users[uid] = User(uid, uname, mins, mins)
            self.persist_users()

        # attach user1 to first plug by default if exists
        user1 = None
        for u in self.users.values():
            if u.user_id and u.user_id == user1_id:
                user1 = u
                break
        if user1:
            first_plug = next(iter(self.plugs.values()))
            user1.attach_plug(first_plug)
            logger.info(f"{user1.username} attached to plug {first_plug.name} by default")
            self.persist_users()

    def persist_users(self):
        out = {str(u.user_id): u.to_dict() for u in self.users.values()}
        save_json(USERS_FILE, out)

    def persist_config(self):
        """Save current config including plug active states back to config.json"""
        config_data = dict(self.config)
        # Update plugs with current active states
        plugs_config = []
        for plug in self.plugs.values():
            plug_config = next((p for p in self.config.get("plugs", []) if p["name"] == plug.name), {})
            plug_config["active"] = plug.active
            plugs_config.append(plug_config)
        config_data["plugs"] = plugs_config
        save_json(CONFIG_FILE, config_data)

    def set_plug_active(self, plug_name: str, active: bool) -> bool:
        """Set plug active status and persist to config. Returns True if successful."""
        plug = self.get_plug(plug_name)
        if not plug:
            return False
        plug.active = active
        if not active:
            # Stop listening when deactivated
            plug.stop_listening()
        else:
            # Start listening when activated
            plug.start_listening()
        self.persist_config()
        return True

    def get_user_by_telegram(self, tg_user) -> User:
        if tg_user.id in self.users:
            return self.users[tg_user.id]
        default_minutes = int(self.config.get("default_daily_minutes", DEFAULT_CONFIG["default_daily_minutes"]))
        user = User(tg_user.id, tg_user.username or tg_user.first_name or str(tg_user.id), default_minutes, default_minutes)
        self.users[user.user_id] = user
        self.persist_users()
        logger.info("Created new user %s (%s) with %d minutes", user.username, user.user_id, default_minutes)
        return user

    def get_user_by_id(self, user_id: int) -> Optional[User]:
        return self.users.get(int(user_id))

    def get_plug(self, name: str) -> Optional[Plug]:
        return self.plugs.get(name)

    def list_plugs(self):
        return list(self.plugs.keys())

    async def ensure_plug_listeners(self):
        for plug in self.plugs.values():
            if not plug.active:
                continue
            try:
                plug.start_listening()
            except Exception:
                logger.exception("Failed to start listener for %s", plug.name)

    async def _background_loop(self, bot_application: Application):
        interval = self.interval_minutes
        background_loop_last_run = None
        last_half_hour_stats = None
        last_daily_reset = None
        while True:
            try:
                error_in_any_plug = False
                for plug in self.plugs.values():                    
                    if plug.timerMinutesHoliday > 0:     # holiday mode for the plug
                        if plug.timerMinutesHoliday - interval < 0:
                            plug.timerMinutesHoliday = 0
                        else:
                            plug.timerMinutesHoliday -= interval
                        continue
                    # skip inactive plugs
                    if not plug.active:
                        continue
                    # check error state
                    if (plug.last_seen and (datetime.now() - plug.last_seen).total_seconds() > 80) or (plug.last_seen is None and background_loop_last_run) :
                        if not plug.in_error:
                            plug.in_error = True
                            plug.last_power = None
                            error_in_any_plug = True
                            await broadcast_message(bot_application, f"⚠️ Plug {plug.name} not responding for >80s, entering error state.")
                        error_in_any_plug = True    
                        plug.error_minutes += interval
                    else:
                        plug.in_error = False

                    power = plug.last_power or 0.0
                    user = plug.user
                    if user and power > self.power_threshold:
                        # check booking: user must have an active booking at this time
                        if check_user_time(self.calendar, user, datetime.now()):
                            consumed = user.consume_minutes(interval)
                            if consumed > 0:
                                self.persist_users()
                            # if user exhausted
                            if user.remaining_minutes <= 0 or (user.user_id == user1_id and user.remaining_minutes - user.error_minutes<=0):                                
                                await plug.send_command("OFF")
                                if plug.state or power > 0:
                                    await broadcast_message(bot_application, f"🔌 {plug.name} turned OFF because {user.username} ran out of minutes.")
                                    plug.state = False
                            if user.remaining_minutes == 5 or user.remaining_minutes == 6 or (user.user_id == user1_id and (user.remaining_minutes - user.error_minutes == 5 or user.remaining_minutes - user.error_minutes == 6)):
                                await broadcast_message(bot_application, f"⏳ {user.username}, you have only 5 minutes left!") #approximation to 5 minutes
                        else:
                            # user not booked -> turn off plug and notify admin
                            await plug.send_command("OFF")
                            plug.state = False
                            await broadcast_message(bot_application, f"⛔ {user.username} tried to use {plug.name} outside booking time. Plug OFF.")
                    # else nothing to do

                if error_in_any_plug and self.add_errors_to_user1:
                    self.users[user1_id].error_minutes += interval  # example: increment error minutes for user with id user1_id
               # Check if it's time to send half-hour stats
                current_time = datetime.now()
                if last_half_hour_stats is None or (current_time - last_half_hour_stats).total_seconds() >= 1800:
                    # Send half-hour stats (every half hour)
                    last_half_hour_stats = current_time
                    await send_half_hour_stats(bot_application)

                # Check if it's time for daily reset (every 24 hours)
                if last_daily_reset is not None:
                    if is_yesterday(last_daily_reset):
                        # Send daily reset stats and reset counters (every 24 hours)
                        last_daily_reset = current_time
                        await send_daily_reset_stats(bot_application)
                else:
                    last_daily_reset = current_time
                self.persist_users()
            except Exception:
                logger.exception("Error in background loop")
            finally:
                background_loop_last_run = datetime.now()
            #await sleep_async(interval * 60)
            await sleep_async(interval * 60)

    def start_background(self, app: Application):
        """
        Start background tasks safely when an event loop is available.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("No running event loop yet, delaying background start")
            return

        # Clean up finished or cancelled background task
        if self._bg_task and (self._bg_task.done() or self._bg_task.cancelled()):
            self._bg_task = None

        # Start listeners
        loop.create_task(self.ensure_plug_listeners())

        # Start background monitor only if not running
        if not self._bg_task:
            self._bg_task = loop.create_task(self._background_loop(app))

    async def stop(self):
        for plug in self.plugs.values():
            plug.stop_listening()
        if self._bg_task:
            self._bg_task.cancel()


# -------------------
# Helpers
# -------------------
calendar = WeeklyCalendar()

def is_authorized(user_id: int) -> bool:
    return AUTHORIZED_USER_ID is not None and int(user_id) == int(AUTHORIZED_USER_ID)

def check_user_time(calendar_obj: WeeklyCalendar, user, now: datetime) -> bool:
    day = calendar_obj.day_name_from_date(now.date())
    slots = calendar_obj.bookings.get(str(user.user_id), {}).get(day, {})
    for slot, info in slots.items():
        sh, sm = map(int, slot.split(":"))
        slot_start = datetime.combine(now.date(), dtime(hour=sh, minute=sm))
        slot_end = slot_start + timedelta(minutes=30)
        if slot_start <= now < slot_end:
            return True
    return False


def send_to_telegram_simple(token: str, chat_id: str, text: str):
    apiURL = f'https://api.telegram.org/bot{token}/sendMessage'
    try:
        requests.post(apiURL, json={'chat_id': chat_id, 'text': text})
    except Exception as e:
        logger.error("Failed to send_to_telegram via requests: %s", e)

@with_timeout(5.0)
async def broadcast_message(app: Application, text: str):
    if CHAT_ID:
        try:
            send_to_telegram_simple(TELEGRAM_BOT_TOKEN, CHAT_ID, text)
            return
        except Exception:
            pass
    try:
        for uid in manager.users:
            try:
                await app.bot.send_message(chat_id=uid, text=text)
            except Exception:
                pass
    except Exception:
        logger.exception("Failed to broadcast message with bot")


# -------------------
# Commands: Telegram handlers
# -------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    user = manager.get_user_by_telegram(tg_user)
    await update.message.reply_text(
        f"Hello {tg_user.first_name}! You are registered as '{user.username}'. "
        f"Remaining minutes: ({user.remaining_minutes} - error_minutes:{user.error_minutes}) / {user.initial_minutes}.\n"
        f"Available plugs: {', '.join(manager.list_plugs())}.\n"
        f"Use /startplug <plugname> to start using a plug and /stopplug <plugname> to stop."
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    boot_time = psutil.boot_time()
    uptime_seconds = time.time() - boot_time
    uptime_str = format_uptime(uptime_seconds)   
    tg_user = update.effective_user
    user = manager.get_user_by_telegram(tg_user)
    lines = [
        f"👤 You: @{user.username} ({user.user_id})",
        f"⏳ Remaining minutes: ({user.remaining_minutes} - error_minutes:{user.error_minutes}) / {user.initial_minutes}",
        "",
        "🔌 Plugs:"
    ]
    for plug in manager.plugs.values():
        owner = plug.user.username if plug.user else "—"
        active_status = "ACTIVE" if plug.active else "INACTIVE"
        error_status = "ERROR" if plug.in_error else "OK"
        power_str = f"{plug.last_power:.1f}" if plug.last_power is not None else "N/A"
        lines.append(
            f"  • {plug.name}: power={power_str} W, user={owner}, "
        )
        if plug.timerMinutesHoliday > 0:
            lines.append(
                f"errors={plug.error_minutes} min, state={error_status}, status={active_status}, plug.timerMinutesHoliday={plug.timerMinutesHoliday} min"
            )
        else:
          lines.append(
                f"errors={plug.error_minutes} min, state={error_status}, status={active_status}"
            )                  
    lines.append("")
    for u in manager.users.values():
        message_part_error = f", error = {u.error_minutes:.1f} mins" if u.error_minutes > 0 else ""
        message_part_used = f", used = {u.used_minutes} mins" if u.used_minutes > 0 else ""
        lines.append(
            f"  • @{u.username} ({u.user_id}): remaining={u.remaining_minutes} min{message_part_error}{message_part_used}"
        )
    lines.append("")    
    lines.append(f"Uptime: {uptime_str} Commands: /startplug <plugname>, /stopplug <plugname>, /help, ..., /status")
    await update.message.reply_text("\n".join(lines))


async def startplug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    user = manager.get_user_by_telegram(tg_user)
    if not context.args:
        await update.message.reply_text("Usage: /startplug <plugname>")
        return
    plugname = context.args[0]
    plug = manager.get_plug(plugname)
    if not plug:
        await update.message.reply_text(f"Plug '{plugname}' not found. Known plugs: {', '.join(manager.list_plugs())}")
        return
    for u in manager.users.values():
        if u.active_plug == plug and u.user_id != user.user_id:
            u.detach_plug()
    user.attach_plug(plug)
    manager.persist_users()
    # Turn on the plug physically
    await plug.send_command("ON")
    plug.state = True;
    await update.message.reply_text(f"You're now attached to plug {plugname} and turned it ON. While the plug draws power > {manager.power_threshold}W your minutes will be consumed (only during your booked slots).")

# admin-only: timerMinutesHoliday (only admin may use)
async def timerMinutesHoliday_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("You are not authorized to use this command.")
        return    
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /timerMinutesHoliday <plug> <minutes>")
        return    
    try:
        minutes = int(context.args[1])
    except Exception:
        await update.message.reply_text("Minutes must be integer")
        return    
    tg_user = update.effective_user
    user = manager.get_user_by_telegram(tg_user)
    plugname = context.args[0]
    plug = manager.get_plug(plugname)
    if not plug:
        await update.message.reply_text(f"Plug '{plugname}' not found. Known plugs: {', '.join(manager.list_plugs())}")
        return
    plug.timerMinutesHoliday += minutes
    await update.message.reply_text(f"Set timer for plug {plugname} to {plug.timerMinutesHoliday} minutes.")

async def stopplug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    user = manager.get_user_by_telegram(tg_user)
    if not context.args:
        if user.active_plug:
            pname = user.active_plug.name
            plug = user.active_plug
            user.detach_plug()
            # reassign user1
            if plug.plug_id == plug1_id and user.user_id == user1_id:
                user1=None
                for u in manager.users.values():
                    if u.user_id and u.user_id == user1_id:
                        user1 = u
                        break
                if user1:
                    first_plug = next(iter(manager.plugs.values()))
                    user1.attach_plug(first_plug)
                    logger.info(f"{user1.username} attached to plug {first_plug.name} by default")

            manager.persist_users()
            # Turn off the plug physically
            await plug.send_command("OFF")
            plug.state = False
            await update.message.reply_text(f"Stopped using plug {pname} and turned it OFF.")
        else:
            await update.message.reply_text("You are not attached to any plug.")
        return
    plugname = context.args[0]
    plug = manager.get_plug(plugname)
    if not plug:
        await update.message.reply_text(f"Plug '{plugname}' not found.")
        return
    if plug.user and plug.user.user_id == user.user_id:
        user.detach_plug()

        # reassign user1
        if plug.plug_id == plug1_id and user.user_id != user1_id:
            user1=None
            for u in manager.users.values():
                if u.user_id and u.user_id == user1_id:
                    user1 = u
                    break
            if user1:
                first_plug = next(iter(manager.plugs.values()))
                user1.attach_plug(first_plug)
                logger.info(f"{user1.username} attached to plug {first_plug.name} by default")

        manager.persist_users()
        # Turn off the plug physically
        await plug.send_command("OFF")
        plug.state = False
        await update.message.reply_text(f"Stopped using plug {plugname} and turned it OFF.")
    else:
        await update.message.reply_text(f"You are not the current user of plug {plugname}.")


# admin-only: addminutes (only admin may use)
async def addminutes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("You are not authorized to use this command.")
        return
    if len(context.args) == 0:
        await update.message.reply_text("Usage: /addminutes <user_id|@username> <minutes>")
        return
    if len(context.args) == 1:
        await update.message.reply_text("Usage: /addminutes <user_id|@username> <minutes>")
        return
    target_key = context.args[0]
    try:
        minutes = int(context.args[1])
    except Exception:
        await update.message.reply_text("Minutes must be integer")
        return
    target_user = None
    # try id
    try:
        t_uid = int(target_key)
        target_user = manager.get_user_by_id(t_uid)
    except Exception:
        # username
        for u in manager.users.values():
            if u.username and u.username.lower() == target_key.lstrip("@").lower():
                target_user = u
                break
    if not target_user:
        await update.message.reply_text("Target user not found")
        return
    target_user.remaining_minutes += minutes
    manager.persist_users()
    await update.message.reply_text(f"Added {minutes} minutes to {target_user.username}. Now {target_user.remaining_minutes} min left.")

# admin-only: set daily minutes
async def set_daily_minutes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("You are not authorized to use this command.")
        return
    if len(context.args) == 0:
        await update.message.reply_text("Usage: /setDailyMinutes <user_id|@username> <minutes>")
        return
    if len(context.args) == 1:
        await update.message.reply_text("Usage: /setDailyMinutes <user_id|@username> <minutes>")
        return
    target_key = context.args[0]
    try:
        default_daily_minutes = int(context.args[1])
    except Exception:
        await update.message.reply_text("Minutes must be integer")
        return
    target_user = None
    try:
        t_uid = int(target_key)
        target_user = manager.get_user_by_id(t_uid)
    except Exception:
        for u in manager.users.values():
            if u.username and u.username.lower() == target_key.lstrip("@").lower():
                target_user = u
                break
    if not target_user:
        await update.message.reply_text("Target user not found")
        return
    target_user.reset_daily(default_daily_minutes,None, reset_remaining=False, reset_used=False, reset_error=False)  # only change default_minutes
    manager.persist_users()
    await update.message.reply_text(f"{target_user.username} default daily minutes set to {default_daily_minutes}. Remaining reset.")


#   # admin-only: show calendar
async def show_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("You are not authorized to use this command.")
        return
    text = calendar.get_week_text()
    max_length = 4000  # Leave margin for Markdown formatting

    if len(text) <= max_length:
        await update.message.reply_text(text, parse_mode='Markdown')
    else:
        # Split and send in chunks
        for i in range(0, len(text), max_length):
            chunk = text[i:i+max_length]
            await update.message.reply_text(chunk, parse_mode='Markdown')

async def my_bookings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        if not is_authorized(update.effective_user.id):  # only admin may check others
            await update.message.reply_text("You are not authorized to use this command.")
            return
    target_id = None
    if context.args:
        try:
            target_id = int(context.args[0])
        except Exception:
            # search by username
            key = context.args[0].lstrip("@").lower()
            for u in manager.users.values():
                if u.username and u.username.lower() == key:
                    target_id = u.user_id
                    break
    else:
        target_id = int(update.effective_user.id)

    if not target_id:
        await update.message.reply_text("User not found")
        return

    slots = calendar.get_user_slots(target_id)
    if not slots:
        await update.message.reply_text("No bookings found for this user")
        return

    lines = [f"👤 @{manager.get_user_by_id(target_id).username} ({target_id})"]
    for d, slot, info in slots:
        lines.append(f"  • {d} {slot} (booked {info.get('booked_at')})")
    await update.message.reply_text("\n".join(lines))


# Enhanced booking functions with rate limit protection and multi-slot support
async def show_time_slots(update: Update, context: ContextTypes.DEFAULT_TYPE, day_name: str, target_user_id: int):
    """Show available time slots for a specific day with multi-selection"""
    user_id = update.callback_query.from_user.id
    selected_slots = calendar.get_user_selected_slots(user_id, day_name)

    # Find the corresponding date for display
    week_dates = calendar.get_week_dates()
    date_str = next((d.strftime("%d/%m") for d in week_dates if calendar.day_name_from_date(d) == day_name), "")
    keyboard = []
    slot_times = calendar.slot_times()

    for time_slot in slot_times:
        if time_slot in selected_slots:
            cb = f"toggle|{target_user_id}|{day_name}|{time_slot.replace(':','h')}"
            keyboard.append([InlineKeyboardButton(f"☑️ {time_slot} (Selected)", callback_data=cb)])
        elif not calendar.is_slot_free(target_user_id, day_name, time_slot):
            cb = f"cancel_direct|{target_user_id}|{day_name}|{time_slot.replace(':','h')}"
            keyboard.append([InlineKeyboardButton(f"🗑️ {time_slot} (Cancel)", callback_data=cb)])
        else:
            cb = f"toggle|{target_user_id}|{day_name}|{time_slot.replace(':','h')}"
            keyboard.append([InlineKeyboardButton(f"⬜ {time_slot}", callback_data=cb)])

    actions = []
    if selected_slots:
        actions.append(InlineKeyboardButton(f"📅 Book {len(selected_slots)}", callback_data=f"confirm|{target_user_id}|{day_name}"))
        actions.append(InlineKeyboardButton("🗑️ Clear", callback_data=f"clear|{target_user_id}|{day_name}"))
    if actions:
        keyboard.append(actions)
    keyboard.append([InlineKeyboardButton("« Back", callback_data=f"back_to_days|{target_user_id}")])

    await update.callback_query.edit_message_text(
        f"**{day_name} {date_str}** - Select your slots.\nSelected: {', '.join(selected_slots) if selected_slots else 'none'}",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def show_time_slots_safe(update: Update, context: ContextTypes.DEFAULT_TYPE, day_name: str, target_user_id: int, max_retries: int = 3):
    """Show time slots with rate limit protection"""
    for attempt in range(max_retries):
        try:
            await show_time_slots(update, context, day_name, target_user_id)
            return
        except Exception as e:
            if "429" in str(e) or "Too Many Requests" in str(e) or "RetryAfter" in str(e):
                if attempt < max_retries - 1:
                    # Wait a bit before retrying
                    wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                    await sleep_async(wait_time)
                    continue
                else:
                    # Final attempt failed, just acknowledge
                    logger.warning(f"Rate limited, skipping UI update")
                    return
            else:
                logger.error(f"Error updating time slots: {e}")
                raise

async def handle_multiple_booking(update: Update, context: ContextTypes.DEFAULT_TYPE, day_name: str, target_user_id: int, time_slots: list):
    """Handle booking multiple time slots at once"""
    target_user = manager.get_user_by_id(target_user_id)
    if not target_user:
        await update.callback_query.edit_message_text("User not found")
        return

    # Find the corresponding date for display
    week_dates = calendar.get_week_dates()
    date_str = next((d.strftime("%d/%m") for d in week_dates if calendar.day_name_from_date(d) == day_name), "")
    booked = []
    for ts in time_slots:
        if calendar.is_slot_free(target_user_id, day_name, ts):
            calendar.book_slot(target_user_id, day_name, ts, target_user.username)
            booked.append(ts)
    msg = f"✅ Booked {len(booked)} slot(s) on {day_name} {date_str} for @{target_user.username}: {', '.join(booked)}" if booked else "No slots booked."
    await update.callback_query.edit_message_text(msg)

async def handle_direct_cancellation(update: Update, context: ContextTypes.DEFAULT_TYPE, day_name: str, time_slot: str, target_user_id: int):
    if calendar.is_slot_free(target_user_id, day_name, time_slot):
        await update.callback_query.answer("Slot not booked", show_alert=True)
        return
    calendar.cancel_slot(target_user_id, day_name, time_slot)
    await update.callback_query.answer(f"Cancelled {time_slot}")
    await show_time_slots(update, context, day_name, target_user_id)

# admin-only: booking start. Usage: /book <user_id|@username>
async def book_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("You are not authorized to use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /book <user_id|@username>")
        return
    target = context.args[0]
    target_user = None
    try:
        t_uid = int(target)
        target_user = manager.get_user_by_id(t_uid)
    except Exception:
        key = target.lstrip("@").lower()
        for u in manager.users.values():
            if u.username and u.username.lower() == key:
                target_user = u
                break
    if not target_user:
        await update.message.reply_text("Target user not found")
        return
    # show days (buttons)
    week_dates = calendar.get_week_dates()
    keyboard = []
    for dt in week_dates:
        day_name = calendar.day_name_from_date(dt)
        date_str = dt.strftime("%d/%m")
        cb = f"day|{target_user.user_id}|{day_name}"
        keyboard.append([InlineKeyboardButton(f"{day_name} {date_str}", callback_data=cb)])
    await update.message.reply_text(f"Booking for @{target_user.username}. Select a day:", reply_markup=InlineKeyboardMarkup(keyboard))


# Enhanced button handler with multi-selection and rate limiting
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    from_user = update.effective_user
    if not is_authorized(from_user.id):
        await query.answer("You are not authorized to use this.", show_alert=True)
        return

    data = query.data
    logger.info(f"Received callback data: '{data}'")
    parts = data.split("|")
    if not parts:
        await query.answer("Invalid callback", show_alert=True)
        return

    if parts[0] == "day" and len(parts) == 3:
        await query.answer()
        _, uid_str, day = parts
        try:
            target_id = int(uid_str)
        except Exception as e:
            logger.error(f"Invalid user id: {uid_str}, error: {e}")
            await query.edit_message_text("Invalid user id")
            return
        logger.info(f"Calling show_time_slots_safe for day={day}, target_id={target_id}")
        try:
            await show_time_slots_safe(update, context, day, target_id)
        except Exception as e:
            logger.error(f"Error in show_time_slots_safe: {e}")
            await query.edit_message_text(f"Error showing time slots: {e}")

    elif parts[0] == "toggle" and len(parts) == 4:
        # Handle slot selection/deselection
        _, uid_str, day, time_slot_encoded = parts
        try:
            target_id = int(uid_str)
        except Exception:
            await query.answer("Invalid user id", show_alert=True)
            return

        time_slot = time_slot_encoded.replace('h', ':')
        user_id = query.from_user.id
        calendar.toggle_user_slot_selection(user_id, day, time_slot)

        # Provide immediate feedback via callback answer
        selected_slots = calendar.get_user_selected_slots(user_id, day)
        if time_slot in selected_slots:
            await query.answer(f"✅ Selected {time_slot}")
        else:
            await query.answer(f"❌ Deselected {time_slot}")

        # Debounce UI updates
        if not hasattr(context, 'ui_update_pending'):
            context.ui_update_pending = {}

        # Cancel any pending update for this day
        update_key = f"{target_id}_{day}"
        if update_key in context.ui_update_pending:
            context.ui_update_pending[update_key].cancel()

        # Schedule a new update with delay
        async def delayed_update():
            await sleep_async(0.5)
            try:
                await show_time_slots_safe(update, context, day, target_id)
            except Exception as e:
                logger.error(f"Error in delayed UI update: {e}")
            finally:
                if update_key in context.ui_update_pending:
                    del context.ui_update_pending[update_key]

        context.ui_update_pending[update_key] = asyncio.create_task(delayed_update())

    elif parts[0] == "confirm" and len(parts) == 3:
        await query.answer()
        _, uid_str, day = parts
        try:
            target_id = int(uid_str)
        except Exception:
            await query.answer("Invalid user id", show_alert=True)
            return

        user_id = query.from_user.id
        time_slots = calendar.get_user_selected_slots(user_id, day)

        if time_slots:
            calendar.clear_user_selection(user_id, day)
            await handle_multiple_booking(update, context, day, target_id, time_slots)
        else:
            await query.answer("No slots selected!", show_alert=True)

    elif parts[0] == "clear" and len(parts) == 3:
        await query.answer("Selection cleared!")
        _, uid_str, day = parts
        try:
            target_id = int(uid_str)
        except Exception:
            return

        user_id = query.from_user.id
        calendar.clear_user_selection(user_id, day)
        await show_time_slots_safe(update, context, day, target_id)

    elif parts[0] == "cancel_direct" and len(parts) == 4:
        _, uid_str, day, time_slot_encoded = parts
        try:
            target_id = int(uid_str)
        except Exception:
            await query.answer("Invalid user id", show_alert=True)
            return

        time_slot = time_slot_encoded.replace('h', ':')
        await handle_direct_cancellation(update, context, day, time_slot, target_id)

    elif parts[0] == "back_to_days" and len(parts) == 2:
        await query.answer()
        _, uid_str = parts
        try:
            target_id = int(uid_str)
        except Exception:
            await query.edit_message_text("Invalid user id")
            return

        target_user = manager.get_user_by_id(target_id)
        if not target_user:
            await query.edit_message_text("Target user not found")
            return

        week_dates = calendar.get_week_dates()
        keyboard = []
        for dt in week_dates:
            day_name = calendar.day_name_from_date(dt)
            date_str = dt.strftime("%d/%m")
            callback_data = f"day|{target_id}|{day_name}"
            keyboard.append([InlineKeyboardButton(f"{day_name} {date_str}", callback_data=callback_data)])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"Booking for @{target_user.username}. Select a day:", reply_markup=reply_markup)

    elif data == "not_your_booking":
        await query.answer("This booking belongs to someone else!", show_alert=True)

    # Legacy support for old callback format
    elif parts[0] == "slot" and len(parts) == 4:
        await query.answer()
        _, uid_str, day, slot = parts
        try:
            target_id = int(uid_str)
        except Exception:
            await query.edit_message_text("Invalid user id")
            return
        # toggle booking: if slot free -> book for user; if booked -> cancel
        existing = calendar.bookings.get(day, {}).get(slot)
        if existing:
            # if booked by same user -> cancel; else deny
            if int(existing.get("user_id")) == int(target_id):
                calendar.cancel_slot(target_id,day, slot)
                await query.edit_message_text(f"Cancelled booking {day} {slot} for user {target_id}")
            else:
                await query.edit_message_text(f"Slot {day} {slot} is booked by @{existing.get('username')} (id {existing.get('user_id')}). Cancel first.")
        else:
            # book
            target_user = manager.get_user_by_id(target_id)
            username = target_user.username if target_user else str(target_id)
            calendar.book_slot(target_id, day, slot, username)
            await query.edit_message_text(f"Booked {day} {slot} for @{username}")

    else:
        await query.answer("Unknown action", show_alert=True)


async def listplugs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Available plugs: " + ", ".join(manager.list_plugs()))


# admin-only: raw plug on/off
async def plug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("You are not authorized to use this command.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /plug <on|off> <plugname>")
        return
    action = context.args[0].lower()
    plugname = context.args[1]
    plug = manager.get_plug(plugname)
    if not plug:
        await update.message.reply_text(f"Plug {plugname} not found")
        return
    if action == "on":
        await plug.send_command("ON")
        plug.state = True
        await update.message.reply_text(f"Sent ON to {plugname}")
    elif action == "off":
        await plug.send_command("OFF")
        plug.state = False
        await update.message.reply_text(f"Sent OFF to {plugname}")
    else:
        await update.message.reply_text("Action must be 'on' or 'off'.")


# admin-only: activate/deactivate plug
async def admin_activate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("You are not authorized to use this command.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /activate <activate|deactivate> <plugname>")
        return
    action = context.args[0].lower()
    plugname = context.args[1]

    if action not in ["activate", "deactivate"]:
        await update.message.reply_text("Action must be 'activate' or 'deactivate'.")
        return

    active = action == "activate"
    success = manager.set_plug_active(plugname, active)

    if not success:
        await update.message.reply_text(f"Plug '{plugname}' not found. Available plugs: {', '.join(manager.list_plugs())}")
        return

    status = "activated" if active else "deactivated"
    await update.message.reply_text(f"Plug {plugname} has been {status}.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("\n".join(commands_help_text))

# -------------------
# Startup & main
# -------------------
config = load_json_if_exists(CONFIG_FILE, DEFAULT_CONFIG)
manager = SystemManager(config, calendar)


def format_uptime(seconds):
    """Format uptime in a human-readable way"""
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")

    return " ".join(parts) if parts else "0m"

async def send_daily_reset_stats(app: Application):
    """Send daily reset message with previous day's stats and reset system"""
    try:
        global half_hours
        half_hours = 0
        current_time = datetime.now()
        previous_day = (current_time - timedelta(days=1)).strftime('%d/%m/%Y')

        # Collect stats before reset
        message_lines = [
            f"🌅 **Daily Reset Report**",
            f"📅 Previous day: {previous_day}",
            f"🕐 Reset time: {current_time.strftime('%H:%M - %d/%m/%Y')}",
            "",
            "📊 **Previous Day Summary:**"
        ]

        # Collect plug error stats before reset
        plugs_with_errors = []
        total_error_minutes = 0
        message_part_holiday = ""
        message_part_error = ""
        try:
            for plug in manager.plugs.values():
                if plug.error_minutes > 0:
                    message_part_error = f"{plug.error_minutes:.1f} minutes in error"
                    total_error_minutes += plug.error_minutes
                if plug.timerMinutesHoliday > 0:
                    message_part_holiday = f" {plug.timerMinutesHoliday:.1f} minutes on holiday"
                if plug.error_minutes > 0 or plug.timerMinutesHoliday > 0:
                    plugs_with_errors.append(f"  • {plug.name}: {message_part_error}{message_part_holiday}")
        except Exception as e:
            logger.error(f"Error collecting plug error stats: {e}")
        if plugs_with_errors:
            message_lines.extend([
                "⚠️ **Error Summary:**",
                f"  • Total error minutes: {total_error_minutes:.1f}",
            ])
            message_lines.extend(plugs_with_errors)
        else:
            message_lines.append("✅ **No plug errors yesterday**")

        # Collect user minute consumption before reset
        users_consumed = []
        total_consumed = 0
        for user in manager.users.values():
            consumed = user.used_minutes
            if consumed > 0:
                users_consumed.append(f"  • @{user.username}: consumed {consumed} minutes")
                total_consumed += consumed

        if users_consumed:
            message_lines.extend([
                "",
                "⏱️ **Usage Summary:**",
                f"  • Total minutes consumed: {total_consumed}",
            ])
            message_lines.extend(users_consumed)
        else:
            message_lines.append("📱 **No minutes consumed yesterday**")

        # Reset user minutes
        users_reset = []
        default_minus_err_minutes = 0
        for user in manager.users.values():
            old_remaining = user.remaining_minutes
            if user.remaining_minutes > 0 and user.error_minutes > 0:
                user.error_minutes = max(0, user.error_minutes - user.remaining_minutes)
            if user.default_minutes >0 and user.error_minutes > 0:
                default_minus_err_minutes = max(0, int(user.default_minutes) - int(user.error_minutes))
            else:
                default_minus_err_minutes = user.default_minutes

            user.reset_daily( default_minus_err_minutes, reset_remaining=True, reset_used=True, reset_error=False )
            error_text = f", error mins: {user.error_minutes} → 0" if user.error_minutes > 0 else ""
            users_reset.append(f"  • @{user.username}: {old_remaining} → {user.remaining_minutes} mins{error_text}")

        # Reset plug error minutes
        plugs_reset = []
        for plug in manager.plugs.values():
            if plug.error_minutes > 0:
                plugs_reset.append(f"  • {plug.name}: {plug.error_minutes:.1f} → 0 minutes")
                plug.error_minutes = 0.0

        # Save changes
        manager.persist_users()

        # Add reset confirmation to message
        message_lines.extend([
            "",
            "🔄 **Daily Reset Applied:**"
        ])

        if users_reset:
            message_lines.append("👥 **User Minutes Reset:**")
            message_lines.extend(users_reset)

        if plugs_reset:
            message_lines.append("🔌 **Plug Error Minutes Reset:**")
            message_lines.extend(plugs_reset)
        else:
            message_lines.append("🔌 **No plug errors to reset**")

        message_lines.extend([
            "",
            f"🎯 **Ready for new day!**",
            f"🕐 Next reset: Tomorrow, /status"
        ])

        reset_message = "\n".join(message_lines)
        await broadcast_message(app, reset_message)

        logger.info("Sent daily reset message and reset system counters")

    except Exception as e:
        logger.error(f"Failed to send daily reset message: {e}")

async def send_half_hour_stats(app: Application):
    """Send half-hour stats message with plug and user information"""
    global half_hours
    try:
        if half_hours == None:
            half_hours = 0
        else:
            half_hours += 1        
        boot_time = psutil.boot_time()
        uptime_seconds = time.time() - boot_time
        uptime_str = format_uptime(uptime_seconds)        
        current_time = datetime.now()

        # Create half-hour stats message
        message_lines = [
            f"📊 **Half-Hour n°{half_hours} Stats Report**,  Uptime: {uptime_str}",
            f"🕐 Time: {current_time.strftime('%H:%M - %d/%m/%Y')}",
            "",
            "🔌 **Plug Status:**"
        ]

        # Add plug information
        total_power = 0.0
        active_plugs_count = 0
        error_plugs = []

        for plug in manager.plugs.values():
            if not plug.active:
                continue

            active_plugs_count += 1
            if plug.last_power is not None:
                total_power += plug.last_power

            user_info = f"@{plug.user.username}" if plug.user else "—"
            status_info = "ERROR" if plug.in_error else "OK"
            last_seen_info = ""

            if plug.last_seen:
                minutes_ago = int((current_time - plug.last_seen).total_seconds() / 60)
                if minutes_ago < 60:
                    last_seen_info = f" (seen {minutes_ago}m ago)"
                else:
                    hours_ago = int(minutes_ago / 60)
                    last_seen_info = f" (seen {hours_ago}h ago)"
            else:
                last_seen_info = " (never seen)"
            power_str = f"{plug.last_power:.1f}" if plug.last_power is not None else "N/A"
            message_part_holiday = f", holiday minutes: {plug.timerMinutesHoliday:.1f}" if plug.timerMinutesHoliday > 0 else ""
            message_lines.append(
                f"  • {plug.name}: {power_str} W, {user_info}, {status_info}{last_seen_info} error minutes: {plug.error_minutes:.0f}{message_part_holiday}"
            )

            if plug.in_error:
                error_plugs.append(plug.name)

        if active_plugs_count == 0:
            message_lines.append("  • No active plugs")
        else:
            message_lines.append(f"  • **Total Power:** {total_power:.1f}W")

        # Add user information
        message_lines.extend([
            "",
            "👥 **User Summary:**"
        ])

        users_with_plugs = []

        for user in manager.users.values():
            message_part_error = f", error minutes: {user.error_minutes:.0f}" if user.error_minutes > 0 else ""
            message_part_used = f", used today: {user.used_minutes}" if user.used_minutes > 0 else ""
            message_lines.append(f"@{user.username}: {user.remaining_minutes} min{message_part_error}{message_part_used}")

            if user.active_plug:
                users_with_plugs.append(f"@{user.username} → {user.active_plug.name}")

        if users_with_plugs:
            message_lines.append(f"  • Active connections:")
            for connection in users_with_plugs:
                message_lines.append(f"    - {connection}")

        # Add system information
        try:
            cpu_percent = psutil.cpu_percent(interval=1)
            memory = psutil.virtual_memory()
            message_lines.extend([
                "",
                "💻 **System:**",
                f"  • CPU: {cpu_percent:.1f}%, /help",
                f"  • Memory: {memory.percent:.1f}%, /status"
            ])
        except Exception:
            pass

        # Add alerts if any
        if error_plugs:
            message_lines.extend([
                "",
                "⚠️ **Alerts:**",
                f"  • {len(error_plugs)} plug(s) in error: {', '.join(error_plugs)}"
            ])
        stats_message = "\n".join(message_lines)
        await broadcast_message(app, stats_message)

        logger.info("Sent hourly stats message")

    except Exception as e:
        logger.error(f"Failed to send hourly stats: {e}")

async def send_startup_message(app: Application):
    """Send startup message with system info and commands"""
    try:
        # Get system information
        boot_time = psutil.boot_time()
        uptime_seconds = time.time() - boot_time
        uptime_str = format_uptime(uptime_seconds)

        cpu_count = psutil.cpu_count()
        memory = psutil.virtual_memory()
        memory_total = round(memory.total / (1024**3), 1)  # GB
        memory_used = round(memory.used / (1024**3), 1)   # GB

        # Get configuration info
        active_plugs = [name for name, plug in manager.plugs.items() if plug.active]
        inactive_plugs = [name for name, plug in manager.plugs.items() if not plug.active]

        # Create startup message
        message_lines = [
            "🚀 **Parental Control Bot Started**",
            "",
            "📊 **System Information:**",
            f"• Uptime: {uptime_str} , since {datetime.fromtimestamp(boot_time).strftime('%Y-%m-%d %H:%M:%S')}",
            f"• Platform: {platform.system()} {platform.release()}",
            f"• CPU cores: {cpu_count}",
            f"• Memory: {memory_used}GB / {memory_total}GB ({memory.percent:.1f}%)",
            "",
            "⚙️ **Configuration:**",
            f"• MQTT Broker: {manager.broker}:{manager.port}",
            f"• Power threshold: {manager.power_threshold}W",
            f"• Check interval: {manager.interval_minutes} minutes",
            f"• Default daily minutes: {manager.config.get('default_daily_minutes', 'N/A')}",
            "",
            "🔌 **Plugs:**",
        ]

        if active_plugs:
            message_lines.append(f"• Active: {', '.join(active_plugs)}")
        if inactive_plugs:
            message_lines.append(f"• Inactive: {', '.join(inactive_plugs)}")
        if not active_plugs and not inactive_plugs:
            message_lines.append("• No plugs configured")
        message_lines.extend(commands_help_text)

        startup_message = "\n".join(message_lines)
        await broadcast_message(app, startup_message)

    except Exception as e:
        logger.error(f"Failed to send startup message: {e}")

async def schedule_background_tasks(app: Application):
    # This is awaited by PTB inside an event loop
    await manager.ensure_plug_listeners()
    manager.start_background(app)

    # Send startup message after everything is initialized
    await send_startup_message(app)


def shutdown_tasks():
    try:
        loop = asyncio.get_running_loop()
        for t in asyncio.all_tasks(loop):
            t.cancel()
    except Exception:
        pass

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)
    # Optionally notify the admin
    if AUTHORIZED_USER_ID:
        try:
            await context.bot.send_message(
                chat_id=AUTHORIZED_USER_ID,
                text=f"⚠️ Error: {context.error}"
            )
        except Exception:
            pass

def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # register handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("startplug", startplug_command))
    application.add_handler(CommandHandler("timerMinutesHoliday", timerMinutesHoliday_command))
    application.add_handler(CommandHandler("stopplug", stopplug_command))
    application.add_handler(CommandHandler("addminutes", addminutes_command))
    application.add_handler(CommandHandler("setDailyMinutes", set_daily_minutes_command))
    application.add_handler(CommandHandler("listplugs", listplugs_command))
    application.add_handler(CommandHandler("plug", plug_command))
    application.add_handler(CommandHandler("activate", admin_activate_command))
    application.add_handler(CommandHandler("calendar", show_calendar))
    application.add_handler(CommandHandler("book", book_command))
    application.add_handler(CommandHandler("my_bookings", my_bookings_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_error_handler(global_error_handler)

    application.post_init = schedule_background_tasks

    def _terminate(signum, frame):
        logger.info("Terminating (signal %s)", signum)
        asyncio.create_task(manager.stop())
        shutdown_tasks()
        try:
            application.stop()
        except Exception:
            pass

    signal.signal(signal.SIGINT, _terminate)
    signal.signal(signal.SIGTERM, _terminate)

    logger.info("Starting Telegram bot...")
    application.run_polling()


if __name__ == "__main__":
    main()
