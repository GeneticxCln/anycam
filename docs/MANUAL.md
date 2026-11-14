# AnyCam user guide

Task by task. Anything not verified on the reference machine (CachyOS, KDE/Wayland, RTX 4060 Ti) is marked
**(not run)**. [README.md](../README.md) has the install steps and the limits.

## Your first recording

1. Start the studio: `anycam gui`.
2. Click an empty slot and choose a camera, a screen, a video or an image.
3. Press **Record** or `Ctrl+R`. Press it again to stop.
4. A notice shows the length, size, picture size and whether the file has sound, with **Play** and
   **Show in folder**. Files go to `~/Videos/AnyCam/` unless you chose a folder in
   **Settings → Output → Recording**.

Recordings are fragmented MP4, so a crash or power cut leaves a file that plays up to the last
half-second. If the notice says the recording was not closed cleanly, it is that case.

## Use AnyCam as a camera in a browser or Discord

1. Turn on the virtual camera (`Ctrl+Shift+V`). The first time, AnyCam offers a one-time setup
   (one password prompt) that installs the v4l2loopback driver settings.
2. In the other app, choose the camera called **AnyCam**. If the app was already open, reopen its camera list.
3. Browsers that cannot see `/dev/video*` cameras can use the PipeWire camera instead:
   **Settings → Output → Publish as**.

If the camera does not show up, open **Settings → Diagnostics**.

## Phone as a camera or remote

**Add → Phone camera (QR code)**, scan the code, accept the certificate warning once and allow the camera.
To switch scenes, mute and record from the phone, turn on **Settings → General → Phone remote**; it is off by
default. Details: [PHONE_CAMERA.md](PHONE_CAMERA.md).

## Go live

1. **Settings → Stream**: pick YouTube, Twitch, Facebook or Kick and paste your stream key, or enter any
   `rtmp://`, `rtmps://` or `srt://` address. Extra destinations receive the same picture.
2. Press **Go Live**.

**Where the key is kept.** Stream keys, SRT passphrases and camera passwords are stored in your desktop
keyring (gnome-keyring, KWallet or KeePassXC), and the project file holds only a `${secret:…}` reference.
`anycam check` says whether a keyring is available. Without one, the secret stays in the project file,
which only you can read. A project copied to another computer asks for the key again.

## Keyboard shortcuts and the command line

Shortcuts are listed and remappable in **Settings → General**. The same actions run from a terminal, which
makes key bindings in your compositor easy:

```bash
anycam ctl list                # every action
anycam ctl record              # start or stop recording
anycam ctl mute                # mute or unmute the microphone
anycam ctl scene "Close up"    # a scene by name, number or id
anycam ctl state               # scenes, recording, mute, preview, virtual camera
```

The answer is one line of JSON. The exit code is 0 when done, 1 when refused and 2 when AnyCam is not
running. Only your own user can connect to the socket. Example key bindings:

```
# Hyprland (hyprland.conf)
bind = SUPER, F9, exec, anycam ctl record
```

KDE: **System Settings → Keyboard → Shortcuts → Add Command**, with the command `anycam ctl record`.
Both key-binding examples are **(not run)**; the `anycam ctl` commands themselves are tested.

## Choose an encoder

AnyCam encodes a few frames with each encoder in a separate process, so a broken driver cannot take the
app down, and offers only the ones that worked. The answer is remembered until the AnyCam version,
GStreamer, the kernel or a graphics driver changes.

```bash
anycam encoders --again        # test again now and print the result
```

The same test runs from **Settings → Output → Video encoder → Test encoders again**. On the reference
machine NVENC H.264, H.265 and AV1 and x264 passed; the VA-API encoders are not installed there, so VA-API
is **(not run)** on real hardware.

## If something goes wrong

- **The log.** Everything AnyCam prints is also written to `~/.local/state/anycam/anycam.log` (secrets
  masked, 512 KiB, one older copy). **Settings → Diagnostics** has **Open log folder** and **Copy log**.
  A crash inside GStreamer or a driver leaves every thread's stack there.
- **After a crash.** The next start tells you the last run did not quit cleanly and what it was doing, and
  offers **safe mode**.
- **Safe mode.** `anycam gui --safe-mode`: CPU canvas, image preview, no virtual camera, no phone link, no
  AI effects, no tray icon, and the preview does not start by itself. Nothing is written to your project;
  restart normally to leave it.
- **A project that will not open.** AnyCam moves the unreadable file to `~/.config/anycam/set-aside/` (never
  deletes it) and offers the last good copy. From a terminal: `anycam repair` checks the project,
  `hotkeys.json` and `phones.json`.
- **Check this computer.** `anycam check` (or **Settings → General → Check this computer**) lists what works
  and the command that fixes the rest.
