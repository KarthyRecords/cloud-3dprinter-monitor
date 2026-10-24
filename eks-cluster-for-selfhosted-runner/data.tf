data "aws_vpc" "main" {
  filter {
    name   = "tag:Name"
    values = ["k-vpc"]
  }
}

data "aws_subnet" "subnet1" {
  filter {
    name   = "tag:Name"
    values = ["k-subnet1"]
  }
}

data "aws_subnet" "subnet2" {
  filter {
    name   = "tag:Name"
    values = ["k-subnet2"]
  }
}

data "aws_ssm_parameter" "github_token" {
  name = "/github/eks/token"
}
# NAT gateway public IPs: runner pods reach the cluster API through these
data "aws_nat_gateways" "main" {
  vpc_id = data.aws_vpc.main.id

  filter {
    name   = "state"
    values = ["available"]
  }
}

data "aws_nat_gateway" "main" {
  for_each = toset(data.aws_nat_gateways.main.ids)
  id       = each.value
}
