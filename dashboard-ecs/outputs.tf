output "dashboard_url" {
  description = "Open this in a browser (only from an address in allowed_cidrs)"
  value       = "http://${aws_lb.dashboard.dns_name}"
}

output "ecr_repository_url" {
  value = aws_ecr_repository.dashboard.repository_url
}

output "ecs_cluster" {
  value = aws_ecs_cluster.dashboard.name
}

output "ecs_service" {
  value = aws_ecs_service.dashboard.name
}
