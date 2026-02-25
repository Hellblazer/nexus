FROM node:20

ARG TZ
ENV TZ="$TZ"
ARG CLAUDE_CODE_VERSION=latest

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    less \
    git \
    procps \
    sudo \
    fzf \
    zsh \
    man-db \
    unzip \
    gnupg2 \
    gh \
    iptables \
    ipset \
    iproute2 \
    dnsutils \
    jq \
    nano \
    vim \
    curl \
    ripgrep \
    tmux \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Ensure node user has access to npm global prefix
RUN mkdir -p /usr/local/share/npm-global && chown -R node:node /usr/local/share

ARG USERNAME=node

# Persist shell history
RUN mkdir /commandhistory && touch /commandhistory/.bash_history \
    && chown -R $USERNAME /commandhistory

ENV DEVCONTAINER=true

# Create workspace and .claude dirs
RUN mkdir -p /workspace /home/node/.claude && chown -R node:node /workspace /home/node/.claude

WORKDIR /workspace

USER node

ENV NPM_CONFIG_PREFIX=/usr/local/share/npm-global
ENV PATH=$PATH:/usr/local/share/npm-global/bin:/home/node/.local/bin
ENV SHELL=/bin/zsh

# Install Claude Code
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

# Install uv (Python toolchain manager)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# Install Python 3.12 and conexus (provides the nx CLI)
RUN /home/node/.local/bin/uv python install 3.12 \
    && /home/node/.local/bin/uv tool install conexus --python 3.12

# Verify installs
RUN claude --version && nx --version
