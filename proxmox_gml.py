#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Proxmox-GML (GPU Monitoring for LXC)
System for monitoring GPU resource usage by LXC containers in Proxmox environments
"""

import time
import json
import subprocess
import os
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import threading
from urllib.parse import urlparse
import sys

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('proxmox_gml')

# Web server port
PORT = 8001

# Path to nvitop virtual environment
NVITOP_VENV = '/opt/nvitop-venv'
NVITOP_PYTHON = f"{NVITOP_VENV}/bin/python"

# Cache for collected data
last_data = {}
last_update_time = 0
update_interval = 5  # seconds

# Check if nvitop virtual environment exists
if not os.path.exists(NVITOP_VENV):
    logger.error(f"nvitop virtual environment not found at {NVITOP_VENV}")
    logger.error("Please install nvitop: python -m pip install nvitop")
    sys.exit(1)

def run_nvitop_script(script):
    """Run Python script with nvitop's Python"""
    try:
        cmd = [NVITOP_PYTHON, "-c", script]
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode()
        return output
    except subprocess.CalledProcessError as e:
        logger.error(f"Error executing nvitop script: {e}")
        logger.error(f"Output: {e.output.decode() if hasattr(e, 'output') else 'No output'}")
        return None

def get_container_info(pid):
    """Get container information by process PID"""
    try:
        # Read cgroup file directly
        cgroup_file = f"/proc/{pid}/cgroup"
        if not os.path.exists(cgroup_file):
            return None
            
        with open(cgroup_file, 'r') as f:
            cgroup_content = f.read().strip()
        
        # Look for LXC container patterns
        if '/lxc/' in cgroup_content:
            # Extract container ID from /lxc/ID/... pattern
            import re
            container_match = re.search(r'/lxc/([0-9]+)/', cgroup_content)
            if container_match:
                container_id = container_match.group(1)
                
                # Extract service name if available
                service_match = re.search(r'system\.slice/([^/]+)\.service', cgroup_content)
                service_name = service_match.group(1) if service_match else None
                
                container_name = 'Unknown'
                # If we got numeric ID, try to get container name
                if container_id.isdigit():
                    # Check Proxmox config file
                    proxmox_conf = f"/etc/pve/lxc/{container_id}.conf"
                    if os.path.exists(proxmox_conf):
                        try:
                            cmd = f"grep -E '^hostname:' {proxmox_conf} | cut -d' ' -f2 || echo 'Unknown'"
                            container_name = subprocess.check_output(cmd, shell=True).decode().strip()
                        except Exception:
                            pass
                    
                    # Check with pct command in Proxmox
                    if container_name == 'Unknown' and os.path.exists("/usr/sbin/pct"):
                        try:
                            cmd = f"pct list | grep {container_id} | awk '{{print $3}}' || echo 'Unknown'"
                            pct_name = subprocess.check_output(cmd, shell=True).decode().strip()
                            if pct_name != 'Unknown':
                                container_name = pct_name
                        except Exception:
                            pass
                    
                    # If we still don't have a name but have a service name, use it
                    if container_name == 'Unknown' and service_name:
                        container_name = service_name
                        
                return {
                    'id': container_id,
                    'name': container_name
                }
        
        return None
    except Exception as e:
        logger.error(f"Error getting container info: {e}")
        return None

def collect_data():
    """Collect all data about GPUs and processes"""
    global last_data, last_update_time
    
    # If data was collected recently, return cached data
    current_time = time.time()
    if current_time - last_update_time < update_interval and last_data:
        return last_data
    
    logger.info("Collecting data about GPUs and processes...")
    
    # Get GPU and process information
    gpu_script = """
import json
from nvitop import Device, take_snapshots

# Get GPU information
devices = Device.all()
gpu_info = []

for device in devices:
    # Базовая информация о GPU
    gpu_data = {
        'index': device.index,
        'name': device.name(),
        'uuid': device.uuid(),
        'utilization': device.gpu_utilization(),
        'memory_used': device.memory_used(),
        'memory_used_human': device.memory_used_human(),
        'memory_total': device.memory_total(),
        'memory_total_human': device.memory_total_human(),
        'memory_percent': device.memory_percent(),
        'temperature': device.temperature(),
        'power_usage': device.power_usage(),
        'power_limit': device.power_limit()
    }
    
    # Дополнительная информация о GPU
    try:
        # Частоты GPU
        gpu_data['graphics_clock'] = device.graphics_clock()
        gpu_data['memory_clock'] = device.memory_clock()
        gpu_data['sm_clock'] = device.sm_clock()
        
        # Максимальные частоты
        gpu_data['max_graphics_clock'] = device.max_graphics_clock()
        gpu_data['max_memory_clock'] = device.max_memory_clock()
        gpu_data['max_sm_clock'] = device.max_sm_clock()
        
        # PCIe пропускная способность
        gpu_data['pcie_tx'] = device.pcie_tx_throughput()
        gpu_data['pcie_rx'] = device.pcie_rx_throughput()
        # Методы форматирования нужно вызывать, а не передавать сами методы
        if hasattr(device, 'pcie_tx_throughput_human') and callable(getattr(device, 'pcie_tx_throughput_human')):
            gpu_data['pcie_tx_human'] = device.pcie_tx_throughput_human()
            gpu_data['pcie_rx_human'] = device.pcie_rx_throughput_human()
        
        # NVLink пропускная способность (если доступна)
        if hasattr(device, 'nvlink_tx_throughput') and callable(getattr(device, 'nvlink_tx_throughput')):
            gpu_data['nvlink_tx'] = device.nvlink_tx_throughput()
            gpu_data['nvlink_rx'] = device.nvlink_rx_throughput()
            # Вызываем методы для получения строковых значений
            if hasattr(device, 'nvlink_tx_throughput_human') and callable(getattr(device, 'nvlink_tx_throughput_human')):
                gpu_data['nvlink_tx_human'] = device.nvlink_tx_throughput_human()
                gpu_data['nvlink_rx_human'] = device.nvlink_rx_throughput_human()
            
        # Производительность энкодера/декодера видео
        if hasattr(device, 'encoder_utilization') and callable(getattr(device, 'encoder_utilization')):
            gpu_data['encoder_utilization'] = device.encoder_utilization()
            gpu_data['decoder_utilization'] = device.decoder_utilization()
            
        # Информация о режиме и драйвере
        gpu_data['compute_mode'] = device.compute_mode()
        gpu_data['driver_version'] = device.driver_version()
    except Exception as e:
        print(f"Error getting additional GPU info: {e}")
    
    gpu_info.append(gpu_data)

# Get process information
snapshots = take_snapshots()
processes = []

for process in snapshots.gpu_processes:
    # Базовая информация о процессе
    process_info = {
        'pid': process.pid,
        'command': process.command,
        'username': process.username,
        'gpu_index': process.device.index,
        'gpu_memory': process.gpu_memory,
        'gpu_memory_human': process.gpu_memory_human
    }
    
    # Дополнительная информация о процессе
    try:
        # GPU загрузка процесса
        if hasattr(process, 'gpu_sm_utilization'):
            process_info['gpu_utilization'] = process.gpu_sm_utilization
        
        # Время работы процесса
        process_info['running_time'] = process.running_time_in_seconds
        process_info['running_time_human'] = process.running_time_human
        
        # Использование CPU и памяти хоста
        process_info['cpu_percent'] = process.cpu_percent
        process_info['host_memory'] = process.host_memory
        process_info['host_memory_human'] = process.host_memory_human
        process_info['host_memory_percent'] = process.host_memory_percent
        
        # Статус процесса
        process_info['status'] = process.status
        process_info['is_running'] = process.is_running
    except Exception as e:
        print(f"Error getting additional process info for PID {process.pid}: {e}")
    
    processes.append(process_info)

# Output combined data
result = {
    'gpu_info': gpu_info,
    'processes': processes
}
print(json.dumps(result))
"""
    
    output = run_nvitop_script(gpu_script)
    if not output:
        logger.error("Failed to get data from nvitop")
        return {'timestamp': current_time, 'error': 'Failed to get data from nvitop'}
    
    try:
        nvitop_data = json.loads(output)
        
        # Add container information to processes
        for process in nvitop_data['processes']:
            container_info = get_container_info(process['pid'])
            if container_info:
                process['container_id'] = container_info['id']
                process['container_name'] = container_info['name']
            else:
                process['container_id'] = None
                process['container_name'] = None
        
        # Identify containers using multiple GPUs
        containers = {}
        container_names = {}
        
        for process in nvitop_data['processes']:
            if process.get('container_id'):
                container_id = process['container_id']
                container_name = process.get('container_name', 'Unknown')
                gpu_index = process['gpu_index']
                
                # Save container name
                if container_id not in container_names:
                    container_names[container_id] = container_name
                
                # Track GPU usage by this container
                if container_id not in containers:
                    containers[container_id] = set()
                
                containers[container_id].add(gpu_index)
        
        # Filter containers using multiple GPUs
        multi_gpu_containers = {}
        for container_id, gpu_indices in containers.items():
            if len(gpu_indices) > 1:
                multi_gpu_containers[container_id] = {
                    'gpu_indices': list(gpu_indices),
                    'name': container_names.get(container_id, 'Unknown')
                }
        
        # Group processes by container and GPU
        container_processes = {}
        for process in nvitop_data['processes']:
            container_id = process.get('container_id') or 'Host'
            container_name = process.get('container_name') or 'Host System'
            gpu_index = process['gpu_index']
            
            key = f"{container_id}_{gpu_index}"
            if key not in container_processes:
                container_processes[key] = {
                    'container_id': container_id,
                    'container_name': container_name,
                    'gpu_index': gpu_index,
                    'process_count': 0,
                    'total_memory': 0,
                    'gpu_utilization': 0
                }
            
            container_processes[key]['process_count'] += 1
            container_processes[key]['total_memory'] += process['gpu_memory']
            
            # Добавляем процент использования GPU, если доступен
            if 'gpu_utilization' in process:
                container_processes[key]['gpu_utilization'] += process.get('gpu_utilization', 0)
        
        # Final data structure
        data = {
            'timestamp': current_time,
            'gpu_info': nvitop_data['gpu_info'],
            'processes': nvitop_data['processes'],
            'multi_gpu_containers': multi_gpu_containers,
            'container_processes': container_processes
        }
        
        # Update cache
        last_data = data
        last_update_time = current_time
        
        return data
    except Exception as e:
        logger.error(f"Error processing nvitop data: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return {'timestamp': current_time, 'error': str(e)}

def generate_html(data):
    """Generate HTML page with GPU and process data"""
    try:
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(data['timestamp']))
        
        # Check for errors
        if 'error' in data:
            return f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>GPU Monitoring Error</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; }}
                    .error {{ color: red; font-weight: bold; }}
                </style>
            </head>
            <body>
                <h1>GPU Monitoring Error</h1>
                <p>Last updated: {timestamp}</p>
                <p class="error">Error: {data['error']}</p>
                <button onclick="window.location.reload()">Refresh</button>
            </body>
            </html>
            """
        
        # Prepare rows for GPU summary table
        gpu_rows = ""
        gpu_details = ""
        
        for gpu in data['gpu_info']:
            # Преобразуем мощность из mW в W
            power_usage_watts = gpu['power_usage'] / 1000
            power_limit_watts = gpu['power_limit'] / 1000
            # Рассчитываем процент использования мощности
            power_percent = int((power_usage_watts / power_limit_watts) * 100) if power_limit_watts > 0 else 0
            
            # Получаем информацию о частотах, если доступна
            clock_info = ""
            if 'graphics_clock' in gpu and 'memory_clock' in gpu and 'sm_clock' in gpu:
                clock_info = f"Graphics: {gpu['graphics_clock']} MHz<br>Memory: {gpu['memory_clock']} MHz<br>SM: {gpu['sm_clock']} MHz"
            
            # Цвета для индикаторов загрузки
            memory_bar_color = "#4CAF50"  # Зеленый по умолчанию
            power_bar_color = "#4CAF50"   # Зеленый по умолчанию
            gpu_bar_color = "#4CAF50"     # Зеленый по умолчанию
            
            # Изменяем цвет на желтый для значений > 70%
            if gpu['memory_percent'] > 70:
                memory_bar_color = "#FFEB3B"
            if power_percent > 70:
                power_bar_color = "#FFEB3B"
            if gpu['utilization'] > 70:
                gpu_bar_color = "#FFEB3B"
                
            # Изменяем цвет на красный для значений > 90%
            if gpu['memory_percent'] > 90:
                memory_bar_color = "#F44336"
            if power_percent > 90:
                power_bar_color = "#F44336"
            if gpu['utilization'] > 90:
                gpu_bar_color = "#F44336"
            
            gpu_rows += f"""
            <tr>
                <td>{gpu['index']}</td>
                <td>{gpu['name']}</td>
                <td>
                    <div>{gpu['utilization']}%</div>
                    <div class="gpu-bar-container">
                        <div class="gpu-bar" style="width:{gpu['utilization']}%; background-color:{gpu_bar_color}"></div>
                    </div>
                </td>
                <td>
                    <div>{gpu['memory_used_human']}/{gpu['memory_total_human']} ({gpu['memory_percent']}%)</div>
                    <div class="gpu-bar-container">
                        <div class="gpu-bar" style="width:{gpu['memory_percent']}%; background-color:{memory_bar_color}"></div>
                    </div>
                </td>
                <td>
                    <div>{power_usage_watts:.1f}W / {power_limit_watts:.1f}W ({power_percent}%)</div>
                    <div class="gpu-bar-container">
                        <div class="gpu-bar" style="width:{power_percent}%; background-color:{power_bar_color}"></div>
                    </div>
                </td>
                <td>{gpu['temperature']}°C</td>
                <td>
                    PCIe TX (↑): {gpu.get('pcie_tx_human', 'N/A')}<br>
                    PCIe RX (↓): {gpu.get('pcie_rx_human', 'N/A')}
                    {f"<br>NVLink TX: {gpu.get('nvlink_tx_human', 'N/A')}<br>NVLink RX: {gpu.get('nvlink_rx_human', 'N/A')}" if 'nvlink_tx_human' in gpu and gpu.get('nvlink_tx_human') != '[]' else ''}
                </td>
                <td>{clock_info}</td>
            </tr>
            """
            
            # Подробная информация о GPU для аккордеона
            pcie_info = ""
            nvlink_info = ""
            video_info = ""
            driver_info = ""
            
            if 'pcie_tx_human' in gpu and 'pcie_rx_human' in gpu:
                pcie_info = f"""<tr><td>PCIe Throughput</td><td>TX: {gpu.get('pcie_tx_human', 'N/A')}, RX: {gpu.get('pcie_rx_human', 'N/A')}</td></tr>"""
            
            if 'nvlink_tx_human' in gpu and 'nvlink_rx_human' in gpu:
                nvlink_info = f"""<tr><td>NVLink Throughput</td><td>TX: {gpu.get('nvlink_tx_human', 'N/A')}, RX: {gpu.get('nvlink_rx_human', 'N/A')}</td></tr>"""
            
            if 'encoder_utilization' in gpu and 'decoder_utilization' in gpu:
                video_info = f"""<tr><td>Video Encoder/Decoder</td><td>Encoder: {gpu.get('encoder_utilization', 'N/A')}%, Decoder: {gpu.get('decoder_utilization', 'N/A')}%</td></tr>"""
            
            if 'compute_mode' in gpu and 'driver_version' in gpu:
                driver_info = f"""<tr><td>Driver Info</td><td>Version: {gpu.get('driver_version', 'N/A')}, Compute Mode: {gpu.get('compute_mode', 'N/A')}</td></tr>"""
            
            max_clocks = """"""
            if 'max_graphics_clock' in gpu and 'max_memory_clock' in gpu and 'max_sm_clock' in gpu:
                max_clocks = f"""<tr><td>Max Clocks</td><td>Graphics: {gpu.get('max_graphics_clock', 'N/A')} MHz, Memory: {gpu.get('max_memory_clock', 'N/A')} MHz, SM: {gpu.get('max_sm_clock', 'N/A')} MHz</td></tr>"""
            
            gpu_details += f"""
            <div class="accordion-item">
                <h3 class="accordion-header">GPU {gpu['index']}: {gpu['name']}</h3>
                <div class="accordion-content">
                    <table class="details-table">
                        <tr><td>UUID</td><td>{gpu['uuid']}</td></tr>
                        {pcie_info}
                        {nvlink_info}
                        {video_info}
                        {max_clocks}
                        {driver_info}
                    </table>
                </div>
            </div>
            """
        
        # Prepare rows for container table
        container_rows = ""
        for key, info in data['container_processes'].items():
            container_id = info['container_id']
            container_name = info['container_name']
            gpu_index = info['gpu_index']
            process_count = info['process_count']
            total_memory = info['total_memory'] / (1024 * 1024)  # Bytes to MB
            
            # Check if container uses multiple GPUs
            is_multi_gpu = container_id in data['multi_gpu_containers']
            row_class = "multi-gpu" if is_multi_gpu else ""
            
            # Получаем информацию о проценте использования GPU
            gpu_util = info.get('gpu_utilization', 0)
            
            # Выбираем цвет для индикатора загрузки
            util_bar_color = "#4CAF50"  # Зеленый по умолчанию
            
            # Изменяем цвет на желтый или красный для высоких значений
            if gpu_util > 70:
                util_bar_color = "#FFEB3B"  # Желтый
            if gpu_util > 90:
                util_bar_color = "#F44336"  # Красный
            
            container_rows += f"""
            <tr class="{row_class}">
                <td>{container_id}</td>
                <td>{container_name}</td>
                <td>{gpu_index}</td>
                <td>
                    <div>{gpu_util:.1f}%</div>
                    <div class="gpu-bar-container">
                        <div class="gpu-bar" style="width:{gpu_util}%; background-color:{util_bar_color}"></div>
                    </div>
                </td>
                <td>{process_count}</td>
                <td>{total_memory:.2f} MiB</td>
            </tr>
            """
        
        # Prepare rows for multi-GPU containers table
        multi_gpu_rows = ""
        for container_id, container_info in data['multi_gpu_containers'].items():
            gpu_indices = container_info['gpu_indices']
            container_name = container_info['name']
            multi_gpu_rows += f"""
            <tr>
                <td>{container_id}</td>
                <td>{container_name}</td>
                <td>{', '.join(map(str, gpu_indices))}</td>
            </tr>
            """
        
        # Prepare rows for process details table
        process_rows = ""
        for process in data['processes']:
            container_id = process.get('container_id') or 'Host'
            container_name = process.get('container_name') or 'Host System'
            
            # Check if process belongs to multi-GPU container
            is_multi_gpu = container_id in data['multi_gpu_containers']
            row_class = "multi-gpu" if is_multi_gpu else ""
            
            # GPU memory
            memory_mb = process['gpu_memory'] / (1024 * 1024)
            
            # GPU utilization 
            gpu_util = process.get('gpu_utilization', 'N/A')
            if gpu_util != 'N/A':
                gpu_util = f"{gpu_util}%"
            
            # Host memory
            host_memory = process.get('host_memory_human', 'N/A')
            host_memory_percent = process.get('host_memory_percent', 'N/A')
            if host_memory != 'N/A' and host_memory_percent != 'N/A':
                host_memory = f"{host_memory} ({host_memory_percent}%)"
            
            # CPU usage
            cpu_percent = process.get('cpu_percent', 'N/A')
            if cpu_percent != 'N/A':
                cpu_percent = f"{cpu_percent:.1f}%"
            
            # Running time
            running_time = process.get('running_time_human', 'N/A')
            
            # Status
            status = process.get('status', 'N/A')
            
            # Удаляем процент использования хост-памяти
            if host_memory != 'N/A' and isinstance(host_memory, str) and '(' in host_memory:
                # Выбираем только размер памяти без процента
                host_memory = host_memory.split('(')[0].strip()
            
            process_rows += f"""
            <tr class="{row_class}">
                <td>{container_id}</td>
                <td>{container_name}</td>
                <td>{process['pid']}</td>
                <td>{process['command']}</td>
                <td>{process['gpu_index']}</td>
                <td>{gpu_util}</td>
                <td>{cpu_percent}</td>
                <td>{memory_mb:.2f} MiB</td>
                <td>{host_memory}</td>
                <td>{running_time}</td>
            </tr>
            """
        
        # Заполняем переменные в шаблоне
        template_vars = {
            'timestamp': timestamp,
            'gpu_rows': gpu_rows,
            'gpu_details': gpu_details,
            'container_rows': container_rows,
            'multi_gpu_rows': multi_gpu_rows,
            'process_rows': process_rows
        }
        
        # Complete HTML template
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>GPU and LXC Container Monitoring</title>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    margin: 20px;
                    background-color: #f5f5f5;
                }}
                h1 {{
                    color: #333;
                    border-bottom: 2px solid #ccc;
                    padding-bottom: 10px;
                }}
                table {{
                    width: 100%;
                    border-collapse: collapse;
                    margin-bottom: 20px;
                    background-color: white;
                }}
                th, td {{
                    padding: 8px;
                    text-align: left;
                    border: 1px solid #ddd;
                }}
                th {{
                    background-color: #f2f2f2;
                    font-weight: bold;
                }}
                tr:nth-child(even) {{
                    background-color: #f9f9f9;
                }}
                .container {{
                    margin-bottom: 30px;
                }}
                .timestamp {{
                    color: #666;
                    font-style: italic;
                    margin-bottom: 10px;
                }}
                .gpu-bar {{
                    height: 20px;
                    background-color: #4CAF50;
                    text-align: center;
                    color: black;
                    font-weight: bold;
                    border-radius: 3px;
                }}
                .gpu-bar-container {{
                    width: 100%;
                    background-color: #f1f1f1;
                    border-radius: 3px;
                }}
                .refresh-controls {{
                    display: flex;
                    align-items: center;
                    margin-bottom: 20px;
                }}
                .refresh-button {{
                    background-color: #4CAF50;
                    color: white;
                    padding: 10px 15px;
                    border: none;
                    border-radius: 4px;
                    cursor: pointer;
                    font-size: 16px;
                    margin-right: 15px;
                }}
                .refresh-button:hover {{
                    background-color: #45a049;
                }}
                .auto-refresh-label {{
                    display: flex;
                    align-items: center;
                    font-size: 14px;
                    cursor: pointer;
                }}
                .auto-refresh-label input {{
                    margin-right: 5px;
                    width: 18px;
                    height: 18px;
                }}
                .multi-gpu {{
                    background-color: #ffeeba;
                }}
                .multi-gpu td {{
                    border: 1px solid #ffeeba;
                }}
                
                /* Accordion Styles */
                .accordion {{
                    width: 100%;
                    margin-bottom: 20px;
                }}
                .accordion-item {{
                    margin-bottom: 5px;
                    border: 1px solid #ddd;
                    border-radius: 4px;
                    overflow: hidden;
                }}
                .accordion-header {{
                    background-color: #f2f2f2;
                    padding: 10px 15px;
                    cursor: pointer;
                    margin: 0;
                    font-size: 16px;
                    font-weight: bold;
                    position: relative;
                }}
                .accordion-header::after {{
                    content: '+';
                    position: absolute;
                    right: 15px;
                    top: 10px;
                }}
                .accordion-header.active::after {{
                    content: '-';
                }}
                .accordion-content {{
                    padding: 0;
                    max-height: 0;
                    overflow: hidden;
                    transition: max-height 0.3s ease-out;
                }}
                .details-table {{
                    width: 100%;
                    border-collapse: collapse;
                }}
                .details-table td {{
                    padding: 8px 15px;
                    border: 1px solid #ddd;
                }}
                .details-table td:first-child {{
                    width: 30%;
                    font-weight: bold;
                    background-color: #f9f9f9;
                }}
            </style>
            <script>
                document.addEventListener('DOMContentLoaded', function() {{
                    // Accordion functionality
                    var headers = document.querySelectorAll('.accordion-header');
                    headers.forEach(function(header) {{
                        header.addEventListener('click', function() {{
                            this.classList.toggle('active');
                            var content = this.nextElementSibling;
                            if (content.style.maxHeight) {{
                                content.style.maxHeight = null;
                            }} else {{
                                content.style.maxHeight = content.scrollHeight + 'px';
                            }}
                        }});
                    }});
                    
                    // Auto-refresh functionality
                    var autoRefreshCheckbox = document.getElementById('auto-refresh');
                    var refreshTimerId = null;
                    
                    // Load saved preference from localStorage
                    if (localStorage.getItem('gpu_monitor_autorefresh') === 'true') {{
                        autoRefreshCheckbox.checked = true;
                        startAutoRefresh();
                    }}
                    
                    autoRefreshCheckbox.addEventListener('change', function() {{
                        if (this.checked) {{
                            localStorage.setItem('gpu_monitor_autorefresh', 'true');
                            startAutoRefresh();
                        }} else {{
                            localStorage.setItem('gpu_monitor_autorefresh', 'false');
                            stopAutoRefresh();
                        }}
                    }});
                    
                    function startAutoRefresh() {{
                        if (refreshTimerId) {{
                            clearInterval(refreshTimerId);
                        }}
                        refreshTimerId = setInterval(function() {{
                            window.location.reload();
                        }}, 5000); // Refresh every 5 seconds
                    }}
                    
                    function stopAutoRefresh() {{
                        if (refreshTimerId) {{
                            clearInterval(refreshTimerId);
                            refreshTimerId = null;
                        }}
                    }}
                }});
            </script>
        </head>
        <body>
            <h1>GPU and LXC Container Monitoring</h1>
            <div class="timestamp">Last updated: {timestamp}</div>
            <div class="refresh-controls">
                <button class="refresh-button" onclick="window.location.reload()">Refresh Data</button>
                <label class="auto-refresh-label">
                    <input type="checkbox" id="auto-refresh" /> Auto-refresh (5s)
                </label>
            </div>
            
            <div class="container">
                <h2>GPU Summary</h2>
                <table>
                    <tr>
                        <th>GPU Index</th>
                        <th>GPU Name</th>
                        <th>GPU Usage (%)</th>
                        <th>Memory Used / Total</th>
                        <th>Power Usage</th>
                        <th>Temperature</th>
                        <th>Throughput</th>
                        <th>Clock Speeds</th>
                    </tr>
                    {gpu_rows}
                </table>
            </div>
            
            <!-- Удален раздел с детальной информацией о GPU -->
            
            <div class="container">
                <h2>Containers</h2>
                <table>
                    <tr>
                        <th>Container ID</th>
                        <th>Container Name</th>
                        <th>GPU Index</th>
                        <th>GPU %</th>
                        <th>Processes</th>
                        <th>GPU Memory Used (MiB)</th>
                    </tr>
                    {container_rows}
                </table>
            </div>
            
            <div class="container">
                <h2>Containers Using Multiple GPUs</h2>
                <table>
                    <tr>
                        <th>Container ID</th>
                        <th>Container Name</th>
                        <th>GPU Indices</th>
                    </tr>
                    {multi_gpu_rows}
                </table>
            </div>
            
            <div class="container">
                <h2>Process Details</h2>
                <table>
                    <tr>
                        <th>Container ID</th>
                        <th>Container Name</th>
                        <th>PID</th>
                        <th>Command</th>
                        <th>GPU Index</th>
                        <th>GPU Usage</th>
                        <th>CPU %</th>
                        <th>GPU Memory</th>
                        <th>Host Memory</th>
                        <th>Running Time</th>
                    </tr>
                    {process_rows}
                </table>
            </div>
        </body>
        </html>
        """
        
        return html
    except Exception as e:
        logger.error(f"Error generating HTML: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return f"<html><body><h1>Error</h1><p>{str(e)}</p></body></html>"

def generate_prometheus_metrics(data):
    """Generate Prometheus-format metrics"""
    metrics = []
    
    # Check for errors
    if 'error' in data:
        metrics.append(f'# HELP gpu_monitor_error Error status of GPU monitor')
        metrics.append(f'# TYPE gpu_monitor_error gauge')
        metrics.append(f'gpu_monitor_error 1')
        return '\n'.join(metrics)
    
    # Helper function to ensure numeric values
    def safe_numeric(value, default=0):
        if value is None or value == "":
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            return default
    
    # GPU metrics
    for gpu in data['gpu_info']:
        gpu_index = gpu['index']
        
        metrics.append(f'# HELP gpu_utilization GPU utilization percentage')
        metrics.append(f'# TYPE gpu_utilization gauge')
        metrics.append(f'gpu_utilization{{gpu="{gpu_index}"}} {safe_numeric(gpu.get("utilization", 0))}')
        
        metrics.append(f'# HELP gpu_memory_used GPU memory used in bytes')
        metrics.append(f'# TYPE gpu_memory_used gauge')
        metrics.append(f'gpu_memory_used{{gpu="{gpu_index}"}} {safe_numeric(gpu.get("memory_used", 0))}')
        
        metrics.append(f'# HELP gpu_memory_total GPU total memory in bytes')
        metrics.append(f'# TYPE gpu_memory_total gauge')
        metrics.append(f'gpu_memory_total{{gpu="{gpu_index}"}} {safe_numeric(gpu.get("memory_total", 0))}')
        
        metrics.append(f'# HELP gpu_temperature GPU temperature in Celsius')
        metrics.append(f'# TYPE gpu_temperature gauge')
        metrics.append(f'gpu_temperature{{gpu="{gpu_index}"}} {safe_numeric(gpu.get("temperature", 0))}')
        
        # Преобразуем мощность из mW в W
        power_watts = safe_numeric(gpu.get("power_usage", 0)) / 1000
        power_limit_watts = safe_numeric(gpu.get("power_limit", 0)) / 1000
        
        metrics.append(f'# HELP gpu_power_usage GPU power usage in Watts')
        metrics.append(f'# TYPE gpu_power_usage gauge')
        metrics.append(f'gpu_power_usage{{gpu="{gpu_index}"}} {power_watts}')
        
        metrics.append(f'# HELP gpu_power_limit GPU power limit in Watts')
        metrics.append(f'# TYPE gpu_power_limit gauge')
        metrics.append(f'gpu_power_limit{{gpu="{gpu_index}"}} {power_limit_watts}')
        
        # Частоты GPU
        if 'graphics_clock' in gpu:
            metrics.append(f'# HELP gpu_graphics_clock GPU graphics clock in MHz')
            metrics.append(f'# TYPE gpu_graphics_clock gauge')
            metrics.append(f'gpu_graphics_clock{{gpu="{gpu_index}"}} {safe_numeric(gpu.get("graphics_clock", 0))}')
        
        if 'memory_clock' in gpu:
            metrics.append(f'# HELP gpu_memory_clock GPU memory clock in MHz')
            metrics.append(f'# TYPE gpu_memory_clock gauge')
            metrics.append(f'gpu_memory_clock{{gpu="{gpu_index}"}} {safe_numeric(gpu.get("memory_clock", 0))}')
        
        if 'sm_clock' in gpu:
            metrics.append(f'# HELP gpu_sm_clock GPU SM clock in MHz')
            metrics.append(f'# TYPE gpu_sm_clock gauge')
            metrics.append(f'gpu_sm_clock{{gpu="{gpu_index}"}} {safe_numeric(gpu.get("sm_clock", 0))}')
        
        # PCIe пропускная способность
        if 'pcie_tx' in gpu:
            metrics.append(f'# HELP gpu_pcie_tx PCIe TX throughput in bytes per second')
            metrics.append(f'# TYPE gpu_pcie_tx gauge')
            metrics.append(f'gpu_pcie_tx{{gpu="{gpu_index}"}} {safe_numeric(gpu.get("pcie_tx", 0))}')
        
        if 'pcie_rx' in gpu:
            metrics.append(f'# HELP gpu_pcie_rx PCIe RX throughput in bytes per second')
            metrics.append(f'# TYPE gpu_pcie_rx gauge')
            metrics.append(f'gpu_pcie_rx{{gpu="{gpu_index}"}} {safe_numeric(gpu.get("pcie_rx", 0))}')
        
        # NVLink пропускная способность
        if 'nvlink_tx' in gpu:
            metrics.append(f'# HELP gpu_nvlink_tx NVLink TX throughput in bytes per second')
            metrics.append(f'# TYPE gpu_nvlink_tx gauge')
            metrics.append(f'gpu_nvlink_tx{{gpu="{gpu_index}"}} {safe_numeric(gpu.get("nvlink_tx", 0))}')
        
        if 'nvlink_rx' in gpu:
            metrics.append(f'# HELP gpu_nvlink_rx NVLink RX throughput in bytes per second')
            metrics.append(f'# TYPE gpu_nvlink_rx gauge')
            metrics.append(f'gpu_nvlink_rx{{gpu="{gpu_index}"}} {safe_numeric(gpu.get("nvlink_rx", 0))}')
    
    # Process metrics
    for process in data['processes']:
        pid = process['pid']
        gpu_index = process['gpu_index']
        container_id = process.get('container_id') or 'Host'
        container_name = process.get('container_name') or 'Host'
        
        metrics.append(f'# HELP gpu_process_memory GPU memory used by a process in bytes')
        metrics.append(f'# TYPE gpu_process_memory gauge')
        metrics.append(f'gpu_process_memory{{pid="{pid}",gpu="{gpu_index}",container_id="{container_id}",container_name="{container_name}"}} {safe_numeric(process.get("gpu_memory", 0))}')
        
        # CPU использование процесса
        if 'cpu_percent' in process:
            metrics.append(f'# HELP process_cpu_percent CPU usage percentage by a process')
            metrics.append(f'# TYPE process_cpu_percent gauge')
            metrics.append(f'process_cpu_percent{{pid="{pid}",gpu="{gpu_index}",container_id="{container_id}",container_name="{container_name}"}} {safe_numeric(process.get("cpu_percent", 0))}')
        
        # Использование памяти хоста
        if 'host_memory' in process:
            metrics.append(f'# HELP process_host_memory Host memory used by a process in bytes')
            metrics.append(f'# TYPE process_host_memory gauge')
            metrics.append(f'process_host_memory{{pid="{pid}",gpu="{gpu_index}",container_id="{container_id}",container_name="{container_name}"}} {safe_numeric(process.get("host_memory", 0))}')
        
        # Время работы процесса
        if 'running_time' in process:
            metrics.append(f'# HELP process_running_time Process running time in seconds')
            metrics.append(f'# TYPE process_running_time gauge')
            metrics.append(f'process_running_time{{pid="{pid}",gpu="{gpu_index}",container_id="{container_id}",container_name="{container_name}"}} {safe_numeric(process.get("running_time", 0))}')
        
        # GPU загрузка процесса, если доступна
        if 'gpu_utilization' in process:
            metrics.append(f'# HELP process_gpu_utilization GPU utilization percentage by a process')
            metrics.append(f'# TYPE process_gpu_utilization gauge')
            metrics.append(f'process_gpu_utilization{{pid="{pid}",gpu="{gpu_index}",container_id="{container_id}",container_name="{container_name}"}} {safe_numeric(process.get("gpu_utilization", 0))}')
    
    # Container metrics
    for container_id, container_info in data['multi_gpu_containers'].items():
        metrics.append(f'# HELP container_gpu_count Number of GPUs used by a container')
        metrics.append(f'# TYPE container_gpu_count gauge')
        metrics.append(f'container_gpu_count{{container_id="{container_id}",container_name="{container_info["name"]}"}} {len(container_info["gpu_indices"])}')
    
    return '\n'.join(metrics)

# HTTP server for monitoring
class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in separate threads"""
    daemon_threads = True

class RequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for monitoring"""
    
    def do_GET(self):
        """Handle GET requests"""
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        
        # Get data
        data = collect_data()
        
        # Handle different endpoints
        if path == '/' or path == '/index.html':
            # HTML page
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(generate_html(data).encode('utf-8'))
        
        elif path == '/metrics':
            # Prometheus metrics
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(generate_prometheus_metrics(data).encode('utf-8'))
        
        elif path == '/api/data.json':
            # JSON API
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(data).encode('utf-8'))
        
        else:
            # Page not found
            self.send_response(404)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(b"<html><body><h1>404 Not Found</h1></body></html>")
    
    def log_message(self, format, *args):
        """Override request logging"""
        logger.info(f"{self.client_address[0]} - {format % args}")

def main():
    """Main program function"""
    logger.info(f"Starting Proxmox-GML (GPU Monitoring for LXC) server on port {PORT}")
    
    # Check if nvitop is working
    try:
        test_script = "import nvitop; print('nvitop is working')"
        output = run_nvitop_script(test_script)
        if not output or "nvitop is working" not in output:
            logger.error("Failed to import nvitop. Check your installation.")
            sys.exit(1)
        logger.info("nvitop is working correctly")
    except Exception as e:
        logger.error(f"Error checking nvitop: {e}")
        sys.exit(1)
    
    # Start web server
    try:
        server = ThreadedHTTPServer(('0.0.0.0', PORT), RequestHandler)
        logger.info(f"Server started at http://0.0.0.0:{PORT}/")
        logger.info(f"Prometheus metrics available at http://0.0.0.0:{PORT}/metrics")
        logger.info(f"JSON API available at http://0.0.0.0:{PORT}/api/data.json")
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server stopped")
        server.server_close()
    except Exception as e:
        logger.error(f"Error starting server: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
