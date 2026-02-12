variable "subscription_id" {
  description = "Azure subscription ID where resources will be deployed."
  type        = string
}

variable "tenant_id" {
  description = "Azure Entra tenant ID used for authentication and resource configuration."
  type        = string
}

variable "location" {
  description = "Azure region for all resources."
  type        = string
  default     = "uksouth"
}

variable "env_name" {
  description = "Short environment name used in resource naming (example: dev, test, prod)."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9-]{2,12}$", var.env_name))
    error_message = "env_name must be 2-12 characters and contain only lowercase letters, numbers, and hyphens."
  }
}

variable "tags" {
  description = "Common tags applied to all resources."
  type        = map(string)
  default     = {}
}

variable "postgres_admin_username" {
  description = "PostgreSQL admin username."
  type        = string
  default     = "authpgadmin"
}

variable "postgres_sku" {
  description = "PostgreSQL Flexible Server SKU name."
  type        = string
  default     = "B_Standard_B1ms"
}

variable "postgres_storage_mb" {
  description = "PostgreSQL storage size in MB."
  type        = number
  default     = 32768
}

variable "postgres_public_network_access_enabled" {
  description = "Enable public network access to PostgreSQL. Set false when private networking is configured."
  type        = bool
  default     = true
}

variable "allow_azure_services" {
  description = "Allow Azure services access to PostgreSQL via firewall rule 0.0.0.0."
  type        = bool
  default     = true
}

variable "postgres_allowed_ips" {
  description = "List of IPv4 addresses allowed to connect to PostgreSQL when public access is enabled."
  type        = list(string)
  default     = []
}

variable "redis_sku" {
  description = "Azure Cache for Redis SKU."
  type        = string
  default     = "Basic"

  validation {
    condition     = contains(["Basic", "Standard", "Premium"], var.redis_sku)
    error_message = "redis_sku must be one of: Basic, Standard, Premium."
  }
}

variable "redis_capacity" {
  description = "Redis capacity size. For Basic/Standard, 0 means C0."
  type        = number
  default     = 0
}

variable "enable_container_apps_env" {
  description = "Deploy an Azure Container Apps Environment for hosting the FastAPI service later."
  type        = bool
  default     = true
}

variable "enable_storage_account" {
  description = "Deploy a Storage Account for future avatar/artifact assets."
  type        = bool
  default     = true
}
