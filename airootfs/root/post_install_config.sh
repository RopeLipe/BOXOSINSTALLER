#!/bin/bash

# Exit immediately if a command exits with a non-zero status.
set -e

# --- Arguments ---
USERNAME="${1}"
USER_HOME="${2}"

# Basic validation
if [[ -z "$USERNAME" ]] || [[ -z "$USER_HOME" ]]; then
    echo "ERROR: Username or Home Directory not provided to post-install script."
    exit 1
fi

if [[ ! -d "$USER_HOME" ]]; then
    echo "ERROR: User home directory '$USER_HOME' does not exist."
    exit 1
fi

echo "--- Starting Post-Installation Configuration for user ${USERNAME} ---"

# --- Copy Dotfiles ---
DOTFILES_SRC="/root/dotfiles/.config"
DOTFILES_DEST="${USER_HOME}/.config"

if [[ -d "$DOTFILES_SRC" ]]; then
    echo "Copying dotfiles from ${DOTFILES_SRC} to ${DOTFILES_DEST}..."
    # Ensure destination .config directory exists
    mkdir -p "${DOTFILES_DEST}"
    # Copy recursively, preserving attributes, and overwriting if necessary
    cp -aT "${DOTFILES_SRC}" "${DOTFILES_DEST}"
    echo "Dotfiles copied."

    # --- Set Ownership ---
    echo "Setting ownership for ${DOTFILES_DEST} to ${USERNAME}..."
    chown -R "${USERNAME}:${USERNAME}" "${DOTFILES_DEST}"
    echo "Ownership set."

    # --- Set Script Permissions ---
    HYPR_SCRIPTS_DIR="${DOTFILES_DEST}/hypr/scripts"
    if [[ -d "$HYPR_SCRIPTS_DIR" ]]; then
        echo "Setting execute permissions for scripts in ${HYPR_SCRIPTS_DIR}..."
        chmod +x "${HYPR_SCRIPTS_DIR}"/*
        echo "Script permissions set."
    else
        echo "WARN: Hyprland scripts directory not found at ${HYPR_SCRIPTS_DIR}, skipping permission setting."
    fi
else
    echo "WARN: Source dotfiles directory ${DOTFILES_SRC} not found. Skipping dotfile copy."
fi

# --- Enable Greetd Service ---
echo "Enabling Greetd service..."
systemctl enable greetd.service
echo "Greetd service enabled."

# --- Configure Greetd (Optional Example) ---
# Greetd typically needs a config file at /etc/greetd/config.toml
# to specify the command to run (e.g., qtgreet).
# You might need to create this file or ensure it's part of your dotfiles/airootfs setup.
# Example minimal config.toml to run qtgreet:
#
mkdir -p /etc/greetd
cat > /etc/greetd/config.toml << EOF
[terminal]
# The VT to run the greeter on.
# Can be "/dev/ttyX" or just "X" (integer)
vt = 1

# The default session, also known as the greeter.
[default_session]
# `agreety` is the default greeter. Use `gtkgreet` with `cage` instead.
# command = "agreety --cmd $SHELL"
command = "cage -s -- gtkgreet" # Use cage to run gtkgreet, -s enables VT switching

# The user to run the command as.
The user needs to be root or have permission to start Wayland/X sessions.
# user = "greeter" 
EOF
echo "NOTE: Ensure /etc/greetd/config.toml is configured to use qtgreet."


echo "--- Post-Installation Configuration Complete ---"

exit 0 