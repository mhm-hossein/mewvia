#!/bin/sh
set -e

WG_DIR=/data
WG_CONF=/etc/wireguard/wgcf.conf

# Install procps if sysctl is missing (fallback if Dockerfile wasn't updated)
if ! command -v sysctl >/dev/null 2>&1; then
  echo "[+] Installing procps (sysctl missing)"
  apt-get update -qq && apt-get install -y -qq procps >/dev/null 2>&1 || true
fi

mkdir -p /etc/wireguard "$WG_DIR"

if [ ! -f "$WG_DIR/wgcf-account.toml" ]; then
  echo "[+] Registering wgcf account"
  cd "$WG_DIR"
  wgcf register --accept-tos
fi

if [ ! -f "$WG_CONF" ]; then
  echo "[+] Generating WireGuard profile"
  cd "$WG_DIR"
  wgcf generate
  cp wgcf-profile.conf "$WG_CONF"
  chmod 600 "$WG_CONF"
fi

echo "[+] Starting WireGuard"
# /proc/sys is read-only without --privileged, so wg-quick's internal
# `sysctl -q net.ipv4.conf.all.src_valid_mark=1` aborts the whole setup and the
# container exits. The compose `sysctls:` key already seeds that value, so make
# the in-script call non-fatal instead of requiring a privileged container.
if ! sysctl -q net.ipv4.conf.all.src_valid_mark=1 2>/dev/null; then
  echo "[!] sysctl is not writable here; making wg-quick's src_valid_mark call non-fatal"
  sed -i 's/cmd sysctl -q net.ipv4.conf.all.src_valid_mark=1/true/' /usr/bin/wg-quick
fi
wg-quick up wgcf
echo "[+] WireGuard is up"

# Wait for WireGuard to fully initialize
sleep 3

# Check DNS configuration 
echo "[+] Checking DNS configuration..."
if [ -f /etc/resolv.conf ]; then
  echo "[+] Current /etc/resolv.conf:"
  cat /etc/resolv.conf
fi

# Test DNS resolution using different methods
echo "[+] Testing DNS resolution..."
DNS_WORKING=false

# Try getent (most reliable in containers)
if command -v getent >/dev/null 2>&1; then
  if getent hosts google.com >/dev/null 2>&1; then
    echo "[+] DNS is working (getent test passed)"
    DNS_WORKING=true
  fi
fi

# If getent failed, try with explicit DNS servers
if [ "$DNS_WORKING" = "false" ]; then
  echo "[!] DNS resolution failed with default settings"
  echo "[!] This may cause issues - domain names may not resolve"
  echo "[!] IP addresses should still work"
  echo "[!] Note: In Docker with network_mode: service:warp, DNS is shared"
  echo "[!] The bot container should inherit DNS from this container"
fi

echo "[+] Starting microsocks on 0.0.0.0:1080"

# Ensure 'warp' resolves to localhost inside this network namespace
if ! grep -q "warp" /etc/hosts 2>/dev/null; then
  echo "127.0.0.1 warp" >> /etc/hosts 2>/dev/null || true
fi

# Enforce destination policy on the proxy's actual outbound sockets, after DNS
# resolution. Established client connections must still receive SOCKS replies.
iptables -A OUTPUT -m owner --uid-owner nobody -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
# Docker's embedded resolver is reached over UDP, TCP and raw sockets; a
# port-53-only rule leaves name resolution broken.
iptables -A OUTPUT -m owner --uid-owner nobody -d 127.0.0.11 -j ACCEPT
for net in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.0.2.0/24 192.88.99.0/24 192.168.0.0/16 198.18.0.0/15 198.51.100.0/24 203.0.113.0/24 224.0.0.0/4 240.0.0.0/4; do
  iptables -A OUTPUT -m owner --uid-owner nobody -d "$net" -j REJECT
done
# Do NOT blanket-REJECT IPv6 egress here: microsocks resolves destinations
# with AF_UNSPEC and aborts the whole SOCKS request when the IPv6 attempt is
# rejected, so every download fails with "Can't complete SOCKS5 connection".
# The WireGuard interface has no IPv6 route (the tunnel is IPv4-only here),
# so there is nothing to leak; block only the IPv6 metadata/link-local scopes.
for net in ::1/128 fe80::/10 fc00::/7 ff00::/8; do
  ip6tables -A OUTPUT -m owner --uid-owner nobody -d "$net" -j REJECT
done
exec setpriv --reuid=nobody --regid=nogroup --clear-groups --no-new-privs microsocks -i 0.0.0.0 -p 1080
