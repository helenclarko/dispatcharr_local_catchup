"""
Dispatcharr Local Catchup Plugin

Continuously records selected channels locally and serves those recordings
for catchup/timeshift playback. Enables catchup for channels even when the
IPTV provider doesn't support it.

Note: Auto-install logic is in plugin.py (which Dispatcharr imports directly).
"""

from .plugin import Plugin

__all__ = ['Plugin']
__version__ = '1.0.0'
