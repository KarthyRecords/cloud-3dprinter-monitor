terraform {
  backend "s3" {
    bucket       = "terracotta-terraform-state"
    key          = "cluster/terraform.tfstate"
    region       = "eu-west-1"
    use_lockfile = true
  }
}