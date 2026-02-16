"""
Dispatcharr Local Catchup Plugin

Continuously records selected channels locally and serves those recordings
for catchup/timeshift playback. Enables catchup for channels even when the
IPTV provider doesn't support it.

AUTO-INSTALL ON STARTUP:
    This module auto-installs hooks when loaded if the plugin is enabled.
    Dispatcharr's PluginManager imports this module on startup, triggering
    the auto-install code at the bottom of this file.

    IMPORTANT - uWSGI Multi-Worker Architecture:
    Dispatcharr runs with multiple uWSGI workers (separate processes).
    Each worker has its own memory space, so hooks must be installed
    in EACH worker independently.
"""

import logging
import os

logger = logging.getLogger("plugins.dispatcharr_local_catchup")

_DEFAULT_STORAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'catchup')

# Track if hooks are installed in THIS worker
_hooks_installed = False


def _clear_pycache():
    """Delete stale bytecode cache to ensure fresh code is loaded after updates."""
    try:
        import shutil
        pycache = os.path.join(os.path.dirname(os.path.abspath(__file__)), '__pycache__')
        if os.path.isdir(pycache):
            shutil.rmtree(pycache, ignore_errors=True)
    except Exception:
        pass


def _auto_install_hooks():
    """Install hooks automatically on Django startup."""
    global _hooks_installed

    if _hooks_installed:
        return

    try:
        _clear_pycache()
        from .hooks import install_hooks
        if install_hooks():
            _hooks_installed = True
            logger.info("[LocalCatchup] Hooks installed (will check enabled state at runtime)")
            _auto_start_recorder()
    except Exception as e:
        logger.error(f"[LocalCatchup] Auto-install error: {e}")


def _auto_start_recorder():
    """Start the recording manager on startup if enabled and configured."""
    try:
        from apps.plugins.models import PluginConfig
        from .recorder import get_manager

        config = PluginConfig.objects.filter(key='dispatcharr_local_catchup').first()
        if not config or not config.enabled:
            return
        settings = config.settings or {}
        if not bool(settings.get('recorder_enabled', True)):
            return
        if not bool(settings.get('auto_start_recorder', True)):
            return

        manager = get_manager()
        result = manager.start(settings)
        if isinstance(result, dict):
            logger.info(f"[LocalCatchup] Auto-start recorder: {result.get('message')}")
    except Exception as e:
        logger.error(f"[LocalCatchup] Auto-start recorder error: {e}")

class Plugin:
    """
    Main plugin class for Dispatcharr Local Catchup.

    Dispatcharr's PluginManager calls run() with action IDs when the user
    clicks action buttons in the plugin UI.
    """

    def __init__(self):
        self.name = "Local Catchup"
        self.version = "1.0.0"
        self.description = (
            "Continuously records selected channels locally and serves "
            "recordings for catchup/timeshift playback"
        )
        self.author = "Dispatcharr Community"
        self.author_url = ""

        self.fields = [
            {
                "id": "storage_dir",
                "type": "string",
                "label": "Storage Directory",
                "default": _DEFAULT_STORAGE_DIR,
                "help_text": "Directory for recorded .ts files. Defaults to catchup/ inside the plugin folder."
            },
            {
                "id": "max_retention_days",
                "type": "number",
                "label": "Max Retention (days)",
                "default": 3,
                "help_text": "Delete recordings older than this many days"
            },
            {
                "id": "max_storage_gb",
                "type": "number",
                "label": "Max Storage (GB)",
                "default": 50,
                "help_text": "Maximum total storage in GB. Oldest recordings deleted first when limit is reached."
            },
            {
                "id": "timezone",
                "type": "select",
                "label": "Timezone",
                "default": "UTC",
                "options": [
                    {"value": "UTC", "label": "UTC"},
                    {"value": "Europe/Amsterdam", "label": "Europe/Amsterdam (CET)"},
                    {"value": "Europe/Athens", "label": "Europe/Athens (EET)"},
                    {"value": "Europe/Berlin", "label": "Europe/Berlin (CET)"},
                    {"value": "Europe/Brussels", "label": "Europe/Brussels (CET)"},
                    {"value": "Europe/Bucharest", "label": "Europe/Bucharest (EET)"},
                    {"value": "Europe/Budapest", "label": "Europe/Budapest (CET)"},
                    {"value": "Europe/Copenhagen", "label": "Europe/Copenhagen (CET)"},
                    {"value": "Europe/Dublin", "label": "Europe/Dublin (GMT/IST)"},
                    {"value": "Europe/Helsinki", "label": "Europe/Helsinki (EET)"},
                    {"value": "Europe/Istanbul", "label": "Europe/Istanbul (TRT)"},
                    {"value": "Europe/Lisbon", "label": "Europe/Lisbon (WET)"},
                    {"value": "Europe/London", "label": "Europe/London (GMT/BST)"},
                    {"value": "Europe/Madrid", "label": "Europe/Madrid (CET)"},
                    {"value": "Europe/Moscow", "label": "Europe/Moscow (MSK)"},
                    {"value": "Europe/Oslo", "label": "Europe/Oslo (CET)"},
                    {"value": "Europe/Paris", "label": "Europe/Paris (CET)"},
                    {"value": "Europe/Prague", "label": "Europe/Prague (CET)"},
                    {"value": "Europe/Rome", "label": "Europe/Rome (CET)"},
                    {"value": "Europe/Stockholm", "label": "Europe/Stockholm (CET)"},
                    {"value": "Europe/Vienna", "label": "Europe/Vienna (CET)"},
                    {"value": "Europe/Warsaw", "label": "Europe/Warsaw (CET)"},
                    {"value": "Europe/Zurich", "label": "Europe/Zurich (CET)"},
                    {"value": "America/Anchorage", "label": "America/Anchorage (AKST)"},
                    {"value": "America/Chicago", "label": "America/Chicago (CST)"},
                    {"value": "America/Denver", "label": "America/Denver (MST)"},
                    {"value": "America/Los_Angeles", "label": "America/Los_Angeles (PST)"},
                    {"value": "America/New_York", "label": "America/New_York (EST)"},
                    {"value": "America/Toronto", "label": "America/Toronto (EST)"},
                    {"value": "America/Vancouver", "label": "America/Vancouver (PST)"},
                    {"value": "Asia/Dubai", "label": "Asia/Dubai (GST)"},
                    {"value": "Asia/Hong_Kong", "label": "Asia/Hong_Kong (HKT)"},
                    {"value": "Asia/Kolkata", "label": "Asia/Kolkata (IST)"},
                    {"value": "Asia/Shanghai", "label": "Asia/Shanghai (CST)"},
                    {"value": "Asia/Singapore", "label": "Asia/Singapore (SGT)"},
                    {"value": "Asia/Tokyo", "label": "Asia/Tokyo (JST)"},
                    {"value": "Australia/Melbourne", "label": "Australia/Melbourne (AEST)"},
                    {"value": "Australia/Sydney", "label": "Australia/Sydney (AEST)"},
                    {"value": "Pacific/Auckland", "label": "Pacific/Auckland (NZST)"},
                    {"value": "Pacific/Honolulu", "label": "Pacific/Honolulu (HST)"},
                ],
                "help_text": "Timezone for EPG/timestamp display and conversion"
            },
            {
                "id": "language",
                "type": "select",
                "label": "EPG Language",
                "default": "en",
                "options": [
                    {"value": "bg", "label": "Bulgarian"},
                    {"value": "cs", "label": "Czech"},
                    {"value": "da", "label": "Danish"},
                    {"value": "de", "label": "Deutsch"},
                    {"value": "el", "label": "Greek"},
                    {"value": "en", "label": "English"},
                    {"value": "es", "label": "Español"},
                    {"value": "fi", "label": "Finnish"},
                    {"value": "fr", "label": "Français"},
                    {"value": "hu", "label": "Hungarian"},
                    {"value": "it", "label": "Italiano"},
                    {"value": "nl", "label": "Nederlands"},
                    {"value": "no", "label": "Norwegian"},
                    {"value": "pl", "label": "Polish"},
                    {"value": "pt", "label": "Português"},
                    {"value": "ro", "label": "Romanian"},
                    {"value": "ru", "label": "Russian"},
                    {"value": "sv", "label": "Swedish"},
                    {"value": "tr", "label": "Turkish"},
                    {"value": "uk", "label": "Ukrainian"},
                ],
                "help_text": "Language code for EPG data (ISO 639-1)"
            },
            {
                "id": "debug_mode",
                "type": "boolean",
                "label": "Debug Mode",
                "default": False,
                "help_text": "Enable verbose logging for troubleshooting (check Dispatcharr logs)"
            },
            {
                "id": "auto_start_recorder",
                "type": "boolean",
                "label": "Auto-Start Recorder",
                "default": True,
                "help_text": "Start the recording daemon on Dispatcharr startup (single worker only)"
            },
        ]

        self.actions = [
            {
                "id": "start",
                "label": "Start Recording",
                "description": "Starts the background recording engine for all enabled channels",
            },
            {
                "id": "stop",
                "label": "Stop Recording",
                "description": "Gracefully stops all active recording threads",
            },
            {
                "id": "status",
                "label": "Recording Status",
                "description": "Shows active recordings, disk usage, and enabled channels",
            },
            {
                "id": "cleanup",
                "label": "Run Cleanup Now",
                "description": "Immediately runs retention cleanup (age + size based)",
            },
        ]

    def run(self, action=None, params=None, context=None):
        """Execute plugin action."""
        context = context or {}
        settings = context.get('settings', {})
        from apps.plugins.models import PluginConfig

        if action == "enable":
            logger.info("[LocalCatchup] Enabling plugin...")
            from .hooks import install_hooks
            if install_hooks():
                return {"status": "ok", "message": "Local Catchup plugin enabled"}
            return {"status": "error", "message": "Failed to install hooks"}

        elif action == "disable":
            logger.info("[LocalCatchup] Plugin disabled")
            # Stop recording if running
            try:
                config = PluginConfig.objects.filter(key='dispatcharr_local_catchup').first()
                if config:
                    cfg = config.settings or {}
                    cfg['recorder_enabled'] = False
                    config.settings = cfg
                    config.save(update_fields=['settings'])
                from .recorder import get_manager
                manager = get_manager()
                manager.stop()
            except Exception:
                pass
            return {"status": "ok", "message": "Local Catchup plugin disabled"}

        elif action == "start":
            try:
                config = PluginConfig.objects.filter(key='dispatcharr_local_catchup').first()
                if config:
                    cfg = config.settings or {}
                    cfg['recorder_enabled'] = True
                    config.settings = cfg
                    config.save(update_fields=['settings'])
            except Exception:
                pass
            from .recorder import get_manager
            manager = get_manager()
            return manager.start(settings)

        elif action == "stop":
            try:
                config = PluginConfig.objects.filter(key='dispatcharr_local_catchup').first()
                if config:
                    cfg = config.settings or {}
                    cfg['recorder_enabled'] = False
                    config.settings = cfg
                    config.save(update_fields=['settings'])
            except Exception:
                pass
            from .recorder import get_manager
            manager = get_manager()
            result = manager.stop()
            if isinstance(result, dict):
                result['message'] = "Stop requested. Recorder will shut down across workers."
                return result
            return {"status": "ok", "message": "Stop requested. Recorder will shut down across workers."}

        elif action == "status":
            from .recorder import get_manager
            manager = get_manager()
            return manager.status()

        elif action == "cleanup":
            from . import storage
            deleted = storage.run_cleanup(settings)
            return {
                "status": "ok",
                "message": f"Cleanup complete. {deleted or 0} files deleted."
            }

        return {"status": "error", "message": f"Unknown action: {action}"}

    def stop(self, context=None):
        """Called when plugin is being stopped (disable or shutdown)."""
        try:
            from .recorder import get_manager
            manager = get_manager()
            manager.stop()
        except Exception as e:
            logger.error(f"[LocalCatchup] Error during stop: {e}")


# Auto-install hooks when this module is imported (on Django startup)
try:
    import django
    if django.apps.apps.ready:
        _auto_install_hooks()
    else:
        from django.core.signals import request_finished

        def _on_first_request(sender, **kwargs):
            _auto_install_hooks()
            request_finished.disconnect(_on_first_request)

        request_finished.connect(_on_first_request)
except Exception:
    pass
