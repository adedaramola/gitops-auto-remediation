# Remote state: S3 backend with native lockfile-based locking (Terraform >= 1.10).
# The bucket is created out-of-band (encrypted, public access blocked):
#   aws s3api create-bucket --bucket <bucket> --region us-east-1
terraform {
  backend "s3" {
    bucket       = "gitops-auto-remediation-tfstate-4da300bc"
    key          = "gitops-auto-remediation/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}
