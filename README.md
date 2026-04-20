# ⚒️ AxeForge

**Open-source auto-tuner for Bitaxe miners.**

AxeForge monitors your Bitaxe miners in real time and intelligently tunes frequency and voltage to find the optimal stable overclock — using a multi-factor stability scoring system that considers hashrate consistency, ASIC temperature, VRM temperature, error rate, and power stability simultaneously.

Built and maintained by [@Juiceman8686](https://github.com/Juiceman8686).

---

## Features

- 📊 **Real-time dashboard** — live hashrate, temperature, VRM temp, error rate, voltage, frequency, fan speed, and power per miner, updating every second
- ⚙️ **Multi-factor stability scoring** — evaluates hashrate coefficient of variation, thermal trends, VRM health, error rate spikes, and power stability
- 🎯 **Priority-weighted tuning** — choose whether to optimise for hashrate, temperature, or error rate, and set the order of importance
- 🔒 **Safety first** — hard ceilings enforced in code, instant backoff on any breach, immediate stop at any time
- 📈 **Session history** — overlay and compare multiple tuning sessions on a single chart with unique colours per session
- 🌐 **Web UI** — dark/light mode, accessible from any browser on your network
- 🐳 **One-command deploy** — single Docker container, no setup beyond Docker itself

---

## Requirements

- Docker (and Docker Compose) — that's it. Nothing else to install.

---

## Quick Start

**1. Create a `docker-compose.yml` file anywhere on your machine:**

```yaml
services:
  axeforge:
    image: ghcr.io/juiceman8686/axeforge:latest
    container_name: axeforge
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
    restart: unless-stopped
```

**2. Start it:**

```bash
docker compose up -d
```

**3. Open your browser:**

```
http://localhost:8080
```

That's it. Docker pulls the image automatically. No cloning, no building, no config files to edit.

Add your miners via the UI and you're ready to go.

---

## Updating

```bash
docker compose pull
docker compose up -d
```

---

## How Tuning Works

AxeForge never restarts your miner. Settings are pushed live via the AxeOS API and observed in real time.

1. **Set your priority** — choose the order: Hashrate › Temperature › Error Rate (or any combination)
2. **Choose a baseline** — start from stock settings or enter a custom frequency and voltage
3. **Set your safety limits** — max ASIC temp, VRM temp, voltage, frequency, and error rate threshold per session
4. **AxeForge observes and evaluates** — at each frequency step it collects data for a full observation window, then scores stability across five factors:
   - **Hashrate** — mean, coefficient of variation (CV), trend, and alignment with the 1-minute rolling average from AxeOS
   - **ASIC Temperature** — absolute level, rising trend, and variance (thermal saturation detection)
   - **VRM Temperature** — absolute level and rising trend, scored independently from ASIC temp
   - **Error Rate** — window average, peak spikes, trend direction, and statistical confidence based on share count
   - **Power** — variance and efficiency in GH/W

5. **Verdicts drive decisions:**

| Verdict | Stability Score | Action |
|---------|----------------|--------|
| Stable | ≥ 0.75 | Step up frequency |
| Marginal | 0.55 – 0.74 | Re-observe same settings |
| Unstable | 0.35 – 0.54 | Back off one frequency step |
| Critical | < 0.35 or hard breach | Back off two steps immediately |

6. **Best settings applied** — when tuning ends, the highest-scored settings are applied to the miner silently with no restart

**Observation windows by primary priority:**

| Primary Priority | Window Length |
|-----------------|---------------|
| Hashrate | 60 seconds |
| Temperature | 120 seconds |
| Error Rate | 45 seconds |

---

## Safety Limits

| Limit | Default | User Adjustable | Hard Ceiling |
|-------|---------|----------------|--------------|
| Max ASIC Temperature | 65°C | Yes | **80°C** |
| Max VRM Temperature | 80°C | Yes | **90°C** |
| Max Voltage | 1250 mV | Yes | **1400 mV** |
| Max Frequency | 1000 MHz | Yes | **1100 MHz** |
| Error Rate Threshold | 1.0% | Yes | — |

Hard ceilings are enforced in code and cannot be exceeded regardless of what is entered. If a ceiling is hit during tuning, AxeForge backs off immediately — it does not wait for the observation window to finish.

---

## Tuning Options

Each tuning session lets you configure:

- **Priority order** — drag to reorder Hashrate, Temperature, and Error Rate
- **Step mode** — Slow (recommended) or Fast. Fast mode automatically switches to Slow as it approaches limits
- **Time limit** — run indefinitely or set a limit in minutes
- **Baseline** — start from the miner's current settings or enter a custom starting frequency and voltage
- **Per-session safety limits** — override global defaults for ASIC temp, VRM temp, voltage, frequency, and error rate threshold

You can tune individual miners or start tuning on all miners simultaneously. A master **Stop All** button stops every active session instantly.

---

## Accessing the UI

By default AxeForge runs on port **8080**. To use a different port, change the left side of the ports mapping:

```yaml
ports:
  - "9000:8080"   # now accessible on port 9000
```

---

## Data Persistence

All settings and session history are stored in `./data/axeforge.db` — a SQLite file in the same directory as your `docker-compose.yml`. This is mounted as a Docker volume so data survives container restarts and upgrades.

Session history is kept indefinitely. Individual sessions can be deleted from the History view in the UI.

---

## Local Development

To build and run from source:

```bash
git clone https://github.com/Juiceman8686/axeforge.git
cd axeforge
docker compose -f docker-compose.dev.yml up -d --build
```

---

## Contributing

PRs and issues are welcome. This project was built for the Bitaxe community — if you find better tuning parameters, observe different behaviour on a specific model, or have ideas for improvement, please open an issue or PR.

---

## Disclaimer

AxeForge modifies your miner's hardware settings via the AxeOS API. While safety limits are enforced and AxeForge never restarts your miner, overclocking always carries risk. The authors are not responsible for any hardware damage. Start with conservative settings and monitor your miners.

---

## License

MIT
