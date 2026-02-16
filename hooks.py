"""
Local Catchup Plugin - Hooks

Implements local catchup via monkey-patching (no modification to Dispatcharr source):

1. Patches xc_get_live_streams - marks locally-recorded channels with tv_archive=1
2. Patches stream_xc - resolves provider stream_id to internal channel for live streaming
3. Patches xc_get_epg - generates EPG from actual recorded files
4. Patches generate_epg - timezone conversion for XMLTV timestamps
5. Patches URLResolver.resolve - intercepts /timeshift/ and /local-catchup/ URLs
6. Patches ChannelSerializer - adds local_catchup field to API responses/updates
7. Patches middleware - injects <script> tag for inject.js into HTML responses

RUNTIME ENABLE/DISABLE:
    Hooks are installed once at startup but check _is_plugin_enabled() at runtime.
"""

import os
import re
import logging

logger = logging.getLogger("plugins.dispatcharr_local_catchup.hooks")

# Default storage directory: catchup/ folder inside the plugin directory
_DEFAULT_STORAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'catchup')

# Store original functions
_original_xc_get_live_streams = None
_original_stream_xc = None
_original_xc_get_epg = None
_original_generate_epg = None
_original_url_callbacks = {}
_original_resolve = None
_original_to_representation = None
_original_serializer_update = None


def _get_true_original(target_obj, attr_name):
    """Retrieve the true original function, storing it on the target object.

    The true original is stored as a hidden attribute on the target object
    (class or module), which survives module reloads, fork(), and stale .pyc
    bytecode. This is the only reliable way to find the real original when
    the plugin module gets loaded/reloaded multiple times.

    Args:
        target_obj: The object being patched (e.g., URLResolver class, output_views module)
        attr_name: The attribute name being patched (e.g., 'resolve', 'xc_get_live_streams')
    """
    marker = f'_lc_orig_{attr_name}'

    # Check if we already stored the true original on the target
    if hasattr(target_obj, marker):
        return getattr(target_obj, marker)

    # First time patching this target - current value IS the original
    original = getattr(target_obj, attr_name)

    # Store on the target object for future reloads
    setattr(target_obj, marker, original)
    return original


def _get_plugin_config():
    """Get plugin configuration from database."""
    defaults = {
        'timezone': 'UTC',
        'language': 'en',
        'debug_mode': False,
        'storage_dir': _DEFAULT_STORAGE_DIR,
        'max_retention_days': 3,
        'max_storage_gb': 50,
        'auto_start_recorder': True,
        'recorder_enabled': True,
    }
    try:
        from apps.plugins.models import PluginConfig
        config = PluginConfig.objects.filter(key='dispatcharr_local_catchup').first()
        if config and config.settings:
            return {
                'timezone': config.settings.get('timezone', 'UTC').strip(),
                'language': config.settings.get('language', 'en').strip(),
                'debug_mode': bool(config.settings.get('debug_mode', False)),
                'storage_dir': config.settings.get('storage_dir', '').strip() or _DEFAULT_STORAGE_DIR,
                'max_retention_days': int(config.settings.get('max_retention_days', 3)),
                'max_storage_gb': float(config.settings.get('max_storage_gb', 50)),
                'auto_start_recorder': bool(config.settings.get('auto_start_recorder', True)),
                'recorder_enabled': bool(config.settings.get('recorder_enabled', True)),
            }
    except Exception:
        pass
    return defaults


_enabled_cache = {'value': None, 'time': 0}


def _is_plugin_enabled():
    """Check if plugin is enabled in database (cached for 2 seconds)."""
    import time
    now = time.time()
    if now - _enabled_cache['time'] < 2:
        return _enabled_cache['value']
    try:
        from apps.plugins.models import PluginConfig
        config = PluginConfig.objects.get(key='dispatcharr_local_catchup')
        _enabled_cache['value'] = config.enabled
        _enabled_cache['time'] = now
        return config.enabled
    except Exception:
        _enabled_cache['value'] = False
        _enabled_cache['time'] = now
        return False


def _ensure_package_registered():
    """Ensure our plugin package is registered in sys.modules.

    The plugin loader may unload/reload modules during discovery, leaving
    patched functions with stale __globals__ that reference a package no
    longer in sys.modules. This re-registers the package so import statements
    in those functions can resolve correctly.
    """
    import sys
    import types
    pkg = __package__
    base_path = os.path.dirname(os.path.abspath(__file__))

    def _register(name):
        if not name or name in sys.modules:
            return
        ns = types.ModuleType(name)
        ns.__path__ = [base_path]
        ns.__package__ = name
        sys.modules[name] = ns
        logger.info(f"[LocalCatchup] Re-registered package {name} in sys.modules")

    # Register the active package name
    _register(pkg)

    # Register common alias names used by the plugin loader
    _register('dispatcharr_local_catchup')
    _register('_dispatcharr_plugin_dispatcharr_local_catchup')


def install_hooks():
    """Install all local catchup hooks."""
    logger.info("[LocalCatchup] Installing hooks...")

    _ensure_package_registered()

    try:
        _patch_xc_get_live_streams()
        _patch_stream_xc()
        _patch_xc_get_epg()
        _patch_generate_epg()
        _patch_url_resolver()
        _patch_channel_serializer()
        _patch_middleware()
        logger.info("[LocalCatchup] All hooks installed successfully")
        return True
    except Exception as e:
        logger.error(f"[LocalCatchup] Failed to install hooks: {e}", exc_info=True)
        return False


# =============================================================================
# Patch 1: xc_get_live_streams - Mark locally-recorded channels
# =============================================================================

def _patch_xc_get_live_streams():
    """
    Patch xc_get_live_streams to add tv_archive=1 for channels with local recordings.
    Also replaces stream_id with provider's stream_id (same as Timeshift).
    """
    global _original_xc_get_live_streams

    from apps.output import views as output_views
    from . import channel_config as _channel_config
    from . import storage as _storage

    _original_xc_get_live_streams = _get_true_original(output_views, 'xc_get_live_streams')

    def patched_xc_get_live_streams(request, user, category_id=None):
        try:
            return _patched_xc_get_live_streams_inner(request, user, category_id)
        except ModuleNotFoundError:
            return _original_xc_get_live_streams(request, user, category_id)

    def _patched_xc_get_live_streams_inner(request, user, category_id=None):
        streams = _original_xc_get_live_streams(request, user, category_id)

        if not _is_plugin_enabled():
            return streams

        from apps.channels.models import Channel

        config = _get_plugin_config()
        debug = config['debug_mode']
        storage_dir = config['storage_dir']

        if debug:
            logger.info(f"[LocalCatchup] API: Processing {len(streams)} streams")

        catchup_count = 0

        for stream_data in streams:
            original_stream_id = stream_data.get('stream_id')
            try:
                channel = Channel.objects.filter(id=original_stream_id).first()
                if not channel:
                    continue

                # Get first stream for provider_stream_id
                first_stream = channel.streams.order_by('channelstream__order').first()
                if not first_stream:
                    continue

                props = first_stream.custom_properties or {}

                # Check if this channel has local catchup enabled AND has recordings
                if _channel_config.is_enabled(channel.id):
                    days = _storage.get_days_of_recordings(storage_dir, channel.id)
                    if days > 0:
                        stream_data['tv_archive'] = 1
                        stream_data['tv_archive_duration'] = days
                        catchup_count += 1
                        if debug:
                            logger.info(
                                f"[LocalCatchup] API: {channel.name} → tv_archive=1 ({days}d of recordings)"
                            )
                    else:
                        # No recordings yet → do not advertise catchup
                        stream_data['tv_archive'] = 0
                        stream_data['tv_archive_duration'] = 0
                else:
                    # Not locally recorded - check provider's catch-up support (fallback chain)
                    tv_archive = 0
                    tv_archive_duration = 0

                    for stream in channel.streams.order_by('channelstream__order'):
                        stream_props = stream.custom_properties or {}
                        if int(stream_props.get('tv_archive', 0)):
                            tv_archive = 1
                            tv_archive_duration = int(stream_props.get('tv_archive_duration', 0))
                            break

                    stream_data['tv_archive'] = tv_archive
                    stream_data['tv_archive_duration'] = tv_archive_duration

                # Replace stream_id with provider's stream_id
                provider_stream_id = props.get('stream_id')
                if provider_stream_id:
                    stream_data['stream_id'] = int(provider_stream_id)

            except Exception as e:
                logger.error(f"[LocalCatchup] API: Error enhancing stream {original_stream_id}: {e}")

        if debug and catchup_count > 0:
            logger.info(f"[LocalCatchup] API: {catchup_count}/{len(streams)} channels with local catchup")

        return streams

    output_views.xc_get_live_streams = patched_xc_get_live_streams
    logger.info("[LocalCatchup] Patched xc_get_live_streams")


# =============================================================================
# Patch 2: stream_xc - Resolve provider stream_id for live streaming
# =============================================================================

def _patch_stream_xc():
    """
    Patch stream_xc to find channels by provider stream_id first.
    (Same logic as Timeshift plugin - needed because we replace stream_id in API)
    """
    global _original_stream_xc, _original_url_callbacks

    from apps.proxy.ts_proxy import views as proxy_views
    from dispatcharr import urls as main_urls

    _original_stream_xc = _get_true_original(proxy_views, 'stream_xc')

    def patched_stream_xc(request, username, password, channel_id):
        try:
            return _patched_stream_xc_inner(request, username, password, channel_id)
        except ModuleNotFoundError:
            return _original_stream_xc(request, username, password, channel_id)

    def _patched_stream_xc_inner(request, username, password, channel_id):
        if not _is_plugin_enabled():
            return _original_stream_xc(request, username, password, channel_id)

        import pathlib
        from django.shortcuts import get_object_or_404
        from django.http import JsonResponse
        from apps.accounts.models import User
        from apps.channels.models import Channel, Stream

        config = _get_plugin_config()
        debug = config['debug_mode']

        user = get_object_or_404(User, username=username)
        channel_id_str = pathlib.Path(channel_id).stem

        if debug:
            logger.info(f"[LocalCatchup] Live: user={username}, channel_id={channel_id_str}")

        custom_properties = user.custom_properties or {}
        if "xc_password" not in custom_properties:
            return JsonResponse({"error": "Invalid credentials"}, status=401)
        if custom_properties["xc_password"] != password:
            return JsonResponse({"error": "Invalid credentials"}, status=401)

        channel = None

        # Try provider stream_id first
        stream = Stream.objects.filter(
            custom_properties__stream_id=channel_id_str,
            m3u_account__account_type='XC'
        ).first()
        if stream:
            channel = stream.channels.first()
            if channel and debug:
                logger.info(f"[LocalCatchup] Live: Found by provider_stream_id={channel_id_str} → {channel.name}")

        # Fallback to internal ID
        if not channel:
            try:
                internal_id = int(channel_id_str)
                if user.user_level < 10:
                    user_profile_count = user.channel_profiles.count()
                    if user_profile_count == 0:
                        channel = Channel.objects.filter(
                            id=internal_id,
                            user_level__lte=user.user_level
                        ).first()
                    else:
                        channel = Channel.objects.filter(
                            id=internal_id,
                            channelprofilemembership__enabled=True,
                            user_level__lte=user.user_level,
                            channelprofilemembership__channel_profile__in=user.channel_profiles.all()
                        ).distinct().first()
                else:
                    channel = Channel.objects.filter(id=internal_id).first()
            except (ValueError, TypeError):
                pass

        if not channel:
            logger.error(f"[LocalCatchup] Live: Channel not found for ID={channel_id_str}")
            return JsonResponse({"error": "Not found"}, status=404)

        if user.user_level < channel.user_level:
            return JsonResponse({"error": "Not found"}, status=404)

        from apps.proxy.ts_proxy.views import stream_ts
        actual_request = getattr(request, '_request', request)
        return stream_ts(actual_request, str(channel.uuid))

    proxy_views.stream_xc = patched_stream_xc

    # Patch URL pattern callbacks (match both original and previously-patched)
    for pattern in main_urls.urlpatterns:
        if hasattr(pattern, 'callback'):
            cb = pattern.callback
            if cb == _original_stream_xc or cb != _original_stream_xc and 'local_catchup' in getattr(cb, '__module__', ''):
                _original_url_callbacks[id(pattern)] = _original_stream_xc
                pattern.callback = patched_stream_xc

    logger.info("[LocalCatchup] Patched stream_xc")


# =============================================================================
# Patch 3: xc_get_epg - Generate EPG from recorded files
# =============================================================================

def _patch_xc_get_epg():
    """
    Patch xc_get_epg to generate EPG from actual recorded files for locally-recorded channels.
    Falls back to original EPG for non-locally-recorded channels.
    """
    global _original_xc_get_epg

    from apps.output import views as output_views
    from . import channel_config as _channel_config
    from . import storage as _storage

    _original_xc_get_epg = _get_true_original(output_views, 'xc_get_epg')

    def patched_xc_get_epg(request, user, short=False):
        try:
            return _patched_xc_get_epg_inner(request, user, short)
        except ModuleNotFoundError:
            return _original_xc_get_epg(request, user, short)

    def _patched_xc_get_epg_inner(request, user, short=False):
        if not _is_plugin_enabled():
            return _original_xc_get_epg(request, user, short)

        from django.http import Http404
        from apps.channels.models import Channel, Stream

        config = _get_plugin_config()
        debug = config['debug_mode']
        storage_dir = config['storage_dir']

        channel_id = request.GET.get('stream_id')
        if not channel_id:
            raise Http404()

        if debug:
            logger.info(f"[LocalCatchup] EPG: stream_id={channel_id}, short={short}")

        channel = None

        try:
            # Try provider stream_id first
            stream = Stream.objects.filter(
                custom_properties__stream_id=str(channel_id),
                m3u_account__account_type='XC'
            ).first()
            if stream:
                channel = stream.channels.first()

            # Fallback to internal ID
            if not channel:
                if user.user_level < 10:
                    user_profile_count = user.channel_profiles.count()
                    if user_profile_count == 0:
                        channel = Channel.objects.filter(
                            id=channel_id,
                            user_level__lte=user.user_level
                        ).first()
                    else:
                        channel = Channel.objects.filter(
                            id=channel_id,
                            channelprofilemembership__enabled=True,
                            user_level__lte=user.user_level,
                            channelprofilemembership__channel_profile__in=user.channel_profiles.all()
                        ).distinct().first()
                else:
                    channel = Channel.objects.filter(id=channel_id).first()

            if not channel:
                raise Http404()

            # Check if this channel uses local catchup
            if not _channel_config.is_enabled(channel.id):
                # Not locally recorded - delegate to original (with ID fixup)
                from django.http import QueryDict
                original_get = request.GET
                new_get = original_get.copy()
                new_get['stream_id'] = str(channel.id)
                request.GET = new_get
                try:
                    return _original_xc_get_epg(request, user, short)
                finally:
                    request.GET = original_get

            # Generate EPG from recorded files + current EPG data
            if debug:
                logger.info(f"[LocalCatchup] EPG: Generating from recordings for {channel.name}")

            return _generate_local_epg(channel, config, short, storage_mod=_storage)

        except Http404:
            raise
        except Exception as e:
            logger.error(f"[LocalCatchup] EPG: Error for stream_id={channel_id}: {e}", exc_info=True)
            raise Http404()

    output_views.xc_get_epg = patched_xc_get_epg
    logger.info("[LocalCatchup] Patched xc_get_epg")


def _generate_local_epg(channel, config, short=False, storage_mod=None):
    """Generate EPG listings from recorded files and current EPG data."""
    import base64
    from datetime import timedelta
    from django.utils import timezone as django_timezone
    from zoneinfo import ZoneInfo

    timezone_str = config['timezone']
    language = config['language']
    storage_dir = config['storage_dir']
    local_tz = ZoneInfo(timezone_str)
    max_days = config['max_retention_days']

    # Get first stream for metadata
    first_stream = channel.streams.order_by('channelstream__order').first()
    props = first_stream.custom_properties or {} if first_stream else {}

    now = django_timezone.now()
    start_date = now - timedelta(days=max_days)

    # Get EPG programs that we have recordings for
    output = {"epg_listings": []}

    if channel.epg_data and not short:
        programs = channel.epg_data.programs.filter(
            start_time__gte=start_date
        ).order_by('start_time')

        for program in programs:
            start = program.start_time
            end = program.end_time
            title = program.title or ""
            description = program.description or ""

            start_local = start.astimezone(local_tz)
            end_local = end.astimezone(local_tz)
            program_id = int(start.timestamp())

            # Check if we have a recording for this program
            has_recording = False
            if end < now:
                recording = storage_mod.find_recording(
                    storage_dir, channel.id, start_local.replace(tzinfo=None)
                )
                has_recording = recording is not None
            # Only include listings we actually have recorded
            if not has_recording:
                continue

            program_output = {
                "id": str(program_id),
                "epg_id": str(program.id) if hasattr(program, 'id') and program.id else str(program_id),
                "title": base64.b64encode(title.encode()).decode(),
                "lang": language,
                "start": start_local.strftime("%Y-%m-%d %H:%M:%S"),
                "end": end_local.strftime("%Y-%m-%d %H:%M:%S"),
                "description": base64.b64encode(description.encode()).decode(),
                "channel_id": str(props.get('epg_channel_id') or channel.id),
                "start_timestamp": str(int(start.timestamp())),
                "stop_timestamp": str(int(end.timestamp())),
                "stream_id": str(props.get('stream_id', channel.id)),
                "now_playing": 0 if start > now or end < now else 1,
                "has_archive": 1 if has_recording else 0,
            }

            output['epg_listings'].append(program_output)

    return output


# =============================================================================
# Patch 4: generate_epg - XMLTV timezone conversion
# =============================================================================

def _patch_generate_epg():
    """Patch generate_epg for XMLTV timezone conversion (same as Timeshift)."""
    global _original_generate_epg

    from apps.output import views as output_views

    _original_generate_epg = _get_true_original(output_views, 'generate_epg')

    def patched_generate_epg(request, profile_name=None, user=None):
        try:
            return _patched_generate_epg_inner(request, profile_name, user)
        except ModuleNotFoundError:
            return _original_generate_epg(request, profile_name, user)

    def _patched_generate_epg_inner(request, profile_name=None, user=None):
        if not _is_plugin_enabled():
            return _original_generate_epg(request, profile_name, user)

        try:
            from zoneinfo import ZoneInfo
            from django.http import StreamingHttpResponse
            import re

            plugin_config = _get_plugin_config()
            timezone_str = plugin_config['timezone']
            debug = plugin_config['debug_mode']
            local_tz = ZoneInfo(timezone_str)

            original_response = _original_generate_epg(request, profile_name, user)

            timestamp_pattern = re.compile(r'(\d{14}) ([+-]\d{4})')

            if hasattr(original_response, 'streaming_content'):
                original_generator = original_response.streaming_content
            else:
                content = original_response.content
                if isinstance(content, bytes):
                    content = content.decode('utf-8')
                original_generator = iter([content])

            def timezone_converting_generator():
                from datetime import datetime

                for chunk in original_generator:
                    if isinstance(chunk, bytes):
                        chunk = chunk.decode('utf-8')

                    if 'start="' in chunk or 'stop="' in chunk:
                        def convert_timestamp(match):
                            timestamp_str = match.group(1)
                            try:
                                utc_time = datetime.strptime(timestamp_str, "%Y%m%d%H%M%S")
                                utc_time = utc_time.replace(tzinfo=ZoneInfo("UTC"))
                                local_time = utc_time.astimezone(local_tz)
                                return local_time.strftime("%Y%m%d%H%M%S %z")
                            except Exception:
                                return match.group(0)

                        chunk = timestamp_pattern.sub(convert_timestamp, chunk)

                    yield chunk

            response = StreamingHttpResponse(
                timezone_converting_generator(),
                content_type='application/xml'
            )
            response['Content-Disposition'] = 'attachment; filename="Dispatcharr.xml"'
            response['Cache-Control'] = 'no-cache'
            return response

        except Exception as e:
            logger.error(f"[LocalCatchup] XMLTV error, falling back: {e}")
            return _original_generate_epg(request, profile_name, user)

    output_views.generate_epg = patched_generate_epg
    logger.info("[LocalCatchup] Patched generate_epg")


# =============================================================================
# Patch 5: URLResolver - Intercept /timeshift/ and /local-catchup/ URLs
# =============================================================================

def _patch_url_resolver():
    """
    Patch URLResolver.resolve to intercept:
    - /timeshift/ URLs -> serve local recordings
    - /local-catchup/inject.js -> serve frontend script
    """
    global _original_resolve

    from django.urls.resolvers import URLResolver
    from django.urls import ResolverMatch

    from .views import catchup_proxy, serve_inject_js
    from . import channel_config as _channel_config

    _original_resolve = _get_true_original(URLResolver, 'resolve')

    TIMESHIFT_PATTERN = re.compile(
        r'^/?timeshift/(?P<username>[^/]+)/(?P<password>[^/]+)/'
        r'(?P<stream_id>\d+)/(?P<timestamp>[\d\-:]+)/(?P<duration>\d+)\.ts$'
    )

    INJECT_JS_PATTERN = re.compile(r'^/?local-catchup/inject\.js$')

    def _is_local_catchup_channel(provider_stream_id):
        """Check if a provider stream_id maps to a channel with local catchup enabled."""
        try:
            from apps.channels.models import Stream

            stream = Stream.objects.filter(
                custom_properties__stream_id=str(provider_stream_id),
                m3u_account__account_type='XC'
            ).first()

            if stream:
                channel = stream.channels.first()
                if channel:
                    return _channel_config.is_enabled(channel.id)
        except Exception:
            pass
        return False

    def _resolve_impl(self, path):
        """The actual resolve logic. Stored on URLResolver class so it's always current."""
        try:
            _ensure_package_registered()
        except Exception:
            pass

        # Intercept /timeshift/ URLs for locally-recorded channels
        if path.startswith('/timeshift/') or path.startswith('timeshift/'):
            if not _is_plugin_enabled():
                return _original_resolve(self, path)
            match = TIMESHIFT_PATTERN.match(path)
            if match:
                groups = match.groupdict()
                provider_stream_id = groups['duration'].rstrip('.ts')

                if _is_local_catchup_channel(provider_stream_id):
                    config = _get_plugin_config()
                    if config['debug_mode']:
                        logger.info(f"[LocalCatchup] URL intercepted (local): {path}")
                    return ResolverMatch(
                        catchup_proxy,
                        (),
                        groups,
                        route=path,
                    )
                else:
                    if _get_plugin_config()['debug_mode']:
                        logger.info(f"[LocalCatchup] URL not local, passing through: {path}")

        # Intercept /local-catchup/inject.js
        if path.startswith('/local-catchup/') or path.startswith('local-catchup/'):
            if INJECT_JS_PATTERN.match(path):
                return ResolverMatch(
                    serve_inject_js,
                    (),
                    {},
                    route=path,
                )

        if not _is_plugin_enabled():
            return _original_resolve(self, path)

        return _original_resolve(self, path)

    # Store the implementation on the class - always updated on each install
    URLResolver._lc_resolve_impl = staticmethod(_resolve_impl)

    # Install (or re-install) a trampoline.
    # The trampoline reads _lc_resolve_impl from the class at call time,
    # so it always dispatches to the latest implementation.
    # Because it only uses class attributes (no module globals or imports),
    # it CANNOT fail with ModuleNotFoundError even in stale workers.
    def _trampoline(self, path):
        impl = getattr(type(self), '_lc_resolve_impl', None)
        if impl is not None:
            return impl(self, path)
        # Fallback: use the stored original
        orig = getattr(type(self), '_lc_orig_resolve', None)
        if orig is not None:
            return orig(self, path)
        raise RuntimeError("LocalCatchup: no resolve implementation found")

    URLResolver.resolve = _trampoline
    URLResolver._lc_trampoline_installed = True
    logger.info("[LocalCatchup] Patched URLResolver.resolve")


# =============================================================================
# Patch 6: ChannelSerializer - Add local_catchup field
# =============================================================================

def _patch_channel_serializer():
    """
    Monkey-patch ChannelSerializer to:
    - Include local_catchup boolean in API responses (to_representation)
    - Intercept local_catchup from PATCH data and save to channel_config (update)
    """
    global _original_to_representation, _original_serializer_update

    from apps.channels.serializers import ChannelSerializer
    from . import channel_config

    _original_to_representation = _get_true_original(ChannelSerializer, 'to_representation')
    _original_serializer_update = _get_true_original(ChannelSerializer, 'update')

    def patched_to_representation(self, instance):
        try:
            data = _original_to_representation(self, instance)
            if _is_plugin_enabled():
                try:
                    data['local_catchup'] = channel_config.is_enabled(instance.id)
                except Exception:
                    data['local_catchup'] = False
            return data
        except ModuleNotFoundError:
            return _original_to_representation(self, instance)

    def patched_update(self, instance, validated_data):
        try:
            if _is_plugin_enabled():
                initial = getattr(self, 'initial_data', {})
                if initial and 'local_catchup' in initial:
                    try:
                        value = initial['local_catchup']
                        channel_config.set_enabled(instance.id, bool(value))
                        logger.info(
                            f"[LocalCatchup] Channel {instance.name} (id={instance.id}): "
                            f"local_catchup={'enabled' if value else 'disabled'}"
                        )
                    except Exception as e:
                        logger.error(f"[LocalCatchup] Failed to save channel config: {e}")
            return _original_serializer_update(self, instance, validated_data)
        except ModuleNotFoundError:
            return _original_serializer_update(self, instance, validated_data)

    ChannelSerializer.to_representation = patched_to_representation
    ChannelSerializer.update = patched_update
    logger.info("[LocalCatchup] Patched ChannelSerializer")


# =============================================================================
# Patch 7: SPA View - Inject <script> tag for inject.js
# =============================================================================

def _patch_middleware():
    """
    Monkey-patch the SPA TemplateView callbacks in urlpatterns to inject
    the <script> tag for inject.js into the HTML response.

    Django's middleware chain is built at startup, so we can't add middleware
    after init. Instead, we wrap the catch-all view callbacks that serve
    index.html.

    Also cleans up any stale middleware entries from previous versions.
    """
    # Clean up stale middleware entry from previous plugin versions
    from django.conf import settings
    stale = 'dispatcharr_local_catchup.hooks.LocalCatchupMiddleware'
    if stale in settings.MIDDLEWARE:
        settings.MIDDLEWARE.remove(stale)
        logger.info("[LocalCatchup] Removed stale middleware entry")

    from dispatcharr import urls as main_urls
    from django.views.generic import TemplateView as TV

    SCRIPT_TAG = b'<script src="/local-catchup/inject.js"></script>'
    patched_count = 0

    def _is_spa_view(pattern):
        """Detect if a URL pattern serves the React SPA (index.html)."""
        view = getattr(pattern, 'callback', None)
        if not view:
            return False

        # Already wrapped by us (with attribute marker)
        if getattr(view, '_local_catchup_wrapped', False):
            return True

        # Check route - SPA catch-all patterns are "" and "<path:unused_path>"
        route = getattr(pattern, 'pattern', None)
        route_str = str(route) if route else ''
        is_catchall_route = (route_str == '' or 'unused_path' in route_str)

        # Approach 1: view_class is TemplateView with template_name='index.html'
        view_cls = getattr(view, 'view_class', None)
        if view_cls is not None:
            try:
                if issubclass(view_cls, TV):
                    initkwargs = getattr(view, 'view_initkwargs', {})
                    if initkwargs.get('template_name') == 'index.html':
                        return True
                    # TemplateView on catch-all route is almost certainly the SPA
                    if is_catchall_route:
                        return True
            except (TypeError, AttributeError):
                pass

        # Approach 2: Already wrapped by a previous plugin load (lost view_class attrs)
        # Detect by function name + module from our hooks module
        view_name = getattr(view, '__name__', '')
        view_module = getattr(view, '__module__', '')
        if view_name == 'wrapped_view' and 'local_catchup' in view_module:
            return True

        # Approach 3: Catch-all route with Django's standard view() function name
        # TemplateView.as_view() returns a function named 'view'
        if is_catchall_route and view_name == 'view':
            return True

        return False

    for pattern in main_urls.urlpatterns:
        if not hasattr(pattern, 'callback'):
            continue

        if not _is_spa_view(pattern):
            continue

        view = pattern.callback

        # Skip already-wrapped views (on re-install or module reload)
        if getattr(view, '_local_catchup_wrapped', False):
            patched_count += 1
            continue
        view_name = getattr(view, '__name__', '')
        view_module = getattr(view, '__module__', '')
        if view_name == 'wrapped_view' and 'local_catchup' in view_module:
            # Previously wrapped but lost _local_catchup_wrapped attr (module reload)
            view._local_catchup_wrapped = True
            patched_count += 1
            continue

        logger.info(f"[LocalCatchup] Found SPA view: {pattern.pattern}")

        # Wrap this view to inject our script tag
        def make_wrapper(orig):
            def wrapped_view(request, *args, **kwargs):
                response = orig(request, *args, **kwargs)

                # TemplateResponse needs to be rendered first
                if hasattr(response, 'render') and callable(response.render):
                    response = response.render()

                try:
                    content = response.content
                    if SCRIPT_TAG not in content and b'</body>' in content:
                        content = content.replace(
                            b'</body>',
                            SCRIPT_TAG + b'\n</body>'
                        )
                        response.content = content
                        response['Content-Length'] = len(response.content)
                except (AttributeError, UnicodeDecodeError):
                    pass

                return response

            wrapped_view._local_catchup_wrapped = True
            # Preserve view_class and view_initkwargs for compatibility
            for attr in ('view_class', 'view_initkwargs', 'cls', 'initkwargs'):
                if hasattr(orig, attr):
                    setattr(wrapped_view, attr, getattr(orig, attr))
            return wrapped_view

        pattern.callback = make_wrapper(view)
        patched_count += 1

    if patched_count > 0:
        logger.info(f"[LocalCatchup] Patched {patched_count} SPA view(s) for script injection")
    else:
        logger.warning("[LocalCatchup] No SPA views found to patch - dumping patterns for debug:")
        for i, p in enumerate(main_urls.urlpatterns):
            cb = getattr(p, 'callback', None)
            route = getattr(p, 'pattern', '?')
            attrs = {}
            if cb:
                for attr in ('view_class', 'view_initkwargs', 'cls', 'initkwargs', '__name__', '__module__'):
                    val = getattr(cb, attr, None)
                    if val is not None:
                        attrs[attr] = val
            logger.warning(f"[LocalCatchup]   [{i}] route={route} attrs={attrs}")
