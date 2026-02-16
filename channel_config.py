"""
Local Catchup Plugin - Per-Channel Configuration

Persists enabled/disabled state for local catchup per channel in a JSON file.
Thread-safe via threading.Lock.

File format: {"42": true, "108": true, "215": false}
"""

import json
import logging
import os
import threading

logger = logging.getLogger("plugins.dispatcharr_local_catchup.channel_config")

_lock = threading.RLock()
_config_path = None
_cache = None


def _get_config_path():
    """Get path to channels.json, auto-detecting plugin directory."""
    global _config_path
    if _config_path:
        return _config_path

    # Default: alongside this file
    _config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "channels.json")
    return _config_path


def set_config_path(path):
    """Override the config file path (useful for testing)."""
    global _config_path, _cache
    with _lock:
        _config_path = path
        _cache = None


def load():
    """Load channel config from disk. Returns dict of {channel_id_str: bool}."""
    global _cache
    with _lock:
        path = _get_config_path()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    _cache = json.load(f)
            else:
                _cache = {}
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"[LocalCatchup] Failed to load channels.json: {e}")
            _cache = {}
        return dict(_cache)


def save(data):
    """Save channel config to disk."""
    global _cache
    with _lock:
        path = _get_config_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            _cache = dict(data)
        except IOError as e:
            logger.error(f"[LocalCatchup] Failed to save channels.json: {e}")


def is_enabled(channel_id):
    """Check if local catchup is enabled for a channel."""
    global _cache
    with _lock:
        if _cache is None:
            load()
        return bool((_cache or {}).get(str(channel_id), False))


def set_enabled(channel_id, enabled):
    """Enable or disable local catchup for a channel."""
    data = load()
    data[str(channel_id)] = bool(enabled)
    save(data)


def get_all_enabled():
    """Return list of channel IDs (as integers) where local catchup is enabled."""
    data = load()
    result = []
    for ch_id, enabled in data.items():
        if enabled:
            try:
                result.append(int(ch_id))
            except (ValueError, TypeError):
                pass
    return result


def remove_channel(channel_id):
    """Remove a channel from the config entirely."""
    data = load()
    data.pop(str(channel_id), None)
    save(data)
