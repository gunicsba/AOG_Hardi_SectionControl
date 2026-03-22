# TUVR Serial Protocol (Trimble ↔ HC5500 reverse engineered)
## Overview
This document describes the reverse engineered serial protocol between Trimble and HC5500.
The communication is:
- ASCII based
- framed (control characters)
- XOR checksum protected
- request / response driven
---
## Physical Layer
- Interface: RS232
- Baudrate: 9600
- Parity: none
- Stop bits: 1
Important:
- Sniffing REQUIRES a 10kΩ resistor on the signal line
- Without it, Trimble may stop transmitting or behave incorrectly
- Likely due to line state / bus monitoring
---
## Frame Structure
[SOH] CMD [STX] DATA [ETX] CS [EOT]
Byte values:
- 0x01 → SOH
- 0x02 → STX
- 0x03 → ETX
- 0x04 → EOT
---
## Commands
### R0D → Request
R0D 6A → config
R0D 69 → target rate
R0D 6B → sections
R0D 6D → mode
---
### A0D → Response
A0D 6A → config
A0D 69 → rate
A0D 6B → sections
A0D 6D → mode
---
### S0C → Set
S0C 68,<value> → set rate
S0C 6C,<values> → set sections
---
## Checksum
XOR over:
CMD + DATA
Example:
"S0C68,0.0200"
Python:
cs = 0
for c in "S0C68,0.0200":
 cs ^= ord(c)
---
## Scaling
rate = l/ha / 10000
Examples:
100 → 0.0100
150 → 0.0150
200 → 0.0200
250 → 0.0250
---
## Sections
Always 13 values:
S0C 6C,1,1,1,1,1,1,1,1,1,1,1,1,1
---
## Boot
Trimble sends:
R0D 6A repeatedly
HC responds once with full block:
- A0D 6A
- V0C 68
- A0D 69
- A0D 6B
- A0D 6D
---
## Run Cycle (~5Hz)
S0C 68
R0D 69
S0C 6C
R0D 6B
R0D 6D
---
## State Machine
BOOT → wait for valid response
RUN → periodic communication
Fallback → return to BOOT if lost
---
## Key Learnings
- Strict order required
- Continuous communication required
- XOR checksum
- 10k resistor needed for sniffing