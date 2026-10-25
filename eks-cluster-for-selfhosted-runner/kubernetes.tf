# --- Karpenter: runner nodes (min 0, max 3 x t3.small) ---

resource "kubectl_manifest" "runner_node_class" {
  yaml_body = yamlencode({
    apiVersion = "karpenter.k8s.aws/v1"
    kind       = "EC2NodeClass"
    metadata   = { name = "runner" }
    spec = {
      amiSelectorTerms = [{ alias = "al2023@latest" }]
      role             = module.karpenter.node_iam_role_name
      subnetSelectorTerms = [
        { tags = { "karpenter.sh/discovery" = module.eks.cluster_name } }
      ]
      securityGroupSelectorTerms = [
        { tags = { "karpenter.sh/discovery" = module.eks.cluster_name } },
        # primary cluster SG, so nodes can reach Fargate pods (CoreDNS etc.)
        { tags = { "aws:eks:cluster-name" = module.eks.cluster_name } },
      ]
    }
  })

  depends_on = [helm_release.karpenter, aws_ec2_tag.karpenter_discovery]
}

resource "kubectl_manifest" "runner_node_pool" {
  yaml_body = yamlencode({
    apiVersion = "karpenter.sh/v1"
    kind       = "NodePool"
    metadata   = { name = "runner" }
    spec = {
      template = {
        metadata = { labels = { role = "runner" } }
        spec = {
          nodeClassRef = {
            group = "karpenter.k8s.aws"
            kind  = "EC2NodeClass"
            name  = "runner"
          }
          taints = [{ key = "dedicated", value = "runner", effect = "NoSchedule" }]
          requirements = [
            { key = "node.kubernetes.io/instance-type", operator = "In", values = ["t3.small"] },
            { key = "karpenter.sh/capacity-type", operator = "In", values = ["on-demand"] },
          ]
        }
      }
      limits = { cpu = "6" } # max 3 x t3.small (2 vCPU each)
      disruption = {
        consolidationPolicy = "WhenEmpty"
        consolidateAfter    = "2m" # back to 0 nodes when no runner pods
      }
    }
  })

  depends_on = [kubectl_manifest.runner_node_class]
}

# --- GitHub Actions infra runner ---

resource "kubectl_manifest" "infra_runner_sa" {
  yaml_body = yamlencode({
    apiVersion = "v1"
    kind       = "ServiceAccount"
    metadata = {
      name        = "infra-runner"
      namespace   = "actions-runner-system"
      annotations = { "eks.amazonaws.com/role-arn" = module.irsa_role.iam_role_arn }
    }
  })

  depends_on = [helm_release.arc]
}

# Use in a workflow with:  runs-on: [self-hosted, infra-runner]
resource "kubectl_manifest" "infra_runner" {
  yaml_body = yamlencode({
    apiVersion = "actions.summerwind.dev/v1alpha1"
    kind       = "RunnerDeployment"
    metadata = {
      name      = "infra-runner"
      namespace = "actions-runner-system"
    }
    spec = {
      replicas = 1
      template = {
        spec = {
          repository         = "karthyy19/cloud-3dprinter-monitor"
          labels             = ["infra-runner"]
          ephemeral          = true  # fresh pod per job, then deleted
          dockerEnabled      = false # no Docker daemon, so no privileged container
          serviceAccountName = "infra-runner"
          nodeSelector       = { role = "runner" }
          tolerations = [{
            key      = "dedicated"
            operator = "Equal"
            value    = "runner"
            effect   = "NoSchedule"
          }]
          resources = {
            requests = { cpu = "500m", memory = "1Gi" }
            limits   = { memory = "2Gi" }
          }
        }
      }
    }
  })

  depends_on = [kubectl_manifest.infra_runner_sa, kubectl_manifest.runner_node_pool]
}
