terraform {
  backend "s3" {
    bucket       = "terracotta-terraform-state"
    key          = "dashboard-ecs/terraform.tfstate"
    region       = "eu-west-1"
    use_lockfile = true
  }
}
