# Dispatcharr Local Catchup

Local Catchup is a Dispatcharr plugin that records selected live channels to local storage and exposes those recordings through catchup/timeshift URLs.

It is designed to provide catchup playback even when your IPTV provider does not offer native archive support.

## Features

- Continuous background recording for channels you enable.
- Catchup playback from local `.ts` recordings.
- Per-channel `Local Catchup` toggle in the Dispatcharr channel editor UI.
- Automatic hook installation at startup (runtime enable/disable aware).
- Scheduled and manual cleanup by maximum retention days.
- Scheduled and manual cleanup by maximum storage size (oldest files removed first).
- Recorder control actions in plugin UI (`Start Recording`, `Stop Recording`, `Recording Status`, `Run Cleanup Now`).

## How It Works

- The plugin monkey-patches Dispatcharr output/API behavior at runtime.
- Recorded files are stored locally and mapped back to channels/program times.
- `/timeshift/...` requests are intercepted and served from local files when available.
- If no local recording exists for a requested time, catchup returns not found.

## Requirements

- A working Dispatcharr instance with plugin support.
- Python environment used by Dispatcharr with `requests` available.
- Write access to the configured recording directory.
- Sufficient disk space for continuous recording.

## Installation

1. Copy this plugin folder into your Dispatcharr plugins directory as `dispatcharr_local_catchup`.
2. Confirm `plugin.py` and `plugin.json` are at the plugin root.
3. Restart Dispatcharr.
4. In Dispatcharr plugin settings, enable **Local Catchup**.

## Configuration

From the plugin settings UI:

- `Storage Directory`: Folder used for recordings. If blank, defaults to `catchup/` inside the plugin folder.
- `Max Retention (days)`: Deletes recordings older than this.
- `Max Storage (GB)`: Caps disk usage; oldest recordings are removed first.
- `Timezone`: Used for EPG/timestamp conversion and filename alignment.
- `EPG Language`: ISO 639-1 language code used for EPG-related behavior.
- `Debug Mode`: Verbose plugin logging.
- `Auto-Start Recorder`: Starts recorder automatically on Dispatcharr startup.

## Usage

1. Enable the plugin.
2. Open a channel in Dispatcharr and turn on the `Local Catchup` toggle.
3. Start recording using the plugin action (`Start Recording`) or rely on auto-start.
4. Let recordings accumulate.
5. Use your client’s normal catchup/timeshift flow; local recordings will be served when matched.

## Storage Layout

Recordings are written as:

```text
{storage_dir}/
  {channel_id}/
    {YYYY-MM-DD}/
      {HH-MM}_{program_title}.ts
      {HH-MM}_{program_title}.ts.recording
```

- `.ts.recording` is an in-progress file.
- On finalize, it is renamed/merged into `.ts`.

## Actions

- `Start Recording`: Enables recorder and starts worker supervision.
- `Stop Recording`: Signals recorder shutdown across workers.
- `Recording Status`: Returns active workers and usage summary.
- `Run Cleanup Now`: Triggers age + size cleanup immediately.

## Troubleshooting

- Enable `Debug Mode` and inspect Dispatcharr logs for `[LocalCatchup]` messages.
- Verify the storage directory exists and is writable by the Dispatcharr process.
- Confirm channels are explicitly enabled via the `Local Catchup` channel toggle.
- If playback fails, check whether a recording actually exists for the requested timestamp.

## Key Files

- `plugin.py`: Plugin class, actions, startup auto-install.
- `hooks.py`: Runtime patching and route interception.
- `recorder.py`: Background recording manager/workers.
- `storage.py`: File naming, lookup, cleanup, disk usage.
- `views.py`: Catchup file serving and JS endpoint.
- `inject.js`: UI toggle injection and API PATCH integration.
- `channel_config.py`: Per-channel local catchup state persistence.

## Acknowledgements

Special thanks to `cedric-marcoux` for the Timeshift plugin work that helped inform parts of this plugin's approach.

## License

MIT (see `LICENSE`).
