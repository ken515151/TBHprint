"""TBHprint tray applet - one codebase for Windows and Linux (and macOS).

pystray draws the tray icon and menu on every platform; tkinter (bundled
with Python on Windows, python3-tk on Linux) draws the Status, History,
Settings and Log windows. The applet is a CLIENT of the daemon's control
channel (tbhprint.control), exactly like the CLI - on Windows it can also
run the daemon embedded in its own process so a single logon task gives
the desk both.
"""
