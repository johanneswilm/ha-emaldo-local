#!/usr/bin/env python3
"""Transparent ARP forwarder for capturing one LAN host's traffic.

Poisons ARP between the module (TARGET) and the router (GATEWAY) so both
sides resolve each other to this machine, while this machine routes the
packets through unchanged. The captured host keeps full connectivity;
every frame also lands in the running tcpdump.

This is the CAPTURE-stage tool: it exists so you can observe your own
module's traffic (and DNAT its API/relay sessions to your takeover
server) while the vendor cloud still exists.

Only ever use on networks you own. Stop with Ctrl-C: it sends corrective
ARPs so both hosts relearn the truth immediately.

Configuration (all required):
  EMALDO_TARGET_IP / EMALDO_TARGET_MAC    the Emaldo data module
  EMALDO_GW_IP   / EMALDO_GW_MAC          your router/lan uplink
  EMALDO_IFACE   the local interface carrying that traffic
"""
import argparse
import os
import signal
import sys
import time

from scapy.all import ARP, Ether, sendp

BATTERY = os.environ.get("EMALDO_TARGET_IP", "")
BATTERY_MAC = os.environ.get("EMALDO_TARGET_MAC", "")
GATEWAY = os.environ.get("EMALDO_GW_IP", "")
GATEWAY_MAC = os.environ.get("EMALDO_GW_MAC", "")
IFACE = os.environ.get("EMALDO_IFACE", "")

if not all((BATTERY, BATTERY_MAC, GATEWAY, GATEWAY_MAC, IFACE)):
    sys.exit("set EMALDO_TARGET_IP/-MAC, EMALDO_GW_IP/-MAC, EMALDO_IFACE")


def arp_reply(dst_mac: str, src_ip: str, src_mac: str, dst_ip: str) -> Ether:
    return Ether(dst=dst_mac, src=src_mac) / ARP(
        op=2, psrc=src_ip, hwsrc=src_mac, pdst=dst_ip, hwdst=dst_mac
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=0.8)
    args = ap.parse_args()

    import subprocess

    my_mac = subprocess.check_output(
        ["cat", f"/sys/class/net/{IFACE}/address"]
    ).decode().strip()

    print(f"relaying {BATTERY} <-> {GATEWAY} on {IFACE} (me={my_mac}); Ctrl-C to restore")

    def restore(*_):
        print("\nrestoring true ARP entries...")
        for _ in range(6):
            sendp(
                [
                    arp_reply(BATTERY_MAC, GATEWAY, GATEWAY_MAC, BATTERY),
                    arp_reply(GATEWAY_MAC, BATTERY, BATTERY_MAC, GATEWAY),
                ],
                iface=IFACE,
                verbose=False,
            )
            time.sleep(0.2)
        sys.exit(0)

    signal.signal(signal.SIGTERM, restore)
    signal.signal(signal.SIGINT, restore)

    while True:
        # battery: "gateway lives at me"  |  router: "battery lives at me"
        sendp(
            [
                arp_reply(BATTERY_MAC, GATEWAY, my_mac, BATTERY),
                arp_reply(GATEWAY_MAC, BATTERY, my_mac, GATEWAY),
            ],
            iface=IFACE,
            verbose=False,
        )
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
