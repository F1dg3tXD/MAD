# MAD
Microphone Audio Driver: A driver assignment tool for Blender.

Latest release: **v0.1.7**

## Supported platforms

| Platform | Package |
| --- | --- |
| Windows x64 / ARM64 | `MAD` |
| Linux x64 | `MAD` |
| macOS (Apple Silicon and Intel) | `MAD_OSX` |

macOS ships as a separate package because it needs its own wheels. On Linux, PortAudio
itself is **not** bundled (sounddevice publishes no Linux binary wheel), so install it
once with your package manager if MAD reports that PortAudio could not be loaded:

```sh
sudo apt install libportaudio2     # Debian, Ubuntu
sudo dnf install portaudio         # Fedora
sudo pacman -S portaudio           # Arch
```

## What's new in v0.1.7

- Bundled Python wheels for **CPython 3.11, 3.13 and 3.14**, so MAD installs on every
  current Blender release (Blender 5.0 uses Python 3.11, Blender 5.1/5.2 use 3.13).
- Updated `sounddevice` to 0.5.6 and `cffi` to 2.1.1, with `windows-arm64` support.
- **Linux is now supported** again, with `manylinux` wheels for Linux x64.
- **Windows:** devices are opened through WASAPI first, so the
  *Microsoft Basic Audio Driver* (and the resulting `PaErrorCode-9985`, `-9996`, `-9999`
  and DirectSound errors such as `-2005401590`) is no longer the first thing MAD tries.
  If WASAPI fails, MAD retries through WASAPI shared, DirectSound, MME and finally the
  system default device.
- **Windows:** duplicate entries are removed from the microphone list (one device used to
  show up 4-5 times, once per host API) and virtual devices such as *Sound Mapper* are
  ranked last.
- The selected microphone is now actually opened on every platform, instead of silently
  falling back to the system default input.
- Clearer, actionable error messages when a device cannot be opened.
- macOS and Windows/Linux builds now ship as separate packages with their own wheels.

# Switching to v0.1.7 on MacOS
## Uninstalling MAD (Microphone Audio Driver) on macOS

Follow these steps to fully remove the MAD addon from Blender.

---

## 1. Remove the Addon via Blender UI

1. Open **Blender**.
2. Go to **Edit → Preferences**.
3. Click the **Extensions** tab (or the **Add-ons** tab on older versions).
4. In the search bar, type **MAD** or **Microphone Audio Driver**.
5. Click the **Remove** button next to the addon.
6. (Optional) Restart Blender to complete the removal.

---

## 2. Manually Delete Leftover Addon Files (if necessary)

1. Open **Finder**.
2. Press `Cmd + Shift + G` to open the **Go to Folder** dialog.
3. Enter the following path:
   `~/Library/Application Support/Blender/`
4. Open the folder corresponding to your Blender version, for example: `5.0/scripts/addons`
5. Look for the `mad` folder (or the folder you used when installing MAD) and **delete it**.

---

## 3. (Optional) Remove Installed Python Dependencies

v0.1.7 ships its own wheels, so nothing needs to be installed. This step is only needed if
you previously installed `sounddevice`/`cffi` yourself (for example with the old
`install_mad_dependencies.py` script) and want to clean them up:

1. In Blender, open the **Scripting tab**.
2. In the Python console, run:

```python
import sys
print(sys.executable)
```

This will print the path to Blender's internal Python.

3. Open Terminal and run:

```sh
"/path/to/blender/python/bin/python3.11" -m pip uninstall sounddevice cffi
```

Use the interpreter path from step 2. The `python3.11` in the middle of the path depends
on your Blender version (3.11 for Blender 5.0, 3.13 for Blender 5.1/5.2), so copy the path
exactly as printed.

# Installing v0.1.7 OSX
MAD should now be fully uninstalled from your system.

- Go get the latest version from the [releases tab](https://github.com/F1dg3tXD/MAD/releases).
- Unzip the file.
- Install the addon: **Edit → Preferences → Extensions → Install from Disk**, and select
  the unzipped `blender_manifest.toml` folder or the `MAD_OSX` folder inside the zip.
- `install_mad_dependencies.py` is optional now. The required wheels are bundled and are
  installed with the extension, so the addon works without running it.
- Restart Blender, then open the addon in the 3D viewport sidebar: **N → MAD**.
