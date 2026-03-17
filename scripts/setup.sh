#!/usr/bin/env bash
# scripts/setup.sh — Automated system dependency installer for the Polymarket Weather Bot.
#
# Installs:
#   - eccodes (required for ECMWF GRIB2 parsing via cfgrib)
#
# Supports:
#   - Ubuntu / Debian (apt)
#   - macOS (Homebrew)
#
# Usage:
#   bash scripts/setup.sh

set -euo pipefail

echo "Polymarket Weather Bot — System Dependency Setup"
echo "================================================="

OS="$(uname -s)"

install_eccodes_linux() {
    echo "Detected Linux (Ubuntu/Debian)"
    if ! command -v apt-get &>/dev/null; then
        echo "ERROR: apt-get not found. Install eccodes manually:"
        echo "  See: https://confluence.ecmwf.int/display/ECC/ecCodes+installation"
        exit 1
    fi
    echo "Installing eccodes via apt-get..."
    sudo apt-get update -q
    sudo apt-get install -y libeccodes-dev eccodes
    echo "eccodes installed successfully."
}

install_eccodes_macos() {
    echo "Detected macOS"
    if ! command -v brew &>/dev/null; then
        echo "ERROR: Homebrew not found. Install it first:"
        echo "  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
        exit 1
    fi
    echo "Installing eccodes via Homebrew..."
    brew install eccodes
    echo "eccodes installed successfully."
}

case "$OS" in
    Linux*)
        install_eccodes_linux
        ;;
    Darwin*)
        install_eccodes_macos
        ;;
    *)
        echo "ERROR: Unsupported OS: $OS"
        echo "Please install eccodes manually:"
        echo "  https://confluence.ecmwf.int/display/ECC/ecCodes+installation"
        exit 1
        ;;
esac

echo ""
echo "System dependencies installed."
echo ""
echo "Next steps:"
echo "  1. pip install -r requirements.txt"
echo "  2. cp .env.example .env && chmod 600 .env"
echo "  3. python main.py"
