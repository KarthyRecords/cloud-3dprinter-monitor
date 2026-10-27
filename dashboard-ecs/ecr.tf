resource "aws_ecr_repository" "dashboard" {
  name                 = var.name
  image_tag_mutability = "MUTABLE" # the service runs :latest; CI also pushes an immutable-style :<git sha> tag
  force_delete         = true      # lets `terraform destroy` remove the repo even with images in it

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

# keeps storage cost flat: only the newest images stay
resource "aws_ecr_lifecycle_policy" "dashboard" {
  repository = aws_ecr_repository.dashboard.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 5 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = { type = "expire" }
    }]
  })
}
