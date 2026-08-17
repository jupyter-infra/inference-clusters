# FSx for Lustre CSI driver on EKS — installation, PV/PVC/StorageClass, static vs dynamic

## TL;DR

- The [aws-fsx-csi-driver](https://github.com/kubernetes-sigs/aws-fsx-csi-driver) is a CSI plugin (`provisioner: fsx.csi.aws.com`) that lets EKS Pods mount and, optionally, provision Amazon FSx for Lustre file systems. It is packaged as a Helm chart, a Kustomize overlay, and — since 2024 — an official [EKS managed add-on](https://docs.aws.amazon.com/eks/latest/userguide/workloads-add-ons-available-eks.html#add-ons-aws-fsx-csi-driver) named `aws-fsx-csi-driver`.
- The controller Deployment needs AWS credentials to call `fsx:*`, `iam:CreateServiceLinkedRole`, and (for S3 Data Repository Association) a bit of S3. AWS recommends attaching the managed policy [`AmazonFSxFullAccess`](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonFSxFullAccess.html) to the `fsx-csi-controller-sa` service account via **IRSA** or the newer **EKS Pod Identity** flow.
- **Static provisioning** wires a pre-existing file system to a `PersistentVolume` whose `csi.volumeHandle` is the `fs-xxxxxxxxxxxxxxxxx` FSx ID and whose `volumeAttributes` supply `dnsname` and `mountname` (obtained from `aws fsx describe-file-systems`). Reclaim policy is your problem — you own the file system's lifecycle.
- **Dynamic provisioning** uses a `StorageClass` whose `parameters` block passes through to the FSx `CreateFileSystem` API (`subnetId`, `securityGroupIds`, `deploymentType`, `perUnitStorageThroughput`, `storageType`, `driveCacheType`, `dataCompressionType`, `fileSystemTypeVersion`, S3 DRA settings, `kmsKeyId`, `weeklyMaintenanceStartTime`, `extraTags`, etc.). Deleting the PVC with the default `Delete` reclaim policy **deletes the underlying FSx file system**.
- FSx for Lustre is a single-AZ file system (except Persistent-2 Intelligent-Tiering, which is multi-AZ). One `StorageClass` maps to exactly one subnet — plan pod scheduling accordingly. All well-behaved workloads should mount with `flock` because many trainers, `logging`, `sqlite`, `torch.distributed`, etc. call `flock(2)`.
- ReadWriteMany is fully supported and is the primary reason to use FSx for Lustre for shared training data, checkpoints, and dataset caches on multi-node inference/training clusters.

---

## 1. What this driver is (and isn't)

The [FSx for Lustre CSI driver](https://github.com/kubernetes-sigs/aws-fsx-csi-driver) implements the [Container Storage Interface (CSI)](https://kubernetes-csi.github.io/docs/) for Amazon FSx for Lustre. It is an upstream `kubernetes-sigs` project, dual-owned by AWS and the Kubernetes storage community, and it is what the EKS-managed `aws-fsx-csi-driver` add-on packages under the covers.

At runtime the driver ships two workloads in the `kube-system` namespace:

- A **controller `Deployment`** (`fsx-csi-controller`) that runs the CSI Controller service. It talks to the FSx API to create/delete/expand file systems and to describe them for volume attachment.
- A **node `DaemonSet`** (`fsx-csi-node`) that runs on every node, hosting the CSI Node service. It is responsible for calling `mount -t lustre` on the node when a Pod needs the file system.

Feature capabilities exposed by the CSI plugin ([source](https://github.com/kubernetes-sigs/aws-fsx-csi-driver#csi-interface)):

| CSI service | Implemented RPCs |
| --- | --- |
| Identity | `GetPluginInfo`, `GetPluginCapabilities`, `Probe` |
| Controller | `CreateVolume`, `DeleteVolume`, `ControllerExpandVolume`, `ControllerGetCapabilities`, `ValidateVolumeCapabilities` |
| Node | `NodePublishVolume`, `NodeUnpublishVolume`, `NodeGetCapabilities`, `NodeGetInfo`, `NodeGetId` |

Some things the driver does **not** do:

- It does **not** support snapshots (`CreateSnapshot`/`DeleteSnapshot` are unimplemented). FSx has native backups, driven by `automaticBackupRetentionDays` / `dailyAutomaticBackupStartTime` — but there is no `VolumeSnapshot` API here.
- It does **not** support `ReadWriteOncePod` semantics — FSx is inherently a multi-writer POSIX file system. Access modes should be `ReadWriteMany` for shared workloads (or `ReadWriteOnce` if you want K8s to enforce single-consumer scheduling).
- It is **not supported on AWS Fargate** ([EKS user guide](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi-create.html)). Fargate pods cannot bind-mount Lustre (no privileged host mount available).

### 1.1 Versions and compatibility

At the time of writing the current upstream driver release is **v1.9.0**, published to `public.ecr.aws/fsx-csi-driver/aws-fsx-csi-driver:v1.9.0`. It requires Kubernetes v1.20+ ([install docs](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/blob/master/docs/install.md)). The EKS add-on tracks upstream with an `-eksbuildN` suffix (e.g. `v1.9.0-eksbuild.1`) — enumerate available versions with:

```bash
aws eks describe-addon-versions --addon-name aws-fsx-csi-driver \
  --query 'addons[].addonVersions[].{Version:addonVersion,K8s:compatibilities[].clusterVersion}'
```

---

## 2. Installing the driver

There are three viable installation paths. In order of AWS's own recommendation, they are: the EKS add-on, Helm, and Kustomize.

### 2.1 EKS managed add-on (recommended)

The [EKS add-on entry](https://docs.aws.amazon.com/eks/latest/userguide/workloads-add-ons-available-eks.html#add-ons-aws-fsx-csi-driver) is named `aws-fsx-csi-driver`. It supports both **IRSA** and **EKS Pod Identity** for controller auth. AWS docs are explicit that the add-on installation will fail if you already have a self-managed installation on the cluster; use `--resolve-conflicts OVERWRITE` in that case.

```bash
# Prereq: IAM role that trusts the controller SA. See section 3.
export CLUSTER=my-eks-cluster
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

aws eks create-addon \
  --cluster-name "$CLUSTER" \
  --addon-name aws-fsx-csi-driver \
  --service-account-role-arn "arn:aws:iam::$ACCOUNT_ID:role/AmazonEKS_FSx_CSI_DriverRole" \
  --resolve-conflicts OVERWRITE
```

Verify:

```bash
aws eks describe-addon --cluster-name "$CLUSTER" --addon-name aws-fsx-csi-driver
kubectl get pods -n kube-system -l app.kubernetes.io/name=aws-fsx-csi-driver
```

You should see two controller replicas plus one node pod per Linux worker.

### 2.2 Helm chart (self-managed)

Chart repo: `https://kubernetes-sigs.github.io/aws-fsx-csi-driver`. The [chart's `values.yaml`](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/blob/master/charts/aws-fsx-csi-driver/values.yaml) is the source of truth for parameters.

```bash
helm repo add aws-fsx-csi-driver https://kubernetes-sigs.github.io/aws-fsx-csi-driver
helm repo update

helm upgrade --install aws-fsx-csi-driver aws-fsx-csi-driver/aws-fsx-csi-driver \
  --namespace kube-system \
  --set controller.serviceAccount.annotations."eks\.amazonaws\.com/role-arn"=arn:aws:iam::123456789012:role/AmazonEKS_FSx_CSI_DriverRole \
  --set controller.region=us-west-2
```

Selected default chart values worth being aware of:

```yaml
image:
  repository: public.ecr.aws/fsx-csi-driver/aws-fsx-csi-driver
  tag: "v1.9.0"

controller:
  replicaCount: 2
  serviceAccount:
    create: true
    name: fsx-csi-controller-sa
    annotations: {}         # <- IRSA role ARN goes here
  tolerations:
    - key: CriticalAddonsOnly
      operator: Exists
    - effect: NoExecute
      operator: Exists
      tolerationSeconds: 300
  podDisruptionBudget:
    enabled: true

node:
  serviceAccount:
    create: true
    name: fsx-csi-node-sa
  kubeletPath: /var/lib/kubelet    # override if using Bottlerocket / custom kubelet root
  tolerateAllTaints: true
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
          - matchExpressions:
              - key: eks.amazonaws.com/compute-type
                operator: NotIn
                values: [fargate]

csidriver:
  fsGroupPolicy: ReadWriteOnceWithFSType
```

Notes:

- `controller.region` is only required on EKS Auto Mode — the driver otherwise uses the [IMDS-provided region](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/blob/master/pkg/cloud/cloud.go).
- `csidriver.fsGroupPolicy: ReadWriteOnceWithFSType` means the kubelet will `chown -R` a volume to `spec.securityContext.fsGroup` **only for `ReadWriteOncePod`/`ReadWriteOnce` volumes**. For `ReadWriteMany` (the common FSx case) you should manage POSIX ownership yourself.
- Set `controller.serviceAccount.annotations."eks.amazonaws.com/role-arn"` for IRSA. Leave it empty if you are using Pod Identity.
- On Bottlerocket you must set `node.kubeletPath=/var/lib/containerd/kubelet` (or whatever your OS uses); otherwise `mount --bind` in the driver's Node service fails silently.

### 2.3 Kustomize

For clusters that pin manifests in git:

```bash
kubectl apply -k "github.com/kubernetes-sigs/aws-fsx-csi-driver/deploy/kubernetes/overlays/stable/?ref=release-1.9"
```

Then annotate the SA:

```bash
kubectl annotate serviceaccount -n kube-system fsx-csi-controller-sa \
  eks.amazonaws.com/role-arn=arn:aws:iam::123456789012:role/AmazonEKS_FSx_CSI_DriverRole
```

---

## 3. IAM: IRSA vs Pod Identity

The controller SA needs to call the FSx API, create the FSx service-linked role, and (for Data Repository Associations) list S3 buckets. AWS provides an [`AmazonFSxFullAccess`](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonFSxFullAccess.html) managed policy that satisfies all of this; you can tighten it later.

### 3.1 IRSA (IAM Roles for Service Accounts)

Prereq: your cluster has an [OIDC provider configured](https://docs.aws.amazon.com/eks/latest/userguide/enable-iam-roles-for-service-accounts.html). Then:

```bash
eksctl create iamserviceaccount \
  --name fsx-csi-controller-sa \
  --namespace kube-system \
  --cluster my-eks-cluster \
  --role-name AmazonEKS_FSx_CSI_DriverRole \
  --role-only \
  --attach-policy-arn arn:aws:iam::aws:policy/AmazonFSxFullAccess \
  --approve
```

The `--role-only` flag tells eksctl to create the IAM role and its trust policy but not the `ServiceAccount` — the EKS add-on installer will bind the same SA name back to that role via `--service-account-role-arn` (see § 2.1). If you are installing via Helm, drop `--role-only`.

### 3.2 EKS Pod Identity (preferred on new clusters)

[Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html) replaces OIDC-signed service-account tokens with an [`eks-pod-identity-agent`](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html) daemonset that vends credentials over IMDS. There is no per-cluster OIDC provider to keep in sync and no trust policy to hand-craft — you associate a role with a namespaced SA using a single API call:

```bash
aws eks create-pod-identity-association \
  --cluster-name my-eks-cluster \
  --namespace kube-system \
  --service-account fsx-csi-controller-sa \
  --role-arn arn:aws:iam::123456789012:role/AmazonEKS_FSx_CSI_DriverRole
```

The role's trust policy must allow `pods.eks.amazonaws.com` to assume it:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "pods.eks.amazonaws.com" },
    "Action": ["sts:AssumeRole", "sts:TagSession"]
  }]
}
```

The AWS docs for the [FSx CSI Driver EKS add-on](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi-create.html) call this out: *"The Amazon FSx CSI Driver EKS add-on supports authentication through either EKS Pod Identity or IAM Roles for Service Accounts (IRSA)."*

### 3.3 Minimal IAM policy (if `AmazonFSxFullAccess` is too broad)

The driver's install doc includes a scoped example. Reproduced verbatim from [install.md](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/blob/master/docs/install.md):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "iam:CreateServiceLinkedRole",
        "iam:AttachRolePolicy",
        "iam:PutRolePolicy"
      ],
      "Resource": "arn:aws:iam::*:role/aws-service-role/s3.data-source.lustre.fsx.amazonaws.com/*"
    },
    {
      "Action": "iam:CreateServiceLinkedRole",
      "Effect": "Allow",
      "Resource": "*",
      "Condition": {
        "StringLike": {
          "iam:AWSServiceName": ["fsx.amazonaws.com"]
        }
      }
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:ListBucket",
        "fsx:CreateFileSystem",
        "fsx:DeleteFileSystem",
        "fsx:DescribeFileSystems",
        "fsx:TagResource",
        "fsx:UpdateFileSystem"
      ],
      "Resource": ["*"]
    }
  ]
}
```

Notes on the two `iam:CreateServiceLinkedRole` statements:

- The first (with the `s3.data-source.lustre.fsx.amazonaws.com` resource pattern) is required so that FSx can create the service-linked role it uses when you attach a **Data Repository Association** to an S3 bucket. Without it, the very first S3-import-enabled `StorageClass` create will fail with `AccessDenied` on `iam:CreateServiceLinkedRole`.
- The second (guarded by `iam:AWSServiceName=fsx.amazonaws.com`) is required so that FSx can create its top-level SLR at all in an account that has never used FSx before.

For read-only workloads that only need to mount a pre-existing file system (static provisioning), the CSI controller strictly only needs `fsx:DescribeFileSystems` — everything else is provisioner-related.

### 3.4 Verifying the controller has creds

```bash
kubectl -n kube-system exec -it deploy/fsx-csi-controller -c fsx-plugin -- \
  sh -c 'echo $AWS_ROLE_ARN; env | grep AWS_ | sort'
```

For IRSA you should see `AWS_ROLE_ARN` and `AWS_WEB_IDENTITY_TOKEN_FILE` set. For Pod Identity you'll instead see `AWS_CONTAINER_CREDENTIALS_FULL_URI` and `AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE` set by the pod-identity webhook.

---

## 4. FSx for Lustre 101 (the parts that shape the K8s wiring)

Before writing a `StorageClass`, know these constraints — they are enforced by the FSx `CreateFileSystem` API and will surface as `CreateVolume` errors, not as validation errors on the CR.

### 4.1 Deployment types

FSx for Lustre supports four deployment types ([FSx User Guide](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html)):

| deploymentType | Persistence | Redundancy | Common use |
| --- | --- | --- | --- |
| `SCRATCH_1` | Ephemeral | None | Short-lived batch jobs; 1.2, 2.4, 3.6 TiB, then +3.6 TiB steps |
| `SCRATCH_2` | Ephemeral | Encryption in transit; better throughput | Batch/burst workloads; 1.2, 2.4 TiB, then +2.4 TiB steps |
| `PERSISTENT_1` | Persistent | Intra-AZ replication of disks | Long-running workloads; SSD or HDD; 50/100/200 MB/s/TiB throughput classes |
| `PERSISTENT_2` | Persistent | Intra-AZ, EFA-optional, metadata IOPS provisioned | Recommended default for new persistent clusters; 125/250/500/1000 MB/s/TiB |

**Persistent-2 with Intelligent-Tiering** is the only multi-AZ variant. Every other type provisions the file system into **one Availability Zone** — which is why `StorageClass.parameters.subnetId` is a scalar, not a list ([README](https://github.com/kubernetes-sigs/aws-fsx-csi-driver#storageclass-parameters)):

> *"For dynamically provisioned volumes, only one subnet is allowed inside a storageclass's parameters.subnetId. This is a limitation enforced by FSx for Lustre."*

### 4.2 Storage capacity rounding

The FSx API rounds `spec.resources.requests.storage` up to a legal size:

- **SSD**: 1.2 TiB, 2.4 TiB, or multiples of 3.6 TiB (for SCRATCH_1); 1.2 TiB, 2.4 TiB, or multiples of 2.4 TiB (for SCRATCH_2, PERSISTENT_1 SSD, PERSISTENT_2).
- **HDD (`storageType: HDD`, PERSISTENT_1 only)**:
  - `perUnitStorageThroughput: 12` → multiples of 6.0 TiB.
  - `perUnitStorageThroughput: 40` → multiples of 1.8 TiB.

You will pay for the rounded-up size. Ask for `storage: 1200Gi` on an SSD SC and you'll get exactly 1.2 TiB.

### 4.3 Multi-AZ scheduling

Because one FSx file system lives in one AZ, Pods that mount it must land in that AZ. There are two common patterns:

1. **Pin the SC to a subnet, and let Karpenter/CA respect the topology** by ensuring the node pools that run FSx-consuming workloads have subnet selectors that overlap the FSx subnet. On self-managed nodes, add a node selector on the Pod like `topology.kubernetes.io/zone: us-west-2a`.
2. **One StorageClass per AZ** if you want dynamic provisioning to still work across zones, at the cost of manually picking the SC. Pods in `us-west-2b` reference `fsx-sc-usw2b`, pods in `us-west-2c` reference `fsx-sc-usw2c`. This is uglier but avoids stranding nodes.

Note the FSx CSI driver **does not** currently emit CSI Topology hints (it does not implement the `NodeGetInfo` topology reply that the EBS driver uses). The K8s scheduler cannot know a priori that a given FSx PV is only usable in one zone. Static PVs can carry a `nodeAffinity` clause to make this explicit — see § 6.5.

### 4.4 Security groups

FSx for Lustre requires clients to reach the file servers on TCP 988 and 1018–1023. Your Lustre security group must ingress those ports from your worker node SG. See [AWS's SG guidance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/limit-access-security-groups.html#fsx-vpc-security-groups). The EKS CSI-create guide explicitly walks you through this ([step 2 of the fsx-csi-create doc](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi-create.html#fsx-csi-deploy-storage-class)):

> *"For the security groups associated with your Lustre clients, use your cluster security group. You can leave the outbound rules alone to allow All traffic."*

---

## 5. Dynamic provisioning

### 5.1 The `StorageClass`

`provisioner: fsx.csi.aws.com` and a bag of `parameters` that map 1:1 to `CreateFileSystem` args. The most-complete parameter list is in the dynamic-provisioning example [README](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/tree/master/examples/kubernetes/dynamic_provisioning):

| Parameter | Required? | Values | Notes |
| --- | --- | --- | --- |
| `subnetId` | Yes | `subnet-xxxx` | Scalar. Determines AZ. |
| `securityGroupIds` | Yes | Comma-sep | Applied to file system's ENIs. |
| `deploymentType` | No, default `SCRATCH_1` | `SCRATCH_1`, `SCRATCH_2`, `PERSISTENT_1`, `PERSISTENT_2` | |
| `perUnitStorageThroughput` | For `PERSISTENT_*` | Quoted string, e.g. `"200"` | MB/s/TiB — valid values depend on class (see § 4.1). |
| `storageType` | Optional (`PERSISTENT_1` only), default `SSD` | `SSD`, `HDD` | |
| `driveCacheType` | Required if `storageType: HDD` | `NONE`, `READ` | SSD read cache for HDD tier. |
| `kmsKeyId` | Optional (`PERSISTENT_*` only) | KMS key ARN | Enables encryption at rest with a CMK. |
| `dataCompressionType` | Optional, default `NONE` | `NONE`, `LZ4` | LZ4 compression is transparent, saves ~30–50% on typical corpora. |
| `fileSystemTypeVersion` | Optional, default `"2.10"` | `"2.10"`, `"2.12"`, `"2.15"` | Passthrough to `FileSystemTypeVersion`. |
| `weeklyMaintenanceStartTime` | Optional, default `"7:09:00"` | `d:HH:MM` UTC | Weekday 1–7 = Mon–Sun. |
| `automaticBackupRetentionDays` | Optional | `"0"`–`"35"` | Enable automatic backups for `PERSISTENT_*`. `"0"` disables. |
| `dailyAutomaticBackupStartTime` | Optional | `HH:MM` UTC | Requires `automaticBackupRetentionDays > 0`. |
| `copyTagsToBackups` | Optional, default `"false"` | Boolean string | |
| `extraTags` | Optional | `Tag1=Value1,Tag2=Value2` | Applied to the FSx resource in AWS. |
| `metadataConfigurationMode` | Optional (`PERSISTENT_2` only) | `AUTOMATIC`, `USER_PROVISIONED` | |
| `metadataIops` | Required if `metadataConfigurationMode: USER_PROVISIONED` | Integer | Metadata IOPS to provision. |
| `efaEnabled` | Optional (`PERSISTENT_2` only) | `"true"` / `"false"` | Enable EFA on the file system's ENIs; needed for the very-high-throughput classes. |
| `s3ImportPath` | Optional | `s3://bucket[/prefix]` | Legacy DRA — see § 5.5. |
| `s3ExportPath` | Optional | `s3://bucket[/prefix]` | Must share the bucket with `s3ImportPath`. |
| `autoImportPolicy` | Optional | `NONE`, `NEW`, `NEW_CHANGED`, `NEW_CHANGED_DELETED` | Legacy DRA passthrough. |

Every value is a **string**; YAML parsers will otherwise coerce `"200"` to `200`, which the CSI driver rejects. This is why the upstream example is careful to quote everything.

### 5.2 Full example

The canonical StorageClass from the [upstream dynamic-provisioning example](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/blob/master/examples/kubernetes/dynamic_provisioning/specs/storageclass.yaml):

```yaml
kind: StorageClass
apiVersion: storage.k8s.io/v1
metadata:
  name: fsx-sc
provisioner: fsx.csi.aws.com
parameters:
  subnetId: subnet-0eabfaa81fb22bcaf
  securityGroupIds: sg-068000ccf82dfba88
  deploymentType: PERSISTENT_1
  automaticBackupRetentionDays: "1"
  dailyAutomaticBackupStartTime: "00:00"
  copyTagsToBackups: "true"
  perUnitStorageThroughput: "200"
  dataCompressionType: "NONE"
  weeklyMaintenanceStartTime: "7:09:00"
  fileSystemTypeVersion: "2.12"
  extraTags: "Tag1=Value1,Tag2=Value2"
mountOptions:
  - flock
```

A PERSISTENT_2 variant with LZ4 compression and a KMS CMK for inference training data:

```yaml
kind: StorageClass
apiVersion: storage.k8s.io/v1
metadata:
  name: fsx-persistent2-ssd
provisioner: fsx.csi.aws.com
reclaimPolicy: Retain
volumeBindingMode: Immediate
parameters:
  subnetId: subnet-0eabfaa81fb22bcaf
  securityGroupIds: sg-068000ccf82dfba88
  deploymentType: PERSISTENT_2
  perUnitStorageThroughput: "500"
  dataCompressionType: LZ4
  kmsKeyId: arn:aws:kms:us-west-2:123456789012:key/abcd1234-...
  fileSystemTypeVersion: "2.15"
  metadataConfigurationMode: AUTOMATIC
  weeklyMaintenanceStartTime: "7:09:00"
  extraTags: "Team=ml-platform,Env=prod"
mountOptions:
  - flock
```

Field notes:

- `reclaimPolicy: Retain` — the SC's default is `Delete`. On a persistent training-data volume you almost certainly want `Retain` so that an accidental `kubectl delete pvc` doesn't nuke the file system.
- `volumeBindingMode: Immediate` is the default. There is no benefit to `WaitForFirstConsumer` for FSx: the file system's AZ is already fixed by `subnetId` in the SC, so late binding wouldn't buy you any topology awareness.
- `mountOptions: [flock]` — this becomes `-o flock` on the `mount -t lustre` call. Many workloads use POSIX `flock(2)`; without this the mount is `localflock` and cross-node locks silently no-op, which manifests as corrupt SQLite databases, stuck training loops, and duplicated `AutoModel.from_pretrained()` downloads.

### 5.3 The PVC

Nothing FSx-specific — it's a normal PVC that references the SC:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: fsx-claim
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: fsx-persistent2-ssd
  resources:
    requests:
      storage: 4800Gi
```

`ReadWriteMany` is the whole point — Lustre is a shared file system. If you accidentally use `ReadWriteOnce`, Kubernetes will prevent more than one node from binding it and you'll be very confused about "why can't my two-node training job share a checkpoint dir." The FSx CSI driver reports `MULTI_NODE_MULTI_WRITER` in `ControllerGetCapabilities`.

Provisioning takes 5–15 minutes. During this window `kubectl describe pvc fsx-claim` shows `Status: Pending` with `ExternalProvisioning`/`Provisioning` events. AWS's own doc notes: *"The Status may show as Pending for 5-10 minutes, before changing to Bound. Don't continue with the next step until the Status is Bound. If the Status shows Pending for more than 10 minutes, use warning messages in the Events as reference for addressing any problems."* ([source](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi-create.html)).

### 5.4 The consumer Pod

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: fsx-app
spec:
  containers:
    - name: app
      image: amazonlinux:2
      command: ["/bin/sh"]
      args: ["-c", "while true; do echo $(date -u) >> /data/out.txt; sleep 5; done"]
      volumeMounts:
        - name: persistent-storage
          mountPath: /data
  volumes:
    - name: persistent-storage
      persistentVolumeClaim:
        claimName: fsx-claim
```

The container image does not need the Lustre client installed — the CSI Node service does the mount on the host and bind-mounts it into the container's namespace. What you cannot do is `mount -t lustre` **inside** the container.

Verify:

```bash
kubectl exec -ti fsx-app -- df -h /data
# Filesystem                   Size  Used Avail Use% Mounted on
# 192.0.2.0@tcp:/abcdef01      1.1T  7.8M  1.1T   1% /data
```

The `NNN.NNN.NNN.NNN@tcp:/mountname` device string is Lustre's LNet convention — the MDS's LNet address and the mount subdirectory.

### 5.5 S3 Data Repository Association (legacy `s3ImportPath` mode)

The CSI driver supports the pre-DRA style S3 integration where you set `s3ImportPath` and (optionally) `s3ExportPath` on the StorageClass, and FSx creates a linked repository at file-system-create time:

```yaml
kind: StorageClass
apiVersion: storage.k8s.io/v1
metadata:
  name: fsx-sc
provisioner: fsx.csi.aws.com
parameters:
  subnetId: subnet-0d7b5e117ad7b4961
  securityGroupIds: sg-05a37bfe01467059a
  s3ImportPath: s3://ml-training-data-000
  s3ExportPath: s3://ml-training-data-000/export
  deploymentType: SCRATCH_2
mountOptions:
  - flock
```

Caveats spelled out in the [S3 example README](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/tree/master/examples/kubernetes/dynamic_provisioning_s3):

- The bucket in `s3ImportPath` and `s3ExportPath` must be the same.
- You can't set `s3ExportPath` or `autoImportPolicy` without also setting `s3ImportPath`.
- **New files are not synced back automatically.** You have to `lfs hsm_archive /path/to/file` from a container that (a) has the Lustre client installed and (b) runs `privileged: true` with `CAP_SYS_ADMIN`. In practice this means baking `lustre-client` into your job image.
- `autoImportPolicy` values: `NONE`, `NEW`, `NEW_CHANGED`, `NEW_CHANGED_DELETED` — see the [FSx API reference](https://docs.aws.amazon.com/fsx/latest/APIReference/API_CreateFileSystemLustreConfiguration.html).

On Persistent-2, the newer approach is to **not** use `s3ImportPath` on the SC and instead attach a [Data Repository Association](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-dra-linked-data-repo.html) to the file system after creation. DRAs are more flexible (multiple prefixes per FS, richer batching), but the CSI driver doesn't create them for you — you'd do it out of band, e.g. via a Terraform `aws_fsx_data_repository_association` resource keyed off the CSI-created file system's ID.

### 5.6 Deletion semantics

With the default `reclaimPolicy: Delete`:

1. User does `kubectl delete pvc fsx-claim`.
2. The external CSI provisioner calls `DeleteVolume` on the controller.
3. The controller calls `fsx:DeleteFileSystem` on the underlying `fs-xxxx`.
4. The FSx file system is destroyed. **All data is lost.**

This is fine for scratch, catastrophic for persistent training checkpoints. Explicit choices:

- Set `reclaimPolicy: Retain` on the SC. On PVC deletion, the PV moves to `Released` state and the FSx file system stays. Manual cleanup required later.
- Add a finalizer + admission policy that requires an annotation on the PVC before deletion is allowed.

There is **no `deleteOnDrain` option** on FSx CSI as there is on some other CSI drivers — retention is entirely governed by K8s reclaim policy plus your own guardrails.

---

## 6. Static provisioning

Use when the file system already exists — created by Terraform, click-ops, another team, or a previous cluster — and you want K8s to mount it without owning its lifecycle.

### 6.1 Find the file system's identifiers

```bash
aws fsx describe-file-systems --file-system-ids fs-0199e5a63bd90f796 \
  --query 'FileSystems[0].{Id:FileSystemId,DNS:DNSName,Mount:LustreConfiguration.MountName,SG:VpcId,SubnetIds:SubnetIds}'
```

You need three things:

- **FileSystemId** — e.g. `fs-0199e5a63bd90f796`. Becomes `spec.csi.volumeHandle`.
- **DNSName** — e.g. `fs-0199e5a63bd90f796.fsx.us-east-1.amazonaws.com`. Becomes `volumeAttributes.dnsname`.
- **MountName** — a short random suffix like `fsx` or `abcdef01`. Becomes `volumeAttributes.mountname`. It is **not** the same as the FS ID and **not** the same as the DNS name — this is the piece people miss.

### 6.2 The PersistentVolume

From the [upstream static example](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/blob/master/examples/kubernetes/static_provisioning/specs/pv.yaml):

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: fsx-pv
spec:
  capacity:
    storage: 1200Gi
  volumeMode: Filesystem
  accessModes:
    - ReadWriteMany
  mountOptions:
    - flock
  persistentVolumeReclaimPolicy: Retain
  csi:
    driver: fsx.csi.aws.com
    volumeHandle: fs-0199e5a63bd90f796
    volumeAttributes:
      dnsname: fs-0199e5a63bd90f796.fsx.us-east-1.amazonaws.com
      mountname: fsx
```

Points:

- `persistentVolumeReclaimPolicy: Retain` is the only sane choice for static PVs — you don't want K8s deleting a file system it didn't create. Note the driver's `DeleteVolume` for a static PV will attempt to delete the file system on `Delete`/`Recycle` policies. Don't do it.
- `capacity.storage` is only used by K8s for accounting / matching claims. Lustre doesn't enforce it. Set it to the actual provisioned size.
- `spec.claimRef` can be pre-populated to bind this PV to one specific PVC namespace/name, preventing an unrelated PVC from binding it first. Recommended for shared clusters.

### 6.3 The claim

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: fsx-claim
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: ""     # explicit empty string: bind to a pre-provisioned PV
  resources:
    requests:
      storage: 1200Gi
  volumeName: fsx-pv       # optional but recommended for deterministic binding
```

`storageClassName: ""` (empty string) is the correct way to tell K8s "do not use a StorageClass, only bind to pre-provisioned PVs". Omitting the field entirely will fall back to the cluster's default SC and can trigger a wasteful dynamic provisioning.

### 6.4 The Pod

Identical to the dynamic case:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: fsx-app
spec:
  containers:
    - name: app
      image: amazonlinux:2
      command: ["/bin/sh"]
      args: ["-c", "while true; do echo $(date -u) >> /data/out.txt; sleep 5; done"]
      volumeMounts:
        - name: persistent-storage
          mountPath: /data
  volumes:
    - name: persistent-storage
      persistentVolumeClaim:
        claimName: fsx-claim
```

### 6.5 Zone pinning for static PVs

Because FSx (except Persistent-2 Intelligent-Tiering) is single-AZ, add a `nodeAffinity` clause so K8s schedules consumer pods only into that AZ:

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: fsx-pv
spec:
  # ... as above ...
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: topology.kubernetes.io/zone
              operator: In
              values: ["us-east-1a"]
```

Without this, a pod could be scheduled to a node in a zone where the FSx ENIs aren't reachable and the mount will hang until it times out (`connect: no route to host` in `dmesg`).

---

## 7. Mount options and Lustre client tuning

The CSI driver runs `mount -t lustre <MGSNIDs>:/<mountname> <target> -o <opts>` under the hood. Anything in `mountOptions` on the PV/SC is forwarded. Common, and important:

- **`flock`** — enable cluster-wide POSIX file locks. Required for `sqlite`, `logging.handlers.WatchedFileHandler`, most training checkpoint utilities, `torch.distributed.FileStore`, `git`, `apt` locks, etc.
- **`localflock`** — the default. Cross-node `flock(2)` calls silently no-op. Use this only if you're 100% sure the workload doesn't need cross-node locking.
- **`noatime`** / **`relatime`** — skip access-time updates. Big win on training-data reads.
- **`user_xattr`** — enable extended attributes; needed by some tools.

For **very high** throughput workloads (multi-GB/s per node), also consider Lustre client-side tuning applied on the node itself (not the mount options). AWS documents a recommended set of `sysctl`/`lctl` tweaks that should be applied via **node user-data / launch template**, not by the CSI driver:

- [Optimize FSx for Lustre performance on nodes (non-EFA)](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi-tuning-non-efa.html)
- [Optimize FSx for Lustre performance on nodes (EFA)](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi-tuning-efa.html)

Typical changes there include `lru_size`, RPC concurrency (`osc.*.max_rpcs_in_flight`), and disabling client-side write-back cache for streaming workloads.

---

## 8. Volume expansion

The driver implements `ControllerExpandVolume`. To expand a dynamically-provisioned FSx PV:

1. Ensure the SC has `allowVolumeExpansion: true`.
2. Patch the PVC: `kubectl patch pvc fsx-claim -p '{"spec":{"resources":{"requests":{"storage":"9600Gi"}}}}'`.
3. The controller calls `fsx:UpdateFileSystem` with the new storage capacity.
4. FSx grows the file system online.

Two constraints from FSx:

- You can only grow, never shrink.
- Growth follows the same rounding rules as § 4.2. Asking for `9600Gi` on a SSD PERSISTENT_2 will actually round to the nearest legal size.

Note that Lustre online expansion adds OSTs — throughput scales linearly with capacity, so a growth is a throughput bump as well as a capacity bump.

---

## 9. Wiring FSx into a training/inference workflow

A pattern we use in `inference-clusters`:

1. **Central data lake in S3** — training corpora, model weights, tokenizer artifacts.
2. **One or more FSx file systems per cluster** — pre-warmed cache for the S3 data lake, plus a shared checkpoint volume.
3. **Data Repository Associations** attached out-of-band via Terraform, linking specific S3 prefixes to specific mount points inside the FSx file system.
4. **Static PVs** in Kubernetes referencing those file systems by ID.

Why static and not dynamic:

- The FSx file system is a **cluster-level, cross-workload** resource. Its lifecycle should not be tied to any one PVC.
- Data Repository Associations, backups, and the KMS key are declared in Terraform. Coupling them to a K8s SC would introduce state duplication.
- Multiple namespaces / teams can bind their own PVCs to the same underlying volume via `claimRef` slicing.

For scratch use — say, a hyperparameter sweep that needs its own /scratch — dynamic provisioning with `reclaimPolicy: Delete` is fine.

---

## 10. Observability & troubleshooting

### 10.1 Standard checks

```bash
# Driver pods
kubectl -n kube-system get pods -l app.kubernetes.io/name=aws-fsx-csi-driver

# Controller logs (create/delete/expand)
kubectl -n kube-system logs deploy/fsx-csi-controller -c fsx-plugin --tail=200

# Node logs (mount troubles)
kubectl -n kube-system logs ds/fsx-csi-node -c fsx-plugin --tail=200

# CSI events on the PVC
kubectl describe pvc fsx-claim
```

### 10.2 Common failure modes

| Symptom | Root cause | Fix |
| --- | --- | --- |
| PVC stuck `Pending` for 15+ min, controller logs show `AccessDenied` | Controller SA lacks `fsx:CreateFileSystem` | Attach `AmazonFSxFullAccess` (or scoped policy). Verify with `kubectl exec deploy/fsx-csi-controller -- env \| grep AWS_`. |
| PVC `Pending` with `iam:CreateServiceLinkedRole` denied | First-time FSx use in the account | Add `iam:CreateServiceLinkedRole` with `iam:AWSServiceName=fsx.amazonaws.com` to the controller role. |
| Pod stuck `ContainerCreating`, `MountVolume.SetUp failed for volume … Warning: mount options: no such device` | Lustre kernel module not loaded on the node | Use an AL2 or Bottlerocket AMI recent enough to bundle the Lustre client. Or install `lustre-client` at boot via user-data. |
| Pod scheduled to wrong AZ, mount hangs then times out | No topology hint on the PV | Add `spec.nodeAffinity` pinning to the FSx zone (§ 6.5). |
| Mount succeeds but writes look corrupted | `flock` not enabled | Add `mountOptions: [flock]` to the PV/SC and recycle the pod. |
| EKS add-on install fails with `resource conflict` | An older Helm/kustomize install already present | `--resolve-conflicts OVERWRITE` on `create-addon`. |
| Controller pod repeatedly restarts, unable to reach FSx API | Cluster subnets have no route to FSx API endpoint | Ensure the cluster has a VPC endpoint or NAT to `fsx.<region>.amazonaws.com`. |

### 10.3 Metrics

The chart exposes Prometheus metrics on the CSI controller and node pods when `controller.enableMetrics=true`. Scrape them at `:9808/metrics`. Add a `ServiceMonitor` via `controller.serviceMonitor.enabled=true` if you run Prom Operator.

---

## 11. Terraform sketch

A minimal Terraform sketch of the underlying dependencies. Not a production module — this is to make the surface area concrete.

```hcl
# Security group for FSx clients (i.e. worker nodes) to talk to file servers
resource "aws_security_group" "fsx" {
  name        = "fsx-lustre-clients"
  vpc_id      = var.vpc_id
  description = "FSx for Lustre clients"

  ingress {
    from_port       = 988
    to_port         = 988
    protocol        = "tcp"
    security_groups = [var.cluster_sg_id]
  }
  ingress {
    from_port       = 1018
    to_port         = 1023
    protocol        = "tcp"
    security_groups = [var.cluster_sg_id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Persistent-2 SSD file system
resource "aws_fsx_lustre_file_system" "training" {
  deployment_type             = "PERSISTENT_2"
  storage_capacity            = 4800   # TiB * 1024 GiB; must respect rounding rules
  per_unit_storage_throughput = 500
  subnet_ids                  = [var.private_subnet_id]        # single AZ
  security_group_ids          = [aws_security_group.fsx.id]
  kms_key_id                  = var.kms_key_arn
  file_system_type_version    = "2.15"
  data_compression_type       = "LZ4"

  tags = {
    Team = "ml-platform"
    Env  = "prod"
  }
}

# IRSA role for the CSI controller
data "aws_iam_policy" "fsx_full" {
  arn = "arn:aws:iam::aws:policy/AmazonFSxFullAccess"
}

module "fsx_csi_irsa" {
  source            = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  role_name         = "AmazonEKS_FSx_CSI_DriverRole"
  attach_fsx_policy = false
  role_policy_arns  = { fsx = data.aws_iam_policy.fsx_full.arn }
  oidc_providers = {
    main = {
      provider_arn               = var.oidc_provider_arn
      namespace_service_accounts = ["kube-system:fsx-csi-controller-sa"]
    }
  }
}

# Static PV pointing at the file system (rendered into the cluster's manifests)
locals {
  fsx_pv = {
    apiVersion = "v1"
    kind       = "PersistentVolume"
    metadata   = { name = "fsx-training-pv" }
    spec = {
      capacity                      = { storage = "4800Gi" }
      volumeMode                    = "Filesystem"
      accessModes                   = ["ReadWriteMany"]
      mountOptions                  = ["flock", "noatime"]
      persistentVolumeReclaimPolicy = "Retain"
      csi = {
        driver       = "fsx.csi.aws.com"
        volumeHandle = aws_fsx_lustre_file_system.training.id
        volumeAttributes = {
          dnsname   = aws_fsx_lustre_file_system.training.dns_name
          mountname = aws_fsx_lustre_file_system.training.mount_name
        }
      }
      nodeAffinity = {
        required = {
          nodeSelectorTerms = [{
            matchExpressions = [{
              key      = "topology.kubernetes.io/zone"
              operator = "In"
              values   = [data.aws_subnet.private.availability_zone]
            }]
          }]
        }
      }
    }
  }
}
```

The static PV manifest can then be rendered into your GitOps repo or applied by the Terraform Kubernetes provider.

---

## 12. Design decisions and gotchas — a checklist

Things you almost certainly want to decide up front, because retrofitting them mid-flight is unpleasant.

- **Deployment type.** Default to `PERSISTENT_2` for anything long-lived. Only pick `SCRATCH_2` if you're OK losing the file system on hardware failure. `PERSISTENT_1` is legacy — new file systems should not use it unless you specifically need the HDD tier.
- **Throughput class.** Match `perUnitStorageThroughput` to your workload. Training ingest at ~1 GB/s per GPU needs `"1000"`. A shared config volume can live on `"125"`.
- **Data compression.** Turn `dataCompressionType: LZ4` on for training-data volumes. It's transparent, CPU-cheap, and saves real money.
- **KMS.** Persistent file systems support customer-managed KMS keys — use one and audit it. `kmsKeyId` on the SC is passed through verbatim.
- **Backups.** Persistent file systems support automatic backups. If you're using dynamic provisioning, set `automaticBackupRetentionDays` on the SC or you'll get the default (7 days).
- **Reclaim policy.** For any persistent workload, set `reclaimPolicy: Retain` on the SC or on the pre-provisioned PV. Test the deletion path in a sandbox before trusting it.
- **Mount options.** Always include `flock`. Consider `noatime`. Never use `localflock` if any pod on any other node might touch the same files.
- **AZ pinning.** Pin PVs to the file system's zone with `nodeAffinity`, or make sure your node pool's subnet selector matches the SC's `subnetId`.
- **Security groups.** Open 988 and 1018–1023 TCP from your worker SG to the FSx SG. Get this wrong and you'll spend an afternoon reading `dmesg`.
- **IRSA vs Pod Identity.** For new clusters, use Pod Identity — one API call to associate role↔SA, no OIDC provider drift.
- **Add-on vs Helm.** For AWS-native EKS clusters, prefer the add-on — you get IRSA/Pod-Identity wiring, upgrade windows, and CloudFormation-managed lifecycle.
- **Fargate.** Not supported. FSx pods must land on EC2 or Auto Mode nodes.
- **Existing installations.** If migrating from a Helm install to the EKS add-on, use `--resolve-conflicts OVERWRITE` on `create-addon`, or `uninstall` the Helm release first (retaining the CRDs and PVs).

---

## 13. References

- [aws-fsx-csi-driver on GitHub](https://github.com/kubernetes-sigs/aws-fsx-csi-driver) — canonical source.
- [Install docs](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/blob/master/docs/install.md) — prerequisites, Helm, Kustomize, IAM policy.
- [Dynamic provisioning example](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/tree/master/examples/kubernetes/dynamic_provisioning) — parameter list, StorageClass, PVC, Pod.
- [Static provisioning example](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/tree/master/examples/kubernetes/static_provisioning) — PV / PVC / Pod.
- [S3 dynamic provisioning example](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/tree/master/examples/kubernetes/dynamic_provisioning_s3) — legacy DRA integration.
- [Helm chart values.yaml](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/blob/master/charts/aws-fsx-csi-driver/values.yaml) — full list of tunables.
- [EKS user guide: FSx CSI driver](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi.html) — top-level docs.
- [EKS user guide: Deploy the FSx CSI driver](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi-create.html) — step-by-step install, add-on and manual paths.
- [EKS user guide: AWS add-ons](https://docs.aws.amazon.com/eks/latest/userguide/workloads-add-ons-available-eks.html#add-ons-aws-fsx-csi-driver) — the `aws-fsx-csi-driver` add-on entry.
- [EKS user guide: Non-EFA performance tuning](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi-tuning-non-efa.html) and [EFA tuning](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi-tuning-efa.html).
- [FSx for Lustre User Guide: Deployment types](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html) — SCRATCH_1/2, PERSISTENT_1/2, storage classes, regions.
- [FSx for Lustre API: CreateFileSystemLustreConfiguration](https://docs.aws.amazon.com/fsx/latest/APIReference/API_CreateFileSystemLustreConfiguration.html) — the API the CSI driver's parameters map to.
- [FSx for Lustre: VPC security groups](https://docs.aws.amazon.com/fsx/latest/LustreGuide/limit-access-security-groups.html#fsx-vpc-security-groups) — port list.
- [FSx for Lustre: Data Repositories](https://docs.aws.amazon.com/fsx/latest/LustreGuide/fsx-data-repositories.html) — S3 DRA concepts.
- [AmazonFSxFullAccess managed policy](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonFSxFullAccess.html).
- [EKS Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html) — recommended IAM auth for new EKS clusters.
