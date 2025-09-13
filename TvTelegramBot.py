# Commands: /addMinutes /status /help /pause /restart /kill /calendar /book /my_bookings /setDailyMinutes
# Command to copy file to raspberry pi: scp "C:\Users\alexl\OneDrive\Documenti\pythonProjects\parental\TvTelegramBot.py" alexl@raspberrypi:tv
# Command to restart raspberry pi: first enter with: "ssh alexl@raspberrypi" then "sudo reboot"
# Commands: add minutes (even negative), remove day programming
# Every 2 min checks if it's a new day and if consuming energy adds to day minutes (total minutes/used minutes)
# Works with ctrl+c to stop
# Made with modifications suggested by chatgpt and claude
# Would like to show (but can't) in chat the bot commands but from program, with async def post_init(application: Application) -> None:
# CryptoBot Channel ID: obtained with instructions in https://help.nethunt.com/en/articles/6467726-how-to-create-a-telegram-bot-and-use-it-to-post-in-telegram-channels#:~:text=NOTE%3A%20Bots%20can't%20message,first%2C%20but%20can%20message%20channels.
# Send message to channel, check bot permissions otherwise it may not be able to send messages apart from replies
# pip install python-telegram-bot --upgrade
#!/usr/bin/env python
# pylint: disable=unused-argument, wrong-import-position
# This program is dedicated to the public domain under the CC0 license.

"""
Simple Bot to reply to Telegram messages and manage TV time with calendar booking system.

First, a few handler functions are defined. Then, those functions are passed to
the Application and registered at their respective places.
Then, the bot is started and runs until we press Ctrl-C on the command line.

Usage:
TV control bot with parental controls and calendar booking system.
Press Ctrl-C on the command line or send a signal to the process to stop the bot.
"""

import logging
import json
import os
import subprocess
import signal  # To completely stop the telegram bot script
import multiprocessing
import asyncio   # To handle asynchronously
import time  # To be able to use time.sleep(n_seconds) in non-async child process for now
from datetime import time as dttime, datetime, timedelta, date
from typing import Dict, List, Optional
from meross_iot.controller.mixins.electricity import ElectricityMixin
from meross_iot.http_api import MerossHttpClient
from meross_iot.manager import MerossManager
from dotenv import load_dotenv
import psutil

# Load environment variables
load_dotenv()

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Import Telegram modules
from telegram import __version__ as TG_VER, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters

try:
    from telegram import __version_info__
except ImportError:
    __version_info__ = (0, 0, 0, 0, 0)  # type: ignore[assignment]

if __version_info__ < (20, 0, 0, "alpha", 1):
    raise RuntimeError(
        f"This example is not compatible with your current PTB version {TG_VER}. To view the "
        f"{TG_VER} version of this example, "
        f"visit https://docs.python-telegram-bot.org/en/examples.html"
    )

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration management
class Config:
    def __init__(self, config_file: str = os.path.join(BASE_DIR,'config.json')):
        self.config_file = config_file
        self.default_config = {
            'daily_minutes': 240-55-10-5-15-15,  # Default value
            'powered_on_min_watts': 30  # Default minimum power consumption to establish that the screen is turned on
        }
        self.config = self.load_config()
    
    def load_config(self) -> Dict:
        """Load configuration from file"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
                    # Ensure all required keys exist
                    for key, default_value in self.default_config.items():
                        if key not in config:
                            config[key] = default_value
                    return config
            else:
                # Create config file with defaults
                self.save_config(self.default_config)
                return self.default_config.copy()
        except Exception as e:
            logger.error(f"Error loading config: {e}")
            return self.default_config.copy()
    
    def save_config(self, config: Dict = None):
        """Save configuration to file"""
        try:
            config_to_save = config if config is not None else self.config
            with open(self.config_file, 'w') as f:
                json.dump(config_to_save, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving config: {e}")
    
    def get(self, key: str):
        """Get configuration value"""
        return self.config.get(key, self.default_config.get(key))
    
    def set(self, key: str, value):
        """Set configuration value and save"""
        self.config[key] = value
        self.save_config()

# Calendar class integration with day-of-week support
class WeeklyCalendar:
    def __init__(self, data_file: str = os.path.join(BASE_DIR,'calendar_data.json')):
        self.data_file = data_file
        # Day of week mapping - define this FIRST
        self.day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        
        # Generate time slots from 7:30 to 23:30 in 30-minute intervals
        self.time_slots = []
        hour = 7
        minute = 0
        while hour < 23 or (hour == 23 and minute <= 30):
            self.time_slots.append(f"{hour:02d}:{minute:02d}")
            minute += 30
            if minute >= 60:
                minute = 0
                hour += 1
        
        # Load bookings after day_names is defined
        self.bookings = self.load_bookings()
        # Store user selections temporarily (not persisted)
        self.user_selections = {}
    
    def get_week_dates(self) -> List[datetime]:
        """Get the current week's dates (Monday to Sunday)"""
        today = datetime.now()
        monday = today - timedelta(days=today.weekday())
        return [monday + timedelta(days=i) for i in range(7)]
    
    def get_day_name_from_date(self, date: datetime) -> str:
        """Get day name from date"""
        return self.day_names[date.weekday()]
    
    def get_user_selection_key(self, user_id: int, day_name: str) -> str:
        """Generate a key for storing user selections"""
        return f"{user_id}_{day_name}"
    
    def get_user_selected_slots(self, user_id: int, day_name: str) -> List[str]:
        """Get currently selected slots for a user and day"""
        key = self.get_user_selection_key(user_id, day_name)
        return self.user_selections.get(key, [])
    
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
        """Clear all selections for a user and day"""
        key = self.get_user_selection_key(user_id, day_name)
        self.user_selections[key] = []
    
    def load_bookings(self) -> Dict:
        """Load bookings from file"""
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, 'r') as f:
                    data = json.load(f)
                    # Convert old date-based format to day-based format if needed
                    if data and list(data.keys())[0].startswith('2'):  # Old format with dates
                        logger.info("Converting old date-based calendar to day-based format...")
                        converted_data = {}
                        for date_key, slots in data.items():
                            try:
                                date_obj = datetime.strptime(date_key, "%Y-%m-%d")
                                day_name = self.get_day_name_from_date(date_obj)
                                if day_name not in converted_data:
                                    converted_data[day_name] = {}
                                # Merge slots for the same day of week
                                for time_slot, booking_info in slots.items():
                                    if time_slot not in converted_data[day_name]:
                                        converted_data[day_name][time_slot] = booking_info
                            except ValueError:
                                logger.warning(f"Skipping invalid date key: {date_key}")
                        
                        # Save converted data
                        self.save_bookings_data(converted_data)
                        return converted_data
                    return data
            return {}
        except Exception as e:
            logger.error(f"Error loading bookings: {e}")
            return {}
    
    def save_bookings(self):
        """Save bookings to file"""
        self.save_bookings_data(self.bookings)
    
    def save_bookings_data(self, data: Dict):
        """Save specific bookings data to file"""
        try:
            with open(self.data_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving bookings: {e}")
    
    def is_slot_booked(self, day_name: str, time_slot: str) -> bool:
        """Check if a time slot is booked for a day of week"""
        return day_name in self.bookings and time_slot in self.bookings[day_name]
    
    def book_slot(self, day_name: str, time_slot: str, user_id: int, username: str = None) -> bool:
        """Book a time slot for a day of week"""
        if self.is_slot_booked(day_name, time_slot):
            return False
        
        if day_name not in self.bookings:
            self.bookings[day_name] = {}
        
        self.bookings[day_name][time_slot] = {
            'user_id': user_id,
            'username': username,
            'booked_at': datetime.now().isoformat()
        }
        
        self.save_bookings()
        return True
    
    def cancel_slot(self, day_name: str, time_slot: str, user_id: int) -> bool:
        """Cancel a booked time slot for a day of week"""
        if not self.is_slot_booked(day_name, time_slot):
            logger.warning(f"Slot {time_slot} on {day_name} is not booked")
            return False
        
        # Check if the user owns this booking
        booking_info = self.bookings[day_name][time_slot]
        if booking_info['user_id'] != user_id:
            logger.warning(f"User {user_id} tried to cancel slot owned by {booking_info['user_id']}")
            return False
        
        del self.bookings[day_name][time_slot]
        
        # Clean up empty day entries
        if not self.bookings[day_name]:
            del self.bookings[day_name]
        
        self.save_bookings()
        logger.info(f"Cancelled slot {time_slot} on {day_name} for user {user_id}")
        return True
    
    def get_week_calendar_text(self) -> str:
        """Generate text representation of the week calendar"""
        week_dates = self.get_week_dates()
        text = "📅 **Weekly Calendar**\n\n"
        
        for date in week_dates:
            day_name = self.get_day_name_from_date(date)
            date_str = date.strftime("%d/%m")
            
            text += f"**{day_name} {date_str}**\n"
            
            booked_slots = self.bookings.get(day_name, {})
            if booked_slots:
                for time_slot in sorted(booked_slots.keys()):
                    booking_info = booked_slots[time_slot]
                    username = booking_info.get('username', 'Unknown')
                    text += f"  ✅ {time_slot} - @{username}\n"
            else:
                text += "  No bookings\n"
            text += "\n"
        
        return text

# Global variables
interval = 2   # Check every two minutes
EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
chatID = os.getenv("chatID")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID")) if os.getenv("AUTHORIZED_USER_ID") else None
commands = "/addMinutes /help /pause /restart /kill /status /calendar /book /my_bookings /setDailyMinutes"
pause = False

# Initialize configuration and calendar
config = Config()
global_initial_daily_minutes = config.get('daily_minutes')
global_powered_on_min_watts = config.get('powered_on_min_watts')
global_daily_minutes = global_initial_daily_minutes

global_daily_used_minutes = 0
global_error_minutes = 0
global_last_error_time = None
global_current_date = date.today()
global_queue_parent_send = multiprocessing.Queue()
global_queue_parent_receive = multiprocessing.Queue()
global_force_consumption = 0  # If set to 35 acts as if there's energy consumption
new_error_time = None
http_api_client = None
manager = None
devs = None
dev = None
continue_running = True
instant_consumption = None
half_hours = None

# Initialize calendar
calendar = WeeklyCalendar()

async def sleep_async(seconds) -> None:
    """Async sleep that doesn't block other threads"""
    for i in range(int(seconds)):
        time.sleep(0.9)    # This is blocking, stops threads, it could save energy
        await asyncio.sleep(0.1) # This is non-blocking, doesn't stop threads

def check_time(time_obj):
    """Check if current day and time match a booked slot using day of week"""
    try:
        current_day_name = calendar.get_day_name_from_date(time_obj)
        current_time_str = time_obj.strftime("%H:%M")
        
        # Check if there are any bookings for current day of week
        if current_day_name not in calendar.bookings:
            return False
        
        # Check if current time matches any booked slot
        # We need to check if current time falls within any booked slot
        # Since slots are 30 minutes long, we check if current time is within the slot
        for booked_time_slot in calendar.bookings[current_day_name].keys():
            # Parse the booked time slot (e.g., "14:30")
            slot_hour, slot_minute = map(int, booked_time_slot.split(':'))
            slot_start = dttime(slot_hour, slot_minute)
            
            # Calculate slot end time (30 minutes later)
            if slot_hour == 23:
                slot_end_minute = slot_minute + 29  #otherwise it goes to next day and becomes smaller than current time
            else:
                slot_end_minute = slot_minute + 30
            slot_end_hour = slot_hour
            if slot_end_minute >= 60:
                slot_end_minute -= 60
                slot_end_hour += 1
            slot_end = dttime(slot_end_hour, slot_end_minute)
            
            current_time = time_obj.time()
            
            # Check if current time falls within this booked slot
            if slot_start <= current_time < slot_end:
                return True
        
        return False
    except Exception as e:
        logger.error(f"Error in check_time: {e}")
        return False

async def half_hour_report() -> None:
    """Send status report every 30 minutes"""
    while True:
        global half_hours
        if half_hours == None:
            half_hours = 0
        else:
            half_hours += 1
        global global_daily_minutes
        global global_daily_used_minutes
        global instant_consumption
        global global_error_minutes
        send_to_telegram("half hours: " + str(half_hours) +", time: " +" "+str(datetime.now())+ " latest instant consumption: " + str(instant_consumption) +  " remaining minutes: " + str(global_daily_minutes-global_daily_used_minutes)+" out of "+str(global_daily_minutes)+" minutes, error minutes " + str(global_error_minutes) )
        await asyncio.sleep(60*30)

async def background_task(queue_parent) -> None:
    """Main background task for energy monitoring and TV control"""
    global continue_running 
    global instant_consumption
    global interval
    continue_manual = True
    global http_api_client
    global manager
    global devs
    global dev
    global global_current_date
    global global_initial_daily_minutes
    global global_daily_minutes
    global global_daily_used_minutes
    global global_force_consumption
    global global_error_minutes
    global global_last_error_time
    global half_hours
    global new_error_time
    global config

    new_error_time = None
    while continue_running: # Connection loop to meross
        new_error_time = None
        if not pause:
            err = False
            try:
                # Setup the HTTP client API from user-password
                http_api_client = await MerossHttpClient.async_from_user_password(email=EMAIL, password=PASSWORD, api_base_url="https://iot.meross.com")

                # Setup and start the device manager
                manager = MerossManager(http_client=http_api_client)
                await manager.async_init()

                # Retrieve all the devices that implement the electricity mixin
                await manager.async_device_discovery()
                devs = manager.find_devices(device_class=ElectricityMixin)
                if len(devs) < 1:
                    err = True
                    send_to_telegram("Warning, error connecting to device, time: " + str(datetime.now()) + " latest instant consumption: " + str(instant_consumption) +  " remaining minutes: " + str(global_daily_minutes-global_daily_used_minutes)+" out of "+str(global_daily_minutes)+" minutes, error minutes " + str(global_error_minutes) )
                    new_error_time = time.time()
                    await sleep_async(120) # Wait 120 seconds before retrying
                else:
                    dev = devs[0]
            except:
                err = True
                send_to_telegram ("Warning, error connecting to device, time: " +" "+str(datetime.now())+ " latest instant consumption: " + str(instant_consumption) +  " remaining minutes: " + str(global_daily_minutes-global_daily_used_minutes)+" out of "+str(global_daily_minutes)+" minutes, error minutes " + str(global_error_minutes) )
                new_error_time = time.time()
            if not err:
                new_error_time = None
                break
            else:
                if new_error_time != None and global_last_error_time != None:
                    if new_error_time - global_last_error_time >= 1:
                        global_error_minutes = global_error_minutes + (new_error_time - global_last_error_time)/60
                        global_last_error_time = new_error_time
                else:
                    if new_error_time != None and global_last_error_time == None:
                        global_last_error_time = new_error_time
                        new_error_time = None                        
                    else:
                        if new_error_time == None:
                            global_last_error_time = None
            await sleep_async(120) # Wait 120 seconds before retrying
        else:
            global_last_error_time = None
            new_error_time = None
            await sleep_async(5) # Wait 5 seconds before retrying

    while continue_running:
        try:
            while continue_manual:  # Power reading loop
                new_error_time = None
                if not pause:
                    err = False
                    try:
                        # Read the electricity power/voltage/current
                        instant_consumption = None
                        instant_consumption = await dev.async_get_instant_metrics()

                    except:
                        err = True
                        try:
                            await dev.async_update()
                        except:
                            pass
                    if not err:
                        break
                    print("Error reading plug data, time: " + str(datetime.now()))
                    print (" time: " +" "+str(datetime.now())+ " latest instant consumption: " + str(instant_consumption) +  " remaining minutes: " + str(global_daily_minutes-global_daily_used_minutes)+" out of "+str(global_daily_minutes)+" minutes, error minutes " + str(global_error_minutes) )
                    new_error_time = time.time()
                    # Error tracking
                    if new_error_time != None and global_last_error_time != None:
                        if new_error_time - global_last_error_time >= 1:
                            global_error_minutes = global_error_minutes + (new_error_time - global_last_error_time)/60
                            global_last_error_time = new_error_time
                    else:
                        if new_error_time != None and global_last_error_time == None:
                            global_last_error_time = new_error_time
                            new_error_time = None                        
                        else:
                            if new_error_time == None:
                                global_last_error_time = None
                    await sleep_async(120)  # wait for 120 seconds
                else:
                    await sleep_async(5)  # wait for 5 seconds
            print(f"Current consumption data: {instant_consumption}")                
            # Connected and got the value

            # Reset counter if day changed
            if global_current_date != date.today():
                send_to_telegram("Daily summary for " + str(global_current_date) +" used minutes: " + str(global_daily_used_minutes) +" out of " + str(global_daily_minutes))
                send_to_telegram("Error minutes: " + str(global_error_minutes))
                global_daily_used_minutes = -interval # Account for the first while loop adding used minutes immediately
                global_current_date = date.today()
                # Reload daily minutes from config in case it was changed
                global_initial_daily_minutes = config.get('daily_minutes')
                global_daily_minutes = global_initial_daily_minutes
                global_error_minutes = 0
                global_last_error_time = None
                new_error_time = None
                half_hours = None
                if check_time(datetime.now()):
                    try:
                        await dev.async_turn_on(channel=0)
                    except:
                        try:
                            await dev.async_update()
                            await dev.async_turn_on(channel=0)
                        except:
                            send_to_telegram("Warning, cannot turn on plug, time: " + str(datetime.now())+ " latest instant consumption: " + str(instant_consumption) +  " remaining minutes: " + str(global_daily_minutes-global_daily_used_minutes)+" out of "+str(global_daily_minutes)+" minutes, error minutes " + str(global_error_minutes) )                

            if instant_consumption.power + global_force_consumption > global_powered_on_min_watts:
                global_daily_used_minutes += interval
                if global_daily_used_minutes + 6 >= global_daily_minutes and global_daily_used_minutes <= global_daily_minutes:
                    send_to_telegram("TV SHUTDOWN in "+ str(global_daily_minutes - global_daily_used_minutes)+ " minutes. time: " + str(datetime.now())+ " latest instant consumption: " + str(instant_consumption) +  " remaining minutes: " + str(global_daily_minutes-global_daily_used_minutes)+" out of "+str(global_daily_minutes)+" minutes, error minutes " + str(global_error_minutes) )
                if not check_time(datetime.now()+timedelta(minutes=6)):
                    send_to_telegram("TV SHUTDOWN in a short time due to scheduling")
            if global_daily_used_minutes >= global_daily_minutes and instant_consumption.power > 0.2 or not check_time(datetime.now()):
                print(f"Turning off {dev.name}...")
                try:
                    await dev.async_turn_off(channel=0)
                    if instant_consumption.power + global_force_consumption > global_powered_on_min_watts:
                        send_to_telegram("WARNING: manually turn off the TV")
                except:
                    try:
                        await dev.async_update()
                        await dev.async_turn_off(channel=0)
                        if instant_consumption.power + global_force_consumption > global_powered_on_min_watts:
                            send_to_telegram("WARNING: manually turn off the TV")                        
                    except:
                        send_to_telegram("Warning, cannot turn off plug, time: " + str(datetime.now())+ " latest instant consumption: " + str(instant_consumption) +  " remaining minutes: " + str(global_daily_minutes-global_daily_used_minutes)+" out of "+str(global_daily_minutes)+" minutes, error minutes " + str(global_error_minutes) )                       
            else:
                if global_daily_used_minutes < global_daily_minutes:
                    print(f"Turning on {dev.name}...")
                    if check_time(datetime.now()):
                        try:
                            await dev.async_turn_on(channel=0)
                        except:
                            try:
                                await dev.async_update()
                                await dev.async_turn_on(channel=0)
                            except:
                                send_to_telegram("Warning, cannot turn on plug, time: " + str(datetime.now())+ " latest instant consumption: " + str(instant_consumption) +  " remaining minutes: " + str(global_daily_minutes-global_daily_used_minutes)+" out of "+str(global_daily_minutes)+" minutes, error minutes " + str(global_error_minutes) )                
            
            print (" time: " +" "+str(datetime.now())+ " latest instant consumption: " + str(instant_consumption) +  " remaining minutes: " + str(global_daily_minutes-global_daily_used_minutes)+" out of "+str(global_daily_minutes)+" minutes, error minutes " + str(global_error_minutes) )
            await sleep_async(interval*60)  # wait 

        except asyncio.CancelledError:
            # Close the manager and logout from http_api
            manager.close()
            await http_api_client.async_logout()
            break

def schedule_background_task() -> None:
    """Schedule the background tasks"""
    loop = asyncio.get_event_loop()
    loop.create_task(background_task(global_queue_parent_receive))
    loop.create_task(half_hour_report())

# Function to send message to telegram channel
import requests

def send_to_telegram(message):
    """Send message to telegram channel"""
    response = ''
    global TELEGRAM_BOT_TOKEN
    global chatID
    apiURL = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'

    try:
        response = requests.post(apiURL, json={'chat_id': chatID, 'text': message})
        print("send_to_telegram:" + message)
    except Exception as e:
        print(e)

# Command handlers
async def add_minutes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add minutes to daily allowance"""
    global global_daily_minutes    
    global global_daily_used_minutes
    global instant_consumption
    global global_error_minutes
    global half_hours
    global AUTHORIZED_USER_ID
    user = update.effective_user
    if AUTHORIZED_USER_ID and user.id != AUTHORIZED_USER_ID:   
        await update.message.reply_text(user.first_name + ", you are not authorized to give me this command")
    else:
        sum_minutes = 0
        for i in context.args:
            sum_minutes += int(i)
        global_daily_minutes += sum_minutes
        await update.message.reply_html(
            rf"Hello {user.mention_html()}! Adding "+str(sum_minutes)+" minutes"
        )   
    await update.message.reply_text("Remaining minutes: " + str(global_daily_minutes-global_daily_used_minutes)+" out of "+str(global_daily_minutes)+" minutes, error minutes " + str(global_error_minutes) + ", half hours: " + str(half_hours) + ", time: " + str(datetime.now()))
    await update.message.reply_text("Commands: " + commands + ", plug: " + str(instant_consumption))

async def set_daily_minutes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the daily minutes allowance"""
    global global_initial_daily_minutes
    global global_daily_minutes
    global global_daily_used_minutes
    global instant_consumption
    global global_error_minutes
    global half_hours
    global AUTHORIZED_USER_ID
    global config
    
    user = update.effective_user
    if AUTHORIZED_USER_ID and user.id != AUTHORIZED_USER_ID:   
        await update.message.reply_text(user.first_name + ", you are not authorized to give me this command")
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /setDailyMinutes <minutes>\nExample: /setDailyMinutes 180")
        return
    
    try:
        new_daily_minutes = int(context.args[0])
        if new_daily_minutes < 0:
            await update.message.reply_text("Daily minutes cannot be negative")
            return
        
        # Update configuration
        config.set('daily_minutes', new_daily_minutes)
        
        # Update global variables
        global_initial_daily_minutes = new_daily_minutes
        global_daily_minutes = new_daily_minutes
        
        await update.message.reply_html(
            rf"Hello {user.mention_html()}! Daily minutes set to {new_daily_minutes} minutes"
        )
        await update.message.reply_text(f"Configuration saved to config.json")
        
    except ValueError:
        await update.message.reply_text("Please provide a valid number of minutes")
        return
    
    await update.message.reply_text("Remaining minutes: " + str(global_daily_minutes-global_daily_used_minutes)+" out of "+str(global_daily_minutes)+" minutes, error minutes " + str(global_error_minutes) + ", half hours: " + str(half_hours) + ", time: " + str(datetime.now()))
    await update.message.reply_text("Commands: " + commands + ", plug: " + str(instant_consumption))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send help message"""
    global commands
    await update.message.reply_text("Help! Commands: " + commands)

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Echo the user message"""
    await update.message.reply_text(update.message.text)

async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pause the monitoring system"""
    global global_daily_minutes
    global global_daily_used_minutes
    global global_error_minutes
    global instant_consumption
    global AUTHORIZED_USER_ID
    user = update.effective_user
    if AUTHORIZED_USER_ID and user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text(user.first_name + ", you are not authorized to give me this command")
    else:
        global pause
        pause = True 
        send_to_telegram("'Pause' started, time: " + str(datetime.now())+ " latest instant consumption: " + str(instant_consumption) +  " remaining minutes: " + str(global_daily_minutes-global_daily_used_minutes)+" out of "+str(global_daily_minutes)+" minutes, error minutes " + str(global_error_minutes) )  

async def kill_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kill the bot"""
    global global_daily_minutes
    global global_daily_used_minutes
    global global_error_minutes
    global instant_consumption    
    global manager
    global http_api_client
    global continue_running
    global AUTHORIZED_USER_ID
    user = update.effective_user    
    if AUTHORIZED_USER_ID and user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text(user.first_name + ", you are not authorized to give me this command")
    else:
        send_to_telegram("Closing script in progress, time: " + str(datetime.now())+ " latest instant consumption: " + str(instant_consumption) +  " remaining minutes: " + str(global_daily_minutes-global_daily_used_minutes)+" out of "+str(global_daily_minutes)+" minutes, error minutes " + str(global_error_minutes) )  
        continue_running = False
        manager.close
        time.sleep(1)
        await asyncio.sleep(1)
        await http_api_client.async_logout()
        await asyncio.sleep(1)
        loop = asyncio.get_event_loop()
        for task in asyncio.all_tasks(loop=loop):
            task.cancel()
        loop.run_until_complete(asyncio.gather(*asyncio.all_tasks(loop=loop), return_exceptions=True))
        loop.close() 
        os.kill(os.getpid(), signal.SIGINT)     

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current status"""
    global global_daily_minutes
    global global_daily_used_minutes
    global instant_consumption
    global global_error_minutes
    global half_hours
    boot_time = datetime.fromtimestamp(psutil.boot_time())
    #print(f"Last boot: {boot_time}")
    await update.message.reply_text("time: " + str(datetime.now()) + " last boot: " + str(boot_time) + ", Remaining minutes: " + str(global_daily_minutes-global_daily_used_minutes)+" out of "+str(global_daily_minutes)+" minutes, error minutes " + str(global_error_minutes) + " half hours: " + str(half_hours) )
    await update.message.reply_text("Commands: " + commands + ", plug: " + str(instant_consumption))

async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Restart monitoring after pause"""
    global pause
    global global_daily_minutes
    global global_daily_used_minutes
    global global_error_minutes
    global instant_consumption    
    global AUTHORIZED_USER_ID
    user = update.effective_user
    if AUTHORIZED_USER_ID and user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text(user.first_name + ", you are not authorized to give me this command")
    else:
        pause = False 
        send_to_telegram("'Restart' initiated, time: " + str(datetime.now())+ " latest instant consumption: " + str(instant_consumption) +  " remaining minutes: " + str(global_daily_minutes-global_daily_used_minutes)+" out of "+str(global_daily_minutes)+" minutes, error minutes " + str(global_error_minutes) )  

# Calendar command handlers
async def show_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the current week calendar"""
    calendar_text = calendar.get_week_calendar_text()
    await update.message.reply_text(calendar_text, parse_mode='Markdown')

async def book_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the booking process"""
    week_dates = calendar.get_week_dates()
    
    # Create keyboard with days of the week
    keyboard = []
    for date in week_dates:
        day_name = calendar.get_day_name_from_date(date)
        date_str = date.strftime("%d/%m")
        callback_data = f"day_{day_name}"
        keyboard.append([InlineKeyboardButton(f"{day_name} {date_str}", callback_data=callback_data)])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Select a day:", reply_markup=reply_markup)

async def my_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's current bookings"""
    user_id = update.message.from_user.id
    
    user_bookings = []
    for day_name in calendar.day_names:
        if day_name in calendar.bookings:
            for time_slot, booking_info in calendar.bookings[day_name].items():
                if booking_info['user_id'] == user_id:
                    user_bookings.append((day_name, time_slot))
    
    if not user_bookings:
        await update.message.reply_text("You have no current bookings.")
        return
    
    # Sort bookings by day of week and time
    day_order = {day: i for i, day in enumerate(calendar.day_names)}
    user_bookings.sort(key=lambda x: (day_order[x[0]], x[1]))
    
    # Create the message text
    text = "📋 *Your Bookings:*\n\n"
    
    # Group bookings by day
    current_day = None
    booking_lines = []
    
    for day_name, time_slot in user_bookings:
        if day_name != current_day:
            if current_day is not None:
                booking_lines.append("")  # Add empty line between days
            booking_lines.append(f"*{day_name}*")
            current_day = day_name
        
        booking_lines.append(f"  • {time_slot}")
    
    # Check if message will be too long and split if necessary
    full_text = text + "\n".join(booking_lines)
    
    if len(full_text) <= 4000:  # Leave some margin below 4096 limit
        await update.message.reply_text(full_text, parse_mode='Markdown')
    else:
        # Split into multiple messages
        messages = []
        current_message = text
        
        for line in booking_lines:
            test_message = current_message + line + "\n"
            if len(test_message) > 3500:  # Conservative limit to account for formatting
                messages.append(current_message)
                current_message = line + "\n"
            else:
                current_message = test_message
        
        if current_message.strip():
            messages.append(current_message)
        
        # Send multiple messages
        for i, message in enumerate(messages):
            if i == 0:
                await update.message.reply_text(message, parse_mode='Markdown')
            else:
                await update.message.reply_text(f"📋 *Your Bookings (continued):*\n\n{message}", parse_mode='Markdown')

async def show_time_slots(update: Update, context: ContextTypes.DEFAULT_TYPE, day_name: str):
    """Show available time slots for a specific day with multi-selection"""
    user_id = update.callback_query.from_user.id
    selected_slots = calendar.get_user_selected_slots(user_id, day_name)
    
    # Find the corresponding date for display
    week_dates = calendar.get_week_dates()
    date_str = ""
    for date in week_dates:
        if calendar.get_day_name_from_date(date) == day_name:
            date_str = date.strftime("%d/%m")
            break
    
    keyboard = []
    available_count = 0
    
    for time_slot in calendar.time_slots:
        if calendar.is_slot_booked(day_name, time_slot):
            # Show booked slots - clickable for cancellation if it's user's booking
            booking_info = calendar.bookings.get(day_name, {}).get(time_slot, {})
            
            logger.info(f"Slot {time_slot} is booked. Booking info: {booking_info}")
            logger.info(f"Current user_id: {user_id}, Booking user_id: {booking_info.get('user_id')}")
            
            if booking_info.get('user_id') == user_id:
                # User's own booking - allow cancellation
                callback_data = f"cancel_direct_{day_name}_{time_slot.replace(':', 'h')}"
                username = booking_info.get('username', 'You')
                keyboard.append([InlineKeyboardButton(f"🗑️ {time_slot} - @{username} (Tap to cancel)", callback_data=callback_data)])
                logger.info(f"Created cancel button with callback_data: '{callback_data}'")
            else:
                # Someone else's booking - show but not clickable
                username = booking_info.get('username', 'Unknown')
                keyboard.append([InlineKeyboardButton(f"❌ {time_slot} - @{username}", callback_data="not_your_booking")])
        else:
            available_count += 1
            if time_slot in selected_slots:
                # Selected slot - show with checkmark
                callback_data = f"toggle_{day_name}_{time_slot.replace(':', 'h')}"
                keyboard.append([InlineKeyboardButton(f"☑️ {time_slot} (Selected)", callback_data=callback_data)])
            else:
                # Available slot - show as selectable
                callback_data = f"toggle_{day_name}_{time_slot.replace(':', 'h')}"
                keyboard.append([InlineKeyboardButton(f"⬜ {time_slot}", callback_data=callback_data)])
    
    # Add action buttons
    action_buttons = []
    if selected_slots:
        confirm_data = f"confirm_{day_name}"
        action_buttons.append(InlineKeyboardButton(f"📅 Book {len(selected_slots)} slot(s)", callback_data=confirm_data))
        action_buttons.append(InlineKeyboardButton("🗑️ Clear Selection", callback_data=f"clear_{day_name}"))
    
    if action_buttons:
        keyboard.append(action_buttons)
    
    keyboard.append([InlineKeyboardButton("« Back to Days", callback_data="back_to_days")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    selection_text = ""
    if selected_slots:
        selection_text = f"\n\n**Selected:** {', '.join(sorted(selected_slots))}"
    
    await update.callback_query.edit_message_text(
        f"**{day_name} {date_str}** - Select time slots to book:{selection_text}\n\n"
        f"Available slots: {available_count - len(selected_slots)}/{available_count}",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def show_time_slots_safe(update: Update, context: ContextTypes.DEFAULT_TYPE, day_name: str, max_retries: int = 3):
    """Show time slots with rate limit protection"""
    for attempt in range(max_retries):
        try:
            await show_time_slots(update, context, day_name)
            return
        except Exception as e:
            if "429" in str(e) or "Too Many Requests" in str(e) or "RetryAfter" in str(e):
                if attempt < max_retries - 1:
                    # Wait a bit before retrying
                    wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    # Final attempt failed, just acknowledge
                    logger.warning(f"Rate limited, skipping UI update")
                    return
            else:
                logger.error(f"Error updating time slots: {e}")
                raise

async def handle_multiple_booking(update: Update, context: ContextTypes.DEFAULT_TYPE, day_name: str, time_slots: List[str]):
    """Handle booking multiple time slots at once"""
    user = update.callback_query.from_user
    
    # Find the corresponding date for display
    week_dates = calendar.get_week_dates()
    date_str = ""
    for date in week_dates:
        if calendar.get_day_name_from_date(date) == day_name:
            date_str = date.strftime("%d/%m")
            break
    
    successful_bookings = []
    failed_bookings = []
    
    for time_slot in time_slots:
        success = calendar.book_slot(day_name, time_slot, user.id, user.username)
        if success:
            successful_bookings.append(time_slot)
        else:
            failed_bookings.append(time_slot)
    
    # Create result message
    message_parts = []
    
    if successful_bookings:
        slots_text = ', '.join(sorted(successful_bookings))
        if len(successful_bookings) == 1:
            message_parts.append(f"✅ Successfully booked {slots_text} on {day_name} {date_str}!")
        else:
            message_parts.append(f"✅ Successfully booked {len(successful_bookings)} slots on {day_name} {date_str}:\n{slots_text}")
    
    if failed_bookings:
        slots_text = ', '.join(sorted(failed_bookings))
        if len(failed_bookings) == 1:
            message_parts.append(f"❌ Could not book {slots_text} (already taken)")
        else:
            message_parts.append(f"❌ Could not book {len(failed_bookings)} slots (already taken):\n{slots_text}")
    
    result_message = '\n\n'.join(message_parts)
    await update.callback_query.edit_message_text(result_message)

async def handle_direct_cancellation(update: Update, context: ContextTypes.DEFAULT_TYPE, day_name: str, time_slot: str):
    """Handle cancellation directly from the time slots view"""
    user_id = update.callback_query.from_user.id
    
    logger.info(f"Attempting to cancel slot {time_slot} on {day_name} for user {user_id}")
    
    # Debug: Check if slot exists and ownership
    if not calendar.is_slot_booked(day_name, time_slot):
        await update.callback_query.answer("❌ Slot is not booked", show_alert=True)
        return
    
    booking_info = calendar.bookings.get(day_name, {}).get(time_slot, {})
    if booking_info.get('user_id') != user_id:
        await update.callback_query.answer("❌ This is not your booking", show_alert=True)
        return
    
    success = calendar.cancel_slot(day_name, time_slot, user_id)
    
    if success:
        await update.callback_query.answer(f"✅ Cancelled {time_slot}")
        # Refresh the time slots view to show the change
        await show_time_slots_safe(update, context, day_name)
    else:
        await update.callback_query.answer("❌ Failed to cancel booking", show_alert=True)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses"""
    query = update.callback_query
    
    data = query.data
    logger.info(f"Received callback data: '{data}'")
    
    if data.startswith("day_"):
        await query.answer()
        day_name = data.replace("day_", "")
        await show_time_slots_safe(update, context, day_name)
    
    elif data.startswith("toggle_"):
        # Handle slot selection/deselection
        parts = data.replace("toggle_", "").split("_", 1)
        day_name = parts[0]
        time_slot = parts[1].replace('h', ':')
        
        user_id = query.from_user.id
        calendar.toggle_user_slot_selection(user_id, day_name, time_slot)
        
        # Provide immediate feedback via callback answer
        selected_slots = calendar.get_user_selected_slots(user_id, day_name)
        if time_slot in selected_slots:
            await query.answer(f"✅ Selected {time_slot}")
        else:
            await query.answer(f"❌ Deselected {time_slot}")
        
        # Debounce UI updates
        if not hasattr(context, 'ui_update_pending'):
            context.ui_update_pending = {}
        
        # Cancel any pending update for this day
        if day_name in context.ui_update_pending:
            context.ui_update_pending[day_name].cancel()
        
        # Schedule a new update with delay
        async def delayed_update():
            await asyncio.sleep(0.5)
            try:
                await show_time_slots_safe(update, context, day_name)
            except Exception as e:
                logger.error(f"Error in delayed UI update: {e}")
            finally:
                if day_name in context.ui_update_pending:
                    del context.ui_update_pending[day_name]
        
        context.ui_update_pending[day_name] = asyncio.create_task(delayed_update())
    
    elif data.startswith("confirm_"):
        await query.answer()
        day_name = data.replace("confirm_", "")
        user_id = query.from_user.id
        time_slots = calendar.get_user_selected_slots(user_id, day_name)
        
        if time_slots:
            calendar.clear_user_selection(user_id, day_name)
            await handle_multiple_booking(update, context, day_name, time_slots)
        else:
            await query.answer("No slots selected!", show_alert=True)
    
    elif data.startswith("clear_"):
        await query.answer("Selection cleared!")
        day_name = data.replace("clear_", "")
        user_id = query.from_user.id
        calendar.clear_user_selection(user_id, day_name)
        await show_time_slots_safe(update, context, day_name)
    
    elif data.startswith("cancel_direct_"):
        parts = data.replace("cancel_direct_", "").split("_", 1)
        day_name = parts[0]
        time_slot = parts[1].replace('h', ':')
        await handle_direct_cancellation(update, context, day_name, time_slot)
    
    elif data == "back_to_days":
        await query.answer()
        week_dates = calendar.get_week_dates()
        keyboard = []
        for date in week_dates:
            day_name = calendar.get_day_name_from_date(date)
            date_str = date.strftime("%d/%m")
            callback_data = f"day_{day_name}"
            keyboard.append([InlineKeyboardButton(f"{day_name} {date_str}", callback_data=callback_data)])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Select a day:", reply_markup=reply_markup)
    
    elif data == "not_your_booking":
        await query.answer("This booking belongs to someone else!", show_alert=True)

async def post_init(application: Application) -> None:
    """Set bot commands - doesn't seem to work"""
    await application.bot.set_my_commands([
        ('status', 'Shows if the telegram bot is working'),
        ('addMinutes', 'Add minutes to daily allowance'),
        ('setDailyMinutes', 'Set the daily minutes allowance'),
        ('help', 'Shows information about commands'),
        ('pause', 'Stops the monitoring system'),
        ('restart', 'Ends the pause'),
        ('kill', 'WARNING, closes everything, cannot restart with telegram commands'),
        ('calendar', 'Show weekly TV time calendar'),
        ('book', 'Book TV time slots'),
        ('my_bookings', 'Show your current bookings')
    ])

def main() -> None:
    """Main function to run the bot"""
    global global_queue_parent_send
    global continue_running
    global manager
    global interval
    global global_daily_used_minutes
    global global_daily_minutes
    
    # Create the Application and pass it your bot's token
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Add command handlers
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("addMinutes", add_minutes))
    application.add_handler(CommandHandler("setDailyMinutes", set_daily_minutes))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("pause", pause_command))
    application.add_handler(CommandHandler("restart", restart_command))
    application.add_handler(CommandHandler("kill", kill_command))
    
    # Add calendar command handlers
    application.add_handler(CommandHandler("calendar", show_calendar))
    application.add_handler(CommandHandler("book", book_slot))
    application.add_handler(CommandHandler("my_bookings", my_bookings))
    application.add_handler(CallbackQueryHandler(button_handler))

    send_to_telegram("Telegram startup, "+ commands + ", time: " + str(datetime.now()))
    send_to_telegram("Remaining minutes: " + str(global_daily_minutes-global_daily_used_minutes)+" out of "+str(global_daily_minutes))
    global_daily_used_minutes = global_daily_used_minutes - interval # Account for first while loop removing 2 minutes immediately

    schedule_background_task()

    # Run the bot until the user presses Ctrl-C
    try:
        application.run_polling()
    except KeyboardInterrupt:
        logger.info("Stopping the script...")
        continue_running = False
        if manager:
            manager.close()
        time.sleep(3)
        
        loop = asyncio.get_event_loop()
        for task in asyncio.all_tasks(loop=loop):
            task.cancel()
        loop.run_until_complete(asyncio.gather(*asyncio.all_tasks(loop=loop), return_exceptions=True))
        loop.close()

if __name__ == "__main__":
    main()   

async def set_daily_minutes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the daily minutes allowance"""
    global global_initial_daily_minutes
    global global_daily_minutes
    global global_daily_used_minutes
    global instant_consumption
    global global_error_minutes
    global half_hours
    global AUTHORIZED_USER_ID
    global config
    
    user = update.effective_user
    if AUTHORIZED_USER_ID and user.id != AUTHORIZED_USER_ID:   
        await update.message.reply_text(user.first_name + ", you are not authorized to give me this command")
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /setDailyMinutes <minutes>\nExample: /setDailyMinutes 180")
        return
    
    try:
        new_daily_minutes = int(context.args[0])
        if new_daily_minutes < 0:
            await update.message.reply_text("Daily minutes cannot be negative")
            return
        
        # Update configuration
        config.set('daily_minutes', new_daily_minutes)
        
        # Update global variables
        global_initial_daily_minutes = new_daily_minutes
        global_daily_minutes = new_daily_minutes
        
        await update.message.reply_html(
            rf"Hello {user.mention_html()}! Daily minutes set to {new_daily_minutes} minutes"
        )
        await update.message.reply_text(f"Configuration saved to config.json")
        
    except ValueError:
        await update.message.reply_text("Please provide a valid number of minutes")
        return
    
    await update.message.reply_text("Remaining minutes: " + str(global_daily_minutes-global_daily_used_minutes)+" out of "+str(global_daily_minutes)+" minutes, error minutes " + str(global_error_minutes) + ", half hours: " + str(half_hours) + ", time: " + str(datetime.now()))