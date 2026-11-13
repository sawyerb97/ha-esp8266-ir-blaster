# ESP8266 IR Blaster (HTTP) for Home Assistant

Custom integration for IR blasters running the stock
[mdhiggins/ESP8266-HTTP-IR-Blaster](https://github.com/mdhiggins/ESP8266-HTTP-IR-Blaster)
firmware. No reflashing — it talks to the firmware's HTTP API (`/msg`, `/json`, `/received`).

## Features
- **Remote entities** `Output 1–4` (one per transmitter output; 2–4 disabled by default) supporting
  `remote.send_command`, `remote.learn_command`, `remote.delete_command`.
- **Online IR code library** — pick *Category → Brand → Model* from
  [Flipper-IRDB](https://github.com/Lucaslhm/Flipper-IRDB) (~1,950 curated files + ~6,900 converted from other public databases),
  **test a button** from the UI, then save it as a named device. No IR receiver needed.
- **Paste any Flipper `.ir` file** to import codes from elsewhere.
- Protocols: NEC / NECext / NEC42, Samsung32, Sony SIRC 12/15/20, RC5 / RC5X, RC6, Kaseikyo (Panasonic/Denon), RCA, Pioneer, and raw.
  Everything is converted to raw timings, so it works regardless of the firmware's named-protocol support.
- **Sensors**: last received code (with raw timings + GPIO pin map) and last sent code.
- **Event** `esp8266_ir_blaster_code_received` when the blaster's receiver sees a code.

## Install
**HACS:** HACS → ⋮ → *Custom repositories* → `https://github.com/sawyerb97/ha-esp8266-ir-blaster`, type *Integration* → install → restart HA.

**Manual:** copy `custom_components/esp8266_ir_blaster` into `/config/custom_components/` → restart HA.

Then *Settings → Devices & services → Add integration → ESP8266 IR Blaster* and enter the blaster's IP, port (default 80) and passcode (blank if none).
Requests from HA's own subnet bypass the firmware passcode check, but it's sent if configured.

## Adding codes
*Settings → Devices & services → ESP8266 IR Blaster → Configure → Add a device from the online IR code library*
1. Pick category, brand, model/remote.
2. Choose a test button (e.g. `Power`) and output → **Test**. If the device doesn't respond, choose **Back** and try another file.
3. Name the device (e.g. `samsung_tv`) → **Save**.

## Using codes
```yaml
action: remote.send_command
target:
  entity_id: remote.ir_blaster_output_1
data:
  device: samsung_tv
  command: Power          # or a list: [Vol_up, Vol_up]
  num_repeats: 1
  delay_secs: 0.4
```
Firmware short codes work directly (no `device`): `command: "E0E040BF:SAMSUNG:32"`.
Raw firmware JSON: `esp8266_ir_blaster.send_json` with `commands: '[{"type":"raw","data":[...],"khz":38}]'`.

Saved devices and buttons are listed in the remote entity's `learned_devices` attribute.

## Notes / limits
- Raw signals are capped at ~230 timings (device JSON buffer); longer ones (mostly A/C units) are trimmed to whole frames or skipped.
- Learning (`remote.learn_command`) requires an IR receiver wired to the blaster.

## Credits / license
Protocol encoders are ported from the [Flipper Zero firmware](https://github.com/flipperdevices/flipperzero-firmware) (GPL-3.0);
codes from [Flipper-IRDB](https://github.com/Lucaslhm/Flipper-IRDB). This project is licensed **GPL-3.0**.
