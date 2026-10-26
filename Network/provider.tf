provider "aws" {
  region = "eu-west-1"
}

terraform {
  backend "s3" {
    bucket       = "terracotta-terraform-state"
    key          = "terraform.tfstate"
    region       = "eu-west-1"
    use_lockfile = true
  }
}