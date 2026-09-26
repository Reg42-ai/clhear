variable "subnet_ids" {
  type        = list(string)
  description = "Private subnet ids for the task."
}

variable "image_digest" {
  type        = string
  description = "Image by digest, for example ghcr.io/reg42-ai/clhear@sha256:..."
}

variable "security_group_id" {
  type        = string
  description = "Security group supplied by the caller. No public ingress is created here."
}

variable "command" {
  type        = list(string)
  description = "Container command: serve, worker, run, or migrate."
  default     = ["serve"]
}

variable "assign_public_ip" {
  type    = bool
  default = false
}

variable "service_token_secret_arn" {
  type        = string
  description = "Optional Secrets Manager ARN whose value is CLHEAR_SERVICE_TOKENS."
  default     = ""
}
