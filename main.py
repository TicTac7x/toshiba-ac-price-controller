
import asyncio
import logging
import statistics
import requests

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from toshiba_ac_community.device.properties import (
    ToshibaAcFanMode,
    ToshibaAcMode,
    ToshibaAcStatus,
)
from toshiba_ac_community.device_manager import ToshibaAcDeviceManager


# ==========================================
# CONFIGURATION
# ==========================================

TOSHIBA_USER = ""
TOSHIBA_PASS = ""

TIMEZONE = ZoneInfo("Europe/Tallinn")

# Price deviation boundaries from daily average.
INNER_PRICE_DEVIATION = 0.20
OUTER_PRICE_DEVIATION = 0.30

# Heat pump temperature for each price profile.
MODES = {
    ToshibaAcFanMode.QUIET: {
        "temperature": 21,
    },
    ToshibaAcFanMode.LOW: {
        "temperature": 22,
    },
    ToshibaAcFanMode.MEDIUM: {
        "temperature": 23,
    },
    ToshibaAcFanMode.MEDIUM_HIGH: {
        "temperature": 25,
    },
    ToshibaAcFanMode.HIGH: {
        "temperature": 26,
    },
}

AC_MODE = ToshibaAcMode.HEAT

ENSURE_POWER_ON = True

DRY_RUN = False


# ==========================================
# LOGGING
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger("toshiba-controller")


# ==========================================
# ELECTRICITY PRICES
# ==========================================

def get_prices():
    now = datetime.now(TIMEZONE)

    today = now.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    end = today.replace(hour=23, minute=45)

    params = {
        "start": today.astimezone(timezone.utc).isoformat(),
        "end": end.astimezone(timezone.utc).isoformat(),
    }

    response = requests.get(
        "https://dashboard.elering.ee/api/nps/price",
        params=params,
        timeout=15,
    )

    response.raise_for_status()

    logger.info("Price API: %s", response.url)

    raw_prices = response.json()["data"]["ee"]

    if not raw_prices:
        raise RuntimeError("No electricity prices returned by Elering")

    # Convert EUR/MWh to c/kWh, including 24% VAT.
    prices = [
        {
            "timestamp": int(item["timestamp"]),
            "price": (item["price"] / 10) * 1.24,
        }
        for item in raw_prices
    ]

    # Calculate average price for the entire day.
    average = statistics.mean(
        item["price"] for item in prices
    )

    inner_low = average * (1 - INNER_PRICE_DEVIATION)
    inner_high = average * (1 + INNER_PRICE_DEVIATION)

    outer_low = average * (1 - OUTER_PRICE_DEVIATION)
    outer_high = average * (1 + OUTER_PRICE_DEVIATION)

    # Find current 15-minute interval.
    interval = now.replace(
        minute=(now.minute // 15) * 15,
        second=0,
        microsecond=0,
    )

    interval_timestamp = int(interval.timestamp())

    current = next(
        (
            item for item in prices
            if item["timestamp"] == interval_timestamp
        ),
        None,
    )

    if current is None:
        raise RuntimeError(
            f"No price found for current interval: {interval}"
        )

    return {
        "interval": interval,
        "price": current["price"],
        "average": average,
        "inner_low": inner_low,
        "inner_high": inner_high,
        "outer_low": outer_low,
        "outer_high": outer_high,
        "count": len(prices),
    }


def determine_mode(price, price_data):
    if price <= price_data["outer_low"]:
        return ToshibaAcFanMode.HIGH

    if price <= price_data["inner_low"]:
        return ToshibaAcFanMode.MEDIUM_HIGH

    if price <= price_data["inner_high"]:
        return ToshibaAcFanMode.MEDIUM

    if price <= price_data["outer_high"]:
        return ToshibaAcFanMode.LOW

    return ToshibaAcFanMode.QUIET


# ==========================================
# TOSHIBA CONTROL
# ==========================================


async def control_heat_pump(price_data):
    manager = ToshibaAcDeviceManager(
        TOSHIBA_USER,
        TOSHIBA_PASS,
        "3e6e4eb5f0e5aa40",
    )

    try:
        await manager.connect()

        devices = await manager.get_devices()

        if not devices:
            raise RuntimeError("No Toshiba devices found")

        price = price_data["price"]
        average = price_data["average"]
        interval = price_data["interval"]

        target_fan = determine_mode(price, price_data)
        settings = MODES[target_fan]

        target_temperature = settings["temperature"]

        logger.info("========================================")
        logger.info("Interval: %s", interval.strftime("%H:%M"))
        logger.info("Current price: %.2f c/kWh", price)
        logger.info("Daily average: %.2f c/kWh", average)
        logger.info("----------------------------------------")
        logger.info(
            "HIGH profile (cheapest): <= %.2f c/kWh",
            price_data["outer_low"],
        )
        logger.info(
            "MEDIUM_HIGH profile: <= %.2f c/kWh",
            price_data["inner_low"],
        )
        logger.info(
            "MEDIUM profile: <= %.2f c/kWh",
            price_data["inner_high"],
        )
        logger.info(
            "LOW profile: <= %.2f c/kWh",
            price_data["outer_high"],
        )
        logger.info(
            "QUIET profile (most expensive): > %.2f c/kWh",
            price_data["outer_high"],
        )
        logger.info("Available intervals: %s", price_data["count"])
        logger.info("----------------------------------------")
        logger.info("Selected profile: %s", target_fan.name)
        logger.info("Target temperature: %s°C", target_temperature)
        logger.info("Target fan: %s", target_fan.name)
        logger.info("Devices found: %s", len(devices))
        logger.info("========================================")

        for device in devices:
            logger.info("Processing device: %s", device.name)

            changes = []

            if device.ac_mode != AC_MODE:
                changes.append(
                    ("AC mode", device.set_ac_mode, AC_MODE)
                )

            if device.ac_temperature != target_temperature:
                changes.append(
                    (
                        "Temperature",
                        device.set_ac_temperature,
                        target_temperature,
                    )
                )

            if device.ac_fan_mode != target_fan:
                changes.append(
                    ("Fan", device.set_ac_fan_mode, target_fan)
                )

            if ENSURE_POWER_ON and device.ac_status != ToshibaAcStatus.ON:
                changes.append(
                    (
                        "Power",
                        device.set_ac_status,
                        ToshibaAcStatus.ON,
                    )
                )

            if not changes:
                logger.info(
                    "%s: No changes needed.",
                    device.name,
                )
                continue

            for name, setter, value in changes:
                logger.info(
                    "Changing %s on %s to %s",
                    name,
                    device.name,
                    value,
                )
                await setter(value)

        logger.info("Controller execution completed.")

    finally:
        await manager.shutdown()



# ==========================================
# ENTRY POINT
# ==========================================

async def main():
    price_data = get_prices()
    await control_heat_pump(price_data)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        logger.exception("Controller execution failed")
        raise
