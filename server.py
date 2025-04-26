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
import crypt # Import the crypt module
import secrets # Import secrets for random password generation
import string # Import string for character sets

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

# In-memory buffer to store JSON progress messages
progress_buffer = deque(maxlen=200)

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

@app.route('/api/install', methods=['POST'])
def api_install():
    """Receive installation config, write JSON files, and start archinstall guided script in background"""
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
                    cmd = ["blockdev", "--getsize64", target_device_path]
                    total_disk_bytes_str = subprocess.check_output(cmd, universal_newlines=True).strip()
                    total_disk_bytes = int(total_disk_bytes_str)
                    print(f"DEBUG: Total disk size for {target_device_path}: {total_disk_bytes} bytes")

                    # Define /boot partition (1 GiB)
                    boot_size_gib = 1
                    boot_size_bytes = boot_size_gib * 1024 * 1024 * 1024
                    boot_start_mib = 1
                    boot_start_bytes = boot_start_mib * 1024 * 1024

                    boot_part = {
                        "status": "create", "type": "primary",
                        "dev_path": None,
                        "obj_id": 0,
                        "start": {"unit": "MiB", "value": boot_start_mib, "sector_size": {"unit": "B", "value": 512}},
                        "size": {"unit": "GiB", "value": boot_size_gib, "sector_size": {"unit": "B", "value": 512}},
                        "fs_type": "fat32",
                        "mountpoint": "/boot",
                        "flags": ["esp", "boot"],
                        "mount_options": [],
                        "btrfs": []
                    }

                    # Define / (root) partition (rest of the disk)
                    # Start immediately after boot partition
                    root_start_bytes_calc = boot_start_bytes + boot_size_bytes
                    root_size_bytes_calc = total_disk_bytes - root_start_bytes_calc

                    # Add a small buffer/rounding check - don't request more than available
                    if root_size_bytes_calc < 0:
                        raise ValueError("Calculated root partition size is negative. Check disk size and boot partition size.")
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
                    print(f"DEBUG: Using calculated explicit layout: {json.dumps(disk_cfg, indent=4)}")

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
        "bootloader": data.get("bootloader", "grub-install"),
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
        # extra packages - Add pipewire and Hyprland/GUI packages
        "packages": data.get("packages", []) + [
            'pipewire', 'pipewire-pulse', 'pipewire-alsa', 'wireplumber', # Audio
            'hyprland', 'wayland', 'xorg-xwayland', # Compositor & Wayland
            'sddm', 'qt5-wayland', 'qt5-quickcontrols2', 'qt5-graphicaleffects', # Login Manager (SDDM)
            'kitty', 'wofi', 'waybar', 'mako', 'hyprpaper', # Core Hyprland ecosystem apps
            'polkit-kde-agent', 'xdg-desktop-portal-hyprland', 'xdg-desktop-portal-gtk', # Portals & Auth
            'qt6-wayland', 'wl-clipboard', 'network-manager-applet', # Utilities & Qt6 support
            'noto-fonts', 'noto-fonts-emoji', 'ttf-jetbrains-mono-nerd', # Fonts
            'git', 'fontconfig', 'ttf-dejavu', # Version control & Font rendering support
        ],
        "parallel downloads": data.get("parallel downloads", 0),
        # use guided script
        "script": "guided",
        # silent mode
        "silent": data.get("silent", False),
        # Add services to enable
        "services": ["sddm"], # Enable SDDM graphical login manager
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
        # User config (non-sensitive parts)
        "user_config": {
            "users": [
                {
                    "username": data.get("user", {}).get("username"),
                    "sudo": True
                }
            ]
        }
        # Passwords are moved to creds file
    }

    # --- Prepare Credentials --- 
    # Generate a strong random password for root
    alphabet = string.ascii_letters + string.digits + string.punctuation
    root_plain_password = ''.join(secrets.choice(alphabet) for i in range(16))
    print(f"DEBUG: Generated random root password (plain): {root_plain_password}")
    # Hash the random root password
    root_hashed_password = crypt.crypt(root_plain_password, crypt.mksalt(crypt.METHOD_SHA512))
    print(f"DEBUG: Hashed root password.")
    
    creds_config = {
        "!root-password": root_hashed_password,
        "!users": [
            {
                "!password": data.get("user", {}).get("password"), # Plaintext user password
                "username": data.get("user", {}).get("username") # Username for matching
            }
        ]
    }

    # debug-print the final configs for troubleshooting
    print(f"DEBUG: final archinstall main config: {config}")
    print(f"DEBUG: final archinstall creds config: {creds_config}")

    # save configs in the project root (Boxlinux folder)
    project_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(project_dir, 'archinstall_config.json')
    creds_path = os.path.join(project_dir, 'archinstall_creds.json') # Path for creds file
    
    with open(config_path, 'w') as f:
        json.dump(config, f) # Save main config
    with open(creds_path, 'w') as f:
        json.dump(creds_config, f) # Save creds config
        
    # Start installation in background thread
    def run_install():
        print("DEBUG: run_install thread starting")
        # log beginning of install thread
        print("DEBUG: run_install invoked: clearing previous progress and starting install")
        try:
            # clear any previous progress messages
            progress_buffer.clear()
            # Linux: run real archinstall in JSON mode using both config and creds files
            cmd = ['python', '-m', 'archinstall', 
                   '--config', config_path, 
                   '--creds', creds_path, # Add creds file path
                   '--script', 'guided', '--json']
            print(f"DEBUG: running archinstall command: {cmd}")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                line = line.strip()
                print(f"DEBUG: raw line from archinstall: {line}")
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # log any raw archinstall output for debugging
                    print(f"ARCHINSTALL RAW: {line}")
                    msg = {'message': line}
                progress_buffer.append(msg)
            proc.wait()
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"DEBUG: exception in run_install thread: {e}")
            # push error into the progress buffer for UI feedback
            progress_buffer.append({'status': 'error', 'message': str(e)})
    Thread(target=run_install, daemon=True).start()
    return jsonify({'status': 'running'})

@app.route('/api/install/logs')
def api_install_logs():
    # serve the in-memory JSON progress messages
    return jsonify(list(progress_buffer))

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
    zoneinfo_path = f'/usr/share/zoneinfo/{region}'
    timezones = []
    try:
        if not os.path.isdir(zoneinfo_path):
             return jsonify({"error": f"Region '{region}' not found.", "timezones": []}), 404
                 
        for root, dirs, files in os.walk(zoneinfo_path):
            for file in files:
                # Construct the full timezone path relative to the region
                full_path = os.path.join(root, file)
                # Get the timezone name relative to /usr/share/zoneinfo
                tz_name = os.path.relpath(full_path, '/usr/share/zoneinfo')
                # Avoid including non-timezone files if any exist
                if "/" in tz_name and not tz_name.startswith(('.', 'posix/', 'right/')):
                     # Check if the first char is uppercase (common convention for city names)
                     # This helps filter out potential helper files sometimes found in zoneinfo
                     if os.path.basename(tz_name)[0].isupper():
                        timezones.append(tz_name.replace('\\', '/')) # Ensure forward slashes

        timezones.sort()
    except FileNotFoundError:
        return jsonify({"error": f"Region path '{zoneinfo_path}' not found.", "timezones": []}), 404
    except Exception as e:
        return jsonify({"error": str(e), "timezones": []}), 500
    return jsonify({"timezones": timezones})

@app.route('/api/locale/<lang>')
def api_locale(lang):
    # serve translation JSON, fallback to en.json
    locale_file = f'{lang}.json'
    path = os.path.join('locales', locale_file)
    if not os.path.exists(path):
        path = os.path.join('locales', 'en.json')
    return send_from_directory('locales', os.path.basename(path), mimetype='application/json')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True) 