# LiveKit media-path test (TURN forced)

Headless Chromium (Playwright image, fake microphone, `livekit-client`) with two participants and
`iceTransportPolicy: relay`, so all media goes through `turns:turn.<TRADUIA_DOMAIN>:443`.
It reports candidate types, the selected ICE pair and the bytes sent by the publisher / received by the subscriber.

```bash
TRADUIA_DOMAIN=example.org LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=... ./run.sh relay   # TURN forced (the case that was failing)
TRADUIA_DOMAIN=example.org LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=... ./run.sh all     # normal ICE
```

Healthy result: both connects `ok` within about a second, `selected pair ... remote host:udp@<public IP>:7882`,
and `subscriber recv.bytesReceived` close to `publisher sender.bytesSent`.
It covers the LiveKit leg only (not ASR) and runs from where the script runs: from the swarm node it exercises the
TURN/TLS endpoint via Traefik, but not a real external network path.

Gotchas learned while building it:
- a page needs a secure context (`https://...`, default `https://$TRADUIA_DOMAIN/login`) or `getUserMedia` is undefined;
- the stats are sampled during the attempt, because the peer connections are already closed when connect fails.
