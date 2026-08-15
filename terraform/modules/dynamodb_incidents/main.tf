variable "project_name" { type = string }

resource "aws_dynamodb_table" "this" {
  # dedup_key must be the entire primary key: the signal collector's
  # conditional put relies on one item per dedup_key. A created_at range
  # key would make every put a distinct item and disable dedup.
  name         = "${var.project_name}-incidents"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "dedup_key"

  attribute {
    name = "dedup_key"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = { Project = var.project_name }
}

output "table_name" { value = aws_dynamodb_table.this.name }
output "table_arn" { value = aws_dynamodb_table.this.arn }
