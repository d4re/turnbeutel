#!/bin/sh
# Turnbeutel sandbox toolchain, run as root by the kit's setup.install on
# sandbox creation. Brings the stock claude-code-docker template (Ubuntu,
# Python 3.14, uv, gh, Docker) up to what the project needs.
set -eu

PLAYWRIGHT_VERSION=1.64.0 # must match .claude/skills/verify
NODE_MAJOR=24             # matches CI (.github/workflows)

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
  zsh fzf less man-db unzip gnupg2 nano vim procps iproute2 dnsutils \
  git-delta xz-utils

# Node: the template ships Ubuntu's Node 22; install the latest $NODE_MAJOR
# into /usr/local (ahead of /usr/bin on PATH), checksum-verified.
case "$(dpkg --print-architecture)" in
  amd64) node_arch=x64 ;;
  arm64) node_arch=arm64 ;;
  *) echo "unsupported architecture" >&2; exit 1 ;;
esac
tmp=$(mktemp -d)
base="https://nodejs.org/dist/latest-v${NODE_MAJOR}.x"
curl -fsSL "$base/SHASUMS256.txt" -o "$tmp/SHASUMS256.txt"
tarball=$(awk -v a="linux-${node_arch}.tar.xz" '$2 ~ a"$" {print $2}' "$tmp/SHASUMS256.txt")
curl -fsSL "$base/$tarball" -o "$tmp/$tarball"
(cd "$tmp" && grep " $tarball\$" SHASUMS256.txt | sha256sum -c -)
tar -xJf "$tmp/$tarball" -C /usr/local --strip-components=1 \
  --exclude=CHANGELOG.md --exclude=LICENSE --exclude=README.md
rm -rf "$tmp"

# Headless Chromium for in-sandbox verification (see .claude/skills/verify),
# at a fixed system path so any playwright-core install finds it.
PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
  /usr/local/bin/npx --yes "playwright@${PLAYWRIGHT_VERSION}" install --with-deps chromium
chmod -R a+rX /opt/pw-browsers

# The Makefile expects uv at ~/.local/bin/uv (and would curl-install it there).
ln -sf /usr/local/bin/uv /home/agent/.local/bin/uv

# zsh + oh-my-zsh/powerlevel10k, as in the old devcontainer. Bash picks up the
# sandbox's shell init via BASH_ENV; zsh has to source it explicitly.
su agent -c 'sh -c "$(curl -fsSL https://github.com/deluan/zsh-in-docker/releases/download/v1.2.0/zsh-in-docker.sh)" -- \
  -p git -p fzf \
  -a "[ -f /etc/sandbox-persistent.sh ] && . /etc/sandbox-persistent.sh" \
  -a "source /usr/share/doc/fzf/examples/key-bindings.zsh" \
  -a "source /usr/share/doc/fzf/examples/completion.zsh" \
  -x'
chsh -s /usr/bin/zsh agent

apt-get clean
rm -rf /var/lib/apt/lists/*
