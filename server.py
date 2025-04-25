from flask import Flask, jsonify, send_from_directory, request
import archinstall
from archinstall.lib.disk.device_handler import devices as DISK_DEVICES
import psutil
import os
from threading import Thread
import json
import subprocess

app = Flask(__name__, static_folder='.')

@app.route('/')
def index():
    return send_from_directory('.', 'Install.html')

@app.route('/api/disks')
def api_disks():
    result = []
    for dev in DISK_DEVICES:
        info = dev.device_info
        total = info.total_size.value
        free = sum(region.total_size.value for region in info.free_space_regions)
        result.append({
            'model': info.model.strip() if info.model else info.path,
            'path': info.path,
            'total_bytes': total,
            'free_bytes': free
        })
    return jsonify(result)

@app.route('/api/network/interfaces')
def api_interfaces():
    result = [ {'name': name} for name in psutil.net_if_addrs().keys() ]
    return jsonify(result)

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
        subprocess.run(['python', '-m', 'archinstall', '--config', config_path], check=True)
    Thread(target=run_install, daemon=True).start()
    return jsonify({'status': 'running'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000) 