#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# The POSIX entry claude_credentials.py run --remote execs on the Hyper-V host
# (Git for Windows' bash; WSL interop there is disabled by policy). The token is
# already in this process's environment; powershell.exe inherits it.
exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\build\guest\nxgate\token-send.ps1'
