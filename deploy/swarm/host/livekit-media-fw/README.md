# livekit-media-fw (host firewall for the LiveKit media ports)

LiveKit's media ports (`7881/tcp`, `7882/udp`) are published in host mode only so that the TURN relay inside the
LiveKit container can reach LiveKit through the host's public IP (hairpin from `docker_gwbridge`). They must not be
reachable from the Internet. `livekit-media-fw.sh` drops everything arriving on the external interface for those
ports, in `DOCKER-USER` (forwarded/DNAT path) and in `INPUT` (docker-proxy listeners), for IPv4 and IPv6. It is
idempotent. The external interface defaults to the one of the default route; set `EXT=<interface>` to override it. The
systemd unit reapplies the rules at boot and whenever Docker restarts.

Install on the swarm node **before** deploying the application stack:

```bash
sudo install -m 755 livekit-media-fw.sh /usr/local/sbin/livekit-media-fw.sh
sudo install -m 644 livekit-media-fw.service /etc/systemd/system/livekit-media-fw.service
sudo systemctl daemon-reload && sudo systemctl enable --now livekit-media-fw.service
```

Check from another network: `7881/tcp` must time out while `443` connects, and the DROP counters
(`sudo iptables -vnL DOCKER-USER`) increase. Rule persistence across a reboot relies on the unit; reboot-test it on your host.
