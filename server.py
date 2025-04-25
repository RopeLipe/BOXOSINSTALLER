from flask import Flask, jsonify, send_from_directory, request
import archinstall
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
import psutil
import os
from threading import Thread
import json
import subprocess
import socket
import re
from collections import deque

app = Flask(__name__, static_folder='.', static_url_path='')

# In-memory buffer to store JSON progress messages
progress_buffer = deque(maxlen=200)

@app.route('/')
def index():
    return send_from_directory('.', 'Install.html')

@app.route('/api/disks')
def api_disks():
    # use lsblk JSON to enumerate physical disks and compute free space
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
    stats = psutil.net_if_stats()
    addrs = psutil.net_if_addrs()
    # check for active Ethernet
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
            # scan for Wi-Fi networks
            try:
                scan = subprocess.check_output(['iwlist', iface, 'scan'], universal_newlines=True, stderr=subprocess.DEVNULL)
                ssids = re.findall(r'ESSID:"([^"]+)"', scan)
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
    data = request.json or {}
    # Build archinstall config
    config = {
        'filesystem': 'ext4',
        'harddrive': {'path': data.get('harddrive')},
        'kernels': ['linux'],
        'mirror-region': 'Worldwide',
        'ntp': True,
        'kernels': ['linux'],
        'swap': True,
        'timezone': 'UTC'
    }
    # Network
    net = data.get('network', {})
    if net:
        nic_cfg = {'NetworkManager': net.get('method') == 'dhcp'}
        if net.get('interface'):
            nic_cfg['nic'] = net.get('interface')
        config['nic'] = nic_cfg
        # Static fields are automatically applied by archinstall when NetworkManager is false
    # User account
    user = data.get('user', {})
    username = user.get('username')
    password = user.get('password')
    if username and password:
        config['superusers'] = {username: {'!password': password}}
    # Write config file
    config_path = '/tmp/archinstall_config.json'
    with open(config_path, 'w') as f:
        json.dump(config, f)
    # Start installation in background thread
    def run_install():
        # start archinstall in JSON mode, capture stdout lines
        cmd = ['python', '-m', 'archinstall', '--config', config_path, '--script', 'guided', '--json']
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # fallback to raw line
                msg = {'message': line}
            progress_buffer.append(msg)
        proc.wait()
    Thread(target=run_install, daemon=True).start()
    return jsonify({'status': 'running'})

@app.route('/api/install/logs')
def api_install_logs():
    # serve the in-memory JSON progress messages
    return jsonify(list(progress_buffer))

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