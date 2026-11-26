#!/bin/sh
# LiveKit media ports (7881/tcp, 7882/udp) are published in host mode only so that the TURN relay
# inside the LiveKit container can reach LiveKit through the host's public IP (hairpin, arriving on
# docker_gwbridge). They must NOT be reachable from the Internet: drop everything arriving on the
# external interface. Idempotent. Applied at boot / Docker (re)start by livekit-media-fw.service.
set -eu
# External interface: set EXT to override; default = the interface of the default route.
EXT="${EXT:-$(ip -o route get 1.1.1.1 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p')}"
[ -n "$EXT" ] || { echo "cannot detect the external interface; set EXT=<interface>" >&2; exit 1; }
rule4() { chain=$1; shift; iptables -C "$chain" "$@" 2>/dev/null || iptables -I "$chain" "$@"; }
rule6() { chain=$1; shift; ip6tables -C "$chain" "$@" 2>/dev/null || ip6tables -I "$chain" "$@"; }
# forwarded (iptables DNAT) path
rule4 DOCKER-USER -i "$EXT" -p udp -m conntrack --ctstate DNAT --ctorigdstport 7882 -j DROP
rule4 DOCKER-USER -i "$EXT" -p tcp -m conntrack --ctstate DNAT --ctorigdstport 7881 -j DROP
# direct hits on the docker-proxy listeners (IPv4 and IPv6)
rule4 INPUT -i "$EXT" -p udp --dport 7882 -j DROP
rule4 INPUT -i "$EXT" -p tcp --dport 7881 -j DROP
rule6 INPUT -i "$EXT" -p udp --dport 7882 -j DROP
rule6 INPUT -i "$EXT" -p tcp --dport 7881 -j DROP
