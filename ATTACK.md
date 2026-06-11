## ATTACK METHODS & USECASES

LOIC v2.0 provides six distinct attack methods designed for chaos engineering, infrastructure resilience testing, and stress testing. Each method targets a different layer of the network stack.

---

## 1. TCP (`--method tcp`)

**Layer:** Transport (L4)  
**Protocol:** Raw TCP stream socket  
**Thread model:** One persistent connection per thread, reused until failure  

### How it works
Opens a TCP connection to the target, sends a configurable payload, optionally waits for a response, then repeats on the same socket. When the connection drops, a new one is established.

### Parameters
| Flag | Effect |
|------|--------|
| `--data` | Payload string sent on each iteration |
| `--random` | Appends random ASCII suffix to each payload |
| `--payload-size` | Pads payload to minimum byte size |
| `--no-wait` | Don't block on response (fire-and-forget) |
| `--delay` | Millisecond pause between sends |

### Use cases
- **Socket exhaustion testing** — Saturate a server's connection table by opening many TCP connections
- **Middleware throughput testing** — Measure how many raw TCP connections a proxy/load balancer can accept before rejecting
- **Protocol parser fuzzing** — Send malformed or unexpected data to services expecting a specific protocol
- **Stateful firewall testing** — Verify that firewalls correctly track and timeout idle TCP connections
- **Database connection pool saturation** — Target a database port to exhaust backend connection pools

### Example
```bash
# TCP flood a service with custom payload, 200 threads
loic --target-ip 10.0.0.50 --port 1433 --method tcp \
     --data "CONNECT" --threads 200 --delay 10
```

---

## 2. UDP (`--method udp`)

**Layer:** Transport (L4)  
**Protocol:** Raw UDP datagram socket  
**Thread model:** One socket per thread, single-packet send-receive loop  

### How it works
Sends individual UDP datagrams to the target with a configurable payload. Since UDP is connectionless, there is no handshake — packets are sprayed at the target. Optionally listens for ICMP responses (e.g., "port unreachable").

### Parameters
| Flag | Effect |
|------|--------|
| `--data` | Payload string |
| `--random` | Random suffix per packet |
| `--payload-size` | Large payloads for bandwidth testing |
| `--rate-limit` | Cap packets/sec per thread |
| `--jitter` | Randomize send timing |

### Use cases
- **DNS server stress testing** — Flood a DNS resolver with queries to measure capacity
- **Network saturation testing** — Generate high-bandwidth UDP traffic to test QoS/rate-limiting
- **NTP amplification simulation** — Replicate NTP-based traffic patterns for defense testing
- **VoIP infrastructure testing** — Simulate high-volume RTP streams to test SIP trunk capacity
- **DDoS mitigation validation** — Verify that upstream DDoS scrubbing correctly handles UDP floods

### Example
```bash
# UDP flood with 4KB payloads, rate-limited to 1000 pps per thread
loic --target-ip 10.0.0.53 --port 53 --method udp \
     --payload-size 4096 --rate-limit 1000 --threads 20 --duration 60
```

---

## 3. HTTP (`--method http`)

**Layer:** Application (L7)  
**Protocol:** HTTP/1.1 over TCP, optional TLS  
**Thread model:** New connection per request  

### How it works
Creates a fresh TCP (or TLS) connection for each HTTP request, sends a GET or HEAD request with randomized user-agents and headers, optionally reads the response, then closes the connection. Tracks HTTP status codes across all threads.

### Parameters
| Flag | Effect |
|------|--------|
| `--subsite` | URL path to request (default: `/`) |
| `--port` | Server port |
| `--tls` | Use HTTPS (TLS/SSL) |
| `--use-get` | Send GET instead of HEAD |
| `--gzip` | Advertise gzip/deflate support |
| `--random` | Append random suffix to the URL path |
| `--verify-response` | Parse and track HTTP status codes (2xx, 3xx, 4xx, 5xx) |
| `--header` | Add custom headers (auth tokens, cookies, etc.) — repeatable |
| `--rate-limit` | Cap requests/sec per thread |
| `--ramp-up` | Gradually start threads over N seconds |
| `--jitter` | Randomize request timing |
| `--duration` | Run for exactly N seconds, then stop |

### Use cases
- **Web server capacity planning** — Determine how many requests/sec a deployment can handle before latency spikes
- **Load balancer validation** — Verify round-robin distribution and session affinity under load
- **CDN edge cache testing** — Test cache hit/miss ratios under sustained load, measure origin shield fallback
- **API rate limit testing** — Flood an API endpoint to verify rate-limiting kicks in at the expected threshold (watch for 429s)
- **Web application firewall (WAF) testing** — Send randomized requests to test WAF rule performance under load
- **Autoscaling trigger validation** — Verify that CPU/memory-based autoscaling kicks in when traffic exceeds thresholds
- **TLS termination performance** — Test the max TLS handshakes/sec your load balancer or reverse proxy can handle
- **Zero-downtime deployment testing** — Run sustained HTTP load during a rolling deploy to verify no dropped requests
- **Graceful degradation testing** — Push beyond capacity and observe whether the service returns 503s cleanly or crashes

### Status code tracking
When `--verify-response` is enabled, LOIC parses the first line of each HTTP response and tracks status codes. This lets you see:
- `200` — healthy responses
- `301/302` — redirects (maybe CDN is kicking in)
- `401/403` — auth failures (target is blocking you)
- `404` — missing resource
- `429` — rate limiting is active
- `500` — application errors
- `502/503/504` — backend overload / gateway errors

### Example
```bash
# HTTPS load test with auth headers, ramp-up, status tracking, metrics export
loic --target-url api.example.com --port 443 --method http --tls \
     --threads 200 --ramp-up 30 --rate-limit 100 \
     --header "Authorization: Bearer ${API_TOKEN}" \
     --header "X-Test-Id: chaos-$(date +%s)" \
     --subsite /api/v1/health \
     --duration 300 \
     --output chaos_results.json
```

---

## 4. SlowLoris (`--method slowloris`)

**Layer:** Application (L7)  
**Protocol:** HTTP/1.1 over TCP  
**Thread model:** Multiple persistent, intentionally-stalled connections per thread  

### How it works
Opens TCP connections to the target and sends partial HTTP headers (e.g., declares `Content-Length: 42` but never sends the body). Periodically sends keep-alive headers (`X-a: b`) at configurable intervals to prevent the server from timing out the connection. Each connection holds a server worker/thread hostage.

### Parameters
| Flag | Effect |
|------|--------|
| `--socks-per-thread` | Number of concurrent stalled connections per thread |
| `--threads` | Number of threads |
| `--timeout` | Seconds between keep-alive pings on each connection |
| `--subsite` | URL path to target |
| `--random` | Randomize the URL path per connection |
| `--use-get` | Use GET instead of POST in the partial header |
| `--gzip` | Advertise gzip support |
| `--tls` | Use HTTPS |
| `--delay` | Milliseconds between opening each new connection |

### Use cases
- **Apache connection limit testing** — Apache's prefork MPM has a `MaxClients` limit; SlowLoris saturates it quickly
- **Thread-per-connection server testing** — Test servers that spawn a new thread per connection (Tomcat, older Node.js)
- **Reverse proxy timeout validation** — Verify that your Nginx/HAProxy has proper `client_header_timeout` and `client_body_timeout` configured
- **Keep-alive timeout configuration testing** — Determine if server keep-alive timeouts are set appropriately
- **Connection pool exhaustion** — Exhaust the connection pool of a backend database or service that uses persistent connections

### Example
```bash
# 500 stalled connections, 5 threads, 100 connections each, 30s keep-alive
loic --target-ip 10.0.0.10 --port 80 --method slowloris \
     --threads 5 --socks-per-thread 100 --timeout 30 \
     --subsite / --random
```

---

## 5. ReCoil (`--method recoil`)

**Layer:** Application (L7)  
**Protocol:** HTTP/1.1 over TCP  
**Thread model:** Multiple long-lived connections per thread, slow-draining responses  

### How it works
ReCoil performs a reverse-DDOS by sending legitimate HTTP GET requests for large resources, then throttling the download to a trickle (~16 bytes per read). The server is forced to allocate RAM for the response buffer, read the full file from disk or generate it from a database, but can never flush it to the client. This consumes server memory while using minimal attacker bandwidth.

Unlike SlowLoris, ReCoil sends a complete, well-formed HTTP request. The attack is harder to detect and cannot be mitigated by simple header-timeout configurations.

### Target selection rules (from the original research)
- **Always target dynamic content** — PHP, Python, Node.js pages that query a database and generate responses on the fly
- **The file must be larger than ~24KB** — Smaller files fit in the socket send buffer and flush immediately
- **Look for search endpoints, report generators, large API responses** — These typically build the full response in memory before sending
- **Avoid static files** — Servers can often `sendfile()` static files directly from disk with near-zero RAM overhead
- **Gzip with `--gzip`** — Can trigger CVE-2009-1891 (Apache mod_deflate DoS in older versions)

### Parameters
| Flag | Effect |
|------|--------|
| `--socks-per-thread` | Number of slow-draining connections per thread |
| `--subsite` | URL path to target (must be a large resource) |
| `--timeout` | Seconds between data reads per connection |
| `--tls` | Use HTTPS |
| `--delay` | Milliseconds between opening new connections |
| `--random` | Randomize URL path to bypass cache |
| `--gzip` | Request gzip-compressed response |

### Use cases
- **Apache memory exhaustion** — Apache buffers the full response in memory before sending; ReCoil fills server RAM
- **PHP-FPM pool exhaustion** — Each slow connection ties up a PHP worker until the response is fully flushed
- **Database connection exhaustion** — Dynamic pages that query a DB hold the DB connection open while the client throttles
- **Cloud instance memory overcommit testing** — Push a cloud VM into OOM territory to test OOM killer behavior
- **WAF evasion testing** — Since requests are legitimate, many WAFs don't flag ReCoil traffic

### Example
```bash
# Target a search endpoint with 250 slow connections
loic --target-url search.example.com --port 80 --method recoil \
     --subsite "/search?q=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" \
     --socks-per-thread 50 --threads 5 --timeout 20 --random --gzip
```

---

## 6. ICMP (`--method icmp`)

**Layer:** Network (L3)  
**Protocol:** ICMP Echo Request (ping)  
**Thread model:** Per-thread burst loop  

### How it works
Sends ICMP Echo Request packets (pings) to the target. Each thread sends a configurable number of pings per burst, with optional large random payloads. Uses raw sockets (requires root) or falls back to Scapy if installed.

### Parameters
| Flag | Effect |
|------|--------|
| `--socks-per-thread` | Pings per burst per thread |
| `--random` | Fill payload with random bytes (up to 65KB) |
| `--delay` | Milliseconds between bursts |
| `--ipv6` | Use IPv6 (ICMPv6) |

### Use cases
- **Network path MTU testing** — Send large ICMP packets with DF flag to find path MTU
- **Rate-limiting validation** — Verify that your ISP/cloud provider enforces ICMP rate limits
- **Infrastructure monitoring under load** — Test whether monitoring systems (Nagios, Prometheus blackbox) still function during ICMP storms
- **Kernel interrupt load testing** — High PPS ICMP floods can stress kernel interrupt handling
- **VRRP/CARP failover testing** — Flood a virtual IP to test failover behavior under load

### Prerequisites
```bash
# Method 1: Run as root (uses raw sockets)
sudo loic --target-ip 10.0.0.1 --method icmp

# Method 2: Install scapy (no root needed on some systems)
pip install scapy
loic --target-ip 10.0.0.1 --method icmp
```

---

## Chaos Engineering Patterns

### Pattern 1: Capacity Discovery
Find the breaking point of a service by gradually ramping load.
```bash
loic --target-url api.example.com --method http --tls \
     --threads 500 --ramp-up 120 --rate-limit 50 --verify-response \
     --output capacity_test.json
```
Watch the status codes as load increases. When 502/503s start appearing, you've found the ceiling.

### Pattern 2: Resilience Verification
Push beyond capacity, then stop abruptly, verify recovery.
```bash
# Phase 1: Overload for 60 seconds
loic --target-ip 10.0.0.1 --method tcp --threads 1000 --duration 60 --output phase1.json

# Phase 2: Verify service recovers (check externally)
curl -s -o /dev/null -w "%{http_code}" https://api.example.com/health
```

### Pattern 3: Slow Burn (Resource Exhaustion)
Gradually exhaust server resources over time to test monitoring/alerting thresholds.
```bash
loic --target-url app.example.com --method recoil \
     --socks-per-thread 10 --threads 5 --timeout 15 \
     --delay 1000 --duration 600
```
This opens 5 connections every second, totalling 3000 connections over 10 minutes.

### Pattern 4: Mixed-Method Stress
Run multiple LOIC instances simultaneously targeting different layers.
```bash
# Terminal 1: HTTP at L7
loic --target-ip 10.0.0.1 --method http --threads 100 --duration 120 &

# Terminal 2: TCP at L4
loic --target-ip 10.0.0.1 --method tcp --threads 200 --duration 120 &

# Terminal 3: ICMP at L3
sudo loic --target-ip 10.0.0.1 --method icmp --threads 5 --duration 120 &

wait
```

### Pattern 5: HiveMind Coordinated Test
Use IRC to coordinate multiple LOIC instances across different machines.
```bash
# On each test node:
loic --hivemind --irc-server irc.internal --irc-channel "#chaos"

# From IRC (as op):
!lazor targetip=10.0.0.1 method=http port=443 tls=true threads=100 rate-limit=200 start
```
All connected instances will begin the attack simultaneously, giving you distributed load testing.

---

## Metrics & Observability

### Real-time Dashboard
LOIC displays a live ANSI dashboard showing:
- Total requests, downloads, failures
- Requests/second and bandwidth
- Per-thread state distribution (How many idle vs connecting vs downloading)
- HTTP status code breakdown
- Average latency

### Post-Test Report
On Ctrl+C or `--duration` expiry, LOIC prints a final report:
- Total elapsed time
- Total requests sent, downloaded, failed
- Failure rate percentage
- Average and peak requests/second
- Total bytes sent/received
- Average latency
- HTTP status code distribution

### Metrics Export (`--output`)
Exports per-second snapshots in JSON or CSV:
```json
{
  "summary": {
    "total_requested": 150000,
    "total_failed": 3500,
    "elapsed_seconds": 60.0,
    "avg_req_per_sec": 2500.0,
    "peak_req_per_sec": 3200.0,
    "avg_latency_ms": 12.3,
    "status_codes": {"200": 142000, "503": 3500, "429": 4500},
    "failure_rate": 2.33
  },
  "history": [
    {"timestamp": 1718000000.0, "requested": 2500, "downloaded": 2400, "failed": 100, ...},
    {"timestamp": 1718000001.0, "requested": 5000, "downloaded": 4780, "failed": 220, ...},
    ...
  ]
}
```
Use this data to graph throughput over time, correlate failures with system metrics, and identify degradation points.

---

## Safety Checklist

Before running LOIC against any target, verify:
- [ ] **You own the target** or have explicit written permission to test it
- [ ] **Stakeholders are notified** — Dev, Ops, and SRE teams know a test is running
- [ ] **Monitoring is active** — You can observe the target's behavior during the test
- [ ] **Kill switch is ready** — You know how to stop LOIC immediately (Ctrl+C, or `!lazor stop` in IRC)
- [ ] **Rate limits are configured** — Start with conservative `--threads` and `--rate-limit`
- [ ] **Duration limits are set** — Always use `--duration` for automated tests to prevent runaway
- [ ] **You're not testing production during peak hours** — Unless the goal is specifically peak-load resilience
- [ ] **Metrics are being exported** — Use `--output` to capture results for post-test analysis