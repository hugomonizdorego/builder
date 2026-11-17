# OS Builder

**Build your own Ubuntu based operating system.**

OS Builder turns an official Ubuntu live image into your own distribution — for example
*DebION GNU/Linux* — and writes a bootable ISO for legacy BIOS and UEFI PCs with Secure Boot.
It is a rewrite, in Python 3.12+ and Qt 6, of [Ubuntu Builder](https://launchpad.net/ubuntu-builder)
2.5.2 by Francesco Muriana (2010–2014), and keeps that program's window and workflow:
**Select ISO → Console / Synaptic / Desktop / Wizard → Build → QEMU**.

![OS Builder](docs/screenshot.png)

## Features

| Button | What it does |
|---|---|
| **Select ISO** | Use a local `.iso`, or download an official image. Releases (20.04 LTS … 26.10), their support status (supported / ESM / end of life), the official flavors and the images are fetched live from ubuntu.com; downloads resume and are verified with SHA-256. |
| **Wizard** | Packages to add or remove, desktop and login manager, GRUB menu (timeout, kernel arguments), Plymouth splash, wallpaper and GTK/icon theme, Subiquity `autoinstall.yaml`, Calamares, shell commands run inside the image; optionally build right after. |
| **Build** | Applies the Distribution settings, repacks the squashfs (xz, zstd, gzip, lz4), regenerates `md5sum.txt` and the installer metadata, and writes the ISO with `xorriso` — **hybrid** (legacy BIOS + UEFI) or **UEFI only** for modern PCs without CSM. |
| **QEMU** | Boot the ISO in a virtual machine: legacy BIOS, UEFI, or UEFI with **Secure Boot** enabled (OVMF with Microsoft keys). |
| **Export** | Show in the file manager, write to a USB stick (GNOME Disks, usb-creator, mintstick, ISO Image Writer) or burn (Brasero, K3b, Xfburn). |
| **Remove** | Delete the extracted image (downloaded ISOs are kept). |
| **Settings** | Workspace folder, dark or light toolbar, Desktop window size, host requirements check. |
| **Console** | Root shell inside the image (embedded, or in the system terminal). |
| **Synaptic** | Synaptic from inside the image, shown on your display (X11 or XWayland). |
| **Desktop** | Starts the image's desktop session in a window (Xephyr) to try it out. |
| **Install .deb** | Installs local `.deb` files into the image; apt resolves their dependencies. |

The **Distribution** panel sets the name (e.g. `DebION GNU/Linux`), description, default locale
(`en_US.UTF-8` for new workspaces), console welcome message and removal of Windows autorun files.

### Desktops, window managers and login managers

Every package below was checked against the Ubuntu 26.04 LTS archive.

| Kind | Choices |
|---|---|
| Desktop environments | GNOME 50 (minimal or full), GNOME Flashback, KDE Plasma 6.6 (Kubuntu or minimal), Xfce 4.20 (Xubuntu or minimal), LXQt (Lubuntu), MATE, Budgie 10.10, Cinnamon 6.4, Unity, Edubuntu, Ubuntu Studio, Ubuntu Kylin, LXDE, Enlightenment |
| Wayland compositors | Hyprland 0.53, Sway 1.11, labwc 0.9, Wayfire 0.10 — each with foot, fuzzel, waybar, mako, wallpaper, screenshots and portals |
| X11 window managers | i3, Openbox, awesome, bspwm, Qtile, xmonad, herbstluftwm, IceWM, Fluxbox |
| Login managers | GDM 50, SDDM 0.21, LightDM + Slick Greeter, LightDM + GTK Greeter, greetd + tuigreet, LXDM, XDM |

Each desktop brings its usual login manager unless you choose another one. The choice is preseeded
through debconf and linked as `display-manager.service`. The **Desktop** button can run full X11
desktops, the X11 window managers and the wlroots compositors (Sway, labwc, Wayfire) in its window;
GNOME 49+ (Wayland only) and Hyprland cannot be nested — test those with QEMU.

### UEFI and Secure Boot

- The boot records of the source image — MBR, GPT, El Torito catalog and the EFI system partition
  with Canonical's **signed shim** — are reused, so the new ISO boots with Secure Boot enabled.
- **Hybrid** (default) boots on legacy BIOS and UEFI. **UEFI only** leaves out the BIOS boot code
  for modern machines; it was tested to boot under OVMF with Secure Boot and to be refused by BIOS.
- The Distribution panel and `os-builder-cli boot-info` show the boot support of the image,
  including an Authenticode signature check of the EFI loader.
- Only when a source image has no boot record at all are new GRUB images generated with
  `grub-mkimage` and `mtools`; those are unsigned and need Secure Boot disabled.

## Install

### Debian package (recommended)

```bash
sudo apt install debhelper
```

```bash
dpkg-buildpackage -us -uc -b
```

```bash
sudo apt install ../os-builder_3.1.0_all.deb
```

Start **OS Builder** from the application menu, or run `os-builder`. The program runs as your
user; polkit asks for the administrator password for extracting, chroot sessions and building
(`/usr/libexec/os-builder/os-builder-helper`, action `org.os-builder.helper`).

### From source

```bash
./install_host_deps.sh --with-qemu
```

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
PATH="$PWD/.venv/bin:$PATH" src/os-builder/os-builder
```

### Requirements

- Host: Ubuntu 24.04 LTS or newer (or Debian 13), amd64, about 20 GB free disk space.
- Required: `python3` (3.12+), `python3-pyqt6`, `python3-requests`, `python3-bs4`, `python3-lxml`,
  `python3-yaml`, `squashfs-tools`, `xorriso`, `pkexec`.
- Recommended: `mtools`, `grub-pc-bin`, `grub-efi-amd64-bin`, `qemu-system-x86`, `ovmf`,
  `xserver-xephyr`.
- Source images: Ubuntu 20.04 LTS to 26.10 and the official flavors (classic and layered squashfs).

## Command line

```bash
sudo os-builder-cli -w ~/debion extract ubuntu-26.04.1-desktop-amd64.iso
```

```bash
echo '{"distro_name": "DebION GNU/Linux", "locale": "en_US.UTF-8", "desktop": "kde", "display_manager": "sddm"}' > debion.json
```

```bash
sudo os-builder-cli -w ~/debion customize debion.json
```

```bash
sudo os-builder-cli -w ~/debion build ~/DebION-GNU-Linux-26.04.iso --volume-id "DebION 26.04" --boot-mode hybrid
```

Other commands: `preflight`, `inspect`, `shell`, `boot-info`, `synaptic`, `desktop`, `cleanup`,
`delete` — see `os-builder-cli --help` and `man os-builder-cli`.

## Source layout

Follows the layout of the original source tarball (`src/<name>/` and `debian/`):

```
.
├── debian/                        Debian packaging (debhelper 13, native)
├── src/os-builder/
│   ├── os-builder                 GUI launcher          → /usr/bin
│   ├── os-builder-cli             CLI launcher          → /usr/bin
│   ├── os-builder-helper          privileged helper     → /usr/libexec/os-builder
│   ├── org.os-builder.policy      polkit action
│   ├── os-builder.png             logo
│   ├── icons/{Dark,Light}/        toolbar icons (from Ubuntu Builder 2.x)
│   └── os_builder/                Python package        → /usr/share/os-builder
│       ├── models/                MODEL: releases, workspace, customization, desktops
│       ├── core/                  backend without Qt: scraper, downloader, preflight, runner,
│       │                          iso, layers, chroot, customizer, graphical, backend
│       ├── controllers/           CONTROLLER: main controller, QThread workers
│       ├── views/                 VIEW: main window, dialogs, terminal, widgets
│       ├── app.py                 GUI entry point
│       └── cli.py                 command line and privileged helper
├── legacy/2.5.2/                  original Ubuntu Builder 2.5.2 scripts, for reference
├── tests/                         pytest, no root and no network needed
└── install_host_deps.sh, requirements.txt, pyproject.toml
```

## Limitations

- Layered images (Ubuntu 23.04+): changes go into the one layer chosen at extraction (by default
  the installer's default source); layers above it, such as the live session layer, are unchanged.
- Wallpaper and themes are applied through GSettings (GNOME based desktops only).
- greetd and XDM have no automatic login in the live session; the live user logs in at the prompt.
- Debian live images (`live/filesystem.squashfs`) are not supported yet; only Ubuntu images.
- Only amd64 has been tested.

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/pytest && .venv/bin/ruff check src tests
```

## License

GPL-3.0-or-later. Ubuntu Builder 2.x and its icons © 2010–2014 Francesco Muriana (GPL-2.0-or-later).
Maintainer: hugomonizdorego <hugomonizdorego@gmail.com>.
