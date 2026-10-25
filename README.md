# 3D printer remote-monitoring web app

A hands-on DevOps/IoT project to create a cloud based dashboard for our FDM 3D printer:
1. IoT hardware:
- [Phase 1] Serial port connection to the printer enabling read/command capability (Anycubic Kobra 2 Neo, Marlin based OEM Firmware)
- [Phase 2] 2x ADXL345 accelerometers (on extruder and table), USB webcam connected via STM32 (Arduino Uno Q)
2. Cloud dashboard:
AWS EKS cluster built with Terraform, with GitHub Actions self-hosted runners on the cluster that deploy infrastructure and applications. 

<!-- STATUS:START -->
**Goal:** Goal not stated yet, add it to docs/status-notes.md  

**Where I am now:** The repository now contains CI workflows for runners, an EKS cluster configured for self‑hosted runners, and recent fixes to the repo‑agent code. The infrastructure is being prepared but core application deployment is not yet in place.  

**In progress**
- Adding runner workflow for infra‑runner and app‑runner.  
- Setting up the EKS cluster to host self‑hosted runners.  
- Applying fixes to the repo‑agent implementation.  

**Up next**
- Apply network configuration and provision the EKS cluster via the pipeline.  
- Deploy runners (infra‑runner, app‑runner) with separate IAM roles.  
- Build the application image and push it to ECR.  

**Done recently**
- Added runner workflow to the repository.  
- Configured EKS cluster for self‑hosted runner usage

_Last updated by the status agent on 2026-10-25 using openai/gpt-oss-120b._
<!-- STATUS:END -->

## Architecture

- **Network:** VPC, subnets and NAT (`Network/`)
- **Cluster:** EKS with managed node groups, IRSA and KMS secrets encryption (`eks-cluster/`)
- **Bootstrap:** S3 remote state and base resources (`bootstrap/`)
- **CI/CD:** GitHub Actions on self-hosted runners (infra-runner and app-runner) via ARC
- **App:** Python API, MQTT telemetry in, DynamoDB storage, Cognito auth, Cloudflare in front

## Repository layout

| Path | What it holds |
|---|---|
| `bootstrap/` | Terraform state bucket and first-time setup |
| `Network/` | VPC and networking |
| `eks-cluster/` | EKS cluster, node groups and IAM |
| `k8s/runners/` | Runner manifests (infra-runner, app-runner) |
| `.github/workflows/` | CI/CD and repo-agent workflows |
| `docs/` | Notes, including `status-notes.md` |

## Roadmap

- [ ] Bootstrap: remote state in S3
- [ ] Network and EKS cluster applied from the pipeline
- [ ] Runners deployed (infra-runner, app-runner) with separate IAM roles
- [ ] App image build and push to ECR
- [ ] Deploy the app to Kubernetes
- [ ] Cognito auth and Cloudflare in front
- [ ] Monitoring and alerts
- [ ] Simulated printer sending telemetry end to end

## How to run it

1. Prerequisites: AWS account, Terraform, kubectl, Helm and the GitHub CLI.
2. Bootstrap: `cd bootstrap && terraform init && terraform apply`
3. Network: `cd Network && terraform init && terraform apply`
4. Cluster: `cd eks-cluster && terraform init && terraform plan`
5. Runners: `kubectl apply -f k8s/runners/`

## Security notes

- No secrets, sensitive `.tfvars`, plan files or state are stored in this repo.
- Runner pods use IRSA roles scoped to what each one needs.
- Every change goes through a pull request.
