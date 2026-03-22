# Unistellar Telescope Data Downloader

Reliable command-line tool to download FITS, TIFF, or PNG observation data from Unistellar telescopes (eVscope, eQuinox, Odyssey) over WiFi.

## Why?

The official Unistellar web UI at `http://192.168.100.1` bundles all selected observations into one giant zip download. On the telescope's Raspberry Pi WiFi, this frequently fails mid-transfer — especially for large science observations with thousands of FITS frames.

This tool fixes that by:
- **Downloading one observation at a time** — a failure doesn't lose the others
- **Auto-retrying on WiFi drops** — up to 10 attempts per observation
- **Concurrent event polling** — required by the telescope's backend to stream data (the key discovery that makes this work)
- **Validating downloads** — verifies zip integrity, won't skip corrupted files
- **Canceling stuck downloads** — automatically clears stale telescope state before starting

## Install

```bash
pip install unistellar-downloader
```

Or clone and run directly:
```bash
git clone https://github.com/vamshikesireddy/unistellar-downloader.git
cd unistellar-downloader
pip install .
```

## Usage

1. **Connect your laptop WiFi to the telescope** (the network named like `eVscope-XXXX` or `UNI-XXXX`)
2. Run:

```bash
unistellar-download
```

That's it. The tool will:
- Connect to the telescope at `192.168.100.1`
- List all observations stored on the telescope
- Let you pick which ones to download and in what format
- Download each one individually with progress and retry

### Options

```bash
# Just list what's on the telescope
unistellar-download --check

# Download everything as FITS without prompting
unistellar-download --all --format fits

# Download to a specific folder
unistellar-download --output ~/my_observations

# Cancel a stuck download on the telescope
unistellar-download --cancel
```

### Alternative ways to run

```bash
# As a Python module
python -m unistellar_downloader

# Directly (without installing)
python unistellar_download.py
```

## Supported Telescopes

Works with any Unistellar telescope that uses the `Direct Data Download` web interface at `http://192.168.100.1`:

- **eVscope** / **eVscope 2**
- **eQuinox** / **eQuinox 2**
- **Odyssey** / **Odyssey Pro**

All models run the same `evsoft` firmware and expose the same API.

## How It Works

The telescope runs a Raspberry Pi with a web server. The official web UI is a Vue.js app that communicates via:

| Endpoint | Purpose |
|----------|---------|
| `GET /api/observations/list` | List all stored observations |
| `GET /api/observations/zip/{format}/0x0/{vpaths}` | Download observations as zip |
| `GET /api/event` | Long-poll for download progress events |
| `POST /api/event` | Send commands (e.g., cancel download) |

**Key discovery:** The telescope backend will not stream zip data unless a client is actively long-polling `/api/event`. The browser's Vue.js app does this automatically. This tool replicates that behavior with a background `EventPoller` thread, which is why it works where a simple `requests.get()` to the zip URL would hang and return 502.

## Requirements

- Python 3.9+
- `requests` (installed automatically)

## License

MIT
