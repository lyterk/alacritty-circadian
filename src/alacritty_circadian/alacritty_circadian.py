#!/usr/bin/env python

"""
Small python module for handling the automatic theme switching of alacritty
via explicit time or phases of the sun
"""

# Std imports
import os
import sys
from argparse import ArgumentParser
from pathlib import Path
from pprint import pprint
from datetime import datetime, timezone, timedelta
from threading import Thread, Timer, Lock, get_ident

# dbus
import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

# External imports
from tomlkit import load as load_toml, dump as dump_toml
from astral import Observer
from astral.sun import sun

# Create global thread lock
lock = Lock()
# Global thread queue
thread_list = []


def check_path(p: Path) -> Path:
    p = p.expanduser()
    if not p.exists():
        sys.exit(f"[ERROR] Critical file {p} does not exist, check your configurations")
    return p


config_path_str = (
    os.getenv("APPDATA") if sys.platform == "win32" else os.getenv("XDG_CONFIG_HOME")
)
config_path_str = "~/.config" if not config_path_str else config_path_str
alacritty_path = (Path(config_path_str) / "alacritty").expanduser()
parser = ArgumentParser(
    prog="alacritty_circadian", description="Change your alacritty theme by time of day"
)
parser.add_argument(
    "-s",
    "--alacritty-source",
    type=Path,
    default=alacritty_path / "alacritty.toml",
    help="Location of your `alacritty.toml` file. NOTE: This will be read from.",
)
parser.add_argument(
    "-d",
    "--alacritty-dest",
    type=Path,
    default=alacritty_path / "alacritty.toml",
    help="Location of your `alacritty.toml` file. NOTE: This will be read from.",
)
parser.add_argument(
    "-c",
    "--circadian-path",
    type=Path,
    default=alacritty_path / "circadian.toml",
    help="Location of your `circadian.toml` file",
)
args = parser.parse_args()
alacritty_source = check_path(args.alacritty_source)
circadian_path = check_path(args.circadian_path)
alacritty_dest = args.alacritty_dest.expanduser()

if alacritty_source == alacritty_dest:
    print(
        "[WARN] Your alacritty source and destination files are the same. This file will be mutated twice daily."
    )
    print("[WARN] You may wish to keep a source alacritty.toml in source control.")

with open(alacritty_source) as f:
    config = load_toml(f)

with open(circadian_path, "r") as f:
    circadian = load_toml(f)
theme_folder_path = Path(str(circadian["theme-folder"])).expanduser()
check_path(theme_folder_path)


times_of_sun = {
    "dawn",
    "sunrise",
    "noon",
    "sunset",
    "dusk",
}


def switch_theme(theme_data):
    """
    Put theme_data in alacritty's config_data.
    """
    config["colors"] = theme_data["colors"]
    with open(alacritty_dest, "w") as f:
        if not f.writable():
            print(f"{f} not writable")
        dump_toml(config, f)


def thread_switch_theme(theme_data):
    """
    Wrapper function to make a thread run switch_theme and exit gracefully.
    See switch_theme for more info.
    """
    # Thread locking to prevent race conditions
    with lock:
        switch_theme(theme_data)
    print("[LOG] Thread timer- {get_ident()} has finished execution, exiting")
    sys.exit()


def get_theme_time(theme, now_time):
    """
    Get the time associated with theme, either from a times_of_sun
    String or an HH:MM timestamp.
    """
    theme_time_str = theme["time"]
    if theme_time_str in times_of_sun:
        s_lat = circadian.get("coordinates", {}).get("latitude")
        s_lon = circadian.get("coordinates", {}).get("longitude")
        ok = True
        d = {"lat": s_lat, "lon": s_lon}
        # Catch both errors at once, if exist
        for k, v in d.items():
            try:
                d[k] = float(v)
            except ValueError:
                ok = False
        if not ok:
            sys.exit(f"[ERROR] Coordinates {s_lat}, {s_lon} not valid number(s)")
        obs = Observer(latitude=d["lat"], longitude=d["lon"])
        theme_time = sun(obs)[theme_time_str]
    else:
        try:
            theme_time = datetime.strptime(theme["time"], "%H:%M")
        except ValueError:
            sys.exit(f"[ERROR] Unknown time format {theme['time']}")
    theme_time = theme_time.replace(
        year=now_time.year, month=now_time.month, day=now_time.day
    )
    # "Convert" to localtime (datetime doesn't convert since the time is the
    # same as the localtime, so actually we just make the naive timestamp
    # offset aware)
    theme_time = theme_time.astimezone(tz=None)
    # Convert to UTC
    theme_time = theme_time.astimezone(tz=timezone.utc)
    return theme_time


def set_appropriate_theme(now_time):
    """
    Get the nearest neighbor themes list element to now_time and set it as the
    current theme.
    """
    # nearest list element neighbor to now_time
    diff = -1
    try:
        themes = circadian["themes"]
    except KeyError:
        sys.exit("[ERROR] Circadian config theme section not found")
    if not themes:
        sys.exit("[ERROR] No themes specified in circadian config")
    for theme in themes:
        theme_time = get_theme_time(theme, now_time)
        switch_time = now_time.replace(
            hour=theme_time.hour, minute=theme_time.minute, second=0, microsecond=0
        )
        delta_t = now_time - switch_time
        seconds = delta_t.seconds + 1
        if seconds > 0 and (seconds < diff or diff == -1):
            diff = seconds
            preferred_theme = theme

    theme_file = theme_folder_path / f"{preferred_theme['name']}.toml"

    if not theme_file.exists():
        sys.exit(f"[ERROR] {preferred_theme['name']} is not installed in {theme_file}")
    with open(theme_file, "r") as f:
        theme_data = load_toml(f)
    switch_theme(theme_data)


def set_theme_switch_timers():
    """
    Set a suitable theme and start/restart thread timers
    for theme switching. The main daemon loop.
    """
    set_appropriate_theme(datetime.now(timezone.utc))
    # Hot loop
    while True:
        now_time = datetime.now(timezone.utc)
        for theme in circadian["themes"]:
            theme_file = theme["name"] + ".toml"
            curr_theme_path = theme_folder_path / theme_file
            if not curr_theme_path.exists():
                sys.exit(
                    f"[ERROR] Theme {theme['name']} not installed in {theme_folder_path}"
                )
            theme_time = get_theme_time(theme, now_time)
            with open(curr_theme_path, "r") as f:
                theme_data = load_toml(f)
            if theme_time < now_time:
                # Set Date to today
                switch_time = now_time.replace(
                    day=now_time.day,
                    hour=theme_time.hour,
                    minute=theme_time.minute,
                    second=0,
                    microsecond=0,
                )
                # Add one day without overflowing current month
                switch_time = switch_time + timedelta(days=1)
            else:
                switch_time = now_time.replace(
                    hour=theme_time.hour,
                    minute=theme_time.minute,
                    second=0,
                    microsecond=0,
                )
            delta_t = switch_time - now_time
            seconds = delta_t.seconds + 1
            timer_thread = Timer(seconds, thread_switch_theme, [theme_data])
            thread_list.append(timer_thread)
            timer_thread.start()
            # Flush stdout to output to log journal
            local_timezone = datetime.now(timezone.utc).astimezone().tzinfo
            print(
                f"[LOG] Setting up timer- {timer_thread.ident} for {theme['name']}"
                f" at: {switch_time.astimezone(local_timezone)}",
                flush=True,
            )
        for thread in thread_list:
            thread.join()
        # All threads have finished, flush
        thread_list.clear()


def handle_wakeup_callback(going_to_sleep_flag):
    if going_to_sleep_flag == 0:
        print("[LOG] System has just woken up from hibernate/sleep, refreshing threads")
        for thread in thread_list:
            thread.cancel()
        set_appropriate_theme(datetime.now(timezone.utc))


def enable_dbus_main_loop():
    DBusGMainLoop(set_as_default=True)
    system_bus = dbus.SystemBus()
    dbus.mainloop.glib.threads_init()
    system_bus.add_signal_receiver(
        handle_wakeup_callback,
        "PrepareForSleep",
        "org.freedesktop.login1.Manager",
        "org.freedesktop.login1",
    )
    loop = GLib.MainLoop()
    loop.run()


def main():
    """
    Entry point
    """
    print("[LOG] Starting dbus main loop")
    dbus_thread = Thread(target=enable_dbus_main_loop)
    dbus_thread.start()
    # Set flag to true for first run
    set_theme_switch_timers()


if __name__ == "__main__":
    main()
