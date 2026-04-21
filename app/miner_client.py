import httpx
import asyncio
import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)

TIMEOUT = 5.0
RESTART_WAIT_INITIAL = 12   # seconds to wait before polling after restart
RESTART_POLL_INTERVAL = 3   # seconds between online checks
RESTART_TIMEOUT = 90        # max seconds to wait for miner to return


class MinerClient:
    def __init__(self, ip: str):
        self.ip = ip
        self.base = f"http://{ip}"

    async def get_stats(self) -> Optional[Dict]:
        """Fetch /api/system/info. Returns None if unreachable."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                r = await c.get(f"{self.base}/api/system/info")
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        return None

    async def apply_settings(self, frequency: float, voltage: int) -> bool:
        """PATCH new freq/voltage to the miner."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                r = await c.patch(
                    f"{self.base}/api/system",
                    json={"frequency": frequency, "coreVoltage": voltage},
                )
                return r.status_code == 200
        except Exception as e:
            logger.warning(f"[{self.ip}] apply_settings failed: {e}")
            return False

    async def restart(self) -> bool:
        """POST /api/system/restart."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                r = await c.post(f"{self.base}/api/system/restart")
                return r.status_code == 200
        except Exception as e:
            logger.warning(f"[{self.ip}] restart failed: {e}")
            return False

    async def wait_for_online(self) -> bool:
        """
        Wait up to RESTART_TIMEOUT seconds for the miner to come back online
        with a non-zero hashrate after a restart.
        """
        await asyncio.sleep(RESTART_WAIT_INITIAL)
        deadline = asyncio.get_event_loop().time() + RESTART_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            stats = await self.get_stats()
            if stats is not None:
                # Miner is reachable; wait for it to actually start hashing
                if stats.get("hashRate", 0) > 0:
                    return True
            await asyncio.sleep(RESTART_POLL_INTERVAL)
        return False
