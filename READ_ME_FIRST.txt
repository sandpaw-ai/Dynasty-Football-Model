DYNASTY MODEL — READ THIS FIRST
================================

You should see two scripts in this folder:

    RUN_ME_MAC.command       — for Mac users
    RUN_ME_WINDOWS.bat       — for Windows users

WHAT TO DO:
-----------

1. If you don't have Python installed, download Python 3.12 from:
   https://www.python.org/downloads/

   ON WINDOWS: When the installer asks, check the box
   "Add Python to PATH" before clicking Install.

2. Double-click the script for your computer:

   Mac:      RUN_ME_MAC.command
   Windows:  RUN_ME_WINDOWS.bat

That's it. The script will:
  - Set up everything it needs (only the first time, ~1 minute)
  - Pull the latest dynasty rankings
  - Open a clean web page in your browser with the top 300 players

To get fresh rankings any time after that, just double-click the script
again. It will skip the setup and just refresh the data (~10 seconds).

The data file (dynasty_rankings.html) is also saved in this folder so
you can open it again without re-running the script.


MAC SECURITY WARNING
--------------------
The first time you double-click RUN_ME_MAC.command, macOS may say
"cannot be opened because it is from an unidentified developer".
To fix:
  1. In Finder, right-click (or Control-click) the file
  2. Choose "Open"
  3. Click "Open" in the dialog that appears
  4. Future double-clicks will work normally.


WINDOWS SECURITY WARNING
------------------------
The first time you double-click RUN_ME_WINDOWS.bat, you may see a
"Windows protected your PC" screen. To bypass:
  1. Click "More info"
  2. Click "Run anyway"


HAVING TROUBLE?
---------------
- If the window flashes and closes immediately, run from a Terminal /
  Command Prompt instead so you can see the error:

      Mac:      open a Terminal, drag RUN_ME_MAC.command into it, press Enter
      Windows:  open Command Prompt in this folder, type RUN_ME_WINDOWS.bat

- If you see "could not connect" or "403 Forbidden", your network is
  blocking the data sources. Try a different network (e.g. home wifi
  instead of corporate).

- If you see "Python not found" even after installing, restart your
  computer once so the PATH changes take effect.
