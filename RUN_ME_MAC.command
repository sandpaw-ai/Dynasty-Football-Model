#!/bin/bash
# Dynasty Model — Mac launcher
# Double-click this file in Finder. It does everything from scratch.

set -e

# Change to the directory of this script so paths are reliable
cd "$(dirname "$0")"

BLUE='\033[1;34m'
GREEN='\033[1;32m'
YELLOW='\033[1;33m'
RED='\033[1;31m'
RESET='\033[0m'

printf "${BLUE}\n========================================================\n"
printf "  DYNASTY MODEL — Mac launcher\n"
printf "========================================================${RESET}\n\n"

# --- Check Python ---------------------------------------------------------
printf "${BLUE}Checking for Python 3.11+...${RESET}\n"

PYTHON=""
for candidate in python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    version=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
    major=$(echo "$version" | cut -d. -f1)
    minor=$(echo "$version" | cut -d. -f2)
    if [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; then
      PYTHON="$candidate"
      printf "${GREEN}  Found $candidate (version $version)${RESET}\n"
      break
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  printf "${RED}\n  Could not find Python 3.11 or newer.${RESET}\n\n"
  printf "  Please install Python from one of these options:\n"
  printf "    - https://www.python.org/downloads/  (recommended)\n"
  printf "    - or in Terminal: ${YELLOW}brew install python@3.12${RESET}\n\n"
  printf "  Then double-click this file again.\n\n"
  read -p "Press Enter to close..."
  exit 1
fi

# --- Set up virtual environment ------------------------------------------
if [ ! -d ".venv" ]; then
  printf "\n${BLUE}First-time setup: creating virtual environment...${RESET}\n"
  "$PYTHON" -m venv .venv
  printf "${GREEN}  Done.${RESET}\n"
fi

# Activate it
source .venv/bin/activate

# --- Install dependencies (only if not already installed) ----------------
if ! python -c "import dynasty" 2>/dev/null; then
  printf "\n${BLUE}Installing dependencies (one-time, ~1 minute)...${RESET}\n"
  pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt
  pip install --quiet -e .
  printf "${GREEN}  Installed.${RESET}\n"
fi

# --- Run the model -------------------------------------------------------
python -m dynasty.launcher

# --- Keep window open so you can read messages ---------------------------
printf "\n\nPress Enter to close this window..."
read
