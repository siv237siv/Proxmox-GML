# Proxmox-GML (GPU Monitoring for LXC)

Система мониторинга использования GPU ресурсов в LXC контейнерах для среды Proxmox. Отслеживает, какие контейнеры используют ресурсы физических GPU NVIDIA и как эти ресурсы распределяются.

## Возможности

- Мониторинг всех доступных GPU NVIDIA в системе
- Отслеживание использования GPU в LXC контейнерах
- Идентификация контейнеров, использующих несколько GPU одновременно
- Визуализация данных через веб-интерфейс
- Предоставление метрик в формате Prometheus
- Доступ к данным через JSON API

## Отслеживаемые метрики

### Для GPU

- Загрузка GPU (utilization)
- Использование памяти GPU (память, занятая процессами)
- Температура GPU
- Энергопотребление
- Тактовые частоты (graphics_clock, memory_clock, sm_clock)
- Пропускная способность PCIe и NVLink (если доступно)
- Использование видеокодеров/декодеров

### Для процессов

- PID и команда процесса
- Имя и ID контейнера
- Используемая GPU память
- Загрузка GPU процессом
- Время работы процесса
- Использование CPU и памяти хоста

## Требования

- Python 3.6+
- Библиотека nvitop (устанавливается в виртуальное окружение)
- NVIDIA драйверы с поддержкой nvidia-smi
- Proxmox VE 8+ с LXC контейнерами
- Доступ к файлам /proc для идентификации контейнеров

## Установка

1. Создайте директорию для проекта и скопируйте файл скрипта:

```bash
mkdir -p /opt/proxmox-gml
cp proxmox_gml.py /opt/proxmox-gml/
chmod +x /opt/proxmox-gml/proxmox_gml.py
```
2. Создайте виртуальное окружение для nvitop:

```bash
mkdir -p /opt/nvitop-venv
python3 -m venv /opt/nvitop-venv
/opt/nvitop-venv/bin/pip install nvitop
```

3. Проверьте и, при необходимости, измените настройки в начале файла:
   - `PORT` - порт веб-сервера (по умолчанию 8001)
   - `NVITOP_VENV` - путь к виртуальному окружению с nvitop
   - `update_interval` - интервал обновления данных в секундах

## Запуск

### Ручной запуск

```bash
python3 /opt/proxmox-gml/proxmox_gml.py
```

### Настройка в качестве службы systemd

Создайте файл `/etc/systemd/system/proxmox-gml.service`:

```ini
[Unit]
Description=Proxmox-GML (GPU Monitoring for LXC)
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/proxmox-gml/proxmox_gml.py
WorkingDirectory=/opt/proxmox-gml
Restart=always
User=root
Group=root
Environment=PATH=/usr/bin:/bin:/usr/local/bin

[Install]
WantedBy=multi-user.target
```

Затем включите и запустите службу:

```bash
systemctl daemon-reload
systemctl enable proxmox-gml.service
systemctl start proxmox-gml.service
```

## Использование

После запуска сервера доступны следующие URL:

- `http://yourserver:8001/` - Веб-интерфейс мониторинга
- `http://yourserver:8001/metrics` - Метрики в формате Prometheus
- `http://yourserver:8001/api/data.json` - Данные в формате JSON

## Интеграция с Prometheus

Добавьте следующую конфигурацию в ваш `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: 'gpu_lxc_monitor'
    scrape_interval: 15s
    static_configs:
      - targets: ['yourserver:8001']
```

## Пример визуализации в Grafana

Сервис предоставляет следующие метрики для построения панелей в Grafana:

- `gpu_utilization` - Загрузка GPU в процентах
- `gpu_memory_used` - Использование памяти GPU в байтах
- `gpu_memory_percent` - Процент использования памяти GPU
- `gpu_temperature` - Температура GPU в градусах Цельсия
- `process_gpu_memory` - Использование памяти GPU процессом
- `container_gpu_count` - Количество GPU, используемых контейнером

## Решение проблем

1. Убедитесь, что драйверы NVIDIA установлены и работают:
   ```bash
   nvidia-smi
   ```

2. Проверьте, что библиотека nvitop правильно установлена:
   ```bash
   /opt/nvitop-venv/bin/python -c "import nvitop; print('nvitop installed successfully')"
   ```

3. Проверьте логи службы:
   ```bash
   journalctl -u gpu-lxc-monitor.service
   ```

## Особенности реализации

- Скрипт анализирует cgroup файлы процессов для определения LXC контейнеров
- Получает имена контейнеров из конфигурационных файлов Proxmox или через команду `pct`
- Данные обновляются с заданным интервалом (по умолчанию 5 секунд)
- Правильно идентифицирует индексы GPU для каждого процесса
- Отдельно отслеживает контейнеры, использующие несколько GPU одновременно

## Лицензия

[MIT](LICENSE)
