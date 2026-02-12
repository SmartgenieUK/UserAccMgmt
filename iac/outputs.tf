output "resource_group_name" {
  description = "Resource group name."
  value       = azurerm_resource_group.this.name
}

output "postgres_server_fqdn" {
  description = "PostgreSQL Flexible Server FQDN."
  value       = azurerm_postgresql_flexible_server.this.fqdn
}

output "postgres_database_name" {
  description = "PostgreSQL database name."
  value       = azurerm_postgresql_flexible_server_database.authdb.name
}

output "key_vault_name" {
  description = "Azure Key Vault name."
  value       = azurerm_key_vault.this.name
}

output "redis_hostname" {
  description = "Azure Redis hostname."
  value       = azurerm_redis_cache.this.hostname
}

output "application_insights_connection_string" {
  description = "Application Insights connection string."
  value       = azurerm_application_insights.this.connection_string
  sensitive   = true
}

output "log_analytics_workspace_id" {
  description = "Log Analytics workspace resource ID."
  value       = azurerm_log_analytics_workspace.this.id
}

output "container_apps_environment_id" {
  description = "Container Apps Environment resource ID when enabled."
  value       = try(azurerm_container_app_environment.this[0].id, null)
}

output "redis_primary_access_key" {
  description = "Redis primary access key."
  value       = azurerm_redis_cache.this.primary_access_key
  sensitive   = true
}
