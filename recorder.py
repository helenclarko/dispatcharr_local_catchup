"""
Local Catchup Plugin - Recording Engine

Background recording engine with a supervisor thread and per-channel recording workers.

RecordingManager (singleton):
- Supervisor thread checks every 30 seconds for enabled channels
- Starts/stops RecordingWorker threads per channel based on EPG schedule
- Runs storage cleanup every hour

RecordingWorker (one thread per active channel):
- Streams channel content via HTTP GET
- Writes raw bytes to .ts.recording files
- Renames to .ts on program end
- Supports retry with exponential backoff and stream fallback
"""

import logging
import os
import threading
import time
import requests
from datetime import datetime, timedelta

from . import channel_config
from . import storage

logger = logging.getLogger("plugins.dispatcharr_local_catchup.recorder")

# Singleton instance
_manager = None
_manager_lock = threading.Lock()


def get_manager():
    """Get or create the singleton RecordingManager."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = RecordingManager()
        return _manager


class RecordingWorker:
    """Records a single channel's stream to a .ts file."""

    MAX_RETRIES = 3
    CHUNK_SIZE = 32768  # 32KB chunks
    NO_EPG_CHUNK_HOURS = 2  # Record in 2-hour chunks when no EPG

    def __init__(self, channel_id, channel_name, stream, program_info, storage_dir, settings):
        """
        Args:
            channel_id: Channel ID
            channel_name: Channel display name
            stream: Stream model object to record from
            program_info: dict with 'title', 'start_time', 'end_time' (or None for no EPG)
            storage_dir: Base storage directory
            settings: Plugin settings dict
        """
        self.channel_id = channel_id
        self.channel_name = channel_name
        self.stream = stream
        self.program_info = program_info
        self.storage_dir = storage_dir
        self.settings = settings
        self.debug = settings.get('debug_mode', False)

        self._stop_event = threading.Event()
        self._thread = None
        self._current_file = None
        self._final_path = None
        self._bytes_written = 0

    @property
    def program_key(self):
        """Unique key for this channel+program combination."""
        if self.program_info:
            start = self.program_info.get('start_time', '')
            return f"{self.channel_id}:{start}"
        return f"{self.channel_id}:no_epg"

    def start(self):
        """Start the recording worker thread."""
        self._thread = threading.Thread(
            target=self._run,
            name=f"recorder-ch{self.channel_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        """Signal the worker to stop gracefully."""
        self._stop_event.set()

    def is_alive(self):
        """Check if the worker thread is still running."""
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout=10):
        """Wait for the worker thread to finish."""
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self):
        """Main recording loop with retry logic."""
        try:
            self._record_with_retries()
        except Exception as e:
            logger.error(f"[LocalCatchup] Recorder ch{self.channel_id} fatal error: {e}")
        finally:
            self._finalize_file()

    def _record_with_retries(self):
        """Attempt to record, retrying on failure with exponential backoff."""
        retry_count = 0

        while not self._stop_event.is_set() and retry_count <= self.MAX_RETRIES:
            try:
                self._record_stream()
                return  # Clean exit (program ended or stop signal)
            except (requests.exceptions.RequestException, IOError) as e:
                retry_count += 1
                if retry_count > self.MAX_RETRIES:
                    logger.error(
                        f"[LocalCatchup] ch{self.channel_id}: Max retries exceeded, stopping"
                    )
                    return

                backoff = min(2 ** retry_count, 30)
                logger.warning(
                    f"[LocalCatchup] ch{self.channel_id}: Stream error ({e}), "
                    f"retry {retry_count}/{self.MAX_RETRIES} in {backoff}s"
                )
                self._stop_event.wait(backoff)

    def _record_stream(self):
        """Connect to stream and write chunks to file."""
        from django.utils import timezone as django_timezone

        # Build the stream URL
        url = self._get_stream_url()
        if not url:
            logger.error(f"[LocalCatchup] ch{self.channel_id}: Cannot build stream URL")
            return

        # Determine file path
        if self.program_info:
            start_time = self.program_info['start_time']
            title = self.program_info.get('title', 'unknown')
            end_time = self.program_info.get('end_time')
            end_time_compare = end_time
            # Normalize to configured timezone for filename alignment with catchup lookups
            try:
                from zoneinfo import ZoneInfo
                timezone_str = self.settings.get('timezone', 'UTC')
                local_tz = ZoneInfo(timezone_str)
                if django_timezone.is_aware(start_time):
                    start_time = start_time.astimezone(local_tz).replace(tzinfo=None)
                if end_time and django_timezone.is_aware(end_time):
                    end_time = end_time.astimezone(local_tz).replace(tzinfo=None)
            except Exception:
                local_tz = None
        else:
            local_tz = None
            end_time_compare = None
            # No EPG: use current time, record for NO_EPG_CHUNK_HOURS
            start_time = django_timezone.now()
            title = start_time.strftime("recording_%H-%M")
            end_time = start_time + timedelta(hours=self.NO_EPG_CHUNK_HOURS)
            end_time_compare = end_time

        file_path = storage.get_recording_path(
            self.storage_dir, self.channel_id, start_time, title
        )
        recording_path = file_path + ".recording"
        self._final_path = file_path

        # Create directory
        os.makedirs(os.path.dirname(recording_path), exist_ok=True)

        self._current_file = recording_path

        if self.debug:
            logger.info(
                f"[LocalCatchup] ch{self.channel_id}: Recording '{title}' -> {recording_path}"
            )

        # Get user agent from m3u_account
        user_agent = "dispatcharr/1.0"
        try:
            if hasattr(self.stream, 'm3u_account') and self.stream.m3u_account:
                ua = self.stream.m3u_account.get_user_agent()
                if ua:
                    user_agent = ua.user_agent
        except Exception:
            pass

        headers = {'User-Agent': user_agent}

        # Open HTTP connection and stream
        response = requests.get(url, headers=headers, stream=True, timeout=30)
        response.raise_for_status()

        try:
            with open(recording_path, 'ab') as f:
                for chunk in response.iter_content(chunk_size=self.CHUNK_SIZE):
                    if self._stop_event.is_set():
                        break

                    if chunk:
                        f.write(chunk)
                        self._bytes_written += len(chunk)

                    # Check if program has ended (normalize tz awareness)
                    if end_time_compare:
                        if local_tz and django_timezone.is_naive(end_time):
                            now_local = django_timezone.localtime(django_timezone.now(), local_tz).replace(tzinfo=None)
                            if now_local >= end_time:
                                if self.debug:
                                    logger.info(
                                        f"[LocalCatchup] ch{self.channel_id}: Program ended, finalizing"
                                    )
                                break
                        else:
                            now = django_timezone.now()
                            # Normalize awareness
                            if django_timezone.is_aware(end_time_compare) and django_timezone.is_naive(now):
                                now = django_timezone.make_aware(now, django_timezone.get_current_timezone())
                            if django_timezone.is_naive(end_time_compare) and django_timezone.is_aware(now):
                                end_time_compare = django_timezone.make_aware(end_time_compare, django_timezone.get_current_timezone())
                            if now >= end_time_compare:
                                if self.debug:
                                    logger.info(
                                        f"[LocalCatchup] ch{self.channel_id}: Program ended, finalizing"
                                    )
                                break
        finally:
            response.close()

    def _get_stream_url(self):
        """Build the stream URL from the Stream model object."""
        try:
            m3u = self.stream.m3u_account
            if not m3u:
                return None

            props = self.stream.custom_properties or {}

            if m3u.account_type == 'XC':
                # Xtream Codes: build live stream URL
                stream_id = props.get('stream_id')
                if not stream_id:
                    return None
                return (
                    f"{m3u.server_url.rstrip('/')}/live/"
                    f"{m3u.username}/{m3u.password}/{stream_id}.ts"
                )
            else:
                # M3U/other: use the stream URL directly
                return self.stream.url
        except Exception as e:
            logger.error(f"[LocalCatchup] ch{self.channel_id}: Error building URL: {e}")
            return None

    def _finalize_file(self):
        """Rename .recording file to .ts on completion."""
        if not self._current_file:
            return

        if not os.path.exists(self._current_file):
            return

        # Only rename if we wrote some data
        if self._bytes_written == 0:
            try:
                os.remove(self._current_file)
            except OSError:
                pass
            return

        if self._current_file.endswith('.recording'):
            final_path = self._final_path or self._current_file[:-len('.recording')]
            try:
                # If final path already exists, append instead of creating parts
                if os.path.exists(final_path):
                    with open(final_path, 'ab') as dst, open(self._current_file, 'rb') as src:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            dst.write(chunk)
                    os.remove(self._current_file)
                else:
                    os.rename(self._current_file, final_path)
                size_mb = self._bytes_written / (1024 * 1024)
                logger.info(
                    f"[LocalCatchup] ch{self.channel_id}: Saved {os.path.basename(final_path)} "
                    f"({size_mb:.1f} MB)"
                )
            except OSError as e:
                logger.error(f"[LocalCatchup] ch{self.channel_id}: Failed to finalize: {e}")


class RecordingManager:
    """
    Manages all recording workers and the supervisor thread.

    Supervisor runs every 30 seconds to:
    - Check which channels should be recording
    - Start/stop workers as needed
    - Run cleanup every hour
    """

    SUPERVISOR_INTERVAL = 30  # seconds
    CLEANUP_INTERVAL = 3600  # 1 hour
    LOCK_STALE_SECONDS = 120  # consider lock stale after 2 minutes without heartbeat

    def __init__(self):
        self._workers = {}  # channel_id -> RecordingWorker
        self._workers_lock = threading.Lock()
        self._supervisor_thread = None
        self._watchdog_thread = None
        self._stop_event = threading.Event()
        self._running = False
        self._last_cleanup = 0
        self._settings = {}
        self._lock_path = None

    def start(self, settings):
        """Start the recording manager and supervisor thread."""
        if self._running:
            return {"status": "ok", "message": "Already running"}

        self._settings = settings
        self._stop_event.clear()
        self._running = True

        storage_dir = settings.get('storage_dir', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'catchup'))
        os.makedirs(storage_dir, exist_ok=True)
        if not self._acquire_lock(storage_dir):
            self._running = False
            return {"status": "ok", "message": "Recorder already running in another worker"}

        self._supervisor_thread = threading.Thread(
            target=self._supervisor_loop,
            name="recording-supervisor",
            daemon=True,
        )
        self._supervisor_thread.start()

        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="recording-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()

        logger.info("[LocalCatchup] Recording manager started")
        return {"status": "ok", "message": "Recording started"}

    def stop(self):
        """Stop all recording workers and the supervisor."""
        if not self._running:
            return {"status": "ok", "message": "Not running"}

        self._running = False
        self._stop_event.set()

        # Stop all workers
        self._stop_all_workers()

        # Wait for supervisor
        if self._supervisor_thread:
            self._supervisor_thread.join(timeout=15)
            self._supervisor_thread = None
        if self._watchdog_thread:
            self._watchdog_thread.join(timeout=5)
            self._watchdog_thread = None
        self._release_lock()

        logger.info("[LocalCatchup] Recording manager stopped")
        return {"status": "ok", "message": "Recording stopped"}

    def status(self):
        """Get current recording status."""
        with self._workers_lock:
            active = []
            for ch_id, worker in self._workers.items():
                active.append({
                    'channel_id': ch_id,
                    'channel_name': worker.channel_name,
                    'alive': worker.is_alive(),
                    'bytes_written': worker._bytes_written,
                })

        storage_dir = self._settings.get('storage_dir', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'catchup'))
        disk_usage = storage.get_disk_usage(storage_dir)
        disk_usage_gb = disk_usage / (1024 * 1024 * 1024)

        enabled_channels = channel_config.get_all_enabled()

        return {
            "status": "ok",
            "message": (
                f"Running: {self._running}\n"
                f"Active recordings: {len(active)}\n"
                f"Enabled channels: {len(enabled_channels)}\n"
                f"Disk usage: {disk_usage_gb:.2f} GB\n"
                f"Storage dir: {storage_dir}"
            ),
            "data": {
                "running": self._running,
                "active_recordings": active,
                "enabled_channels": enabled_channels,
                "disk_usage_bytes": disk_usage,
                "disk_usage_gb": round(disk_usage_gb, 2),
            }
        }

    def _supervisor_loop(self):
        """Main supervisor loop that manages recording workers."""
        logger.info("[LocalCatchup] Supervisor started")

        while not self._stop_event.is_set():
            self._touch_lock()
            if not self._is_recorder_enabled():
                logger.info("[LocalCatchup] Recorder disabled via settings; stopping")
                self._stop_all_workers()
                self._running = False
                self._release_lock()
                self._stop_event.set()
                break
            try:
                self._check_channels()
            except Exception as e:
                logger.error(f"[LocalCatchup] Supervisor error: {e}", exc_info=True)

            # Run cleanup periodically
            now = time.time()
            if now - self._last_cleanup > self.CLEANUP_INTERVAL:
                try:
                    storage.run_cleanup(self._settings)
                    self._last_cleanup = now
                except Exception as e:
                    logger.error(f"[LocalCatchup] Cleanup error: {e}")

            # Wait for next cycle
            self._stop_event.wait(self.SUPERVISOR_INTERVAL)

        logger.info("[LocalCatchup] Supervisor stopped")

    def _watchdog_loop(self):
        """Restart supervisor if it unexpectedly stops."""
        logger.info("[LocalCatchup] Watchdog started")
        while not self._stop_event.is_set():
            try:
                self._touch_lock()
                if not self._is_recorder_enabled():
                    # Stop everything if disabled from settings
                    if self._running:
                        self._stop_all_workers()
                        self._running = False
                        self._release_lock()
                        self._stop_event.set()
                    break
                if self._running and self._supervisor_thread and not self._supervisor_thread.is_alive():
                    logger.warning("[LocalCatchup] Supervisor thread died, restarting")
                    self._supervisor_thread = threading.Thread(
                        target=self._supervisor_loop,
                        name="recording-supervisor",
                        daemon=True,
                    )
                    self._supervisor_thread.start()
            except Exception as e:
                logger.error(f"[LocalCatchup] Watchdog error: {e}")
            self._stop_event.wait(5)
        logger.info("[LocalCatchup] Watchdog stopped")

    def _stop_all_workers(self):
        """Stop all recording workers without touching supervisor threads."""
        with self._workers_lock:
            for channel_id, worker in self._workers.items():
                logger.info(f"[LocalCatchup] Stopping recorder for ch{channel_id}")
                worker.stop()
            for channel_id, worker in self._workers.items():
                worker.join(timeout=10)
            self._workers.clear()

    def _is_recorder_enabled(self):
        """Check shared setting to stop recorder across workers."""
        try:
            from apps.plugins.models import PluginConfig
            config = PluginConfig.objects.filter(key='dispatcharr_local_catchup').first()
            if not config:
                return True
            settings = config.settings or {}
            # Default true to preserve behavior unless explicitly disabled
            return bool(settings.get('recorder_enabled', True))
        except Exception:
            return True

    def _acquire_lock(self, storage_dir):
        """Prevent multiple workers from running the recorder."""
        self._lock_path = os.path.join(storage_dir, '.local_catchup_recorder.lock')
        pid = os.getpid()
        def _write_lock():
            with open(self._lock_path, 'w', encoding='utf-8') as f:
                f.write(f"{pid}\n{int(time.time())}\n")
        try:
            # Atomic create
            fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(f"{pid}\n{int(time.time())}\n")
            return True
        except FileExistsError:
            # If recorder is globally disabled, treat lock as stale
            if not self._is_recorder_enabled():
                try:
                    _write_lock()
                    return True
                except Exception:
                    return False
            # Check if stale by heartbeat timestamp / mtime
            try:
                mtime = os.path.getmtime(self._lock_path)
                if time.time() - mtime > self.LOCK_STALE_SECONDS:
                    _write_lock()
                    return True
            except Exception:
                pass
            # Check if stale by PID
            try:
                with open(self._lock_path, 'r', encoding='utf-8') as f:
                    lines = f.read().splitlines()
                other_pid = int(lines[0]) if lines else 0
                if other_pid:
                    try:
                        os.kill(other_pid, 0)
                        return False
                    except Exception:
                        pass
                # Stale lock, replace
                _write_lock()
                return True
            except Exception:
                return False
        except Exception:
            return False

    def _release_lock(self):
        """Release the recorder lock file."""
        if not self._lock_path:
            return
        try:
            os.remove(self._lock_path)
        except OSError:
            pass

    def _touch_lock(self):
        """Update lock heartbeat timestamp."""
        if not self._lock_path:
            return
        try:
            os.utime(self._lock_path, None)
        except OSError:
            pass

    def _check_channels(self):
        """Check enabled channels and start/stop workers as needed."""
        enabled = set(channel_config.get_all_enabled())
        storage_dir = self._settings.get('storage_dir', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'catchup'))
        debug = self._settings.get('debug_mode', False)

        with self._workers_lock:
            current_channels = set(self._workers.keys())

            # Stop workers for disabled channels
            to_stop = current_channels - enabled
            for ch_id in to_stop:
                worker = self._workers.pop(ch_id, None)
                if worker:
                    logger.info(f"[LocalCatchup] Stopping ch{ch_id} (disabled)")
                    worker.stop()

            # Clean up dead workers
            dead = []
            for ch_id, worker in self._workers.items():
                if not worker.is_alive():
                    dead.append(ch_id)
            for ch_id in dead:
                self._workers.pop(ch_id, None)

            # Start workers for enabled channels that aren't recording
            for ch_id in enabled:
                if ch_id in self._workers and self._workers[ch_id].is_alive():
                    # Check if program changed
                    worker = self._workers[ch_id]
                    current_program = self._get_current_program(ch_id)
                    current_key = self._program_key(ch_id, current_program)
                    if current_key != worker.program_key:
                        if debug:
                            logger.info(f"[LocalCatchup] ch{ch_id}: Program changed, restarting")
                        worker.stop()
                        worker.join(timeout=10)
                        self._workers.pop(ch_id, None)
                    else:
                        continue

                # Start new worker
                self._start_worker(ch_id, storage_dir)

    def _start_worker(self, channel_id, storage_dir):
        """Start a recording worker for a channel."""
        try:
            from apps.channels.models import Channel

            channel = Channel.objects.filter(id=channel_id).first()
            if not channel:
                logger.warning(f"[LocalCatchup] Channel {channel_id} not found")
                return

            # Get first available stream
            stream = channel.streams.order_by('channelstream__order').first()
            if not stream:
                logger.warning(f"[LocalCatchup] No streams for channel {channel.name}")
                return

            # Get current program from EPG
            program_info = self._get_current_program(channel_id)

            worker = RecordingWorker(
                channel_id=channel_id,
                channel_name=channel.name,
                stream=stream,
                program_info=program_info,
                storage_dir=storage_dir,
                settings=self._settings,
            )
            worker.start()
            self._workers[channel_id] = worker

            program_title = program_info.get('title', 'no EPG') if program_info else 'no EPG'
            logger.info(f"[LocalCatchup] Started recording ch{channel_id} ({channel.name}): {program_title}")

        except Exception as e:
            logger.error(f"[LocalCatchup] Failed to start worker for ch{channel_id}: {e}")

    def _get_current_program(self, channel_id):
        """Get the current EPG program for a channel."""
        try:
            from apps.channels.models import Channel
            from django.utils import timezone as django_timezone

            channel = Channel.objects.filter(id=channel_id).first()
            if not channel or not channel.epg_data:
                return None

            now = django_timezone.now()
            program = channel.epg_data.programs.filter(
                start_time__lte=now,
                end_time__gt=now,
            ).first()

            if not program:
                return None

            return {
                'title': program.title or 'unknown',
                'start_time': program.start_time,
                'end_time': program.end_time,
            }
        except Exception as e:
            logger.debug(f"[LocalCatchup] EPG lookup failed for ch{channel_id}: {e}")
            return None

    def _program_key(self, channel_id, program_info):
        """Generate a unique key for a channel+program."""
        if program_info:
            return f"{channel_id}:{program_info.get('start_time', '')}"
        return f"{channel_id}:no_epg"
