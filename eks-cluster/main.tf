
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = "teracotta"
  cluster_version = "1.37"

  vpc_id     = data.aws_vpc.main.id
  subnet_ids = [data.aws_subnet.subnet1.id, data.aws_subnet.subnet2.id]

  eks_managed_node_groups = {
    system = {
      instance_types = ["t3.medium"]
      min_size       = 1
      max_size       = 2
      desired_size   = 2
    }

    runner-nodes = {
      instance_types = ["t3.large"]
      min_size       = 0
      max_size       = 5
      desired_size   = 1

      taints = {
        dedicated = {
          key    = "dedicated"
          value  = "runner"
          effect = "NO_SCHEDULE"
        }
      }
    }
  }

  enable_irsa = true
}


module "irsa_role" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name = "teracotta-terraform-runner"

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["actions-runner-system:terraform-runner"]
    }
  }

  role_policy_arns = {
    s3       = "arn:aws:iam::aws:policy/AmazonS3FullAccess"
    dynamodb = "arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess"
  }
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