# ESP32 primer — the crash course this project assumes

Written for someone starting from zero on microcontrollers. Read top to
bottom once; skim again on device-arrival day.

## What a microcontroller actually is

A microcontroller (MCU) is a complete computer on one chip — CPU, RAM, flash
storage, peripherals — designed to run **one program, forever**. Unlike the
HTPC:

- **No operating system.** Your program *is* the software. It starts at
  power-on and runs until power-off. No processes, no users, no filesystem
  unless you add one.
- **Tiny resources, by PC standards.** The ESP32-S3 in the Inkplate has two
  240 MHz cores and (unusually generously) 16 MB of RAM — a Raspberry Pi has
  ~250× more. For "fetch an image and display it," this is plenty.
- **Absurdly low power.** This is the entire point. An idle Pi Zero draws
  ~100 milliwatts; a deep-sleeping ESP32 draws ~50 *micro*watts. That
  four-orders-of-magnitude gap is why the Inkplate can promise months on one
  charge and a Pi-based frame can't survive a weekend.

## The ESP32 family, and the chip in this board

ESP32 is a line of Wi-Fi/Bluetooth MCUs from Espressif (Shanghai) that took
over the hobby/IoT world because they're a few dollars and radio is built in.
The Inkplate 13SPECTRA uses an **ESP32-S3-WROOM-2-N32R16V** — decoding that
name: S3 generation (dual-core Xtensa LX7), WROOM-2 module package, N32 =
32 MB flash, R16 = 16 MB PSRAM.

Terms you'll meet:

- **Flash (32 MB)** — where the program lives; survives power loss. Your
  compiled firmware is a few hundred KB; the rest is free for assets/files.
- **PSRAM (16 MB)** — extra RAM bolted on beside the chip's internal ~512 KB.
  Needed here because one 1600×1200 frame buffer is ~2 MB — a plain ESP32
  without PSRAM literally could not hold the screen in memory.
- **RTC memory** — a few KB that stays powered during deep sleep. Our
  firmware keeps two integers there (boot count, last-drawn version). Wiped
  by power loss or the reset button, kept across sleep cycles. The
  `RTC_DATA_ATTR` marker in the sketch is what puts a variable there.
- **Deep sleep** — the chip powers off everything except a clock and RTC
  memory (~14 µA on this board). Waking is a *reboot*: your program starts
  from the top, not from where it left off. This is why the firmware is
  structured as "do everything in setup(), then sleep" — there is no long-
  running loop in a deep-sleep design.
- **GPIO / I2C / SPI / UART / Qwiic** — pins and protocols for attaching
  sensors and peripherals. The board's three **Qwiic** ports are just I2C
  with a standardized plug — sensors daisy-chain with no soldering. Irrelevant
  until you want, say, a temperature reading on the dashboard.

## How code gets onto it, and how you see what it's doing

1. You write C++ in the **Arduino IDE** (an editor + compiler + flasher).
   "Arduino" here is a *software ecosystem*, not the hardware brand — ESP32
   boards adopted its conventions (`setup()`/`loop()`, the Library Manager).
2. Connect USB-C, pick the board + port, click **Upload**. The IDE compiles
   for the Xtensa CPU and writes the result into flash (~30 s). This is
   "flashing." The old program is gone; the new one boots immediately.
3. **Serial Monitor** is your `printf` debugger and only window into the
   device: the running program's `Serial.printf(...)` lines appear in the IDE
   over the same USB cable (baud rate 115200 must match both sides). Our
   firmware narrates every step precisely because this is all you get — there
   is no SSH, no logs, no debugger in normal use.

Rite-of-passage errors, pre-answered: if the port doesn't appear on Linux,
add yourself to `dialout` (`sudo usermod -aG dialout $USER`, re-login). If an
upload fails mid-boot-loop, hold the board's boot/wake button while clicking
upload. If the serial output is garbage, the baud rate is wrong.

## The alternatives you'll see mentioned (and why we passed)

- **MicroPython** — Python on the chip; friendlier, but a second-class
  citizen on brand-new boards and slower. Our firmware is so small the
  language barely matters.
- **ESPHome** — no-code YAML firmware for Home Assistant setups. Great for
  sensors; its dashboard rendering happens *on the device*, which is exactly
  what our architecture avoids. Vendor only pledged support "in time for
  delivery."
- **ESP-IDF** — Espressif's raw professional SDK, what Arduino wraps. More
  control, much more ceremony. Nothing here needs it.

## E-paper in three paragraphs

E-paper holds an image with **zero power** — energy is spent only *changing*
the display. Pigment particles are physically moved by voltage and then stay
put. That's why a dashboard that updates hourly can live on a battery for
months: 23 hours, 59 minutes of every day, the device is essentially off and
the image is still there.

The cost is refresh behavior: a full update on a color panel like this takes
~20 seconds and flashes through color cycles as every pixel is driven to its
target. This is normal, not a defect — but it dictates the design: refresh
rarely, never refresh when content hasn't changed (our version-gate), and
treat the display as a *poster you reprint*, not a screen.

**Spectra 6** panels produce exactly six colors — black, white, yellow, red,
blue, green — with no shades in between. Intermediate tones are faked by
**dithering**: scattering dots of the available colors so the eye averages
them (newspaper photos, same trick). Our server does this with the
Floyd-Steinberg algorithm before the device ever sees the image. Design
takeaway for future screens: bold flat shapes and large text look superb;
photos look like nice newsprint; subtle gradients and small anti-aliased text
look bad. Check everything in the pane's *panel sim* preview.

## How our pieces map onto all this

| Concept | Where it shows up here |
|---|---|
| Deep sleep / wake-is-reboot | firmware does everything in `setup()`, ends in `esp_deep_sleep_start()` |
| RTC memory | `RTC_DATA_ATTR int bootCount, shownVersion` |
| Serial monitor | every step logged at 115200 baud; first tool on arrival day |
| PSRAM | makes full-frame PNG download/decode a non-issue |
| Flashing | Arduino IDE, README §3–4; needed ~once, thanks to server-side rendering |
| Refresh cost | version-gated redraws; `/api/display/meta` check costs ~100 bytes |
| Battery | `readBattery()` in every check-in → control pane badge |
| Dithering | `palette.py` on the server; firmware draws with `dither=false` |
