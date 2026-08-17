---
title: "FSx for Lustre — networking, security groups, VPC, IAM, KMS"
slug: networking-security
audience: infra/platform engineers running inference on EKS
last_reviewed: 2026-08-06
---

# FSx for Lustre — networking, security groups, VPC, IAM, KMS

## TL;DR

- **FSx for Lustre file systems are pinned to a subnet, and (except for Persistent-2 Intelligent-Tiering) to a single Availability Zone.** Every EKS pod that mounts the file system must run on a node whose ENI is reachable at the file-system's ENIs — in practice, in the same AZ as the file system, or across a peered/attached network that carries the required ports.
- **The Lustre wire protocol needs TCP 988 and TCP 1018–1023** between clients and the FSx ENIs, plus **the file-system security group must self-reference** so servers can talk to each other. For EFA-enabled Persistent-2 file systems, both SGs must **allow all traffic** and be referenced by security-group ID (CIDRs do not satisfy EFA rules).
- **The FSx *data* plane is not routed over VPC Interface Endpoints.** PrivateLink (`com.amazonaws.<region>.fsx` / `fsx-fips`) only fronts the FSx *control* plane (the `fsx.*` API). Data traffic flows client-ENI ↔ file-system-ENI directly inside the VPC.
- **Encryption at rest is always on.** Scratch file systems use FSx-owned KMS keys; Persistent file systems let you pick an AWS-managed key (`aws/fsx`) or a customer-managed CMK. Only symmetric CMKs are accepted.
- **Encryption in transit is automatic — but only on Scratch-2, Persistent-1, and Persistent-2, and only between Nitro-based EC2 instances.** Scratch-1 does not do in-flight encryption. Mixing legacy Xen instances into an EKS node group defeats it silently.
- **Two service-linked roles run underneath:** `AWSServiceRoleForAmazonFSx` (creates ENIs, publishes CloudWatch metrics, etc.) and `AWSServiceRoleForFSxS3Access_<fs-id>` (only if you attach an S3 data repository). The CSI driver's own IAM role is separate.
- **Pod Identity is the preferred way to wire the `aws-fsx-csi-driver` controller to IAM** on modern EKS clusters; IRSA still works and is the path documented by the upstream install guide. Both need `AmazonFSxFullAccess` (or a scoped-down equivalent), `iam:CreateServiceLinkedRole` for `fsx.amazonaws.com`, and a couple of `s3:` reads if you use data repositories.

---

## 1. Where this guide fits

This note is scoped to production **Amazon EKS clusters running inference workloads** that need a shared, high-throughput POSIX file system for model weights, dataset staging, KV-cache spill, and checkpoint I/O. The choice of file system is Amazon FSx for Lustre; the choice of driver is the upstream [`kubernetes-sigs/aws-fsx-csi-driver`](https://github.com/kubernetes-sigs/aws-fsx-csi-driver). Everything below assumes:

- A private-subnet EKS cluster (worker nodes have no public IPs).
- The VPC has `enableDnsSupport = true` and `enableDnsHostnames = true` (required for the FSx DNS name to resolve to the file-system ENIs — see [Amazon VPC DNS attributes](https://docs.aws.amazon.com/vpc/latest/userguide/vpc-dns.html#vpc-dns-updating)).
- Karpenter (or a managed node group) launches worker nodes and honours a `topology.kubernetes.io/zone` constraint.

For the inference cluster templates in this repo, that means the FSx file system's subnet is one of the private inference subnets and the Karpenter `EC2NodeClass` limits `subnetSelectorTerms` to the *same* AZ for any node pool that needs FSx.

## 2. Subnet placement and the single-AZ rule

### 2.1 What "single-AZ" means for FSx for Lustre

Every FSx for Lustre deployment type except **Persistent-2 Intelligent-Tiering** creates its metadata servers (MDS) and object storage servers (OSS) inside **one subnet in one AZ**. From [Deployment and storage class options for FSx for Lustre file systems](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html):

> Persistent file systems are designed for longer-term storage and workloads. For SSD and HDD-based file systems, data is automatically replicated within the same Availability Zone in which the file system is located. For Intelligent-Tiering file systems, data is replicated across multiple Availability Zones.

The [`CreateFileSystem`](https://docs.aws.amazon.com/fsx/latest/APIReference/API_CreateFileSystem.html) API accepts a list of `SubnetIds`, but for FSx for Lustre it only ever contains **one subnet**. That subnet's AZ becomes the FS's AZ, and the FS is not movable across AZs — you would have to create a new FS and copy data.

Practically, this means:

- A pod that mounts `PersistentVolumeClaim -> StorageClass -> FSx` must land on a node in the FSx's AZ. Otherwise the mount will succeed (the DNS name resolves cluster-wide) but every RPC crosses AZ boundaries: cost, latency, and — on some deployment types — a hard capacity ceiling because the traffic runs on regular ENIs instead of EFA fabric.
- If the AZ suffers an event, the whole FSx is unavailable. Multi-AZ resilience for FSx for Lustre is a customer-side pattern: two FSx file systems in two AZs with an S3 data repository as the durable source of truth, or Persistent-2 Intelligent-Tiering which does the AZ replication for you.

### 2.2 Pinning EKS nodes to the FSx AZ

Karpenter's [`NodePool`](https://karpenter.sh/docs/concepts/nodepools/) supports zone requirements. For every FSx file system the cluster mounts, we recommend a dedicated node pool (or at minimum a labelled subset) constrained to the same AZ:

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: inference-fsx-az-a
spec:
  template:
    metadata:
      labels:
        workload.example.com/fsx-az: "a"
    spec:
      requirements:
        - key: topology.kubernetes.io/zone
          operator: In
          values: ["us-west-2a"]        # must match FSx subnet AZ
        - key: karpenter.k8s.aws/instance-family
          operator: In
          values: ["p5", "p5e", "g6", "g6e", "trn2", "m7i", "c7i"]  # Nitro only
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["on-demand"]
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: inference-fsx-az-a
```

And a matching `EC2NodeClass` that limits `subnetSelectorTerms` to the private inference subnet in `us-west-2a` — the same subnet the FSx ENI lives in, or a subnet in the same AZ that has route-table connectivity to it.

Pods that require FSx should carry the affinity to match:

```yaml
spec:
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
          - matchExpressions:
              - key: topology.kubernetes.io/zone
                operator: In
                values: ["us-west-2a"]
```

If you *must* let pods drift to other AZs (e.g., during a zonal outage), be honest about the trade: the pod will still mount FSx but each read/write incurs [inter-AZ data transfer](https://aws.amazon.com/ec2/pricing/on-demand/#Data_Transfer_within_the_same_AWS_Region) at $0.01/GB *each way*, and the latency floor climbs from ~sub-ms to ~1–2 ms per operation before Lustre round-trips.

### 2.3 Subnet sizing and ENI count

FSx for Lustre puts **one ENI per MDS + one per OSS** in the file-system subnet. Per [IP addresses for file systems](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html#ip-addesses-for-fs), each OSS covers a fixed amount of storage:

| Deployment type            | Storage per OSS |
|----------------------------|-----------------|
| Persistent-2 EFA, 125 MB/s/TiB     | 38.4 TiB |
| Persistent-2 EFA, 250 MB/s/TiB     | 19.2 TiB |
| Persistent-2 EFA, 500 MB/s/TiB     | 9.6 TiB  |
| Persistent-2 EFA, 1000 MB/s/TiB    | 4.8 TiB  |
| Persistent-2 non-EFA               | 2.4 TiB  |
| Persistent-1 SSD                   | 2.4 TiB  |
| Scratch-2                          | 2.4 TiB  |
| Scratch-1                          | 3.6 TiB  |

A 96 TiB Persistent-2 SSD file system at 250 MB/s/TiB has `96 / 19.2 = 5` OSSes plus MDS ENIs, so budget **at least 6–10 free /28 addresses** in the FSx subnet. Karpenter should share the same subnet or use one right next to it — remember to also budget IP space for the daemons that run on every EKS node (VPC CNI needs `pods_per_node` addresses per node).

## 3. Security groups

### 3.1 The two ports Lustre actually uses

From [File system access control with Amazon VPC](https://docs.aws.amazon.com/fsx/latest/LustreGuide/limit-access-security-groups.html):

- **TCP 988** — client ↔ server Lustre RPCs.
- **TCP 1018–1023** — server ↔ server Lustre RPCs (MDS ↔ OSS, OSS ↔ OSS).

Both directions matter because Lustre servers open connections back to clients in some code paths. The security-group model splits into two roles:

- **File-system SG** (`sg-fs`) — attached to the FSx ENIs by the FSx service.
- **Client SG** (`sg-client`) — attached to any EC2/EKS node ENI (and, if you use security groups for pods, to the pod ENI) that needs to mount.

The minimum non-EFA rule set is:

**On `sg-fs`:**

| Direction | Protocol | Port(s)   | Source/Dest        | Purpose |
|-----------|----------|-----------|--------------------|---------|
| Inbound   | TCP      | 988       | `sg-client`        | Client → server |
| Inbound   | TCP      | 1018–1023 | `sg-client`        | Client callbacks |
| Inbound   | TCP      | 988       | `sg-fs` (self-ref) | Server ↔ server |
| Inbound   | TCP      | 1018–1023 | `sg-fs` (self-ref) | Server ↔ server |
| Outbound  | TCP      | 988       | `sg-fs`, `sg-client` | Reverse path |
| Outbound  | TCP      | 1018–1023 | `sg-fs`, `sg-client` | Reverse path |

**On `sg-client`:**

| Direction | Protocol | Port(s)   | Source/Dest | Purpose |
|-----------|----------|-----------|-------------|---------|
| Inbound   | TCP      | 988       | `sg-fs`     | Server → client (callbacks) |
| Inbound   | TCP      | 1018–1023 | `sg-fs`     | Server → client (callbacks) |
| Outbound  | TCP      | 988       | `sg-fs`     | Client → server |
| Outbound  | TCP      | 1018–1023 | `sg-fs`     | Client → server |

You can collapse this into a single SG that self-references — it satisfies both sides — but in an EKS cluster you almost always want a dedicated FSx SG so you can control blast radius. `sg-client` is typically the node/pod SG that Karpenter already assigns.

### 3.2 EFA-enabled Persistent-2

If the FSx is Persistent-2 with EFA (the high-throughput 125/250/500/1000 MB/s/TiB SKUs), the SG rules change per [EFA-enabled security groups](https://docs.aws.amazon.com/fsx/latest/LustreGuide/limit-access-security-groups.html#efa-security-groups):

- Both SGs must **allow all traffic** in and out, referencing each other by SG-ID.
- The file-system SG must also **self-reference** for all traffic.
- **CIDR-based rules do not satisfy EFA** — even `0.0.0.0/0` is rejected as a match for the EFA all-traffic requirement. You *must* reference security-group IDs.

This is the same requirement documented for EFA generally in [Step 1: Prepare an EFA-enabled security group](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa-start.html#efa-start-security). The reason is that EFA uses SRD (Scalable Reliable Datagram) over UDP-like semantics and the SG filter runs on the Nitro card — CIDRs alone don't select EFA-capable peers.

Terraform for the EFA case:

```hcl
resource "aws_security_group" "fsx" {
  name        = "fsx-lustre-${local.postfix}"
  description = "FSx for Lustre file-system SG"
  vpc_id      = var.vpc_id
  tags = merge(local.tags, {
    Name          = "fsx-lustre-${local.postfix}"
    DeploymentId  = local.postfix
  })
}

# Self-referencing all-traffic rule (required for EFA and for Lustre server-to-server)
resource "aws_vpc_security_group_ingress_rule" "fsx_self" {
  security_group_id            = aws_security_group.fsx.id
  ip_protocol                  = "-1"
  referenced_security_group_id = aws_security_group.fsx.id
}
resource "aws_vpc_security_group_egress_rule" "fsx_self_out" {
  security_group_id            = aws_security_group.fsx.id
  ip_protocol                  = "-1"
  referenced_security_group_id = aws_security_group.fsx.id
}

# From/To the EKS node SG (also allows pod ENIs if you use SG for pods)
resource "aws_vpc_security_group_ingress_rule" "fsx_from_nodes" {
  security_group_id            = aws_security_group.fsx.id
  ip_protocol                  = "-1"
  referenced_security_group_id = var.eks_node_security_group_id
}
resource "aws_vpc_security_group_egress_rule" "fsx_to_nodes" {
  security_group_id            = aws_security_group.fsx.id
  ip_protocol                  = "-1"
  referenced_security_group_id = var.eks_node_security_group_id
}

# On the node side, allow FSx SG to reach nodes and vice versa
resource "aws_vpc_security_group_ingress_rule" "nodes_from_fsx" {
  security_group_id            = var.eks_node_security_group_id
  ip_protocol                  = "-1"
  referenced_security_group_id = aws_security_group.fsx.id
}
resource "aws_vpc_security_group_egress_rule" "nodes_to_fsx" {
  security_group_id            = var.eks_node_security_group_id
  ip_protocol                  = "-1"
  referenced_security_group_id = aws_security_group.fsx.id
}
```

For non-EFA workloads, replace `ip_protocol = "-1"` with the two TCP port rules to reduce blast radius:

```hcl
resource "aws_vpc_security_group_ingress_rule" "fsx_lustre_988" {
  security_group_id            = aws_security_group.fsx.id
  ip_protocol                  = "tcp"
  from_port                    = 988
  to_port                      = 988
  referenced_security_group_id = var.eks_node_security_group_id
}
resource "aws_vpc_security_group_ingress_rule" "fsx_lustre_1018_1023" {
  security_group_id            = aws_security_group.fsx.id
  ip_protocol                  = "tcp"
  from_port                    = 1018
  to_port                      = 1023
  referenced_security_group_id = var.eks_node_security_group_id
}
# mirror for self-ref and egress
```

### 3.3 Security groups for pods (SGP)

If you use [security groups for pods](https://docs.aws.amazon.com/eks/latest/userguide/security-groups-for-pods.html), the pod ENI carries its *own* SG, not the node SG. In that model, `sg-client` above should be attached to the pod ENI via a `SecurityGroupPolicy` CR:

```yaml
apiVersion: vpcresources.k8s.aws/v1beta1
kind: SecurityGroupPolicy
metadata:
  name: fsx-clients
  namespace: inference
spec:
  podSelector:
    matchLabels:
      workload.example.com/fsx-client: "true"
  securityGroups:
    groupIds:
      - sg-0aa...fsx-client-sg
```

Two gotchas:

- The **VPC CNI must have `ENABLE_POD_ENI=true`** and, when combined with Pod Identity, `POD_SECURITY_GROUP_ENFORCING_MODE=standard`. See the [Pod Identity considerations](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html#pod-id-considerations).
- The **CSI driver's own pods** (`fsx-csi-controller-*`, `fsx-csi-node-*`) run in `kube-system` and their DaemonSet uses `hostNetwork: true`. They inherit the *node* SG, so the node SG must also be a Lustre client — even if application pods have their own SG.

### 3.4 Network ACLs

Security groups are stateful, but if you have restrictive NACLs on the FSx subnet or the node subnets, add the same TCP 988 and 1018–1023 rules **plus the ephemeral return range** (`1024–65535`) in the opposite direction. NACLs are stateless; forgetting the ephemeral range causes reads to hang after the first RPC.

## 4. MTU, jumbo frames, and TCP tuning

FSx for Lustre supports and prefers **MTU 9001 (jumbo frames)** on client ENIs. All Nitro-based EC2 instances default to 9001 inside a VPC. To verify on a node:

```bash
ip -o link show | awk '{print $2, $5}' | grep -E 'eth0|ens5'
# ens5: 9001
```

Sanity check the path with a large-packet ping — jumbo frames only work end-to-end if every hop supports them, and the FSx ENIs and every intermediate router inside a single VPC do. Crossing a Transit Gateway or a VPN drops you back to 1500/8500, which will cause Lustre RPCs to fragment and hurt throughput:

```bash
ping -M do -s 8972 <fs-xxxx-ip>       # 8972 = 9000 - IP(20) - ICMP(8)
```

Lustre itself does not need TCP tuning on Nitro clients — the kernel's autotuning handles the bandwidth-delay product for typical FSx throughputs. If you are pushing multi-GB/s from a single client, consider raising `net.core.rmem_max`/`wmem_max` and `net.ipv4.tcp_rmem`/`tcp_wmem` and setting `sysctl net.ipv4.tcp_congestion_control=bbr`. These are pod-level or node-level knobs; the FSx side is auto-tuned.

## 5. DNS resolution

### 5.1 The name and where it resolves

Every FSx file system exposes a DNS record of the form:

```
fs-0123456789abcdef0.fsx.<region>.amazonaws.com
```

That name resolves to the **private IPv4 addresses of the file-system ENIs** (typically one per OSS). Resolution requires:

1. `enableDnsSupport = true` on the VPC.
2. `enableDnsHostnames = true` on the VPC (not strictly required for the FSx name to resolve, but required if you also want to reach EC2's `ec2.internal` names).
3. The DHCP options set on the VPC must include `AmazonProvidedDNS` (the `169.254.169.253` resolver) **or** a downstream resolver that forwards `fsx.amazonaws.com` upstream.

If you use a Route 53 Resolver forwarder or a custom on-prem DNS, make sure `fsx.amazonaws.com` and `fsx.<region>.amazonaws.com` are not caught by a wildcard override — they must reach the AWS resolver.

### 5.2 Cross-VPC and hybrid resolution

The FSx name is not a public DNS record. It only resolves to private addresses. In practice:

- **Same VPC:** resolves via the VPC resolver (Route 53 Resolver inbound at `+2` on the VPC CIDR, i.e., `<vpc-base>.2`).
- **Peered VPCs:** you need to enable [`AllowDnsResolutionFromRemoteVpc`](https://docs.aws.amazon.com/vpc/latest/peering/modify-peering-connections.html) on the peering connection *and* route the FSx subnet from the peer.
- **On-premises via Direct Connect/VPN:** create a [Route 53 Resolver inbound endpoint](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/resolver-getting-started.html) in the FSx VPC and forward from on-prem DNS.

### 5.3 The mount command

Once DNS resolves, the actual mount from a Nitro Linux client is:

```bash
sudo mkdir -p /fsx
sudo mount -t lustre -o relatime,flock \
  fs-0123456789abcdef0.fsx.us-west-2.amazonaws.com@tcp:/mountname \
  /fsx
```

See [Mounting from an Amazon Elastic Compute Cloud instance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/mounting-ec2-instance.html). Options that matter in production:

- `flock` — enables POSIX file locking. Frameworks like PyTorch DataLoader's file-based caches need this. Without it, `fcntl(F_SETLK)` returns `ENOSYS` and libraries fall back to broken shared-memory paths.
- `relatime` (default) or `noatime` — reduces write amplification. Use `noatime` for read-heavy inference weight caches where you don't care about access-time tracking; use `relatime` if you rely on the `file release` background tenancy on Intelligent-Tiering.
- `_netdev` — tells systemd this is a network mount; do not add it in `/etc/fstab` on EKS nodes because the CSI driver handles mounting.

The CSI driver constructs and executes this exact command inside its node pod when `NodePublishVolume` is called. You do not have to mount manually.

## 6. VPC endpoints and PrivateLink

### 6.1 What PrivateLink covers and what it does not

FSx supports an **Interface VPC Endpoint for the control-plane API** — see [Amazon FSx for Lustre and interface VPC endpoints (AWS PrivateLink)](https://docs.aws.amazon.com/fsx/latest/LustreGuide/fsx-vpc-endpoints.html):

- `com.amazonaws.<region>.fsx` — standard endpoint.
- `com.amazonaws.<region>.fsx-fips` — FIPS-compliant endpoint.

This is what the CSI driver, `aws fsx create-file-system` CLI calls, CloudFormation, and Terraform all talk to. With `PrivateDnsEnabled = true` on the endpoint, calls to `fsx.<region>.amazonaws.com` route over the endpoint and never leave the VPC.

**What PrivateLink does *not* cover:** the actual Lustre wire protocol between clients and the file-system ENIs. That's a private-address flow within the VPC (or a peered/attached VPC) on TCP 988/1018–1023 — it is not fronted by any endpoint service and cannot be exposed to arbitrary VPCs via PrivateLink. This distinction bites teams whose "landing-zone" networking assumes every AWS service is reachable through a shared-services endpoint VPC. FSx *data* requires either same-VPC placement or a routed peering/TGW attachment with the SG rules above.

### 6.2 When you actually need the interface endpoint

The FSx control-plane endpoint is worth it when:

- Your worker subnets are private and have **no NAT** (fully isolated), so the CSI driver has no way out to the public FSx API otherwise.
- You need to enforce that FSx API calls stay in the AWS backbone (regulated environments).
- You want to attach a **VPC endpoint policy** to gate what `fsx:*` actions may be called from inside the VPC — for example, to prevent a compromised pod from deleting file systems in other accounts.

Example endpoint policy that limits calls to your own account and disallows destructive verbs:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowReadAndCreateOnly",
      "Effect": "Allow",
      "Principal": {"AWS": "arn:aws:iam::111122223333:root"},
      "Action": [
        "fsx:CreateFileSystem",
        "fsx:CreateFileSystemFromBackup",
        "fsx:CreateDataRepositoryTask",
        "fsx:CreateDataRepositoryAssociation",
        "fsx:TagResource",
        "fsx:Describe*",
        "fsx:List*"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {"aws:PrincipalAccount": "111122223333"}
      }
    }
  ]
}
```

You still need `sts:*` (for role assumption) and `kms:*` (for CMK operations) available through other endpoints or NAT if you want a fully-airgapped subnet — those are separate endpoint services (`com.amazonaws.<region>.sts`, `com.amazonaws.<region>.kms`).

### 6.3 Endpoint SG

The interface endpoint gets its own SG. Allow TCP 443 inbound from the EKS node/pod SG and from the CSI controller SG. Restrict outbound to the same SGs; the endpoint's ENIs never initiate connections.

## 7. Cross-AZ and cross-Region considerations

### 7.1 Cross-AZ latency and cost

Inside a single AZ, FSx for Lustre RPCs on a Nitro instance clock in around 250–500 µs at p50 for metadata and considerably lower for large sequential reads over EFA. Cross-AZ pushes that to ~1–2 ms floor per RPC because you pay for the AZ hop. For metadata-heavy training/inference pre-processing (lots of `stat`, `open`, `readdir`), the difference is dramatic — a 4x hit on p99 is normal.

Data transfer between AZs in the same Region is [$0.01/GB in each direction](https://aws.amazon.com/ec2/pricing/on-demand/#Data_Transfer_within_the_same_AWS_Region) — so a 100 GB weight file pulled cross-AZ costs $2 round trip. That adds up on a fleet.

### 7.2 Cross-Region — no

FSx for Lustre is a regional service and file systems cannot be accessed across Regions. For cross-Region model-weight distribution, the pattern is: FSx with an S3 data repository per Region, one S3 replication rule, and load the local FSx from S3.

### 7.3 Persistent-2 Intelligent-Tiering

The one exception to single-AZ is **Persistent-2 Intelligent-Tiering**. From the deployment-options doc:

> For Intelligent-Tiering file systems, data is replicated across multiple Availability Zones.

The FS still has a primary AZ where its ENIs live, and clients still mount through a DNS name, but the *durability* domain is multi-AZ. Practically for EKS, you can still only have client-side low-latency access from the primary AZ; if that AZ fails, FSx replaces infrastructure but there is no automatic failover of the DNS to another AZ's ENIs (as of 2026). Treat it as a durability improvement, not an HA one.

## 8. IAM — control plane vs data plane

Amazon FSx splits IAM into two very different scopes:

- **Control plane** — the `fsx:*` and adjacent `iam:*`/`ec2:*` actions used to create, describe, tag, delete, and repair file systems. This is where the CSI driver's IAM role and human operators live.
- **Data plane** — the actual Lustre protocol. FSx for Lustre does **not** authorize file I/O through IAM. Once a client has network reach and can mount, POSIX permissions (uid/gid, `chmod`, ACLs) govern access. There is no per-object IAM authz check on read/write. Segmentation is via the VPC and security groups.

This has two consequences:

1. You cannot use IAM to deny `read` to a specific bucket-like resource inside a Lustre file system. If a pod can mount, it can see whatever the file mode allows. Multi-tenant designs need per-tenant file systems or per-tenant paths with disciplined uid/gid mapping.
2. Compromising a control-plane IAM role does not, by itself, expose data. It exposes the *ability to create/delete/tag* file systems, and (with S3 data repositories) the ability to change how S3 objects are imported.

### 8.1 Service-linked roles

Two SLRs run underneath every FSx file system — see [Using service-linked roles for Amazon FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-service-linked-roles.html).

#### AWSServiceRoleForAmazonFSx

Created on the first `CreateFileSystem` call in the account (or manually with `iam:CreateServiceLinkedRole`). Its attached AWS-managed policy is `AmazonFSxServiceRolePolicy`. Full JSON per AWS:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CreateFileSystem",
      "Effect": "Allow",
      "Action": [
        "ds:AuthorizeApplication",
        "ds:GetAuthorizedApplicationDetails",
        "ds:UnauthorizeApplication",
        "ec2:CreateNetworkInterface",
        "ec2:CreateNetworkInterfacePermission",
        "ec2:DeleteNetworkInterface",
        "ec2:DescribeAddresses",
        "ec2:DescribeDhcpOptions",
        "ec2:DescribeNetworkInterfaces",
        "ec2:DescribeRouteTables",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeSubnets",
        "ec2:DescribeVPCs",
        "ec2:DisassociateAddress",
        "ec2:GetSecurityGroupsForVpc",
        "route53:AssociateVPCWithHostedZone"
      ],
      "Resource": "*"
    },
    {
      "Sid": "PutMetrics",
      "Effect": "Allow",
      "Action": ["cloudwatch:PutMetricData"],
      "Resource": ["*"],
      "Condition": {"StringEquals": {"cloudwatch:namespace": "AWS/FSx"}}
    },
    {
      "Sid": "TagResourceNetworkInterface",
      "Effect": "Allow",
      "Action": ["ec2:CreateTags"],
      "Resource": ["arn:aws:ec2:*:*:network-interface/*"],
      "Condition": {
        "StringEquals": {"ec2:CreateAction": "CreateNetworkInterface"},
        "ForAllValues:StringEquals": {"aws:TagKeys": "AmazonFSx.FileSystemId"}
      }
    },
    {
      "Sid": "ManageNetworkInterface",
      "Effect": "Allow",
      "Action": [
        "ec2:AssignPrivateIpAddresses",
        "ec2:ModifyNetworkInterfaceAttribute",
        "ec2:UnassignPrivateIpAddresses"
      ],
      "Resource": ["arn:aws:ec2:*:*:network-interface/*"],
      "Condition": {"Null": {"aws:ResourceTag/AmazonFSx.FileSystemId": "false"}}
    },
    {
      "Sid": "ManageRouteTable",
      "Effect": "Allow",
      "Action": ["ec2:CreateRoute", "ec2:ReplaceRoute", "ec2:DeleteRoute"],
      "Resource": ["arn:aws:ec2:*:*:route-table/*"],
      "Condition": {"StringEquals": {"aws:ResourceTag/AmazonFSx": "ManagedByAmazonFSx"}}
    },
    {
      "Sid": "PutCloudWatchLogs",
      "Effect": "Allow",
      "Action": ["logs:DescribeLogGroups", "logs:DescribeLogStreams", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:*:*:log-group:/aws/fsx/*"
    },
    {
      "Sid": "ManageAuditLogs",
      "Effect": "Allow",
      "Action": ["firehose:DescribeDeliveryStream", "firehose:PutRecord", "firehose:PutRecordBatch"],
      "Resource": "arn:aws:firehose:*:*:deliverystream/aws-fsx-*"
    }
  ]
}
```

Note the tag-scoped writes: FSx can only modify network interfaces and route-table entries **that carry an `AmazonFSx.FileSystemId` or `AmazonFSx=ManagedByAmazonFSx` tag**. This is enforced by IAM, not by hope — if you strip those tags manually, FSx loses the ability to manage those resources.

#### AWSServiceRoleForFSxS3Access_<fs-id>

Created *per file system* whenever you attach an S3 data repository (via `DataRepositoryAssociation` or the legacy `ImportPath`/`ExportPath` on Persistent-1). Its permissions:

- `s3:AbortMultipartUpload`
- `s3:DeleteObject`
- `s3:Get*`
- `s3:List*`
- `s3:PutBucketNotification`
- `s3:PutObject`

Bucket-scoped. If you use S3 data repositories, the bucket policy must allow this SLR — otherwise `DataRepositoryTask`s will fail with `AccessDenied` deep inside FSx's background workers, where the error is hard to trace.

### 8.2 AWS-managed policies for humans and CI

The full set (see [AWS managed policies for Amazon FSx for Lustre](https://docs.aws.amazon.com/fsx/latest/LustreGuide/security-iam-awsmanpol.html)):

| Policy                                | Attach to                        | Use |
|---------------------------------------|----------------------------------|-----|
| `AmazonFSxServiceRolePolicy`          | (SLR only — you can't attach)    | The service acts on your behalf |
| `AmazonFSxDeleteServiceLinkedRoleAccess` | (SLR only)                    | FSx cleans up the S3-access SLR |
| `AmazonFSxFullAccess`                 | CI role, CSI driver role (broad) | Full FSx admin |
| `AmazonFSxConsoleFullAccess`          | Human admin                      | Console + FSx |
| `AmazonFSxReadOnlyAccess`             | Read-only human/CI               | Describe/List only |
| `AmazonFSxConsoleReadOnlyAccess`      | Read-only human console          | Describe/List via console |

For a production CSI driver role, `AmazonFSxFullAccess` is what the upstream install docs recommend, but it is over-broad — it grants `fsx:*` in the account. A tighter policy is in §10.

### 8.3 Data-plane identity (POSIX uid/gid)

Inside the file system, POSIX ownership is by uid/gid. There is no automatic mapping from IAM to uid. Practical choices:

- **Single tenant per file system:** run every pod as a known uid (`fsGroup` / `runAsUser`), and set the mount root to `chown -R` that uid at bootstrap. Fine for a single-team inference cluster.
- **Multi-tenant per file system:** carve out `/tenants/<tenant-id>` directories, `chown` them to a per-tenant uid, and rely on PodSecurity to enforce that pods can only run as their tenant's uid. This is fragile — a pod running as uid 0 defeats it — so add an admission controller (Kyverno/Gatekeeper) that pins `runAsUser` per namespace.

## 9. KMS and encryption at rest

### 9.1 Always on, but *which* key?

From [Encrypting data at rest](https://docs.aws.amazon.com/fsx/latest/LustreGuide/encryption-at-rest.html):

> Encryption of data at rest is automatically enabled when you create an Amazon FSx for Lustre file system through the AWS Management Console, the AWS CLI, or programmatically through the Amazon FSx API or one of the AWS SDKs.

The choice is *which* KMS key protects the data-encryption keys:

- **Scratch file systems (Scratch-1, Scratch-2):** encrypted with keys owned and managed by FSx itself. You cannot specify a CMK. Keys are destroyed when the FS is deleted.
- **Persistent file systems (Persistent-1, Persistent-2 SSD, Persistent-2 Intelligent-Tiering, Persistent HDD):** you specify the KMS key. It can be:
  - **AWS-managed key `aws/fsx`** — default. No creation cost, standard per-request usage cost. You cannot rotate or scope it — it is shared by all `fsx.amazonaws.com` uses in your account/region.
  - **Customer-managed key (CMK)** — a symmetric-only KMS key you own. You control the key policy, rotation, and grants.

The cipher is **XTS-AES-256** in both cases. AWS's key management infrastructure is FIPS 140-2.

### 9.2 CMK key policy

FSx needs the following actions on the CMK — either through key-policy statements or through grants (the SLR requests grants automatically):

- `kms:Encrypt` — optional but included by default
- `kms:Decrypt` — **required**
- `kms:ReEncrypt` — optional
- `kms:GenerateDataKeyWithoutPlaintext` — **required** (rolled up in `kms:GenerateDataKey*`)
- `kms:CreateGrant` — **required**
- `kms:DescribeKey` — **required**
- `kms:ListAliases` — optional (only for console UX)

A minimal, hardened key policy for a CMK dedicated to FSx that lives in a `kms` module:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowRootFullAccess",
      "Effect": "Allow",
      "Principal": {"AWS": "arn:aws:iam::111122223333:root"},
      "Action": "kms:*",
      "Resource": "*"
    },
    {
      "Sid": "AllowFSxToUseTheKey",
      "Effect": "Allow",
      "Principal": {"Service": "fsx.amazonaws.com"},
      "Action": [
        "kms:Decrypt",
        "kms:GenerateDataKey*",
        "kms:CreateGrant",
        "kms:DescribeKey"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "kms:CallerAccount": "111122223333",
          "kms:ViaService": "fsx.us-west-2.amazonaws.com"
        }
      }
    },
    {
      "Sid": "AllowCSIDriverToDescribeKey",
      "Effect": "Allow",
      "Principal": {"AWS": "arn:aws:iam::111122223333:role/AmazonEKSFSxLustreCSIDriverRole"},
      "Action": ["kms:DescribeKey"],
      "Resource": "*"
    }
  ]
}
```

`kms:ViaService` scopes the grant to calls made *through* FSx, not directly from an arbitrary principal that also happens to have permission. This is what stops a compromised IAM role in the account from calling `kms:Decrypt` directly to decrypt data-encryption-key blobs it exfiltrated.

**Enable automatic annual rotation** (`aws kms enable-key-rotation`) on the CMK. FSx transparently picks up the rotated version.

### 9.3 Cross-account CMK

If your CMK lives in a central "security" account and FSx runs in an "app" account:

- Add the app account's root or the SLR (`arn:aws:iam::<app>:role/aws-service-role/fsx.amazonaws.com/AWSServiceRoleForAmazonFSx`) to the key policy with `kms:Decrypt`, `kms:GenerateDataKey*`, `kms:CreateGrant`, `kms:DescribeKey`.
- Grant an IAM identity in the app account permissions to `kms:CreateGrant` and `kms:DescribeKey` on the key — the identity issuing `CreateFileSystem` needs to be able to create grants on the CMK.

### 9.4 What KMS does not protect

- Ephemeral OS-level caches on the client (page cache in the pod). These are unencrypted plaintext copies. If instance-store or EBS on the node is unencrypted, the plaintext can be recovered from a compromised node. Always use **encrypted EBS root volumes** on nodes; NVMe instance-store on Nitro is encrypted with per-instance keys automatically (see [Amazon EC2 Data protection: Instance store volumes](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/data-protection.html#ebs-data-security)).
- CloudWatch log groups (`/aws/fsx/*`). These are separately encrypted with their own KMS key configured on the log group.

## 10. Encryption in transit

### 10.1 What "in-transit" actually means for Lustre

Lustre's wire protocol is not TLS. AWS's implementation of encryption in transit for FSx for Lustre uses **the Nitro System's in-flight encryption between EC2 instances**. From [Amazon EC2 Data protection — Encryption in transit](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/data-protection.html#encryption-transit):

> AWS provides secure and private connectivity between EC2 instances of all types. In addition, some instance types use the offload capabilities of the underlying Nitro System hardware to automatically encrypt in-transit traffic between instances. This encryption uses Authenticated Encryption with Associated Data (AEAD) algorithms, with 256-bit encryption. There is no impact on network performance.

Requirements (from the same doc):

1. **Both endpoints (client instance and file-system instance) are on Nitro instance types.** FSx for Lustre servers are Nitro-based for Persistent-1, Persistent-2, and Scratch-2. Scratch-1 is not.
2. **Same Region.**
3. **Same VPC or peered VPCs**, and the traffic **does not pass through a virtual network device or service such as a load balancer or a transit gateway.**

That last point is important: **traffic that traverses a Transit Gateway is not covered by Nitro in-flight encryption**. If your architecture has FSx in a "shared services" VPC and pods in a "workload" VPC attached to a TGW, you lose in-transit encryption between them. Use VPC peering (which is covered) or same-VPC placement instead.

### 10.2 Deployment-type support

From [Encrypting data in transit](https://docs.aws.amazon.com/fsx/latest/LustreGuide/encryption-in-transit-fsxl.html):

> Scratch 2 and persistent file systems can automatically encrypt data in transit when the file system is accessed from Amazon EC2 instances that support encryption in transit, and also for all communications between hosts within the file system.

Concretely:

| Deployment type | In-transit encryption between clients and servers |
|-----------------|--------------------------------------------------|
| Scratch-1       | **No** |
| Scratch-2       | **Yes**, if client is Nitro |
| Persistent-1    | **Yes**, if client is Nitro |
| Persistent-2 SSD | **Yes**, if client is Nitro |
| Persistent-2 Intelligent-Tiering | **Yes**, if client is Nitro |

There is no toggle — if the conditions are met, encryption is on; otherwise it's off. There is no way to *force* encryption at the file-system level and there is no client-visible way (e.g., via `mount` options) to verify it. If you need attestable encryption, restrict node pools by instance family (see the supported list below) and monitor node types via a policy engine.

### 10.3 The Nitro instance list

The set of families that support Nitro in-flight encryption, from the EC2 data-protection doc (as of 2026):

- **General purpose:** M5dn, M5n, M5zn, M6a, M6i, M6id, M6idn, M6in, M7a, M7g, M7gd, M7i, M7i-flex, M8a, M8azn, M8g, M8gb, M8gd, M8gn, M8i, M8id, M8i-flex, M8in, M8idn, M8ine, M8ib, M8idb, M9g, M9gd, Mac-m4, Mac-m4pro.
- **Compute optimized:** C5n, C6a, C6gn, C6i, C6id, C6in, C7a, C7g, C7gd, C7gn, C7i, C7i-flex, C8a, C8g, C8gb, C8gd, C8gn, C8i, C8id, C8i-flex, C8in, C8ine, C8ib, C9g, C9gd.
- **Memory optimized:** R5dn, R5n, R6a, R6i, R6id, R6idn, R6in, R7a, R7g, R7gd, R7i, R7iz, R8a, R8g, R8gb, R8gd, R8gn, R8i, R8id, R8i-flex, R8in, R8idn, R8ib, R8idb, U-*, U7*, X2idn, X2iedn, X2iezn, X8g, X8aedz, X8i.
- **Storage optimized:** D3, D3en, I3en, I4g, I4i, I7i, I7ie, I8g, I8ge, Im4gn, Is4gen.
- **Accelerated computing:** DL1, DL2q, F2, G4ad, G4dn, G5, G6, G6e, G6f, Gr6, Gr6f, G7, G7e, Inf1, Inf2, P3dn, P4d, P4de, P5, P5e, P5en, P6-B200, P6-B300, P6e-GB200, Trn1, Trn1n, Trn2, Trn2u, VT1.
- **HPC:** Hpc6a, Hpc6id, Hpc7a, Hpc7g, Hpc8a.

For inference clusters, the relevant subset is roughly **P5/P5e/P5en, P6-*, G6/G6e, Inf2, Trn2, plus M7i/C7i/R7i-class control nodes**. To pin a node pool to encryption-supporting families you can filter Karpenter with the boolean instance-type attribute:

```yaml
- key: karpenter.k8s.aws/instance-encryption-in-transit-supported
  operator: In
  values: ["true"]
```

To programmatically list them:

```bash
aws ec2 describe-instance-types \
  --filters Name=network-info.encryption-in-transit-supported,Values=true \
  --query 'InstanceTypes[*].InstanceType' \
  --output text | tr '\t' '\n' | sort -u
```

### 10.4 Verifying it is actually on

There is no per-connection flag on the client side. Best you can do:

- Confirm the FS's deployment type via `aws fsx describe-file-systems` (`LustreConfiguration.DeploymentType`).
- Confirm the client instance type supports in-flight encryption via the `describe-instance-types` filter above.
- Confirm both are in the same VPC (no TGW hop).

If your compliance regime requires attestation, wrap the above three checks in a Kyverno policy that denies pods scheduled onto non-Nitro nodes when they carry the `workload.example.com/fsx-mount: required-encrypted` annotation, and periodically snapshot the FS deployment type via Config.

## 11. Pod Identity vs IRSA for the CSI driver

### 11.1 The two ways to give the CSI driver AWS credentials

The `aws-fsx-csi-driver`'s controller pods call the FSx API to create/delete/tag/describe file systems (when you use dynamic provisioning) and to authorize the driver's own actions. There are two mechanisms:

- **IAM Roles for Service Accounts (IRSA)** — historical default. Uses an OIDC identity provider on the cluster and a service-account annotation. See [IAM roles for service accounts](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html). Works everywhere, including EKS Anywhere, Outposts, and Fargate.
- **EKS Pod Identity** — newer, simpler. Uses a per-cluster association (`aws eks create-pod-identity-association`) instead of OIDC. The EKS Pod Identity Agent DaemonSet handles credential delivery. See [EKS Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html). Not available on Fargate, Outposts, EKS Anywhere, or Windows nodes.

For a standard EKS + Linux Nitro node inference cluster in 2026, **Pod Identity is the recommended choice** — cleaner separation of concerns, no OIDC provider to manage, and the trust policy is reusable across clusters.

### 11.2 IRSA setup (still supported, upstream default)

The upstream install doc uses `eksctl`:

```bash
eksctl create iamserviceaccount \
  --name fsx-csi-controller-sa \
  --namespace kube-system \
  --cluster "$CLUSTER_NAME" \
  --attach-policy-arn arn:aws:iam::aws:policy/AmazonFSxFullAccess \
  --approve \
  --role-name AmazonEKSFSxLustreCSIDriverFullAccess \
  --region "$REGION"
```

This produces a role with a trust policy of the form:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {"Federated": "arn:aws:iam::111122223333:oidc-provider/oidc.eks.us-west-2.amazonaws.com/id/EXAMPLE"},
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "oidc.eks.us-west-2.amazonaws.com/id/EXAMPLE:sub": "system:serviceaccount:kube-system:fsx-csi-controller-sa",
          "oidc.eks.us-west-2.amazonaws.com/id/EXAMPLE:aud": "sts.amazonaws.com"
        }
      }
    }
  ]
}
```

And the SA is annotated:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: fsx-csi-controller-sa
  namespace: kube-system
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::111122223333:role/AmazonEKSFSxLustreCSIDriverFullAccess
```

### 11.3 Pod Identity setup (recommended)

Step 1 — install the Pod Identity Agent (once per cluster; skip if EKS Auto Mode):

```bash
aws eks create-addon \
  --cluster-name "$CLUSTER_NAME" \
  --addon-name eks-pod-identity-agent \
  --region "$REGION"
```

Step 2 — create the IAM role with a trust policy for the Pod Identity service principal:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {"Service": "pods.eks.amazonaws.com"},
      "Action": ["sts:AssumeRole", "sts:TagSession"]
    }
  ]
}
```

Step 3 — attach the policy from §11.4 and associate the role with the driver's SA:

```bash
aws eks create-pod-identity-association \
  --cluster-name "$CLUSTER_NAME" \
  --namespace kube-system \
  --service-account fsx-csi-controller-sa \
  --role-arn arn:aws:iam::111122223333:role/AmazonEKSFSxLustreCSIDriverRole \
  --region "$REGION"
```

Step 4 — install the CSI driver with the SA name and *no* IRSA annotations. If installing via Helm:

```yaml
# values.yaml
controller:
  serviceAccount:
    create: true
    name: fsx-csi-controller-sa
    annotations: {}   # no eks.amazonaws.com/role-arn
node:
  serviceAccount:
    create: true
    name: fsx-csi-node-sa
    annotations: {}
```

The node SA usually does not need AWS permissions — the node driver only performs local mounts. Some data-repository features do call FSx APIs from the node; if you use them, associate the node SA with a limited role too.

### 11.4 Least-privilege IAM policy for the CSI driver

`AmazonFSxFullAccess` is convenient but includes destructive verbs across the whole account. A scoped policy that supports dynamic provisioning, static provisioning, tagging, and the S3 SLR flow:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "FSxCoreLifecycle",
      "Effect": "Allow",
      "Action": [
        "fsx:CreateFileSystem",
        "fsx:DeleteFileSystem",
        "fsx:DescribeFileSystems",
        "fsx:DescribeDataRepositoryAssociations",
        "fsx:UpdateFileSystem",
        "fsx:TagResource",
        "fsx:UntagResource",
        "fsx:ListTagsForResource"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {"aws:RequestedRegion": "us-west-2"}
      }
    },
    {
      "Sid": "FSxServiceLinkedRole",
      "Effect": "Allow",
      "Action": "iam:CreateServiceLinkedRole",
      "Resource": "*",
      "Condition": {
        "StringEquals": {"iam:AWSServiceName": "fsx.amazonaws.com"}
      }
    },
    {
      "Sid": "FSxS3DataRepoServiceLinkedRole",
      "Effect": "Allow",
      "Action": [
        "iam:CreateServiceLinkedRole",
        "iam:AttachRolePolicy",
        "iam:PutRolePolicy"
      ],
      "Resource": "arn:aws:iam::*:role/aws-service-role/s3.data-source.lustre.fsx.amazonaws.com/*"
    },
    {
      "Sid": "S3DataRepoInspection",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": ["arn:aws:s3:::my-inference-datasets", "arn:aws:s3:::my-inference-datasets/*"]
    }
  ]
}
```

If you use a customer-managed CMK for encryption at rest, also grant the CSI driver role `kms:DescribeKey` on the CMK — nothing more; the SLR handles the actual grant creation via `kms:CreateGrant` (its policy already covers that).

For **static provisioning only** (you `CreateFileSystem` out-of-band and hand the FS-ID to Kubernetes), the CSI driver's controller does not need any AWS permissions — you can skip Pod Identity for it. Only the node driver runs, and it only executes `mount.lustre` locally.

## 12. A hardened end-to-end example

Below is a self-contained example of the Terraform/Helm/manifest fragments that stand up a production-grade FSx for Lustre attached to an EKS cluster, encrypted at rest with a CMK and in transit via Nitro, with least-privilege networking and Pod Identity for the CSI driver.

### 12.1 Terraform — KMS CMK

```hcl
resource "aws_kms_key" "fsx" {
  description             = "FSx for Lustre CMK - ${local.postfix}"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowRoot"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "AllowFSxService"
        Effect    = "Allow"
        Principal = { Service = "fsx.amazonaws.com" }
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey*",
          "kms:CreateGrant",
          "kms:DescribeKey",
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:CallerAccount" = data.aws_caller_identity.current.account_id
            "kms:ViaService"    = "fsx.${var.aws_region}.amazonaws.com"
          }
        }
      }
    ]
  })
  tags = merge(local.tags, { DeploymentId = local.postfix })
}

resource "aws_kms_alias" "fsx" {
  name          = "alias/fsx-inference-${local.postfix}"
  target_key_id = aws_kms_key.fsx.key_id
}
```

### 12.2 Terraform — security groups (non-EFA case shown)

```hcl
resource "aws_security_group" "fsx" {
  name        = "fsx-lustre-${local.postfix}"
  description = "FSx for Lustre - inference"
  vpc_id      = var.vpc_id
  tags        = merge(local.tags, { Name = "fsx-lustre-${local.postfix}" })
}

# self-referencing rules (server-to-server)
resource "aws_vpc_security_group_ingress_rule" "fsx_self_988" {
  security_group_id            = aws_security_group.fsx.id
  from_port                    = 988
  to_port                      = 988
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.fsx.id
}
resource "aws_vpc_security_group_ingress_rule" "fsx_self_1018" {
  security_group_id            = aws_security_group.fsx.id
  from_port                    = 1018
  to_port                      = 1023
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.fsx.id
}

# client-to-server (from EKS node SG)
resource "aws_vpc_security_group_ingress_rule" "fsx_from_nodes_988" {
  security_group_id            = aws_security_group.fsx.id
  from_port                    = 988
  to_port                      = 988
  ip_protocol                  = "tcp"
  referenced_security_group_id = var.eks_node_security_group_id
}
resource "aws_vpc_security_group_ingress_rule" "fsx_from_nodes_1018" {
  security_group_id            = aws_security_group.fsx.id
  from_port                    = 1018
  to_port                      = 1023
  ip_protocol                  = "tcp"
  referenced_security_group_id = var.eks_node_security_group_id
}

# reverse path on the node SG
resource "aws_vpc_security_group_ingress_rule" "nodes_from_fsx_988" {
  security_group_id            = var.eks_node_security_group_id
  from_port                    = 988
  to_port                      = 988
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.fsx.id
}
resource "aws_vpc_security_group_ingress_rule" "nodes_from_fsx_1018" {
  security_group_id            = var.eks_node_security_group_id
  from_port                    = 1018
  to_port                      = 1023
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.fsx.id
}

# egress: allow all (default) or scope to fsx and node SGs symmetrically
resource "aws_vpc_security_group_egress_rule" "fsx_egress_all" {
  security_group_id = aws_security_group.fsx.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"   # OK — egress from FSx SG has no attack surface
}
```

### 12.3 Terraform — the file system

```hcl
resource "aws_fsx_lustre_file_system" "inference" {
  storage_capacity            = 4800  # GiB - multiple of 2400 for Persistent-2 SSD
  subnet_ids                  = [var.fsx_subnet_id]     # single AZ subnet
  deployment_type             = "PERSISTENT_2"
  per_unit_storage_throughput = 500                    # MB/s per TiB
  security_group_ids          = [aws_security_group.fsx.id]
  kms_key_id                  = aws_kms_key.fsx.arn
  storage_type                = "SSD"

  # optional S3 data repo via aws_fsx_data_repository_association
  # (creates AWSServiceRoleForFSxS3Access_<fs-id>)

  automatic_backup_retention_days = 7
  daily_automatic_backup_start_time = "03:00"
  copy_tags_to_backups              = true

  tags = merge(local.tags, {
    Name          = "fsx-inference-${local.postfix}"
    DeploymentId  = local.postfix
  })
}

output "fsx_dns_name"    { value = aws_fsx_lustre_file_system.inference.dns_name }
output "fsx_mount_name"  { value = aws_fsx_lustre_file_system.inference.mount_name }
output "fsx_file_system_id" { value = aws_fsx_lustre_file_system.inference.id }
```

The `dns_name` is the `fs-xxxx.fsx.<region>.amazonaws.com` FQDN. The `mount_name` is the Lustre volume identifier (typically 8 random characters) that appears after the `:/` in the mount command.

### 12.4 Terraform — CSI driver IAM role for Pod Identity

```hcl
resource "aws_iam_role" "fsx_csi_driver" {
  name = "eks-${var.cluster_name}-fsx-csi-driver"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "pods.eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
  tags = local.tags
}

resource "aws_iam_policy" "fsx_csi_driver" {
  name = "eks-${var.cluster_name}-fsx-csi-driver"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "FSxCoreLifecycle"
        Effect = "Allow"
        Action = [
          "fsx:CreateFileSystem",
          "fsx:DeleteFileSystem",
          "fsx:DescribeFileSystems",
          "fsx:DescribeDataRepositoryAssociations",
          "fsx:UpdateFileSystem",
          "fsx:TagResource",
          "fsx:UntagResource",
          "fsx:ListTagsForResource",
        ]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
      },
      {
        Sid       = "FSxServiceLinkedRole"
        Effect    = "Allow"
        Action    = "iam:CreateServiceLinkedRole"
        Resource  = "*"
        Condition = { StringEquals = { "iam:AWSServiceName" = "fsx.amazonaws.com" } }
      },
      {
        Sid    = "FSxS3DataRepoSLR"
        Effect = "Allow"
        Action = [
          "iam:CreateServiceLinkedRole",
          "iam:AttachRolePolicy",
          "iam:PutRolePolicy",
        ]
        Resource = "arn:aws:iam::*:role/aws-service-role/s3.data-source.lustre.fsx.amazonaws.com/*"
      },
      {
        Sid      = "KMSDescribe"
        Effect   = "Allow"
        Action   = ["kms:DescribeKey"]
        Resource = aws_kms_key.fsx.arn
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "fsx_csi_driver" {
  role       = aws_iam_role.fsx_csi_driver.name
  policy_arn = aws_iam_policy.fsx_csi_driver.arn
}

resource "aws_eks_pod_identity_association" "fsx_csi_driver" {
  cluster_name    = var.cluster_name
  namespace       = "kube-system"
  service_account = "fsx-csi-controller-sa"
  role_arn        = aws_iam_role.fsx_csi_driver.arn
}
```

### 12.5 Helm — install the CSI driver

```bash
helm repo add aws-fsx-csi-driver https://kubernetes-sigs.github.io/aws-fsx-csi-driver/
helm upgrade --install aws-fsx-csi-driver aws-fsx-csi-driver/aws-fsx-csi-driver \
  --namespace kube-system \
  --set controller.serviceAccount.name=fsx-csi-controller-sa \
  --set controller.serviceAccount.create=true \
  --set 'controller.serviceAccount.annotations=null' \
  --set node.serviceAccount.name=fsx-csi-node-sa \
  --set node.serviceAccount.create=true
```

### 12.6 Kubernetes — StorageClass and PVC

Dynamic provisioning:

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: fsx-lustre-p2
provisioner: fsx.csi.aws.com
parameters:
  subnetId: subnet-0abc...        # SAME AZ as node pool
  securityGroupIds: sg-0abc...    # FSx SG created above
  deploymentType: PERSISTENT_2
  perUnitStorageThroughput: "500"
  storageType: SSD
  kmsKeyId: arn:aws:kms:us-west-2:111122223333:key/... # CMK
  automaticBackupRetentionDays: "7"
  copyTagsToBackups: "true"
  extraTags: "Environment=prod,Workload=inference"
mountOptions:
  - flock
  - noatime
reclaimPolicy: Retain
volumeBindingMode: Immediate
allowVolumeExpansion: true
```

Static provisioning (recommended when the FS is Terraform-managed):

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: fsx-inference-pv
spec:
  capacity:
    storage: 4800Gi
  volumeMode: Filesystem
  accessModes: [ReadWriteMany]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  csi:
    driver: fsx.csi.aws.com
    volumeHandle: fs-0123456789abcdef0
    volumeAttributes:
      dnsname: fs-0123456789abcdef0.fsx.us-west-2.amazonaws.com
      mountname: mountname12
  mountOptions:
    - flock
    - noatime
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: fsx-inference-pvc
  namespace: inference
spec:
  accessModes: [ReadWriteMany]
  storageClassName: ""
  resources:
    requests:
      storage: 4800Gi
  volumeName: fsx-inference-pv
```

### 12.7 Kubernetes — pod pinning

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: llm-server
  namespace: inference
spec:
  replicas: 4
  selector: { matchLabels: { app: llm-server } }
  template:
    metadata:
      labels:
        app: llm-server
        workload.example.com/fsx-client: "true"   # picked up by SGP
    spec:
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
              - matchExpressions:
                  - key: topology.kubernetes.io/zone
                    operator: In
                    values: ["us-west-2a"]
                  - key: karpenter.k8s.aws/instance-encryption-in-transit-supported
                    operator: In
                    values: ["true"]
      containers:
        - name: server
          image: your-registry/llm-server:1.0.0
          volumeMounts:
            - name: weights
              mountPath: /models
      volumes:
        - name: weights
          persistentVolumeClaim:
            claimName: fsx-inference-pvc
```

## 13. Operational gotchas

- **`iam:CreateServiceLinkedRole` needed once.** The first `CreateFileSystem` call in an account calls this. If your Pod Identity role does not have it, the very first PVC create will fail with `AccessDenied` on `iam:CreateServiceLinkedRole` even though every subsequent call would work. Include it in the driver policy.
- **Tag every resource with `DeploymentId`.** Terraform's `random_id.postfix` (already used in this repo's templates) makes FSx cleanups tractable via the Resource Groups Tagging API. FSx also injects `AmazonFSx.FileSystemId` tags on the ENIs it creates — do not remove those; the SLR uses them for tag-scoped writes.
- **The FSx ENIs are not yours.** From the AWS docs:

  > You must not modify or delete the Amazon FSx elastic network interface. Modifying or deleting the network interface can cause a permanent loss of connection between your VPC and your file system.

  Automation that scans and closes "unused" ENIs (Config auto-remediation, custodian rules) must exclude ENIs tagged `AmazonFSx.FileSystemId`.
- **VPC endpoints and DNS collisions.** If you have both an FSx interface endpoint with private DNS and a Route 53 private hosted zone that also covers `*.amazonaws.com`, resolution races can create silent black holes. Prefer one authoritative resolution path.
- **Cross-account CMKs.** If the CMK is in a different account than the FSx, and you use the CSI driver to `CreateFileSystem` dynamically, the driver's role needs `kms:DescribeKey` and the CMK's policy needs to allow the SLR (`AWSServiceRoleForAmazonFSx`) in the FSx account. Otherwise the FS reaches `FAILED` after `CREATING` with a KMS error only visible in `describe-file-systems`.
- **PVC deletion and orphaned file systems.** With `reclaimPolicy: Retain`, deleting a PVC does not delete the FS. Combined with dynamic provisioning, that is easy to forget — run periodic tag-based drift scans.
- **Backups.** FSx backups (Persistent only) are separately KMS-encrypted with the same CMK. Retention policy is set at FS-creation time and hard to change; pick 7–35 days deliberately.

## 14. Compliance and audit notes

- FSx for Lustre is in scope for **HIPAA, PCI-DSS, SOC 1/2/3, ISO 27001/27017/27018, and IRAP** (see the [AWS Compliance Programs](https://aws.amazon.com/compliance/services-in-scope/) list). It is available in `GovCloud (US-East/West)` for FedRAMP-High and DoD SRG IL5 workloads (Persistent-2 SSD only, per the region table above).
- CloudTrail captures every `fsx:*` API call. Turn on a Region-level trail with S3 bucket delivery, and consider a CloudTrail Lake to power ad-hoc queries.
- FSx does *not* natively emit data-plane access logs (no equivalent to S3 access logging). If you need per-file audit, that has to live at the application layer, in the kernel audit subsystem on the client, or via `lctl` on the Lustre client — none are great.
- KMS operations against the CMK show up in CloudTrail with `kms:ViaService = fsx.<region>.amazonaws.com` — a durable signal for detecting decrypts that did *not* go through FSx (e.g., an attacker directly issuing `kms:Decrypt` on data-encryption-key blobs, though that would already require them to have exfiltrated the blobs).

## 15. Selected AWS documentation index

- [Amazon FSx for Lustre — user guide root](https://docs.aws.amazon.com/fsx/latest/LustreGuide/what-is.html)
- [File system access control with Amazon VPC (security groups)](https://docs.aws.amazon.com/fsx/latest/LustreGuide/limit-access-security-groups.html)
- [Deployment and storage class options](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html)
- [Encrypting data at rest](https://docs.aws.amazon.com/fsx/latest/LustreGuide/encryption-at-rest.html)
- [Encrypting data in transit](https://docs.aws.amazon.com/fsx/latest/LustreGuide/encryption-in-transit-fsxl.html)
- [Amazon EC2 Data protection — Encryption in transit (Nitro list)](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/data-protection.html#encryption-transit)
- [Using service-linked roles for Amazon FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-service-linked-roles.html)
- [AWS managed policies for Amazon FSx for Lustre](https://docs.aws.amazon.com/fsx/latest/LustreGuide/security-iam-awsmanpol.html)
- [FSx and Interface VPC Endpoints (PrivateLink)](https://docs.aws.amazon.com/fsx/latest/LustreGuide/fsx-vpc-endpoints.html)
- [Mounting from an EC2 instance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/mounting-ec2-instance.html)
- [`CreateFileSystem` API](https://docs.aws.amazon.com/fsx/latest/APIReference/API_CreateFileSystem.html)
- [EKS Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html)
- [EKS IAM roles for service accounts](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html)
- [EKS: Deploy the FSx for Lustre CSI driver](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi.html)
- [`kubernetes-sigs/aws-fsx-csi-driver` on GitHub](https://github.com/kubernetes-sigs/aws-fsx-csi-driver)
- [`aws-fsx-csi-driver` install docs](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/blob/master/docs/install.md)
- [Amazon VPC DNS attributes](https://docs.aws.amazon.com/vpc/latest/userguide/vpc-dns.html#vpc-dns-updating)
- [EFA setup: prepare an EFA-enabled security group](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa-start.html#efa-start-security)
- [Security groups for pods](https://docs.aws.amazon.com/eks/latest/userguide/security-groups-for-pods.html)
