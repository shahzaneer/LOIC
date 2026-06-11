# ARCHITECTURE

Onboarding document for contributors. Covers design philosophy, component
topology, data flow, and extension points.

---

## 1. Project Overview

LOIC is an async-first, pure-stdlib-core network stress testing tool. It
generates configurable high-throughput traffic across six protocol stacks,
orchestrated by a central asyncio event loop. Optional features (IRC
HiveMind, ICMP via scapy) are installed separately and loaded lazily.

### Key numbers

| Metric | Value |
|---|---|
| Source files | 17 Python modules |
| Core dependencies | **zero** (stdlib only) |
| Optional deps | `irc`, `scapy` |
| Python target | 3.9+ |
| Tested throughput | 8k–50k HTTP req/sec per machine |
| License | Public Domain |

---

## 2. High-Level Architecture

```
                         ┌──────────────────────────────┐
                         │           cli.py             │
                         │  argparse → loop → dashboard │
                         │  handle_irc_params()         │
                         └─────────────┬────────────────┘
                                       │ creates & drives
                         ┌─────────────▼────────────────┐
                         │        attack.py             │
                         │     AttackEngine             │
                         │  ┌────────────────────────┐  │
                         │  │ _stats_loop (1 Hz)      │  │
                         │  │ _collect_and_maintain() │  │
                         │  └────────────────────────┘  │
                         │  flooder pool management     │
                         │  ramp-up / rate-limit / jit  │
                         └──┬──────────┬───────────┬───┘
                  ┌─────────▼┐  ┌──────▼──┐  ┌─────▼──────────┐
                  │ metrics  │  │  irc_   │  │   flooders/     │
                  │ .py      │  │  client │  │   (6 classes)   │
                  │ Metrics  │  │ .py     │  │                 │
                  │ Collector│  │ Hive    │  │  HTTPFlooder    │
                  │ JSON/CSV │  │ Mind    │  │  XXPFlooder     │
                  │ export   │  │ Client  │  │  SlowLoic       │
                  └──────────┘  └─────────┘  │  ReCoil         │
                                             │  ICMPFlooder    │
                                             │  (base: Async   │
                                             │   Flooder (ABC)│
                                             └─────────────────┘
```

### Separation of concerns

| Layer | Module | Responsibility |
|---|---|---|
| Presentation | `cli.py` | Argument parsing, TTY dashboard, signal handling, report printing |
| Orchestration | `attack.py` | Pool life cycle, stats collection, ramp-up, rate limiting, backpressure |
| Flooders | `flooders/*.py` | Protocol-specific I/O loops, per-connection state machines |
| Metrics | `metrics.py` | Time-series aggregation, JSON/CSV serialisation |
| Configuration | `config.py` | Immutable parameter object (frozen dataclass) |
| Utilities | `functions.py` | Random generators, HTTP header builder, DNS resolver |
| IRC | `irc_client.py` | Optional HiveMind client with reconnect and operator auth |
| Types | `protocol.py`, `req_state.py` | Shared enums |

---

## 3. Design Decisions

### 3.1 Why asyncio (not threads, not multiprocessing)

Threads are serialised by the GIL for CPU work. Multiprocessing has
expensive IPC and per-process memory. asyncio gives us:

- **10–100x more concurrent connections** than the original C# thread model
- **Cooperative scheduling** — one thread, thousands of sockets, zero context switches
- **Graceful cancellation** — `asyncio.CancelledError` propagates cleanly through `await` stacks
- **Single event loop** — stats collection, ramp-up timers, and flooder I/O all share one loop. No locks needed (except `attack.py`'s `asyncio.Lock` for config mutation)

The one exception is ICMP, which uses `loop.run_in_executor()` for raw socket
writes (raw sockets don't support asyncio directly) and IRC, which runs a
dedicated daemon `threading.Thread` because the `irc` library is synchronous.

### 3.2 Frozen config (immutable dataclass)

`AttackConfig` is `frozen=True`. This means:

- No data races from concurrent mutations (IRC callback vs stats loop)
- Configuration changes produce a **new copy** via `.copy(port=443)`
- The engine's `self.config` is atomically replaced, not mutated in place
- Easy diffing: `old_config != new_config` tells you if anything changed

### 3.3 Base class carries stats

Every `AsyncFlooder` subclass inherits `requested`, `downloaded`, `failed`,
`bytes_sent`, `bytes_received`, `status_codes`, and `avg_latency` from the
base class. The engine never checks `hasattr()` — it's always safe to access.
Subclasses only add protocol-specific fields (socket lists, backoff timers).

### 3.4 Circuit breaker pattern

All flooders track `_consecutive_failures`. After 50 consecutive failures,
the flooder enters a 5-second backoff before retrying. This prevents a
brick-wall target from wasting CPU in a connect-fail-spin loop.

### 3.5 Optional dependencies are lazy

```
loic.py          → zero deps, works immediately
pip install irc  → HiveMindClient.start() actually connects
pip install scapy → ICMPFlooder._run() doesn't fall to raw sockets
```

Nothing crashes at import time. Missing deps produce a log error and
graceful degradation.

---

## 4. Component Walkthrough

### 4.1 `cli.py` — Entry Point and TTY Dashboard

`main()` is the sole entry point. It:

1. Parses args via `parse_args()`
2. Resolves the target (IP or URL) via `resolve_target()`
3. Builds an `AttackConfig` via `build_config()`
4. Creates an `AttackEngine` wrapped in a `MetricsCollector`
5. Optionally starts a `HiveMindClient`
6. Creates a fresh `asyncio` event loop
7. Runs `run_attack()` which drives the main loop

**Signal handling:** SIGINT / SIGTERM call `engine.stop()` via
`loop.call_soon_threadsafe()`. This cancels all flooder tasks and the
stats loop, then prints the final summary.

**Dashboard rendering:** `draw_dashboard()` uses ANSI escape codes
(`\033[H` reposition, `\033[2J` clear) to paint a full-screen status
panel every second. If stdout is not a TTY (e.g. piped to a file), it
falls back to plain text output.

**IRC parameter dispatch:** `handle_irc_params()` is the callback
passed to `HiveMindClient`. It parses `!lazor` commands and calls
`asyncio.ensure_future(engine.start(...))` or
`asyncio.ensure_future(engine.stop())` to schedule state changes on
the main event loop.

### 4.2 `attack.py` — AttackEngine

The engine owns the flooder pool and coordinates their lifecycle.

```
      start(config)
           │
     ┌─────▼──────┐
     │ ramp_up>0? │
     │ yes: spawn  │   _start_ramp()
     │  one at a   │   asyncio.sleep(interval)
     │  time       │
     │ no:  spawn  │   for _ in range(threads):
     │  all at     │     f.start_async()
     │  once       │
     └────────────┘
           │
     ┌─────▼──────┐
     │ _stats_loop │   every 1 second:
     │             │   _collect_and_maintain()
     │             │   → aggregate counters
     │             │   → replace dead flooders
     │             │   → scale pool to config.threads
     └────────────┘
           │
     ┌─────▼──────┐
     │ stop()      │   cancel all tasks
     │             │   await cleanup()
     │             │   print / export report
     └────────────┘
```

**Factory method:** `_create_flooder(config)` maps `Protocol` enum to a
flooder class. To add a new attack method, add one `elif` branch here
and register the class in `flooders/__init__.py`.

**Thread maintenance:** `_collect_and_maintain()` does three things in a
single pass (under `asyncio.Lock`):

1. Aggregates all `f.requested`, `f.failed`, etc. into a `MetricsSnapshot`
2. Replaces any flooder where `f.is_flooding == False` (crashed/stopped)
3. Grows or shrinks the pool to match `config.threads`

### 4.3 `flooders/base.py` — AsyncFlooder ABC

Every flooder inherits this. Key contract:

| Method | Caller | What it does |
|---|---|---|
| `start_async()` | Engine | Creates `asyncio.Task` wrapping `_run()` |
| `stop()` / `stop_async()` | Engine / IRC | Sets `_is_flooding=False`, cancels task |
| `_run()` | **(abstract)** | The flooding loop; must check `_is_flooding` |
| `cleanup()` | Engine on stop | Close sockets, reset state |
| `record_latency(sec)` | Flooder self | Tracks response time (capped at 1000 samples) |
| `avg_latency` (property) | Metrics | Mean of stored latencies |

**Stats fields on the base class** (inherited by all subclasses):

```python
self.requested: int = 0       # total sends / connection attempts
self.downloaded: int = 0      # total successful reads
self.failed: int = 0          # total errors
self.bytes_sent: int = 0      # total payload bytes out
self.bytes_received: int = 0  # total payload bytes in
self.status_codes: dict[int, int] = {}  # {200: 1500, 503: 42, ...}
```

Each subclass increments these in its `_run()` loop.

### 4.4 Flooder Implementations

#### HTTPFlooder (`flooders/http_flooder.py`)

```
while flooding:
    connect (TCP or TLS via asyncio.open_connection)
    send GET/HEAD with random headers
    if resp:
        read 4096 bytes (may contain status line)
        parse status code → status_codes[200] += 1
    close connection
    sleep(delay + jitter) or obey rate_limit
```

- Circuit breaker: backoff 5s after 50 consecutive failures
- TLS: `ssl.create_default_context()` with `check_hostname=False`
- IPv6: `family = socket.AF_INET6` when `config.ipv6`
- Status parsing: reads first line of response, extracts `int` status code

#### XXPFlooder (`flooders/xxp_flooder.py`) — TCP + UDP

```
while flooding:
    TCP:
        connect → enter inner loop:
            send payload (+ random bytes if config.random_msg)
            if resp: read 4096 bytes
            sleep
        on disconnect: reconnect
    UDP:
        create SOCK_DGRAM
        loop:
            sendto(payload, addr)
            if resp: recvfrom
            sleep
```

- UDP uses raw `socket.socket` + `loop.sock_sendto()` (asyncio has no native UDP stream)
- Payload comes from `build_tcp_payload()` which pads to `config.payload_size`
- Circuit breaker present

#### SlowLoic (`flooders/slow_loic.py`) — SlowLoris

```
Phase 1 — Connect:
    while sockets_needed:
        open TCP connection
        send partial HTTP header (Content-Length: 42 but never send body)
        append to self._sockets list

Phase 2 — Keep-alive:
    for each socket in self._sockets:
        send "X-a: b\r\n" keep-alive padding
        if send fails: remove socket, increment failed

Phase 3 — Wait:
    sleep(timeout) then go to Phase 1 if pool not full
```

- `self.is_delayed` flag gates whether to open new connections
- `self.n_sockets` = `config.socks_per_thread` = max concurrent connections per thread
- Keep-alive interval = `config.timeout` (seconds)
- Randomised commands: `_rand_cmds=True` appends random ASCII to `X-a: b` padding

#### ReCoil (`flooders/recoil.py`) — Reverse HTTP Drain

Similar two-phase structure to SlowLoic, but:

1. Sends **complete** GET requests (not partial headers)
2. Examines response headers to find large resources:
   - `Content-Length >= 16384` → keep this connection
   - `Transfer-Encoding: chunked` → keep this connection
   - `Location: ...` → follow the redirect
3. In Download phase, reads data at `RECV_BUF_SIZE` (4096 bytes) chunks,
   counting each successful read as `downloaded += 1`
4. The server buffers the response in RAM but the client throttles the download —
   this exhausts server memory

#### ICMPFlooder (`flooders/icmp_flooder.py`) — Ping Flood

```
try raw socket (SOCK_RAW, IPPROTO_ICMP)  ← needs root
    if success:
        build ICMP echo request packets manually
        send via loop.run_in_executor(sock.sendto)
    if PermissionError:
        try scapy
            send via IP(dst=ip)/ICMP()/payload
        if ImportError:
            log error, set state=FAILED, stop
```

- `pings_per_thread` pings per burst, then `sleep(delay)`
- Random payload: up to 65500 bytes of `os.urandom()` if `config.random_msg`
- `loop.run_in_executor()` because raw socket I/O is blocking by nature

### 4.5 `metrics.py` — Time-Series Aggregation

`MetricsCollector` stores up to 3600 snapshots (1 hour at 1 Hz). Each
snapshot is a `MetricsSnapshot` dataclass with 16 fields.

**Export flow:**
```
MetricsCollector.record(snapshot)
    → computes req_per_sec, bandwidth_out, bandwidth_in
    → appends to self._history

After attack:
    MetricsCollector.summary()      → dict with aggregate stats
    MetricsCollector.export_json()  → writes {summary, history} to file
    MetricsCollector.export_csv()   → writes CSV rows to file
```

### 4.6 `irc_client.py` — HiveMind

```
HiveMindClient.start()
    └→ threading.Thread(target=self._run)
       └→ irc.bot.SingleServerIRCBot(server, nick, realname)
          ├─ on_welcome    → join channel
          ├─ on_names      → populate op_list
          ├─ on_topic      → if "!lazor ..." → callback(pars)
          ├─ on_pubmsg     → if from op and "!lazor ..." → callback(pars)
          ├─ on_op/deop    → maintain op_list
          ├─ on_part/quit  → remove from op_list
          └─ on_disconnect → exponential backoff reconnect
```

Op authentication: only nicks prefixed with `@`, `&`, or `~` in `NAMES`
reply are added to `self._op_list`. Only messages from these nicks trigger
attack commands. Topic commands trigger for anyone (topic can only be set
by ops anyway).

The callback (`handle_irc_params` in `cli.py`) runs on the IRC thread
but schedules work onto the main asyncio loop via
`asyncio.ensure_future(engine.start(...))`.

### 4.7 `config.py` — Frozen Dataclass

```python
@dataclass(frozen=True)
class AttackConfig:
    target_ip: str = ""
    target_host: str = ""
    port: int = 80
    method: Protocol = Protocol.TCP
    threads: int = 10
    delay: int = 0              # ms between requests
    timeout: int = 30           # seconds
    subsite: str = "/"          # HTTP path
    data: str = "U dun goofed"  # TCP/UDP payload
    wait_reply: bool = True
    random_sub: bool = False
    random_msg: bool = False
    use_get: bool = False       # GET vs HEAD for HTTP
    allow_gzip: bool = False
    socks_per_thread: int = 25  # for SlowLoris/ReCoil
    ipv6: bool = False
    use_tls: bool = False
    verify_response: bool = True  # parse HTTP status codes
    rate_limit: int = 0         # max req/s per thread (0=unlimited)
    ramp_up: float = 0.0        # seconds to start all threads
    jitter: float = 0.0         # random delay factor
    payload_size: int = 0       # minimum TCP/UDP payload size
    extra_headers: dict = {}    # custom HTTP headers
    duration: float = 0.0       # auto-stop after N seconds (0=forever)
```

`AttackConfig.copy(**overrides)` returns a new instance (via
`dataclasses.replace`). This is the **only** way to change
configuration during an attack.

### 4.8 `functions.py` — Utilities

| Function | Used by | Purpose |
|---|---|---|
| `random_string(n)` | HTTP, SlowLoic, ReCoil | Random uppercase ASCII |
| `random_bytes(n)` | ICMP, TCP/UDP | Cryptographically random bytes |
| `random_user_agent()` | HTTP, ReCoil | Firefox on Windows UA string |
| `random_http_header()` | HTTP, ReCoil | Full HTTP/1.1 request as bytes |
| `build_tcp_payload()` | XXP | Payload with optional random padding |
| `parse_int(s, min, max)` | (unused; available utility) | Safe integer parsing |
| `resolve_host()` | (unused; available utility) | DNS resolution |
| `icmp_checksum()` | ICMP | ICMP header checksum |

---

## 5. Data Flow During an Attack

```
$ loic --target-url api.example.com --method http --tls --threads 50

1. cli.parse_args()
   → args.target_url = "api.example.com"
   → args.method = "http"
   → args.tls = True
   → args.threads = 50

2. cli.resolve_target(args)
   → DNS lookup: api.example.com → 10.0.0.42
   → returns ("10.0.0.42", "api.example.com")

3. cli.build_config(args, "10.0.0.42", "api.example.com")
   → AttackConfig(target_ip="10.0.0.42", target_host="api.example.com",
                  method=HTTP, port=443, use_tls=True, threads=50, ...)

4. cli.main() creates event loop, calls run_attack()

5. run_attack(config)
   → engine.start(config)
      → 50× HTTPFlooder(config).start_async()
         → each creates asyncio.Task wrapping _run()
   → while True: draw_dashboard(), sleep(1s)
   → on SIGINT: engine.stop()

6. Each HTTPFlooder._run():
   → while self._is_flooding:
        connect 10.0.0.42:443 (TLS)
        send "GET / HTTP/1.1\r\nHost: api.example.com\r\n..."
        read response (4096 bytes)
        parse status → status_codes[200] += 1
        self.downloaded += 1
        sleep(delay)
        close

7. Engine._stats_loop() every 1s:
   → sum all flooder.requested → snap.requested
   → sum all flooder.status_codes → snap.status_codes
   → record into MetricsCollector
   → CLI reads snap and draws dashboard

8. On SIGINT:
   → engine.stop()
      → all flooder.stop_async() → cancel tasks
      → gather cleanup()
      → cancel stats loop
   → metrics.summary() printed to terminal
   → if --output: metrics.export_json(path)
```

---

## 6. How to Add a New Flooder

1. **Create** `loic/flooders/new_flooder.py`
   ```python
   from loic.flooders.base import AsyncFlooder
   from loic.config import AttackConfig

   class NewFlooder(AsyncFlooder):
       def __init__(self, config: AttackConfig):
           super().__init__()
           self.target = config.target_ip
           self.port = config.port
           # extract other needed config fields

       async def _run(self):
           while self._is_flooding:
               # your I/O loop here
               # increment self.requested, self.failed, etc.
               pass

       async def cleanup(self):
           # close any open connections
           pass
   ```

2. **Register** it in `loic/flooders/__init__.py`
   ```python
   from loic.flooders.new_flooder import NewFlooder
   ```

3. **Wire** it in `attack.py` → `_create_flooder()`
   ```python
   elif config.method == Protocol.NEW_PROTO:
       return NewFlooder(config)
   ```

4. **Add** the protocol in `protocol.py`
   ```python
   class Protocol(IntEnum):
       ...
       NEW_PROTO = 7
   ```

5. **Add** CLI support in `cli.py` → `METHODS` dict
   ```python
   METHODS = {
       ...
       "newproto": Protocol.NEW_PROTO,
   }
   ```

6. **Optionally** add new config fields to `config.py` if the attack
   method needs unique parameters.

That's it. The engine automatically handles stats aggregation,
thread maintenance, ramp-up, and graceful shutdown for any new
`AsyncFlooder` subclass.

---

## 7. How to Add a New CLI Flag

1. **Add** the field to `AttackConfig` in `config.py`
   ```python
   @dataclass(frozen=True)
   class AttackConfig:
       ...
       my_new_flag: bool = False
   ```

2. **Add** the argparse argument in `cli.py` → `parse_args()`
   ```python
   a.add_argument("--my-new-flag", action="store_true",
                  help="Description of what it does")
   ```

3. **Pass** it through in `build_config()`
   ```python
   return AttackConfig(
       ...
       my_new_flag=args.my_new_flag,
   )
   ```

4. **Consume** it in the relevant flooder's `__init__()`
   ```python
   self.my_new_flag = config.my_new_flag
   ```

If the flag affects engine behaviour (not just a flooder), add the
logic in `attack.py`.

---

## 8. Thread Safety Model

```
┌─────────────────────────────────────────────────────┐
│ Main thread (event loop)                            │
│  cli.py::run_attack()                               │
│  attack.py::AttackEngine                            │
│  flooders/*.py::_run()         ← asyncio tasks       │
│  ← everything here is single-threaded, cooperative  │
│    no data races between asyncio tasks               │
│                                                     │
│  asyncio.Lock in AttackEngine for config mutation   │
│  and flooder pool resize                            │
└─────────────────────────────────────────────────────┘
          ▲                    ▲
          │ asyncio.ensure_    │ loop.call_soon_
          │ future()           │ threadsafe()
          │                    │
┌─────────┴──────┐    ┌───────┴──────────┐
│ IRC thread     │    │ Signal handler   │
│ (daemon)       │    │ (main thread)    │
│ HiveMindClient │    │ SIGINT/SIGTERM   │
│ _run()         │    │ → engine.stop()  │
└────────────────┘    └──────────────────┘
```

**Rules:**
- Flooders only touch their own fields (`self.requested`, etc.)
- Engine reads flooder fields only inside `_collect_and_maintain()` (under lock)
- IRC callback and signal handler **schedule** work onto the event loop;
  they never touch engine state directly
- `AttackConfig` is frozen — no write-after-read races

---

## 9. Event Loop and Task Lifecycle

```
asyncio.new_event_loop()
  │
  ├─→ loop.run_until_complete(run_attack())
  │     │
  │     ├─→ engine.start(config)
  │     │     └─→ for each thread:
  │     │           f = Flooder(config)
  │     │           f.start_async()   ← asyncio.ensure_future(f._run())
  │     │
  │     ├─→ engine._stats_task = asyncio.ensure_future(_stats_loop())
  │     │
  │     └─→ while True: draw_dashboard(); await asyncio.sleep(1)
  │
  ├─→ SIGINT → signal_handler → engine.stop()
  │     └─→ cancel all flooder tasks
  │         cancel stats task
  │         gather cleanup()
  │
  └─→ loop.close()
```

**Task tree during an active attack:**
```
run_attack()                      ← 1 task
├── HTTPFlooder[0]._run()         ← 1 task per thread
├── HTTPFlooder[1]._run()
├── ...
├── HTTPFlooder[49]._run()        ← 50 flooder tasks total
└── _stats_loop()                 ← 1 task
    Total: 52 concurrent tasks in one thread
```

---

## 10. Performance Considerations

### Where the time goes (HTTP flooder, per request)

```
asyncio.open_connection()  ← TCP handshake (1 RTT)
writer.drain()             ← flush send buffer
reader.read(4096)           ← wait for response (1+ RTT)
writer.wait_closed()       ← TCP close handshake
asyncio.sleep(delay)        ← configurable
```

The bottleneck is almost always **network RTT**, not CPU. asyncio lets
thousands of these pipelines overlap on the same thread.

### Max connections per machine

Limited by:
1. **Ephemeral port range:** `net.ipv4.ip_local_port_range = 1024 65535` → ~64k ports
2. **TIME_WAIT recycling:** `tcp_tw_reuse = 1` is critical for high-rate HTTP
3. **File descriptors:** `ulimit -n 1048576` (each TCP socket is one fd)
4. **Conntrack table:** `nf_conntrack_max = 1048576` (if behind NAT)

Until these are tuned, a single machine tops out at ~28k concurrent
connections before `EADDRNOTAVAIL`.

### Async I/O patterns used

| Pattern | Where | Why |
|---|---|---|
| `asyncio.open_connection()` | HTTP, TCP, SlowLoic, ReCoil | Stream-based TCP (supports TLS) |
| `loop.sock_sendto()` | UDP | No native async UDP, wrap raw socket |
| `loop.run_in_executor()` | ICMP | Raw socket I/O is blocking |
| `asyncio.gather(*tasks)` | Engine.stop() | Parallel cleanup of all flooders |
| `asyncio.wait_for(fut, timeout)` | All connect/read ops | Prevent hanging on dead targets |
| `asyncio.sleep(interval)` | All delay paths | Non-blocking pause |

---

## 11. Error Handling Strategy

### At the flooder level

```python
try:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(...), timeout=timeout_sec)
except asyncio.TimeoutError:
    self.failed += 1
    self._consecutive_failures += 1
    self._check_backoff()      # → 5s cooldown after 50 fails
except ConnectionRefusedError:
    self.failed += 1
    self._consecutive_failures += 1
    self._check_backoff()
except asyncio.CancelledError:
    raise                       # let the engine handle cancellation
except Exception:
    self.failed += 1
    logger.debug("unexpected: %s", e)  # don't crash the task
```

Flooder tasks **never crash**. They log, count failures, back off, and
keep running. If `is_flooding` becomes `False`, the loop exits cleanly.

### At the engine level

```python
try:
    await f.stop_async()
except Exception:
    pass  # best-effort cleanup
```

The engine `gather`s shutdown with `return_exceptions=True`. Individual
flooder cleanup failures don't block the shutdown of others.

### At the CLI level

`run_attack()` wraps everything in try/finally — the summary report is
always printed, even if the attack itself fails.

---

## 12. Testing Strategy

The project currently has no unit tests. Here's how to add them:

### Unit tests per module

| Module | What to test |
|---|---|
| `functions.py` | `random_http_header()` produces valid HTTP; `icmp_checksum()` matches known values; `build_tcp_payload()` respects `min_length` |
| `config.py` | `AttackConfig.copy()` returns new instance; frozen prevents mutation |
| `protocol.py` | `Protocol.label` returns correct strings |
| `metrics.py` | `MetricsCollector.record()` maintains history cap; `summary()` math is correct; `export_json()` produces valid JSON |

### Integration tests

| Scenario | What to verify |
|---|---|
| HTTP flood against local echo server | Engine starts/stops; stats increment; status codes parsed |
| TCP flood against local listener | Connections established; payloads received match config |
| Ramp-up | Flooder count increases over time; reaches `config.threads` |
| Circuit breaker | After 50 failures, flooder pauses; resets after cooldown |
| Graceful shutdown | Ctrl+C produces summary report; metrics export file exists |
| Frozen config mutation | `config.port = 443` raises `FrozenInstanceError` |
| IRC param parsing | `parse_irc_params(["key=val", "start"])` returns `{"key": "val", "start": True}` |

### Test fixtures

```python
import asyncio
import pytest
from loic.attack import AttackEngine
from loic.config import AttackConfig
from loic.protocol import Protocol

@pytest.fixture
async def echo_server(unused_tcp_port):
    """Start an echo server on a random port for integration tests."""
    async def handler(reader, writer):
        data = await reader.read(4096)
        writer.write(b"HTTP/1.1 200 OK\r\n\r\n")
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(handler, "127.0.0.1", unused_tcp_port)
    yield unused_tcp_port
    server.close()

@pytest.mark.asyncio
async def test_http_flood_counts_requests(echo_server):
    config = AttackConfig(target_ip="127.0.0.1", port=echo_server,
                          method=Protocol.HTTP, threads=2, duration=2)
    engine = AttackEngine()
    await engine.start(config)
    await asyncio.sleep(3)
    stats = engine.get_stats()
    assert stats.requested > 0
    assert stats.failed == 0
```

---

## 13. Common Pitfalls for Contributors

1. **Don't `time.sleep()` in async code.** Use `await asyncio.sleep()`.
   The former blocks the entire event loop; the latter yields to other tasks.

2. **Don't mutate `AttackConfig`.** It's frozen. Use `.copy(**changes)`.

3. **Always check `self._is_flooding` in loops.** The engine sets it to
   `False` on shutdown. Loops that don't check will delay cancellation.

4. **Always `await writer.wait_closed()` after `writer.close()`.** Without
   it, the OS TCP close handshake may not complete, leaking file descriptors.

5. **Catch `asyncio.CancelledError` explicitly.** This is how the engine
   cancels tasks. Don't swallow it — if you catch it and don't re-raise,
   the task won't actually stop.

6. **Use `asyncio.wait_for()` for connect/read with timeouts.** Without a
   timeout, a dead target can hang a flooder task forever.

7. **Keep `cleanup()` idempotent.** The engine may call it on a partially
   initialised flooder. Close operations should be wrapped in try/except.

8. **Don't add required deps.** Core LOIC must work with nothing but stdlib.
   Optional features (IRC, scapy) must use lazy imports and graceful
   degradation.

---

## 14. File Index

```
loic/
├── __init__.py            # Public API re-exports, version
├── __main__.py            # python -m loic entry point
├── protocol.py            # Protocol enum (TCP, UDP, HTTP, SlowLoris, ReCoil, ICMP)
├── req_state.py           # ReqState enum (Idle, Connecting, Requesting, Downloading, Completed, Failed)
├── config.py              # AttackConfig frozen dataclass (23 fields)
├── functions.py           # 10 utility functions (random, HTTP, DNS, ICMP checksum)
├── metrics.py             # MetricsSnapshot + MetricsCollector + JSON/CSV export
├── irc_client.py          # HiveMindClient (optional irc dep) + parse_irc_params()
├── attack.py              # AttackEngine (flooder pool, stats loop, ramp-up, graceful shutdown)
├── cli.py                 # argparse, ANSI dashboard, signal handling, IRC callback dispatch
└── flooders/
    ├── __init__.py         # Re-exports all flooder classes
    ├── base.py             # AsyncFlooder(ABC) — stats fields, task lifecycle, latency tracking
    ├── http_flooder.py     # HTTPFlooder — TLS, status parsing, circuit breaker, rate limiting
    ├── xxp_flooder.py      # XXPFlooder — TCP + UDP, configurable payloads, jitter
    ├── slow_loic.py        # SlowLoic — SlowLoris, partial headers, keep-alive padding
    ├── recoil.py           # ReCoil — reverse HTTP drain, content-length filtering
    └── icmp_flooder.py     # ICMPFlooder — raw socket + scapy fallback, checksum construction
```

---

## 15. Further Reading

- [ATTACK.md](ATTACK.md) — User-facing attack method reference and use cases
- [DEPLOYMENT.md](DEPLOYMENT.md) — Deployment topologies, kernel tuning, cloud orchestration
- [README.md](README.md) — Quickstart and feature overview
- [Python asyncio docs](https://docs.python.org/3/library/asyncio.html) — Official asyncio reference
- [Original C# LOIC](https://github.com/NewEraCracker/LOIC) — Source project this was ported from