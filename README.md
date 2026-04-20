# ⚒️ AxeForge

**Auto-tune your Bitaxe miners for maximum performance.**

AxeForge monitors your Bitaxe miners in real time and intelligently tunes frequency and voltage to find the optimal stable overclock — factoring in hashrate, temperature, **and error rate** simultaneously.

---

## Features

- 📊 **Real-time dashboard** — live hashrate, temperature, error rate, voltage, frequency, fan speed per miner
- ⚙️ **Smart auto-tuner** — priority-weighted algorithm balancing hashrate, thermals, and error rate
- 🔒 **Safety first** — hard ceilings of 80°C and 1400 mV, auto back-off on threshold breach
- 📈 **Session history** — overlay and compare multiple tuning sessions on a single chart
- 🌐 **Web UI** — accessible from any browser on your network
- 🐳 **One-command deploy** — single Docker container, no setup beyond Docker itself

---

## Requirements

- Docker (and Docker Compose) installed — that's it.

---

## Quick Start

**1. Create a `docker-compose.yml` file anywhere on your machine:**

```yaml
services:
  axeforge:
    image: ghcr.io/YOUR_GITHUB_USERNAME/axeforge:latest
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

That's it. Docker pulls the image automatically. No cloning, no building.

Add your miners via the UI — no other config needed.

---

## Updating

```bash
docker compose pull
docker compose up -d
```

---

## Local Development

If you want to build and run from source:

```bash
git clone https://github.com/YOUR_GITHUB_USERNAME/axeforge.git
cd axeforge
docker compose -f docker-compose.dev.yml up -d --build
```

---

## How Tuning Works

1. **You set the priority** — choose the order: Hashrate › Temperature › Error Rate (or any order)
2. **AxeForge starts from a safe baseline** — stock settings or your custom starting point
3. **It steps frequency upward** — observing for a priority-determined window at each step:
   - Hashrate priority: 30 second windows
   - Temperature priority: 2 minute windows  
   - Error Rate priority: 15 second windows
4. **Safety auto-backoff** — if temp or error rate exceeds your ceiling, it backs off immediately
5. **Fast → Slow transition** — fast mode automatically switches to slower steps near limits
6. **Best settings applied** — when tuning ends (time limit, manual stop, or space exhausted) the best-scored settings are applied and the miner restarts

---

## Safety Limits

| Limit | Default | User Adjustable | Hard Ceiling |
|-------|---------|----------------|--------------|
| Max Temperature | 65°C | Yes | **80°C** |
| Max Voltage | 1250 mV | Yes | **1400 mV** |
| Error Rate Threshold | 1.0% | Yes | — |

Hard ceilings **cannot be exceeded** regardless of what is entered.

---

## Accessing the UI

By default AxeForge runs on port **8080**. If you want a different port, change the left side of the ports mapping in `docker-compose.yml`:

```yaml
ports:
  - "9000:8080"   # access on port 9000
```

---

## Data Persistence

Session history and settings are stored in `./data/axeforge.db` — a SQLite file in the same directory as your `docker-compose.yml`. This directory is mounted as a volume so data survives container restarts and upgrades.

---



## Contributing

PRs and issues welcome. This project was built for the Bitaxe community — if you find better tuning parameters, observe different behaviour on a specific model, or have ideas for improvement, please open an issue or PR.

---

## Disclaimer

AxeForge modifies your miner's hardware settings. While safety limits are enforced, overclocking always carries risk. The authors are not responsible for any hardware damage. Start conservatively and monitor your miners.

---

## License

MIT
