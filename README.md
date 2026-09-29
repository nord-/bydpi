# bydpi

Reads the battery state of charge (SoC) of a BYD Dolphin over a Bluetooth LE ELM327 OBD dongle and publishes it to MQTT, for use in Home Assistant. Runs as a systemd service on a Raspberry Pi 3B+ that sits within Bluetooth range of the parked car.

The repo mirrors the files on the Pi, with the same paths: `home/rnord/...` is `/home/rnord/...` and `etc/systemd/system/...` is `/etc/systemd/system/...`.

## How it works

`byd_soc.py` scans for the dongle every 30 s. When the car is in range it connects, initialises the ELM327 for CAN (11-bit, 500 kbit/s, request header `7E7`, replies from `7EF`) and then polls UDS `22 00 05` every 15 minutes over the same connection. The byte after `62 00 05` in the reply is the SoC in percent.

Everything goes to MQTT with QoS 1 and the retained flag:

| Topic | Payload | Meaning |
|---|---|---|
| `byd/soc` | `{"soc": 75}` | Last SoC read |
| `byd/car_status` | `connected` / `disconnected` | Whether the BLE link to the dongle is up. Not the same as the car being awake: a sleeping car keeps the link but answers nothing. |
| `byd/pi_status` | `online` / `offline` | Service liveness; `offline` is also the MQTT Last Will |

`car_status` is published as `disconnected` at startup and on SIGTERM, so a service restart never leaves a stale retained `connected` that would stop Home Assistant `to: connected` triggers from firing.

## Files

- `home/rnord/byd_soc.py`: the service. Depends on `bleak` and `paho-mqtt` only; `home/rnord/requirements.txt` is a `pip freeze` of the Pi venv and includes leftovers from earlier experiments.
- `home/rnord/activate_byd.sh`: activates the venv and exports `~/.env` for manual runs.
- `home/rnord/.env.example`: template for `~/.env`, which holds `MQTT_USER` and `MQTT_PASSWORD` and is not committed.
- `etc/systemd/system/byd-soc.service`: runs the script as user `rnord` with `EnvironmentFile=/home/rnord/.env` and `Restart=always`.
- `etc/systemd/system/bt-unblock.service`: unblocks the Bluetooth adapter at boot.

## Setting up a Pi

```bash
sudo apt install -y python3 python3-venv bluetooth bluez
python3 -m venv ~/venvs/byd
~/venvs/byd/bin/pip install bleak paho-mqtt
cp home/rnord/.env.example ~/.env && chmod 600 ~/.env   # then fill in the credentials
```

Adjust `DEVICE_MAC` and `MQTT_HOST` at the top of `byd_soc.py`, copy the files to their paths, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bt-unblock.service byd-soc.service
journalctl -u byd-soc -f
```

## Running by hand

Stop the service first; only one process can hold the BLE connection.

```bash
sudo systemctl stop byd-soc
source ~/activate_byd.sh && python -u ~/byd_soc.py
```

Log output is in Swedish. `parse_soc()` is the only pure function and can be exercised without hardware; the rest needs BlueZ and the dongle in range.
