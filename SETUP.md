# DJI Footage Sorter: setup

Drop DJI photos and videos (or the whole card) into the window and they get copied into folders like:

```
<your destination>/
  2026-09-12 - Moab, Utah/
    DJI_0003.MP4
    DJI_0003.SRT
    DJI_20260912101500_0001_D.JPG
  2026-09-13 - Salt Lake City, Utah/
    ...
```

## 1. Build the app (one time)

1. Unzip this folder somewhere permanent, like `Documents\DJI Sorter`.
2. Double-click **`Setup (Windows).bat`**.
   - If Python isn't installed, it installs it and asks you to run the setup again.
   - It then builds the **DJI Sorter** app (installed in `%LOCALAPPDATA%\DJISorter\app`), asks whether to install ffmpeg (for LUTs) and exiftool (better video GPS), puts a **DJI Sorter** shortcut on your desktop, and turns on SD card auto-open.
3. Sign out and back in once so Windows finds ffmpeg and exiftool.

After that, the app runs on its own and you don't need Python to use it. It updates itself: each time it opens it checks for a newer version and installs it (the version number is in the window title). You only need to run Setup again if the app tells you to.

Windows SmartScreen may warn the first time because the app isn't signed. Click **More info > Run anyway**.

## 2. Auto-open

With **"Open automatically when a DJI SD card is inserted"** ticked (the setup ticks it for you), a hidden watcher starts when you log in. When a card with a `DCIM` folder of `DJI_` files shows up, the sorter opens with the card already loaded. Untick it in the app to turn it off.

## 3. Use it

Open **DJI Sorter** from the desktop, or just insert a card.

1. Drag files, or the card's `DCIM` folder, into the box (or use **Add files / Add folder**).
2. Pick **Save to**: the dropdown remembers your last 10 destinations; **Browse** picks a new one.
3. **Preview** shows which folders will be made without copying anything. **Sort footage** does it and opens the destination when finished.

Options:
- **Move instead of copy:** empties the card as it goes. Off by default.
- **Include LRF proxy files:** DJI's low-res preview copies. Skipped by default. `.SRT` telemetry files always travel with their video.
- **Look up place names online:** turns GPS into "Town, State" using OpenStreetMap. Offline or turned off, folders use coordinates like `38.573N 109.550W`. Names are cached, so repeat spots don't need the internet.
- **Apply LUT to videos:** pick a `.cube` file (e.g. DJI's D-Log M to Rec.709 LUT) and tick the box once; it stays on, so every sort, including auto-open, grades the videos automatically. Each video gets a `_graded` copy (high-quality H.264) next to the untouched original. Needs ffmpeg.
- **Make Facebook copies:** also saves a copy of everything into a `Facebook` folder inside your destination, with the same date and place folders. Videos there have the audio removed, are cropped to the size you pick (4:5 vertical by default; also 1:1, 9:16 for Reels/Stories, or 16:9) and get your LUT if it's on. Photos are copied as they are. Needs ffmpeg.

While sorting, the bar shows the percentage and an estimate of the time left. It learns your card reader and computer speed after the first file or two, so the first estimate may jump.

## How location and date are found

- Photos: EXIF GPS and capture time.
- Videos: exiftool if installed, otherwise the `.SRT` file DJI writes when "Video Subtitles" is on in DJI Fly, otherwise GPS stored in the MP4.
- A video with no GPS at all borrows the location of the nearest-in-time clip or photo from the same batch (within 2 hours). Otherwise it goes in `Unknown Location`.
- **Tip:** turn on *Camera settings > Video Subtitles* in DJI Fly so every video has GPS.

## Your own spot names (optional)

To name places yourself (e.g. "Bonneville Salt Flats" instead of "Wendover, Utah"), edit `config.json` in `%APPDATA%\DJISorter\` (paste that into the Explorer address bar).

```json
"spots": [
  {"name": "Bonneville Salt Flats", "lat": 40.760, "lon": -113.890, "radius_km": 5}
]
```

The same file has `folder_format` (default `"{date} - {place}"`; also accepts `{year}` and `{month}`, e.g. `"{year}/{date} - {place}"`).
