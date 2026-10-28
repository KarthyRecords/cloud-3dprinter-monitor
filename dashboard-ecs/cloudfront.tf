# Public HTTPS entry point on the free *.cloudfront.net name (no domain needed).
# CloudFront talks plain HTTP to the ALB, so the ALB only accepts CloudFront's addresses
# AND requests that carry this secret header (see alb.tf).
resource "random_password" "origin_verify" {
  length  = 40
  special = false
}

# AWS managed policies, looked up by name so no ids are hardcoded
data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled" # the dashboard data is live
}

data "aws_cloudfront_origin_request_policy" "all_viewer" {
  name = "Managed-AllViewer" # forward cookies, query string (?code=) and headers to the app
}

resource "aws_cloudfront_distribution" "dashboard" {
  enabled         = true
  comment         = var.name
  price_class     = "PriceClass_100" # US/EU edges only, cheapest
  is_ipv6_enabled = true

  origin {
    origin_id   = "alb"
    domain_name = aws_lb.dashboard.dns_name

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }

    custom_header {
      name  = "X-Origin-Verify"
      value = random_password.origin_verify.result
    }
  }

  default_cache_behavior {
    target_origin_id         = "alb"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
    compress                 = true
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
    minimum_protocol_version       = "TLSv1"
  }
}
