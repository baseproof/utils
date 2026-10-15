"""Browser helpers shared by the inventory (main.py) and detail (details.py) stages."""
import asyncio
from datetime import datetime

import nodriver as uc
import nodriver.cdp.page as cdp_page
from nodriver.core import util as uc_util

from tuning import TUNING, wait

BROWSER_ARGS = ["--password-store=basic", "--use-mock-keychain"]

def log(msg):
    """Helper to print messages with a timestamp."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")

async def start_browser():
    """Starts Chrome. nodriver gives a new Chrome under 3 seconds to answer, which a cold start
    sometimes misses, so that one failure is retried once. Start browsers one at a time."""
    before = set(uc_util.get_registered_instances())
    try:
        return await uc.start(headless=False, browser_args=BROWSER_ARGS)
    except Exception as e:
        if "Failed to connect to browser" not in str(e):
            raise
    # nodriver's stop() fails on a browser that never connected, so end the Chrome this call
    # started directly; it stays registered so nodriver still removes its temp profile at exit
    for stale in set(uc_util.get_registered_instances()) - before:
        proc = getattr(stale, "_process", None)
        if stale.info is None and proc is not None and proc.returncode is None:
            proc.terminate()
    log("Chrome did not answer in time; retrying once...")
    return await uc.start(headless=False, browser_args=BROWSER_ARGS)

async def smooth_scroll(page):
    """Simulates human scrolling to trigger lazy-loaded elements."""
    js_height_script = "document.body ? document.body.scrollHeight : 0"
    total_height = await page.evaluate(js_height_script)
    current_position: float = 0

    if not isinstance(total_height, (int, float)):
        return

    while current_position < total_height:
        scroll_step = round(wait(TUNING.scroll.step_px))
        current_position += scroll_step

        if current_position > total_height:
            current_position = total_height

        await page.evaluate(f"window.scrollTo(0, {current_position});")
        await asyncio.sleep(wait(TUNING.scroll.step_pause))

        new_height = await page.evaluate(js_height_script)
        if isinstance(new_height, (int, float)):
            total_height = new_height

async def capture_mhtml(page):
    """The page as currently rendered, as MHTML. Chrome leaves <script> contents out."""
    return await page.send(cdp_page.capture_snapshot(format_="mhtml"))
