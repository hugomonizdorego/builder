# Ubuntu Builder

Penulisan ulang modern dari [Ubuntu Builder](https://launchpad.net/ubuntu-builder) klasik:
aplikasi desktop untuk membuat distro turunan Ubuntu sendiri (remastering ISO).
Ditulis dengan Python 3.12+ dan PyQt6, dengan pemisahan MVC yang tegas antara
backend dan tampilan.

## Fitur

- **Data rilis langsung dari Ubuntu** — daftar rilis, status dukungan (Supported / ESM / EOL),
  flavor resmi, dan daftar ISO + checksum SHA-256 diambil saat aplikasi berjalan dari
  `releases.ubuntu.com`, `ubuntu.com/about/release-cycle`, `ubuntu.com/desktop/flavors`,
  daftar rilis tim release, serta `cdimage.ubuntu.com`. Tidak ada nomor versi yang ditulis
  mati di kode; hasil terakhir disimpan sebagai cache untuk pemakaian offline.
- **Unduh & ekstrak** — unduhan bisa dilanjutkan (resume), diverifikasi SHA-256, lalu ISO
  diekstrak tanpa di-mount. Bisa juga memakai file `.iso` lokal.
- **Mendukung image berlapis** (Ubuntu 23.04+: `minimal.squashfs`,
  `minimal.standard.squashfs`, …) maupun `filesystem.squashfs` klasik. Lapisan di bawahnya
  di-mount read-only dan digabung dengan overlayfs — cara yang sama dipakai casper saat boot.
- **Terminal chroot tertanam** — shell root di dalam image dengan `/dev`, `/dev/pts`, `/proc`,
  `/sys`, `/run` privat, DNS, dan pool paket ISO di `/cdrom`. Semua mount selalu dilepas,
  termasuk saat error, dibatalkan, atau jendela ditutup.
- **Kustomisasi visual** — nama distro, timeout & argumen kernel GRUB, tema Plymouth,
  wallpaper dan tema GTK/ikon (desktop berbasis GNOME), desktop environment
  (GNOME / KDE / Xfce), paket tambahan, `autoinstall.yaml` Subiquity, Calamares.
- **ISO hybrid BIOS + UEFI** — squashfs dikompres ulang (xz/zstd/gzip/lz4), `md5sum.txt`
  diperbarui, dan ISO dibangun dengan `xorriso` memakai ulang boot record serta partisi EFI
  bertanda tangan dari ISO sumber, sehingga Secure Boot tetap berfungsi.
- **GUI tidak pernah freeze** — semua pekerjaan berat berjalan di `QThread`, dengan progress
  bar, log real-time, dan tombol Cancel.
- **Pre-flight check** paket host saat aplikasi dibuka.

## Instalasi

```bash
./install_host_deps.sh            # tambahkan --with-qemu untuk uji boot ISO
```

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python -m ubuntu_builder
```

Aplikasi berjalan sebagai pengguna biasa. Operasi yang memerlukan root (ekstrak squashfs,
mount, chroot, build) dijalankan lewat `pkexec`, satu kali permintaan sandi per operasi.

### Antarmuka baris perintah

Backend yang sama tersedia tanpa GUI:

```bash
python -m ubuntu_builder.cli preflight
```

```bash
python -m ubuntu_builder.cli inspect ubuntu-26.04.1-desktop-amd64.iso
```

```bash
sudo python -m ubuntu_builder.cli -w ~/remaster extract ubuntu-26.04.1-desktop-amd64.iso
```

```bash
sudo python -m ubuntu_builder.cli -w ~/remaster shell
```

```bash
sudo python -m ubuntu_builder.cli -w ~/remaster build ~/custom.iso --volume-id "Distro Saya 26.04"
```

## Struktur proyek

```
.
├── install_host_deps.sh        # dependensi host (apt)
├── requirements.txt            # dependensi Python
├── pyproject.toml              # metadata paket, entry point, konfigurasi ruff/pytest
├── ubuntu_builder/
│   ├── app.py                  # entry point GUI
│   ├── cli.py                  # CLI + helper ber-hak root untuk GUI (event JSON)
│   ├── models/                 # MODEL — dataclass murni
│   │   ├── release.py          #   UbuntuRelease, Flavor, IsoImage, Catalog
│   │   ├── workspace.py        #   Workspace (tata letak direktori), ProjectState
│   │   └── customization.py    #   CustomizationConfig
│   ├── core/                   # backend, tanpa Qt
│   │   ├── scraper.py          #   pengambilan data rilis/flavor/ISO
│   │   ├── downloader.py       #   unduhan resumable + verifikasi SHA-256
│   │   ├── preflight.py        #   pemeriksaan tool host
│   │   ├── runner.py           #   subprocess dengan output real-time & pembatalan
│   │   ├── iso.py              #   ekstraksi ISO, analisis boot, md5sum, xorriso
│   │   ├── layers.py           #   logika squashfs berlapis
│   │   ├── chroot.py           #   ChrootSession: mount/unmount yang aman
│   │   ├── customizer.py       #   penerapan CustomizationConfig
│   │   ├── backend.py          #   BuilderBackend: alur ekstrak → kustomisasi → build
│   │   └── errors.py, system.py
│   ├── controllers/            # CONTROLLER
│   │   ├── workers.py          #   TaskWorker & PrivilegedWorker (QThread)
│   │   └── main_controller.py  #   menghubungkan sinyal view dengan worker
│   └── views/                  # VIEW — widget Qt, hanya memancarkan sinyal
│       ├── main_window.py      #   navigasi, progress bar, log
│       ├── source_page.py      #   workspace, dropdown rilis/flavor/ISO, pre-flight
│       ├── terminal.py         #   terminal tertanam (pty)
│       ├── customize_page.py
│       └── build_page.py
├── tests/                      # pytest — tanpa root, tanpa jaringan
└── src/, debian/               # kode Gambas/Bash lama (referensi sejarah, tidak dipakai)
```

## Catatan & batasan

- **Image berlapis:** perubahan disimpan di satu lapisan yang dipilih saat ekstrak (bawaan:
  sumber instalasi default installer). Lapisan di atasnya — misalnya lapisan sesi live —
  tetap seperti aslinya; jika lapisan itu membawa salinan sendiri dari file yang Anda ubah
  (contoh: basis data dpkg), sesi live akan menampilkan versi miliknya, sedangkan sistem
  yang ter-install memakai versi Anda.
- **Secure Boot** bergantung pada boot loader bertanda tangan dari ISO sumber. Hanya jika ISO
  sumber sama sekali tidak memiliki boot record, boot image dibuat ulang dengan
  `grub-mkimage`/`mtools`; hasilnya tidak bertanda tangan.
- **Tampilan:** wallpaper dan tema diterapkan lewat GSettings (GNOME, Budgie, Cinnamon).
  KDE Plasma dan Xfce menyimpan bawaannya di tempat lain dan belum diatur otomatis.
- **Tema Plymouth:** splash sesi live hanya berubah bila lapisan yang diedit memuat kernel
  dan casper; selain itu tema berlaku pada sistem hasil instalasi.
- **Terminal tertanam** berorientasi baris (`TERM=dumb`): cocok untuk `apt`, `dpkg`,
  `add-apt-repository`. Untuk program layar penuh (nano, dialog debconf) gunakan tombol
  *Open in system terminal…*.
- Hanya arsitektur amd64 yang diuji.

## Pengembangan

```bash
.venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/pytest
```

```bash
.venv/bin/ruff check ubuntu_builder tests
```

## Lisensi

GPL-3.0-or-later. Ubuntu Builder asli © 2010–2013 Francesco Muriana.
