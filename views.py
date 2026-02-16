"""
Local Catchup Plugin - Views

Serves locally recorded .ts files for catchup playback and the inject.js frontend script.

catchup_proxy():
    Handles /timeshift/ URLs by serving local recordings instead of proxying to provider.
    Supports HTTP Range requests for seeking.

serve_inject_js():
    Serves the inject.js file for frontend toggle injection.
"""

import logging
import os
import re
import mimetypes
from datetime import datetime
from zoneinfo import ZoneInfo

from django.http import (
    FileResponse,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseNotFound,
    StreamingHttpResponse,
)

from . import storage

logger = logging.getLogger("plugins.dispatcharr_local_catchup.views")


def catchup_proxy(request, username, password, stream_id, timestamp, duration):
    """
    Serve a local recording for catchup/timeshift requests.

    URL format from IPTV clients:
        /timeshift/{user}/{pass}/{epg_channel}/{timestamp}/{provider_stream_id}.ts

    Same URL quirk as Timeshift plugin:
        - Position 3 (stream_id param) = EPG channel number (NOT used)
        - Position 5 (duration param) = Provider's stream_id (USED for lookup)

    Args:
        username: Dispatcharr username
        password: User's xc_password
        stream_id: EPG channel number (ignored)
        timestamp: Start time as YYYY-MM-DD:HH-MM
        duration: Provider's stream_id (misleading param name from URL pattern)
    """
    from .hooks import _get_plugin_config

    # The "duration" param is actually the provider's stream_id
    provider_stream_id = duration.rstrip('.ts')

    config = _get_plugin_config()
    debug = config['debug_mode']
    timezone_str = config['timezone']
    storage_dir = config['storage_dir']

    if debug:
        logger.info(f"[LocalCatchup] === CATCHUP REQUEST ===")
        logger.info(f"[LocalCatchup] User: {username}, Provider stream_id: {provider_stream_id}")
        logger.info(f"[LocalCatchup] Timestamp: {timestamp}")

    # Step 1: Authenticate user
    user = _authenticate_user(username, password)
    if not user:
        return HttpResponseForbidden("Invalid credentials")

    # Step 2: Find channel by provider's stream_id
    channel, stream = _find_channel_by_provider_stream_id(provider_stream_id)
    if not channel:
        logger.error(f"[LocalCatchup] Channel not found for provider_stream_id={provider_stream_id}")
        return HttpResponseNotFound("Channel not found")

    if debug:
        logger.info(f"[LocalCatchup] Channel: {channel.name} (id={channel.id})")

    # Step 3: Verify user access
    if user.user_level < channel.user_level:
        return HttpResponseForbidden("Access denied")

    # Step 4: Convert timestamp to local timezone
    local_timestamp = _convert_timestamp_to_local(timestamp, timezone_str)
    if debug:
        logger.info(f"[LocalCatchup] Timestamp: {timestamp} (UTC) -> {local_timestamp} ({timezone_str})")

    # Step 5: Parse timestamp to datetime
    try:
        local_dt = datetime.strptime(local_timestamp, "%Y-%m-%d:%H-%M")
    except ValueError:
        return HttpResponseBadRequest(f"Invalid timestamp format: {timestamp}")

    # Step 6: Find recording
    if debug:
        logger.info(f"[LocalCatchup] Lookup: storage_dir={storage_dir}, channel_id={channel.id}")
        try:
            channel_dir = os.path.join(storage_dir, str(channel.id))
            if os.path.isdir(channel_dir):
                dates = sorted(os.listdir(channel_dir))
                logger.info(f"[LocalCatchup] Lookup: date dirs={dates}")
            else:
                logger.info(f"[LocalCatchup] Lookup: channel dir missing ({channel_dir})")
        except Exception as e:
            logger.info(f"[LocalCatchup] Lookup: error reading channel dir: {e}")

    recording_path = storage.find_recording(storage_dir, channel.id, local_dt)

    if not recording_path:
        if debug:
            logger.info(f"[LocalCatchup] No recording found for ch{channel.id} at {local_timestamp}")
        return HttpResponseNotFound("Recording not found")

    if debug:
        logger.info(f"[LocalCatchup] Found recording: {recording_path}")

    # Step 7: Serve the file
    if recording_path.endswith('.ts.recording'):
        # In-progress recording: stream without Content-Length
        return _serve_streaming(recording_path, debug)
    else:
        # Completed recording: serve with Range support
        return _serve_file(request, recording_path, debug)


def _serve_file(request, file_path, debug=False):
    """
    Serve a completed .ts file with Range header support for seeking.
    """
    try:
        file_size = os.path.getsize(file_path)
    except OSError:
        return HttpResponseNotFound("File not found")

    range_header = request.META.get('HTTP_RANGE')

    if range_header:
        # Parse Range header: bytes=start-end
        match = re.match(r'bytes=(\d+)-(\d*)', range_header)
        if not match:
            return HttpResponseBadRequest("Invalid Range header")

        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else file_size - 1

        if start >= file_size:
            response = HttpResponse(status=416)
            response['Content-Range'] = f'bytes */{file_size}'
            return response

        end = min(end, file_size - 1)
        content_length = end - start + 1

        if debug:
            logger.info(f"[LocalCatchup] Range request: bytes={start}-{end}/{file_size}")

        def range_generator():
            with open(file_path, 'rb') as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk_size = min(65536, remaining)
                    data = f.read(chunk_size)
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        response = StreamingHttpResponse(
            range_generator(),
            content_type='video/mp2t',
            status=206,
        )
        response['Content-Length'] = content_length
        response['Content-Range'] = f'bytes {start}-{end}/{file_size}'
        response['Accept-Ranges'] = 'bytes'
        return response
    else:
        # Full file response
        response = FileResponse(
            open(file_path, 'rb'),
            content_type='video/mp2t',
        )
        response['Content-Length'] = file_size
        response['Accept-Ranges'] = 'bytes'
        return response


def _serve_streaming(file_path, debug=False):
    """
    Serve an in-progress recording as a streaming response (no Content-Length).
    """
    if debug:
        logger.info(f"[LocalCatchup] Streaming in-progress recording: {file_path}")

    def stream_generator():
        with open(file_path, 'rb') as f:
            while True:
                data = f.read(65536)
                if not data:
                    break
                yield data

    response = StreamingHttpResponse(
        stream_generator(),
        content_type='video/mp2t',
    )
    return response


def serve_inject_js(request):
    """Serve the inject.js file from the plugin directory."""
    js_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inject.js")

    if not os.path.exists(js_path):
        return HttpResponseNotFound("inject.js not found")

    try:
        with open(js_path, 'r', encoding='utf-8') as f:
            content = f.read()
        response = HttpResponse(content, content_type='application/javascript')
        response['Cache-Control'] = 'no-cache'
        return response
    except IOError:
        return HttpResponseNotFound("inject.js not readable")


def _authenticate_user(username, password):
    """Authenticate user by username and xc_password."""
    from apps.accounts.models import User

    try:
        user = User.objects.get(username=username)
        xc_password = (user.custom_properties or {}).get('xc_password')
        if not xc_password or xc_password != password:
            logger.error(f"[LocalCatchup] Auth failed for user '{username}'")
            return None
        return user
    except User.DoesNotExist:
        logger.error(f"[LocalCatchup] User '{username}' does not exist")
        return None


def _find_channel_by_provider_stream_id(provider_stream_id):
    """Find channel by provider's stream_id in custom_properties."""
    from apps.channels.models import Stream

    stream = Stream.objects.filter(
        custom_properties__stream_id=str(provider_stream_id),
        m3u_account__account_type='XC'
    ).first()

    if stream:
        channel = stream.channels.first()
        if channel:
            return channel, stream

    return None, None


def _convert_timestamp_to_local(timestamp, timezone_str):
    """Convert UTC timestamp to local timezone."""
    try:
        utc_time = datetime.strptime(timestamp, "%Y-%m-%d:%H-%M")
        utc_time = utc_time.replace(tzinfo=ZoneInfo("UTC"))
        local_time = utc_time.astimezone(ZoneInfo(timezone_str))
        return local_time.strftime("%Y-%m-%d:%H-%M")
    except Exception as e:
        logger.error(f"[LocalCatchup] Timestamp conversion failed: {e}")
        return timestamp
