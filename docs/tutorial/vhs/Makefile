# Tutorial Video Recording Pipeline
#
# Usage:
#   make          # full pipeline: build → record → speed-map → splice → composite
#   make record   # just re-record (containers must exist)
#   make post     # just re-process (cast/gif/mp4 must exist)
#   make clean    # remove all generated files and containers
#
# Prerequisites:
#   brew install asciinema agg ffmpeg
#   Docker running
#   expect (ships with macOS)

SHELL := /bin/bash
.ONESHELL:

# Speed-map runner
SPEEDMAP := uv run python poc-speedmap.py

# Container names
BARE_CONTAINER   := nexus-tutorial-bare
READY_CONTAINER  := nexus-tutorial-ready
BARE_IMAGE       := nexus-tutorial-bare
READY_IMAGE      := nexus-tutorial-ready

# Intermediate files
INSTALL_CAST     := poc-install.cast
USAGE_CAST       := poc-usage.cast
INSTALL_GIF      := poc-install.gif
USAGE_GIF        := poc-usage.gif
INSTALL_RAW      := poc-install.mp4
USAGE_RAW        := poc-usage.mp4
INSTALL_FAST     := poc-install-speedmapped.mp4
USAGE_FAST       := poc-usage-speedmapped.mp4
SPLICED          := poc-full-demo.mp4
FINAL            := poc-full-demo-composited.mp4

# agg settings (match VHS look)
AGG_THEME    := monokai
AGG_COLS     := 120
AGG_ROWS     := 35
AGG_FONT     := 16

.PHONY: all record post images containers clean nuke check-deps

# --- Top-level targets ---

all: check-deps $(FINAL)
	@echo ""
	@echo "Done: $(FINAL)"
	@ls -lh $(FINAL)

record: containers $(INSTALL_RAW) $(USAGE_RAW)

post: $(FINAL)

# --- Dependency check ---

check-deps:
	@command -v expect    >/dev/null || { echo "Missing: expect"; exit 1; }
	@command -v asciinema >/dev/null || { echo "Missing: asciinema (brew install asciinema)"; exit 1; }
	@command -v agg       >/dev/null || { echo "Missing: agg (brew install agg)"; exit 1; }
	@command -v ffmpeg    >/dev/null || { echo "Missing: ffmpeg (brew install ffmpeg)"; exit 1; }
	@command -v docker    >/dev/null || { echo "Missing: docker"; exit 1; }
	@docker info >/dev/null 2>&1    || { echo "Docker not running"; exit 1; }

# --- Docker images ---

images: .image-bare .image-ready

.image-bare: Dockerfile
	docker build -t $(BARE_IMAGE) -f Dockerfile .
	@touch $@

.image-ready: Dockerfile.installed
	docker build -t $(READY_IMAGE) -f Dockerfile.installed .
	@touch $@

# --- Docker containers ---

containers: .container-bare .container-ready

.container-bare: .image-bare
	-docker rm -f $(BARE_CONTAINER) 2>/dev/null
	docker run -d --name $(BARE_CONTAINER) $(BARE_IMAGE) sleep 3600
	@touch $@

.container-ready: .image-ready
	-docker rm -f $(READY_CONTAINER) 2>/dev/null
	docker run -d --name $(READY_CONTAINER) $(READY_IMAGE) sleep 3600
	@touch $@

# --- Record with expect + asciinema ---

$(INSTALL_CAST): install-demo.expect .container-bare
	asciinema rec --command="./install-demo.expect" --cols=$(AGG_COLS) --rows=$(AGG_ROWS) $@

$(USAGE_CAST): usage-demo.expect .container-ready
	asciinema rec --command="./usage-demo.expect" --cols=$(AGG_COLS) --rows=$(AGG_ROWS) $@

# --- Render cast → gif → mp4 ---

$(INSTALL_GIF): $(INSTALL_CAST)
	agg --theme $(AGG_THEME) --font-size $(AGG_FONT) $< $@

$(USAGE_GIF): $(USAGE_CAST)
	agg --theme $(AGG_THEME) --font-size $(AGG_FONT) $< $@

$(INSTALL_RAW): $(INSTALL_GIF)
	ffmpeg -y -i $< -movflags faststart -pix_fmt yuv420p \
	  -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" $@

$(USAGE_RAW): $(USAGE_GIF)
	ffmpeg -y -i $< -movflags faststart -pix_fmt yuv420p \
	  -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" $@

# --- Speed-map ---

$(INSTALL_FAST): $(INSTALL_RAW) poc-install-speedmap.toml poc-speedmap.py
	$(SPEEDMAP) $(INSTALL_RAW) poc-install-speedmap.toml

$(USAGE_FAST): $(USAGE_RAW) poc-usage-speedmap.toml poc-speedmap.py
	$(SPEEDMAP) $(USAGE_RAW) poc-usage-speedmap.toml

# --- Splice ---

$(SPLICED): $(INSTALL_FAST) $(USAGE_FAST)
	printf "file '%s'\nfile '%s'\n" \
	  "$(CURDIR)/$(INSTALL_FAST)" \
	  "$(CURDIR)/$(USAGE_FAST)" > /tmp/tutorial-concat.txt
	ffmpeg -y -f concat -safe 0 -i /tmp/tutorial-concat.txt \
	  -c:v libx264 -preset fast -crf 18 $@
	rm -f /tmp/tutorial-concat.txt

# --- Composite ---

$(FINAL): $(SPLICED) poc-composite.py
	uv run python poc-composite.py $(SPLICED)

# --- Clean ---

clean:
	-docker rm -f $(BARE_CONTAINER) $(READY_CONTAINER) 2>/dev/null
	rm -f .image-bare .image-ready .container-bare .container-ready
	rm -f $(INSTALL_CAST) $(USAGE_CAST)
	rm -f $(INSTALL_GIF) $(USAGE_GIF)
	rm -f $(INSTALL_RAW) $(USAGE_RAW)
	rm -f $(INSTALL_FAST) $(USAGE_FAST)
	rm -f $(SPLICED) $(FINAL)

nuke: clean
	-docker rmi -f $(BARE_IMAGE) $(READY_IMAGE) 2>/dev/null
