# FLIR PT-Series AI SR — Python Test Suite Plan

## Camera Hardware Context (from Datasheet)

| Parameter | Value |
|-----------|-------|
| Pan range | Continuous 360° |
| Pan speed | 0.1° to 60°/sec |
| Tilt range | +90° to −90° |
| Tilt speed | 0.1° to 30°/sec |
| Pointing accuracy | 0.2° |
| Security | Digest Authentication, TLS/HTTPS, IEEE 802.1x |

---

## Overall Suite Flow

```mermaid
flowchart TD
    config["config.yaml"] --> CM

    subgraph task0 [Task 0 — Bootstrap]
        CM["CommunicationManager.__init__"]
        CM --> connect["_connect()\nONVIF GetDeviceInformation"]
        connect --> map["_map_services()\nRewrite NAT URLs\nCreate PTZ/Media/Imaging clients"]
        map --> ptz["_configure_ptz()\nVerify spaces\nSetConfiguration"]
        ptz --> home["_go_home()\nAbsoluteMove(0,0)\nwait_for_idle()"]
    end

    home --> T1["Task 1\ntask1_validation.py"]
    home --> T2["Task 2\ntask2_latency.py"]

    T1 --> csv1["results/results_*.csv"]
    T2 --> csv2["results/results_*.csv"]
```

---

## Task 0: Bootstrap & Configuration Layer (`comm_manager.py`)

```mermaid
sequenceDiagram
    participant Script
    participant CM as CommunicationManager
    participant Camera as FLIR Camera

    Script->>CM: CommunicationManager()
    CM->>Camera: TCP connect (port-forwarded WAN)
    Camera-->>CM: TCP established
    CM->>Camera: GetDeviceInformation (SOAP)
    Camera-->>CM: Manufacturer / Model / FW
    CM->>Camera: GetCapabilities (SOAP)
    Camera-->>CM: Internal service URLs
    Note over CM: Rewrite URLs from 192.168.x.x → external host:port
    CM->>Camera: GetNodes (PTZ SOAP)
    Camera-->>CM: PTZ node token + spaces
    CM->>Camera: SetConfiguration (PTZ SOAP)
    Camera-->>CM: OK
    CM->>Camera: AbsoluteMove(pan=0, tilt=0)
    CM->>Camera: GetStatus (poll until IDLE)
    Camera-->>CM: MoveStatus = IDLE
    CM-->>Script: Ready
```

Implement a `CommunicationManager` using `onvif-zeep` that:

1. **Discovery & Auth** — Performs WS-Discovery to find the camera (or direct IP for WAN access) and authenticates using Digest Authentication.
2. **Service Mapping** — Dynamically discovers the service URLs for DeviceManagement, PTZ, Imaging, and Analytics. Rewrites internal URLs to external host:port for NAT/port-forward traversal.
3. **PTZ Configuration**
   - Verifies the PTZ node is active.
   - Configures PTZ Spaces to use Absolute Pan/Tilt (Position) and Velocity (Speed).
   - Sets the Timeout parameter for continuous moves for safety during testing.
4. **Initial State Reset** — Moves the camera to Home position (Pan: 0, Tilt: 0) before any test begins.

---

## Task 1: Layer 1 — Command & Readback Validation (`task1_validation.py`)

```mermaid
flowchart TD
    start(["Bootstrap complete"]) --> T1a

    subgraph T1a [1a — Precision Move Test x50]
        rand["Random pan/tilt\n±180° / ±90°"] --> move1["AbsoluteMove"]
        move1 --> imm["Immediate GetStatus\n→ Comm_Latency_ms"]
        imm --> idle1["wait_for_idle"]
        idle1 --> settled["Settled GetStatus\n→ actual position"]
        settled --> delta["Δ = |target − actual|"]
        delta --> check{"> 0.2° ?"}
        check -->|yes| fail1["FAIL"]
        check -->|no| pass1["PASS"]
    end

    T1a --> T1b

    subgraph T1b [1b — Preset Cycle x10]
        create["AbsoluteMove → random pos\nSetPreset → token"] --> goback["AbsoluteMove → home"]
        goback --> goto["GotoPreset(token)"]
        goto --> verify["GetStatus → actual pos"]
        verify --> delta2["Δ = |preset − actual|"]
        delta2 --> check2{"> 0.2° ?"}
        check2 -->|yes| fail2["FAIL"]
        check2 -->|no| pass2["PASS"]
    end

    T1b --> T1c

    subgraph T1c [1c — Velocity Verification]
        startpos["Move to start pan −90°"] --> contmove["ContinuousMove at speed=1.0"]
        contmove --> poll["Background thread\nGetStatus @ 20 Hz\nrecord timestamps + positions"]
        poll --> target["Main thread detects\ntarget pan reached"]
        target --> calcv["V = ΔPosition / ΔTime\nlinear regression on samples"]
        calcv --> checkv{"≈ 60°/sec ?"}
        checkv -->|yes| passv["PASS"]
        checkv -->|no| failv["FAIL / warn"]
    end

    T1c --> csv["results/results_*.csv"]
```

### 1a. Precision Move Test
- Issue **50 `AbsoluteMove`** commands to random coordinates.
- Immediately follow each with a `GetStatus` call.
- Calculate the delta between Target and Reported position.
- Flag any result exceeding the **0.2° spec**.

### 1b. Preset Cycle
- Automate creation and deletion of **10 presets** to verify the 256-preset capacity.
- Move to each preset via `GotoPreset`, read back position, verify accuracy.

### 1c. Velocity Verification
- Execute a **180° pan at maximum 60°/sec** speed.
- Use a background thread to poll `GetStatus` coordinates.
- Calculate realized velocity: `V = ΔPosition / ΔTime`.

---

## Task 2: Layer 2 — Programmatic Control Latency (`task2_latency.py`)

```mermaid
sequenceDiagram
    participant Main as Main Thread
    participant Mech as MechLatencyMonitor\n(background thread)
    participant Scapy as scapy AsyncSniffer\n(background thread)
    participant Camera as FLIR Camera

    Main->>Scapy: start() — filter host:port on NIC
    loop 100 cycles
        Main->>Camera: GetStatus → baseline position
        Main->>Mech: start(baseline)
        Note over Main: t0 = perf_counter()
        Main->>Camera: AbsoluteMove (SOAP POST)
        Camera-->>Main: HTTP 200 OK
        Note over Main: Comm_Latency_ms = (now − t0) × 1000
        loop every 50ms
            Mech->>Camera: GetStatus
            Camera-->>Mech: position
            alt position changed > 0.05°
                Note over Mech: first_motion_ts = perf_counter()
            end
        end
        Main->>Camera: wait_for_idle
        Main->>Mech: stop()
        Note over Main: Mech_Latency_ms = (first_motion_ts − t0) × 1000
        Main->>Main: write CSV row
    end
    Main->>Scapy: stop() → analyse packets
    Note over Main: wire RTT stats + retransmission count
```

### 2a. Application Latency (`Comm_Latency_ms`)
Time from calling `AbsoluteMove` to receiving HTTP 200 OK, measured with `time.perf_counter`.

### 2b. Mechanical Start Latency (`Mech_Latency_ms`)
Time from the Move command to the **first coordinate change** detected by a background `GetStatus` poll at 20 Hz.

### 2c. Reporting Jitter
Std-dev of `Comm_Latency_ms` over all cycles — identifies network or firmware bottlenecks.

### 2d. Wire-Level Packet Analysis (scapy)
- `AsyncSniffer` on the outbound NIC, filtered to camera host and port.
- Cross-validates Python-layer timing against wire timestamps.
- Detects TCP retransmissions.
- Reports min/max/mean RTT from the packet trace.
- **Requires `sudo`** (raw socket privileges).

---

## NAT / Port-Forward Traversal

```mermaid
flowchart LR
    Script["Python Script\n(this machine)"] -->|"SOAP POST\n100.67.177.125:8085"| Router
    Router -->|"port-forward\n→ :80"| Camera["FLIR Camera\n192.168.1.152:80"]
    Camera -->|"response contains\ninternal URLs"| Router
    Router -->|response| Script
    Note1["GetCapabilities returns\nhttp://192.168.1.152:80/onvif/ptz\n↓\n_rewrite_url() replaces with\nhttp://100.67.177.125:8085/onvif/ptz"]
```

---

## Requirements

- Python 3.10+
- No GUI — `scapy` for packet analysis
- Output to CSV: `Command_Type`, `Target_Pos`, `Actual_Pos`, `Delta_Pos`, `Comm_Latency_ms`, `Mech_Latency_ms`, `Pass_Fail`, `Notes`
- Robust error handling for ONVIF Faults (e.g., moving while already in motion)

## Key Libraries

| Library | Purpose |
|---------|---------|
| `onvif-zeep` | ONVIF SOAP communication |
| `scapy` | Packet capture and analysis |
| `pyyaml` | Configuration file |
| `numpy` | Statistics (std-dev, percentiles) |

## Configuration (`config.yaml`)

All tunable parameters in one file:
- Camera host/port/credentials
- PTZ speed limits and accuracy threshold
- Test counts (precision moves, presets)
- Timeouts (connect, soap, move, home)
- scapy interface name
- Latency cycle count and poll rate
