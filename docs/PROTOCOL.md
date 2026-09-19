# Corsair Galleon 100 SD: protocol notes

The keyboard shows up as two USB devices behind an internal hub. The hub reports
itself as "CORSAIR K100 MAX RGB MX KB" (`1b1c:2b19`); that isn't a separate keyboard.

| USB id | What | Interfaces |
|---|---|---|
| `1b1c:2b0c` | The keyboard | 0: boot keyboard · 1: vendor control, usage page `0xFF42`, 128-byte reports · 2: vendor events, `0xFF42`, 64-byte · 3: mouse-type HID |
| `1b1c:2b18` | The Stream Deck | 0: Stream Deck protocol, usage page `0x0C`, vendor usage `0xFF00` · 1: keyboard (NumLock sync) · 2: consumer control (standalone dials) · 3: no endpoint |

## Stream Deck (`2b18`, interface 0)

This is Elgato's "gen 2" Stream Deck protocol under Corsair's vendor ID. Credit:
[node-elgato-stream-deck](https://github.com/Julusian/node-elgato-stream-deck)
(`packages/core/src/models/galleon-k100.ts`), where the device is called "Galleon K100 SD".

**Standalone vs host mode.** With no host software running, the firmware drives
everything itself:
- the LCD keys act as a numpad (sending keys on interface 1, plus NumLock taps to sync
  the state);
- the dials send `KEY_VOLUMEUP`/`KEY_VOLUMEDOWN`/`KEY_MUTE` on interface 2;
- the screen shows Elgato's default image.

The host takes over by sending the keep-alive below. Once the pings stop, the firmware
returns to standalone mode by itself.

### Feature reports (32 bytes)

| Report | Meaning |
|---|---|
| set `03 27` | **Keep-alive.** Send every 500 ms. Until it's received, the device ignores every other command. |
| set `03 08 <0-100>` | Brightness |
| set `03 02` | Reset |
| get `04` / `05` / `07` | Firmware versions (ASCII, from byte 6), e.g. 2.00.002 / 3.06.006 / 3.05.003 |
| get `06` | Serial (all zeros on this unit) |
| get `08` | Layout: `04 03 a0 00 a0 00 d0 02 …`, a 4×3 grid of 160×160 keys on a 720-wide panel |
| get `14` | Module serial, e.g. `GK10044OAA08438` |

### Output reports (1024 bytes)

| Header | Payload |
|---|---|
| `02 07 <key> <last> <len:u16le> <part:u16le>` | Key image: JPEG, 160×160, up to 1016 bytes per packet |
| `02 0c <x:u16> <y:u16> <w:u16> <h:u16> <last> <part:u16> <len:u16> 00` | Top screen region: JPEG, the screen is 720×384, up to 1008 bytes per packet |

Keys are numbered 0-11, left to right, top to bottom, in a grid 3 keys wide and 4 high.
Images need no flipping or rotation. Measured throughput: about 60 full-screen frames per
second, or about 500 key images per second.

### Input reports

| Report | Meaning |
|---|---|
| `01 00 0c 00` + 12 bytes | Key states, 1 = pressed |
| `01 03 03 00 00 <d0> <d1>` | Dial press states |
| `01 03 03 00 01 <d0> <d1>` | Dial rotation, signed clicks per dial |

## Keyboard control (`2b0c`, interface 1): not safe to write

Corsair's "V2"/Bragi protocol. OpenRGB implements it for other keyboards in
`Controllers/CorsairPeripheralV2Controller`. Packets are 128 bytes after a zero report
number, and the write command is `0x08`.

**Safe (read-only):** `08 02 <prop>` gets a property. The reply is `00 02 <status> <value:u32le>`.
Tested properties include 0x11 VID `1b1c`, 0x12 PID `2b0c`, 0x13 firmware `3.70.1`,
0x41 layout `1`, and 0x02 `1000` (probably brightness, on a 0-1000 scale). 44
properties answer in all; none of them looks like an LED count.

**Unsafe:**
- `08 01 03 00 02` switches to software lighting, then colours are written with
  start-transaction (`0d 00 22`), block writes (`06`/`07`) and stop-transaction (`05 01 00`).
- White appeared with a 3×138-byte planar buffer, but other layouts froze the whole
  keyboard, typing included, until it was unplugged. A 193-LED buffer crashed it on its
  5th packet. A later 138-LED planar write froze it after returning to hardware mode.
- The LED count and data layout are unknown, and the top light bar may be on a separate
  zone.

A USB capture of iCUE setting a static colour is the way forward.
