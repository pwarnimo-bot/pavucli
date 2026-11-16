# pavucli

A terminal mixer for **PulseAudio** and **PipeWire** (via `pipewire-pulse`), in the spirit of `pavucontrol`.

```
 pavucli 0.1.0 ─ PulseAudio (on PipeWire 1.6.11)                                             ? help
  Playback 1   Recording 0   Output Devices 2   Input Devices 1   Configuration
────────────────────────────────────────────────────────────────────────────────────────────────────
 ▶ Built-in Audio Analog Stereo  Sunrise Point-LP HD Audio              Speakers  channels unlocked
     █████████████████████████████████████████▊──┊─────────────────────   95%   -1.3dB  L───●┼────R
   ▸ █████████████████████████████████████████▊──┊─────────────────────   95%   -1.3dB  front-left
     ███████████████████████████████████▋────────┊─────────────────────   81%   -5.5dB  front-right

   Esinkin BT Adapter                                                            ★ default  Headset
     ████████████████████████████████████████████┊─────────────────────  100%   +0.0dB  L────●────R
```

## Features

- Tabs like pavucontrol: **Playback**, **Recording**, **Output Devices**, **Input Devices**, **Configuration**
- Volume bars up to a configurable maximum (default 150 %), with percent and dB values
- **Balance** slider (works for stereo and surround; center/LFE channels are untouched)
- **Per-channel volume**: unlock the channels of any stream or device
- Mute, choose the **default device**, select **ports** (speakers/headphones, …)
- **Move streams** to another output/input device
- Switch **card profiles** (e.g. A2DP ↔ HSP/HFP for Bluetooth, HDMI, surround)
- Updates live (`pactl subscribe`); mouse support (click bars, wheel)
- No Python dependencies beyond the standard library. It only needs `pactl`.

## Requirements

- Python ≥ 3.9
- `pactl` from libpulse **≥ 16** (needs `pactl -f json`). Package names: `libpulse` on Arch, `pulseaudio-utils` on Debian/Ubuntu/Fedora
- A running PulseAudio or PipeWire-pulse server

## Keys

| Key | Action |
| --- | --- |
| `Tab` / `Shift-Tab`, `F1`–`F5` | switch tab |
| `↑` `↓` / `k` `j`, `Home` `End` | select |
| `←` `→` / `h` `l` | volume −/+ step (default 5 %) |
| `H` `L` / `Shift-←` `Shift-→` | volume −/+ 1 % |
| `PgDn` `PgUp` | volume −/+ 10 % |
| `1`…`9`, `0` | set 10 %…90 %, 100 % |
| `m` | mute |
| `[` `]`, `b` / `=` | balance left/right, center |
| `c` / `Space` | lock/unlock channels |
| `Enter` | move stream / choose port / choose profile |
| `p` | choose port |
| `d` | set default device |
| `M` | show monitor sources |
| `?` | help |
| `q` / `Esc` | quit |

## Options

```
pavucli [-t {playback,recording,output,input,config}] [-s PCT] [-m PCT]
        [--ascii] [--no-color] [--no-mouse] [-V]
```

## Running from source

```sh
python -m pavucli
make test
```

## Packaging

| Distro | How |
| --- | --- |
| Arch Linux | `make arch` builds `packaging/arch/pavucli-*.pkg.tar.zst`, then run `sudo pacman -U packaging/arch/pavucli-*.pkg.tar.zst` |
| Debian / Ubuntu | `make deb` (needs `devscripts debhelper dh-python pybuild-plugin-pyproject`) |
| Fedora / openSUSE | `make rpm` (needs `rpm-build pyproject-rpm-macros python3-devel`) |
| Anything else | `make install PREFIX=/usr/local`, or `pipx install .` |

`make dist` creates the source tarball `dist/pavucli-<version>.tar.gz` that all the packages are built from.
To publish on the AUR, point `source=()` in `packaging/arch/PKGBUILD` at a release tarball URL, fill in `url=`,
then run `updpkgsums` and `makepkg --printsrcinfo > .SRCINFO`.

## License

MIT
