variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "cluster_name" {
  description = "EKS cluster name"
  type        = string
}

variable "db_password" {
  description = "RDS master password"
  type        = string
  sensitive   = true
}

variable "github_repo" {
  description = "GitHub repository allowed to assume the CI role, as owner/name"
  type        = string
  default     = "Akhilesh0013/GitHub-PR-Code-Reviewer"
}

# GitHub now issues OIDC subjects with immutable numeric owner and repo IDs
# appended, e.g. repo:owner@<owner_id>/name@<repo_id>. Both forms are trusted so
# the role keeps working whichever prefix the token carries.
variable "github_repo_immutable" {
  description = "Immutable form of github_repo, as owner@owner_id/name@repo_id"
  type        = string
  default     = "Akhilesh0013@146669224/GitHub-PR-Code-Reviewer@1359599011"
}

variable "environment" {
  description = "Deployment environment"
  type        = string
  default     = "staging"
}
