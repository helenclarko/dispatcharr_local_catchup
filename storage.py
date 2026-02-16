"""
Local Catchup Plugin - Storage Management

File organization, cleanup, and recording path management.

Directory structure:
    {storage_dir}/
    └── {channel_id}/
        └── {YYYY-MM-DD}/
            └── {HH-MM}_{sanitized_program_title}.ts
            └── {HH-MM}_{sanitized_program_title}.ts.recording  (in-progress)
"""

import logging
import os
import re
import time
from datetime import datetime, timedelta

logger = logging.getLogger("plugins.dispatcharr_local_catchup.storage")


def sanitize_filename(title):
    """Convert a program title to a filesystem-safe filename."""
    if not title:
        return "unknown"
    # Replace unsafe characters with underscore
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title)
    # Collapse multiple underscores
    safe = re.sub(r'_+', '_', safe).strip('_')
    # Limit length
    if len(safe) > 100:
        safe = safe[:100]
    return safe or "unknown"


def get_recording_path(storage_dir, channel_id, program_start, program_title):
    """
    Build the full file path for a recording.

    Args:
        storage_dir: Base storage directory
        channel_id: Channel ID
        program_start: datetime of program start
        program_title: Program title (will be sanitized)

    Returns:
        str: Full path like {storage_dir}/{channel_id}/2025-01-15/14-30_My_Show.ts
    """
    date_str = program_start.strftime("%Y-%m-%d")
    time_str = program_start.strftime("%H-%M")
    safe_title = sanitize_filename(program_title)
    filename = f"{time_str}_{safe_title}.ts"
    return os.path.join(storage_dir, str(channel_id), date_str, filename)


def find_recording(storage_dir, channel_id, timestamp):
    """
    Find a recording file that contains the given timestamp.

    Looks for .ts files where the file's start time is at or before the timestamp,
    considering files in the same date directory.

    Args:
        storage_dir: Base storage directory
        channel_id: Channel ID
        timestamp: datetime to search for

    Returns:
        str: Path to the .ts file, or None if not found
    """
    channel_dir = os.path.join(storage_dir, str(channel_id))
    if not os.path.isdir(channel_dir):
        return None

    date_str = timestamp.strftime("%Y-%m-%d")
    date_dir = os.path.join(channel_dir, date_str)

    if not os.path.isdir(date_dir):
        # Try previous day (program may have started before midnight)
        prev_date = (timestamp - timedelta(days=1)).strftime("%Y-%m-%d")
        date_dir = os.path.join(channel_dir, prev_date)
        if not os.path.isdir(date_dir):
            return None

    # List all .ts files (including .ts.recording for in-progress)
    candidates = []
    try:
        for fname in os.listdir(date_dir):
            if not (fname.endswith('.ts') or fname.endswith('.ts.recording')):
                continue
            # Parse time from filename: HH-MM_title.ts
            match = re.match(r'^(\d{2})-(\d{2})_', fname)
            if not match:
                continue
            hour, minute = int(match.group(1)), int(match.group(2))
            file_start = timestamp.replace(hour=hour, minute=minute, second=0, microsecond=0)
            # Handle date_dir being previous day
            dir_date = os.path.basename(date_dir)
            try:
                dir_dt = datetime.strptime(dir_date, "%Y-%m-%d")
                file_start = file_start.replace(year=dir_dt.year, month=dir_dt.month, day=dir_dt.day)
            except ValueError:
                pass
            candidates.append((file_start, os.path.join(date_dir, fname)))
    except OSError:
        return None

    if not candidates:
        return None

    # Sort by start time descending, find the latest one that starts at or before timestamp
    candidates.sort(key=lambda x: x[0], reverse=True)
    for file_start, file_path in candidates:
        if file_start <= timestamp:
            return file_path

    # If nothing starts before timestamp, return the earliest file
    return candidates[-1][1] if candidates else None


def get_disk_usage(storage_dir):
    """Calculate total disk usage of the storage directory in bytes."""
    total = 0
    try:
        for dirpath, dirnames, filenames in os.walk(storage_dir):
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                try:
                    total += os.path.getsize(fpath)
                except OSError:
                    pass
    except OSError:
        pass
    return total


def cleanup_by_age(storage_dir, max_days):
    """Delete recordings older than max_days."""
    if max_days <= 0:
        return 0

    cutoff = datetime.now() - timedelta(days=max_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    deleted = 0

    try:
        for channel_id in os.listdir(storage_dir):
            channel_dir = os.path.join(storage_dir, channel_id)
            if not os.path.isdir(channel_dir):
                continue

            for date_dir_name in os.listdir(channel_dir):
                date_dir = os.path.join(channel_dir, date_dir_name)
                if not os.path.isdir(date_dir):
                    continue

                # Compare date directory name with cutoff
                if date_dir_name < cutoff_str:
                    # Delete all files in this date directory
                    for fname in os.listdir(date_dir):
                        fpath = os.path.join(date_dir, fname)
                        # Don't delete in-progress recordings
                        if fname.endswith('.ts.recording'):
                            continue
                        try:
                            os.remove(fpath)
                            deleted += 1
                        except OSError as e:
                            logger.warning(f"[LocalCatchup] Failed to delete {fpath}: {e}")

                    # Remove empty date directory
                    try:
                        if not os.listdir(date_dir):
                            os.rmdir(date_dir)
                    except OSError:
                        pass

            # Remove empty channel directory
            try:
                if not os.listdir(channel_dir):
                    os.rmdir(channel_dir)
            except OSError:
                pass
    except OSError as e:
        logger.error(f"[LocalCatchup] Age cleanup error: {e}")

    if deleted > 0:
        logger.info(f"[LocalCatchup] Age cleanup: deleted {deleted} files older than {max_days} days")
    return deleted


def cleanup_by_size(storage_dir, max_gb):
    """Delete oldest recordings until total storage is under max_gb."""
    if max_gb <= 0:
        return 0

    max_bytes = max_gb * 1024 * 1024 * 1024
    current_usage = get_disk_usage(storage_dir)

    if current_usage <= max_bytes:
        return 0

    # Collect all .ts files with their modification times
    all_files = []
    try:
        for dirpath, dirnames, filenames in os.walk(storage_dir):
            for fname in filenames:
                if not fname.endswith('.ts'):
                    continue
                # Skip in-progress recordings
                if fname.endswith('.ts.recording'):
                    continue
                fpath = os.path.join(dirpath, fname)
                try:
                    stat = os.stat(fpath)
                    all_files.append((stat.st_mtime, stat.st_size, fpath))
                except OSError:
                    pass
    except OSError as e:
        logger.error(f"[LocalCatchup] Size cleanup scan error: {e}")
        return 0

    # Sort by modification time (oldest first)
    all_files.sort(key=lambda x: x[0])

    deleted = 0
    for mtime, size, fpath in all_files:
        if current_usage <= max_bytes:
            break
        try:
            os.remove(fpath)
            current_usage -= size
            deleted += 1

            # Clean up empty parent directories
            parent = os.path.dirname(fpath)
            try:
                if not os.listdir(parent):
                    os.rmdir(parent)
                    grandparent = os.path.dirname(parent)
                    if not os.listdir(grandparent):
                        os.rmdir(grandparent)
            except OSError:
                pass
        except OSError as e:
            logger.warning(f"[LocalCatchup] Failed to delete {fpath}: {e}")

    if deleted > 0:
        logger.info(f"[LocalCatchup] Size cleanup: deleted {deleted} files to stay under {max_gb}GB")
    return deleted


def run_cleanup(settings):
    """Run both age-based and size-based cleanup."""
    storage_dir = settings.get('storage_dir', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'catchup'))
    max_days = int(settings.get('max_retention_days', 3))
    max_gb = float(settings.get('max_storage_gb', 50))

    if not os.path.isdir(storage_dir):
        return

    age_deleted = cleanup_by_age(storage_dir, max_days)
    size_deleted = cleanup_by_size(storage_dir, max_gb)
    return age_deleted + size_deleted


def list_recordings(storage_dir, channel_id=None):
    """
    List all recorded files with metadata.

    Returns list of dicts with: channel_id, date, filename, path, size, recording (bool)
    """
    results = []
    try:
        if channel_id:
            channel_dirs = [str(channel_id)]
        else:
            channel_dirs = os.listdir(storage_dir)

        for ch_dir in channel_dirs:
            channel_path = os.path.join(storage_dir, ch_dir)
            if not os.path.isdir(channel_path):
                continue

            for date_dir in sorted(os.listdir(channel_path)):
                date_path = os.path.join(channel_path, date_dir)
                if not os.path.isdir(date_path):
                    continue

                for fname in sorted(os.listdir(date_path)):
                    if not (fname.endswith('.ts') or fname.endswith('.ts.recording')):
                        continue
                    fpath = os.path.join(date_path, fname)
                    try:
                        size = os.path.getsize(fpath)
                    except OSError:
                        size = 0

                    results.append({
                        'channel_id': ch_dir,
                        'date': date_dir,
                        'filename': fname,
                        'path': fpath,
                        'size': size,
                        'recording': fname.endswith('.ts.recording'),
                    })
    except OSError as e:
        logger.error(f"[LocalCatchup] Error listing recordings: {e}")

    return results


def get_days_of_recordings(storage_dir, channel_id):
    """Get the number of days of recordings available for a channel."""
    channel_dir = os.path.join(storage_dir, str(channel_id))
    if not os.path.isdir(channel_dir):
        return 0

    dates = set()
    try:
        for date_dir in os.listdir(channel_dir):
            date_path = os.path.join(channel_dir, date_dir)
            if os.path.isdir(date_path):
                # Check if directory has any .ts files
                for fname in os.listdir(date_path):
                    if fname.endswith('.ts') and not fname.endswith('.ts.recording'):
                        dates.add(date_dir)
                        break
    except OSError:
        pass

    if not dates:
        return 0

    # Calculate span from earliest to latest
    sorted_dates = sorted(dates)
    try:
        earliest = datetime.strptime(sorted_dates[0], "%Y-%m-%d")
        latest = datetime.strptime(sorted_dates[-1], "%Y-%m-%d")
        return (latest - earliest).days + 1
    except ValueError:
        return len(dates)
