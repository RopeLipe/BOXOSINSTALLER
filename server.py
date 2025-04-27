from flask import Flask, jsonify, send_from_directory, request
# Attempt to import archinstall (in Windows stub-mode this will be skipped)
try:
    import archinstall
except ImportError:
    archinstall = None
# Only import DISK_DEVICES if archinstall is available
if archinstall:
    try:
        from archinstall.disk.device_handler import devices as DISK_DEVICES
    except ImportError:
        try:
            # older archinstall versions may export a global 'devices'
            from archinstall.lib.disk.device_handler import devices as DISK_DEVICES
        except ImportError:
            # fallback: instantiate DeviceHandler
            from archinstall.lib.disk.device_handler import DeviceHandler
            DISK_DEVICES = DeviceHandler().devices
else:
    # stub for Windows/development mode
    DISK_DEVICES = []
import psutil
import os
from threading import Thread
import json
import subprocess
import socket
import re
from collections import deque
import time
import logging
import secrets # Import secrets for random password generation
import string # Import string for character sets
import pty # Import pty for pseudo-terminal
import select # Needed for checking pty readability
import threading # For the reader thread

# --- Archinstall Library Imports ---
try:
    from archinstall.disk.configurator import suggest_single_disk_layout
    from archinstall.disk.device_handler import device_handler
    from archinstall.disk.types import FilesystemType
    ARCHINSTALL_LIB_AVAILABLE = True
except ImportError as e:
    print(f"WARNING: Failed to import archinstall library components: {e}. Disk layout generation will be skipped.")
    suggest_single_disk_layout = None
    device_handler = None
    FilesystemType = None
    ARCHINSTALL_LIB_AVAILABLE = False

logging.basicConfig(level=logging.DEBUG)
print("DEBUG: starting server.py in debug mode")
app = Flask(__name__, static_folder='.', static_url_path='')

# --- Global state for installation process ---
# Store PID and thread to potentially manage later
install_process_info = {'pid': None, 'thread': None}
progress_file_path = '/tmp/archinstall_progress.json'
stderr_log_path = '/tmp/archinstall_stderr.log'
# --------------------------------------------

# In-memory buffer to store JSON progress messages - NOT USED with pty approach
# progress_buffer = deque(maxlen=200) # Keep commented or remove

@app.route('/')
def index():
    return send_from_directory('.', 'Install.html')

@app.route('/api/disks')
def api_disks():
    # Linux: original lsblk-based implementation
    result = []
    # include MODEL so we can show the vendor/model string (e.g. VBOX HARDDISK)
    ls = subprocess.check_output(
        ['lsblk', '--bytes', '--json', '-o', 'NAME,PATH,SIZE,TYPE,MODEL'],
        universal_newlines=True
    )
    data = json.loads(ls)
    for blk in data.get('blockdevices', []):
        # only top-level disks
        if blk.get('type') != 'disk':
            continue
        path = blk.get('path')
        total = int(blk.get('size', 0))
        # sum partition sizes
        used = 0
        for part in blk.get('children', []):
            if part.get('type') == 'part':
                used += int(part.get('size', 0))
        free = total - used
        # include the lsblk 'name' field alongside path/model
        result.append({
            'name': blk.get('name'),
            'model': blk.get('model', ''),   # actual hardware model string
            'path': path,
            'total_bytes': total,
            'free_bytes': free
        })
    return jsonify(result)

@app.route('/api/network/status')
def api_network_status():
    # always gather interface stats and addresses before branching
    stats = psutil.net_if_stats()
    addrs = psutil.net_if_addrs()
    # Linux: check for active Ethernet
    for iface, stat in stats.items():
        if iface == 'lo':
            continue
        if (iface.startswith('en') or iface.startswith('eth')) and stat.isup:
            # has IPv4 assigned?
            if any(a.family == socket.AF_INET for a in addrs.get(iface, [])):
                return jsonify({'connection_type': 'ethernet', 'interface': iface})
    # fallback to wireless
    for iface, stat in stats.items():
        if iface.startswith('w') and stat.isup:
            try:
                scan = subprocess.check_output(['iwlist', iface, 'scan'], universal_newlines=True, stderr=subprocess.DEVNULL)
                ssids = re.findall(r'ESSID:"([^\"]+)"', scan)
            except Exception:
                ssids = []
            return jsonify({'connection_type': 'wifi', 'interface': iface, 'networks': ssids})
    # no network
    return jsonify({'connection_type': 'none', 'interface': None})

@app.route('/api/network/config', methods=['POST'])
def api_net_config():
    data = request.json
    iface = data.get('interface')
    method = data.get('method')
    if method == 'dhcp':
        os.system(f"dhclient {iface}")
    elif method == 'static':
        cfg = data.get('config', {})
        ip = cfg.get('address')
        mask = cfg.get('netmask')
        gw = cfg.get('gateway')
        os.system(f"ip addr flush dev {iface}")
        os.system(f"ip addr add {ip}/{mask} dev {iface}")
        os.system(f"ip route add default via {gw}")
    return jsonify({'status':'ok'})

# --- PTY Reader Thread Function ---
def read_pty_output(master_fd, output_path, stderr_path):
    """Reads from the master pty FD and writes to output files."""
    print(f"DEBUG: Starting PTY reader thread for fd {master_fd}")
    try:
        # Open output files within the thread
        # Use line buffering (buffering=1) for text mode
        progress_file = open(output_path, 'w', buffering=1, encoding='utf-8')
        # stderr_file = open(stderr_path, 'w', buffering=1, encoding='utf-8') # Not directly captured via pty master

        while True:
            # Wait until the master FD is readable, with a timeout (e.g., 1 second)
            # This prevents spinning busy-wait and allows graceful exit check
            r, _, _ = select.select([master_fd], [], [], 1.0)
            if master_fd in r:
                try:
                    # Read available data (up to 1024 bytes)
                    data = os.read(master_fd, 1024)
                except OSError:
                    # EIO typically means the slave PTY has been closed
                    print("DEBUG: PTY master OSError (likely closed), exiting reader thread.")
                    break

                if not data:
                    # EOF - process terminated
                    print("DEBUG: PTY master EOF, exiting reader thread.")
                    break

                # Decode assuming UTF-8, replace errors
                text_output = data.decode('utf-8', errors='replace')
                print(f"PTY RAW: {text_output.strip()}") # Log raw output for debugging
                progress_file.write(text_output)
                # Note: stderr is merged with stdout via PTY, so we don't write to stderr_file here.
                # If separate stderr is needed, Popen needs separate pipes *before* pty.

            # Add a check here if we need to forcefully stop the thread externally
            # if should_stop_reading(): break

    except Exception as e:
        print(f"ERROR: Exception in PTY reader thread: {e}")
    finally:
        print(f"DEBUG: Closing files and PTY master fd {master_fd} in reader thread.")
        if 'progress_file' in locals() and not progress_file.closed:
            progress_file.close()
        # if 'stderr_file' in locals() and not stderr_file.closed:
        #     stderr_file.close() # Close if we were using it
        if master_fd:
            os.close(master_fd)

# ---------------------------------

@app.route('/api/install', methods=['POST'])
def api_install():
    """Receive installation config, write JSON files, and start archinstall guided script in a PTY."""
    global install_process_info # Allow modification of global state

    # --- Stop existing installation if running ---
    if install_process_info['pid'] is not None:
        try:
            # Check if the process actually exists
            os.kill(install_process_info['pid'], 0)
            print(f"WARN: Killing previous installation process (PID: {install_process_info['pid']})")
            os.kill(install_process_info['pid'], 9) # SIGKILL
            if install_process_info['thread'] and install_process_info['thread'].is_alive():
                 # We might need a way to signal the thread to stop cleanly if possible
                 print("WARN: Previous reader thread might still be running.")
        except OSError:
            print(f"DEBUG: Previous install process (PID: {install_process_info['pid']}) already finished.")
        except Exception as kill_err:
            print(f"ERROR: Failed to kill previous process {install_process_info['pid']}: {kill_err}")
        finally:
            install_process_info['pid'] = None
            install_process_info['thread'] = None
    # --------------------------------------------

    raw_body = request.json
    print(f"DEBUG: raw request body: {raw_body}")
    try:
        data = request.get_json(force=True)
    except Exception as e:
        print(f"ERROR: failed to parse JSON: {e}")
        data = {}
    print(f"DEBUG: parsed JSON data: {data}")
    # map language codes to full language names for Archinstall
    lang_map = {
        "en": "English",
        "fr": "Français",
        "es": "Español",
        "de": "Deutsch",
        "it": "Italiano",
        "pt": "Português",
        "ja": "Japanese",  # Added Japanese
        "ko": "Korean",    # Added Korean
        "zh-CN": "Chinese (Simplified)", # Added Chinese (Simplified)
        "ru": "Russian",   # Added Russian
        "ar": "Arabic",    # Added Arabic
        "tr": "Turkish",   # Added Turkish
        "nl": "Dutch",     # Added Dutch
        "pl": "Polish",    # Added Polish
        "vi": "Vietnamese",# Added Vietnamese
        "hi": "Hindi",     # Added Hindi
        "bn": "Bengali",   # Added Bengali
        "th": "Thai",      # Added Thai
        "ms": "Malay"      # Added Malay
        # add more mappings as needed
    }
    lang_code = data.get("archinstall-language")
    lang_name = lang_map.get(lang_code, lang_code)

    # --- Determine Disk Configuration ---
    disk_cfg_request = data.get("disk_config")
    filesystem_str = data.get("filesystem", "ext4")
    disk_cfg = None # Final config to use

    if isinstance(disk_cfg_request, dict) and disk_cfg_request.get('config_type') == 'default_layout':
        mods = disk_cfg_request.get('device_modifications', [])
        if mods and isinstance(mods, list) and len(mods) > 0:
            target_device_path = mods[0].get('device')
            wipe_disk = mods[0].get('wipe', True)

            if target_device_path:
                print(f"DEBUG: Calculating explicit layout for {target_device_path}")
                try:
                    # Get total disk size in bytes using blockdev
                    cmd_size = ["blockdev", "--getsize64", target_device_path]
                    total_disk_bytes_str = subprocess.check_output(cmd_size, universal_newlines=True).strip()
                    total_disk_bytes = int(total_disk_bytes_str)
                    print(f"DEBUG: Total disk size for {target_device_path}: {total_disk_bytes} bytes")

                    # Define /boot partition (1 GiB)
                    boot_size_gib = 1
                    boot_size_bytes = boot_size_gib * 1024 * 1024 * 1024
                    boot_start_mib = 1
                    boot_start_bytes = boot_start_mib * 1024 * 1024

                    # Check if boot partition fits
                    if boot_start_bytes + boot_size_bytes > total_disk_bytes:
                         raise ValueError(f"Boot partition ({boot_size_gib} GiB) is too large for disk ({total_disk_bytes / (1024**3):.2f} GiB).")


                    boot_part = {
                        "status": "create", "type": "primary",
                        "dev_path": None,
                        "obj_id": 0,
                        "start": {"unit": "MiB", "value": boot_start_mib, "sector_size": {"unit": "B", "value": 512}},
                        "size": {"unit": "GiB", "value": boot_size_gib, "sector_size": {"unit": "B", "value": 512}},
                        "fs_type": "fat32",
                        "mountpoint": "/boot",
                        "flags": ["Boot"],
                        "mount_options": [],
                        "btrfs": []
                    }

                    # Define / (root) partition (rest of the disk)
                    # Start immediately after boot partition
                    root_start_bytes_calc = boot_start_bytes + boot_size_bytes
                    root_size_bytes_calc = total_disk_bytes - root_start_bytes_calc

                    # Add a small buffer/rounding check - don't request more than available
                    if root_size_bytes_calc <= 0: # Check for non-positive size
                        raise ValueError(f"Calculated root partition size is non-positive ({root_size_bytes_calc} bytes). Check disk size ({total_disk_bytes / (1024**3):.2f} GiB) and boot partition size ({boot_size_gib} GiB).")
                    # Optional: Align to MiB boundary if needed, but bytes should be fine
                    # root_start_mib_calc = (root_start_bytes_calc + 1024*1024 - 1) // (1024*1024)
                    # root_size_bytes_calc = total_disk_bytes - (root_start_mib_calc * 1024 * 1024)

                    root_part = {
                        "status": "create", "type": "primary",
                        "dev_path": None,
                        "obj_id": 1,
                        "start": {"unit": "B", "value": root_start_bytes_calc, "sector_size": {"unit": "B", "value": 512}},
                        "size": {"unit": "B", "value": root_size_bytes_calc, "sector_size": {"unit": "B", "value": 512}},
                        "fs_type": filesystem_str,
                        "mountpoint": "/",
                        "flags": [],
                        "mount_options": [],
                        "btrfs": []
                    }

                    device_mod = {
                        "device": target_device_path,
                        "wipe": wipe_disk,
                        "partitions": [boot_part, root_part] # Explicitly define the two partitions
                    }
                    disk_cfg = {
                        "config_type": "default_layout", # Keep this type
                        "device_modifications": [device_mod]
                    }
                    print(f"DEBUG: Using calculated explicit layout: {json.dumps(disk_cfg, indent=2)}") # Indent 2 for brevity

                except subprocess.CalledProcessError as proc_err:
                    print(f"ERROR: Failed to get disk size for {target_device_path}: {proc_err}")
                    disk_cfg = disk_cfg_request # Fallback to original request
                except ValueError as val_err:
                    print(f"ERROR: Calculation error for partitions: {val_err}")
                    disk_cfg = disk_cfg_request # Fallback
                except Exception as calc_err:
                    print(f"ERROR: Failed calculating explicit layout: {calc_err}")
                    disk_cfg = disk_cfg_request # Fallback
            else:
                 print("WARN: No device path found in device_modifications for default layout.")
                 disk_cfg = disk_cfg_request # Use original request
        else:
             print("WARN: No device_modifications found for default layout type.")
             disk_cfg = disk_cfg_request # Use original request
    else:
        # If not requesting default layout, or request was invalid, use it as is
        print("DEBUG: Using disk_config as provided (not generating default layout).")
        disk_cfg = disk_cfg_request


    # --- Prepare Network and Profile ---
    network_cfg = {"type": "nm"}
    profile_cfg = {
        "gfx_driver": None,
        "greeter": None,
        "profile": {
            "main": data.get("profile", "Minimal"),
            "details": [],
            "custom_settings": {}
        }
    }

    # include version and config metadata
    version_val = data.get("version", getattr(archinstall, "__version__", None))
    config = {
        # Use the potentially generated (or original) disk_cfg here
        "disk_config": disk_cfg,
        # Filesystem needs to be top-level for the guided script when using default layout strategy implicitly
        # Let's keep it for clarity, even if partitions now specify their own fs_type
        "filesystem": filesystem_str,
        "config_version": version_val,
        "version": version_val,
        "additional-repositories": data.get("additional-repositories", []),
        # translation & UI (use full language name)
        "archinstall-language": lang_name,
        # audio (pipewire) - REMOVED to avoid user service errors in chroot
        # "audio_config": {"audio": data.get("audio_config", "pipewire")},
        # bootloader - Changed default to grub-install for broader compatibility
        "bootloader": data.get("bootloader", "grub"),
        # debugging
        "debug": data.get("debug", False),
        # drive to install on (auto-partition default layout)
        "harddrive": data.get("harddrive", {}),
        # locale settings
        "locale_config": {"sys_lang": lang_name, "sys_enc": data.get("sys_enc", "UTF-8"), "kb_layout": data.get("kb_layout", "us")},
        # mirrors
        "mirror_config": data.get("mirror_config", {}),
        # network: NM
        "network_config": network_cfg,
        # lookups
        "no_pkg_lookups": data.get("no_pkg_lookups", False),
        # time sync
        "ntp": data.get("ntp", True),
        # offline
        "offline": data.get("offline", False),
        # extra packages - Minimal Hyprland + Greetd + nwg-panel
        "packages": data.get("packages", []) + [
            # Audio
            'pipewire', 'pipewire-pulse', 'pipewire-alsa', 'wireplumber',
            # Core Hyprland/Wayland
            'hyprland', 'wayland', 'xorg-xwayland',
            # Login Manager
            'greetd', 
            'greetd-gtkgreet', # Use GTK greeter
            'cage', # Minimal compositor for gtkgreet
            'gtk3', # Dependency for gtkgreet
            # Panel
            'nwg-panel',
            # Terminal
            'kitty',
            # System Tray Applets
            'network-manager-applet', # Wifi/Network
            'blueman', # Provides blueman-applet for Bluetooth
            'mate-power-manager', # Battery icon/management
            # System Integration & Core Utilities
            'polkit-kde-agent', 'xdg-desktop-portal-hyprland', 'xdg-desktop-portal-gtk',
            'qt6-wayland', 'qt5-wayland', 'qt5-quickcontrols2', 'qt5-graphicaleffects',
            'wl-clipboard',
            # Fonts
            'noto-fonts', 'noto-fonts-emoji', 'ttf-jetbrains-mono-nerd', 'ttf-dejavu',
            # Base Utils
            'git', 'fontconfig', 'tzdata'
        ],
        "parallel downloads": data.get("parallel downloads", 0),
        # use guided script
        "script": "guided",
        # silent mode - Set via silent=True within the config dict itself now
        # Add services to enable
        "services": ["greetd"], # Enable Greetd login manager
        # skip flags
        "skip_ntp": data.get("skip_ntp", False),
        "skip_version_check": data.get("skip_version_check", False),
        # swap
        "swap": data.get("swap", True),
        # timezone
        "timezone": data.get("timezone", "UTC"),
        # UI toolkit
        "uikit": data.get("uikit", False),
        # profile config
        "profile_config": profile_cfg,
        # --- Add silent flag here ---
        "silent": True, # Ensure silent mode is enabled to avoid TTY issues
        # ---------------------------
        # User config (non-sensitive parts)
        "user_config": {
            "users": [
                {
                    "username": data.get("user", {}).get("username"),
                    "sudo": True
                }
            ]
        },
        # --- Post-installation Script ---
        # Add the command to run our custom configuration script after installation
        # Assuming the default mount point is /mnt/archinstall
        "post-install": [
            f"arch-chroot /mnt/archinstall /root/post_install_config.sh {data.get('user', {}).get('username')} /home/{data.get('user', {}).get('username')}"
        ]
        # ----------------------------------
        # Passwords are moved to creds file
    }

    # --- Prepare Credentials ---
    # Generate a strong random password for root if none provided
    user_provided_root_pw = data.get("root_password") # Check if user provided one
    if user_provided_root_pw:
         root_plain_password = user_provided_root_pw
         print("DEBUG: Using user-provided root password.")
    else:
        alphabet = string.ascii_letters + string.digits + string.punctuation.replace('"', '').replace("'", "").replace("\\", "") # Avoid shell-problematic chars
        root_plain_password = ''.join(secrets.choice(alphabet) for i in range(16))
        print(f"DEBUG: Generated random root password.") # Don't log the generated password

    # User password from request
    user_plain_password = data.get("user", {}).get("password")

    creds_config = {
        "!root-password": root_plain_password,
        "!users": []
    }
    # Only add user creds if username and password exist
    username = data.get("user", {}).get("username")
    if username and user_plain_password:
         creds_config["!users"].append({
             "!password": user_plain_password,
             "username": username
         })
    elif username:
         print("WARN: Username provided but no password found in request for user_config.")
    else:
         print("WARN: No username provided in user_config.")


    # debug-print the final configs for troubleshooting
    print(f"DEBUG: final archinstall main config: {json.dumps(config, indent=2)}")
    print(f"DEBUG: final archinstall creds config: {json.dumps(creds_config, indent=2)}")

    # save configs in the project root (Boxlinux folder)
    project_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(project_dir, 'archinstall_config.json')
    creds_path = os.path.join(project_dir, 'archinstall_creds.json') # Path for creds file

    try:
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=4) # Save main config with indent
        with open(creds_path, 'w') as f:
            # Ensure restrictive permissions for the credentials file
            os.chmod(creds_path, 0o600) # Read/Write for owner only
            json.dump(creds_config, f, indent=4) # Save creds config with indent

    except Exception as e:
         print(f"ERROR: Failed to write config/creds files: {e}")
         return jsonify({'status': 'error', 'message': f'Failed to write configuration files: {e}'}), 500

    # --- Update Keyring ---
    # Based on common archinstall/pacstrap issues, update keyring first
    print("DEBUG: Attempting to update archlinux-keyring...")
    try:
        keyring_update_cmd = ['pacman', '-Sy', 'archlinux-keyring', '--noconfirm']
        keyring_result = subprocess.run(keyring_update_cmd, check=True, capture_output=True, text=True)
        print(f"DEBUG: Keyring update successful:\n{keyring_result.stdout}")
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Failed to update archlinux-keyring: {e}")
        print(f"ERROR STDOUT: {e.stdout}")
        print(f"ERROR STDERR: {e.stderr}")
        # Decide if this is fatal. It might be okay if keyring is recent enough,
        # but it's often the cause of pacstrap failures. Return error for now.
        return jsonify({"status": "error", "message": f"Failed to update archlinux-keyring: {e.stderr}"}), 500
    except FileNotFoundError:
        print("ERROR: pacman command not found. Cannot update keyring.")
        # This is definitely fatal in the live environment
        return jsonify({"status": "error", "message": "pacman command not found"}), 500
    except Exception as e:
        print(f"ERROR: An unexpected error occurred during keyring update: {e}")
        return jsonify({"status": "error", "message": f"Unexpected error updating keyring: {e}"}), 500
    # ----------------------

    # --- Prepare PTY and Command ---
    command = [
        "archinstall",
        "--config", config_path,
        "--creds", creds_path,
        "--json", # Output progress as JSON
        "--log-file", "/tmp/archinstall_main.log", # Main log separate from progress
        # "--silent" is now set within the config JSON
    ]
    print(f"DEBUG: Prepared archinstall command: {' '.join(command)}")

    # Clean up old progress/stderr files before starting
    if os.path.exists(progress_file_path):
        os.remove(progress_file_path)
    if os.path.exists(stderr_log_path):
        os.remove(stderr_log_path)

    try:
        # Create a pseudo-terminal (PTY)
        master_fd, slave_fd = pty.openpty()
        print(f"DEBUG: Opened PTY pair: master={master_fd}, slave={slave_fd}")

        # Start the archinstall process, connecting its std* to the slave PTY
        # Use preexec_fn=os.setsid to run in a new session, making it easier to kill later if needed
        process = subprocess.Popen(
            command,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd, # Redirect stderr to slave PTY as well
            close_fds=True, # Close other inherited file descriptors
            universal_newlines=False, # Read/write bytes with PTY
            preexec_fn=os.setsid # Run in a new session
        )
        print(f"DEBUG: Started archinstall process with PID: {process.pid}")

        # Close the slave FD in the parent process, it's only needed by the child (archinstall)
        os.close(slave_fd)
        print(f"DEBUG: Closed slave PTY fd {slave_fd} in parent.")

        # Start the reader thread
        reader_thread = threading.Thread(
            target=read_pty_output,
            args=(master_fd, progress_file_path, stderr_log_path), # Pass master FD and paths
            daemon=True # Allows main thread to exit even if this thread is running
        )
        reader_thread.start()
        print("DEBUG: Started PTY reader thread.")

        # Store PID and thread info globally
        install_process_info['pid'] = process.pid
        install_process_info['thread'] = reader_thread

        return jsonify({"status": "started", "pid": process.pid})

    except FileNotFoundError:
        print("ERROR: archinstall command not found!")
        # Ensure PTY FDs are closed if opened
        if 'master_fd' in locals() and master_fd: os.close(master_fd)
        if 'slave_fd' in locals() and slave_fd: os.close(slave_fd)
        return jsonify({"status": "error", "message": "archinstall command not found"}), 500
    except Exception as e:
        print(f"ERROR: Failed to start archinstall process: {e}")
        # Ensure PTY FDs are closed on error
        if 'master_fd' in locals() and master_fd: os.close(master_fd)
        if 'slave_fd' in locals() and slave_fd: os.close(slave_fd)
        return jsonify({"status": "error", "message": f"Failed to start installation: {e}"}), 500


@app.route('/api/install/logs')
def api_install_logs():
    # Read RAW progress messages from the designated file
    events = []
    try:
        if os.path.exists(progress_file_path):
            with open(progress_file_path, 'r', encoding='utf-8') as f:
                 file_content = f.read()
                 # Split content into lines
                 raw_lines = file_content.strip().split('\n')
                 for line in raw_lines:
                      line = line.strip()
                      if line:
                          # Send back simple message objects
                          events.append({'message': line})

    except FileNotFoundError:
        print(f"WARN: Progress file {progress_file_path} not found yet.")
        events.append({'message': 'Installation starting, waiting for output...'})
    except Exception as e:
        print(f"ERROR: Could not read progress file {progress_file_path}: {e}")
        events.append({'message': f'Error reading progress log: {e}'})

    # Return all collected lines as simple message objects
    return jsonify(events)


@app.route('/api/install/debug_log')
def api_install_debug_log():
    """Reads the last 100 lines from the main archinstall log file for debugging."""
    log_path = '/var/log/archinstall/install.log'
    lines = []
    try:
        with open(log_path, 'r') as f:
            # Efficiently get the last N lines
            lines = deque(f, 100) 
    except FileNotFoundError:
        lines = [f"Error: Log file not found at {log_path}"]
    except Exception as e:
        lines = [f"Error reading log file {log_path}: {e}"]

    # Return the lines as a single string with newlines
    return jsonify({'log_content': "\n".join(lines)})

@app.route('/api/timezones')
def api_timezone_regions():
    """Lists available timezone regions (continents/major areas)."""
    zoneinfo_path = '/usr/share/zoneinfo'
    regions = []
    try:
        # List directories, excluding 'posix', 'right', potentially others
        excluded_dirs = {'posix', 'right', 'Etc', 'SystemV'}
        for item in os.listdir(zoneinfo_path):
            # Check if it's a directory and not a special file/link like 'iso3166.tab' or 'zone.tab'
            # Also check if it's uppercase (common convention for regions)
            if os.path.isdir(os.path.join(zoneinfo_path, item)) and item not in excluded_dirs and item[0].isupper():
                regions.append(item)
        regions.sort()
    except FileNotFoundError:
        return jsonify({"error": "Zoneinfo directory not found.", "regions": []}), 404
    except Exception as e:
        return jsonify({"error": str(e), "regions": []}), 500
    return jsonify({"regions": regions})

@app.route('/api/timezones/<region>')
def api_timezones_in_region(region):
    """Returns a list of timezones within a specific region."""
    zoneinfo_base = '/usr/share/zoneinfo'
    region_path = os.path.join(zoneinfo_base, region)
    timezones = []
    try:
        # Check if the region path exists and is a directory
        if not os.path.isdir(region_path):
            # Handle Etc separately as it's often a file or symlink
            if region == 'Etc' and os.path.exists(os.path.join(zoneinfo_base, 'Etc')):
                 # Special handling for Etc might be needed if it contains files directly
                 # For simplicity, we can list known Etc timezones or walk if it's a dir
                 # Let's try walking it anyway, it might be a directory on some systems
                 pass # Proceed to walk
            else:
                print(f"ERROR: Region directory not found: {region_path}")
                return jsonify({"error": f"Region '{region}' not found.", "timezones": []}), 404

        # Walk the directory for the given region
        for root, dirs, files in os.walk(region_path):
            for filename in files:
                 # Construct the full path to the file
                full_path = os.path.join(root, filename)
                # Get the timezone name relative to the base zoneinfo directory
                tz_name = os.path.relpath(full_path, zoneinfo_base)

                # Basic filtering: Skip common non-timezone files and hidden files
                if tz_name.startswith('.') or filename in ['posixrules', 'Factory', 'iso3166.tab', 'zone.tab', 'zone1970.tab', 'leapseconds']:
                    continue

                # Ensure we use forward slashes for the final timezone identifier
                timezones.append(tz_name.replace('\\', '/'))

        timezones.sort()
    except FileNotFoundError:
        # This might catch cases where zoneinfo_base doesn't exist
        print(f"ERROR: Base zoneinfo path not found: {zoneinfo_base}")
        return jsonify({"error": "Base zoneinfo directory not found.", "timezones": []}), 500
    except Exception as e:
        print(f"ERROR: Failed to list timezones for region {region}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e), "timezones": []}), 500

    if not timezones:
         print(f"WARN: No timezones found for region {region} at path {region_path}")
         # Return empty list, but with success code (200), as the region might just be empty

    return jsonify({"timezones": timezones})

@app.route('/api/locale/<lang>')
def api_locale(lang):
    # serve translation JSON, fallback to en.json
    locale_file = f'{lang}.json'
    path = os.path.join('locales', locale_file)
    if not os.path.exists(path):
        path = os.path.join('locales', 'en.json')
    return send_from_directory('locales', os.path.basename(path), mimetype='application/json')

@app.route('/api/reboot', methods=['POST'])
def api_reboot():
    """Initiates a system reboot."""
    print("INFO: Received request to reboot system.")
    try:
        # Ensure buffers are flushed before rebooting
        import sys
        sys.stdout.flush()
        sys.stderr.flush()
        # Execute the reboot command
        # Using os.system might be simpler in this context if running as root
        # result = subprocess.run(['reboot', 'now'], check=True, capture_output=True, text=True)
        # print(f"INFO: Reboot command executed. stdout: {result.stdout}, stderr: {result.stderr}")
        os.system('reboot') # Simpler, relies on PATH
        # If os.system returns, it likely failed or is non-blocking in some odd way
        return jsonify({"status": "rebooting"}), 200
    except Exception as e:
        print(f"ERROR: Failed to initiate reboot: {e}")
        # Log the exception traceback for more details
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    # Ensure log files exist with correct permissions if needed?
    # Or let the reader thread create them.
    app.run(host='0.0.0.0', port=8000, debug=True) # Keep debug for now 