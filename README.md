## INFO

Low Orbit Ion Cannon (**LOIC**) - Python Edition is an open source network stress testing and chaos engineering tool, rewritten in Python from the original C# project by Praetox / NewEraCracker.

## DISCLAIMER

LOIC is for educational purposes only, intended to help server owners and SREs develop a "hacker defense" attitude and test infrastructure resilience. This tool comes without any warranty.

**You may not use this software for illegal or unethical purposes. This includes activities which give rise to criminal or civil liability.**

**Under no event shall the licensor be responsible for any activities, or misdeeds, by the licensee.**

## FEATURES

- **Asyncio core** — 10-100x more concurrent connections than the original C# version
- **6 attack methods** — TCP, UDP, HTTP, SlowLoris, ReCoil, ICMP
- **TLS/SSL support** — HTTPS flood targets with `--tls`
- **IPv6 support** — Test IPv6 targets with `--ipv6`
- **Response validation** — Tracks HTTP status codes (2xx, 3xx, 4xx, 5xx)
- **Rate limiting** — Control exact requests/sec with `--rate-limit`
- **Ramp-up** — Gradually increase load with `--ramp-up`
- **Jitter** — Add randomization to request timing with `--jitter`
- **Duration limit** — Auto-stop after N seconds with `--duration`
- **Custom headers** — Add auth tokens, cookies, etc. with `--header`
- **Metrics export** — JSON or CSV output with `--output`
- **Live dashboard** — Real-time ANSI terminal stats display
- **Graceful shutdown** — Final report with summary statistics
- **Circuit breaker** — Auto-backoff on consecutive failures, auto-recover
- **DNS resolution** — Automatic IP lookup from URLs
- **Bandwidth tracking** — Bytes sent/received, throughput in B/s
- **HiveMind** — IRC-based remote control for coordinated testing

## REQUIREMENTS

- Python 3.9+
- Core has **zero mandatory external dependencies** (pure stdlib asyncio)

Optional:
```bash
pip install "loic[irc]"      # IRC HiveMind support
pip install "loic[icmp]"     # ICMP flood (or use raw sockets as root)
pip install "loic[all]"      # Everything
```

## INSTALLATION

```bash
# Install from source
git clone https://github.com/NewEraCracker/LOIC.git
cd LOIC
pip install -e .

# Or with all optional dependencies
pip install -e ".[all]"
```

## USAGE

### Basic TCP flood
```bash
loic --target-ip 10.0.0.1 --port 80 --method tcp --threads 50
```

### HTTP flood with TLS, response validation, ramp-up
```bash
loic --target-url example.com --port 443 --method http --tls \
     --threads 100 --ramp-up 10 --rate-limit 50 --duration 60
```

### SlowLoris with 200 persistent connections
```bash
loic --target-url target.site --port 80 --method slowloris --socks-per-thread 200
```

### UDP flood with large payload
```bash
loic --target-ip 10.0.0.1 --port 53 --method udp --data "test" --payload-size 4096
```

### ICMP flood (requires root or scapy)
```bash
sudo loic --target-ip 10.0.0.1 --method icmp --socks-per-thread 10
```

### With custom headers (auth tokens, cookies)
```bash
loic --target-url api.example.com --method http --port 443 --tls \
     --header "Authorization: Bearer tok_abc123" \
     --header "X-Custom: value"
```

### Export metrics to JSON
```bash
loic --target-ip 10.0.0.1 --method http --duration 30 --output results.json
```

### HiveMind (IRC remote control)
```bash
loic --hivemind --irc-server irc.example.com --irc-port 6667 --irc-channel "#loic"
```

### All options
```
usage: loic [-h] [--version] [--target-ip TARGET_IP] [--target-url TARGET_URL]
            [--ipv6] [--method {tcp,udp,http,slowloris,slowloic,recoil,icmp}]
            [--port PORT] [--threads THREADS] [--delay DELAY]
            [--timeout TIMEOUT] [--subsite SUBSITE] [--data DATA] [--no-wait]
            [--random] [--use-get] [--gzip] [--tls] [--verify-response]
            [--no-verify-response] [--socks-per-thread SOCKS_PER_THREAD]
            [--payload-size PAYLOAD_SIZE] [--rate-limit RATE_LIMIT]
            [--ramp-up RAMP_UP] [--jitter JITTER] [--duration DURATION]
            [--header HEADER] [--dns-refresh DNS_REFRESH] [--output OUTPUT]
            [--quiet] [--no-color] [--hivemind] [--irc-server IRC_SERVER]
            [--irc-port IRC_PORT] [--irc-channel IRC_CHANNEL]
```

## ATTACK METHODS

| Method      | Description                                                        |
|-------------|--------------------------------------------------------------------|
| `tcp`       | Raw TCP socket flood with optional response reading                 |
| `udp`       | UDP datagram flood with configurable payload size                   |
| `http`      | HTTP GET/HEAD request flood with TLS support and status tracking    |
| `slowloris` | Slow HTTP attack — holds connections open with partial headers       |
| `recoil`    | Reverse DDOS — throttled download to exhaust server memory          |
| `icmp`      | ICMP Echo flood (requires root or `pip install scapy`)              |

## HIVEMIND / IRC MODE

HiveMind mode connects your client to an IRC server for coordinated testing.

```bash
loic --hivemind --irc-server irc.example.com --irc-port 6667 --irc-channel "#loic"
```

Hidden mode (no output):
```bash
loic --hivemind --irc-server irc.example.com --hidden --quiet
```

### Controlling from IRC

As an OP, set the topic or send a message:
```
!lazor targetip=10.0.0.1 port=80 method=http threads=50 start
```

Stop: `!lazor stop`  
Reset: `!lazor default`

Available IRC parameters: `targetip`, `targethost`, `port`, `method`, `threads`, `timeout`, `subsite`, `message`, `wait`, `random`, `speed`, `useget`, `gzip`, `sockspthread`

## METRICS OUTPUT

The `--output` flag exports per-second metrics in JSON or CSV:

```json
{
  "summary": {
    "total_requested": 15000,
    "total_downloaded": 14200,
    "total_failed": 800,
    "elapsed_seconds": 30.5,
    "avg_req_per_sec": 491.8,
    "failure_rate": 5.33,
    "status_codes": {"200": 12000, "503": 2200, "404": 800},
    "avg_latency_ms": 45.2
  },
  "history": [...]
}
```

## PROJECT STRUCTURE

```
loic/
  __init__.py          Package init
  __main__.py          Entry point (python -m loic)
  cli.py               Rich CLI with live dashboard
  config.py            AttackConfig dataclass
  attack.py            Async attack engine
  protocol.py          Protocol enum
  req_state.py         Request state enum
  functions.py         Utilities (random, headers, DNS)
  metrics.py           Metrics collector + JSON/CSV export
  irc_client.py        IRC HiveMind client (optional dep)
  flooders/
    __init__.py
    base.py            Abstract async flooder
    http_flooder.py    Async HTTP flood (TLS, IPv6, status codes)
    xxp_flooder.py     Async TCP/UDP flood
    slow_loic.py       Async SlowLoris
    recoil.py          Async ReCoil
    icmp_flooder.py    ICMP flood (raw socket + scapy fallback)
```

## LICENSE

Public Domain. See [LICENSE.md](LICENSE.md) for details.