<span style="font-size:24px; font-weight:bold;">Screen Time Parental Control for tv/monitor with smart plugs and Telegram</span>

<span style="font-size:20px; font-weight:bold;">📂 Table of Contents</span>

- [About](#about)
- [Telegram Bot and chat](#telegram-bot-and-chat)
  - [🤖 Available commands](#-available-commands)
  - [🤖 How to create a Telegram BOT](#-how-to-create-a-telegram-bot)
  - [📱 How to get the Chat ID](#-how-to-get-the-chat-id)
- [Raspberry Pi Zero W](#raspberry-pi-zero-w)
  - [Prepare an SD card with a Raspberry Pi OS image (so you can boot it).](#prepare-an-sd-card-with-a-raspberry-pi-os-image-so-you-can-boot-it)
  - [Connecting to Raspberry Pi via SSH in Ms Windows](#connecting-to-raspberry-pi-via-ssh-in-ms-windows)
  - [Set up the MQTT broker Mosquitto in the Raspberry Pi](#set-up-the-mqtt-broker-mosquitto-in-the-raspberry-pi)
  - [Create the “venv” Virtual environment for python](#create-the-venv-virtual-environment-for-python)
  - [Use venv](#use-venv)
  - [create a .env file with the credentials of the services](#create-a-env-file-with-the-credentials-of-the-services)
  - [create a "usersMQTT.json" file with the data of the users, e.g.:](#create-a-usersmqttjson-file-with-the-data-of-the-users-eg)
  - [create a "calendarMQTT.json" file with the datetime slots of the users, e.g.:](#create-a-calendarmqttjson-file-with-the-datetime-slots-of-the-users-eg)
  - [Copy files from Ubuntu to Raspberry Pi](#copy-files-from-ubuntu-to-raspberry-pi)
  - [Copy files from Ms Windows to Raspberry Pi](#copy-files-from-ms-windows-to-raspberry-pi)
    - [Copy the file to the right folder from an ssh session connected ot the Raspberry Pi](#copy-the-file-to-the-right-folder-from-an-ssh-session-connected-ot-the-raspberry-pi)
  - [Use systemD to start the script at boot:](#use-systemd-to-start-the-script-at-boot)
  - [optimizing power consumption](#optimizing-power-consumption)
    - [turn off USB port](#turn-off-usb-port)
    - [Disable Bluetooth](#disable-bluetooth)
    - [Disable HDMI](#disable-hdmi)
    - [Disable On-Board LEDs](#disable-on-board-leds)
    - [lower clock frequency](#lower-clock-frequency)


# <span id="anchor"></span>About 
The televisions are powered by four Tasmota plugs (Refoss P11) which support MQTT protocol. The smart plugs can be used by 5 Telegram users. A smart plug is turned off after a certain daily limit of minutes has been reached or outside allowed time slots(30 mins each) ranging from 7:30 am to midnight. 
Disclaimer: in this document I am describing my implementation; I am not suggesting you do the same, safety must be assessed by a professional.
The cable of each tv is glued to the plug with a very small amount (so as not to impede heat dissipation) of fire-resistant glue for electrical parts. 

"TvTelegramBotMQTT.py" runs in a Raspberry Pi Zero W (which is also the MQTT broker), it manages  and uses the messaging app Telegram to communicate with the users.

The first plug is associated by default to the user "user1", if another users starts the plug and then stops it, the plug is assigned again to "user1". Error minutes from any plugs are assigned to "user1" to decrease the risk of tampering. System uptime is shown to decrease the risk of tampering by turning off the Rasperry Pi.

The script is run at boot via systemd, so you just have to connect the Raspberry Pi Zero W to the power supply. 

A user can connect/disconnect from a plug in order to turn it on and use their minutes.
There is a maximum amount of minutes (available every day) for each user that resets after midnight. 
The maximum amount of minutes and the time slots are saved to json files. If the smart plug is disconnected "error" minutes are added. 

Every two minutes the power delivered by the smart plug is read and if it is greater than the threshold "powered_on_min_watts" the available amounts of minutes is decreased. The threshold is useful because the television can consume power whilst in stand-by.
Minutes can be added or subtracted during the day but these changes are lost after midnight.
The timeslots can be set for each week day. 
Every half hour the script posts to telegram a status message.
# <span id="anchor-1"></span><span id="anchor-2"></span><span id="anchor-3"></span><span id="anchor-4"></span><span id="anchor-5"></span>Telegram Bot and chat

## <span id="anchor-6"></span><span id="anchor-7"></span><span id="anchor-8"></span><span id="anchor-9"></span>🤖 Available commands
*Available Commands:*
- `/start` - Register as user
- `/listplugs` - List available plugs
- `/startplug <plugname>` - start using a plug
- `/stopplug [plugname]` - stop using current or given plug
- `/status` - Show system status
- `/help` - Show all commands
- `/my_bookings` - Show your bookings

*Admin Commands:*
- `/addminutes <user_id|@username> <minutes>` - add minutes to the selected user (they are cleared after midnight)
- `/setDailyMinutes <user_id|@username> <minutes>` - set the daily amount of minutes
- `/timerMinutesHoliday <plugname> <minutes>` - set minutes of holidays (admin only)
- `/book <user>` - Manage bookings
- `/my_bookings [user_id|@username]` - admin may check others
- `/calendar` - View weekly calendar
- `/activate <action> <plug>` - Enable/disable plugs
- `/plug <on|off> <plug>` - Control plugs
## <span id="anchor-6"></span><span id="anchor-7"></span><span id="anchor-8"></span><span id="anchor-9"></span>🤖 How to create a Telegram BOT

Open Telegram and search for @BotFather

Type `/start` and then `/newbot`

Choose a name for the bot (e.g., "Consolidation Detector Bot")

Choose a username (it must end with 'bot', e.g., "screen_time_bot")

BotFather will give you a TOKEN – SAVE IT!

## <span id="anchor-10"></span><span id="anchor-11"></span><span id="anchor-12"></span><span id="anchor-13"></span><span id="anchor-14"></span>📱 How to get the Chat ID

Stop the bot(otherwise it will consume the data before you can see it),
send a command to your bot(, if it is not a command the bot should be an
administrator to be able to read the message)

Open: \[https://api.telegram.org/bot\<TOKEN\>/getUpdates
\](*https://api.telegram.org/bot\<TOKEN\>/getUpdates*)

look for: "chat":{"id": NUMBER} – that’s your chat_id

# <span id="anchor-15"></span><span id="anchor-16"></span><span id="anchor-17"></span><span id="anchor-18"></span><span id="anchor-19"></span>Raspberry Pi Zero W

## <span id="anchor-20"></span><span id="anchor-21"></span><span id="anchor-22"></span><span id="anchor-23"></span>Prepare an SD card with a Raspberry Pi OS image (so you can boot it).

This is how you normally set up a fresh Pi:

- Download ****Raspberry Pi Imager**** from raspberrypi.com
- Install it on your PC (Linux, Windows, or macOS).

<!-- -->

- Insert your microSD card into the computer.

<!-- -->

- Open Raspberry Pi Imager → select OS (e.g. **Raspberry Pi OS
  64-bit**).

<!-- -->

- Select the SD card as storage.

<!-- -->

- Click ****Write**** → it flashes the OS image onto the SD card.

<!-- -->

- Insert SD card into the Pi and boot. Set WiFi and other details:

<img src="docs/readme_pictures/os_personalization.png"
style="width:5.6457in;height:7.1252in" />

- Enable SSH:

<img src="docs/readme_pictures/os_SSH_enable.png"
style="width:5.6354in;height:6.8957in" />

In Raspberry Pi Zero W I have created the folders “tv” for the script
and "venvTv" for the Virtual environment

## <span id="anchor-24"></span><span id="anchor-25"></span><span id="anchor-26"></span><span id="anchor-27"></span><span id="anchor-28"></span><span id="anchor-29"></span>Connecting to Raspberry Pi via SSH in Ms Windows

`ssh alexl@raspberrypi` or, less securely: `ssh -o
StrictHostKeyChecking=no alexl@raspberrypi`. If it gives an error and
does not ask you to add the client, delete the raspberrypi client in the
Windows file “C:\Users\alexl\\ssh\known_hosts”.

Once connected, use the following command if you want to disconnect:
`exit`

If you want to restart the raspberry: `sudo reboot`
## <span id="anchor-30"></span><span id="anchor-31"></span><span id="anchor-32"></span><span id="anchor-33"></span><span id="anchor-34"></span><span id="anchor-35"></span>Set up the MQTT broker Mosquitto in the Raspberry Pi
`sudo apt update`

`sudo apt install mosquitto mosquitto-clients`

`sudo systemctl enable mosquitto`

## <span id="anchor-30"></span><span id="anchor-31"></span><span id="anchor-32"></span><span id="anchor-33"></span><span id="anchor-34"></span><span id="anchor-35"></span>Create the “venv” Virtual environment for python

Connecting to the Raspberry pi via “ssh” (see previous chapter)

Create a virtual environment in the folder “venvTv”: `python3 -m venv venvTv`

navigate to the bin subfolder: `cd venvTv/bin`

activate the virtual environment: `source activate`
You should now see (venvTv) at the start of your command prompt.

## <span id="anchor-36"></span><span id="anchor-37"></span><span id="anchor-38"></span><span id="anchor-39"></span><span id="anchor-40"></span><span id="anchor-41"></span>Use venv

Once the virtual environment has been activated, Python usage proceeds
in the normal fashion. Running python or pip will be done in the context
of the virtual environment.

Modules installed with pip will be placed in the local “venv” folders
- **sudo should not be used**.

(venvTv) alexl@raspberrypi:~ \$ `pip install python-telegram-bot`

(venvTv) alexl@raspberrypi:~ \$ `pip install dotenv`

(venvTv) alexl@raspberrypi:~ \$ `pip install paho-mqtt`
install the latest version compatible with Meross

(venvTv) alexl@raspberrypi:~ \$ `pip install aiomqtt`
Install the library to have async MQTT

## <span id="anchor-48"></span><span id="anchor-49"></span><span id="anchor-50"></span><span id="anchor-51"></span><span id="anchor-52"></span><span id="anchor-53"></span>create a .env file with the credentials of the services
The .env files contain the IDs, passwords and tokens used to access the services. The .env file must be created in the same folder ("tv") as the python script. The entry "AUTHORIZED_USER_ID" is the ID of the user who can send commands to the telegram bot.

To create the file you can run `nano.env`

Paste in your credentials (replace the example values with your own):
<pre>TELEGRAM_BOT_TOKEN_MQTT=nnnnnnnnnn:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
AUTHORIZED_USER_ID_MQTT = nnnnnnnnnn
chatID_MQTT='-nnnnnnnnnn'
user1_id = "nnnnnn"
plug1_id = "nnnnnn"</pre>
Press Ctrl+O to save, then Ctrl+X to exit.
## <span id="anchor-48"></span><span id="anchor-49"></span><span id="anchor-50"></span><span id="anchor-51"></span><span id="anchor-52"></span><span id="anchor-53"></span>create a "usersMQTT.json" file with the data of the users, e.g.:
You can use nano or any text editor to create this file in your project directory.
Copy the sample content below and edit as needed
<pre>{
  "100001": {
    "user_id": 100001,
    "username": "user1",
    "default_minutes": 125,
    "initial_minutes": 125,
    "remaining_minutes": 125,
    "used_minutes": 0,
    "error_minutes": 0
  },
  "100002": {
    "user_id": 100002,
    "username": "user2",
    "default_minutes": 125,
    "initial_minutes": 125,
    "remaining_minutes": 125,
    "used_minutes": 0,
    "error_minutes": 0
  },
  "100003": {
    "user_id": 100003,
    "username": "user3",
    "default_minutes": 125,
    "initial_minutes": 125,
    "remaining_minutes": 125,
    "used_minutes": 0,
    "error_minutes": 0
  },
  "100004": {
    "user_id": 100004,
    "username": "user4",
    "default_minutes": 125,
    "initial_minutes": 125,
    "remaining_minutes": 125,
    "used_minutes": 0,
    "error_minutes": 0
  },
  "100005": {
    "user_id": 100005,
    "username": "user5",
    "default_minutes": 125,
    "initial_minutes": 125,
    "remaining_minutes": 125,
    "used_minutes": 0,
    "error_minutes": 0
  }
}</pre>
## <span id="anchor-48"></span><span id="anchor-49"></span><span id="anchor-50"></span><span id="anchor-51"></span><span id="anchor-52"></span><span id="anchor-53"></span>create a "calendarMQTT.json" file with the datetime slots of the users, e.g.:
<pre>{
  "Mon": {
    "08:00": {
      "user_id": 100001,
      "username": "user1",
      "booked_at": "2025-09-19T23:05:36.284277"
    },
    "08:30": {
      "user_id": 100001,
      "username": "user1",
      "booked_at": "2025-09-19T23:05:36.284889"
    },
    "09:00": {
      "user_id": 100001,
      "username": "user1",
      "booked_at": "2025-09-19T23:05:36.289401"
    }
  },
  "Tue": {
    "20:00": {
      "user_id": 100001,
      "username": "user1",
      "booked_at": "2025-09-19T23:06:41.615729"
    },
    "20:30": {
      "user_id": 100001,
      "username": "user1",
      "booked_at": "2025-09-19T23:06:41.616601"
    }
  }
}</pre>
## <span id="anchor-42"></span><span id="anchor-43"></span><span id="anchor-44"></span><span id="anchor-45"></span><span id="anchor-46"></span><span id="anchor-47"></span>Copy files from Ubuntu to Raspberry Pi
Open a terminal on your computer (not via SSH on the Pi) and run:

alex@alex:~/Downloads$ `scp /home/alex/Documents/pythonProjects/Screen_time_limit_with_smart_plug/TvTelegramBotMQTT.py alexl@raspberrypi:tv
TvTelegramBotMQTT.py `

In the previous command replace "/home/alex/Documents/pythonProjects/Screen_time_limit_with_smart_plug/TvTelegramBotMQTT.py" with the path to your file, and "alexl" with your Pi username.

alex@alex:~/Downloads$ `scp /home/alexl/Documents/pythonProjects/screen_time_limit_with_smart_plug/.env alexl@raspberrypi:tv`

alex@alex:~/Downloads$ `scp /home/alex/Documents/pythonProjects/Screen_time_limit_with_smart_plug/calendarMQTT.json alexl@raspberrypi:tv
calendarMQTT.json`

alex@alex:~/Downloads$ `scp /home/alex/Documents/pythonProjects/Screen_time_limit_with_smart_plug/configMQTT.json alexl@raspberrypi:tv
configMQTT.json`

alex@alex:~/Downloads$ `scp /home/alex/Documents/pythonProjects/Screen_time_limit_with_smart_plug/tvTelegramMQTT.service alexl@raspberrypi:/tmp/`

alex@alex:~/Downloads$ `ssh alexl@raspberrypi "sudo install -m 644 /tmp/tvTelegramMQTT.service /lib/systemd/system/"`

## <span id="anchor-42"></span><span id="anchor-43"></span><span id="anchor-44"></span><span id="anchor-45"></span><span id="anchor-46"></span><span id="anchor-47"></span>Copy files from Ms Windows to Raspberry Pi

From PowerShell, without being connected through “ssh”:

> PS C:\Users\alexl\> `scp "C:\Users\alexl\OneDrive\Documenti\pythonProjects\parental\TvTelegramBotMQTT.py" alexl@raspberrypi:tv`

> PS C:\Users\alexl\> `scp "C:\Users\alexl\OneDrive\Documenti\pythonProjects\parental\.env" alexl@raspberrypi:tv`

> PS C:\Users\alexl\> `scp "C:\Users\alexl\OneDrive\Documenti\pythonProjects\parental\calendarMQTT.json" alexl@raspberrypi:tv`

> PS C:\Users\alexl\> `scp "C:\Users\alexl\OneDrive\Documenti\pythonProjects\parental\configMQTT.json" alexl@raspberrypi:tv`

> PS C:\Users\alexl\> `scp "C:\Users\alexl\OneDrive\Documenti\pythonProjects\parental\tvTelegramMQTT.service" alexl@raspberrypi:tvTelegramMQTT.service`

alexl@raspberrypi:~ \$ `exit`

### <span id="anchor-68"></span><span id="anchor-69"></span><span id="anchor-70"></span><span id="anchor-71"></span><span id="anchor-72"></span><span id="anchor-73"></span><span id="anchor-74"></span>Copy the file to the right folder from an ssh session connected ot the Raspberry Pi
- alexl@raspberrypi:~ \$ `sudo cp TvTelegramMQTT.service /lib/systemd/system/tvTelegramMQTT.service`

Giving rights to the file:

- alexl@raspberrypi:~ \$ `sudo chmod 644 /lib/systemd/system/tvTelegramMQTT.service`

It is a good idea to check if the files has been copied by going into the folders and listing their content

## <span id="anchor-48"></span><span id="anchor-49"></span><span id="anchor-50"></span><span id="anchor-51"></span><span id="anchor-52"></span><span id="anchor-53"></span>Use systemD to start the script at boot:

Credits: [**https://learn.adafruit.com/python-virtual-environment-usage-on-raspberry-pi/automatically-running-at-boot**](https://learn.adafruit.com/python-virtual-environment-usage-on-raspberry-pi/automatically-running-at-boot)

alexl@raspberrypi:~ \$ `sudo systemctl daemon-reload`

alexl@raspberrypi:~ \$ `sudo systemctl enable TvTelegramMQTT`

alexl@raspberrypi:~ \$ `sudo systemctl start TvTelegramMQTT`

to check the status:

- alexl@raspberrypi:~ \$ `systemctl status TvTelegramMQTT.service`

to exit the status press “q”

to reboot:

- alexl@raspberrypi:~ \$ `systemctl status TvTelegramMQTT.service`

to stop the service:

- alexl@raspberrypi:~ \$ `sudo systemctl stop TvTelegramMQTT`

disable at startup:

- alexl@raspberrypi:~ \$ `sudo systemctl disable TvTelegramMQTT`

## <span id="anchor-54"></span><span id="anchor-55"></span><span id="anchor-56"></span><span id="anchor-57"></span><span id="anchor-58"></span><span id="anchor-59"></span>optimizing power consumption

Credits: "Rob Lauer", see webpage:  [**https://blues.com/blog/tips-tricks-optimizing-raspberry-pi-power/**](https://blues.com/blog/tips-tricks-optimizing-raspberry-pi-power/)

### <span id="anchor-60"></span><span id="anchor-61"></span><span id="anchor-62"></span><span id="anchor-63"></span>turn off USB port

To shut off power on USB ports (this shuts power on ethernet as well):  
`echo '1-1' \| sudo tee /sys/bus/usb/drivers/usb/unbind`
  
To turn power back on  
`echo '1-1' \| sudo tee /sys/bus/usb/drivers/usb/bind`

### <span id="anchor-64"></span><span id="anchor-65"></span><span id="anchor-66"></span><span id="anchor-67"></span>Disable Bluetooth

Once again there are a number of ways to do this. The easiest way is to
utilise the program \| rfkill \| again. Type the following into the
terminal and press enter for each line (if you have already installed
rkill you will not need to reinstall it). Then on reboot it will be
disabled.

`sudo apt install rfkill`  
`sudo rfkill block bluetooth`  

If you want to re-enable Bluetooth simply type the following into the
terminal.

`sudo rfkill unblock bluetooth`

### <span id="anchor-68"></span><span id="anchor-69"></span><span id="anchor-70"></span><span id="anchor-71"></span><span id="anchor-72"></span><span id="anchor-73"></span><span id="anchor-74"></span>Disable HDMI

Whenever you are remotely accessing the Raspberry Pi using SSH or
running it headless then likely there will be no need to run anything
through the onboard HDMI ports. This means you can disable power to
these ports which will give a small improvement to battery life (~20mA).
Type the following into the terminal to disable power to the HDMI ports.
As soon as you press enter it will be disabled.

- `sudo /opt/vc/bin/tvservice -o`

If you want to re-enable HDMI connection simply type the following into
the terminal.

- `sudo /opt/vc/bin/tvservice -p`

### <span id="anchor-75"></span><span id="anchor-76"></span><span id="anchor-77"></span><span id="anchor-78"></span><span id="anchor-79"></span><span id="anchor-80"></span>Disable On-Board LEDs

The LEDs are a very small power draw and give a good indication visually
on the board as to what the Raspberry Pi Board is doing. However, if you
have the Raspberry Pi tucked away in a hidden place or covered in a case
then likely there will be no need to run the LEDs as nobody will see
them. It also means a running Raspberry Pi will appear to be not powered
which could make for some sweet stealthy spy electronic projects. So by
disabling power to the LEDs you will gain a small improvement to overall
battery life (\<2mA potential saving per LED). To do this edit
the /boot/config.txt file and add the following lines (when using a
Raspberry Pi 4 Model B). Save the file and then on reboot the lights
will be disabled. To return the LED lights to normal remove the added
lines.

<pre>dtparam=act_led_trigger=none

dtparam=act_led_activelow=off

dtparam=pwr_led_trigger=none

dtparam=pwr_led_activelow=off</pre>

### <span id="anchor-81"></span><span id="anchor-82"></span><span id="anchor-83"></span><span id="anchor-84"></span><span id="anchor-85"></span><span id="anchor-86"></span>lower clock frequency

file to be edited: `sudo nano /boot/firmware/config.txt`

Modify in :`arm_boost=0`

Add below `[all]` :

<pre>arm_freq=600

arm_freq_max=600</pre>

comment the linedtparam=audio=on`
