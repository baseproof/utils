#!/usr/bin/env python3
"""
playwright_egress.py — drive Playwright through a residential egress node.

    pip install playwright && playwright install chromium
    python3 playwright_egress.py

Picks a healthy egress node from the control plane at runtime rather than
hardcoding one, then aligns the browser's timezone, locale and geolocation
with wherever that node actually exits. A browser reporting UTC from a
Californian residential IP is a sharper signal than the datacenter IP it
replaced, so the alignment is the point, not a nicety.

Reads the peer token from WG_EGRESS_TOKEN, or from the enrollment file if the
process can read it.
"""

import json
import os
import subprocess
import sys
import urllib.request

IDENTITY = "/etc/wg-egress/identity.json"
CONTROL = "http://10.88.0.1:8080"
IPINFO = "https://ip.decodo.com/json?provider=IPinfo"


def auth_token() -> str:
    tok = os.environ.get("WG_EGRESS_TOKEN")
    if tok:
        return tok.strip()
    try:
        with open(IDENTITY) as fh:
            return json.load(fh)["auth_token"]
    except (OSError, KeyError) as exc:
        sys.exit(
            f"no token: set WG_EGRESS_TOKEN, or run as a user that can read {IDENTITY}\n({exc})"
        )


def pick_egress() -> str:
    """Ask the hub which egress nodes are alive. Health is measured from kernel
    handshake timestamps, so a node that lost power drops out within minutes."""
    req = urllib.request.Request(
        f"{CONTROL}/v1/peers?class=egress&healthy=true",
        headers={"authorization": f"Bearer {auth_token()}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            peers = json.load(resp).get("peers", [])
    except Exception as exc:
        sys.exit(f"control plane unreachable at {CONTROL}: {exc}")

    if not peers:
        sys.exit("no healthy egress nodes")
    for p in peers:
        print(f"  {p['client_id']:16} {p['proxy_url']:28} seen {p['last_handshake_secs_ago']}s ago")
    return peers[0]["proxy_url"]


def exit_identity(proxy: str) -> dict:
    """Find out where this proxy actually exits, so the browser can match it."""
    out = subprocess.run(
        ["curl", "-s", "-m", "30", "-x", proxy, IPINFO],
        capture_output=True, text=True, check=False,
    ).stdout
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        sys.exit(f"could not read exit identity through {proxy}")


def main() -> None:
    from playwright.sync_api import sync_playwright

    print("healthy egress nodes:")
    proxy = pick_egress()
    print(f"\nusing {proxy}")

    info = exit_identity(proxy)
    city, isp = info["city"], info["isp"]
    print(f"exits as {info['proxy']['ip']} — {isp['isp']} (AS{isp['asn']}), "
          f"{city['name']}, {city['state']}\n")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            proxy={"server": proxy, "bypass": "localhost,127.0.0.1,10.88.0.0/16"},
            args=[
                # Chromium can reveal the real address through WebRTC even when
                # every HTTP request goes via the proxy.
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--disable-features=WebRtcHideLocalIpsWithMdns",
            ],
        )
        context = browser.new_context(
            timezone_id=city["time_zone"],
            locale="en-US",
            geolocation={"latitude": city["latitude"], "longitude": city["longitude"]},
            permissions=["geolocation"],
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()

        page.goto(IPINFO, wait_until="domcontentloaded", timeout=60_000)
        seen = json.loads(page.locator("pre").inner_text())
        print("browser sees:")
        print(f"  ip       {seen['proxy']['ip']}")
        print(f"  isp      {seen['isp']['isp']} (AS{seen['isp']['asn']})")
        print(f"  city     {seen['city']['name']}, {seen['city']['state']}")

        tz = page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone")
        print(f"  browser tz {tz}  (node tz {city['time_zone']})")
        print("  aligned" if tz == city["time_zone"] else "  MISMATCH — fingerprintable")

        context.close()
        browser.close()


if __name__ == "__main__":
    main()