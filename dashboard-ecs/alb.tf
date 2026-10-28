# Only the ALB is reachable from outside, and only from CloudFront's origin-facing addresses
resource "aws_security_group" "alb" {
  name        = "${var.name}-alb"
  description = "Dashboard load balancer"
  vpc_id      = data.aws_vpc.main.id

  tags = { Name = "${var.name}-alb" }
}

# AWS keeps this list current. It counts as ~55 rules against the security group quota
# (default 60), so keep this group free of other inbound rules.
data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from CloudFront"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront.id
}

# The tasks only accept traffic from the ALB
resource "aws_security_group" "tasks" {
  name        = "${var.name}-tasks"
  description = "Dashboard Fargate tasks"
  vpc_id      = data.aws_vpc.main.id

  tags = { Name = "${var.name}-tasks" }
}

resource "aws_vpc_security_group_ingress_rule" "tasks_from_alb" {
  security_group_id            = aws_security_group.tasks.id
  description                  = "App port from the ALB"
  ip_protocol                  = "tcp"
  from_port                    = var.container_port
  to_port                      = var.container_port
  referenced_security_group_id = aws_security_group.alb.id
}

resource "aws_vpc_security_group_egress_rule" "alb_to_tasks" {
  security_group_id            = aws_security_group.alb.id
  description                  = "To the tasks"
  ip_protocol                  = "tcp"
  from_port                    = var.container_port
  to_port                      = var.container_port
  referenced_security_group_id = aws_security_group.tasks.id
}

# Tasks reach ECR, CloudWatch Logs, DynamoDB and S3 over HTTPS through the NAT gateway
resource "aws_vpc_security_group_egress_rule" "tasks_https" {
  security_group_id = aws_security_group.tasks.id
  description       = "HTTPS to AWS APIs"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_lb" "dashboard" {
  name                       = var.name
  load_balancer_type         = "application"
  internal                   = false
  security_groups            = [aws_security_group.alb.id]
  subnets                    = [for s in data.aws_subnet.public : s.id]
  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "dashboard" {
  name                 = var.name
  vpc_id               = data.aws_vpc.main.id
  port                 = var.container_port
  protocol             = "HTTP"
  target_type          = "ip" # required for Fargate (awsvpc)
  deregistration_delay = 10

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

# HTTP between CloudFront and the ALB; viewers always use HTTPS on CloudFront.
# Anything that did not come through CloudFront (no secret header) gets a 403.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.dashboard.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "text/plain"
      message_body = "forbidden"
      status_code  = "403"
    }
  }
}

resource "aws_lb_listener_rule" "from_cloudfront" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 10

  condition {
    http_header {
      http_header_name = "X-Origin-Verify"
      values           = [random_password.origin_verify.result]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.dashboard.arn
  }
}
