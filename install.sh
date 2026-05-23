#!/usr/bin/env bash
set -euo pipefail

# ───── bootstrap (curl | bash) ──────────────────────────────────────────
# When piped, $0 is the shell, not a file path. In that case we clone the
# repo to the canonical XDG location and re-exec install.sh from there.
# When run from an existing clone, this block is skipped.
SCRIBE_REPO_URL="${SCRIBE_REPO_URL:-https://github.com/cszach/scribe.git}"
SCRIBE_INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/scribe"

if [[ ! -f "${BASH_SOURCE[0]:-$0}" ]]; then
  if ! command -v git >/dev/null 2>&1; then
    echo "Scribe needs git to install itself. Install git first, then re-run:" >&2
    echo "  curl -fsSL https://raw.githubusercontent.com/cszach/scribe/main/install.sh | bash" >&2
    exit 1
  fi

  if [[ -d "$SCRIBE_INSTALL_DIR/.git" ]]; then
    echo "Scribe is already at $SCRIBE_INSTALL_DIR — pulling latest changes…"
    if ! git -C "$SCRIBE_INSTALL_DIR" pull --ff-only; then
      echo >&2
      echo "Couldn't pull. If you've edited files in $SCRIBE_INSTALL_DIR," >&2
      echo "commit/stash them — or remove the folder and try again:" >&2
      echo "  rm -rf $SCRIBE_INSTALL_DIR" >&2
      exit 1
    fi
  elif [[ -e "$SCRIBE_INSTALL_DIR" ]]; then
    echo "$SCRIBE_INSTALL_DIR exists but isn't a git checkout." >&2
    echo "Remove it and try again:  rm -rf $SCRIBE_INSTALL_DIR" >&2
    exit 1
  else
    echo "Cloning Scribe to $SCRIBE_INSTALL_DIR…"
    mkdir -p "$(dirname "$SCRIBE_INSTALL_DIR")"
    git clone --depth=1 "$SCRIBE_REPO_URL" "$SCRIBE_INSTALL_DIR"
  fi

  exec bash "$SCRIBE_INSTALL_DIR/install.sh" "$@"
fi

REPO_DIR=$(cd "$(dirname "$0")" && pwd)
cd "$REPO_DIR"

# Config (.env) lives under XDG_CONFIG_HOME so it survives uninstall/reinstall
# of the data dir at ~/.local/share/scribe.
ENV_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/scribe/.env"
ENV_DIR=$(dirname "$ENV_FILE")

# ───── colors ───────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; B=$'\033[1m'
  C=$'\033[36m'; M=$'\033[35m'; D=$'\033[2m'; N=$'\033[0m'
else
  R=; G=; Y=; B=; C=; M=; D=; N=
fi

# ───── status helpers ───────────────────────────────────────────────────
ok()   { printf "  %s✅%s %s\n" "$G" "$N" "$*"; }
err()  { printf "  %s❌%s %s\n" "$R" "$N" "$*" >&2; }
warn() { printf "  %s⚠️%s  %s\n" "$Y" "$N" "$*"; }
info() { printf "  %s💡%s %s\n" "$C" "$N" "$*"; }
dry()  { printf "  %s[dry-run]%s would: %s\n" "$Y" "$N" "$*"; }

# ───── spinner ──────────────────────────────────────────────────────────
spinner() {
  local msg=$1; shift
  local frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
  local logfile status=0
  logfile=$(mktemp)

  if [[ ! -t 1 ]]; then
    "$@" >"$logfile" 2>&1 || status=$?
    (( status != 0 )) && cat "$logfile"
    rm -f "$logfile"
    return $status
  fi

  "$@" >"$logfile" 2>&1 &
  local pid=$! i=0
  printf '\033[?25l'
  while kill -0 "$pid" 2>/dev/null; do
    printf "\r  %s%s%s %s" "$C" "${frames[i]}" "$N" "$msg"
    i=$(( (i + 1) % ${#frames[@]} ))
    sleep 0.08
  done
  printf '\033[?25h'
  wait "$pid" || status=$?
  printf "\r\033[K"
  if (( status != 0 )); then
    err "$msg — failed"
    cat "$logfile"
  fi
  rm -f "$logfile"
  return $status
}

# ───── banner ───────────────────────────────────────────────────────────
print_logo() {
  printf '%s' "$M"
  cat <<'BANNER'

   ███████╗ ██████╗██████╗ ██╗██████╗ ███████╗
   ██╔════╝██╔════╝██╔══██╗██║██╔══██╗██╔════╝
   ███████╗██║     ██████╔╝██║██████╔╝█████╗
   ╚════██║██║     ██╔══██╗██║██╔══██╗██╔══╝
   ███████║╚██████╗██║  ██║██║██████╔╝███████╗
   ╚══════╝ ╚═════╝╚═╝  ╚═╝╚═╝╚═════╝ ╚══════╝
BANNER
  printf '%s' "$N"
  printf "   %sLinux push-to-talk dictation, powered by Groq Whisper%s\n" "$D" "$N"
}

# ───── arrow-key menu ──────────────────────────────────────────────────
# Usage:  selected_index=$(select_menu "Prompt:" "Option 1" "Option 2" ...)
# Renders options to /dev/tty, navigates with ↑/↓, selects with Enter.
# On selection, collapses the menu into a single "  Prompt: Choice" line.
select_menu() {
  local prompt=$1; shift
  local options=("$@")
  local count=${#options[@]}
  local selected=0

  # Fall back to numbered input only when there's no usable terminal at all.
  # NOTE: don't test [[ -t 1 ]] — when called via $(select_menu ...), stdout is
  # a pipe even in an interactive shell, which masked the arrow-key path.
  if [[ ! -e /dev/tty ]]; then
    printf "  %s\n" "$prompt" >&2
    for i in "${!options[@]}"; do
      printf "    %d) %s\n" "$((i + 1))" "${options[i]}" >&2
    done
    printf "  Choice [1]: " >&2
    read -r choice
    echo "$(( ${choice:-1} - 1 ))"
    return
  fi

  local old_stty
  old_stty=$(stty -g </dev/tty)
  # Non-canonical, no echo: deliver each keystroke to read() immediately.
  stty -icanon -echo min 1 time 0 </dev/tty
  printf '\033[?25l' >/dev/tty
  trap '
    stty "$old_stty" </dev/tty 2>/dev/null
    printf "\033[?25h" >/dev/tty 2>/dev/null
    exit 130
  ' INT

  draw() {
    {
      printf "  %s%s%s\n" "$C" "$prompt" "$N"
      for i in "${!options[@]}"; do
        if (( i == selected )); then
          printf "    %s❯%s %s%s%s\n" "$M" "$N" "$B$C" "${options[i]}" "$N"
        else
          printf "      %s%s%s\n" "$D" "${options[i]}" "$N"
        fi
      done
      printf "  %s↑↓ navigate, enter to select%s\n" "$D" "$N"
    } >/dev/tty
  }

  redraw() {
    printf "\033[%dA" $((count + 2)) >/dev/tty
    draw
  }

  draw
  while true; do
    IFS= read -rsn1 key </dev/tty
    if [[ $key == $'\x1b' ]]; then
      IFS= read -rsn2 -t 0.05 rest </dev/tty || rest=""
      case $rest in
        '[A') (( selected = (selected - 1 + count) % count )); redraw ;;
        '[B') (( selected = (selected + 1) % count )); redraw ;;
      esac
    elif [[ -z $key ]]; then
      # Enter: collapse menu to a one-line summary.
      {
        printf "\033[%dA" $((count + 2))
        printf '\033[J'
        printf "  %s%s%s %s%s%s\n" "$C" "$prompt" "$N" "$B" "${options[selected]}" "$N"
        printf '\033[?25h'
      } >/dev/tty
      stty "$old_stty" </dev/tty
      trap - INT
      echo "$selected"
      return
    fi
  done
}

# ───── arrow-key Yes/No selector ────────────────────────────────────────
# Usage:  idx=$(select_yes_no "Prompt?" [default_index])
# default_index: 0=Yes, 1=No. Echoes the selected index on stdout.
select_yes_no() {
  local prompt=$1
  local selected=${2:-0}
  local options=("Yes" "No")
  local count=2
  local key rest old_stty i

  if [[ ! -e /dev/tty ]]; then
    # Fallback: traditional [Y/n] / [y/N] prompt
    local hint="Y/n"
    (( selected == 1 )) && hint="y/N"
    printf "  %s%s%s %s[%s]%s: " "$C" "$prompt" "$N" "$D" "$hint" "$N" >&2
    read -r ans
    if [[ -z $ans ]]; then
      echo "$selected"
    elif [[ $ans =~ ^[Yy] ]]; then
      echo 0
    else
      echo 1
    fi
    return
  fi

  old_stty=$(stty -g </dev/tty)
  stty -icanon -echo min 1 time 0 </dev/tty
  printf '\033[?25l' >/dev/tty
  trap '
    stty "$old_stty" </dev/tty 2>/dev/null
    printf "\033[?25h" >/dev/tty 2>/dev/null
    exit 130
  ' INT

  draw_yn() {
    {
      printf "  %s%s%s\n" "$C" "$prompt" "$N"
      printf "    "
      for i in "${!options[@]}"; do
        if (( i == selected )); then
          printf "%s❯%s %s%s%s    " "$M" "$N" "$B$C" "${options[i]}" "$N"
        else
          printf "  %s%s%s    " "$D" "${options[i]}" "$N"
        fi
      done
      printf "\n"
      printf "  %s←→ navigate, enter to select%s\n" "$D" "$N"
    } >/dev/tty
  }

  redraw_yn() {
    printf "\033[3A" >/dev/tty
    draw_yn
  }

  draw_yn
  while true; do
    IFS= read -rsn1 key </dev/tty
    if [[ $key == $'\x1b' ]]; then
      IFS= read -rsn2 -t 0.05 rest </dev/tty || rest=""
      case $rest in
        '[C') (( selected = (selected + 1) % count )); redraw_yn ;;
        '[D') (( selected = (selected - 1 + count) % count )); redraw_yn ;;
      esac
    elif [[ -z $key ]]; then
      {
        printf "\033[3A"
        printf '\033[J'
        printf "  %s%s%s %s%s%s\n" "$C" "$prompt" "$N" "$B" "${options[selected]}" "$N"
      } >/dev/tty
      stty "$old_stty" </dev/tty
      printf "\033[?25h" >/dev/tty
      trap - INT
      echo "$selected"
      return
    fi
  done
}

# ───── masked secret input ──────────────────────────────────────────────
# Usage:  value=$(read_secret "  Prompt: ")
# Echoes '*' per keystroke, handles backspace, ignores arrow keys, etc.
read_secret() {
  local prompt=$1
  local value="" key old_stty

  if [[ ! -e /dev/tty ]]; then
    # Fallback: bash's silent read (no per-char feedback)
    printf "%s" "$prompt" >&2
    read -rs value
    printf "\n" >&2
    echo "$value"
    return
  fi

  printf "%s" "$prompt" >/dev/tty
  old_stty=$(stty -g </dev/tty)
  stty -icanon -echo min 1 time 0 </dev/tty
  trap 'stty "$old_stty" </dev/tty 2>/dev/null; exit 130' INT

  while true; do
    IFS= read -rsn1 key </dev/tty
    case "$key" in
      "")
        # Enter
        printf "\n" >/dev/tty
        break
        ;;
      $'\x7f'|$'\x08')
        # Backspace / DEL
        if [[ -n $value ]]; then
          value="${value%?}"
          printf '\b \b' >/dev/tty
        fi
        ;;
      $'\x1b')
        # Eat escape sequences (arrow keys etc.) so they don't enter the value
        IFS= read -rsn2 -t 0.05 _ </dev/tty || true
        ;;
      *)
        value+="$key"
        printf '*' >/dev/tty
        ;;
    esac
  done

  stty "$old_stty" </dev/tty
  trap - INT
  echo "$value"
}

# ───── arg parsing ──────────────────────────────────────────────────────
DRY_RUN=0
DRY_FULL=0

usage() {
  local me
  me=$(basename "$0")
  print_logo
  echo
  printf "  %s▸%s  %sUsage%s\n\n" "$M" "$N" "$B" "$N"
  printf "      %s%s%s [OPTIONS]\n\n" "$C" "$me" "$N"

  printf "  %s▸%s  %sOptions%s\n\n" "$M" "$N" "$B" "$N"
  printf "      %s--dry-run%s%s[=MODE]%s   Preview without making changes. MODE is:\n" \
    "$C$B" "$N" "$D" "$N"
  printf "                           %slight%s %s(default)%s — same flow as a real install\n" \
    "$Y$B" "$N" "$D" "$N"
  printf "                           %sfull%s            — walks every prompt as if fresh\n" \
    "$Y$B" "$N"
  printf "      %s-h, --help%s         Show this help and exit.\n\n" "$C$B" "$N"

  printf "  %s▸%s  %sExamples%s\n\n" "$M" "$N" "$B" "$N"
  printf "      %s%s%s                    %s# real install%s\n" \
    "$C" "$me" "$N" "$D" "$N"
  printf "      %s%s --dry-run%s          %s# preview honestly (skip what already exists)%s\n" \
    "$C" "$me" "$N" "$D" "$N"
  printf "      %s%s --dry-run=full%s     %s# preview every prompt as a first-time user%s\n\n" \
    "$C" "$me" "$N" "$D" "$N"
}

set_dry_mode() {
  DRY_RUN=1
  case "${1:-light}" in
    light) DRY_FULL=0 ;;
    full)  DRY_FULL=1 ;;
    *) err "Unknown dry-run mode: $1 (use 'light' or 'full')"; exit 1 ;;
  esac
}

while (( $# > 0 )); do
  case "$1" in
    --dry-run)   set_dry_mode light ;;
    --dry-run=*) set_dry_mode "${1#--dry-run=}" ;;
    -h|--help)   usage; exit 0 ;;
    *) err "Unknown option: $1"; echo; usage >&2; exit 1 ;;
  esac
  shift
done

# ───── existence helpers (honor DRY_FULL) ───────────────────────────────
env_exists()  { (( DRY_FULL )) && return 1; [[ -f "$ENV_FILE" ]]; }
venv_exists() { (( DRY_FULL )) && return 1; [[ -d .venv ]]; }
file_exists() { (( DRY_FULL )) && return 1; [[ -f "$1" ]]; }

# ───── screen / section ─────────────────────────────────────────────────
clear_screen() {
  [[ -t 1 ]] || return 0
  printf '\033[2J\033[H'
}

print_banner() {
  print_logo
  if (( DRY_RUN )); then
    local label="DRY RUN"
    (( DRY_FULL )) && label="DRY RUN — full"
    printf "   %s%s%s  no files written, no packages installed, no services started.\n" "$Y$B" "$label" "$N"
  fi
}

FIRST_SECTION=1
hdr() {
  if (( FIRST_SECTION )); then
    FIRST_SECTION=0
  else
    # Brief pause so the previous section's final status is visible.
    [[ -t 1 ]] && sleep 0.8
  fi
  clear_screen
  print_banner
  echo
  printf "  %s▸%s  %s%s%s\n\n" "$M" "$N" "$B" "$*" "$N"
}

# ───── 1. dependency checks ─────────────────────────────────────────────
hdr "Checking dependencies"

if [[ "$(uname -s)" != "Linux" ]]; then
  err "Scribe is Linux-only (you're on $(uname -s))."
  exit 1
fi
ok "Linux"

if ! command -v python3 >/dev/null; then
  err "python3 not found. Install Python 3.11 or newer."
  exit 1
fi
PY_VERSION=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PY_MAJOR=${PY_VERSION%.*}
PY_MINOR=${PY_VERSION#*.}
if (( PY_MAJOR < 3 )) || { (( PY_MAJOR == 3 )) && (( PY_MINOR < 11 )); }; then
  err "Python $PY_VERSION found, but 3.11+ is required."
  exit 1
fi
ok "Python $PY_VERSION"

MISSING=()
command -v wl-copy >/dev/null || MISSING+=("wl-clipboard")
command -v ydotool >/dev/null || MISSING+=("ydotool")

if (( ${#MISSING[@]} > 0 )); then
  err "Missing system packages: ${MISSING[*]}"
  echo
  info "Install them with one of:"
  printf "      %sFedora:%s        sudo dnf install %s\n" "$C" "$N" "${MISSING[*]}"
  printf "      %sDebian/Ubuntu:%s sudo apt install %s\n" "$C" "$N" "${MISSING[*]}"
  printf "      %sArch:%s          sudo pacman -S %s\n" "$C" "$N" "${MISSING[*]}"
  exit 1
fi
ok "wl-clipboard, ydotool"

if id -nG | tr ' ' '\n' | grep -qx input; then
  ok "Member of 'input' group"
elif getent group input | grep -qw "$USER"; then
  warn "You're in the 'input' group but the current shell doesn't see it yet."
  info "Log out and back in (or reboot), then re-run this script."
  exit 1
else
  err "User '$USER' is not in the 'input' group."
  info "Run:  sudo usermod -aG input \$USER"
  info "Then log out and back in, and re-run this script."
  exit 1
fi

if systemctl is-active ydotool >/dev/null 2>&1; then
  ok "ydotool.service running"
else
  err "ydotool.service is not running."
  echo
  info "Start it:  sudo systemctl enable --now ydotool"
  echo
  info "If it fails with 'exit status 2' (typical on Fedora), apply this socket override:"
  cat <<'YDOFIX'

    sudo mkdir -p /etc/systemd/system/ydotool.service.d
    sudo tee /etc/systemd/system/ydotool.service.d/override.conf >/dev/null <<EOF
    [Service]
    ExecStart=
    ExecStart=/usr/bin/ydotoold --socket-path=/tmp/.ydotool_socket --socket-own=$(id -u):$(id -g)
    EOF
    sudo systemctl daemon-reload && sudo systemctl restart ydotool

YDOFIX
  exit 1
fi

# ───── 2. configuration ─────────────────────────────────────────────────
hdr "Configuration"

# One-time migration: earlier versions kept .env inside the repo dir. If we
# find it there but not at the new XDG location, move it.
OLD_ENV="$REPO_DIR/.env"
if [[ -f "$OLD_ENV" && ! -f "$ENV_FILE" ]]; then
  if (( DRY_RUN )); then
    dry "move $OLD_ENV → $ENV_FILE"
  else
    mkdir -p "$ENV_DIR"
    mv "$OLD_ENV" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    ok "Migrated .env from $OLD_ENV to $ENV_FILE"
  fi
fi

if env_exists; then
  ok ".env already exists at $ENV_FILE — keeping it as-is."
  info "Edit it manually to change settings; see .env.example for the full list."
else
  GROQ_KEY=$(read_secret "  ${C}Groq API key${N} (https://console.groq.com): ")
  if [[ -z "$GROQ_KEY" ]]; then
    err "Groq API key is required."
    exit 1
  fi

  PTT_LABELS=(
    "Right Ctrl"
    "Left Ctrl"
    "Right Alt"
    "Right Shift"
    "Caps Lock"
    "F12"
  )
  PTT_VALUES=(KEY_RIGHTCTRL KEY_LEFTCTRL KEY_RIGHTALT KEY_RIGHTSHIFT KEY_CAPSLOCK KEY_F12)
  PTT_INDEX=$(select_menu "Push-to-talk key:" "${PTT_LABELS[@]}")
  HOTKEY="${PTT_VALUES[$PTT_INDEX]}"

  PASTE_LABELS=(
    "Ctrl+Shift+V (terminals, Claude Code, Electron apps)"
    "Ctrl+V       (GTK/GNOME apps, browsers)"
    "char-by-char (universal but slow on long transcripts)"
  )
  PASTE_VALUES=(shortcut ctrl_v type)
  PASTE_INDEX=$(select_menu "Paste mode:" "${PASTE_LABELS[@]}")
  PASTE_MODE="${PASTE_VALUES[$PASTE_INDEX]}"

  echo
  printf "  %sCustom vocabulary%s — words that often get transcribed wrong\n" "$C" "$N"
  printf "  (project names, libraries, coworker names). Comma-separated; press\n"
  printf "  enter to skip.\n"
  # Read from /dev/tty in case stdin is a piped install (curl ... | bash).
  if [[ -e /dev/tty ]]; then
    printf "  %s>%s " "$C" "$N" >/dev/tty
    IFS= read -r PROMPT_VOCAB </dev/tty || PROMPT_VOCAB=""
  else
    printf "  %s>%s " "$C" "$N"
    IFS= read -r PROMPT_VOCAB || PROMPT_VOCAB=""
  fi

  echo
  if (( DRY_RUN )); then
    dry "write $ENV_FILE with:"
    printf "      GROQ_API_KEY=********\n"
    printf "      SCRIBE_HOTKEY=%s\n" "$HOTKEY"
    printf "      SCRIBE_PASTE_MODE=%s\n" "$PASTE_MODE"
    [[ -n "$PROMPT_VOCAB" ]] && printf "      SCRIBE_PROMPT=%s\n" "$PROMPT_VOCAB"
    dry "chmod 600 $ENV_FILE"
  else
    mkdir -p "$ENV_DIR"
    {
      echo "# Generated by install.sh on $(date +%Y-%m-%d)"
      echo "GROQ_API_KEY=$GROQ_KEY"
      echo "SCRIBE_HOTKEY=$HOTKEY"
      echo "SCRIBE_PASTE_MODE=$PASTE_MODE"
      [[ -n "$PROMPT_VOCAB" ]] && echo "SCRIBE_PROMPT=$PROMPT_VOCAB"
    } > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    ok "Wrote $ENV_FILE"
  fi
fi

# ───── 3. Python deps ───────────────────────────────────────────────────
hdr "Installing Python dependencies"

if (( DRY_RUN )); then
  venv_exists || dry "create virtualenv at .venv (python3 -m venv .venv)"
  venv_exists && ok "Virtualenv already exists"
  dry "source .venv/bin/activate"
  dry "pip install --upgrade pip"
  dry "pip install -r requirements.txt"
else
  if [[ ! -d .venv ]]; then
    spinner "Creating virtualenv at .venv…" python3 -m venv .venv
    ok "Virtualenv created"
  else
    ok "Virtualenv already exists"
  fi

  # shellcheck source=/dev/null
  source .venv/bin/activate

  spinner "Upgrading pip…" pip install --upgrade pip
  ok "pip upgraded"

  if ! spinner "Installing dependencies (may take a minute if wheels need to build)…" pip install -r requirements.txt; then
    echo
    info "If you see build errors for evdev or sounddevice, install the native deps:"
    printf "      %sFedora:%s        sudo dnf install libevdev-devel portaudio-devel python3-devel\n" "$C" "$N"
    printf "      %sDebian/Ubuntu:%s sudo apt install libevdev-dev portaudio19-dev python3-dev\n" "$C" "$N"
    exit 1
  fi
  ok "Python dependencies installed"
fi

# ───── 4. scribe CLI ────────────────────────────────────────────────────
hdr "Installing scribe command"

BIN_DIR="$HOME/.local/bin"
BIN_FILE="$BIN_DIR/scribe"
CLI_SRC="$REPO_DIR/bin/scribe"

if [[ ! -f "$CLI_SRC" ]]; then
  err "Missing $CLI_SRC — repo is incomplete."
  exit 1
fi

if (( DRY_RUN )); then
  [[ -d "$BIN_DIR" ]] || dry "mkdir -p $BIN_DIR"
  dry "install scribe CLI at $BIN_FILE (SCRIBE_DIR=$REPO_DIR)"
else
  mkdir -p "$BIN_DIR"
  # Bash parameter expansion handles any character cleanly, unlike sed.
  CLI_TEMPLATE=$(<"$CLI_SRC")
  printf '%s\n' "${CLI_TEMPLATE//__SCRIBE_DIR__/$REPO_DIR}" > "$BIN_FILE"
  chmod +x "$BIN_FILE"
  ok "Installed $BIN_FILE"
fi

if ! printf '%s' ":$PATH:" | grep -q ":$BIN_DIR:"; then
  warn "$BIN_DIR is not on your PATH."
  info "Add this to your shell rc (~/.bashrc, ~/.zshrc):"
  printf "      %sexport PATH=\"\$HOME/.local/bin:\$PATH\"%s\n" "$D" "$N"
fi

# ───── 5. autostart ─────────────────────────────────────────────────────
hdr "Autostart"

UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/scribe.service"

# If a unit file already exists, the user opted into autostart on a previous
# run — don't ask again, just keep it set up (and silently regenerate the
# file in case install paths changed).
if file_exists "$UNIT_FILE"; then
  AUTOSTART_IDX=0
  ok "Autostart already set up — refreshing."
else
  AUTOSTART_IDX=$(select_yes_no "Install scribe as a systemd user service (autostart on login)?" 0)
fi

if (( AUTOSTART_IDX == 0 )); then
  if (( DRY_RUN )); then
    [[ -d "$UNIT_DIR" ]] || dry "mkdir -p $UNIT_DIR"
  else
    mkdir -p "$UNIT_DIR"
  fi

  if (( DRY_RUN )); then
    dry "write $UNIT_FILE with WorkingDirectory=$REPO_DIR"
  else
    cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Scribe push-to-talk dictation
After=graphical-session.target sound.target

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
ExecStart=$REPO_DIR/.venv/bin/python $REPO_DIR/scribe.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
    ok "Wrote $UNIT_FILE"
  fi

  if (( DRY_RUN )); then
    dry "systemctl --user daemon-reload"
    dry "systemctl --user enable scribe.service"
    dry "systemctl --user restart scribe.service"
  else
    spinner "Reloading systemd…" systemctl --user daemon-reload
    spinner "Enabling scribe.service…" systemctl --user enable scribe.service
    # restart works whether it's already running or not, so picks up any
    # changes from this install run.
    spinner "Starting scribe.service…" systemctl --user restart scribe.service
    ok "scribe.service is running"
  fi
  AUTOSTARTED=1
else
  info "Skipped autostart. Start scribe manually with:"
  printf "      %ssource .venv/bin/activate && python scribe.py%s\n" "$D" "$N"
  AUTOSTARTED=0
fi

# ───── 6. useful commands (only if autostart was set up) ────────────────
if (( AUTOSTARTED )); then
  hdr "Useful commands"
  printf "      Start:         %sscribe start%s\n" "$D" "$N"
  printf "      Stop:          %sscribe stop%s\n" "$D" "$N"
  printf "      Restart:       %sscribe restart%s\n" "$D" "$N"
  printf "      Check status:  %sscribe status%s\n" "$D" "$N"
  printf "      Test:          %sscribe test%s\n" "$D" "$N"
  printf "      List vocab:    %sscribe list%s\n" "$D" "$N"
  printf "      Add vocab:     %sscribe add \"Claude Code\" Anthropic OAuth Kubernetes%s\n" "$D" "$N"
  printf "      Update:        %sscribe update%s\n" "$D" "$N"
  printf "      Watch logs:    %sjournalctl --user -u scribe -f%s\n" "$D" "$N"
  printf "      Uninstall:     %sscribe uninstall%s\n" "$D" "$N"
fi

# ───── done (no clear; this stays on screen) ────────────────────────────
echo
[[ -t 1 ]] && sleep 0.6
printf "  %s%sScribe is ready!%s\n\n" "$B" "$G" "$N"
printf "  Hold your push-to-talk key, speak, release.\n"
printf "  The transcript pastes at your cursor.\n\n"
printf "  %sWant a different hotkey? Edit %sSCRIBE_HOTKEY%s in %s%s%s.\n\n" "$D" "$C" "$D" "$C" "$ENV_FILE" "$N"
