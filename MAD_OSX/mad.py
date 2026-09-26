import sys
import time

import bpy
import numpy as np

try:
    import sounddevice as sd
    PORTAUDIO_IMPORT_ERROR = ""
except Exception as error:
    # A missing wheel raises ImportError, a missing system PortAudio library
    # (the normal case on Linux, where no PortAudio binary is bundled) raises
    # OSError. The addon has to stay loadable in both cases so it can explain
    # the problem instead of breaking the whole extension.
    sd = None
    PORTAUDIO_IMPORT_ERROR = f"{type(error).__name__}: {error}"

# Globals
current_volume = 0.0
stream = None
should_run = False
timer = None
last_error = ""
active_backend = ""
status_logged = False
last_drive_error = ""

# Device cache. Every entry maps an enum identifier to one PortAudio device
# index plus the other host API instances of the same physical device, so a
# single list entry can still be opened through a working backend when the
# preferred one fails (a common situation on Windows).
DEVICE_ITEMS = []
DEVICE_INDEX_MAP = {}
DEVICE_FALLBACKS = {}
HOST_API_NAMES = {}

# PortAudio host API identifiers, these are part of the stable PortAudio API.
_HOSTAPI_DIRECTSOUND = 1
_HOSTAPI_MME = 2
_HOSTAPI_ASIO = 3
_HOSTAPI_WASAPI = 6

# Windows exposes virtual capture endpoints that are not real inputs. They are
# pushed to the bottom of the list instead of being hidden, since on some
# machines they are the only entries PortAudio reports.
_VIRTUAL_DEVICE_HINTS = (
    "sound mapper",
    "remote audio",
    "wdm sound device",
    "microsoft wave",
)

# PortAudio error codes translated into something the user can act on.
_ERROR_HINTS = {
    -10000: "The audio subsystem could not be initialised, restart Blender.",
    -9999: "The system audio host reported an error, this is usually the audio driver.",
    -9998: "The system ran out of memory while opening the microphone, close other applications and try again.",
    -9997: "The requested audio settings were rejected, refresh the device list and try again.",
    -9996: "The selected microphone is not a valid input device, refresh the device list and pick another microphone.",
    -9995: "The microphone does not support the sample rate the system offered.",
    -9992: "The microphone stopped responding, close other applications using it and try again.",
    -9985: "The microphone could not be opened, it is most likely already in use by another application. Close the other applications that use the microphone and start MAD again.",
    -9983: "The audio stream was stopped by the system, start MAD again.",
    -9981: "The microphone delivered data faster than Blender could read it, this is not fatal.",
}

# On Windows the Microsoft Basic Audio Driver is the most common reason a USB
# microphone cannot be opened, it is a minimal driver without the APIs that MAD
# prefers, so installing the driver's own software is the actual fix.
_DRIVER_ADVICE = (
    " If the problem persists, install the driver's own software for the "
    "microphone instead of using the Microsoft Basic Audio Driver."
)
_DRIVER_ERROR_CODES = frozenset({-9999, -9996, -9995, -9985})

# On Linux the microphone is usually shared through a sound server, so the
# device is unavailable when the server is not running or when something else
# already records from it.
_LINUX_ADVICE = (
    " Make sure your sound server (PulseAudio or PipeWire) is running and that"
    " no other application is already recording from the microphone."
)

# Errors that are often caused by another application holding the device for a
# moment, so it is worth retrying once before giving up.
_RETRYABLE_ERROR_CODES = frozenset({-9999, -9996, -9992, -9985, -9983, -9981})

_NO_DEVICE_MESSAGE = "No usable input device was found, plug in a microphone and press Refresh Devices."


def is_windows():
    return sys.platform == "win32"


def is_linux():
    return sys.platform.startswith("linux")


def portaudio_missing_message():
    """Explain how to get a working PortAudio on this platform."""
    message = f"PortAudio could not be loaded ({PORTAUDIO_IMPORT_ERROR})."
    if is_linux():
        # No PortAudio binary is bundled for Linux, sounddevice loads the
        # system library instead.
        message += (
            " Install it with your package manager, for example"
            " 'sudo apt install libportaudio2', 'sudo dnf install portaudio'"
            " or 'sudo pacman -S portaudio', then restart Blender."
        )
    else:
        message += (
            " Reinstall the addon so its bundled wheels are unpacked, then"
            " restart Blender."
        )
    return message


def host_api_kind(hostapi, hostapi_name):
    """Classify a PortAudio host API.

    The name is preferred over the numeric id because the ids are only
    guaranteed to be stable inside one PortAudio build.
    """
    name = (hostapi_name or "").lower()
    if "wasapi" in name or (not name and hostapi == _HOSTAPI_WASAPI):
        return "wasapi"
    if "direct sound" in name or "directsound" in name or (not name and hostapi == _HOSTAPI_DIRECTSOUND):
        return "directsound"
    if "asio" in name or (not name and hostapi == _HOSTAPI_ASIO):
        return "asio"
    if any(key in name for key in ("mme", "winsound", "wdm", "wave")) or (not name and hostapi == _HOSTAPI_MME):
        return "mme"
    return "other"


def host_api_rank(kind):
    """Lower is better, the best available backend is tried first."""
    if not is_windows():
        return 0
    # WASAPI is the modern Windows backend and the one that works with the
    # generic Microsoft drivers, DirectSound is what used to fail with
    # "Device unavailable" and the DirectSound host errors.
    return {"wasapi": 0, "directsound": 1, "mme": 2, "other": 3, "asio": 4}.get(kind, 3)


def is_virtual_device(name):
    lowered = name.casefold()
    return any(hint in lowered for hint in _VIRTUAL_DEVICE_HINTS)


def clean_device_name(name):
    """Return a usable device name, or an empty string for unusable ones."""
    if not name:
        return ""
    name = name.replace("\x00", "").strip()
    if not name or any(ord(char) < 32 for char in name):
        return ""
    return name


def refresh_microphones():
    global DEVICE_ITEMS, DEVICE_INDEX_MAP, DEVICE_FALLBACKS, HOST_API_NAMES, last_error

    DEVICE_ITEMS = []
    DEVICE_INDEX_MAP = {}
    DEVICE_FALLBACKS = {}
    HOST_API_NAMES = {}

    if sd is None:
        last_error = portaudio_missing_message()
        print(f"[MAD] {last_error}")
        DEVICE_ITEMS.append(("-1", "No Input Devices Found", ""))
        return

    grouped = {}
    try:
        HOST_API_NAMES = {index: api["name"] for index, api in enumerate(sd.query_hostapis())}
        for index, device in enumerate(sd.query_devices()):
            if device["max_input_channels"] < 1:
                continue

            name = clean_device_name(device["name"])
            if not name:
                continue

            hostapi = device["hostapi"]
            hostapi_name = HOST_API_NAMES.get(hostapi, "")
            kind = host_api_kind(hostapi, hostapi_name)
            rank = host_api_rank(kind)
            if is_virtual_device(name):
                rank += 10

            grouped.setdefault(name.casefold(), []).append(
                (rank, index, hostapi, hostapi_name, kind, name)
            )
    except Exception as error:
        last_error = f"Could not query audio devices: {error}"
        print(f"[MAD] Failed to refresh microphones: {error}")
        DEVICE_ITEMS.append(("-1", "No Input Devices Found", ""))
        return

    entries = []
    for group in grouped.values():
        group.sort(key=lambda entry: entry[0])
        rank, index, hostapi, hostapi_name, kind, name = group[0]

        identifier = str(index)
        entries.append(
            (
                rank,
                name.casefold(),
                identifier,
                name,
                f"Opened with {hostapi_name}" if hostapi_name else "",
            )
        )
        DEVICE_INDEX_MAP[identifier] = index
        DEVICE_FALLBACKS[identifier] = [
            (entry[0], entry[1], entry[2], entry[3], entry[4]) for entry in group
        ]

    entries.sort(key=lambda entry: (entry[0], entry[1]))
    DEVICE_ITEMS = [(identifier, name, description)
                    for _, _, identifier, name, description in entries]

    if not DEVICE_ITEMS:
        DEVICE_ITEMS.append(("-1", "No Input Devices Found", ""))
        last_error = _NO_DEVICE_MESSAGE


def get_microphone_items(self, context):
    return DEVICE_ITEMS


class AudioRigSettings(bpy.types.PropertyGroup):
    mic_list: bpy.props.EnumProperty(
        name="Microphone",
        description="Select input device",
        items=get_microphone_items
    )
    object_ref: bpy.props.PointerProperty(
        name="Object",
        type=bpy.types.Object,
        description="Select the target object"
    )
    property_path: bpy.props.StringProperty(
        name="Property Path",
        description="Data path to drive (e.g. 'location.0', 'rotation_euler.2', 'scale[0]')",
        default="location.0"
    )
    bone_name: bpy.props.EnumProperty(
        name="Bone",
        description="Select bone to drive (if Armature)",
        items=lambda self, context: (
            [(b.name, b.name, "") for b in self.object_ref.pose.bones]
            if self.object_ref and self.object_ref.type == 'ARMATURE' and hasattr(self.object_ref, "pose") else []
        )
    )
    volume_scale: bpy.props.FloatProperty(name="Volume to Value Scale", default=1.0)
    update_interval: bpy.props.FloatProperty(name="Update Interval (s)", default=0.05, min=0.001, max=1.0)


# Audio callback
def audio_callback(indata, frames, time, status):
    global current_volume, status_logged
    if status and not status_logged:
        # Input overflows are normal and are reported for every single block,
        # so only the first message of a stream is printed.
        print(f"[MAD] Stream status: {status}")
        status_logged = True
    volume = np.linalg.norm(indata) / frames
    current_volume = min(volume, 1.0)  # clamp to 1.0 for safety


def error_code(error):
    args = getattr(error, "args", ())
    if len(args) > 1 and isinstance(args[1], int):
        return args[1]
    return None


def host_error(error):
    """Return the (host api, code, text) of a DirectSound/WASAPI failure."""
    args = getattr(error, "args", ())
    if len(args) > 2 and isinstance(args[2], tuple):
        return args[2]
    return None


def describe_error(error):
    """Turn a PortAudio error into a message that explains what to do."""
    args = getattr(error, "args", ())
    text = str(args[0]) if args else str(error)
    code = error_code(error)
    if code is None:
        return f"{text}."

    message = f"{text} [PaErrorCode{code}]."
    hint = _ERROR_HINTS.get(code)
    if hint:
        message = f"{message} {hint}"

    host = host_error(error)
    if host is not None and len(host) == 3:
        message = (
            f"{message} The {HOST_API_NAMES.get(host[0], f'host API {host[0]}')} "
            f"backend reported error {host[1]} ({host[2]})."
        )
    if is_windows() and code in _DRIVER_ERROR_CODES:
        message = f"{message}{_DRIVER_ADVICE}"
    if is_linux() and code in _DRIVER_ERROR_CODES:
        message = f"{message}{_LINUX_ADVICE}"
    return message


def stream_attempts(identifier):
    """Ordered (label, keyword arguments) attempts for opening a stream.

    Every attempt passes a PortAudio device index, which is what makes PortAudio
    open the device through the host API that owns it, so no host API has to be
    named. The first attempts use the selected microphone as the best backend
    sees it, the later ones use the same microphone as the other host APIs
    report it (WASAPI, DirectSound and MME on Windows, ALSA, PulseAudio or JACK
    on Linux), and the last one lets PortAudio open the system default input.
    """
    device = DEVICE_INDEX_MAP.get(identifier)
    if device is None:
        return []

    base = {"callback": audio_callback, "channels": 1, "dtype": "float32"}
    fallbacks = sorted(DEVICE_FALLBACKS.get(identifier) or [],
                       key=lambda entry: entry[0])
    attempts = []
    used = set()

    for _, index, _hostapi, hostapi_name, kind in fallbacks:
        if kind == "asio" or index in used:
            # ASIO is only reachable when PortAudio was built with it, the
            # bundled libraries are not, so it is never picked automatically.
            continue
        used.add(index)
        label = hostapi_name or "system audio"
        if kind == "wasapi":
            # Shared mode is the reliable one for the generic Microsoft
            # drivers, auto convert avoids sample rate mismatches.
            attempts.append((
                f"{label} (shared, auto convert)",
                dict(base, device=index,
                     extra_settings=sd.WasapiSettings(exclusive=False, auto_convert=True))
            ))
            attempts.append((
                f"{label} (shared)",
                dict(base, device=index,
                     extra_settings=sd.WasapiSettings(exclusive=False))
            ))
        else:
            attempts.append((label, dict(base, device=index)))

    attempts.append(("system default", dict(base, device=None)))
    return attempts


def try_stream_attempts(attempts):
    """Run every attempt until one starts, returns (stream, label, error)."""
    error = None
    for label, kwargs in attempts:
        candidate = None
        try:
            candidate = sd.InputStream(**kwargs)
            candidate.start()
            return candidate, label, None
        except Exception as exc:
            error = exc
            print(f"[MAD] {label}: {exc}")
            if candidate is not None:
                try:
                    candidate.close()
                except Exception:
                    pass
    return None, "", error


def open_input_stream(identifier):
    """Open a stream for the given device, falling back over host APIs."""
    global status_logged

    if sd is None:
        return None, "", portaudio_missing_message()

    attempts = stream_attempts(identifier)
    if not attempts:
        return None, "", _NO_DEVICE_MESSAGE

    for attempt in range(2):
        candidate, label, error = try_stream_attempts(attempts)
        if candidate is not None:
            status_logged = False
            return candidate, label, ""

        if attempt == 0 and error_code(error) in _RETRYABLE_ERROR_CODES:
            # The device is regularly only busy for a moment, for instance when
            # another application is shutting down, so give it a second try.
            time.sleep(0.25)

    return None, "", describe_error(error)


def set_property_path(target, path, value):
    """Write value into a data path such as 'location.0' or 'scale[0]'."""
    parts = [part for part in path.replace("[", ".").replace("]", "").split(".") if part]
    for part in parts[:-1]:
        target = target[int(part)] if part.isdigit() else getattr(target, part)
    last = parts[-1]
    if last.isdigit():
        target[int(last)] = value
    else:
        setattr(target, last, value)


# Blender-safe update loop
def report_drive_error(message):
    """Print a driving problem once instead of on every update."""
    global last_drive_error
    if not message:
        last_drive_error = ""
        return
    if message != last_drive_error:
        last_drive_error = message
        print(f"[MAD] {message}")


def update_bone_rotation():
    global should_run
    if not should_run:
        return None

    scene = bpy.context.scene
    s = scene.audio_rig_settings
    obj = s.object_ref
    # Update the UI audio level property
    scene.mad_audio_level = current_volume

    if not obj:
        return s.update_interval

    try:
        # If Armature and bone_name is set, drive the bone property
        if obj.type == 'ARMATURE' and s.bone_name:
            bone = obj.pose.bones.get(s.bone_name)
            if bone is None:
                report_drive_error(f"Bone not found: {s.bone_name}")
                return s.update_interval
            set_property_path(bone, s.property_path, current_volume * s.volume_scale)
        else:
            # Drive the property on the object itself
            set_property_path(obj, s.property_path, current_volume * s.volume_scale)
        report_drive_error("")
    except Exception as e:
        report_drive_error(f"Failed to set property: {e}")

    return s.update_interval  # reschedule


def stop_timer():
    global timer
    if timer is not None:
        try:
            bpy.app.timers.unregister(timer)
        except (ValueError, TypeError):
            pass
        timer = None


def stop_stream():
    global stream, should_run, current_volume
    stop_timer()
    should_run = False
    if stream is not None:
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass
        stream = None
    current_volume = 0.0


# Operators
class AUDIO_OT_Start(bpy.types.Operator):
    bl_idname = "wm.audio_driver_ui_start"
    bl_label = "Start MAD"
    bl_description = "Start MAD audio driver"
    bl_options = {'REGISTER'}

    def execute(self, context):
        global stream, should_run, last_error, active_backend, timer

        if sd is None:
            last_error = portaudio_missing_message()
            self.report({'ERROR'}, last_error)
            return {'CANCELLED'}

        s = context.scene.audio_rig_settings
        identifier = s.mic_list
        if identifier not in DEVICE_INDEX_MAP:
            last_error = "Select a microphone first, then press Refresh Devices if the list is empty."
            self.report({'ERROR'}, last_error)
            return {'CANCELLED'}

        # A previous run may still hold the device open.
        stop_stream()
        context.scene.mad_audio_level = 0.0

        new_stream, label, error = open_input_stream(identifier)
        if new_stream is None:
            last_error = error
            self.report({'ERROR'}, error)
            return {'CANCELLED'}

        stream = new_stream
        active_backend = label
        last_error = ""
        should_run = True

        timer = bpy.app.timers.register(update_bone_rotation, first_interval=0.0)
        print(f"[MAD] Microphone stream started ({label}).")
        return {'FINISHED'}


class AUDIO_OT_Stop(bpy.types.Operator):
    bl_idname = "wm.audio_driver_ui_stop"
    bl_label = "Stop Audio Driver"
    bl_description = "Stop MAD audio driver"
    bl_options = {'REGISTER'}

    def execute(self, context):
        stop_stream()
        context.scene.mad_audio_level = 0.0
        return {'FINISHED'}


class AUDIO_OT_RefreshMics(bpy.types.Operator):
    bl_idname = "wm.audio_refresh_mics"
    bl_label = "Refresh Devices"

    def execute(self, context):
        global last_error
        last_error = ""
        refresh_microphones()
        if last_error:
            self.report({'WARNING'}, last_error)
        else:
            self.report({'INFO'}, "Microphone list refreshed")
        return {'FINISHED'}


def wrap_error(message):
    """Split a long error message so it stays readable in the panel."""
    words = message.split()
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > 56 and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [message]


# UI Panel
class AUDIO_PT_MicDriverPanel(bpy.types.Panel):
    bl_label = "MAD"
    bl_idname = "AUDIO_PT_mic_driver_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MAD"

    def draw(self, context):
        layout = self.layout
        s = context.scene.audio_rig_settings
        global should_run, last_error, active_backend

        layout.prop(s, "mic_list")
        layout.operator("wm.audio_refresh_mics", icon='FILE_REFRESH')
        layout.prop(s, "object_ref")
        if s.object_ref and s.object_ref.type == 'ARMATURE':
            layout.prop(s, "bone_name")
        layout.prop(s, "property_path")
        layout.prop(s, "volume_scale")
        layout.prop(s, "update_interval")

        row = layout.row()
        row.operator("wm.audio_driver_ui_start", text="Start")
        row.operator("wm.audio_driver_ui_stop", text="Stop")

        # Indicator for active state and audio level
        if should_run:
            layout.label(text=f"Audio Driver: ACTIVE ({active_backend})", icon='PLAY')
            # Draw a progress bar for the current audio level
            layout.prop(
                context.scene,
                'mad_audio_level',
                text="Audio Level",
                slider=True
            )
        else:
            layout.label(text="Audio Driver: Inactive", icon='PAUSE')

        if last_error:
            box = layout.box()
            lines = wrap_error(last_error)
            box.label(text=lines[0], icon='ERROR')
            for line in lines[1:]:
                box.label(text=line)


# --- Add a property to hold the audio level for UI updates ---
def ensure_audio_level_property():
    if not hasattr(bpy.types.Scene, "mad_audio_level"):
        bpy.types.Scene.mad_audio_level = bpy.props.FloatProperty(
            name="Audio Level",
            description="Current audio input level",
            default=0.0,
            min=0.0,
            max=1.0
        )


# Register
classes = (
    AudioRigSettings,
    AUDIO_OT_Start,
    AUDIO_OT_Stop,
    AUDIO_OT_RefreshMics,
    AUDIO_PT_MicDriverPanel,
)


def register():
    ensure_audio_level_property()
    refresh_microphones()
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.audio_rig_settings = bpy.props.PointerProperty(type=AudioRigSettings)


def unregister():
    stop_stream()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.audio_rig_settings
    if hasattr(bpy.types.Scene, "mad_audio_level"):
        del bpy.types.Scene.mad_audio_level


if __name__ == "__main__":
    register()
