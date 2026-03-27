# Tutorial Video Recording Pipeline
#
# Usage:
#   make          # full pipeline: build → record → speed-map → splice → composite
#   make record   # just re-record (containers must exist)
#   make post     # just re-process (raw MP4s must exist)
#   make clean    # remove all generated files and containers
#
# Prerequisites:
#   brew install charmbracelet/tap/vhs ffmpeg
#   Docker running

SHELL := /bin/bash
.ONESHELL:

# Chrome path for VHS (macOS)
CHROMIUM ?= /Applications/Google Chrome.app/Contents/MacOS/Google Chrome
export CHROMIUM

# Speed-map runner
SPEEDMAP := uv run python poc-speedmap.py

# Container names
BARE_CONTAINER   := nexus-tutorial-bare
READY_CONTAINER  := nexus-tutorial-ready
BARE_IMAGE       := nexus-tutorial-bare
READY_IMAGE      := nexus-tutorial-ready

# Outputs
INSTALL_RAW      := poc-install.mp4
USAGE_RAW        := poc-usage.mp4
INSTALL_FAST     := poc-install-speedmapped.mp4
USAGE_FAST       := poc-usage-speedmapped.mp4
SPLICED          := poc-full-demo.mp4
FINAL            := poc-full-demo-composited.mp4

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
	@command -v vhs    >/dev/null || { echo "Missing: vhs (brew install charmbracelet/tap/vhs)"; exit 1; }
	@command -v ffmpeg >/dev/null || { echo "Missing: ffmpeg (brew install ffmpeg)"; exit 1; }
	@command -v docker >/dev/null || { echo "Missing: docker"; exit 1; }
	@docker info >/dev/null 2>&1  || { echo "Docker not running"; exit 1; }

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

# --- Record tapes ---

$(INSTALL_RAW): poc-install.tape .container-bare
	vhs poc-install.tape

$(USAGE_RAW): poc-usage.tape .container-ready
	vhs poc-usage.tape

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
	rm -f $(INSTALL_RAW) $(USAGE_RAW)
	rm -f $(INSTALL_FAST) $(USAGE_FAST)
	rm -f $(SPLICED) $(FINAL)

nuke: clean
	-docker rmi -f $(BARE_IMAGE) $(READY_IMAGE) 2>/dev/null
