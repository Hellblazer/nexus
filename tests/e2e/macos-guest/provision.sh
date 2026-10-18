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

# The guest hop. (1) Host keys unique to this image: the source image ships keys
# that every copy of it shares, whose private halves are public, so pinning them
# would pin nothing. Regenerated here, they exist only on our disk and in
# hellmini's known_hosts. (2) Key-only ssh, set in a drop-in that sorts before
# 100-macos.conf (sshd takes the first value it sees). The admin password stays
# "admin": changing it breaks the auto-login that kcpassword records, which the
# reboot row depends on; with password and keyboard-interactive login off it
# cannot be used over the network.
[[ -s "$HOME/.ssh/authorized_keys" ]] || { echo "provision: no authorized key for admin; not closing password ssh" >&2; exit 1; }
sudo -n rm -f /etc/ssh/ssh_host_*
sudo -n ssh-keygen -A >/dev/null
[[ -s /etc/ssh/ssh_host_ed25519_key.pub ]] || { echo "provision: no ed25519 host key after ssh-keygen -A" >&2; exit 1; }
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\n' | sudo -n tee /etc/ssh/sshd_config.d/050-nexus-keyonly.conf >/dev/null
sudo -n chmod 644 /etc/ssh/sshd_config.d/050-nexus-keyonly.conf
eff="$(sudo -n /usr/sbin/sshd -T 2>/dev/null)"
grep -qix 'passwordauthentication no' <<<"$eff" || { echo "provision: sshd still allows password authentication" >&2; exit 1; }
grep -qix 'kbdinteractiveauthentication no' <<<"$eff" || { echo "provision: sshd still allows keyboard-interactive authentication" >&2; exit 1; }
echo "provision: new host keys, sshd key-only (password and keyboard-interactive off)"
if command -v nx >/dev/null 2>&1 || [[ -e "$HOME/.config/nexus" ]] || [[ -e "$HOME/Library/LaunchAgents/com.nexus.service.plist" ]]; then
    echo "provision: nx or its state is present; the clean image must have none" >&2; exit 1
fi
echo "provision: FileVault off, automatic login $al, passwordless sudo, no nx"
