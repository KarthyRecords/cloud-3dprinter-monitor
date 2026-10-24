
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = "teracotta"
  cluster_version = "1.37"

  vpc_id     = data.aws_vpc.main.id
  subnet_ids = [data.aws_subnet.subnet1.id, data.aws_subnet.subnet2.id]

  # No EC2 node groups: system pods run on Fargate, runner nodes come from Karpenter
  cluster_addons = {
    coredns = {
      configuration_values = jsonencode({ computeType = "Fargate" })
    }
    kube-proxy = {}
    vpc-cni    = {}
  }

  fargate_profiles = {
    system = {
      selectors = [
        { namespace = "kube-system", labels = { "k8s-app" = "kube-dns" } },
        { namespace = "kube-system", labels = { "app.kubernetes.io/name" = "karpenter" } },
        { namespace = "cert-manager" },
        { namespace = "actions-runner-system", labels = { "app.kubernetes.io/name" = "actions-runner-controller" } },
      ]
    }
  }

  enable_irsa                              = true
  enable_cluster_creator_admin_permissions = true

  # lets the infra-runner pod authenticate to the cluster API (helm/kubectl providers).
  # The ARN is built by hand because irsa_role depends on this module (it would be a cycle).
  access_entries = {
    infra_runner = {
      principal_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/teracotta-infra-runner"
      policy_associations = {
        admin = {
          policy_arn   = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = { type = "cluster" }
        }
      }
    }
  }
  node_security_group_tags = {
    "karpenter.sh/discovery" = "teracotta"
  }

  cluster_endpoint_public_access = true
  cluster_endpoint_public_access_cidrs = concat(
    ["90.196.238.220/32"],
    [for nat in data.aws_nat_gateway.main : "${nat.public_ip}/32"],
  )
}


module "irsa_role" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name = "teracotta-infra-runner"

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["actions-runner-system:infra-runner"]
    }
  }

  role_policy_arns = {
    s3       = "arn:aws:iam::aws:policy/AmazonS3FullAccess"
    dynamodb = "arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess"
    stack    = aws_iam_policy.infra_runner_stack.arn
  }
}

data "aws_caller_identity" "current" {}

# What `terraform apply` of this stack needs: the EKS cluster, its IAM roles and
# OIDC provider, Karpenter's queue and rules, the cluster log group, the secrets KMS key,
# security groups and subnet tags, and the GitHub token parameter.
data "aws_iam_policy_document" "infra_runner_stack" {
  statement {
    sid    = "ManageClusterStack"
    effect = "Allow"
    actions = [
      "eks:*",
      "iam:*",
      "ec2:*",
      "sqs:*",
      "events:*",
      "logs:*",
      "kms:*",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "ReadGithubToken"
    effect    = "Allow"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = ["arn:aws:ssm:eu-west-1:${data.aws_caller_identity.current.account_id}:parameter/github/eks/token"]
  }
}

resource "aws_iam_policy" "infra_runner_stack" {
  name   = "teracotta-infra-runner-stack"
  policy = data.aws_iam_policy_document.infra_runner_stack.json
}


resource "helm_release" "cert_manager" {
  name             = "cert-manager"
  repository       = "https://charts.jetstack.io"
  chart            = "cert-manager"
  version          = "v1.13.0"
  namespace        = "cert-manager"
  create_namespace = true

  set = [
    {
      name  = "installCRDs"
      value = "true"
    }
  ]

  depends_on = [module.eks]
}

resource "helm_release" "arc" {
  name             = "actions-runner-controller"
  repository       = "https://actions-runner-controller.github.io/actions-runner-controller"
  chart            = "actions-runner-controller"
  namespace        = "actions-runner-system"
  create_namespace = true

  set = [
    {
      name  = "authSecret.create"
      value = "true"
    }
  ]

  set_sensitive = [
    {
      name  = "authSecret.github_token"
      value = data.aws_ssm_parameter.github_token.value
    }
  ]

  depends_on = [helm_release.cert_manager]
}

# Karpenter: runner nodes are provisioned on demand (see runners/karpenter-nodepool.yaml)
resource "aws_ec2_tag" "karpenter_discovery" {
  for_each    = toset([data.aws_subnet.subnet1.id, data.aws_subnet.subnet2.id])
  resource_id = each.value
  key         = "karpenter.sh/discovery"
  value       = module.eks.cluster_name
}

module "karpenter" {
  source  = "terraform-aws-modules/eks/aws//modules/karpenter"
  version = "~> 20.0"

  cluster_name = module.eks.cluster_name

  enable_irsa                     = true
  irsa_oidc_provider_arn          = module.eks.oidc_provider_arn
  irsa_namespace_service_accounts = ["kube-system:karpenter"]

  node_iam_role_additional_policies = {
    AmazonSSMManagedInstanceCore = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
  }
}

resource "helm_release" "karpenter" {
  name       = "karpenter"
  repository = "oci://public.ecr.aws/karpenter"
  chart      = "karpenter"
  version    = "1.6.0"
  namespace  = "kube-system"
  timeout    = 900
  atomic     = true

  set = [
    { name = "settings.clusterName", value = module.eks.cluster_name },
    { name = "settings.interruptionQueue", value = module.karpenter.queue_name },
    { name = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn", value = module.karpenter.iam_role_arn },
    { name = "replicas", value = "1" },
    { name = "controller.resources.requests.cpu", value = "250m" },
    { name = "controller.resources.requests.memory", value = "512Mi" },
  ]

  depends_on = [module.eks]
}

output "karpenter_node_role" {
  value = module.karpenter.node_iam_role_name
}
