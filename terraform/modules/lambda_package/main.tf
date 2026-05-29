variable "function_name" { type = string }
variable "source_dir" { type = string }
variable "requirements_file" { type = string }

locals {
  build_script = "${path.root}/scripts/build_lambda.sh"
  build_dir    = "${path.root}/.terraform/lambda-build/${var.function_name}"
  zip_path     = "${path.root}/.terraform/lambda-build/${var.function_name}.zip"
  package_hash = sha256(join("", concat(
    [for file in sort(fileset(var.source_dir, "**")) : filesha256("${var.source_dir}/${file}")],
    [filesha256(var.requirements_file), filesha256(local.build_script)],
  )))
}

resource "terraform_data" "build" {
  triggers_replace = {
    package_hash = local.package_hash
  }

  provisioner "local-exec" {
    command = "bash '${local.build_script}' '${var.source_dir}' '${local.build_dir}' '${var.requirements_file}'"
  }
}

data "archive_file" "zip" {
  depends_on  = [terraform_data.build]
  type        = "zip"
  source_dir  = local.build_dir
  output_path = local.zip_path
}

output "filename" {
  value = data.archive_file.zip.output_path
}

output "source_code_hash" {
  value = data.archive_file.zip.output_base64sha256
}
