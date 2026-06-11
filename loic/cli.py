from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, unquote

from loic import __version__
from loic.attack import AttackEngine
from loic.config import AttackConfig
from loic.irc_client import HiveMindClient, parse_irc_params
from loic.metrics import MetricsCollector
from loic.protocol import Protocol

BANNER = r"""
  _     ___ ___ ___
 | |   / _ \_ _| __|
 | |__| (_) | || |
 |____|\___/___|___|
                  
 Low Orbit Ion Cannon - Python Edition v{ver}
 Network Stress Testing / Chaos Engineering Tool
""".format(ver=__version__)

METHODS = {
    "tcp": Protocol.TCP,
    "udp": Protocol.UDP,
    "http": Protocol.HTTP,
    "slowloris": Protocol.SLOWLOIC,
    "slowloic": Protocol.SLOWLOIC,
    "recoil": Protocol.RECOIL,
    "icmp": Protocol.ICMP,
}

ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_RED = "\033[91m"
ANSI_GREEN = "\033[92m"
ANSI_YELLOW = "\033[93m"
ANSI_BLUE = "\033[94m"
ANSI_MAGENTA = "\033[95m"
ANSI_CYAN = "\033[96m"
ANSI_WHITE = "\033[97m"
ANSI_DIM = "\033[2m"


def format_bytes(n: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(n) < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}m {s}s"
    else:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}m"


def clear_screen():
    if sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


def draw_dashboard(engine: AttackEngine, config: AttackConfig):
    s = engine.get_stats()
    m = engine.metrics.latest

    lines = []
    lines.append(f"{ANSI_BOLD}{ANSI_CYAN}{'=' * 70}{ANSI_RESET}")
    lines.append(f"{ANSI_BOLD}{ANSI_WHITE}  LOIC  |  {config.method.label}  |  "
                 f"{config.target_ip}:{config.port}{ANSI_RESET}")
    lines.append(f"{ANSI_BOLD}{ANSI_CYAN}{'=' * 70}{ANSI_RESET}")
    lines.append("")

    status_color = ANSI_GREEN if s.is_flooding else ANSI_RED
    status_text = "FLOODING" if s.is_flooding else "STOPPED"
    lines.append(f"  {status_color}{ANSI_BOLD}Status:    {status_text}{ANSI_RESET}  "
                 f"Elapsed: {format_duration(s.elapsed)}")
    lines.append(f"  Threads:  {ANSI_BOLD}{len(engine._flooders)}{ANSI_RESET}/{config.threads}    "
                 f"Delay: {config.delay}ms    Timeout: {config.timeout}s")

    lines.append("")
    lines.append(f"{ANSI_BOLD}{ANSI_WHITE}  ── Requests ──{ANSI_RESET}")
    lines.append(f"  {ANSI_GREEN}Requested:   {s.requested:>10}{ANSI_RESET}    "
                 f"{ANSI_YELLOW}Failed: {s.failed:>10}{ANSI_RESET}")
    lines.append(f"  {ANSI_BLUE}Downloaded:  {s.downloaded:>10}{ANSI_RESET}    "
                 f"Req/sec: {s.req_per_sec:>8.1f}")

    if s.status_codes:
        codes = "  ".join(f"{code}: {count}" for code, count in sorted(s.status_codes.items()))
        lines.append(f"  {ANSI_DIM}Status: {codes}{ANSI_RESET}")

    lines.append("")
    lines.append(f"{ANSI_BOLD}{ANSI_WHITE}  ── Thread States ──{ANSI_RESET}")
    state_bar = (
        f"{ANSI_GREEN}Idle:{s.idle}{ANSI_RESET}  "
        f"{ANSI_YELLOW}Conn:{s.connecting}{ANSI_RESET}  "
        f"{ANSI_BLUE}Req:{s.requesting}{ANSI_RESET}  "
        f"{ANSI_MAGENTA}DL:{s.downloading}{ANSI_RESET}"
    )
    lines.append(f"  {state_bar}")

    lines.append("")
    lines.append(f"{ANSI_BOLD}{ANSI_WHITE}  ── Bandwidth ──{ANSI_RESET}")
    lines.append(f"  Sent: {format_bytes(s.bandwidth_out)}/s    "
                 f"Recv: {format_bytes(s.bandwidth_in)}/s    "
                 f"Total Out: {format_bytes(s.bytes_sent)}")

    if s.avg_latency > 0:
        lines.append(f"  Avg Latency: {s.avg_latency * 1000:.1f}ms")

    lines.append("")
    lines.append(f"{ANSI_DIM}  Ctrl+C to stop{ANSI_RESET}")
    lines.append(f"{ANSI_CYAN}{'=' * 70}{ANSI_RESET}")

    output = "\n".join(lines)
    if sys.stdout.isatty():
        sys.stdout.write(f"\033[H{output}\033[K")
        sys.stdout.flush()
    else:
        print(output)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="loic",
        description="Low Orbit Ion Cannon - Python Edition",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  loic --target-ip 10.0.0.1 --method http --port 80 --threads 50\n"
               "  loic --target-url example.com --method tcp --port 443 --threads 20 --duration 60\n"
               "  loic --target-ip 10.0.0.1 --method slowloris --port 80 --socks-per-thread 100\n"
               "  loic --hivemind --irc-server irc.example.com --irc-channel #loic",
    )
    parser.add_argument("--version", action="version", version=f"loic {__version__}")

    t = parser.add_argument_group("target")
    t.add_argument("--target-ip", help="Target IP address")
    t.add_argument("--target-url", help="Target URL (resolves to IP)")
    t.add_argument("--ipv6", action="store_true", help="Use IPv6")

    a = parser.add_argument_group("attack")
    a.add_argument("--method", choices=list(METHODS.keys()), default="tcp", help="Attack method (default: tcp)")
    a.add_argument("--port", type=int, default=80, help="Target port (default: 80)")
    a.add_argument("--threads", type=int, default=10, help="Number of threads (default: 10)")
    a.add_argument("--delay", type=int, default=0, help="Delay between requests in ms (default: 0)")
    a.add_argument("--timeout", type=int, default=30, help="Timeout in seconds (default: 30)")
    a.add_argument("--subsite", default="/", help="HTTP subsite path (default: /)")
    a.add_argument("--data", default="U dun goofed", help="TCP/UDP payload data")
    a.add_argument("--no-wait", action="store_true", help="Don't wait for reply")
    a.add_argument("--random", action="store_true", help="Randomize subsite/message")
    a.add_argument("--use-get", action="store_true", help="Use GET instead of HEAD/POST")
    a.add_argument("--gzip", action="store_true", help="Allow gzip encoding")
    a.add_argument("--tls", action="store_true", help="Use TLS/SSL (HTTPS)")
    a.add_argument("--verify-response", action="store_true", default=True, help="Parse and track HTTP status codes")
    a.add_argument("--no-verify-response", dest="verify_response", action="store_false", help="Don't parse response status codes")
    a.add_argument("--socks-per-thread", type=int, default=25, help="Sockets per thread for slow methods (default: 25)")
    a.add_argument("--payload-size", type=int, default=0, help="Minimum payload size in bytes for TCP/UDP (0=off)")
    a.add_argument("--rate-limit", type=int, default=0, help="Max requests per second per thread (0=unlimited)")
    a.add_argument("--ramp-up", type=float, default=0.0, help="Ramp-up time in seconds to start all threads (0=instant)")
    a.add_argument("--jitter", type=float, default=0.0, help="Random jitter factor (0=off)")
    a.add_argument("--duration", type=float, default=0.0, help="Attack duration in seconds (0=forever)")
    a.add_argument("--header", action="append", default=[], help="Extra HTTP header (e.g., --header 'Authorization: Bearer x')")
    a.add_argument("--dns-refresh", type=float, default=0.0, help="Refresh DNS every N seconds (0=off)")

    o = parser.add_argument_group("output")
    o.add_argument("--output", help="Export metrics to file (json or csv based on extension)")
    o.add_argument("--quiet", action="store_true", help="Minimal output (no dashboard)")
    o.add_argument("--no-color", action="store_true", help="Disable colored output")

    h = parser.add_argument_group("hivemind")
    h.add_argument("--hivemind", action="store_true", help="Enable IRC HiveMind mode")
    h.add_argument("--irc-server", help="IRC server address")
    h.add_argument("--irc-port", type=int, default=6667, help="IRC server port (default: 6667)")
    h.add_argument("--irc-channel", default="#loic", help="IRC channel (default: #loic)")

    return parser.parse_args(argv)


def resolve_target(args):
    target_ip = ""
    target_host = ""

    if args.target_ip:
        ip, display = AttackEngine.resolve_ip(args.target_ip, args.ipv6)
        target_ip = ip
        target_host = display
    elif args.target_url:
        url = args.target_url
        if "://" not in url:
            url = f"https://{url}" if args.tls else f"http://{url}"
        parsed = urlparse(url)
        host = parsed.hostname or ""
        ip, _ = AttackEngine.resolve_ip(host, args.ipv6)
        target_ip = ip
        target_host = host
        if not args.port and parsed.port:
            args.port = parsed.port

    return target_ip, target_host


def build_config(args, target_ip, target_host) -> AttackConfig:
    extra_headers = {}
    for h in args.header:
        if ":" in h:
            k, _, v = h.partition(":")
            extra_headers[k.strip()] = v.strip()

    return AttackConfig(
        target_ip=target_ip,
        target_host=target_host,
        port=args.port,
        method=METHODS[args.method],
        threads=args.threads,
        delay=args.delay,
        timeout=args.timeout,
        subsite=args.subsite,
        data=args.data,
        wait_reply=not args.no_wait,
        random_sub=args.random,
        random_msg=args.random,
        use_get=args.use_get,
        allow_gzip=args.gzip,
        socks_per_thread=args.socks_per_thread,
        ipv6=args.ipv6,
        use_tls=args.tls,
        verify_response=args.verify_response,
        rate_limit=args.rate_limit,
        ramp_up=args.ramp_up,
        jitter=args.jitter,
        payload_size=args.payload_size,
        extra_headers=extra_headers,
        duration=args.duration,
    )


def handle_irc_params(engine: AttackEngine, pars: list[str]):
    params = parse_irc_params(pars)
    c = engine.config
    start = "start" in params
    stop = "stop" in params
    default = "default" in params

    if default:
        c = AttackConfig()
        engine.config = c
        if not start:
            return

    if "targetip" in params:
        try:
            ip, display = AttackEngine.resolve_ip(params["targetip"])
            c = c.copy(target_ip=ip, target_host=display)
        except ValueError:
            pass
    if "targethost" in params:
        try:
            url = params["targethost"]
            if "://" not in url:
                url = f"http://{url}"
            parsed = urlparse(url)
            host = parsed.hostname or ""
            ip, _ = AttackEngine.resolve_ip(host)
            c = c.copy(target_ip=ip, target_host=host)
        except ValueError:
            pass
    if "port" in params:
        try:
            c = c.copy(port=int(params["port"]))
        except ValueError:
            pass
    if "method" in params:
        m = params["method"].lower()
        if m in METHODS:
            c = c.copy(method=METHODS[m])
    if "threads" in params:
        try:
            c = c.copy(threads=max(1, min(99, int(params["threads"]))))
        except ValueError:
            pass
    if "timeout" in params:
        try:
            c = c.copy(timeout=max(1, int(params["timeout"])))
        except ValueError:
            pass
    if "subsite" in params:
        c = c.copy(subsite=unquote(params["subsite"]))
    if "message" in params:
        c = c.copy(data=unquote(params["message"]))
    if "wait" in params:
        c = c.copy(wait_reply=params["wait"].lower() == "true")
    if "random" in params:
        val = params["random"].lower() == "true"
        c = c.copy(random_sub=val, random_msg=val)
    if "speed" in params:
        try:
            c = c.copy(delay=int(params["speed"]))
        except ValueError:
            pass
    if "useget" in params:
        c = c.copy(use_get=params["useget"].lower() == "true")
    if "gzip" in params or "usegzip" in params:
        c = c.copy(allow_gzip=params.get("gzip", params.get("usegzip", "false")).lower() == "true")
    if "sockspthread" in params:
        try:
            c = c.copy(socks_per_thread=max(1, int(params["sockspthread"])))
        except ValueError:
            pass

    engine.config = c

    if stop:
        asyncio.ensure_future(engine.stop())
    elif start:
        asyncio.ensure_future(engine.start(c))


async def run_attack(engine: AttackEngine, config: AttackConfig, args):
    if args.no_color:
        global ANSI_RESET, ANSI_BOLD, ANSI_RED, ANSI_GREEN, ANSI_YELLOW, ANSI_BLUE, ANSI_MAGENTA, ANSI_CYAN, ANSI_WHITE, ANSI_DIM
        ANSI_RESET = ANSI_BOLD = ANSI_RED = ANSI_GREEN = ANSI_YELLOW = ""
        ANSI_BLUE = ANSI_MAGENTA = ANSI_CYAN = ANSI_WHITE = ANSI_DIM = ""

    if not args.quiet:
        try:
            clear_screen()
        except Exception:
            pass

        print(f"{ANSI_BOLD}{BANNER}{ANSI_RESET}")
        target_str = f"{config.target_ip}:{config.port}" if config.target_ip else "IRC-controlled"
        print(f"  Target: {ANSI_BOLD}{target_str}{ANSI_RESET}")
        print(f"  Method: {ANSI_BOLD}{config.method.label}{ANSI_RESET}  |  Threads: {ANSI_BOLD}{config.threads}{ANSI_RESET}")
        if config.use_tls:
            print(f"  TLS: {ANSI_GREEN}ON{ANSI_RESET}")
        if config.ramp_up > 0:
            print(f"  Ramp-up: {config.ramp_up}s")
        if config.rate_limit > 0:
            print(f"  Rate limit: {config.rate_limit} req/s/thread")
        if config.duration > 0:
            print(f"  Duration: {config.duration}s")
        print(f"\n  {ANSI_YELLOW}Press Ctrl+C to stop{ANSI_RESET}\n")

    await engine.start(config)

    start_time = time.monotonic()
    try:
        while True:
            if config.duration > 0 and (time.monotonic() - start_time) >= config.duration:
                if not args.quiet:
                    print(f"\n{ANSI_YELLOW}Duration reached ({config.duration}s){ANSI_RESET}")
                break
            if not args.quiet:
                draw_dashboard(engine, config)
            await asyncio.sleep(1.0)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await engine.stop()

        if not args.quiet:
            summary = engine.metrics.summary()
            if summary:
                clear_screen()
                print(f"\n{ANSI_BOLD}{ANSI_CYAN}{'=' * 50}{ANSI_RESET}")
                print(f"{ANSI_BOLD}{ANSI_WHITE}  LOIC - Final Report{ANSI_RESET}")
                print(f"{ANSI_BOLD}{ANSI_CYAN}{'=' * 50}{ANSI_RESET}\n")
                print(f"  Duration:          {format_duration(summary['elapsed_seconds'])}")
                print(f"  Total Requests:    {summary['total_requested']}")
                print(f"  Total Downloaded:  {summary['total_downloaded']}")
                print(f"  Total Failed:      {summary['total_failed']}")
                print(f"  Failure Rate:      {summary['failure_rate']}%")
                print(f"  Avg Req/sec:       {summary['avg_req_per_sec']}")
                print(f"  Peak Req/sec:      {summary['peak_req_per_sec']}")
                print(f"  Total Sent:        {format_bytes(summary['total_bytes_sent'])}")
                print(f"  Total Received:    {format_bytes(summary['total_bytes_received'])}")
                print(f"  Avg Latency:       {summary['avg_latency_ms']}ms")
                if summary.get("status_codes"):
                    codes = "  ".join(f"{k}: {v}" for k, v in summary["status_codes"].items())
                    print(f"  Status Codes:      {codes}")
                print(f"\n{ANSI_DIM}  Attack complete.{ANSI_RESET}\n")

        if args.output:
            path = Path(args.output)
            if path.suffix == ".csv":
                engine.metrics.export_csv(path)
            else:
                engine.metrics.export_json(path)
            if not args.quiet:
                print(f"  Metrics exported to {path}")


def main(argv=None):
    args = parse_args(argv)

    log_level = logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(message)s")

    target_ip, target_host = "", ""
    if args.target_ip or args.target_url:
        try:
            target_ip, target_host = resolve_target(args)
        except (ValueError, socket.gaierror) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    config = build_config(args, target_ip, target_host)

    engine = AttackEngine(
        metrics_path=Path(args.output) if args.output else None,
        metrics_format="csv" if args.output and args.output.endswith(".csv") else "json",
    )

    irc_client = None
    if args.hivemind:
        if not args.irc_server:
            print("Error: --irc-server is required for hivemind mode", file=sys.stderr)
            sys.exit(1)
        irc_client = HiveMindClient(
            server=args.irc_server,
            port=args.irc_port,
            channel=args.irc_channel,
            on_params=lambda pars: handle_irc_params(engine, pars),
        )
        irc_client.start()

    if target_ip and not args.hivemind:
        pass  # attack will start in run_attack
    elif not args.hivemind and not target_ip:
        print("Error: No target specified. Use --target-ip or --target-url, or enable --hivemind.", file=sys.stderr)
        sys.exit(1)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def signal_handler(sig, frame):
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(engine.stop()))

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if not args.quiet and not args.hivemind:
        if sys.stdout.isatty():
            try:
                import tty, termios
                old_settings = termios.tcgetattr(sys.stdin)
            except (ImportError, termios.error):
                old_settings = None

    try:
        loop.run_until_complete(run_attack(engine, config, args))
    finally:
        if irc_client:
            irc_client.stop()
        loop.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())