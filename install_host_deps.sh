#!/usr/bin/env bash
#
# Install the host packages OS Builder needs (Ubuntu 22.04 or newer,
# Debian and derivatives).
#
#   ./install_host_deps.sh              required + recommended packages
#   ./install_host_deps.sh --minimal    only what is strictly required
#   ./install_host_deps.sh --with-qemu  also QEMU + OVMF for test-booting ISOs
#
set -euo pipefail

REQUIRED=(
    squashfs-tools   # unsquashfs / mksquashfs
    xorriso          # read ISOs, build hybrid BIOS+UEFI images
    coreutils        # chroot
    mount            # mount / umount
    python3          # the application itself
    python3-venv     # to create the virtual environment below
)
# Fallback boot image generation, only used when a source ISO carries no
# reusable boot record.
RECOMMENDED=(
    mtools
    grub-pc-bin
    grub-efi-amd64-bin
    libegl1          # Qt runtime libraries missing on minimal installs
    libxkbcommon0
    libxcb-cursor0
    xserver-xephyr   # "Desktop": the image's session in a window
    openssl          # hashes the installer password
    python3-jsonschema  # checks autoinstall.yaml against Subiquity's schema
)
QEMU=(
    qemu-system-x86
    qemu-utils
    ovmf
)

mode="default"
for arg in "$@"; do
    case "$arg" in
        --minimal)   mode="minimal" ;;
        --with-qemu) mode="qemu" ;;
        -h|--help)   sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

if ! command -v apt-get >/dev/null 2>&1; then
    echo "This script needs apt (Ubuntu/Debian). Install these packages manually:" >&2
    printf '  %s\n' "${REQUIRED[@]}" "${RECOMMENDED[@]}" >&2
    exit 1
fi

# pkexec became its own package in Ubuntu 22.10; older releases ship it in
# policykit-1.
if apt-cache show pkexec >/dev/null 2>&1; then
    REQUIRED+=(pkexec)
else
    REQUIRED+=(policykit-1)
fi

packages=("${REQUIRED[@]}")
[[ "$mode" != "minimal" ]] && packages+=("${RECOMMENDED[@]}")
[[ "$mode" == "qemu" ]] && packages+=("${QEMU[@]}")

SUDO=""
if [[ $EUID -ne 0 ]]; then
    command -v sudo >/dev/null 2>&1 || { echo "Run this script as root (sudo not found)." >&2; exit 1; }
    SUDO="sudo"
fi

echo "Installing: ${packages[*]}"
$SUDO apt-get update
$SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${packages[@]}"

cat <<'EOF'

Host packages installed. Next, the Python side:

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    PATH="$PWD/.venv/bin:$PATH" src/os-builder/os-builder
EOF
