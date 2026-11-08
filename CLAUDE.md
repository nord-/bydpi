# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A mirror of the files that run on the Raspberry Pi `bydpi` (Pi 3B+, Debian 13, Python 3.13 venv at `~/venvs/byd`), reachable as `rnord@bydpi.nord.cc` with key-only SSH (password login is off, see `etc/ssh/sshd_config.d/10-key-only.conf`). On Rickard's Windows machine `~/.ssh/config` has a `Host bydpi` block (user `rnord`, key `~/.ssh/bydpi`), so `ssh bydpi` and `scp ... bydpi:` work without a password; the commands below use that alias. The Pi reads the state of charge (SoC) of a BYD Dolphin through a BLE ELM327 OBD dongle and publishes it to an MQTT broker (`192.168.1.181:1883`, the Home Assistant side).

Paths in the repo mirror absolute paths on the Pi: `home/rnord/...` → `/home/rnord/...`, `etc/systemd/system/...` → `/etc/systemd/system/...`. Keep that mapping when adding files.

There is no build, lint or test setup. The code needs BlueZ and the car's dongle in range, so it only really runs on the Pi.

## Deploy, run and inspect (on the Pi)

`scp` uses SFTP mode, so give each remote file as its own `host:path` argument. `~/a ~/b` in one quoted argument fails.

```bash
# Push code + credentials (the .env is git-ignored and must be copied explicitly)
scp home/rnord/byd_soc.py home/rnord/activate_byd.sh home/rnord/.env bydpi:~/

# Unit changes need sudo, which prompts for a password, so the user has to run this step (`! ssh -t ...`)
scp etc/systemd/system/byd-soc.service bydpi:/tmp/
ssh -t bydpi "sudo mv /tmp/byd-soc.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart byd-soc"

# sshd drop-in (sudo, user runs it). sshd -t validates before the reload; on failure the file is removed again
scp etc/ssh/sshd_config.d/10-key-only.conf bydpi:/tmp/
ssh -t bydpi "sudo install -m 644 /tmp/10-key-only.conf /etc/ssh/sshd_config.d/ && { sudo sshd -t && sudo systemctl reload ssh || { sudo rm /etc/ssh/sshd_config.d/10-key-only.conf; echo 'sshd -t failed, rolled back'; }; }"

# Logs and drift check
ssh bydpi 'journalctl -u byd-soc -n 50 --no-pager'
ssh bydpi 'md5sum byd_soc.py' && md5sum home/rnord/byd_soc.py

# Manual run on the Pi (stop the service first, since only one process can hold the BLE connection)
source ~/activate_byd.sh && python -u ~/byd_soc.py
```

Dependencies: `home/rnord/requirements.txt` is a `pip freeze` of the Pi venv. Only `bleak` and `paho-mqtt` are imported. `obd`, `pygatt` and the rest are left over from earlier experiments.

## How `byd_soc.py` works

- **Config and secrets:** constants at the top of the file. `MQTT_USER` and `MQTT_PASSWORD` come from `~/.env`. systemd loads it via `EnvironmentFile=` in `byd-soc.service`, and `activate_byd.sh` exports it for manual runs. The script raises `KeyError` at import time if they are missing.
- **Three layers:** `poll_loop()` calls `find_device()` (5 s scan for the dongle MAC) and, when found, `run_session()`, which connects BLE, runs the ELM327 init (`ATSP6` CAN 11-bit/500k, header `7E7`, receive filter `7EF`) and polls UDS `22 00 05` every `POLL_INTERVAL` (15 min) *inside the same BLE connection*. A bleak `disconnected_callback` wakes the poll sleep at once when the car leaves; the session ends only by exception, and `poll_loop()` reconnects after `RECONNECT_DELAY`.
- **Response parsing:** notifications accumulate in `rx_buffer`. `send()` clears it, writes the command and waits for the ELM `>` prompt (or `ELM_TIMEOUT`), then `parse_soc()` reads the byte after `62 00 05` as a percentage. `parse_soc()` is pure and the only unit-testable piece. When the car is asleep, the dongle stays connected but the ECU gives no valid answer (`Ogiltigt svar`), so the retries fail every poll without a reconnect.
- **MQTT:** `paho-mqtt` 2.x with `CallbackAPIVersion.VERSION2`, `connect_async()` + `loop_start()` so the network thread retries a broker that is down at boot; QoS 1 publishes made while offline are queued. All messages QoS 1 and retained:
  - `byd/soc`: JSON `{"soc": <int>}`
  - `byd/car_status`: `connected` / `disconnected`, i.e. BLE state, not whether the car is awake. Published as `disconnected` at startup and on SIGTERM (handled via `add_signal_handler`) so a restart never leaves a stale retained `connected`. Home Assistant automations trigger on this value, so keep the topic and the two strings as they are.
  - `byd/pi_status`: `online` / `offline`; `offline` is also the Last Will
- Log output is in Swedish with emoji and goes to the journal through `print` (`python -u`). Code comments are in English.

## Other files

- `bt-unblock.service`: runs `rfkill unblock` + `hciconfig hci0 up` at boot so the adapter is usable.
- `etc/ssh/sshd_config.d/10-key-only.conf`: turns off password and keyboard-interactive login. sshd keeps the first value it reads and drop-ins come before the rest of `sshd_config`, so a file with a lower prefix that says `PasswordAuthentication yes` would override it.
- `home/rnord/EVNotifyBackend/`: only `createEnv.sh` from an unmodified clone of the upstream EVNotify repo. Nothing on the Pi runs it (no node process, no Docker containers).
- Line endings are LF (`.gitattributes`) because everything runs on Linux.
