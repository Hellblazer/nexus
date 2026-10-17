#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Runs inside the build VM as admin (build-image.sh). Installs uv and Claude
# Code with their own installers into ~/.local/bin, checks the properties the
# MVV row depends on, and leaves no nx behind. Exits non-zero on any miss.
set -euo pipefail
export PATH="$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
curl -fsSL https://claude.ai/install.sh | bash >/dev/null
[[ -x "$HOME/.local/bin/uv" ]] || { echo "provision: uv did not install into ~/.local/bin" >&2; exit 1; }
[[ -x "$HOME/.local/bin/claude" ]] || { echo "provision: claude did not install into ~/.local/bin" >&2; exit 1; }

echo "provision: $(sw_vers -productName) $(sw_vers -productVersion) ($(sw_vers -buildVersion))"
echo "provision: uv $("$HOME/.local/bin/uv" --version)"
echo "provision: claude $("$HOME/.local/bin/claude" --version)"
fv="$(fdesetup status)"
[[ "$fv" == *"FileVault is Off"* ]] || { echo "provision: FileVault is not off: $fv" >&2; exit 1; }
al="$(defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser 2>/dev/null || true)"
[[ "$al" == "$(id -un)" ]] || { echo "provision: automatic login is '$al', not $(id -un)" >&2; exit 1; }
sudo -n true || { echo "provision: admin has no passwordless sudo (the reboot step needs it)" >&2; exit 1; }
if command -v nx >/dev/null 2>&1 || [[ -e "$HOME/.config/nexus" ]] || [[ -e "$HOME/Library/LaunchAgents/com.nexus.service.plist" ]]; then
    echo "provision: nx or its state is present; the clean image must have none" >&2; exit 1
fi
echo "provision: FileVault off, automatic login $al, passwordless sudo, no nx"
