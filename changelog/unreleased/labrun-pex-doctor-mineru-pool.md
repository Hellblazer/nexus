### Fixed

- `nx doctor --check-mineru` no longer leaves MinerU's render-executor worker processes running after it returns. Its one-page real-parse probe exited cleanly but, on Linux and macOS, the pool MinerU starts inside it outlived the probe (about 0.3 GB of memory each) until something else ended it. The probe's process group is now killed when the probe returns, as it already was on Windows (labrun-pex).
