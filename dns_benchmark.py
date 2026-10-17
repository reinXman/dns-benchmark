#!/usr/bin/env python3
"""
DNS Benchmark — inspired by GRC's DNS Benchmark (https://www.grc.com/dns/benchmark.htm)

Tests DNS resolvers for:
  1. Cached lookup time   (popular domains — resolver's cache speed)
  2. Uncached lookup time (random subdomains — forces real recursive resolution)
  3. Dot-com lookup time  (random names under .com — TLD resolution path)
  4. Reliability          (% of queries answered, including retransmits)

Usage:
  python dns_benchmark.py                      # test well-known public resolvers
  python dns_benchmark.py --quick              # faster run, fewer samples
  python dns_benchmark.py --servers 1.1.1.1 9.9.9.9
  python dns_benchmark.py --system             # include system resolver(s)
  python dns_benchmark.py --sort uncached

No third-party dependencies — raw UDP DNS packets via the standard library.
"""

import argparse
import os
import random
import socket
import statistics
import string
import struct
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------------------
# DNS wire protocol
# ---------------------------------------------------------------------------

def build_query(name: str, qtype: int = 1) -> bytes:
    """Build a standard DNS query packet (type A, class IN)."""
    qid = random.randint(0, 65535)
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)  # RD=1
    qname = b""
    for label in name.split("."):
        if label:
            encoded = label.encode("ascii")
            qname += bytes([len(encoded)]) + encoded
    qname += b"\x00"
    tail = struct.pack(">HH", qtype, 1)
    return qid, header + qname + tail


def query_once(server: str, name: str, timeout: float):
    """Send one UDP DNS query. Returns (ok, elapsed_ms, rcode)."""
    qid, packet = build_query(name)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        start = time.perf_counter()
        sock.sendto(packet, (server, 53))
        while True:
            data, _ = sock.recvfrom(4096)
            if len(data) < 12 or data[:2] != packet[:2]:
                continue  # not our response
            elapsed = (time.perf_counter() - start) * 1000.0
            flags = struct.unpack(">H", data[2:4])[0]
            return True, elapsed, flags & 0x000F
    except (socket.timeout, OSError):
        return False, 0.0, -1
    finally:
        sock.close()


def query(server: str, name: str, timeout: float = 2.0, retries: int = 2):
    """Query with retransmission on timeout."""
    for _ in range(retries + 1):
        ok, elapsed, rcode = query_once(server, name, timeout)
        if ok:
            return ok, elapsed, rcode
    return False, 0.0, -1


# ---------------------------------------------------------------------------
# Server list
# ---------------------------------------------------------------------------

KNOWN_SERVERS = {
    "Cloudflare":      ["1.1.1.1", "1.0.0.1"],
    "Google":          ["8.8.8.8", "8.8.4.4"],
    "Quad9":           ["9.9.9.9", "149.112.112.112"],
    "OpenDNS (Cisco)": ["208.67.222.222", "208.67.220.220"],
    "AdGuard DNS":     ["94.140.14.14", "94.140.15.15"],
    "CleanBrowsing":   ["185.228.168.9", "185.228.169.9"],
    "Verisign":        ["64.6.64.6", "64.6.65.6"],
    "Level3":          ["209.244.0.3", "209.244.0.4"],
    "Comodo Secure":   ["8.26.56.26", "8.20.247.20"],
    "DNS.WATCH":       ["84.200.69.80", "84.200.70.40"],
    "Yandex":          ["77.88.8.8", "77.88.8.1"],
}

POPULAR_DOMAINS = [
    "google.com", "youtube.com", "facebook.com", "amazon.com", "wikipedia.org",
    "twitter.com", "instagram.com", "reddit.com", "netflix.com", "microsoft.com",
    "apple.com", "github.com", "stackoverflow.com", "linkedin.com", "ebay.com",
    "bing.com", "grc.com", "cloudflare.com", "adobe.com", "spotify.com",
    "yahoo.com", "zoom.us", "twitch.tv", "dropbox.com", "paypal.com",
]

WARMUP_DOMAIN = "google.com"
CHARS = string.ascii_lowercase + string.digits


def random_name(domain: str, length: int = 12) -> str:
    """Return d<random>.<domain> — virtually guaranteed uncached."""
    label = "d" + "".join(random.choices(CHARS, k=length))
    return f"{label}.{domain}"


def get_system_resolvers():
    """Best-effort detection of the system's configured DNS servers."""
    found = []
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["netsh", "interface", "ip", "show", "dns"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            for line in out.splitlines():
                if ":" not in line:
                    continue
                ip = line.split(":", 1)[1].strip()
                if is_ip(ip) and ip not in found:
                    found.append(ip)
        except Exception:
            pass
    else:
        try:
            out = subprocess.run(
                ["resolvectl", "dns"], capture_output=True, text=True, timeout=5
            ).stdout
            for tok in out.replace(":", " ").split():
                if is_ip(tok) and tok not in found:
                    found.append(tok)
        except Exception:
            pass
        if not found:
            try:
                with open("/etc/resolv.conf") as f:
                    for line in f:
                        if line.strip().startswith("nameserver"):
                            ip = line.split()[1]
                            if is_ip(ip) and ip not in found:
                                found.append(ip)
            except Exception:
                pass
    return found


def is_ip(s: str) -> bool:
    parts = s.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


# ---------------------------------------------------------------------------
# Benchmark phases
# ---------------------------------------------------------------------------

class BenchmarkAborted(Exception):
    """Raised when a benchmark run is cancelled via the stop flag."""
    pass


def measure(server: str, names_fn, count: int, timeout: float,
            stop=None, progress=None) -> dict:
    times = []
    failures = 0
    for i in range(count):
        if stop is not None and stop.is_set():
            raise BenchmarkAborted()
        ok, elapsed, _ = query(server, names_fn(), timeout)
        if ok:
            times.append(elapsed)
        else:
            failures += 1
            if len(times) == 0 and failures >= 3 and i < count - 1:
                # Unreachable: 9 consecutive failed attempts (3 queries x 3 retries).
                # Skip the rest but keep the reported totals correct.
                failures += count - i - 1
                if progress:
                    progress(count)
                break
        if progress:
            progress(i + 1)
    return {
        "times": times,
        "failures": failures,
        "total": count,
        "median": statistics.median(times) if times else float("inf"),
    }


def benchmark_server(server: str, samples: int, timeout: float,
                     stop=None, progress=None) -> dict:
    """progress(phase_index, done_in_phase, total_in_phase) — optional."""
    # Warm-up: excludes first-query latency (ARP / route setup) and doubles as
    # a reachability probe — 3 failed attempts means the server is unreachable.
    ok, _, _ = query(server, WARMUP_DOMAIN, timeout)
    if not ok:
        if progress:
            for idx in range(3):
                progress(idx, samples, samples)
        return {"server": server, "cached": float("inf"), "uncached": float("inf"),
                "dotcom": float("inf"), "reliability": 0.0}
    if stop is not None and stop.is_set():
        raise BenchmarkAborted()

    def phase(idx, names_fn):
        cb = (lambda d: progress(idx, d, samples)) if progress else None
        return measure(server, names_fn, samples, timeout, stop=stop, progress=cb)

    cached = phase(0, lambda: random.choice(POPULAR_DOMAINS))
    uncached = phase(1, lambda: random_name(random.choice(POPULAR_DOMAINS)))
    dotcom = phase(2, lambda: random_name("com"))

    total_fail = cached["failures"] + uncached["failures"] + dotcom["failures"]
    total = cached["total"] + uncached["total"] + dotcom["total"]

    return {
        "server": server,
        "cached": cached["median"],
        "uncached": uncached["median"],
        "dotcom": dotcom["median"],
        "reliability": 100.0 * (1 - total_fail / total),
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def fmt(x: float) -> str:
    return "   ---" if x == float("inf") else f"{x:7.1f}"


def print_results(results: list, sort_key: str):
    for r in results:
        r["score"] = (r["cached"] + r["uncached"]) / max(r["reliability"] / 100.0, 0.05)

    keys = {
        "score": lambda r: r["score"],
        "cached": lambda r: r["cached"],
        "uncached": lambda r: r["uncached"],
        "dotcom": lambda r: r["dotcom"],
        "reliability": lambda r: -r["reliability"],
    }
    results.sort(key=keys[sort_key])

    print()
    print("=" * 86)
    print(f"{'Rank':<6}{'Server':<20}{'Cached':>9}{'Uncached':>10}{'Dot-Com':>10}"
          f"{'Reliability':>13}{'Score':>10}")
    print("-" * 86)
    for i, r in enumerate(results, 1):
        print(f"{i:<6}{r['server']:<20}{fmt(r['cached'])+'ms':>9}"
              f"{fmt(r['uncached'])+'ms':>10}{fmt(r['dotcom'])+'ms':>10}"
              f"{r['reliability']:>12.1f}%{r['score']:>10.0f}")
    print("=" * 86)
    print("  Cached    = median latency for popular (likely-cached) domain lookups")
    print("  Uncached  = median latency for random subdomains (forces full recursion)")
    print("  Dot-Com   = median latency for random .com names (TLD resolution path)")
    print("  Score     = (cached + uncached) / reliability — lower is better")
    print()


def main():
    ap = argparse.ArgumentParser(description="DNS Benchmark (GRC-style)")
    ap.add_argument("--servers", nargs="*", help="IP addresses of DNS servers to test")
    ap.add_argument("--system", action="store_true", help="also test system resolver(s)")
    ap.add_argument("--samples", type=int, default=40, help="queries per phase per server (default 40)")
    ap.add_argument("--timeout", type=float, default=2.0, help="per-query timeout in seconds")
    ap.add_argument("--workers", type=int, default=10, help="servers tested in parallel")
    ap.add_argument("--sort", choices=["score", "cached", "uncached", "dotcom", "reliability"],
                    default="score", help="sort results by this column")
    ap.add_argument("--quick", action="store_true", help="quick mode (10 samples per phase)")
    args = ap.parse_args()

    if args.quick:
        args.samples = 10

    servers = []
    labels = {}

    if args.servers:
        for ip in args.servers:
            if not is_ip(ip):
                print(f"Invalid IP: {ip}", file=sys.stderr)
                sys.exit(1)
            servers.append(ip)
            labels[ip] = ip
    else:
        for name, ips in KNOWN_SERVERS.items():
            for ip in ips:
                servers.append(ip)
                labels[ip] = name

    if args.system or not args.servers:
        for ip in get_system_resolvers():
            if ip not in servers:
                servers.append(ip)
                labels[ip] = f"{ip} (system)"

    if not servers:
        print("No servers to test.", file=sys.stderr)
        sys.exit(1)

    print(f"\nDNS Benchmark — {len(servers)} server(s), {args.samples} samples per phase")
    print(f"Timeout: {args.timeout}s  |  Phases: cached, uncached, dot-com\n")

    def run_bench(server):
        r = benchmark_server(server, args.samples, args.timeout)
        name = labels[server][:20]
        print(f"{name:<24} done:  cached {fmt(r['cached'])}ms  "
              f"uncached {fmt(r['uncached'])}ms  dotcom {fmt(r['dotcom'])}ms")
        sys.stdout.flush()
        return r

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(run_bench, servers))

    print_results(results, args.sort)

    best = min(results, key=lambda r: r["score"])
    fc = min(results, key=lambda r: r["cached"])
    fu = min(results, key=lambda r: r["uncached"])
    mr = max(results, key=lambda r: r["reliability"])

    print("Summary:")
    print(f"  Best overall score:   {labels[best['server']]} ({best['server']})")
    print(f"  Fastest cached:       {labels[fc['server']]} ({fc['server']})")
    print(f"  Fastest uncached:     {labels[fu['server']]} ({fu['server']})")
    print(f"  Most reliable:        {labels[mr['server']]} ({mr['reliability']:.1f}%)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)
