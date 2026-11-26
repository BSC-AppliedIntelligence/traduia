#!/usr/bin/env python3
"""Reads the JSON printed by test.js on stdin (log noise before it is ignored) and prints a short summary."""
import json, sys

raw = sys.stdin.read()
i = raw.find("{\n")
if i < 0:
    print(raw)
    sys.exit(1)
d, _ = json.JSONDecoder().raw_decode(raw[i:])
print("subscriber connect:", d.get("subConnect"))
print("publisher connect :", d.get("pubConnect"))
print("mic published     :", d.get("micPublished"))
print("publisher sender  :", d.get("pubSender"))
print("subscriber recv   :", d.get("subReceiver"))
for n, pc in enumerate(d.get("pcs", [])):
    if not pc.get("localCandidates"):
        continue
    print(f"pc{n}: {pc['connectionState']}/{pc['iceState']} local={pc['localCandidates']} remote={pc['remoteCandidates']}")
    for p in pc["pairs"]:
        if p.get("nominated"):
            print("   selected pair:", {k: v for k, v in p.items() if v is not None})
