variable "project_name" { type = string }

resource "aws_dynamodb_table" "this" {
  name         = "${var.project_name}-incidents"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "dedup_key"
  range_key    = "created_at"

  attribute {
    name = "dedup_key"
    type = "S"
  }
  attribute {
    name = "created_at"
    type = "N"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = { Project = var.project_name }
}

output "table_name" { value = aws_dynamodb_table.this.name }
output "table_arn" { value = aws_dynamodb_table.this.arn }
