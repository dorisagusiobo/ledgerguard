terraform {
  required_version = ">= 1.10.0, < 2.0.0"
  backend "s3" {
    key          = "ledgerguard/8a.tfstate"
    encrypt      = true
    use_lockfile = true
  }
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.71.0"
    }
  }
}
provider "aws" {
  region = var.aws_region
  default_tags {
    tags = { Project = "ledgerguard", Part = "8a" }
  }
}
