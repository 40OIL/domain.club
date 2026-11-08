# -*- coding: cp1251 -*-
import os
import sys
import time
import asyncio
import logging
import random
import shutil
import socket
import ipaddress
import platform
import re
import socks
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs
from logging.handlers import RotatingFileHandler

# Исправление бага Event Loop в Python 3.8 на Windows
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from telethon import TelegramClient, connection
from telethon.errors import (
    FloodWaitError, AuthKeyError, SessionRevokedError, 
    UserDeactivatedBanError, PhoneNumberBannedError, SessionPasswordNeededError
)
from telethon.network.connection import ConnectionTcpObfuscated

try:
    from ruamel.yaml import YAML
except ImportError:
    YAML = None

# ============================================================
# БАЗОВЫЕ ПУТИ И КОНСТАНТЫ
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "proxy_test.log"
LOCK_FILE = BASE_DIR / "proxy_test.lock"

API_ID = 21496880
API_HASH = '5f48a51682c865f8a64c962481d3d76d'
SESSION_NAME = 'telegram_session' 
SESSION_FILE = BASE_DIR / f"{SESSION_NAME}.session"
PHONE = '+79127284081'          # ВАШ номер в межд. формате (для первичной авторизации)
DEVICE_MODEL = 'BookDownloader'    # Имя, которое будет видно в "Активных сеансах" Telegram

# --- ПАРАМЕТРЫ ПОИСКА ---
# Номер телефона ЦЕЛЕВОГО чата, в котором ищем файл (с плюсом)
TARGET_PHONE = '+79226630393'  
TARGET_FILE_NAME = '3. Стратегическая необходимость.fb2'

# Таймауты (CONNECT_TIMEOUT уменьшен для быстрых отбраковок)
CONNECT_TIMEOUT = 15.0
PRECHECK_TIMEOUT = 5.0
SEARCH_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 60.0
DOWNLOAD_RETRIES = 2
REPLACE_RETRIES = 5
REPLACE_BASE_DELAY = 0.7

# Анти-бан задержки
DELAY_MIN = 1.5
DELAY_MAX = 5.0
FLOOD_MAX_SLEEP = 30.0

# Разрешенные порты для 127.0.0.1
ALLOWED_LOOPBACK_PORTS = {1080, 2080, 1443, 4545, 9050, 2090}

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'telegram_media_downloader' / 'config.yaml'
PROXIES_PATH = BASE_DIR / 'proxies.txt'
PROXIES_ENCODING = 'cp1251'

_LOCK_HANDLE = None
logger = logging.getLogger("proxy_test")

# ============================================================
# ЛОГИРОВАНИЕ И МАСКИРОВАНИЕ СЕКРЕТОВ
# ============================================================
USERPASS_RE = re.compile(r"((?:socks[45]|http|https)://[^:/@]+:)[^@]+(@)", re.IGNORECASE)
MTPROTO_SECRET_RE = re.compile(r"(mtproto://[^:/]+:\d+:)\S+", re.IGNORECASE)
KV_SECRET_RE = re.compile(r"(?i)(password|pass|secret|api_hash)(\s*[:=]\s*)\S+")

class RedactingFormatter(logging.Formatter):
    """Маскирует пароли и секреты в логах, оставляя адреса и порты."""
    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        msg = USERPASS_RE.sub(r"\1***\2", msg)
        msg = MTPROTO_SECRET_RE.sub(r"\1***", msg)
        msg = KV_SECRET_RE.sub(r"\1\2***", msg)
        return msg

def setup_logging(level: int = logging.INFO) -> None:
    """Настраивает логирование с ротацией и выводом в консоль."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    formatter = RedactingFormatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")

    fh = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
    fh.setLevel(level)
    fh.setFormatter(formatter)
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(formatter)
    root.addHandler(ch)

    # Не глушим ошибки Telethon, а пишем их в лог
    logging.getLogger('telethon').setLevel(logging.WARNING)
    logging.captureWarnings(True)

# ============================================================
# LOCK-ФАЙЛ (ЗАЩИТА ОТ ПАРАЛЛЕЛЬНОГО ЗАПУСКА)
# ============================================================
def acquire_lock(lock_path: Path) -> bool:
    global _LOCK_HANDLE
    try:
        _LOCK_HANDLE = open(lock_path, "a+b")
        if _LOCK_HANDLE.seek(0, os.SEEK_END) == 0:
            _LOCK_HANDLE.write(b"1")
            _LOCK_HANDLE.flush()
        _LOCK_HANDLE.seek(0)
        if platform.system() == "Windows":
            import msvcrt
            msvcrt.locking(_LOCK_HANDLE.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_LOCK_HANDLE.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        if _LOCK_HANDLE:
            _LOCK_HANDLE.close()
            _LOCK_HANDLE = None
        return False

def release_lock() -> None:
    global _LOCK_HANDLE
    if not _LOCK_HANDLE: return
    try:
        _LOCK_HANDLE.seek(0)
        if platform.system() == "Windows":
            import msvcrt
            msvcrt.locking(_LOCK_HANDLE.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        _LOCK_HANDLE.close()
        _LOCK_HANDLE = None

# ============================================================
# ПОДАВЛЕНИЕ СЛУЖЕБНЫХ ОШИБОК ASYNCIO (WINDOWS)
# ============================================================
def _silent_exception_handler(loop, context):
    exc = context.get('exception')
    if exc and isinstance(exc, OSError) and getattr(exc, 'winerror', None) == 10038:
        return
    if '_call_connection_lost' in str(context.get('handle', '')):
        return
    if exc and isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
        return
    loop.default_exception_handler(context)

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ПРОКСИ
# ============================================================
def get_proxy_endpoint(proxy_info: Dict[str, Any]) -> Tuple[Optional[str], Optional[int]]:
    args = proxy_info.get("args", {})
    proxy = args.get("proxy")
    if not proxy: return None, None
    p_type = proxy_info.get("type")
    try:
        if p_type == "MTPROTO":
            return str(proxy[0]), int(proxy[1])
        return str(proxy[1]), int(proxy[2])
    except Exception:
        return None, None

def safe_proxy_label(proxy_info: Dict[str, Any]) -> str:
    """Возвращает безопасную метку для лога (без паролей)."""
    p_type = str(proxy_info.get("type", "UNKNOWN"))
    host, port = get_proxy_endpoint(proxy_info)
    if host and port:
        return f"{p_type} {host}:{port}"
    return p_type

def host_is_loopback(host: str) -> bool:
    host = str(host).strip().lower().strip("[]")
    if host in {"127.0.0.1", "::1", "localhost"}: return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

def validate_proxy_endpoint(host: Optional[str], port: Optional[int]) -> Tuple[bool, Optional[str]]:
    """Проверяет хост и порт на безопасность."""
    if not host: return False, "empty host"
    host_clean = str(host).strip().lower().strip("[]")
    if not host_clean or any(ch.isspace() for ch in host_clean): return False, "invalid host"
    try:
        port_int = int(port)
    except Exception:
        return False, "invalid port"
    if not (1 <= port_int <= 65535): return False, "port out of range"
    
    if host_is_loopback(host_clean) and port_int not in ALLOWED_LOOPBACK_PORTS:
        return False, f"loopback port not allowed; allowed={sorted(ALLOWED_LOOPBACK_PORTS)}"
    return True, None

def parse_proxy_line(line: str):
    """Парсит строку прокси разных форматов."""
    line = line.strip()
    if not line or line.startswith('#'):
        return None

    if line.lower() == 'direct':
        return {
            'type': 'DIRECT', 'args': {'connection': ConnectionTcpObfuscated},
            'output_format': 'direct', 'normalized_key': 'direct', 'raw': None
        }

    if line.startswith('https://t.me/') or line.startswith('http://t.me/'):
        parsed_url = urlparse(line)
        query = parse_qs(parsed_url.query)
        if parsed_url.path in ['/socks', '/socks5']:
            host = query.get('server', [None])[0]
            port = query.get('port', [None])[0]
            user = query.get('user', [None])[0]
            password = query.get('pass', [None])[0]
            if not host or not port: return None
            proxy_tuple = (socks.SOCKS5, host, int(port), True, user, password)
            output_format = f"socks5://{user}:{password}@{host}:{port}" if user and password else f"socks5://{host}:{port}"
            norm_key = f"socks5|{user or ''}|{password or ''}|{host.lower()}|{port}"
            return {
                'type': 'SOCKS5', 'args': {'proxy': proxy_tuple, 'connection': ConnectionTcpObfuscated},
                'output_format': output_format, 'normalized_key': norm_key,
                'raw': {'scheme': 'socks5', 'hostname': host, 'port': int(port), 'username': user, 'password': password}
            }
        elif parsed_url.path == '/proxy':
            host = query.get('server', [None])[0]
            port = query.get('port', [None])[0]
            secret = query.get('secret', [None])[0]
            if not host or not port or not secret: return None
            output_format = f"mtproto://{host}:{port}:{secret}"
            norm_key = f"mtproto|{host.lower()}|{port}|{secret}"
            return {
                'type': 'MTPROTO', 'args': {'connection': connection.ConnectionTcpMTProxyRandomizedIntermediate, 'proxy': (host, int(port), secret)},
                'output_format': output_format, 'normalized_key': norm_key, 'raw': None
            }
        return None

    try:
        if line.startswith('mtproto://'):
            body = line[len('mtproto://'):]
            host, port, secret = body.split(':')
            norm_key = f"mtproto|{host.lower()}|{port}|{secret}"
            return {
                'type': 'MTPROTO', 'args': {'connection': connection.ConnectionTcpMTProxyRandomizedIntermediate, 'proxy': (host, int(port), secret)},
                'output_format': line, 'normalized_key': norm_key, 'raw': None
            }

        for p_type in ['socks5://', 'socks4://', 'http://']:
            if line.startswith(p_type):
                type_map = {'socks5://': socks.SOCKS5, 'socks4://': socks.SOCKS4, 'http://': socks.HTTP}
                body = line[len(p_type):]
                user, password = None, None
                if '@' in body:
                    auth_part, server_part = body.split('@', 1)
                    user, password = auth_part.split(':', 1)
                else:
                    server_part = body
                host, port = server_part.split(':', 1)
                p_type_clean = p_type.replace('://', '').lower()
                norm_key = f"{p_type_clean}|{user or ''}|{password or ''}|{host.lower()}|{port}"
                return {
                    'type': p_type_clean.upper(),
                    'args': {'proxy': (type_map[p_type], host, int(port), True, user, password), 'connection': ConnectionTcpObfuscated},
                    'output_format': line, 'normalized_key': norm_key,
                    'raw': {'scheme': p_type_clean, 'hostname': host, 'port': int(port), 'username': user, 'password': password}
                }
    except Exception:
        return None
    return None

# ============================================================
# ПЕРВИЧНАЯ АВТОРИЗАЦИЯ (ОСТАВЛЕНА ДЛЯ РУЧНОГО ВЫЗОВА)
# ============================================================
async def authorize_client(client: TelegramClient) -> bool:
    """Первичная авторизация (запрос кода и 2FA). В тестах не используется."""
    loop = asyncio.get_running_loop()
    try:
        await client.send_code_request(PHONE)
    except FloodWaitError as fw:
        logger.warning("FloodWait при запросе кода: %d сек", fw.seconds)
        await asyncio.sleep(min(fw.seconds, 300))
        try: await client.send_code_request(PHONE)
        except Exception as e: logger.error("Не удалось запросить код: %s", e); return False
    except Exception as e:
        logger.error("Не удалось запросить код: %s", e); return False

    try: code = await loop.run_in_executor(None, input, "Введите код из Telegram: ")
    except Exception: return False

    try:
        await client.sign_in(PHONE, code.strip())
    except SessionPasswordNeededError:
        logger.info("Требуется пароль 2FA.")
        try: pwd = await loop.run_in_executor(None, input, "Введите пароль 2FA: ")
        except Exception: return False
        try: await client.sign_in(password=pwd.strip())
        except Exception as e: logger.error("Ошибка пароля 2FA: %s", e); return False
    except Exception as e:
        logger.error("Ошибка авторизации: %s", e); return False

    return await client.is_user_authorized()

# ============================================================
# ПРОВЕРКА TCP И ЗАДЕРЖКИ
# ============================================================
async def precheck_proxy(proxy_info: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    if proxy_info.get('type') == 'DIRECT': return True, None
    host, port = get_proxy_endpoint(proxy_info)
    if not host or not port: return False, "no proxy endpoint"
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=PRECHECK_TIMEOUT)
        writer.close()
        await writer.wait_closed()
        return True, None
    except asyncio.TimeoutError: return False, "tcp precheck timeout"
    except OSError as exc: return False, f"tcp precheck failed: {exc.__class__.__name__}"

async def random_delay(min_s: float, max_s: float) -> None:
    delay = random.uniform(min_s, max_s)
    logger.debug("Случайная задержка: %.2f сек", delay)
    await asyncio.sleep(delay)

# ============================================================
# РАБОТА С ФАЙЛАМИ (БЭКАПЫ И АТОМАРНАЯ ЗАПИСЬ)
# ============================================================
def backup_file(path: Path) -> Optional[Path]:
    if not path.exists(): return None
    backup_path = path.with_name(path.name + ".bak")
    try:
        shutil.copy2(path, backup_path)
        logger.info("Создан бэкап: %s", backup_path)
        return backup_path
    except OSError:
        logger.exception("Ошибка создания бэкапа %s", path)
        return None

def replace_with_retry(src: Path, dst: Path) -> None:
    """Заменяет dst файлом src с ретраями на случай WinError 5.

    Args:
        src: временный файл-источник.
        dst: целевой файл.

    Raises:
        OSError: если не удалось заменить даже fallback-способом.
    """
    last_exc: Optional[OSError] = None

    for attempt in range(1, REPLACE_RETRIES + 1):
        try:
            # Сброс атрибута "только чтение", если он вдруг стоит на цели.
            if dst.exists():
                try:
                    os.chmod(dst, stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last_exc = exc
            delay = REPLACE_BASE_DELAY * attempt
            logger.warning(
                "Replace %s -> %s не удался (попытка %d/%d): %s. Повтор через %.1f с",
                src, dst, attempt, REPLACE_RETRIES, exc, delay,
            )
            time.sleep(delay)

    # Fallback: удаляем цель и переименовываем.
    # Безопасно, т.к. перед этим всегда создаётся .bak.
    try:
        if dst.exists():
            dst.unlink()
        os.rename(src, dst)
        logger.warning("Замена выполнена fallback-способом (unlink+rename): %s", dst)
    except OSError:
        logger.exception("Fallback-замена не удалась; временный файл сохранён: %s", src)
        if last_exc:
            raise last_exc


def atomic_write_text(path: Path, text: str, encoding: str) -> None:
    """Атомарно пишет текстовый файл через .tmp + replace_with_retry.

    Args:
        path: целевой путь.
        text: содержимое.
        encoding: кодировка записи.
    """
    tmp_path = path.with_name(path.name + ".tmp")
    with open(tmp_path, "w", encoding=encoding, errors="replace") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    replace_with_retry(tmp_path, path)

# ============================================================
# ЗАГРУЗКА ФАЙЛА С RETRY
# ============================================================
async def download_with_retry(client, message, timeout=DOWNLOAD_TIMEOUT, retries=DOWNLOAD_RETRIES):
    deadline = time.perf_counter() + timeout
    for attempt in range(1, retries + 1):
        remaining = deadline - time.perf_counter()
        if remaining <= 0: return None, 0.0, False, "deadline exceeded"
        start_time = time.perf_counter()
        try:
            file_bytes = await asyncio.wait_for(client.download_media(message.media, file=bytes), timeout=remaining)
            duration = time.perf_counter() - start_time
            if file_bytes: return file_bytes, duration, True, None
            return None, duration, False, "empty download"
        except asyncio.TimeoutError:
            logger.warning("  [Попытка %d/%d] Таймаут скачивания.", attempt, retries)
            return None, 0.0, False, "download timeout"
        except FloodWaitError: raise
        except (ConnectionError, OSError) as e:
            if attempt < retries:
                logger.warning("  [Попытка %d/%d] Обрыв (%s). Ретрай...", attempt, retries, e.__class__.__name__)
                await asyncio.sleep(random.uniform(0.3, 0.9))
                continue
            logger.exception("  [Попытка %d/%d] Обрыв, попытки исчерпаны.", attempt, retries)
            return None, 0.0, False, f"network error: {e.__class__.__name__}"
        except Exception as e:
            logger.exception("  [Попытка %d/%d] Непредвиденная ошибка.", attempt, retries)
            return None, 0.0, False, f"unexpected: {e.__class__.__name__}"
    return None, 0.0, False, "download failed"

# ============================================================
# ТЕСТ ОДНОГО ПРОКСИ
# ============================================================
async def test_single_proxy(proxy_info, index, total):
    label = safe_proxy_label(proxy_info)
    p_type = proxy_info['type']
    
    result = {
        'output_format': proxy_info.get('output_format', 'unknown'),
        'type': p_type, 'raw': proxy_info.get('raw'),
        'speed': -1.0, 'conn_time': 99999.0,
        'status': 'unknown', 'error': None, 'safe_label': label,
    }
    
    logger.info("[Тест %d/%d] %s", index, total, label)
    
    ok, err = await precheck_proxy(proxy_info)
    if not ok:
        result['status'] = 'precheck_failed'
        result['error'] = err
        logger.warning("Precheck failed: %s", err)
        return result

    client_kwargs = {
        'session': SESSION_NAME, 'api_id': API_ID, 'api_hash': API_HASH,
        'timeout': CONNECT_TIMEOUT, 'connection_retries': 1, 'retry_delay': 1,
        'device_model': DEVICE_MODEL, 'system_version': 'Windows 10',
        'app_version': '1.0', 'lang_code': 'ru',
    }
    client_kwargs.update(proxy_info['args'])
    client = TelegramClient(**client_kwargs)

    try:
        start_conn = time.perf_counter()
        await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT + 2.0)
        conn_time = (time.perf_counter() - start_conn) * 1000
        result['conn_time'] = conn_time

        if not await client.is_user_authorized():
            result['status'] = 'unauthorized'
            result['error'] = 'session not authorized'
            logger.error("Сессия не авторизована!")
            return result
            
        await client.get_me() # Проверка живости сессии
        logger.info("Подключено! Отклик (TCP): %.1f мс", conn_time)

        async def find_message():
            async for message in client.iter_messages(TARGET_PHONE, search=TARGET_FILE_NAME):
                if message.media and message.file and message.file.name == TARGET_FILE_NAME:
                    return message
            return None

        target_message = await asyncio.wait_for(find_message(), timeout=SEARCH_TIMEOUT)
        if not target_message:
            result['status'] = 'file_not_found'
            result['speed'] = 0.0
            logger.warning("Файл '%s' не найден.", TARGET_FILE_NAME)
            return result

        file_size_mb = target_message.file.size / 1024 / 1024
        logger.info("Файл найден. Размер: %.2f MB.", file_size_mb)

        file_bytes, duration, success, dl_err = await download_with_retry(client, target_message)
        if not success or not file_bytes:
            result['status'] = 'download_failed'
            result['error'] = dl_err
            logger.warning("Скачивание не удалось: %s", dl_err)
            return result

        speed_mbps = (len(file_bytes) * 8) / (1024 * 1024) / duration if duration > 0 else 0.0
        result['speed'] = speed_mbps
        result['status'] = 'ok'
        logger.info("Результат: %.2f Mbps (%.2f сек)", speed_mbps, duration)
        return result

    except asyncio.TimeoutError:
        result['status'] = 'timeout'
        result['error'] = 'operation timeout'
        logger.warning("Таймаут операции.")
        return result
    except FloodWaitError as e:
        result['status'] = 'flood_wait'
        result['error'] = f"flood_wait:{e.seconds}"
        logger.warning("FloodWait: %d сек.", e.seconds)
        return result
    except (AuthKeyError, SessionRevokedError) as e:
        result['status'] = 'unauthorized'
        result['error'] = e.__class__.__name__
        logger.error("Сессия недействительна: %s", e.__class__.__name__)
        return result
    except (UserDeactivatedBanError, PhoneNumberBannedError) as e:
        result['status'] = 'banned'
        result['error'] = e.__class__.__name__
        logger.error("Аккаунт забанен: %s", e.__class__.__name__)
        return result
    except (ConnectionError, OSError):
        result['status'] = 'network_error'
        result['error'] = 'network error'
        logger.exception("Ошибка сети.")
        return result
    except Exception:
        result['status'] = 'unexpected_error'
        result['error'] = 'unexpected error'
        logger.exception("Непредвиденная ошибка.")
        return result
    finally:
        try:
            if client.is_connected(): await client.disconnect()
        except Exception: pass

# ============================================================
# ОБНОВЛЕНИЕ CONFIG.YAML (ЧЕРЕЗ RUAMEL.YAML)
# ============================================================
def get_fastest_standard_proxy(results):
    for res in results:
        if res.get('status') != 'ok' or res.get('speed', -1.0) <= 0: continue
        if res.get('type') in ('SOCKS4', 'SOCKS5', 'HTTP') and res.get('raw'): return res
    return None

def normalize_proxy(d):
    if not d: return {}
    return {
        'scheme': str(d.get('scheme') or '').lower().strip(),
        'hostname': str(d.get('hostname') or '').strip(),
        'port': str(d.get('port') or '').strip(),
        'username': str(d.get('username') or '').strip(),
        'password': str(d.get('password') or '').strip(),
    }

def update_config_yaml(new_raw: dict):
    if YAML is None:
        logger.error("ruamel.yaml не установлен. config.yaml не обновлён.")
        return
    if not CONFIG_PATH.exists():
        logger.warning("config.yaml не найден: %s", CONFIG_PATH)
        return
        
    yaml = YAML()
    yaml.preserve_quotes = True
    
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = yaml.load(f)
        if data is None: data = {}
        
        proxy = data.get('proxy')
        if proxy is None or not hasattr(proxy, '__setitem__'):
            data['proxy'] = {}
            proxy = data['proxy']
            
        if normalize_proxy(proxy) == normalize_proxy(new_raw):
            logger.info("Лучший прокси уже прописан в конфиге.")
            return
            
        backup = backup_file(CONFIG_PATH)
        if not backup:
            logger.error("Бэкап config.yaml не удался. Обновление прервано.")
            return
            
        proxy['scheme'] = str(new_raw['scheme'])
        proxy['hostname'] = str(new_raw['hostname'])
        proxy['port'] = int(new_raw['port'])
        
        if new_raw.get('username'): proxy['username'] = str(new_raw['username'])
        else: proxy.pop('username', None)
            
        if new_raw.get('password'): proxy['password'] = str(new_raw['password'])
        else: proxy.pop('password', None)
        
        data['proxy'] = proxy
        
        tmp_path = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
        with open(tmp_path, 'w', encoding='utf-8') as f:
            yaml.dump(data, f)
            
        validator = YAML()
        with open(tmp_path, 'r', encoding='utf-8') as f:
            loaded = validator.load(f)
        if not loaded or 'proxy' not in loaded:
            raise ValueError("Validation failed: proxy key missing")
            
        replace_with_retry(tmp_path, CONFIG_PATH)
        logger.info("config.yaml обновлён: %s://%s:%s", new_raw['scheme'], new_raw['hostname'], new_raw['port'])
        
    except Exception:
        logger.exception("Ошибка обновления config.yaml")
        if 'tmp_path' in locals() and tmp_path.exists():
            try: tmp_path.unlink()
            except OSError: pass

# ============================================================
# MAIN
# ============================================================
async def main():
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_silent_exception_handler)
    
    logger.info("Скрипт запущен.")
    logger.info("Путь к config.yaml: %s", CONFIG_PATH)
    
    if not SESSION_FILE.exists():
        logger.critical("Файл сессии '%s' не найден. Тестирование невозможно. Выход.", SESSION_FILE)
        return
        
    if not PROXIES_PATH.exists():
        logger.critical("Файл proxies.txt не найден!")
        return
        
    try:
        with open(PROXIES_PATH, 'r', encoding=PROXIES_ENCODING, errors='replace') as f:
            lines = f.readlines()
    except Exception:
        logger.exception("Ошибка чтения proxies.txt")
        return
        
    seen_proxies = {}
    unique_proxy_list = []
    duplicates_count = 0
    
    for line_no, line in enumerate(lines, start=1):
        original_line = line.strip()
        if not original_line or original_line.startswith('#'): continue
        
        try:
            parsed = parse_proxy_line(original_line)
        except Exception:
            logger.exception("Ошибка парсинга строки %d", line_no)
            continue
            
        if not parsed: continue
        
        if parsed['type'] != 'DIRECT':
            host, port = get_proxy_endpoint(parsed)
            ok, reason = validate_proxy_endpoint(host, port)
            if not ok:
                logger.warning("Строка %d отклонена валидатором: %s", line_no, reason)
                continue
                
        key = parsed['normalized_key']
        if key in seen_proxies:
            duplicates_count += 1
            logger.debug("Дубликат на строке %d", line_no)
        else:
            seen_proxies[key] = parsed['output_format']
            unique_proxy_list.append(parsed)
            
    if duplicates_count > 0:
        logger.info("Дедупликация: найдено %d, оставлено %d", duplicates_count, len(unique_proxy_list))
        
    if not unique_proxy_list:
        logger.warning("Нет валидных прокси для проверки.")
        return
        
    logger.info("Загружено %d уникальных прокси. Начинаем тест...", len(unique_proxy_list))
    
    results = []
    completed_all = True
    
    for idx, proxy_info in enumerate(unique_proxy_list, start=1):
        res = await test_single_proxy(proxy_info, idx, len(unique_proxy_list))
        results.append(res)
        
        if res.get('status') in ('unauthorized', 'banned'):
            logger.critical("Сессия недействительна или аккаунт забанен. Остановка тестов.")
            completed_all = False
            break
            
        if idx < len(unique_proxy_list):
            await random_delay(DELAY_MIN, DELAY_MAX)
            
    if not results: return
    
    logger.info("Сортировка результатов...")
    results.sort(key=lambda x: (x['speed'] >= 0, x['speed'], -x['conn_time']), reverse=True)
    
    if completed_all:
        if PROXIES_PATH.exists():
            backup = backup_file(PROXIES_PATH)
            if backup:
                text = "\n".join(str(r.get('output_format', '')) for r in results if r.get('output_format')) + "\n"
                try:
                    atomic_write_text(PROXIES_PATH, text, PROXIES_ENCODING)
                    logger.info("proxies.txt перезаписан атомарно.")
                except Exception:
                    logger.exception("Ошибка записи proxies.txt")
                    
        fastest = get_fastest_standard_proxy(results)
        if fastest and fastest.get('raw'):
            update_config_yaml(fastest['raw'])
        else:
            logger.info("Подходящий socks4/socks5/http прокси не найден.")
            
    logger.info("--- Итоги ---")
    for res in results:
        if res.get('status') == 'ok':
            status_text = f"{res.get('speed', 0.0):.2f} Mbps"
        else:
            status_text = str(res.get('status', 'error'))
        logger.info("[%s] %s", status_text, res.get('safe_label', 'unknown'))

def main_sync() -> int:
    setup_logging(logging.INFO)
    if not acquire_lock(LOCK_FILE):
        logger.error("Запущена другая копия скрипта. Выход.")
        return 1
    try:
        asyncio.run(main())
        return 0
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем.")
        return 1
    except Exception:
        logger.exception("Фатальная ошибка.")
        return 1
    finally:
        release_lock()

if __name__ == '__main__':
    sys.exit(main_sync())