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
import platform
import tempfile
import time
import logging

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
print(f"DEBUG: Flask app created, IS_WINDOWS={platform.system() == 'Windows'}")

# Detect Windows to stub Linux-only operations
IS_WINDOWS = platform.system() == "Windows"

# In-memory buffer to store JSON progress messages
progress_buffer = deque(maxlen=200)

@app.route('/')
def index():
    return send_from_directory('.', 'Install.html')

@app.route('/api/disks')
def api_disks():
    # Windows: use psutil to enumerate drives
    if IS_WINDOWS:
        result = []
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except PermissionError:
                continue
            result.append({
                'name': part.device,
                'model': part.device,
                'path': part.mountpoint,
                'total_bytes': usage.total,
                'free_bytes': usage.free
            })
        return jsonify(result)
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
    # Windows: assume first usable interface
    if IS_WINDOWS:
        iface = next((i for i,s in stats.items() if s.isup and not i.lower().startswith('loop')), None)
        # check administrative state via netsh
        enabled = False
        try:
            out = subprocess.check_output(['netsh', 'interface', 'show', 'interface', f'name={iface}'], universal_newlines=True)
            enabled = 'Enabled' in out
        except Exception:
            pass
        return jsonify({'connection_type': 'ethernet', 'interface': iface, 'enabled': enabled})
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
    raw_body = request.get_data(as_text=True)
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
        "pt": "Português"
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
                print(f"DEBUG: Calculating default layout for {target_device_path}")
                try:
                    # Define standard UEFI layout using dictionary format for sizes
                    boot_part = {
                        "status": "create", "type": "primary",
                        "start": {"unit": "MiB", "value": 1}, # 1MiB offset
                        "length": {"unit": "GiB", "value": 1}, # 1GiB size
                        "mountpoint": "/boot", "fs_type": "fat32", "flags": ["boot", "esp"]
                    }
                    root_part = {
                        "status": "create", "type": "primary",
                        "start": {"unit": "MiB", "value": 1025}, # Start after boot (1GiB + 1MiB)
                        # Omitting "length" should imply using the rest of the disk
                        "mountpoint": "/", "fs_type": filesystem_str
                    }

                    device_mod = {
                        "device": target_device_path,
                        "wipe": wipe_disk,
                        "partitions": [boot_part, root_part]
                    }
                    # Set the final config to use this explicit layout
                    disk_cfg = {
                        "config_type": "manual", # Explicitly state we are providing the layout
                        "device_modifications": [device_mod]
                    }
                    print(f"DEBUG: Calculated default layout: {disk_cfg}")

                except Exception as calc_err:
                    print(f"ERROR: Failed calculating default layout: {calc_err}")
                    # Fallback: Use the original request if calculation failed
                    disk_cfg = disk_cfg_request 
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
        # audio (pipewire)
        "audio_config": {"audio": data.get("audio_config", "pipewire")},
        # bootloader
        "bootloader": data.get("bootloader", "systemd-boot"),
        # debugging
        "debug": data.get("debug", False),
        # drive to install on (auto-partition default layout)
        "harddrive": data.get("harddrive", {}),
        # locale settings
        "locale_config": {"sys_lang": lang_code, "sys_enc": data.get("sys_enc", "UTF-8"), "kb_layout": data.get("kb_layout", "us")},
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
        # extra packages
        "packages": data.get("packages", []),
        "parallel downloads": data.get("parallel downloads", 0),
        # use guided script
        "script": "guided",
        # silent mode
        "silent": data.get("silent", False),
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
        # user account to create (must be a list of user objects)
        "user_config": {
            "users": [
                {
                    "username": data.get("user", {}).get("username"),
                    "password": data.get("user", {}).get("password")
                }
            ]
        },
    }
    # debug-print the final config for troubleshooting
    print(f"DEBUG: final archinstall config: {config}")
    # save config in the project root (Boxlinux folder)
    project_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(project_dir, 'archinstall_config.json')
    with open(config_path, 'w') as f:
        json.dump(config, f)
    # Start installation in background thread
    def run_install():
        print("DEBUG: run_install thread starting")
        # log beginning of install thread
        print("DEBUG: run_install invoked: clearing previous progress and starting install")
        try:
            # clear any previous progress messages
            progress_buffer.clear()
            # Windows: simulate progress events
            if IS_WINDOWS:
                for pct in range(0, 101, 10):
                    progress_buffer.append({'percent': pct, 'step': f'Step {pct/10}'})
                    time.sleep(0.2)
                progress_buffer.append({'status': 'done', 'percent': 100, 'message': 'Completed'})
                return
            # Linux: run real archinstall in JSON mode
            cmd = ['python', '-m', 'archinstall', '--config', config_path, '--script', 'guided', '--json']
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

@app.route('/api/timezones/<country>')
def api_timezones(country):
    # return a list of timezones based on country code
    tz_map = {
        'US': ['America/New_York', 'America/Los_Angeles', 'America/Chicago', 'America/Denver'],
        'CA': ['America/Toronto', 'America/Vancouver'],
        'DE': ['Europe/Berlin'],
        'FR': ['Europe/Paris'],
        'JP': ['Asia/Tokyo'],
        'AU': ['Australia/Sydney', 'Australia/Melbourne']
    }
    return jsonify(tz_map.get(country, ['UTC']))

@app.route('/api/locale/<lang>')
def api_locale(lang):
    # serve translation JSON, fallback to en.json
    locale_file = f'{lang}.json'
    path = os.path.join('locales', locale_file)
    if not os.path.exists(path):
        path = os.path.join('locales', 'en.json')
    return send_from_directory('locales', os.path.basename(path), mimetype='application/json')

if __name__ == '__main__':
    # Windows may forbid binding port 8000 to 0.0.0.0 without elevated rights
    if IS_WINDOWS:
        app.run(host='127.0.0.1', port=5000, debug=True)
    else:
        app.run(host='0.0.0.0', port=8000, debug=True) 