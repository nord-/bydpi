import asyncio
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
import paho.mqtt.client as mqtt
from datetime import datetime
import json
import os
import signal
import traceback

# Configuration
DEVICE_MAC = "66:1E:11:EE:C5:E2"
WRITE_CHAR = "0000fff2-0000-1000-8000-00805f9b34fb"
READ_CHAR  = "0000fff1-0000-1000-8000-00805f9b34fb"

MQTT_HOST = "192.168.1.181"
MQTT_PORT = 1883
MQTT_TOPIC_SOC = "byd/soc"
MQTT_TOPIC_PI_STATUS = "byd/pi_status"
MQTT_TOPIC_CAR_STATUS = "byd/car_status"
# Credentials come from ~/.env (systemd EnvironmentFile, or activate_byd.sh for manual runs)
MQTT_USER = os.environ["MQTT_USER"]
MQTT_PASSWORD = os.environ["MQTT_PASSWORD"]

POLL_INTERVAL = 900  # 15 minutes
MQTT_KEEPALIVE = 1200  # 20 minutes
RECONNECT_DELAY = 30  # seconds
MAX_ECU_RETRIES = 3  # Max attempts per poll
SCAN_TIMEOUT = 5  # seconds
ELM_TIMEOUT = 3.0  # seconds to wait for the '>' prompt

# ELM327 init sequence: echo off, headers on, spaces on, timeout 0x96,
# CAN 11-bit/500k, request header 7E7, only accept replies from 7EF
ELM_INIT = ["ATE0", "ATH1", "ATS1", "ATST96", "ATSP6", "ATSH7E7", "ATCRA7EF"]

# Global MQTT client
mqtt_client = None

def ts():
    return datetime.now().strftime('%H:%M:%S')

def setup_mqtt():
    """Create the MQTT client with a Last Will"""
    global mqtt_client

    print(f"[{ts()}] Sätter upp MQTT...")

    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqtt_client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

    def on_connect(client, userdata, flags, reason_code, properties):
        # paho calls on_connect for a refused CONNACK too (e.g. bad credentials)
        if reason_code.is_failure:
            print(f"[{ts()}] ❌ MQTT nekade anslutning: rc={reason_code}")
            return
        print(f"[{ts()}] MQTT ansluten: rc={reason_code}")
        client.publish(MQTT_TOPIC_PI_STATUS, "online", qos=1, retain=True)
        print(f"[{ts()}] 📡 Pi status: online")

    def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
        print(f"[{ts()}] MQTT frånkopplad: rc={reason_code}")
        if reason_code.is_failure:
            print("  ⚠️ Oväntad MQTT-frånkoppling!")

    mqtt_client.on_connect = on_connect
    mqtt_client.on_disconnect = on_disconnect

    # Last Will - sent automatically if the Pi loses its connection
    mqtt_client.will_set(
        MQTT_TOPIC_PI_STATUS,
        payload="offline",
        qos=1,
        retain=True
    )

    # connect_async + loop_start makes paho's network thread retry the first
    # connection until it succeeds, so a broker that is down at boot is not fatal.
    # QoS 1 publishes made while offline are queued and sent after (re)connect.
    mqtt_client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
    mqtt_client.loop_start()
    print("✓ MQTT setup klar")

def publish_car_status(status: str):
    """Publish car status (connected/disconnected)"""
    if mqtt_client:
        mqtt_client.publish(MQTT_TOPIC_CAR_STATUS, status, qos=1, retain=True)
        print(f"📡 [{ts()}] Bilstatus: {status}")

def publish_soc(soc: int):
    """Publish SoC"""
    if mqtt_client:
        payload = json.dumps({"soc": soc})
        print(f"🔍 Publicerar SoC: '{payload}'")
        result = mqtt_client.publish(MQTT_TOPIC_SOC, payload=payload, qos=1, retain=True)
        print(f"🔍 MQTT publish result: {result.rc}")

def parse_soc(response: str) -> int:
    """Extract the SoC percentage from a UDS '62 00 05 XX' response"""
    if not response:
        raise RuntimeError("Inget svar från ECU")

    tokens = response.replace("\r", " ").replace(">", " ").split()

    if "62" not in tokens:
        raise RuntimeError("Ogiltigt svar")

    idx = tokens.index("62")
    if len(tokens) <= idx+3 or tokens[idx+1] != "00" or tokens[idx+2] != "05":
        raise RuntimeError("Fel DID")

    # ValueError from int() means the SoC token was not hex (e.g. '<DATA ERROR')
    return int(tokens[idx+3], 16)

async def find_device():
    """Scan for the OBD dongle; returns the BLEDevice or None when out of range"""
    print("  🔍 Skannar efter OBD BLE...")
    for d in await BleakScanner.discover(timeout=SCAN_TIMEOUT):
        if d.address == DEVICE_MAC:
            print(f"  ✓ Hittade {d.name}")
            return d
    return None

async def run_session(device):
    """Connect BLE, initialise the ELM and poll SoC until the connection drops"""
    ble_lost = asyncio.Event()

    # Passing the BLEDevice (not the MAC) lets bleak connect without a second scan
    async with BleakClient(device, timeout=30,
                           disconnected_callback=lambda _: ble_lost.set()) as client:
        print(f"✓ [{ts()}] BLE ansluten")
        publish_car_status("connected")

        rx_buffer = ""
        prompt_received = asyncio.Event()

        def on_notify(_, data: bytearray):
            nonlocal rx_buffer
            rx_buffer += data.decode(errors="ignore")
            # The ELM327 ends every response with a '>' prompt
            if ">" in rx_buffer:
                prompt_received.set()

        await client.start_notify(READ_CHAR, on_notify)

        async def send(cmd, timeout=ELM_TIMEOUT):
            """Write a command, wait for the ELM prompt (or the timeout) and return the response"""
            nonlocal rx_buffer
            rx_buffer = ""
            prompt_received.clear()
            await client.write_gatt_char(WRITE_CHAR, (cmd + "\r").encode())
            try:
                await asyncio.wait_for(prompt_received.wait(), timeout)
            except asyncio.TimeoutError:
                pass
            return rx_buffer

        # ELM init (once per connection)
        print(f"[{ts()}] Initierar ELM327...")
        await send("ATZ", 5)
        for cmd in ELM_INIT:
            await send(cmd)
        print(f"✓ [{ts()}] ELM327 initierad")

        poll_count = 0
        while True:
            poll_count += 1
            print(f"\n🔄 [{ts()}] Poll #{poll_count}")

            for retry in range(MAX_ECU_RETRIES):
                try:
                    if retry > 0:
                        print(f"  ↻ Retry {retry}/{MAX_ECU_RETRIES-1}")
                        await asyncio.sleep(2)

                    soc = parse_soc(await send("220005"))
                    print(f"  🔋 SoC: {soc}%")
                    publish_soc(soc)
                    break

                except (RuntimeError, ValueError) as e:
                    print(f"  ⚠️ ECU-fel: {e}")
            else:
                print(f"  ❌ Gav upp efter {MAX_ECU_RETRIES} försök")
                print("  💤 Bilen svarar inte, väntar till nästa poll")

            # Wait until the next poll, but wake up at once if BLE drops
            print(f"⏱️  Väntar {POLL_INTERVAL//60} minuter till nästa poll...")
            try:
                await asyncio.wait_for(ble_lost.wait(), POLL_INTERVAL)
            except asyncio.TimeoutError:
                continue
            raise BleakError("BLE-anslutningen tappades")

async def poll_loop():
    """Main loop: find the dongle, run a session, wait, repeat"""
    while True:
        print(f"\n🔁 [{ts()}] Försöker ansluta BLE...")

        try:
            device = await find_device()
            if device is None:
                # Silent - the car is just out of range
                print("  ⚙️ Enheten hittades inte (bilen utom räckvidd)")
            else:
                await run_session(device)

        except BleakError as e:
            print(f"⚠️ [{ts()}] BLE-fel: {e}")
            publish_car_status("disconnected")

        except Exception as e:
            # Unexpected error - log everything
            print(f"⚠️ [{ts()}] Oväntat fel: {e}")
            publish_car_status("disconnected")
            traceback.print_exc()

        # Wait before reconnecting
        print(f"⏳ [{ts()}] Väntar {RECONNECT_DELAY}s innan BLE-reconnect...")
        await asyncio.sleep(RECONNECT_DELAY)

async def main():
    print("🚗 BYD Dolphin SoC Monitor")
    print(f"📡 MQTT: {MQTT_HOST}:{MQTT_PORT}")
    print("   Topics:")
    print(f"     - {MQTT_TOPIC_SOC} (SoC)")
    print(f"     - {MQTT_TOPIC_PI_STATUS} (Pi status)")
    print(f"     - {MQTT_TOPIC_CAR_STATUS} (Car connection)")
    print(f"⏱️  Poll-intervall: {POLL_INTERVAL//60} minuter")
    print(f"🔄 MQTT keepalive: {MQTT_KEEPALIVE//60} minuter")
    print(f"🔄 Reconnect-delay: {RECONNECT_DELAY}s\n")

    # Set up MQTT with a Last Will
    setup_mqtt()

    # A fresh process never has a BLE connection, so clear any retained
    # 'connected' left behind by a previous process that was killed.
    publish_car_status("disconnected")

    # systemd stops us with SIGTERM; cancel the main task so the cleanup below runs
    main_task = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, main_task.cancel)

    try:
        await poll_loop()
    except asyncio.CancelledError:
        print(f"\n[{ts()}] Stoppsignal mottagen")
    except Exception as e:
        print(f"❌ KRITISKT FEL: {e}")
        traceback.print_exc()
    finally:
        # Clean up on exit
        print(f"[{ts()}] Avslutar...")
        if mqtt_client:
            publish_car_status("disconnected")
            info = mqtt_client.publish(MQTT_TOPIC_PI_STATUS, "offline", qos=1, retain=True)
            info.wait_for_publish(timeout=2)
            mqtt_client.disconnect()
            mqtt_client.loop_stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Avslutar...")
    except Exception as e:
        print(f"❌ Top-level exception: {e}")
        traceback.print_exc()
