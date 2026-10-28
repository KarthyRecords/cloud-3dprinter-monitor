resource "aws_cloudwatch_log_group" "dashboard" {
  name              = "/ecs/${var.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_cluster" "dashboard" {
  name = var.name

  setting {
    name  = "containerInsights"
    value = "disabled" # paid per metric; the log group is enough here
  }
}

resource "aws_ecs_task_definition" "dashboard" {
  family                   = var.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64" # the GitHub-hosted runner builds linux/amd64
  }

  container_definitions = jsonencode([{
    name      = "dashboard"
    image     = "${aws_ecr_repository.dashboard.repository_url}:latest"
    essential = true

    portMappings = [{ containerPort = var.container_port, protocol = "tcp" }]

    environment = [
      { name = "HOST", value = "0.0.0.0" },
      { name = "PORT", value = tostring(var.container_port) },
      { name = "AWS_REGION", value = local.region },
      { name = "DEVICE_ID", value = var.device_id },
      { name = "TELEMETRY_TABLE", value = var.telemetry_table },
      { name = "STATE_TABLE", value = var.state_table },
      { name = "DATA_BUCKET", value = local.data_bucket },
      { name = "AUTH_ENABLED", value = "1" },
      { name = "PUBLIC_URL", value = "https://${aws_cloudfront_distribution.dashboard.domain_name}" },
      { name = "COGNITO_DOMAIN", value = "https://${aws_cognito_user_pool_domain.dashboard.domain}.auth.${local.region}.amazoncognito.com" },
      { name = "COGNITO_CLIENT_ID", value = aws_cognito_user_pool_client.dashboard.id },
      { name = "COGNITO_USER_POOL_ID", value = aws_cognito_user_pool.dashboard.id },
    ]

    secrets = [
      { name = "COGNITO_CLIENT_SECRET", valueFrom = aws_ssm_parameter.client_secret.arn },
      { name = "SESSION_KEY", valueFrom = aws_ssm_parameter.session_key.arn },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.dashboard.name
        awslogs-region        = local.region
        awslogs-stream-prefix = "dashboard"
      }
    }
  }])
}

resource "aws_ecs_service" "dashboard" {
  name            = var.name
  cluster         = aws_ecs_cluster.dashboard.id
  task_definition = aws_ecs_task_definition.dashboard.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  health_check_grace_period_seconds = 60

  network_configuration {
    subnets          = [for s in data.aws_subnet.private : s.id]
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.dashboard.arn
    container_name   = "dashboard"
    container_port   = var.container_port
  }

  depends_on = [aws_lb_listener_rule.from_cloudfront]
}
